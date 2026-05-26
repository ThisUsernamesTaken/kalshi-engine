"""Phase 14.0 — Equity-index observer entry point.

Read-only observer for KXINXU / KXNASDAQ100U 1hr above/below markets.
Subscribes to Kalshi book updates, polls Alpaca for SPY/QQQ on-demand at
configured scan minutes (default T+30/40/50), and emits
``book_at_inxu_pretrigger`` envelopes. No orders, no risk envelope, no
execution. Designed for 2+ weeks of data collection before any live
deployment.

Differs from ``observe_1hr.py``:
- Spot feed is REST-polled on-demand (not WS-streamed continuously) —
  Alpaca free tier is 200 req/min, our footprint is ~36 req/day.
- Operates only during US Regular Trading Hours (9:30-16:00 ET, Mon-Fri).
  Outside RTH, the Kalshi WS stays subscribed but no envelopes emit.
- Uses ETF proxy (SPY for SPX, QQQ for NDX) — see ``core.equity`` for the
  basis caveat.

Run:
    python -m kalshi_engine.bin.observe_inxu --equities SPX,NDX
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from kalshi_engine.config import RAW_DIR
from kalshi_engine.core.equity import Equity, SPECS
from kalshi_engine.execution.kalshi_client import KalshiClient
from kalshi_engine.feeds.alpaca_spot import (
    AlpacaSpotPoller, credentials_from_env,
)
from kalshi_engine.feeds.kalshi_ws import KalshiWebSocketFeed
from kalshi_engine.warehouse.adapters import LiveLogWriter
from kalshi_engine.warehouse.settlement import _iso_to_ms

DEFAULT_LOG_PATH = str(RAW_DIR / "live_logs" / "inxu_observer.jsonl")


def _diag(msg: str) -> None:
    print(f"[diag] {msg}", file=sys.stderr, flush=True)


def _strike_from_market(m: dict) -> float:
    """Mirrors observe_1hr._strike_from_market: try floor_strike, fall back
    to parsing the trailing -T<numeric> segment of the ticker."""
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
    p = argparse.ArgumentParser(description="kalshi_engine equity-index observer (KXINXU / KXNASDAQ100U)")
    p.add_argument("--equities", default="SPX",
                   help="comma-separated equity symbols (SPX,NDX). Default SPX only for tight scope.")
    p.add_argument("--observe-times", default="30,40,50",
                   help="minutes into cycle to sample (default 30,40,50)")
    p.add_argument("--log-path", default=DEFAULT_LOG_PATH,
                   help="JSONL output path")
    p.add_argument("--duration-s", type=float, default=0.0,
                   help="0 = run forever; else exit after N seconds")
    p.add_argument("--alpaca-credentials",
                   default=os.environ.get("ALPACA_CREDENTIALS_PATH", ""),
                   help="Path to Alpaca dotenv file. Falls back to "
                        "ALPACA_API_KEY_ID + ALPACA_API_SECRET_KEY env vars.")
    return p.parse_args(argv)


class _InxuObserverState:
    """Tracks registered markets + per-cycle dedup so each window fires once."""

    def __init__(self, observe_minutes: tuple[int, ...]) -> None:
        self.markets: dict[str, dict] = {}  # ticker -> {strike, open_ms, close_ms, series, equity}
        self.observe_minutes = observe_minutes
        # (ticker, window_label) -> done flag (one-shot per cycle)
        self.fired: set[tuple[str, str]] = set()

    def register(self, ticker: str, strike: float, open_ms: int,
                  close_ms: int, series: str, equity: Equity) -> None:
        self.markets[ticker] = {
            "strike": strike, "open_ms": open_ms, "close_ms": close_ms,
            "series": series, "equity": equity,
        }

    def window_label(self, elapsed_min: float) -> str | None:
        """Return 'T+30' / 'T+40' / 'T+50' if elapsed is within 60s of a
        target mark, else None."""
        for tgt in self.observe_minutes:
            if abs(elapsed_min - tgt) < 1.0:  # 60s tolerance window
                return f"T+{tgt}"
        return None


async def _discover_markets(client: KalshiClient, equities: list[Equity],
                             log: LiveLogWriter,
                             ) -> list[dict]:
    out: list[dict] = []
    for eq in equities:
        spec = SPECS[eq]
        try:
            markets = await client.list_markets(
                series_ticker=spec.kalshi_series, status="open", limit=200,
            )
        except Exception as exc:
            log.write({"kind": "discovery_error",
                       "series": spec.kalshi_series, "error": repr(exc)})
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
                "open_ms": open_ms, "close_ms": close_ms,
                "series": spec.kalshi_series, "equity": eq,
            })
    counts: dict[str, int] = {}
    for m in out:
        counts[m["series"]] = counts.get(m["series"], 0) + 1
    log.write({"kind": "discovery", "count": len(out), "by_series": counts})
    return out


async def _handle_book(ev, state: _InxuObserverState,
                        alpaca: AlpacaSpotPoller, log: LiveLogWriter) -> None:
    """On every book update, check if we're inside an observe window for
    this ticker's cycle. If yes and not yet fired this cycle/window, poll
    Alpaca and emit the envelope."""
    market = state.markets.get(ev.ticker)
    if not market:
        return
    elapsed_min = (ev.recv_ms - market["open_ms"]) / 60_000.0
    wl = state.window_label(elapsed_min)
    if wl is None:
        return
    key = (ev.ticker, wl)
    if key in state.fired:
        return  # already captured this window for this cycle
    eq: Equity = market["equity"]
    spec = SPECS[eq]
    trade = await alpaca.get_last_trade(spec.alpaca_symbol)
    if trade is None:
        # Market closed OR poll failed. Log and skip - implied-spot
        # fallback to be wired in a future phase per the prototype findings.
        log.write({
            "kind": "spot_poll_skip", "ticker": ev.ticker,
            "alpaca_symbol": spec.alpaca_symbol, "window_label": wl,
            "reason": "market_closed_or_poll_failed",
        })
        state.fired.add(key)
        return
    # Build pretrigger envelope mirroring book_at_1hr_pretrigger schema
    envelope = {
        "kind": "book_at_inxu_pretrigger",
        "ticker": ev.ticker,
        "equity": eq.value,
        "kalshi_series": spec.kalshi_series,
        "alpaca_symbol": spec.alpaca_symbol,
        "ts_ms": ev.recv_ms,
        "cycle_open_ms": market["open_ms"],
        "cycle_close_ms": market["close_ms"],
        "elapsed_min": elapsed_min,
        "tau_min": (market["close_ms"] - ev.recv_ms) / 60_000.0,
        "window_label": wl,
        "yes_bid": ev.yes_bid, "yes_ask": ev.yes_ask,
        "no_bid": ev.no_bid, "no_ask": ev.no_ask,
        "spot": trade.price,
        "spot_ts_ms": trade.ts_ms,
        "spot_exchange": trade.exchange,
        "strike": market["strike"],
    }
    log.write(envelope)
    state.fired.add(key)


async def _amain(args: argparse.Namespace) -> int:
    _diag("amain entered")
    key_path = os.environ.get("KALSHI_API_KEY_PATH")
    if not key_path or not Path(key_path).exists():
        print("ERROR: KALSHI_API_KEY_PATH missing or invalid", file=sys.stderr)
        return 2
    creds = _read_env_file(key_path)
    api_key = creds.get("KALSHI_API_KEY")
    pem_path = creds.get("KALSHI_PRIVATE_KEY_PATH")
    if not api_key or not pem_path or not Path(pem_path).exists():
        print("ERROR: bad Kalshi credentials", file=sys.stderr)
        return 2
    pem_bytes = Path(pem_path).read_bytes()

    try:
        equities = [Equity(e.strip().upper()) for e in args.equities.split(",") if e.strip()]
    except ValueError as exc:
        print(f"ERROR: invalid --equities: {exc}", file=sys.stderr)
        return 2
    try:
        observe_times = tuple(int(x.strip()) for x in args.observe_times.split(",") if x.strip())
    except ValueError as exc:
        print(f"ERROR: invalid --observe-times: {exc}", file=sys.stderr)
        return 2

    try:
        alp_key, alp_sec = credentials_from_env(args.alpaca_credentials or None)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    log = LiveLogWriter(args.log_path)
    state = _InxuObserverState(observe_minutes=observe_times)

    async with KalshiClient(api_key, pem_bytes) as client:
        _diag("discovery start")
        markets = await _discover_markets(client, equities, log)
        for m in markets:
            state.register(m["ticker"], m["strike"], m["open_ms"],
                            m["close_ms"], m["series"], m["equity"])
        _diag(f"registered {len(markets)} markets")
        if not markets:
            log.write({"kind": "boot_abort", "reason": "no_markets_discovered"})
            return 3

        log.write({
            "kind": "boot",
            "process": "inxu_observer",
            "equities": [e.value for e in equities],
            "observe_times": list(observe_times),
            "markets_registered": len(markets),
            "log_path": str(args.log_path),
        })

        async with AlpacaSpotPoller(alp_key, alp_sec) as alpaca:
            tickers = list(state.markets.keys())
            kalshi_ws = KalshiWebSocketFeed(client, tickers)
            _diag("entering run loop")
            deadline = time.time() + args.duration_s if args.duration_s > 0 else None
            async for ev in kalshi_ws.events():
                if deadline and time.time() >= deadline:
                    break
                # Only book events drive observation
                if getattr(ev, "yes_bid", None) is None:
                    continue
                try:
                    await _handle_book(ev, state, alpaca, log)
                except Exception as exc:
                    log.write({"kind": "observe_error",
                               "ticker": getattr(ev, "ticker", "?"),
                               "error": repr(exc)})
    return 0


def main(argv=None) -> int:
    args = parse_args(argv)
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    sys.exit(main())
