"""Phase 14.5 — minimal live equity-index trading shim (v0).

SINGLE-FILE EXPERIMENT, not the full architectural port. Intended to ship
fast at bounded risk: 1ct per trade, $5/day cap, KXINXU (SPX) only. Less
production-hardened than bin/live_1hr.py (REST-polled book, no WS),
acceptable given the small-cap risk profile and the standing rule
``[[live-small-sizing-over-paper-trading]]``.

WARNING: cutpoints are crypto-calibrated v1, NOT equity-recalibrated.
Score thresholds and gate boundaries may be miscalibrated for SPX. The
1ct/$5 cap protects against catastrophic loss but edge erosion is
possible until equity cutpoints are derived from observer data
(see _tmp_analysis/equity_strategy_design/v14_strategy_design.md).

Run:
    py -m kalshi_engine.bin.live_inxu_v0 --dry-run --duration-s 60
    py -m kalshi_engine.bin.live_inxu_v0
"""

from __future__ import annotations

import argparse
import asyncio
import math
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

import aiohttp

from kalshi_engine.config import MODELS_DIR, RAW_DIR
from kalshi_engine.core.equity import Equity, SPECS
from kalshi_engine.core.types import Action, Side
from kalshi_engine.execution.kalshi_client import KalshiClient
from kalshi_engine.feeds.alpaca_spot import (
    AlpacaSpotPoller, credentials_from_env,
)
from kalshi_engine.strategies.favorite_chase.models.phase4_cutpoints import (
    Phase4CutpointsModel,
)
from kalshi_engine.strategies.favorite_chase.state import FavoriteChaseState
from kalshi_engine.warehouse.adapters import LiveLogWriter
from kalshi_engine.warehouse.settlement import _iso_to_ms

DEFAULT_LOG_PATH = str(RAW_DIR / "live_logs" / "live_inxu_v0.jsonl")
FAV_CHASE_TRIGGER_DC = 750.0  # don't trade favorites < $0.75

# Phase 14.8 — KXINXU is a 1hr series. Reject any market whose
# close_ms - open_ms exceeds this cap, mirroring the 25h-cycle pollution
# defect found in KXBTCD on 2026-05-26.
MAX_INXU_CYCLE_MIN = 90


def _diag(msg: str) -> None:
    print(f"[diag] {msg}", file=sys.stderr, flush=True)


async def _refresh_until_markets_with_retry(shim, log, retry_seconds: int = 300,
                                              process_label: str = "live_inxu_v0",
                                              ) -> bool:
    """Phase 14.11 - sleep+retry until shim.refresh_markets() populates
    shim.markets. KXINXU has zero active markets between RTH-end ~21:00Z
    and next RTH ~14:30Z; under NSSM daemonization we want the process
    alive through that gap, not exit-and-restart-throttling.

    Returns True if cancelled (graceful shutdown), False if discovery
    succeeded normally.
    """
    retry_attempts = 0
    while True:
        await shim.refresh_markets()
        if shim.markets:
            return False
        retry_attempts += 1
        log.write({
            "kind": "no_markets_waiting",
            "process": process_label,
            "retry_attempts": retry_attempts,
            "next_retry_s": retry_seconds,
        })
        _diag(f"no markets discovered; retry in {retry_seconds}s "
              f"(attempt {retry_attempts})")
        try:
            await asyncio.sleep(retry_seconds)
        except asyncio.CancelledError:
            return True


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


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Phase 14.5 minimal live equity shim (KXINXU/SPX)")
    p.add_argument("--align-mode", default="5tier_v13b_equity_1ct_flat",
                   help="Sizing mode. Default 1ct flat for score>=4.")
    p.add_argument("--max-contracts", type=int, default=1,
                   help="Hard ceiling on contracts per trade. Default 1.")
    p.add_argument("--daily-cap-cents", type=int, default=500,
                   help="Daily realized-loss cap in cents. Default 500 ($5).")
    p.add_argument("--observe-times", default="30,40,50",
                   help="Minute marks into each cycle to evaluate (default 30,40,50).")
    p.add_argument("--max-favorite-cost-decicents", type=int, default=920,
                   help="MAX_FAV_COST cap in decicents. Default 920 ($0.92).")
    p.add_argument("--cutpoints-version", default="v1",
                   help="Phase 4 cutpoints version (v1 = crypto, NOT equity-recalibrated).")
    p.add_argument("--spot-poll-seconds", type=float, default=15.0,
                   help="Alpaca SPY poll interval during RTH (default 15s).")
    p.add_argument("--decision-poll-seconds", type=float, default=5.0,
                   help="Cycle/scan-window check interval (default 5s).")
    p.add_argument("--dry-run", action="store_true",
                   help="Log decisions; place no real orders.")
    p.add_argument("--duration-s", type=float, default=0.0,
                   help="0 = run forever; else exit after N seconds.")
    p.add_argument("--log-path", default=DEFAULT_LOG_PATH,
                   help="JSONL output path.")
    p.add_argument("--alpaca-credentials",
                   default=os.environ.get("ALPACA_CREDENTIALS_PATH", ""),
                   help="Path to Alpaca dotenv file.")
    return p.parse_args(argv)


# ---- bootstrapping spot history via Alpaca bars ------------------------

ALPACA_DATA_BASE = "https://data.alpaca.markets/v2"


async def bootstrap_spot_history(symbol: str, key_id: str, secret: str,
                                   minutes: int = 60) -> list[tuple[int, float]]:
    """Fetch the last `minutes` of 1-minute bars from Alpaca to seed the
    vol-30m buffer on boot. Returns list of (ts_ms, close_price)."""
    headers = {
        "APCA-API-KEY-ID": key_id,
        "APCA-API-SECRET-KEY": secret,
        "accept": "application/json",
    }
    now = datetime.now(tz=timezone.utc)
    start = now.timestamp() - minutes * 60
    start_iso = datetime.fromtimestamp(start, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    end_iso = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    url = f"{ALPACA_DATA_BASE}/stocks/{symbol}/bars"
    params = {"timeframe": "1Min", "start": start_iso, "end": end_iso,
              "limit": minutes + 5}
    timeout = aiohttp.ClientTimeout(total=10)
    out: list[tuple[int, float]] = []
    try:
        async with aiohttp.ClientSession(headers=headers, timeout=timeout) as s:
            async with s.get(url, params=params) as resp:
                if resp.status != 200:
                    return out
                data = await resp.json()
                for bar in (data.get("bars") or []):
                    ts_str = bar.get("t", "")
                    px = bar.get("c")
                    if not ts_str or px is None:
                        continue
                    try:
                        ts_clean = ts_str.replace("Z", "+00:00")
                        if "." in ts_clean:
                            head, tail = ts_clean.split(".", 1)
                            tz_idx = max(tail.find("+"), tail.find("-"))
                            if tz_idx == -1:
                                ts_clean = head
                            else:
                                ts_clean = f"{head}.{tail[:tz_idx][:6]}{tail[tz_idx:]}"
                        ts_ms = int(datetime.fromisoformat(ts_clean).timestamp() * 1000)
                        out.append((ts_ms, float(px)))
                    except (ValueError, AttributeError):
                        continue
    except (aiohttp.ClientError, asyncio.TimeoutError):
        pass
    return out


# ---- Kalshi REST helpers ------------------------------------------------

async def fetch_kxinxu_markets(client: KalshiClient) -> list[dict]:
    """Return the list of open KXINXU markets (raw payloads)."""
    return await client.list_markets(
        series_ticker="KXINXU", status="open", limit=200,
    )


async def fetch_orderbook(client: KalshiClient, ticker: str) -> dict:
    """Fetch a single market's orderbook via REST."""
    return await client._request("GET", f"/markets/{ticker}/orderbook")


def parse_orderbook(ob: dict) -> dict:
    """Extract top-of-book + mid prices in decicents from an orderbook
    payload. Returns a dict with yes_bid_dc, yes_ask_dc, no_bid_dc,
    no_ask_dc, yes_bid_size, no_bid_size, etc. All decicents are integers
    in [0, 1000]. Missing sides default to (yes_bid=0, yes_ask=1000)."""
    raw = ob.get("orderbook") or ob.get("orderbook_fp") or {}
    def _top(side_levels, ascending: bool):
        if not side_levels: return None, 0.0
        # Levels are [[price_str, size_str], ...]
        try:
            levels = [(float(p), float(s)) for p, s in side_levels]
        except (TypeError, ValueError):
            return None, 0.0
        levels.sort(key=lambda x: x[0], reverse=not ascending)
        # The orderbook here is in dollars per side. For YES it's "yes side
        # offers" (asks for buying YES). For NO same.
        p, sz = levels[0]
        return int(round(p * 1000)), sz
    yes_ask_dc, yes_ask_sz = _top(raw.get("yes_dollars"), ascending=True)
    no_ask_dc, no_ask_sz = _top(raw.get("no_dollars"), ascending=True)
    # YES bid = 1000 - NO ask; NO bid = 1000 - YES ask (Kalshi binary complement)
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


# ---- main shim ----------------------------------------------------------

class InxuShim:
    def __init__(self, args, log: LiveLogWriter, model: Phase4CutpointsModel,
                  client: KalshiClient, alpaca: AlpacaSpotPoller,
                  alp_key: str, alp_sec: str) -> None:
        self.args = args
        self.log = log
        self.model = model
        self.client = client
        self.alpaca = alpaca
        self.alp_key = alp_key
        self.alp_sec = alp_sec
        # Use SPX (the Equity enum value) as the FavoriteChaseState key.
        # The model's bps_thresholds dict has no SPX entry -> threshold=0 ->
        # bps gate effectively disabled. Documented in boot envelope warning.
        self.state = FavoriteChaseState(Equity.SPX.value)
        self.observe_minutes = tuple(int(x.strip())
                                      for x in args.observe_times.split(",")
                                      if x.strip())
        self.entered: set[str] = set()   # (ticker) — one entry per cycle
        self.evaluated: dict[str, set[int]] = {}  # ticker -> set of minute marks
        self.markets: dict[str, dict] = {}  # ticker -> {strike, open_ms, close_ms}
        self.daily_realized_cents = 0
        self._daily_start_utc_date: str | None = None

    def _check_daily_reset(self) -> None:
        today = datetime.now(tz=timezone.utc).date().isoformat()
        if self._daily_start_utc_date != today:
            self._daily_start_utc_date = today
            self.daily_realized_cents = 0

    def _daily_cap_breached(self) -> bool:
        self._check_daily_reset()
        return self.daily_realized_cents <= -abs(self.args.daily_cap_cents)

    async def refresh_markets(self) -> None:
        markets = await fetch_kxinxu_markets(self.client)
        new_count = 0
        skipped_long = 0
        for m in markets:
            t = m.get("ticker")
            strike = _strike_from_market(m)
            open_ms = _iso_to_ms(m.get("open_time"))
            close_ms = _iso_to_ms(m.get("close_time"))
            if not t or strike <= 0 or open_ms is None or close_ms is None:
                continue
            # Phase 14.8 — cycle-duration filter.
            dur_min = (close_ms - open_ms) / 60_000.0
            if dur_min > MAX_INXU_CYCLE_MIN:
                if t not in self.markets:
                    self.log.write({"kind": "discovery_skip_long_cycle",
                                     "series": "KXINXU", "ticker": t,
                                     "duration_minutes": dur_min,
                                     "cap_minutes": MAX_INXU_CYCLE_MIN})
                    skipped_long += 1
                continue
            if t not in self.markets:
                new_count += 1
                self.markets[t] = {"strike": strike, "open_ms": open_ms,
                                    "close_ms": close_ms}
                self.evaluated.setdefault(t, set())
        if new_count or skipped_long:
            self.log.write({"kind": "market_refresh",
                            "new_markets": new_count,
                            "skipped_long_cycle_count": skipped_long,
                            "total_markets": len(self.markets)})

    async def spot_poll_loop(self, deadline: Optional[float]) -> None:
        # Bootstrap history
        bars = await bootstrap_spot_history("SPY", self.alp_key, self.alp_sec,
                                              minutes=60)
        for ts_ms, px in bars:
            self.state.update_spot(SimpleNamespace(ts_ms=ts_ms, price=px))
        self.log.write({"kind": "spot_bootstrap",
                         "bars_loaded": len(bars),
                         "earliest_ts_ms": bars[0][0] if bars else None,
                         "latest_ts_ms": bars[-1][0] if bars else None})

        while True:
            if deadline and time.time() >= deadline:
                return
            if AlpacaSpotPoller.is_market_open():
                trade = await self.alpaca.get_last_trade("SPY", respect_rth=True)
                if trade is not None:
                    self.state.update_spot(SimpleNamespace(
                        ts_ms=trade.ts_ms, price=trade.price))
                    self.log.write({"kind": "spot_tick", "symbol": "SPY",
                                     "price": trade.price, "ts_ms": trade.ts_ms})
                else:
                    self.log.write({"kind": "spot_poll_skip", "reason": "alpaca_returned_none"})
            try:
                await asyncio.sleep(self.args.spot_poll_seconds)
            except asyncio.CancelledError:
                return

    def _current_window(self, ticker: str) -> int | None:
        meta = self.markets.get(ticker)
        if not meta:
            return None
        elapsed_min = (time.time() * 1000 - meta["open_ms"]) / 60_000.0
        for m in self.observe_minutes:
            if m <= elapsed_min < m + 0.5:  # 30s window
                return m
        return None

    async def evaluate_ticker(self, ticker: str, window_min: int) -> None:
        meta = self.markets[ticker]
        # Fetch the current book via REST
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
            fav_side, fav_mid = Side.YES, yes_mid
        else:
            fav_side, fav_mid = Side.NO, no_mid
        now_ms = int(time.time() * 1000)
        ts_utc = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc)
        diag_base = {
            "ticker": ticker, "side": fav_side.value,
            "favorite_mid_decicents": fav_mid,
            "strike": meta["strike"],
            "elapsed_min": (now_ms - meta["open_ms"]) / 60_000.0,
            "tau_min": (meta["close_ms"] - now_ms) / 60_000.0,
            "window_label": f"T+{window_min}",
            "utc_hour": ts_utc.hour,
            "yes_bid_dc": b["yes_bid_dc"], "yes_ask_dc": b["yes_ask_dc"],
            "no_bid_dc": b["no_bid_dc"], "no_ask_dc": b["no_ask_dc"],
            "yes_ask_sz": b["yes_ask_sz"], "no_ask_sz": b["no_ask_sz"],
        }

        # Hard gates
        if fav_mid < FAV_CHASE_TRIGGER_DC:
            self.log.write({"kind": "decision", "ticker": ticker,
                             "action": "skip", "side": fav_side.value,
                             "reason": f"fav_mid {fav_mid:.0f}dc < trigger {FAV_CHASE_TRIGGER_DC:.0f}dc",
                             "diagnostics": diag_base})
            return
        if fav_mid > self.args.max_favorite_cost_decicents:
            self.log.write({"kind": "decision", "ticker": ticker,
                             "action": "skip", "side": fav_side.value,
                             "reason": f"fav_mid {fav_mid:.0f}dc > MAX_FAV_COST {self.args.max_favorite_cost_decicents}dc",
                             "diagnostics": diag_base})
            return

        # V13b score via Phase4CutpointsModel — note bps gate is effectively
        # disabled because state.crypto='SPX' isn't in BPS_THRESHOLDS.
        decision = self.model.evaluate(
            state=self.state, ticker=ticker, side=fav_side,
            favorite_mid_decicents=fav_mid, strike=meta["strike"],
            now_ms=now_ms, close_ms=meta["close_ms"],
        )
        # Clip to max-contracts
        from dataclasses import replace as _replace
        if decision.action is Action.ENTER and decision.size > self.args.max_contracts:
            decision = _replace(decision, size=self.args.max_contracts)
        # Daily cap pre-check
        if decision.action is Action.ENTER and self._daily_cap_breached():
            self.log.write({"kind": "decision", "ticker": ticker,
                             "action": "skip", "side": fav_side.value,
                             "reason": f"daily_cap_breached realized={self.daily_realized_cents}c",
                             "diagnostics": diag_base})
            return

        # Log the decision (whether enter or skip)
        merged = {**diag_base, **(decision.diagnostics or {})}
        self.log.write({"kind": "decision", "ticker": ticker,
                         "action": decision.action.value,
                         "side": decision.side.value,
                         "size": decision.size,
                         "confidence": decision.confidence,
                         "reason": decision.reason,
                         "diagnostics": merged})

        if decision.action is Action.ENTER:
            self.entered.add(ticker)
            # Place a marketable-limit IOC at 99c
            if self.args.dry_run:
                self.log.write({"kind": "order_intent_dry_run",
                                 "ticker": ticker, "side": fav_side.value,
                                 "size": decision.size, "price_dc": 990})
                return
            try:
                r = await self.client.create_order(
                    ticker=ticker, side=fav_side.value, action="buy",
                    price_decicents=990, count=decision.size,
                )
                order_id = r.get("order_id") or r.get("id")
                self.log.write({"kind": "order_placed", "ticker": ticker,
                                 "side": fav_side.value, "size": decision.size,
                                 "price_dc": 990, "order_id": order_id,
                                 "raw": r})
            except Exception as exc:
                self.log.write({"kind": "order_error", "ticker": ticker,
                                 "error": repr(exc)})

    async def decision_loop(self, deadline: Optional[float]) -> None:
        last_refresh = 0.0
        while True:
            if deadline and time.time() >= deadline:
                return
            now = time.time()
            # Refresh markets every 5 min
            if now - last_refresh > 300:
                try:
                    await self.refresh_markets()
                except Exception as exc:
                    self.log.write({"kind": "market_refresh_error", "error": repr(exc)})
                last_refresh = now

            if AlpacaSpotPoller.is_market_open() and self.state.latest_spot() is not None:
                # Check each ticker for scan windows
                for ticker in list(self.markets.keys()):
                    if ticker in self.entered:
                        continue
                    window_min = self._current_window(ticker)
                    if window_min is None:
                        continue
                    fired = self.evaluated.setdefault(ticker, set())
                    if window_min in fired:
                        continue
                    fired.add(window_min)
                    try:
                        await self.evaluate_ticker(ticker, window_min)
                    except Exception as exc:
                        self.log.write({"kind": "evaluate_error",
                                         "ticker": ticker, "error": repr(exc)})

            try:
                await asyncio.sleep(self.args.decision_poll_seconds)
            except asyncio.CancelledError:
                return


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

    try:
        alp_key, alp_sec = credentials_from_env(args.alpaca_credentials or None)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    log = LiveLogWriter(args.log_path)
    cutpoints_path = MODELS_DIR / "phase4_cutpoints" / args.cutpoints_version / "cutpoints.json"
    if not cutpoints_path.exists():
        print(f"ERROR: cutpoints artifact not found: {cutpoints_path}",
              file=sys.stderr)
        return 3
    model = Phase4CutpointsModel(cutpoints_path=str(cutpoints_path),
                                   align_mode=args.align_mode)
    deadline = time.time() + args.duration_s if args.duration_s > 0 else None

    async with KalshiClient(api_key, pem_bytes) as client:
        async with AlpacaSpotPoller(alp_key, alp_sec) as alpaca:
            shim = InxuShim(args, log, model, client, alpaca, alp_key, alp_sec)
            cancelled = await _refresh_until_markets_with_retry(
                shim, log, retry_seconds=300, process_label="live_inxu_v0",
            )
            if cancelled:
                return 0
            log.write({
                "kind": "boot",
                "process": "live_inxu_v0",
                "warning": ("CRYPTO_CALIBRATED_CUTPOINTS — v1 cutpoints are "
                            "calibrated on crypto, NOT equity. Score "
                            "thresholds and gate boundaries may misfire "
                            "on SPX. 1ct/$5cap protects against catastrophic "
                            "loss; edge erosion possible until equity "
                            "cutpoints are derived."),
                "equity": Equity.SPX.value,
                "kalshi_series": SPECS[Equity.SPX].kalshi_series,
                "alpaca_symbol": SPECS[Equity.SPX].alpaca_symbol,
                "align_mode": args.align_mode,
                "cutpoints_version": args.cutpoints_version,
                "max_contracts": args.max_contracts,
                "daily_cap_cents": args.daily_cap_cents,
                "observe_times": list(shim.observe_minutes),
                "max_favorite_cost_decicents": args.max_favorite_cost_decicents,
                "spot_poll_seconds": args.spot_poll_seconds,
                "decision_poll_seconds": args.decision_poll_seconds,
                "dry_run": args.dry_run,
                "duration_s": args.duration_s,
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
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    sys.exit(main())
