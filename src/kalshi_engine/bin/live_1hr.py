"""Live runner entry point for the 1hr Hourglass trader (Phase 13.1).

Wires the Kalshi REST + WS feeds, the spot feed, the HourglassTraderStrategy
with the Phase4CutpointsModel (default align_mode=5tier_v13b_1to3_flat,
hard ceiling 3 contracts), the risk envelope ($10/day default), and the
live execution adapter. Loads the API key from ``KALSHI_API_KEY_PATH``.

In ``--dry-run`` mode no orders are placed; every decision is still logged
and the rest of the pipeline (feeds, model, envelope, reconcile) runs end
to end.

    python -m kalshi_engine.bin.live_1hr --dry-run --duration-s 60

Runs ALONGSIDE the existing 15m engine — same Kalshi API key, separate log
file (default ``$KALSHI_ENGINE_WAREHOUSE/raw/live_logs/live_hourglass_trader.jsonl``),
separate process, independent $10/day risk envelope.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from collections import defaultdict
from pathlib import Path

from kalshi_engine.config import RAW_DIR
from kalshi_engine.core.events import (
    BookEvent,
    LifecycleEvent,
    SettlementEvent,
    SpotEvent,
    TradeEvent,
)
from kalshi_engine.core.types import Crypto
from kalshi_engine.execution.kalshi_client import KalshiClient
from kalshi_engine.execution.kalshi_live import LiveExecution
from kalshi_engine.feeds.kalshi_ws import KalshiWebSocketFeed
from kalshi_engine.feeds.spot_ws import SpotFeed
from kalshi_engine.risk.envelope import RiskEnvelope, RiskState
from kalshi_engine.strategies.favorite_chase.models.phase4_cutpoints import (
    Phase4CutpointsModel,
)
from kalshi_engine.strategies.hourglass_ladder.ladder import (
    DeepItmSweeperStrategy,
    LadderStrategy,
)
from kalshi_engine.strategies.hourglass_trader import HourglassTraderStrategy
from kalshi_engine.strategies.hourglass_trader.trader import (
    BTC_DOWNSIZE_CONTRACTS,
    BTC_DOWNSIZE_DNORM,
    BTC_MAX_FAV_ASK_DECICENTS,
    BTC_SIZE_TILT_CONTRACTS,
    BTC_SIZE_TILT_MINUTE,
    BTC_SIZE_TILT_SCORE,
)
from kalshi_engine.warehouse.adapters import LiveLogWriter
from kalshi_engine.warehouse.settlement import _iso_to_ms

DEFAULT_LOG_PATH = RAW_DIR / "live_logs" / "live_hourglass_trader.jsonl"

# Phase 14.8 — maximum cycle duration for "1hr" markets. Anything longer
# than this is treated as a non-1hr market (e.g. 25h daily cycle) and
# rejected from discovery. 90 minutes gives 30-min slack over the natural
# 60-min cycle to absorb any Kalshi rounding/timing quirks.
MAX_1HR_CYCLE_MIN = 90


def _strike_from_market(m: dict) -> float:
    """Best-effort strike extraction.

    Kalshi 1hr crypto markets fall into two schemas:
    - KXBTCD/KXETHD/KXSOLD/KXXRPD: ``floor_strike`` is a float in the payload.
    - KXDOGED (and likely KXHYPED, KXBNBD): ``floor_strike`` is null; the
      strike is encoded in the ticker as the segment after the last ``-T``
      (e.g. ``KXDOGED-26MAY2617-T0.1949999`` -> 0.1949999).
    Returns 0.0 if no strike can be recovered (discovery skips those).
    """
    fs = m.get("floor_strike")
    if fs is not None:
        try:
            return float(fs)
        except (TypeError, ValueError):
            pass
    ticker = m.get("ticker") or ""
    idx = ticker.rfind("-T")
    if idx == -1:
        return 0.0
    try:
        return float(ticker[idx + 2:])
    except (TypeError, ValueError):
        return 0.0

SERIES_1HR_FOR_CRYPTO = {
    Crypto.BTC: "KXBTCD",
    Crypto.ETH: "KXETHD",
    Crypto.SOL: "KXSOLD",
    Crypto.XRP: "KXXRPD",
    # NB: Crypto.DOGE has no Kalshi 1hr digital series (KXDOGED does not
    # exist). Discovery for DOGE will produce zero markets; the engine
    # warns at boot and otherwise no-ops for DOGE.
}


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


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="kalshi_engine 1hr live trader")
    p.add_argument("--strategy", default="favorite_chase",
                   choices=["favorite_chase"])
    p.add_argument("--model", default="phase4_cutpoints",
                   choices=["phase4_cutpoints"])
    p.add_argument("--cryptos", default="BTC,ETH",
                   help="comma-separated crypto symbols. Default BTC+ETH — "
                        "only KXBTCD (463ct @ 50c) and KXETHD (217ct @ 50c) "
                        "have deep enough books for reliable 7-10ct fills. "
                        "KXSOLD / KXXRPD / KXDOGED / KXHYPED / KXBNBD are "
                        "all 1-2ct deep — use observer instead of live "
                        "trader for those.")
    p.add_argument("--align-mode", default="5tier_v13b_7_10_10",
                   choices=["disabled", "2tier", "3tier", "5tier",
                            "5tier_v13b", "5tier_v13b_s2", "5tier_v13b_h1h4",
                            "5tier_v13b_1to3_flat", "5tier_v13b_10_flat",
                            "5tier_v13b_7_10_10"],
                   help="Phase-13.2 default: 5tier_v13b_7_10_10 (T6) — same "
                        "V13b score formula, skip<4, then 7ct (score 4-5) / "
                        "10ct (score 5-6) / 10ct (>=6). T6 asymmetric variant "
                        "from the 1hr observer tier sweep: captures 95%% of "
                        "T3's all-flat-10 PnL with -$4.90 worst trade vs "
                        "-$7.00 for T3. Earlier conservative variants "
                        "(5tier_v13b_1to3_flat) remain selectable.")
    p.add_argument("--max-contracts", type=int, default=10,
                   help="Hard ceiling on contracts per trade. Default 10 — "
                        "matches the T6 sizing schedule's max tier. The "
                        "HourglassTraderStrategy clips any larger decision "
                        "to this value (defense-in-depth above align-mode). "
                        "Worst single-trade loss at 10ct * 92c = $9.20 "
                        "(~92%% of $10/day cap — cap binds after one max-tier "
                        "loss).")
    p.add_argument("--per-crypto-max-contracts", default="",
                   help="Phase 13.6 — per-crypto sizing override. Format: "
                        "'BTC=10,ETH=1'. Each entry caps that crypto's per-trade "
                        "size BEFORE the global --max-contracts ceiling. Empty "
                        "string (default) disables the override and falls back "
                        "to --max-contracts uniformly. Use for asset-class-tier "
                        "risk reduction (e.g. ETH at 1ct while BTC stays at 10ct "
                        "while ETH-specific gates are being calibrated).")
    p.add_argument("--per-crypto-align-mode", default="",
                   help="Phase 14.3 — per-crypto align-mode override. Format: "
                        "'BTC=5tier_v13b_7_10_10,ETH=5tier_v13b_1to3_ramp'. Each "
                        "entry RESHAPES that crypto's sizing schedule (not just "
                        "a cap). Missing crypto falls back to the global "
                        "--align-mode. Use for asset-class-tier sizing schemes "
                        "(e.g. ETH on a 1/2/3 ramp by score while BTC keeps T6 "
                        "7/10/10). Each value must be a registered align mode.")
    p.add_argument("--daily-cap-cents", type=int, default=1000,
                   help="Daily realized-loss cap in cents. Default 1000 ($10). "
                        "Independent from the 15m engine's separate $10 cap.")
    p.add_argument("--cutpoints-version", default="v1",
                   help="Cutpoints artifact version (cutpoints.json file).")
    p.add_argument("--trigger-minutes", default="15,30,50",
                   help="Comma-separated minute marks into each cycle where "
                        "the trader evaluates and may enter. Default 15,30,50 — "
                        "earlier T+15 scan catches favorites before they pin "
                        "near 99c (observer data: 95%%+ of T+50 favorites are "
                        "already at 99c where Kalshi fees eat the edge). "
                        "T+45 is intentionally absent per the observer "
                        "tier sweep (-$5.79).")
    p.add_argument("--skip-hours", default="13",
                   help="Comma-separated UTC hours to skip entries in. Default "
                        "13 — the observer sweep flagged 13Z as catastrophic "
                        "(-$84.17 on n=16). Use empty string '' for none.")
    p.add_argument("--max-favorite-cost-decicents", type=int, default=920,
                   help="MAX_FAV_COST in decicents. Default 920 (=$0.92). "
                        "Fee-trap protection — 96%% of 1hr envelopes hit "
                        "fav~$1.00 where Kalshi taker fees eliminate edge.")
    p.add_argument("--min-entry-d-norm", type=float, default=1.5,
                   help="Minimum d_norm for main-trader entries before "
                        "--near-strike-allowed-minute. Default 1.5 blocks "
                        "close-strike entries while 20-45 minutes remain.")
    p.add_argument("--near-strike-allowed-minute", type=int, default=55,
                   help="Minute offset from which close-strike entries may "
                        "be considered. Default T+55.")
    p.add_argument("--stop-mode", default="none", choices=["none", "price"])
    p.add_argument("--shadow-stop-audit", default="enabled",
                   choices=["enabled", "disabled"],
                   help="Audit-only stop monitor. Logs shadow_stop_triggered "
                        "for open positions, but never places exits.")
    p.add_argument("--shadow-stop-bid-decicents", type=int, default=650,
                   help="Audit-only stop threshold on the held side's bid.")
    p.add_argument("--shadow-stop-min-age-sec", type=float, default=60.0,
                   help="Minimum position age before a shadow stop can log.")
    p.add_argument("--bps-gate", default="enabled",
                   choices=["enabled", "disabled"])
    # Phase 14.12 - LadderStrategy companion (BTC only by default, isolated
    # daily-PnL cap so it can fail without affecting the main engine).
    p.add_argument("--ladder-enabled", default="false",
                   choices=["true", "false"],
                   help="Phase 14.12 BTC ladder companion. Default false. "
                        "When true, at T+30 of each 1hr cycle picks top-N "
                        "far-OTM strikes by d_norm and enters at flat "
                        "rung_size ct on the favored side.")
    p.add_argument("--ladder-max-rungs", type=int, default=3,
                   help="Phase 14.12 max ladder rungs per cycle.")
    p.add_argument("--ladder-d-norm-min", type=float, default=1.5,
                   help="Phase 14.12 minimum d_norm for a ladder rung.")
    p.add_argument("--ladder-rung-size", type=int, default=3,
                   help="Phase 14.12 contracts per ladder rung.")
    p.add_argument("--ladder-min-bid-size", type=int, default=3,
                   help="Phase 14.12 favored-side top-of-book depth required "
                        "to include a strike in the ladder candidate set.")
    p.add_argument("--ladder-fav-min-dc", type=float, default=750.0,
                   help="Phase 14.12 minimum favorite mid in deci-cents.")
    p.add_argument("--ladder-fav-max-dc", type=float, default=950.0,
                   help="Phase 14.12 maximum favorite mid in deci-cents "
                        "(slightly above engine's 920 cap because rungs at "
                        "0.93-0.95 are the highest-WR far-OTM zone).")
    p.add_argument("--ladder-daily-cap-cents", type=int, default=500,
                   help="Phase 14.12 ladder-only daily realized-loss cap "
                        "(separate from --daily-cap-cents). Default 500 = $5.")
    p.add_argument("--ladder-cryptos", default="BTC",
                   help="Comma-separated allowlist of cryptos the ladder may "
                        "trade. Default BTC only per stacking-analysis "
                        "finding (88%% of additive signal is on BTC).")
    p.add_argument("--ladder-trigger-minute", type=int, default=30,
                   help="Phase 14.12 minute-into-cycle when the ladder fires.")
    p.add_argument("--deep-itm-enabled", default="false",
                   choices=["true", "false"],
                   help="Conservative 1hr deep-ITM sweeper. When true, scans "
                        "the whole strike ladder early in the cycle and buys "
                        "favorites whose ASK is inside --deep-itm-ask-range-dc. "
                        "Separate cap from main trader and ladder.")
    p.add_argument("--deep-itm-trigger-minutes", default="5,10",
                   help="Comma-separated minute marks for the deep-ITM sweep.")
    p.add_argument("--deep-itm-skip-trigger-minutes", default="20,25",
                   help="Minute marks explicitly disabled for deep-ITM. Kept "
                        "as config because observer data flagged T+20/T+25.")
    p.add_argument("--deep-itm-cryptos", default="BTC,ETH",
                   help="Comma-separated crypto allowlist. Default BTC,ETH.")
    p.add_argument("--deep-itm-max-rungs", type=int, default=2)
    p.add_argument("--deep-itm-rung-size", type=int, default=1)
    p.add_argument("--deep-itm-min-d-norm", type=float, default=3.0)
    p.add_argument("--deep-itm-min-ask-dc", type=int, default=900)
    p.add_argument("--deep-itm-max-ask-dc", type=int, default=970)
    p.add_argument("--deep-itm-min-bid-size", type=int, default=5)
    p.add_argument("--deep-itm-daily-cap-cents", type=int, default=300)
    p.add_argument("--dry-run", action="store_true",
                   help="log decisions but place no real orders")
    p.add_argument("--spot-source", default="bitstamp",
                   choices=["bitstamp", "bitstamp-ws", "coinbase"])
    p.add_argument("--duration-s", type=float, default=0.0,
                   help="0 = run until interrupted; else exit after N seconds")
    p.add_argument("--log-path", default=str(DEFAULT_LOG_PATH))
    return p.parse_args(argv)


def _diag(msg: str) -> None:
    print(f"[diag] {msg}", file=sys.stderr, flush=True)


async def _discover_1hr_markets(
    client: KalshiClient, cryptos: list[Crypto], log: LiveLogWriter,
) -> list[dict]:
    """REST discovery of currently-open 1hr digital markets per crypto.

    Cryptos without a configured KX{C}D series (currently: DOGE) are skipped
    with a warning rather than treated as an error.
    """
    out: list[dict] = []
    skipped_long: list[dict] = []
    missing_series: list[str] = []
    for crypto in cryptos:
        series = SERIES_1HR_FOR_CRYPTO.get(crypto)
        if series is None:
            missing_series.append(crypto.value)
            log.write({
                "kind": "discovery_warning",
                "crypto": crypto.value,
                "reason": "no 1hr digital series configured (e.g. KXDOGED does "
                          "not exist on Kalshi); skipping",
            })
            continue
        try:
            markets = await client.list_markets(
                series_ticker=series, status="open", limit=200,
            )
        except Exception as exc:
            log.write({"kind": "discovery_error", "series": series,
                       "error": repr(exc)})
            continue
        for m in markets:
            ticker = m.get("ticker")
            strike = _strike_from_market(m)
            open_ms = _iso_to_ms(m.get("open_time"))
            close_ms = _iso_to_ms(m.get("close_time"))
            if not ticker or strike <= 0 or open_ms is None or close_ms is None:
                continue
            # Phase 14.8 — cycle-duration filter. KXBTCD/KXETHD/KXSOLD/etc
            # series occasionally contain non-1hr markets (e.g. 25h daily
            # cycles that opened 20:00Z today and close 21:00Z tomorrow).
            # The 1hr engine assumes τ=close-now ≤ ~60min in its math; a
            # 25h market with the same ticker scheme broke the entire risk
            # model on 2026-05-26. Reject anything > MAX_1HR_CYCLE_MIN.
            dur_min = (close_ms - open_ms) / 60_000.0
            if dur_min > MAX_1HR_CYCLE_MIN:
                log.write({
                    "kind": "discovery_skip_long_cycle",
                    "series": series, "ticker": ticker,
                    "duration_minutes": dur_min,
                    "cap_minutes": MAX_1HR_CYCLE_MIN,
                })
                skipped_long.append({"ticker": ticker, "duration_minutes": dur_min,
                                      "series": series})
                continue
            out.append({
                "ticker": ticker, "strike": strike,
                "open_ms": open_ms, "close_ms": close_ms, "series": series,
            })
    counts: dict[str, int] = {}
    for m in out:
        counts[m["series"]] = counts.get(m["series"], 0) + 1
    log.write({
        "kind": "discovery", "count": len(out), "by_series": counts,
        "missing_series_for_cryptos": missing_series,
        "skipped_long_cycle_count": len(skipped_long),
    })
    return out


async def _discovery_loop(
    client: KalshiClient, strategy: HourglassTraderStrategy,
    cryptos: list[Crypto], log: LiveLogWriter,
    interval_seconds: float = 60.0, kalshi_ws=None,
) -> None:
    """Periodic REST re-discovery: register newly-opened 1hr markets."""
    while True:
        try:
            newly: dict[str, list[str]] = defaultdict(list)
            for crypto in cryptos:
                series = SERIES_1HR_FOR_CRYPTO.get(crypto)
                if series is None:
                    continue
                try:
                    markets = await client.list_markets(
                        series_ticker=series, status="open", limit=200,
                    )
                except Exception as exc:
                    log.write({"kind": "discovery_error", "series": series,
                               "error": repr(exc)})
                    continue
                for m in markets:
                    ticker = m.get("ticker")
                    if not ticker or ticker in strategy.markets:
                        continue
                    strike = _strike_from_market(m)
                    open_ms = _iso_to_ms(m.get("open_time"))
                    close_ms = _iso_to_ms(m.get("close_time"))
                    if strike <= 0 or open_ms is None or close_ms is None:
                        continue
                    # Phase 14.8 — same cycle-duration filter as boot discovery
                    dur_min = (close_ms - open_ms) / 60_000.0
                    if dur_min > MAX_1HR_CYCLE_MIN:
                        log.write({
                            "kind": "discovery_skip_long_cycle",
                            "series": series, "ticker": ticker,
                            "duration_minutes": dur_min,
                            "cap_minutes": MAX_1HR_CYCLE_MIN,
                        })
                        continue
                    strategy.register_market(ticker, strike, open_ms, close_ms)
                    newly[series].append(ticker)
            if newly:
                log.write({
                    "kind": "market_discovery",
                    "newly_registered_count": sum(len(v) for v in newly.values()),
                    "total_registered": len(strategy.markets),
                    "by_series": {s: len(t) for s, t in newly.items()},
                    "new_tickers": dict(newly),
                })
                if kalshi_ws is not None:
                    new_tickers = [t for ts in newly.values() for t in ts]
                    try:
                        added = await kalshi_ws.add_tickers(new_tickers)
                        log.write({"kind": "ws_subscription_extended",
                                   "added_count": added, "tickers": new_tickers})
                    except Exception as exc:
                        log.write({"kind": "ws_subscription_extend_error",
                                   "error": repr(exc), "tickers": new_tickers})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.write({"kind": "discovery_loop_error", "error": repr(exc)})
        try:
            await asyncio.sleep(interval_seconds)
        except asyncio.CancelledError:
            raise


def _route(ev, strategy, risk_state, log):
    """Update risk state from non-decision events; return Decision|None."""
    if isinstance(ev, SpotEvent):
        risk_state.last_spot_ms[ev.crypto.value] = ev.ts_ms
        risk_state.now_ms = max(risk_state.now_ms, ev.ts_ms)
        return strategy.on_event(ev)
    if isinstance(ev, BookEvent):
        risk_state.now_ms = max(risk_state.now_ms, ev.recv_ms)
        return strategy.on_event(ev)
    if isinstance(ev, TradeEvent):
        risk_state.now_ms = max(risk_state.now_ms, ev.recv_ms)
        return strategy.on_event(ev)
    if isinstance(ev, LifecycleEvent):
        if ev.strike and ev.open_ms and ev.close_ms:
            strategy.register_market(
                ev.ticker, ev.strike, ev.open_ms, ev.close_ms,
            )
            log.write({"kind": "lifecycle_register", "ticker": ev.ticker,
                       "status": ev.status, "strike": ev.strike})
        else:
            log.write({"kind": "lifecycle", "ticker": ev.ticker,
                       "status": ev.status})
        return None
    if isinstance(ev, SettlementEvent):
        log.write({"kind": "settlement", "ticker": ev.ticker,
                   "result": ev.result.value, "settle_value": ev.settle_value})
        return None
    return None


class ShadowStopAudit:
    """Logs hypothetical stop triggers without submitting exit orders."""

    def __init__(
        self,
        enabled: bool,
        bid_threshold_decicents: int,
        min_age_ms: int,
    ) -> None:
        self.enabled = enabled
        self.bid_threshold_decicents = int(bid_threshold_decicents)
        self.min_age_ms = int(min_age_ms)
        self._triggered: set[str] = set()

    def on_book(
        self,
        book: BookEvent,
        open_positions: dict[str, dict],
        log: LiveLogWriter,
    ) -> None:
        if not self.enabled or book.ticker in self._triggered:
            return
        pos = open_positions.get(book.ticker)
        if not pos:
            return
        side = str(pos.get("side") or "").lower()
        if side == "yes":
            bid_dc = book.yes_bid
        elif side == "no":
            bid_dc = book.no_bid
        else:
            return
        filled_at = pos.get("filled_at_ms")
        if filled_at is None:
            filled_at = pos.get("entered_at_ms")
        age_ms = book.recv_ms - int(filled_at if filled_at is not None else book.recv_ms)
        if age_ms < self.min_age_ms:
            return
        if bid_dc > self.bid_threshold_decicents:
            return
        self._triggered.add(book.ticker)
        log.write({
            "kind": "shadow_stop_triggered",
            "ticker": book.ticker,
            "side": side,
            "count": pos.get("count"),
            "entry_price_decicents": pos.get("entry_price_decicents"),
            "current_bid_decicents": bid_dc,
            "threshold_bid_decicents": self.bid_threshold_decicents,
            "age_ms": age_ms,
            "yes_bid": book.yes_bid,
            "yes_ask": book.yes_ask,
            "no_bid": book.no_bid,
            "no_ask": book.no_ask,
        })


async def _run_loop(
    strategy: HourglassTraderStrategy,
    envelope: RiskEnvelope,
    risk_state: RiskState,
    execution: LiveExecution,
    kalshi_ws: KalshiWebSocketFeed,
    spot_feed: SpotFeed,
    log: LiveLogWriter,
    duration_s: float,
    client: KalshiClient,
    cryptos: list[Crypto],
    discovery_interval_s: float = 60.0,
    log_path: str | None = None,
    pnl_reconcile_interval_s: float = 30.0,
    ladder: "LadderStrategy | None" = None,
    deep_itm: "DeepItmSweeperStrategy | None" = None,
    shadow_stop: ShadowStopAudit | None = None,
) -> None:
    """Merge events from both feeds, route to strategy, gate, submit."""
    queue: asyncio.Queue = asyncio.Queue()
    deadline = (time.time() + duration_s) if duration_s > 0 else None

    async def pump(source: str, gen) -> None:
        try:
            async for ev in gen:
                await queue.put((source, ev))
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            log.write({"kind": "feed_error", "source": source, "error": repr(exc)})

    spot_task = asyncio.create_task(pump("spot", spot_feed.events()))
    ws_task = asyncio.create_task(pump("kalshi", kalshi_ws.events()))
    listener_task = (
        asyncio.create_task(execution.run_order_update_listener())
        if not execution.dry_run
        else None
    )
    discovery_task = asyncio.create_task(_discovery_loop(
        client, strategy, cryptos, log,
        interval_seconds=discovery_interval_s,
        kalshi_ws=kalshi_ws,
    ))
    # Phase 14.7 — periodic PnL reconcile to populate
    # risk_state.daily_realized_cents from the engine's own JSONL log.
    # Fixes the long-standing defect where the daily-loss-cap check at
    # envelope.py:87 was unenforced because the counter was never written.
    async def _pnl_reconcile_loop():
        from kalshi_engine.risk.pnl_reconcile import reconcile_today_realized_cents
        import time as _t
        last_value = None
        while True:
            try:
                cents = reconcile_today_realized_cents(log_path) if log_path else 0
                if cents != last_value:
                    log.write({"kind": "cap_status",
                               "daily_realized_cents": cents,
                               "daily_cap_cents": envelope.daily_loss_cap_cents,
                               "bound": cents <= -envelope.daily_loss_cap_cents})
                    last_value = cents
                risk_state.daily_realized_cents = cents
            except Exception as exc:
                log.write({"kind": "pnl_reconcile_error", "error": repr(exc)[:120]})
            try:
                await asyncio.sleep(pnl_reconcile_interval_s)
            except asyncio.CancelledError:
                return
    pnl_task = asyncio.create_task(_pnl_reconcile_loop())

    try:
        while True:
            if deadline is not None and time.time() >= deadline:
                break
            try:
                remaining = max(0.1, deadline - time.time()) if deadline else 5.0
                _source, ev = await asyncio.wait_for(queue.get(), timeout=remaining)
            except asyncio.TimeoutError:
                continue
            try:
                if isinstance(ev, BookEvent) and shadow_stop is not None:
                    shadow_stop.on_book(ev, execution.open_positions, log)
                decision = _route(ev, strategy, risk_state, log)
                # Phase 14.12 - route SAME event to ladder; it returns 0..N
                # additional Decisions (ENTER rungs). Each is envelope-checked
                # and submitted independently so a ladder rung being clipped
                # by global gates doesn't kill the trader's decision.
                ladder_decisions = ladder.on_event(ev) if ladder is not None else []
                deep_itm_decisions = (
                    deep_itm.on_event(ev) if deep_itm is not None else []
                )
                pending: list = []
                if decision is not None:
                    pending.append(("trader", decision))
                for d in ladder_decisions:
                    pending.append(("ladder", d))
                for d in deep_itm_decisions:
                    pending.append(("deep_itm", d))
                if not pending:
                    continue
                for source, d in pending:
                    d_checked = envelope.check(d, risk_state)
                    # The strategy already logged the decision; the envelope
                    # may have downsized/skipped it - log under a distinct
                    # kind so post-hoc analysis can compare.
                    log.write({
                        "kind": "decision_post_envelope",
                        "source": source,
                        "ticker": d_checked.ticker,
                        "action": d_checked.action.value,
                        "side": d_checked.side.value if d_checked.side else None,
                        "size": d_checked.size,
                        "confidence": d_checked.confidence,
                        "reason": d_checked.reason,
                    })
                    await execution.submit(d_checked)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.write({
                    "kind": "submit_error",
                    "error": repr(exc),
                })
    finally:
        for task in (spot_task, ws_task, listener_task, discovery_task, pnl_task):
            if task is None:
                continue
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass


async def _amain(args: argparse.Namespace) -> int:
    _diag("amain entered")

    # ---- credentials ----
    key_path = os.environ.get("KALSHI_API_KEY_PATH")
    if not key_path:
        print("ERROR: KALSHI_API_KEY_PATH env var is not set", file=sys.stderr)
        return 2
    if not Path(key_path).exists():
        print(f"ERROR: KALSHI_API_KEY_PATH does not exist: {key_path}",
              file=sys.stderr)
        return 2
    try:
        creds = _read_env_file(key_path)
    except Exception as exc:
        print(f"ERROR: cannot read {key_path}: {exc}", file=sys.stderr)
        return 2
    api_key = creds.get("KALSHI_API_KEY")
    pem_path = creds.get("KALSHI_PRIVATE_KEY_PATH")
    if not api_key or not pem_path:
        print("ERROR: KALSHI_API_KEY_PATH file is missing "
              "KALSHI_API_KEY or KALSHI_PRIVATE_KEY_PATH",
              file=sys.stderr)
        return 2
    if not Path(pem_path).exists():
        print(f"ERROR: PEM file does not exist: {pem_path}", file=sys.stderr)
        return 2
    pem_bytes = Path(pem_path).read_bytes()
    _diag(f"creds loaded; pem={len(pem_bytes)}B key_id_len={len(api_key)}")

    # ---- config ----
    try:
        cryptos = [Crypto(c.strip().upper())
                   for c in args.cryptos.split(",") if c.strip()]
    except ValueError as exc:
        print(f"ERROR: invalid --cryptos value: {exc}", file=sys.stderr)
        return 2
    if not cryptos:
        print("ERROR: --cryptos must list at least one crypto", file=sys.stderr)
        return 2
    try:
        trigger_minutes = tuple(
            int(x.strip()) for x in args.trigger_minutes.split(",") if x.strip()
        )
    except ValueError as exc:
        print(f"ERROR: invalid --trigger-minutes: {exc}", file=sys.stderr)
        return 2
    if not trigger_minutes:
        print("ERROR: --trigger-minutes must list at least one minute mark",
              file=sys.stderr)
        return 2
    try:
        deep_itm_trigger_minutes = tuple(
            int(x.strip()) for x in args.deep_itm_trigger_minutes.split(",")
            if x.strip()
        )
        deep_itm_skip_trigger_minutes = tuple(
            int(x.strip()) for x in args.deep_itm_skip_trigger_minutes.split(",")
            if x.strip()
        )
    except ValueError as exc:
        print(f"ERROR: invalid --deep-itm trigger minute config: {exc}",
              file=sys.stderr)
        return 2
    if args.deep_itm_enabled == "true" and not deep_itm_trigger_minutes:
        print("ERROR: --deep-itm-trigger-minutes must list at least one minute",
              file=sys.stderr)
        return 2
    skip_hours = tuple()
    if args.skip_hours.strip():
        try:
            skip_hours = tuple(
                int(x.strip()) for x in args.skip_hours.split(",") if x.strip()
            )
        except ValueError as exc:
            print(f"ERROR: invalid --skip-hours: {exc}", file=sys.stderr)
            return 2

    log = LiveLogWriter(args.log_path)

    # ---- wire components ----
    from kalshi_engine.config import MODELS_DIR
    cutpoints_path = (
        MODELS_DIR / "phase4_cutpoints" / args.cutpoints_version / "cutpoints.json"
    )
    if not cutpoints_path.exists():
        print(f"ERROR: cutpoints artifact not found: {cutpoints_path}",
              file=sys.stderr)
        return 2
    model = Phase4CutpointsModel(
        cutpoints_path=str(cutpoints_path),
        align_mode=args.align_mode,
        time_of_day_skip=False,  # we use our own --skip-hours mechanism
    )
    per_crypto_caps: dict[str, int] = {}
    if args.per_crypto_max_contracts.strip():
        try:
            for pair in args.per_crypto_max_contracts.split(","):
                k, v = pair.strip().split("=", 1)
                per_crypto_caps[k.strip().upper()] = int(v.strip())
        except (ValueError, IndexError) as exc:
            print(f"ERROR: invalid --per-crypto-max-contracts "
                  f"{args.per_crypto_max_contracts!r}: {exc}", file=sys.stderr)
            return 2
    # Phase 14.3 — per-crypto align-mode override. Build a separate
    # Phase4CutpointsModel per (crypto, align_mode) pair.
    per_crypto_align: dict[str, str] = {}
    per_crypto_models: dict[str, Phase4CutpointsModel] = {}
    if args.per_crypto_align_mode.strip():
        try:
            for pair in args.per_crypto_align_mode.split(","):
                k, v = pair.strip().split("=", 1)
                per_crypto_align[k.strip().upper()] = v.strip()
        except (ValueError, IndexError) as exc:
            print(f"ERROR: invalid --per-crypto-align-mode "
                  f"{args.per_crypto_align_mode!r}: {exc}", file=sys.stderr)
            return 2
        for crypto, am in per_crypto_align.items():
            try:
                per_crypto_models[crypto] = Phase4CutpointsModel(
                    cutpoints_path=str(cutpoints_path),
                    align_mode=am,
                    time_of_day_skip=False,
                )
            except ValueError as exc:
                print(f"ERROR: --per-crypto-align-mode {crypto}={am}: {exc}",
                      file=sys.stderr)
                return 2
    strategy = HourglassTraderStrategy(
        log_writer=log,
        model=model,
        trigger_minutes=trigger_minutes,
        skip_hours_utc=skip_hours,
        max_favorite_cost_decicents=args.max_favorite_cost_decicents,
        max_contracts=args.max_contracts,
        per_crypto_max_contracts=per_crypto_caps or None,
        per_crypto_models=per_crypto_models or None,
        min_entry_d_norm=args.min_entry_d_norm,
        near_strike_allowed_minute=args.near_strike_allowed_minute,
    )
    # Phase 14.12 - ladder companion. Shares the trader's per-crypto state +
    # market registry via lookup callable. Disabled by default.
    ladder_enabled = args.ladder_enabled.lower() == "true"
    ladder_cryptos = tuple(c.strip().upper() for c in args.ladder_cryptos.split(",")
                            if c.strip())
    ladder = LadderStrategy(
        log_writer=log,
        per_crypto_states=strategy._states,
        market_lookup=lambda t: strategy.markets.get(t),
        enabled=ladder_enabled,
        max_rungs=args.ladder_max_rungs,
        d_norm_min=args.ladder_d_norm_min,
        rung_size=args.ladder_rung_size,
        min_bid_size=args.ladder_min_bid_size,
        trigger_minute=args.ladder_trigger_minute,
        crypto_allowlist=ladder_cryptos,
        fav_min_dc=args.ladder_fav_min_dc,
        fav_max_dc=args.ladder_fav_max_dc,
        daily_cap_cents=args.ladder_daily_cap_cents,
    )
    deep_itm_cryptos = tuple(c.strip().upper() for c in args.deep_itm_cryptos.split(",")
                             if c.strip())
    deep_itm = DeepItmSweeperStrategy(
        log_writer=log,
        per_crypto_states=strategy._states,
        market_lookup=lambda t: strategy.markets.get(t),
        enabled=(args.deep_itm_enabled == "true"),
        max_rungs=args.deep_itm_max_rungs,
        rung_size=args.deep_itm_rung_size,
        min_d_norm=args.deep_itm_min_d_norm,
        min_fav_ask_dc=args.deep_itm_min_ask_dc,
        max_fav_ask_dc=args.deep_itm_max_ask_dc,
        min_bid_size=args.deep_itm_min_bid_size,
        trigger_minutes=deep_itm_trigger_minutes,
        skip_trigger_minutes=deep_itm_skip_trigger_minutes,
        crypto_allowlist=deep_itm_cryptos,
        daily_cap_cents=args.deep_itm_daily_cap_cents,
    )
    envelope = RiskEnvelope(
        daily_loss_cap_cents=args.daily_cap_cents,
        max_contracts_per_trade=args.max_contracts,
    )
    risk_state = RiskState()
    spot_feed = SpotFeed(cryptos, spot_source=args.spot_source)
    shadow_stop = ShadowStopAudit(
        enabled=(args.shadow_stop_audit == "enabled"),
        bid_threshold_decicents=args.shadow_stop_bid_decicents,
        min_age_ms=int(args.shadow_stop_min_age_sec * 1000),
    )

    _diag("entering KalshiClient context")
    async with KalshiClient(api_key, pem_bytes) as client:
        _diag("KalshiClient ready; constructing LiveExecution")
        execution = LiveExecution(
            client, log, dry_run=args.dry_run, stop_mode=args.stop_mode,
        )
        _diag(f"discovery start; cryptos={[c.value for c in cryptos]}")
        # Phase 14.13+ - sleep+retry on empty discovery instead of boot_abort.
        # Kalshi has transient gap windows (~seconds to a couple of minutes)
        # between settled and next-open 1hr cycles where only the 25h-daily
        # markets remain in status="open"; the Phase 14.8 cycle-duration
        # filter correctly rejects them, but the prior code then exited
        # with code 3 and NSSM restart-throttled the service into Paused.
        # Mirror the Phase 14.11 KXINXU sleep+retry: log a heartbeat per
        # attempt and keep the engine alive until markets reappear.
        retry_seconds = 60
        retry_attempts = 0
        while True:
            markets = await _discover_1hr_markets(client, cryptos, log)
            if markets:
                break
            retry_attempts += 1
            log.write({
                "kind": "no_markets_waiting",
                "process": "hourglass_trader",
                "retry_attempts": retry_attempts,
                "next_retry_s": retry_seconds,
            })
            _diag(f"no 1hr markets discovered; retry in {retry_seconds}s "
                  f"(attempt {retry_attempts})")
            try:
                await asyncio.sleep(retry_seconds)
            except asyncio.CancelledError:
                return 0
        _diag(f"discovery done; markets={len(markets)}")
        for m in markets:
            strategy.register_market(
                m["ticker"], m["strike"], m["open_ms"], m["close_ms"],
            )

        _diag("boot reconcile from /portfolio/positions ...")
        try:
            await execution.reconcile_from_account_at_boot(strategy)
        except Exception as exc:
            log.write({"kind": "boot_reconcile_error", "error": repr(exc)})
        _diag(f"boot reconcile done; local positions="
              f"{len(getattr(strategy, '_entered', set()))}")

        # Phase 14.7 — reconcile today's realized PnL from the log so the
        # daily-cap counter starts in the right state across restarts.
        try:
            from kalshi_engine.risk.pnl_reconcile import reconcile_today_realized_cents
            boot_cents = reconcile_today_realized_cents(str(args.log_path))
            risk_state.daily_realized_cents = boot_cents
            log.write({"kind": "cap_status", "stage": "boot_reconcile",
                       "daily_realized_cents": boot_cents,
                       "daily_cap_cents": envelope.daily_loss_cap_cents,
                       "bound": boot_cents <= -envelope.daily_loss_cap_cents})
            _diag(f"boot pnl reconcile: {boot_cents}c "
                  f"(cap {envelope.daily_loss_cap_cents}c, "
                  f"bound={boot_cents <= -envelope.daily_loss_cap_cents})")
        except Exception as exc:
            log.write({"kind": "boot_pnl_reconcile_error",
                       "error": repr(exc)[:120]})

        _diag("draining spot warmup into strategy + risk_state ...")
        warmup_n = 0
        try:
            warmup_n = await spot_feed.bootstrap_warmup_into(strategy, risk_state)
        except Exception as exc:
            log.write({"kind": "warmup_error", "error": repr(exc)})
        _diag(f"warmup drained; {warmup_n} spot events")

        _diag("writing boot event")
        log.write({
            "kind": "boot",
            "process": "hourglass_trader",
            "strategy": args.strategy,
            "model": args.model,
            "align_mode": args.align_mode,
            "cutpoints_version": args.cutpoints_version,
            "model_cutpoints_path": str(cutpoints_path),
            "model_cutpoints_version": model.cutpoints.get("version"),
            "cryptos": [c.value for c in cryptos],
            "trigger_minutes": list(trigger_minutes),
            "skip_hours_utc": list(skip_hours),
            "max_favorite_cost_decicents": args.max_favorite_cost_decicents,
            "max_contracts": args.max_contracts,
            "min_entry_d_norm": args.min_entry_d_norm,
            "near_strike_allowed_minute": args.near_strike_allowed_minute,
            "per_crypto_max_contracts": per_crypto_caps,
            "per_crypto_align_mode": per_crypto_align,
            "daily_cap_cents": args.daily_cap_cents,
            "spot_source": args.spot_source,
            "stop_mode": args.stop_mode,
            "shadow_stop_audit": args.shadow_stop_audit,
            "shadow_stop_bid_decicents": args.shadow_stop_bid_decicents,
            "shadow_stop_min_age_sec": args.shadow_stop_min_age_sec,
            "bps_gate": args.bps_gate,
            "dry_run": args.dry_run,
            "duration_s": args.duration_s,
            "markets_registered": len(markets),
            "warmup_events_drained": warmup_n,
            "log_path": str(args.log_path),
            # Phase 14.12 ladder config
            "ladder_enabled": ladder.enabled,
            "ladder_max_rungs": ladder.max_rungs,
            "ladder_d_norm_min": ladder.d_norm_min,
            "ladder_rung_size": ladder.rung_size,
            "ladder_min_bid_size": ladder.min_bid_size,
            "ladder_trigger_minute": ladder.trigger_minute,
            "ladder_crypto_allowlist": list(ladder.crypto_allowlist),
            "ladder_fav_range_dc": [ladder.fav_min_dc, ladder.fav_max_dc],
            "ladder_daily_cap_cents": ladder.daily_cap_cents,
            "deep_itm_enabled": deep_itm.enabled,
            "deep_itm_max_rungs": deep_itm.max_rungs,
            "deep_itm_rung_size": deep_itm.rung_size,
            "deep_itm_min_d_norm": deep_itm.min_d_norm,
            "deep_itm_ask_range_dc": [
                deep_itm.min_fav_ask_dc,
                deep_itm.max_fav_ask_dc,
            ],
            "deep_itm_min_bid_size": deep_itm.min_bid_size,
            "deep_itm_trigger_minutes": list(deep_itm.trigger_minutes),
            "deep_itm_skip_trigger_minutes": sorted(deep_itm.skip_trigger_minutes),
            "deep_itm_crypto_allowlist": list(deep_itm.crypto_allowlist),
            "deep_itm_daily_cap_cents": deep_itm.daily_cap_cents,
            # Phase 14.19 BTC-1hr alpha-capture levers (BTC only)
            "phase_14_19_btc_size_tilt_score": BTC_SIZE_TILT_SCORE,
            "phase_14_19_btc_size_tilt_minute": BTC_SIZE_TILT_MINUTE,
            "phase_14_19_btc_size_tilt_contracts": BTC_SIZE_TILT_CONTRACTS,
            "phase_14_19_btc_downsize_dnorm": BTC_DOWNSIZE_DNORM,
            "phase_14_19_btc_downsize_contracts": BTC_DOWNSIZE_CONTRACTS,
            "phase_14_19_btc_max_fav_ask_decicents": BTC_MAX_FAV_ASK_DECICENTS,
        })

        _diag("constructing kalshi_ws + entering run_loop")
        kalshi_ws = KalshiWebSocketFeed(
            client.signer, tickers=[m["ticker"] for m in markets],
        )
        try:
            await _run_loop(
                strategy=strategy, envelope=envelope, risk_state=risk_state,
                execution=execution, kalshi_ws=kalshi_ws,
                spot_feed=spot_feed, log=log,
                duration_s=args.duration_s,
                client=client, cryptos=cryptos,
                log_path=str(args.log_path),
                ladder=ladder,
                deep_itm=deep_itm,
                shadow_stop=shadow_stop,
            )
        finally:
            log.write({"kind": "shutdown", "process": "hourglass_trader"})
    return 0


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        return asyncio.run(_amain(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
