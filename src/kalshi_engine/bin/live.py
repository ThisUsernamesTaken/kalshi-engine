"""Live runner entry point for the new kalshi_engine.

Wires the Kalshi REST + WS feeds, the Coinbase spot feed (with Bitstamp REST
fallback), the favorite-chase strategy with the Phase 4 cutpoints model, the
risk envelope, and the live execution adapter. Loads the API key from
``KALSHI_API_KEY_PATH`` (a .env-style file with ``KALSHI_API_KEY=...`` and
``KALSHI_PRIVATE_KEY_PATH=...``). Writes a JSONL live log.

In ``--dry-run`` mode no orders are placed; every decision is still logged
and the rest of the pipeline (feeds, model, envelope, reconcile) runs end
to end.

    python -m kalshi_engine.bin.live --dry-run --duration-s 900
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
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
from kalshi_engine.research.cycle_tracker import CycleTracker
from kalshi_engine.risk.envelope import RiskEnvelope, RiskState
from kalshi_engine.strategies.favorite_chase.models.phase4_cutpoints import (
    Phase4CutpointsModel,
)
from kalshi_engine.strategies.favorite_chase.strategy import FavoriteChaseStrategy
from kalshi_engine.warehouse.adapters import LiveLogWriter
from kalshi_engine.warehouse.settlement import _iso_to_ms

DEFAULT_LOG_PATH = RAW_DIR / "live_logs" / "live_favorite_chase_v2.jsonl"

SERIES_FOR_CRYPTO = {
    Crypto.BTC: "KXBTC15M",
    Crypto.ETH: "KXETH15M",
    Crypto.SOL: "KXSOL15M",
    Crypto.XRP: "KXXRP15M",
    Crypto.DOGE: "KXDOGE15M",
}


def _read_env_file(path: str) -> dict[str, str]:
    """Parse a simple KEY=value .env file (no exports, no shell quoting)."""
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
    p = argparse.ArgumentParser(description="kalshi_engine live runner")
    p.add_argument("--strategy", default="favorite_chase",
                   choices=["favorite_chase"])
    p.add_argument("--model", default="phase4_cutpoints",
                   choices=["phase4_cutpoints"])
    p.add_argument("--cryptos", default="BTC,ETH,SOL,XRP,DOGE",
                   help="comma-separated crypto symbols")
    p.add_argument("--stop-mode", default="none",
                   choices=["none", "price"])
    p.add_argument("--bps-gate", default="enabled",
                   choices=["enabled", "disabled"])
    p.add_argument("--max-contracts", type=int, default=10,
                   help="Risk envelope cap on contracts per trade. Default 10 "
                        "(Phase 12.7) — user-authorized scale-up after V13b's "
                        "100%% WR backtest. Worst single-trade loss now "
                        "~$9.50 at 10ct * 95c.")
    p.add_argument("--daily-cap-cents", type=int, default=1000)
    p.add_argument("--align-mode", default="5tier_v13b",
                   choices=["disabled", "2tier", "3tier", "5tier", "5tier_v13b"],
                   help="Phase-12 alignment-count sizing mode. '5tier_v13b' "
                        "(default, Phase 12.6) = V13b validated optimization: "
                        "score = 2*bb_div_band + 1.5*side_no + 2*bps_strong + "
                        "super_band(+1 if bb_div in (-0.14,-0.09]). Bootstrap "
                        "99.7%% > V12. '5tier' (Phase 12.4) = original Scheme B "
                        "with side_yes weight. '3tier' = align=0 skip, "
                        "1->1ct/2->2ct/3->3ct. '2tier' = align<=1 skip, "
                        "2->1ct/3->2ct. 'disabled' = legacy UPSIZE_2X.")
    p.add_argument("--reentry-mode", default="disabled",
                   choices=["disabled", "polling"],
                   help="Phase-12.3 re-entry behaviour. 'disabled' (default "
                        "as of Phase 12.5 — Rec 1) reverts to single-shot "
                        "dedup; first-shot only, no polling re-eval. "
                        "Validated: first-shot is 35/35 = 100%% WR. "
                        "'polling' enables re-eval (Phase 12.3) — research-only.")
    p.add_argument("--time-of-day-skip", default="enabled",
                   choices=["enabled", "disabled"],
                   help="Phase-12.5 Rec 2: SKIP all entries during UTC 14-17Z "
                        "(US-AM weak window). Validated: combined LIVE+P4 "
                        "n=100 at 69%% WR / -$14.31. 'enabled' (default) "
                        "applies the SKIP; 'disabled' bypasses it.")
    p.add_argument("--cutpoints-version", default="v3",
                   help="Cutpoints artifact version to load from MODELS_DIR/"
                        "phase4_cutpoints/<version>/cutpoints.json. v3 "
                        "(default, Phase 12.5 Rec 3) recalibrates per-crypto "
                        "bps thresholds for ETH/SOL/XRP. v1 = original "
                        "Phase-4 thresholds. Reversible.")
    p.add_argument("--reentry-cutoff-min", type=float, default=2.0,
                   help="Re-entry lockout window (minutes before cycle close).")
    p.add_argument("--reentry-throttle-sec", type=float, default=30.0,
                   help="Per-ticker re-entry throttle (seconds).")
    p.add_argument("--pre-trigger-observation", default="enabled",
                   choices=["enabled", "disabled"],
                   help="Phase-12.8 — emit `book_at_pre_trigger` envelopes "
                        "during T+5 to T+8 of each cycle (pre-trigger window). "
                        "Pure observability for 'could we trigger earlier?' "
                        "research. Throttled to 30s/ticker. No decision impact.")
    p.add_argument("--dry-run", action="store_true",
                   help="log decisions but place no real orders")
    p.add_argument("--spot-source", default="bitstamp",
                   choices=["bitstamp", "bitstamp-ws", "coinbase"],
                   help="live spot source. 'bitstamp' = REST polling (Phase-4 "
                        "default, ~2-3 s latency). 'bitstamp-ws' = Bitstamp "
                        "live_trades WS (Phase-10, sub-second). 'coinbase' = "
                        "WS, known streaming defect deferred from Phase 4")
    p.add_argument("--duration-s", type=float, default=0.0,
                   help="0 = run until interrupted; otherwise exit after N seconds")
    p.add_argument("--log-path", default=str(DEFAULT_LOG_PATH))
    p.add_argument("--snapshot-interval-ms", type=int, default=0,
                   help="Phase-11A: emit per-market `snapshot` envelopes at this "
                        "cadence (ms) during each trigger window. 0=disabled. "
                        "5000 = sample every 5 s (recommended for research).")
    p.add_argument("--log-spot-ticks", action="store_true",
                   help="Phase-11B: write every SpotEvent as a `spot_tick` "
                        "envelope to the JSONL. High volume (~2M rows/day) - "
                        "intended for research, not production.")
    p.add_argument("--cycle-summary", action="store_true",
                   help="Phase-11C: on each crypto settlement, emit a "
                        "`cycle_summary` envelope with the snapshots + spot "
                        "trajectory + position outcome for that cycle.")
    return p.parse_args(argv)


async def _discover_markets(
    client: KalshiClient,
    cryptos: list[Crypto],
    log: LiveLogWriter,
) -> list[dict]:
    """REST discovery of currently-open 15-min markets across the cryptos."""
    out: list[dict] = []
    for crypto in cryptos:
        series = SERIES_FOR_CRYPTO[crypto]
        try:
            # Kalshi's /markets `status` filter accepts "open" / "closed" /
            # "settled" / "determined"; "active" was the engine-internal term
            # but is rejected by the REST API.
            markets = await client.list_markets(
                series_ticker=series, status="open", limit=200,
            )
        except Exception as exc:
            log.write({"kind": "discovery_error", "series": series, "error": repr(exc)})
            continue
        for m in markets:
            ticker = m.get("ticker")
            try:
                strike = float(m.get("floor_strike") or 0)
            except (TypeError, ValueError):
                continue
            open_ms = _iso_to_ms(m.get("open_time"))
            close_ms = _iso_to_ms(m.get("close_time"))
            if not ticker or strike <= 0 or open_ms is None or close_ms is None:
                continue
            out.append({
                "ticker": ticker, "strike": strike,
                "open_ms": open_ms, "close_ms": close_ms,
                "series": series,
            })
    counts: dict[str, int] = {}
    for m in out:
        counts[m["series"]] = counts.get(m["series"], 0) + 1
    log.write({"kind": "discovery", "count": len(out), "by_series": counts})
    return out


async def _market_discovery_loop(
    client: KalshiClient,
    strategy: FavoriteChaseStrategy,
    cryptos: list[Crypto],
    log: LiveLogWriter,
    interval_seconds: float = 60.0,
    kalshi_ws=None,
) -> None:
    """Periodic REST re-discovery: register newly-opened 15M markets.

    The boot discovery only sees the cycle that's currently open per crypto.
    Without this loop, the engine goes idle after the boot cycle settles
    because the strategy never learns the strike / cycle timing for the next
    15-min market (the WS lifecycle channel emits status updates but not the
    metadata fields needed for ``register_market``).

    Errors (REST 4xx/5xx, network) log a ``discovery_error`` envelope and
    the loop continues -- a hiccup must never kill the engine. If a
    ``kalshi_ws`` reference is supplied, newly-registered tickers are also
    passed to the WS feed so book/trade subscriptions extend to them.
    """
    while True:
        try:
            newly_registered: dict[str, list[str]] = {}
            for crypto in cryptos:
                series = SERIES_FOR_CRYPTO[crypto]
                try:
                    markets = await client.list_markets(
                        series_ticker=series, status="open", limit=200,
                    )
                except Exception as exc:
                    log.write({
                        "kind": "discovery_error",
                        "series": series,
                        "error": repr(exc),
                    })
                    continue
                for m in markets:
                    ticker = m.get("ticker")
                    if not ticker or ticker in strategy.markets:
                        continue
                    try:
                        strike = float(m.get("floor_strike") or 0)
                    except (TypeError, ValueError):
                        continue
                    open_ms = _iso_to_ms(m.get("open_time"))
                    close_ms = _iso_to_ms(m.get("close_time"))
                    if strike <= 0 or open_ms is None or close_ms is None:
                        continue
                    strategy.register_market(ticker, strike, open_ms, close_ms)
                    newly_registered.setdefault(series, []).append(ticker)
            if newly_registered:
                count_by_series = {s: len(t) for s, t in newly_registered.items()}
                log.write({
                    "kind": "market_discovery",
                    "newly_registered_count": sum(count_by_series.values()),
                    "total_registered": len(strategy.markets),
                    "by_series": count_by_series,
                    "new_tickers": newly_registered,
                })
                # Extend the WS subscription so book/trade events flow for
                # the newly-registered tickers (otherwise the strategy
                # registers them but never sees their books, and decisions
                # still never fire). Best-effort: failures are logged but
                # don't crash the discovery loop.
                if kalshi_ws is not None:
                    new_tickers = [t for ts in newly_registered.values() for t in ts]
                    try:
                        added = await kalshi_ws.add_tickers(new_tickers)
                        log.write({
                            "kind": "ws_subscription_extended",
                            "added_count": added,
                            "tickers": new_tickers,
                        })
                    except Exception as exc:
                        log.write({
                            "kind": "ws_subscription_extend_error",
                            "error": repr(exc),
                            "tickers": new_tickers,
                        })
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # An unexpected defect anywhere in the loop body must NOT crash
            # the discovery task -- engine ops loses the safety net otherwise.
            log.write({"kind": "market_discovery_loop_error", "error": repr(exc)})
        try:
            await asyncio.sleep(interval_seconds)
        except asyncio.CancelledError:
            raise


def _route(ev, strategy, risk_state, log, cycle_tracker=None,
           log_spot_ticks=False):
    """Update auxiliary state from non-decision events; return Decision|None."""
    # Phase-11C: feed the tracker every event (no-op when disabled).
    if cycle_tracker is not None:
        cycle_tracker.on_event(ev)
    if isinstance(ev, SpotEvent):
        risk_state.last_spot_ms[ev.crypto.value] = ev.ts_ms
        risk_state.now_ms = max(risk_state.now_ms, ev.ts_ms)
        if log_spot_ticks:
            # Phase-11B: log every spot tick. Volume bound is the operator's
            # responsibility (daily log rotation, etc.).
            log.write({
                "kind": "spot_tick",
                "crypto": ev.crypto.value,
                "venue": ev.venue.value,
                "ts_ms": ev.ts_ms,
                "recv_ms": ev.recv_ms,
                "price": ev.price,
            })
        return strategy.on_event(ev)
    if isinstance(ev, BookEvent):
        risk_state.now_ms = max(risk_state.now_ms, ev.recv_ms)
        return strategy.on_event(ev)
    if isinstance(ev, TradeEvent):
        risk_state.now_ms = max(risk_state.now_ms, ev.recv_ms)
        return strategy.on_event(ev)
    if isinstance(ev, LifecycleEvent):
        if ev.strike and ev.open_ms and ev.close_ms:
            strategy.register_market(ev.ticker, ev.strike, ev.open_ms, ev.close_ms)
            log.write({"kind": "lifecycle_register", "ticker": ev.ticker,
                       "status": ev.status, "strike": ev.strike})
        else:
            log.write({"kind": "lifecycle", "ticker": ev.ticker, "status": ev.status})
        return None
    if isinstance(ev, SettlementEvent):
        log.write({"kind": "settlement", "ticker": ev.ticker,
                   "result": ev.result.value, "settle_value": ev.settle_value})
        return None
    return None


async def _run_loop(
    strategy: FavoriteChaseStrategy,
    envelope: RiskEnvelope,
    risk_state: RiskState,
    execution: LiveExecution,
    kalshi_ws: KalshiWebSocketFeed,
    spot_feed: SpotFeed,
    log: LiveLogWriter,
    duration_s: float,
    client: KalshiClient | None = None,
    cryptos: list[Crypto] | None = None,
    discovery_interval_s: float = 60.0,
    cycle_tracker: CycleTracker | None = None,
    log_spot_ticks: bool = False,
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
    # In dry-run there are no orders to track; skip the fill-channel WS to
    # reduce concurrent Kalshi WS connections (capture already holds one).
    listener_task = (
        asyncio.create_task(execution.run_order_update_listener())
        if not execution.dry_run
        else None
    )
    discovery_task = (
        asyncio.create_task(_market_discovery_loop(
            client, strategy, cryptos, log,
            interval_seconds=discovery_interval_s,
            kalshi_ws=kalshi_ws,
        ))
        if (client is not None and cryptos)
        else None
    )

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
                decision = _route(
                    ev, strategy, risk_state, log,
                    cycle_tracker=cycle_tracker,
                    log_spot_ticks=log_spot_ticks,
                )
                if decision is None:
                    continue
                decision = envelope.check(decision, risk_state)
                log.write({
                    "kind": "decision",
                    "ticker": decision.ticker,
                    "action": decision.action.value,
                    "side": decision.side.value if decision.side else None,
                    "size": decision.size,
                    "confidence": decision.confidence,
                    "reason": decision.reason,
                    "diagnostics": decision.diagnostics,
                })
                await execution.submit(decision)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # An execution / parse defect must NEVER kill the engine.
                # Log a structured event and keep the loop running so other
                # markets in the cycle continue to be evaluated.
                log.write({
                    "kind": "submit_error",
                    "error": repr(exc),
                    "ticker": getattr(decision, "ticker", None)
                              if "decision" in dir() else None,
                })
    finally:
        for task in (spot_task, ws_task, listener_task, discovery_task):
            if task is None:
                continue
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass


def _diag(msg: str) -> None:
    """Unbuffered stderr breadcrumb for boot-phase diagnostics."""
    print(f"[diag] {msg}", file=sys.stderr, flush=True)


async def _amain(args: argparse.Namespace) -> int:
    _diag("amain entered")
    # ---- credentials ----
    key_path = os.environ.get("KALSHI_API_KEY_PATH")
    if not key_path:
        print("ERROR: KALSHI_API_KEY_PATH env var is not set", file=sys.stderr)
        return 2
    if not Path(key_path).exists():
        print(f"ERROR: KALSHI_API_KEY_PATH does not exist: {key_path}", file=sys.stderr)
        return 2
    try:
        creds = _read_env_file(key_path)
    except Exception as exc:
        print(f"ERROR: cannot read {key_path}: {exc}", file=sys.stderr)
        return 2
    api_key = creds.get("KALSHI_API_KEY")
    pem_path = creds.get("KALSHI_PRIVATE_KEY_PATH")
    if not api_key or not pem_path:
        print(
            "ERROR: KALSHI_API_KEY_PATH file is missing "
            "KALSHI_API_KEY or KALSHI_PRIVATE_KEY_PATH",
            file=sys.stderr,
        )
        return 2
    if not Path(pem_path).exists():
        print(f"ERROR: PEM file does not exist: {pem_path}", file=sys.stderr)
        return 2
    pem_bytes = Path(pem_path).read_bytes()
    _diag(f"creds loaded; pem={len(pem_bytes)}B key_id_len={len(api_key)}")

    # ---- config ----
    try:
        cryptos = [Crypto(c.strip().upper()) for c in args.cryptos.split(",") if c.strip()]
    except ValueError as exc:
        print(f"ERROR: invalid --cryptos value: {exc}", file=sys.stderr)
        return 2
    if not cryptos:
        print("ERROR: --cryptos must list at least one crypto", file=sys.stderr)
        return 2

    log = LiveLogWriter(args.log_path)

    # ---- wire components ----
    from kalshi_engine.config import MODELS_DIR
    cutpoints_path = MODELS_DIR / "phase4_cutpoints" / args.cutpoints_version / "cutpoints.json"
    if not cutpoints_path.exists():
        print(f"ERROR: cutpoints artifact not found: {cutpoints_path}",
              file=sys.stderr)
        return 2
    model = Phase4CutpointsModel(
        cutpoints_path=str(cutpoints_path),
        align_mode=args.align_mode,
        time_of_day_skip=(args.time_of_day_skip == "enabled"),
    )
    strategy = FavoriteChaseStrategy(
        model,
        log_writer=log,
        snapshot_interval_ms=args.snapshot_interval_ms,
        reentry_mode=args.reentry_mode,
        reentry_cutoff_ms=int(args.reentry_cutoff_min * 60_000),
        reentry_throttle_ms=int(args.reentry_throttle_sec * 1_000),
        pre_trigger_observation=(args.pre_trigger_observation == "enabled"),
    )
    envelope = RiskEnvelope(
        daily_loss_cap_cents=args.daily_cap_cents,
        max_contracts_per_trade=args.max_contracts,
    )
    risk_state = RiskState()
    spot_feed = SpotFeed(cryptos, spot_source=args.spot_source)

    _diag("entering KalshiClient context")
    async with KalshiClient(api_key, pem_bytes) as client:
        _diag("KalshiClient ready; constructing LiveExecution")
        execution = LiveExecution(
            client, log, dry_run=args.dry_run, stop_mode=args.stop_mode,
        )

        # ---- discover + register current markets ----
        _diag(f"discovery start; cryptos={[c.value for c in cryptos]}")
        markets = await _discover_markets(client, cryptos, log)
        _diag(f"discovery done; markets={len(markets)}")
        for m in markets:
            strategy.register_market(
                m["ticker"], m["strike"], m["open_ms"], m["close_ms"],
            )
        if not markets:
            log.write({"kind": "boot_abort", "reason": "no_markets_discovered"})
            print("ERROR: no markets discovered for the configured cryptos",
                  file=sys.stderr)
            return 3

        # ---- boot reconciliation: import existing account positions ----
        # Any market the account already holds is added to execution state and
        # marked decided on the strategy so the engine never re-enters.
        _diag("boot reconcile from /portfolio/positions ...")
        await execution.reconcile_from_account_at_boot(strategy)
        _diag(f"boot reconcile done; local positions={len(execution.open_positions)}")

        # ---- drain spot warmup BEFORE WS subscribe (eliminates warmup-vs-book race) ----
        _diag("draining spot warmup into strategy + risk_state ...")
        warmup_n = await spot_feed.bootstrap_warmup_into(strategy, risk_state)
        _diag(f"warmup drained; {warmup_n} spot events")

        # ---- boot event (full config snapshot) ----
        _diag("writing boot event")
        log.write({
            "kind": "boot",
            "strategy": args.strategy,
            "model": args.model,
            "model_cutpoints_version": model.cutpoints.get("version"),
            "model_cutpoints_path": str(model.cutpoints_path),
            "cryptos": [c.value for c in cryptos],
            "stop_mode": args.stop_mode,
            "bps_gate": args.bps_gate,
            "max_contracts": args.max_contracts,
            "daily_cap_cents": args.daily_cap_cents,
            "dry_run": args.dry_run,
            "duration_s": args.duration_s,
            "spot_source": args.spot_source,
            "align_mode": args.align_mode,
            "reentry_mode": args.reentry_mode,
            "reentry_cutoff_min": args.reentry_cutoff_min,
            "reentry_throttle_sec": args.reentry_throttle_sec,
            "time_of_day_skip": args.time_of_day_skip,
            "cutpoints_version": args.cutpoints_version,
            "pre_trigger_observation": args.pre_trigger_observation,
            "markets_registered": len(markets),
            "warmup_events_drained": warmup_n,
            "log_path": str(args.log_path),
        })

        # ---- run main loop ----
        _diag("constructing kalshi_ws + entering run_loop")
        kalshi_ws = KalshiWebSocketFeed(
            client.signer, tickers=[m["ticker"] for m in markets],
        )
        # Phase-11C: cycle tracker (no-op unless --cycle-summary supplied).
        cycle_tracker = CycleTracker(
            log_writer=log,
            strategy=strategy,
            execution=execution,
            cryptos=cryptos,
            enabled=args.cycle_summary,
        )

        await _run_loop(
            strategy, envelope, risk_state, execution,
            kalshi_ws, spot_feed, log, args.duration_s,
            client=client, cryptos=cryptos,
            discovery_interval_s=60.0,
            cycle_tracker=cycle_tracker,
            log_spot_ticks=args.log_spot_ticks,
        )

        # ---- reconcile on shutdown ----
        await execution.reconcile()
        log.write({"kind": "shutdown"})
    return 0


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        return asyncio.run(_amain(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
