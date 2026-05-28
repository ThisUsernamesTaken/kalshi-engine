"""Phase 14.16 — live commodity daily-ladder trading engine.

Favorite-chase on Kalshi's Pyth-settled commodity daily ladders. Reuses the
V13b scoring core (Phase4CutpointsModel + FavoriteChaseState) wrapped in a
NEW daily-window controller: the crypto entry window is "T+8..15 of a 15-min
cycle", which does not exist for a product that settles once a day at 5pm ET.
``commodity_daily.window.DailyWindow`` re-expresses the window as minutes
before close (WAITING -> ACTIVE -> POST_SETTLE).

Launch scope (2026-05-28): **GOLD only** at 1ct, $5/day per product, $10/day
total commodity exposure. BRENT is framework-supported but DATA-BLOCKED — its
exact Kalshi settlement feed (BRENTQ6) is not published on Pyth Hermes; only a
spot proxy (UKOILSPOT) is live, which would reintroduce a futures/spot basis
(see core/commodity.py). Brent is therefore ``live_enabled=False`` and excluded
from --commodities unless explicitly forced; even if forced, the Pyth poller
fails closed on its dead feed.

Spot source = Pyth Hermes (free, keyless, ~7s lag, the EXACT settlement
reference). Trigger book = Kalshi REST orderbook poll (daily cadence tolerates
it). Fail-closed on stale / wide-confidence Pyth — never score on bad data.
No exit/stop logic (per the no-stops-on-favorite-chase rule); 1ct bounds the
daily-hold downside at ~$0.92/trade.

Run:
    py -m kalshi_engine.bin.live_commodity --dry-run --duration-s 300
    py -m kalshi_engine.bin.live_commodity
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

from kalshi_engine.config import MODELS_DIR, RAW_DIR
from kalshi_engine.core.commodity import SPECS, Commodity, CommoditySpec, live_specs
from kalshi_engine.core.types import Action, Side
from kalshi_engine.execution.kalshi_client import KalshiClient
from kalshi_engine.feeds.pyth_spot import PythSpotPoller
from kalshi_engine.strategies.commodity_daily.window import (
    DailyWindow, DailyWindowState, active_observe_mark,
)
from kalshi_engine.strategies.favorite_chase.models.phase4_cutpoints import (
    Phase4CutpointsModel,
)
from kalshi_engine.strategies.favorite_chase.state import FavoriteChaseState
from kalshi_engine.warehouse.adapters import LiveLogWriter
from kalshi_engine.warehouse.settlement import _iso_to_ms

DEFAULT_LOG_PATH = str(RAW_DIR / "live_logs" / "live_commodity_kalshi_engine.jsonl")
FAV_CHASE_TRIGGER_DC = 750.0   # don't chase favorites < $0.75
BUY_PRICE_DECICENTS = 990      # marketable-IOC buy ceiling


def _diag(msg: str) -> None:
    print(f"[diag] {msg}", file=sys.stderr, flush=True)


def _read_env_file(path: str) -> dict[str, str]:
    out: dict[str, str] = {}
    text = Path(path).read_text(encoding="utf-8")
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _strike_from_market(m: dict) -> float:
    fs = m.get("floor_strike")
    if fs is not None:
        try:
            return float(fs)
        except (TypeError, ValueError):
            pass
    t = m.get("ticker") or ""
    idx = t.rfind("-T")
    if idx == -1:
        return 0.0
    try:
        return float(t[idx + 2:])
    except (TypeError, ValueError):
        return 0.0


def parse_orderbook(ob: dict) -> dict:
    """Top-of-book + complement bids in decicents from a Kalshi orderbook
    payload. Identical shape to the equity shim's parser: Kalshi returns
    per-side ask ladders ('yes_dollars' / 'no_dollars'); the YES bid is the
    NO-ask complement and vice versa. Missing sides default to bid=0, ask=1000."""
    raw = ob.get("orderbook") or ob.get("orderbook_fp") or {}

    def _top(side_levels, ascending: bool):
        if not side_levels:
            return None, 0.0
        try:
            levels = [(float(p), float(s)) for p, s in side_levels]
        except (TypeError, ValueError):
            return None, 0.0
        levels.sort(key=lambda x: x[0], reverse=not ascending)
        p, sz = levels[0]
        return int(round(p * 1000)), sz

    yes_ask_dc, yes_ask_sz = _top(raw.get("yes_dollars"), ascending=True)
    no_ask_dc, no_ask_sz = _top(raw.get("no_dollars"), ascending=True)
    yes_bid_dc = (1000 - no_ask_dc) if no_ask_dc is not None else 0
    no_bid_dc = (1000 - yes_ask_dc) if yes_ask_dc is not None else 0
    return {
        "yes_bid_dc": yes_bid_dc,
        "yes_ask_dc": yes_ask_dc if yes_ask_dc is not None else 1000,
        "no_bid_dc": no_bid_dc,
        "no_ask_dc": no_ask_dc if no_ask_dc is not None else 1000,
        "yes_ask_sz": yes_ask_sz,
        "no_ask_sz": no_ask_sz,
    }


def parse_minutes_marks(s: str) -> tuple[int, ...]:
    """Parse '60,45,30,20,15' (minutes-before-close marks) to a tuple."""
    return tuple(int(x.strip()) for x in s.split(",") if x.strip())


def resolve_commodities(names: str, force: bool) -> list[CommoditySpec]:
    """Map a --commodities CSV to specs. Skips products that are not
    live_enabled unless --force-disabled is set (they will still fail closed
    on a dead Pyth feed)."""
    out: list[CommoditySpec] = []
    for raw in names.split(","):
        n = raw.strip().upper()
        if not n:
            continue
        try:
            spec = SPECS[Commodity(n)]
        except (KeyError, ValueError):
            _diag(f"unknown commodity {n!r}; skipping")
            continue
        if not (spec.pyth_live and spec.live_enabled) and not force:
            _diag(f"{n} not live_enabled (pyth_live={spec.pyth_live}); "
                  f"skipping (use --force-disabled to include)")
            continue
        out.append(spec)
    return out


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Phase 14.16 live commodity daily-ladder engine (Pyth-settled)")
    p.add_argument("--commodities", default="GOLD",
                   help="CSV of commodities to trade (default GOLD; BRENT is "
                        "data-blocked and skipped unless --force-disabled).")
    p.add_argument("--force-disabled", action="store_true",
                   help="Include commodities flagged not-live (still fails "
                        "closed on a dead Pyth feed). Default off.")
    p.add_argument("--align-mode", default="5tier_v13b_commodity_1ct_flat",
                   help="Sizing mode. Default commodity 1ct flat (score>=4).")
    p.add_argument("--max-contracts", type=int, default=1,
                   help="Hard ceiling on contracts per trade. Default 1.")
    p.add_argument("--daily-cap-cents", type=int, default=500,
                   help="Per-commodity daily entry-premium cap in cents "
                        "(=max daily loss). Default 500 ($5).")
    p.add_argument("--total-daily-cap-cents", type=int, default=1000,
                   help="Aggregate daily cap across ALL commodities in cents. "
                        "Default 1000 ($10).")
    p.add_argument("--window-open-minutes", type=float, default=60.0,
                   help="Entry window opens this many minutes before close.")
    p.add_argument("--window-close-minutes", type=float, default=10.0,
                   help="Entry window shuts this many minutes before close.")
    p.add_argument("--observe-times", default="60,45,30,20,15",
                   help="Minutes-BEFORE-close marks to evaluate (default dense).")
    p.add_argument("--max-favorite-cost-decicents", type=int, default=920,
                   help="MAX_FAV_COST cap in decicents. Default 920 ($0.92).")
    p.add_argument("--cutpoints-version", default="commodity_v1",
                   help="Cutpoints artifact version (commodity_v1 carries "
                        "per-product bps_thresholds).")
    p.add_argument("--pyth-max-stale-s", type=float, default=60.0,
                   help="Skip if the Pyth update is older than this (fail-closed).")
    p.add_argument("--pyth-conf-bps-ceiling", type=float, default=25.0,
                   help="Skip when Pyth confidence interval exceeds this many "
                        "bps of price (wide-conf data-quality gate).")
    p.add_argument("--time-of-day-skip", default="disabled",
                   choices=["enabled", "disabled"],
                   help="Commodity TOD skip hook. Default DISABLED (no "
                        "validated bad-hours window yet).")
    p.add_argument("--spot-poll-seconds", type=float, default=10.0,
                   help="Pyth poll interval (default 10s).")
    p.add_argument("--decision-poll-seconds", type=float, default=5.0,
                   help="Window/scan check interval (default 5s).")
    p.add_argument("--status-log-seconds", type=float, default=30.0,
                   help="How often to emit a window_status heartbeat (default 30s).")
    p.add_argument("--dry-run", action="store_true",
                   help="Log decisions; place no real orders.")
    p.add_argument("--duration-s", type=float, default=0.0,
                   help="0 = run forever; else exit after N seconds.")
    p.add_argument("--log-path", default=DEFAULT_LOG_PATH, help="JSONL output path.")
    return p.parse_args(argv)


# ---- Kalshi REST helpers ------------------------------------------------

async def fetch_orderbook(client: KalshiClient, ticker: str) -> dict:
    return await client._request("GET", f"/markets/{ticker}/orderbook")


# ---- main engine --------------------------------------------------------

class CommodityShim:
    def __init__(self, args, log: LiveLogWriter, model: Phase4CutpointsModel,
                 client: KalshiClient, pyth: PythSpotPoller,
                 specs: list[CommoditySpec]) -> None:
        self.args = args
        self.log = log
        self.model = model
        self.client = client
        self.pyth = pyth
        self.specs = {s.commodity: s for s in specs}
        self.window = DailyWindow(open_minutes=args.window_open_minutes,
                                  close_minutes=args.window_close_minutes)
        self.observe_marks = parse_minutes_marks(args.observe_times)
        # Per-commodity rolling state + latest Pyth price.
        self.state: dict[Commodity, FavoriteChaseState] = {
            c: FavoriteChaseState(c.value) for c in self.specs
        }
        self.latest_pyth: dict[Commodity, object] = {}
        # ticker -> {strike, open_ms, close_ms, commodity}
        self.markets: dict[str, dict] = {}
        self.entered: set[str] = set()             # one entry per ticker (strike)
        self.evaluated: dict[str, set[int]] = {}   # ticker -> fired marks
        self.daily_spend_cents: dict[Commodity, int] = {c: 0 for c in self.specs}
        self.daily_spend_total_cents = 0
        self._daily_start_utc_date: str | None = None
        self._last_status_log = 0.0
        self.stop = asyncio.Event()   # set by a shutdown signal; loops drain promptly

    async def _sleep_or_stop(self, seconds: float) -> None:
        """Sleep, but wake immediately if a shutdown was requested."""
        try:
            await asyncio.wait_for(self.stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    # -- daily reset ------------------------------------------------------
    def _check_daily_reset(self) -> None:
        today = datetime.now(tz=timezone.utc).date().isoformat()
        if self._daily_start_utc_date != today:
            self._daily_start_utc_date = today
            for c in self.daily_spend_cents:
                self.daily_spend_cents[c] = 0
            self.daily_spend_total_cents = 0

    def _cap_blocks(self, commodity: Commodity, cost_cents: int) -> str | None:
        """Return a reason string if entering would breach a cap, else None."""
        self._check_daily_reset()
        proj_c = self.daily_spend_cents[commodity] + cost_cents
        if proj_c > self.args.daily_cap_cents:
            return (f"per-commodity cap: {commodity.value} spend "
                    f"{self.daily_spend_cents[commodity]}c + {cost_cents}c > "
                    f"{self.args.daily_cap_cents}c")
        proj_t = self.daily_spend_total_cents + cost_cents
        if proj_t > self.args.total_daily_cap_cents:
            return (f"total commodity cap: {self.daily_spend_total_cents}c + "
                    f"{cost_cents}c > {self.args.total_daily_cap_cents}c")
        return None

    def _record_spend(self, commodity: Commodity, cost_cents: int) -> None:
        self.daily_spend_cents[commodity] += cost_cents
        self.daily_spend_total_cents += cost_cents

    # -- market discovery -------------------------------------------------
    async def refresh_markets(self) -> None:
        new_count = 0
        for commodity, spec in self.specs.items():
            try:
                markets = await self.client.list_markets(
                    series_ticker=spec.kalshi_series, status="open", limit=200)
            except Exception as exc:
                self.log.write({"kind": "market_refresh_error",
                                "series": spec.kalshi_series, "error": repr(exc)})
                continue
            for m in markets:
                t = m.get("ticker")
                strike = _strike_from_market(m)
                open_ms = _iso_to_ms(m.get("open_time"))
                close_ms = _iso_to_ms(m.get("close_time"))
                if not t or strike <= 0 or close_ms is None:
                    continue
                if t not in self.markets:
                    new_count += 1
                    self.markets[t] = {
                        "strike": strike, "open_ms": open_ms,
                        "close_ms": close_ms, "commodity": commodity,
                    }
                    self.evaluated.setdefault(t, set())
        # Prune markets that have closed (keep dicts bounded across days).
        now_ms = int(time.time() * 1000)
        stale = [t for t, meta in self.markets.items()
                 if meta["close_ms"] is not None and meta["close_ms"] < now_ms - 3_600_000]
        for t in stale:
            self.markets.pop(t, None)
            self.evaluated.pop(t, None)
            self.entered.discard(t)
        if new_count or stale:
            self.log.write({"kind": "market_refresh", "new_markets": new_count,
                            "pruned": len(stale), "total_markets": len(self.markets)})

    # -- spot polling -----------------------------------------------------
    async def spot_poll_loop(self, deadline: Optional[float]) -> None:
        # Bootstrap the vol buffer from Pyth Benchmarks 1-min history.
        for commodity, spec in self.specs.items():
            bars = await self.pyth.bootstrap_history(spec.pyth_symbol, minutes=60)
            for ts_ms, px in bars:
                self.state[commodity].update_spot(
                    SimpleNamespace(ts_ms=ts_ms, price=px))
            self.log.write({"kind": "spot_bootstrap", "commodity": commodity.value,
                            "symbol": spec.pyth_symbol, "bars_loaded": len(bars)})

        while not self.stop.is_set():
            if deadline and time.time() >= deadline:
                return
            for commodity, spec in self.specs.items():
                px = await self.pyth.get_latest(spec.pyth_feed_id)
                if px is None:
                    self.log.write({"kind": "spot_poll_skip",
                                    "commodity": commodity.value,
                                    "reason": "pyth_failed_closed (stale/zero/unreachable)",
                                    "feed_id": spec.pyth_feed_id})
                    continue
                self.latest_pyth[commodity] = px
                self.state[commodity].update_spot(
                    SimpleNamespace(ts_ms=px.publish_time_ms, price=px.price))
                self.log.write({"kind": "spot_tick", "commodity": commodity.value,
                                "symbol": spec.pyth_symbol, "price": px.price,
                                "conf": px.conf, "conf_bps": px.conf_bps,
                                "pyth_ts_ms": px.publish_time_ms})
            try:
                await self._sleep_or_stop(self.args.spot_poll_seconds)
            except asyncio.CancelledError:
                return

    # -- evaluation -------------------------------------------------------
    async def evaluate_ticker(self, ticker: str, mark: int) -> None:
        meta = self.markets[ticker]
        commodity: Commodity = meta["commodity"]
        spec = self.specs[commodity]
        state = self.state[commodity]
        pyth = self.latest_pyth.get(commodity)
        try:
            ob = await fetch_orderbook(self.client, ticker)
        except Exception as exc:
            self.log.write({"kind": "orderbook_error", "ticker": ticker,
                            "error": repr(exc)})
            return
        b = parse_orderbook(ob)
        yes_mid = (b["yes_bid_dc"] + b["yes_ask_dc"]) / 2.0
        no_mid = (b["no_bid_dc"] + b["no_ask_dc"]) / 2.0
        if yes_mid >= no_mid:
            fav_side, fav_mid, fav_ask_dc = Side.YES, yes_mid, b["yes_ask_dc"]
        else:
            fav_side, fav_mid, fav_ask_dc = Side.NO, no_mid, b["no_ask_dc"]
        now_ms = int(time.time() * 1000)
        ts_utc = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc)
        mtc = (meta["close_ms"] - now_ms) / 60_000.0
        strike_spacing_bps = (spec.strike_spacing_usd / pyth.price * 1e4
                              if pyth is not None and pyth.price else None)
        diag_base = {
            "ticker": ticker, "commodity": commodity.value,
            "side": fav_side.value, "favorite_mid_decicents": fav_mid,
            "strike": meta["strike"], "minutes_to_close": mtc,
            "observe_mark": mark, "utc_hour": ts_utc.hour,
            "yes_bid_dc": b["yes_bid_dc"], "yes_ask_dc": b["yes_ask_dc"],
            "no_bid_dc": b["no_bid_dc"], "no_ask_dc": b["no_ask_dc"],
            "yes_ask_sz": b["yes_ask_sz"], "no_ask_sz": b["no_ask_sz"],
            "pyth_price": getattr(pyth, "price", None),
            "pyth_conf": getattr(pyth, "conf", None),
            "pyth_conf_bps": getattr(pyth, "conf_bps", None),
            "pyth_ts_ms": getattr(pyth, "publish_time_ms", None),
            "strike_spacing_bps": strike_spacing_bps,
        }

        def skip(reason: str) -> None:
            self.log.write({"kind": "decision", "ticker": ticker,
                            "action": "skip", "side": fav_side.value,
                            "reason": reason, "diagnostics": diag_base})

        if pyth is None:
            skip("no live Pyth price (fail-closed)")
            return
        # Wide-confidence data-quality gate (Pyth-native, no crypto analogue).
        if pyth.conf_bps > self.args.pyth_conf_bps_ceiling:
            skip(f"pyth_conf_bps {pyth.conf_bps:.2f} > ceiling "
                 f"{self.args.pyth_conf_bps_ceiling}")
            return
        if fav_mid < FAV_CHASE_TRIGGER_DC:
            skip(f"fav_mid {fav_mid:.0f}dc < trigger {FAV_CHASE_TRIGGER_DC:.0f}dc")
            return
        if fav_mid > self.args.max_favorite_cost_decicents:
            skip(f"fav_mid {fav_mid:.0f}dc > MAX_FAV_COST "
                 f"{self.args.max_favorite_cost_decicents}dc")
            return

        decision = self.model.evaluate(
            state=state, ticker=ticker, side=fav_side,
            favorite_mid_decicents=fav_mid, strike=meta["strike"],
            now_ms=now_ms, close_ms=meta["close_ms"])
        from dataclasses import replace as _replace
        if decision.action is Action.ENTER and decision.size > self.args.max_contracts:
            decision = _replace(decision, size=self.args.max_contracts)

        # Entry-premium cap precheck (per-commodity $5/day + $10/day total).
        cost_cents = 0
        if decision.action is Action.ENTER:
            ask_for_cost = min(int(fav_ask_dc), BUY_PRICE_DECICENTS)
            cost_cents = round(decision.size * ask_for_cost / 10)
            blocked = self._cap_blocks(commodity, cost_cents)
            if blocked:
                merged = {**diag_base, **(decision.diagnostics or {}),
                          "est_cost_cents": cost_cents}
                self.log.write({"kind": "decision", "ticker": ticker,
                                "action": "skip", "side": fav_side.value,
                                "reason": f"daily_cap_block: {blocked}",
                                "diagnostics": merged})
                return

        merged = {**diag_base, **(decision.diagnostics or {}),
                  "est_cost_cents": cost_cents}
        self.log.write({"kind": "decision", "ticker": ticker,
                        "action": decision.action.value, "side": decision.side.value,
                        "size": decision.size, "confidence": decision.confidence,
                        "reason": decision.reason, "diagnostics": merged})

        if decision.action is not Action.ENTER:
            return

        self.entered.add(ticker)
        if self.args.dry_run:
            self._record_spend(commodity, cost_cents)
            self.log.write({"kind": "order_intent_dry_run", "ticker": ticker,
                            "side": fav_side.value, "size": decision.size,
                            "price_dc": BUY_PRICE_DECICENTS,
                            "est_cost_cents": cost_cents,
                            "commodity_spend_cents": self.daily_spend_cents[commodity],
                            "total_spend_cents": self.daily_spend_total_cents})
            return
        try:
            r = await self.client.place_limit_order(
                ticker=ticker, side=fav_side.value, action="buy",
                price_decicents=BUY_PRICE_DECICENTS, count=decision.size)
            self._record_spend(commodity, cost_cents)
            order_id = r.get("order_id") or r.get("id")
            self.log.write({"kind": "order_placed", "ticker": ticker,
                            "side": fav_side.value, "size": decision.size,
                            "price_dc": BUY_PRICE_DECICENTS, "order_id": order_id,
                            "est_cost_cents": cost_cents,
                            "commodity_spend_cents": self.daily_spend_cents[commodity],
                            "total_spend_cents": self.daily_spend_total_cents,
                            "raw": r})
        except Exception as exc:
            self.entered.discard(ticker)
            self.log.write({"kind": "order_error", "ticker": ticker,
                            "error": repr(exc)})

    def _maybe_status_log(self) -> None:
        now = time.time()
        if now - self._last_status_log < self.args.status_log_seconds:
            return
        self._last_status_log = now
        now_ms = int(now * 1000)
        per_commodity = []
        for commodity, spec in self.specs.items():
            # Nearest upcoming market for this commodity.
            mkts = [(t, m) for t, m in self.markets.items()
                    if m["commodity"] is commodity and m["close_ms"] is not None]
            nearest = min(mkts, key=lambda kv: kv[1]["close_ms"], default=None)
            px = self.latest_pyth.get(commodity)
            entry = {"commodity": commodity.value,
                     "spot": getattr(px, "price", None),
                     "spend_cents": self.daily_spend_cents[commodity]}
            if nearest is not None:
                meta = nearest[1]
                entry["state"] = self.window.state(now_ms, meta["close_ms"]).value
                entry["minutes_to_close"] = round(
                    (meta["close_ms"] - now_ms) / 60_000.0, 1)
                entry["n_markets"] = sum(1 for _, m in mkts)
            else:
                entry["state"] = "no_markets"
            per_commodity.append(entry)
        self.log.write({"kind": "window_status",
                        "total_spend_cents": self.daily_spend_total_cents,
                        "commodities": per_commodity})

    async def decision_loop(self, deadline: Optional[float]) -> None:
        last_refresh = 0.0
        while not self.stop.is_set():
            if deadline and time.time() >= deadline:
                return
            now = time.time()
            if now - last_refresh > 300:
                await self.refresh_markets()
                last_refresh = now
            now_ms = int(now * 1000)
            for ticker in list(self.markets.keys()):
                if ticker in self.entered:
                    continue
                meta = self.markets[ticker]
                if self.window.state(now_ms, meta["close_ms"]) is not DailyWindowState.ACTIVE:
                    continue
                commodity = meta["commodity"]
                if self.state[commodity].latest_spot() is None:
                    continue
                mark = active_observe_mark(now_ms, meta["close_ms"], self.observe_marks)
                if mark is None:
                    continue
                fired = self.evaluated.setdefault(ticker, set())
                if mark in fired:
                    continue
                fired.add(mark)
                try:
                    await self.evaluate_ticker(ticker, mark)
                except Exception as exc:
                    self.log.write({"kind": "evaluate_error", "ticker": ticker,
                                    "error": repr(exc)})
            self._maybe_status_log()
            try:
                await self._sleep_or_stop(self.args.decision_poll_seconds)
            except asyncio.CancelledError:
                return


def _install_signal_handlers(shim: "CommodityShim") -> None:
    """Request graceful shutdown on SIGINT/SIGTERM/SIGBREAK so a manual Ctrl+C
    (or NSSM stop) drains the loops and exits cleanly. Uses the asyncio loop
    handler where supported, falling back to ``signal.signal`` on Windows
    (where ``add_signal_handler`` is unimplemented for the Proactor loop)."""
    import signal as _signal
    loop = asyncio.get_running_loop()

    def _request_stop() -> None:
        _diag("shutdown signal received; draining loops")
        shim.stop.set()

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(_signal, name, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, _request_stop)
        except (NotImplementedError, RuntimeError, ValueError):
            try:
                _signal.signal(
                    sig, lambda *_: loop.call_soon_threadsafe(_request_stop))
            except (ValueError, OSError):
                pass


async def _amain(args: argparse.Namespace) -> int:
    _diag("amain entered")
    key_path = os.environ.get("KALSHI_API_KEY_PATH")
    if not key_path or not Path(key_path).exists():
        print("ERROR: KALSHI_API_KEY_PATH missing", file=sys.stderr)
        return 2
    creds = _read_env_file(key_path)
    api_key = creds.get("KALSHI_API_KEY")
    pem_path = creds.get("KALSHI_PRIVATE_KEY_PATH")
    if not api_key or not pem_path or not Path(pem_path).exists():
        print("ERROR: bad Kalshi credentials", file=sys.stderr)
        return 2
    pem_bytes = Path(pem_path).read_bytes()

    specs = resolve_commodities(args.commodities, args.force_disabled)
    if not specs:
        print("ERROR: no tradeable commodities resolved from "
              f"--commodities {args.commodities!r}", file=sys.stderr)
        return 3

    cutpoints_path = (MODELS_DIR / "phase4_cutpoints"
                      / args.cutpoints_version / "cutpoints.json")
    if not cutpoints_path.exists():
        print(f"ERROR: cutpoints artifact not found: {cutpoints_path}",
              file=sys.stderr)
        return 3
    model = Phase4CutpointsModel(
        cutpoints_path=str(cutpoints_path), align_mode=args.align_mode,
        time_of_day_skip=(args.time_of_day_skip == "enabled"))

    log = LiveLogWriter(args.log_path)
    deadline = time.time() + args.duration_s if args.duration_s > 0 else None

    async with KalshiClient(api_key, pem_bytes) as client:
        async with PythSpotPoller(max_stale_s=args.pyth_max_stale_s) as pyth:
            shim = CommodityShim(args, log, model, client, pyth, specs)
            _install_signal_handlers(shim)
            await shim.refresh_markets()
            log.write({
                "kind": "boot", "process": "live_commodity",
                "warning": ("CRYPTO_REGIME_CALIBRATED_CUTPOINTS — commodity_v1 "
                            "carries real per-product bps_thresholds and Pyth IS "
                            "the exact settlement source (no SPY/SPX-style basis), "
                            "but vol/bb_div bands remain crypto-calibrated. "
                            "1ct/$5-day cap bounds the forward-test risk until "
                            "commodity recalibration."),
                "commodities": [s.commodity.value for s in specs],
                "kalshi_series": [s.kalshi_series for s in specs],
                "pyth_feeds": {s.commodity.value: s.pyth_feed_id for s in specs},
                "align_mode": args.align_mode,
                "cutpoints_version": args.cutpoints_version,
                "max_contracts": args.max_contracts,
                "daily_cap_cents": args.daily_cap_cents,
                "total_daily_cap_cents": args.total_daily_cap_cents,
                "window_open_minutes": args.window_open_minutes,
                "window_close_minutes": args.window_close_minutes,
                "observe_marks": list(shim.observe_marks),
                "max_favorite_cost_decicents": args.max_favorite_cost_decicents,
                "pyth_max_stale_s": args.pyth_max_stale_s,
                "pyth_conf_bps_ceiling": args.pyth_conf_bps_ceiling,
                "time_of_day_skip": args.time_of_day_skip,
                "dry_run": args.dry_run, "duration_s": args.duration_s,
                "log_path": str(args.log_path),
                "markets_registered": len(shim.markets),
            })
            spot_task = asyncio.create_task(shim.spot_poll_loop(deadline))
            decision_task = asyncio.create_task(shim.decision_loop(deadline))
            try:
                await asyncio.gather(spot_task, decision_task)
            finally:
                for t in (spot_task, decision_task):
                    t.cancel()
                    try:
                        await t
                    except (asyncio.CancelledError, Exception):
                        pass
    return 0


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        return asyncio.run(_amain(args))
    except KeyboardInterrupt:
        _diag("interrupted (Ctrl+C); shutting down cleanly")
        return 0


if __name__ == "__main__":
    sys.exit(main())
