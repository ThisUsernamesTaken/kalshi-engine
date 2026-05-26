"""Phase 13.0 — Hourglass 1hr observer entry point.

Read-only observer for KX{C}D 1hr digital markets. Subscribes to book + spot
feeds; emits `book_at_1hr_pretrigger` envelopes at T+30/40/45/50/55 of each
cycle. No orders, no risk envelope, no execution.

Runs as a separate process from `bin.live`. Same Kalshi API key (read-only
WS subscriptions are safe concurrent with live trading).

    python -m kalshi_engine.bin.observe_1hr
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from kalshi_engine.core.events import (
    BookEvent, LifecycleEvent, SettlementEvent, SpotEvent, TradeEvent,
)
from kalshi_engine.core.types import Crypto
from kalshi_engine.execution.kalshi_client import KalshiClient
from kalshi_engine.feeds.bitstamp_depth import BitstampDepthPoller
from kalshi_engine.feeds.kalshi_ws import KalshiWebSocketFeed
from kalshi_engine.feeds.spot_ws import SpotFeed
from kalshi_engine.strategies.hourglass_observer import HourglassObserverStrategy
from kalshi_engine.warehouse.adapters import LiveLogWriter
from kalshi_engine.warehouse.settlement import _iso_to_ms

from kalshi_engine.config import RAW_DIR

DEFAULT_LOG_PATH = str(RAW_DIR / "live_logs" / "hourglass_observer.jsonl")


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
    # Pattern: ...-T<numeric>
    idx = ticker.rfind("-T")
    if idx == -1:
        return 0.0
    tail = ticker[idx + 2:]
    try:
        return float(tail)
    except (TypeError, ValueError):
        return 0.0

SERIES_1HR_FOR_CRYPTO = {
    Crypto.BTC: "KXBTCD",
    Crypto.ETH: "KXETHD",
    Crypto.SOL: "KXSOLD",
    Crypto.XRP: "KXXRPD",
    Crypto.DOGE: "KXDOGED",
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
    p = argparse.ArgumentParser(description="kalshi_engine hourglass 1hr observer")
    p.add_argument("--cryptos", default="BTC,ETH,SOL,XRP,DOGE",
                   help="comma-separated crypto symbols")
    p.add_argument("--observe-times", default="5,10,15,20,25,30,40,45,50,55",
                   help="comma-separated minutes into cycle to sample. "
                        "Default 5,10,15,20,25,30,40,45,50,55 (Phase 14.4 — "
                        "intra-cycle coverage to locate the optimal T+x for "
                        "each product). Earlier windows added because the "
                        "Phase 14.2a observer data showed favorites are "
                        "already pinned (med >$0.99) by T+30 across all 1hr "
                        "cryptos; the action is BEFORE T+30, not after.")
    p.add_argument("--spot-source", default="bitstamp",
                   choices=["bitstamp", "bitstamp-ws", "coinbase"],
                   help="spot price source")
    p.add_argument("--log-path", default=DEFAULT_LOG_PATH,
                   help="output JSONL path (default: HDD warehouse)")
    p.add_argument("--duration-s", type=float, default=0.0,
                   help="0 = run forever; else exit after N seconds")
    return p.parse_args(argv)


def _diag(msg: str) -> None:
    print(f"[diag] {msg}", file=sys.stderr, flush=True)


async def _discover_1hr_markets(
    client: KalshiClient, cryptos: list[Crypto], log: LiveLogWriter,
) -> list[dict]:
    out: list[dict] = []
    for crypto in cryptos:
        series = SERIES_1HR_FOR_CRYPTO[crypto]
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
    log.write({"kind": "discovery", "count": len(out), "by_series": counts})
    return out


async def _discovery_loop(
    client: KalshiClient, observer: HourglassObserverStrategy,
    cryptos: list[Crypto], log: LiveLogWriter,
    interval_seconds: float = 60.0, kalshi_ws=None,
) -> None:
    from collections import defaultdict
    while True:
        try:
            newly: dict[str, list[str]] = defaultdict(list)
            for crypto in cryptos:
                series = SERIES_1HR_FOR_CRYPTO[crypto]
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
                    if not ticker or ticker in observer.markets:
                        continue
                    strike = _strike_from_market(m)
                    open_ms = _iso_to_ms(m.get("open_time"))
                    close_ms = _iso_to_ms(m.get("close_time"))
                    if strike <= 0 or open_ms is None or close_ms is None:
                        continue
                    observer.register_market(ticker, strike, open_ms, close_ms)
                    newly[series].append(ticker)
            if newly:
                log.write({
                    "kind": "market_discovery",
                    "newly_registered_count": sum(len(v) for v in newly.values()),
                    "total_registered": len(observer.markets),
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


def _route(ev, observer):
    """All events go to the observer; no decisions returned."""
    observer.on_event(ev)


async def _run_loop(observer, kalshi_ws, spot_feed, log, duration_s):
    import time
    queue: asyncio.Queue = asyncio.Queue()
    deadline = (time.time() + duration_s) if duration_s > 0 else None

    async def pump(source, gen):
        try:
            async for ev in gen:
                await queue.put((source, ev))
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            log.write({"kind": "feed_error", "source": source, "error": repr(exc)})

    spot_task = asyncio.create_task(pump("spot", spot_feed.events()))
    ws_task = asyncio.create_task(pump("kalshi", kalshi_ws.events()))
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
                _route(ev, observer)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.write({"kind": "observe_error", "error": repr(exc)})
    finally:
        for task in (spot_task, ws_task):
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass


async def _amain(args: argparse.Namespace) -> int:
    _diag("amain entered")
    key_path = os.environ.get("KALSHI_API_KEY_PATH")
    if not key_path:
        print("ERROR: KALSHI_API_KEY_PATH env var not set", file=sys.stderr)
        return 2
    if not Path(key_path).exists():
        print(f"ERROR: KALSHI_API_KEY_PATH does not exist: {key_path}", file=sys.stderr)
        return 2
    creds = _read_env_file(key_path)
    api_key = creds.get("KALSHI_API_KEY")
    pem_path = creds.get("KALSHI_PRIVATE_KEY_PATH")
    if not api_key or not pem_path or not Path(pem_path).exists():
        print("ERROR: bad credentials", file=sys.stderr)
        return 2
    pem_bytes = Path(pem_path).read_bytes()
    _diag(f"creds loaded; pem={len(pem_bytes)}B")

    try:
        cryptos = [Crypto(c.strip().upper()) for c in args.cryptos.split(",") if c.strip()]
    except ValueError as exc:
        print(f"ERROR: invalid --cryptos: {exc}", file=sys.stderr)
        return 2
    try:
        observe_times = tuple(int(x.strip()) for x in args.observe_times.split(",") if x.strip())
    except ValueError as exc:
        print(f"ERROR: invalid --observe-times: {exc}", file=sys.stderr)
        return 2

    log = LiveLogWriter(args.log_path)
    # Phase 14.2a — Bitstamp depth poller (TTL-cached, 30s default) feeds
    # each envelope with ±0.5%/±1% depth + spread for the underlying.
    depth_poller = BitstampDepthPoller(ttl_seconds=30.0)
    observer = HourglassObserverStrategy(
        log_writer=log, observe_minutes=observe_times,
        liquidity_poller=depth_poller,
    )
    spot_feed = SpotFeed(cryptos, spot_source=args.spot_source)

    _diag("entering KalshiClient")
    async with KalshiClient(api_key, pem_bytes) as client, depth_poller:
        _diag("discovery start")
        markets = await _discover_1hr_markets(client, cryptos, log)
        _diag(f"discovered {len(markets)} 1hr markets")
        for m in markets:
            observer.register_market(m["ticker"], m["strike"], m["open_ms"], m["close_ms"])
        if not markets:
            log.write({"kind": "boot_abort", "reason": "no_1hr_markets_discovered"})
            print("ERROR: no 1hr markets discovered", file=sys.stderr)
            return 3

        _diag("draining spot warmup ...")
        warmup_n = await spot_feed.bootstrap_warmup_into(observer, _RiskStateStub())
        _diag(f"warmup drained; {warmup_n} spot events")

        log.write({
            "kind": "boot",
            "process": "hourglass_observer",
            "cryptos": [c.value for c in cryptos],
            "observe_times": list(observe_times),
            "spot_source": args.spot_source,
            "markets_registered": len(markets),
            "warmup_events_drained": warmup_n,
            "log_path": str(args.log_path),
        })

        _diag("constructing kalshi_ws + entering run_loop")
        kalshi_ws = KalshiWebSocketFeed(
            client.signer, tickers=[m["ticker"] for m in markets],
        )
        # Background discovery loop
        discovery_task = asyncio.create_task(_discovery_loop(
            client, observer, cryptos, log, interval_seconds=60.0,
            kalshi_ws=kalshi_ws,
        ))
        # Phase 14.2a — background Bitstamp depth refresh (every 20s per crypto,
        # well under the 30s cache TTL so get_depth is never stale at emit time).
        async def _depth_refresh_loop():
            while True:
                for c in cryptos:
                    try:
                        await depth_poller.refresh(c.value)
                    except Exception as exc:
                        log.write({"kind": "depth_refresh_error",
                                    "crypto": c.value, "error": repr(exc)[:80]})
                try:
                    await asyncio.sleep(20.0)
                except asyncio.CancelledError:
                    return
        depth_task = asyncio.create_task(_depth_refresh_loop())
        try:
            await _run_loop(observer, kalshi_ws, spot_feed, log, args.duration_s)
        finally:
            for t in (discovery_task, depth_task):
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass
        log.write({"kind": "shutdown", "process": "hourglass_observer"})
    return 0


class _RiskStateStub:
    """Minimal stub matching the spot warmup API; observer has no risk state."""

    def __init__(self):
        self.now_ms = 0
        self.last_spot_ms: dict[str, int] = {}


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        return asyncio.run(_amain(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
