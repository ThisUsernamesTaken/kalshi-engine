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
from kalshi_engine.strategies.hourglass_trader import HourglassTraderStrategy
from kalshi_engine.warehouse.adapters import LiveLogWriter
from kalshi_engine.warehouse.settlement import _iso_to_ms

DEFAULT_LOG_PATH = RAW_DIR / "live_logs" / "live_hourglass_trader.jsonl"


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
    p.add_argument("--daily-cap-cents", type=int, default=1000,
                   help="Daily realized-loss cap in cents. Default 1000 ($10). "
                        "Independent from the 15m engine's separate $10 cap.")
    p.add_argument("--cutpoints-version", default="v1",
                   help="Cutpoints artifact version (cutpoints.json file).")
    p.add_argument("--trigger-minutes", default="30,50",
                   help="Comma-separated minute marks into each cycle where "
                        "the trader evaluates and may enter. Default 30,50 — "
                        "skips T+45 per the observer sweep (-$5.79).")
    p.add_argument("--skip-hours", default="13",
                   help="Comma-separated UTC hours to skip entries in. Default "
                        "13 — the observer sweep flagged 13Z as catastrophic "
                        "(-$84.17 on n=16). Use empty string '' for none.")
    p.add_argument("--max-favorite-cost-decicents", type=int, default=920,
                   help="MAX_FAV_COST in decicents. Default 920 (=$0.92). "
                        "Fee-trap protection — 96%% of 1hr envelopes hit "
                        "fav~$1.00 where Kalshi taker fees eliminate edge.")
    p.add_argument("--stop-mode", default="none", choices=["none", "price"])
    p.add_argument("--bps-gate", default="enabled",
                   choices=["enabled", "disabled"])
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
                decision = _route(ev, strategy, risk_state, log)
                if decision is None:
                    continue
                decision = envelope.check(decision, risk_state)
                # The trader already logged the decision; the envelope may have
                # downsized/skipped it — log the final decision under a
                # distinct kind so post-hoc analysis can compare.
                log.write({
                    "kind": "decision_post_envelope",
                    "ticker": decision.ticker,
                    "action": decision.action.value,
                    "side": decision.side.value if decision.side else None,
                    "size": decision.size,
                    "confidence": decision.confidence,
                    "reason": decision.reason,
                })
                await execution.submit(decision)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
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
    strategy = HourglassTraderStrategy(
        log_writer=log,
        model=model,
        trigger_minutes=trigger_minutes,
        skip_hours_utc=skip_hours,
        max_favorite_cost_decicents=args.max_favorite_cost_decicents,
        max_contracts=args.max_contracts,
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
        _diag(f"discovery start; cryptos={[c.value for c in cryptos]}")
        markets = await _discover_1hr_markets(client, cryptos, log)
        _diag(f"discovery done; markets={len(markets)}")
        for m in markets:
            strategy.register_market(
                m["ticker"], m["strike"], m["open_ms"], m["close_ms"],
            )
        if not markets:
            log.write({"kind": "boot_abort",
                       "reason": "no 1hr markets discovered"})
            print("ERROR: no 1hr markets discovered", file=sys.stderr)
            return 3

        _diag("boot reconcile from /portfolio/positions ...")
        try:
            await execution.reconcile_from_account_at_boot(strategy)
        except Exception as exc:
            log.write({"kind": "boot_reconcile_error", "error": repr(exc)})
        _diag(f"boot reconcile done; local positions="
              f"{len(getattr(strategy, '_entered', set()))}")

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
            "daily_cap_cents": args.daily_cap_cents,
            "spot_source": args.spot_source,
            "stop_mode": args.stop_mode,
            "bps_gate": args.bps_gate,
            "dry_run": args.dry_run,
            "duration_s": args.duration_s,
            "markets_registered": len(markets),
            "warmup_events_drained": warmup_n,
            "log_path": str(args.log_path),
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
