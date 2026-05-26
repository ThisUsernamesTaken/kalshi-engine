"""On-demand Alpaca spot polling for equity index proxies (SPY, QQQ).

Unlike the crypto spot feeds (which stream continuously via WS), the
equity feed is **request-driven**: callers poll ``get_last_trade(symbol)``
at known scan windows (typically T+30/40/50 of an hourly Kalshi cycle).
This pattern fits Alpaca's free tier (200 requests/minute) trivially:
two symbols polled three times per hour over a 6.5-hour trading day
is 36 requests/day, ~0.025/min average.

Auth: APCA-API-KEY-ID + APCA-API-SECRET-KEY env vars (or constructor
args). Endpoint: ``data.alpaca.markets/v2/stocks/{symbol}/trades/latest``.
Free tier returns real-time IEX-exchange trades for US equities; SPY and
QQQ are the densest IEX-coverage tickers in their respective indices.

The adapter intentionally does NOT keep a websocket open: equity markets
are closed 16:00-09:30 ET (and weekends), and persistent connections
across closures complicate state. Polling is stateless and the cost is
negligible at our cadence.

``get_last_trade`` returns ``None`` outside RTH (9:30-16:00 ET, Mon-Fri),
not stale data — the caller must decide whether to use implied-spot from
the contract chain as a fallback (see implied_spot_prototype.py).
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from datetime import datetime, time, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import aiohttp

ALPACA_DATA_BASE = "https://data.alpaca.markets/v2"
_ET = ZoneInfo("America/New_York")
_RTH_OPEN = time(9, 30)
_RTH_CLOSE = time(16, 0)


@dataclass(frozen=True, slots=True)
class EquityTrade:
    """A single Alpaca trade print."""

    symbol: str        # e.g. "SPY"
    price: float       # USD
    ts_ms: int         # exchange timestamp in ms
    recv_ms: int       # local receipt ms
    exchange: str      # e.g. "V" = IEX


class AlpacaSpotPoller:
    """REST-only Alpaca client for on-demand last-trade polling.

    Usage:
        async with AlpacaSpotPoller(key_id, secret) as p:
            trade = await p.get_last_trade("SPY")
            if trade is not None:
                print(trade.price, trade.ts_ms)
    """

    def __init__(self, key_id: str, secret_key: str,
                 timeout_s: float = 5.0,
                 base_url: str = ALPACA_DATA_BASE) -> None:
        if not key_id or not secret_key:
            raise ValueError("Alpaca credentials required (key_id + secret_key)")
        self._headers = {
            "APCA-API-KEY-ID": key_id,
            "APCA-API-SECRET-KEY": secret_key,
            "accept": "application/json",
        }
        self._timeout = aiohttp.ClientTimeout(total=timeout_s)
        self._base = base_url.rstrip("/")
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> "AlpacaSpotPoller":
        self._session = aiohttp.ClientSession(
            headers=self._headers, timeout=self._timeout,
        )
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    @staticmethod
    def is_market_open(now_utc: datetime | None = None) -> bool:
        """RTH check: Mon-Fri, 9:30-16:00 ET. No holiday calendar (caller can
        layer one). Returns False for weekends and outside trading hours."""
        if now_utc is None:
            now_utc = datetime.now(tz=timezone.utc)
        et_now = now_utc.astimezone(_ET)
        if et_now.weekday() >= 5:  # Sat / Sun
            return False
        return _RTH_OPEN <= et_now.time() <= _RTH_CLOSE

    async def get_last_trade(self, symbol: str,
                              respect_rth: bool = True) -> Optional[EquityTrade]:
        """Single REST call for the most recent trade. Returns None when:
        - outside RTH (if respect_rth=True)
        - Alpaca returns an empty / malformed payload
        - HTTP error (logged via exception; caller decides retry policy).
        """
        if self._session is None:
            raise RuntimeError("AlpacaSpotPoller must be used as a context manager")
        if respect_rth and not self.is_market_open():
            return None
        url = f"{self._base}/stocks/{symbol}/trades/latest"
        import time as _time
        try:
            async with self._session.get(url) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return None
        trade = (data or {}).get("trade") or {}
        price = trade.get("p")
        ts_str = trade.get("t")
        if price is None or ts_str is None:
            return None
        # Alpaca timestamps are RFC3339 (e.g. "2026-05-26T19:59:30.123456789Z")
        try:
            # Trim sub-microsecond precision for fromisoformat
            ts_clean = ts_str.replace("Z", "+00:00")
            if "." in ts_clean:
                head, tail = ts_clean.split(".", 1)
                tz_idx = max(tail.find("+"), tail.find("-"))
                if tz_idx == -1:
                    ts_clean = head
                else:
                    frac = tail[:tz_idx][:6]  # microseconds max
                    ts_clean = f"{head}.{frac}{tail[tz_idx:]}"
            dt = datetime.fromisoformat(ts_clean)
            ts_ms = int(dt.timestamp() * 1000)
        except (ValueError, AttributeError):
            ts_ms = int(_time.time() * 1000)
        return EquityTrade(
            symbol=symbol,
            price=float(price),
            ts_ms=ts_ms,
            recv_ms=int(_time.time() * 1000),
            exchange=str(trade.get("x") or ""),
        )

    async def get_last_trades(self, symbols: list[str]) -> dict[str, Optional[EquityTrade]]:
        """Convenience: poll multiple symbols concurrently."""
        results = await asyncio.gather(
            *(self.get_last_trade(s) for s in symbols),
            return_exceptions=True,
        )
        out: dict[str, Optional[EquityTrade]] = {}
        for sym, res in zip(symbols, results):
            out[sym] = res if isinstance(res, EquityTrade) else None
        return out


def credentials_from_env(env_path: str | None = None) -> tuple[str, str]:
    """Read Alpaca credentials. Looks for, in order:

    1. ``ALPACA_API_KEY_ID`` + ``ALPACA_API_SECRET_KEY`` env vars
    2. ``ALPACA_CREDENTIALS_PATH`` env var pointing to a dotenv file with
       those two keys.
    3. ``env_path`` arg (also a dotenv file).
    """
    kid = os.environ.get("ALPACA_API_KEY_ID")
    sec = os.environ.get("ALPACA_API_SECRET_KEY")
    if kid and sec:
        return kid, sec
    path = env_path or os.environ.get("ALPACA_CREDENTIALS_PATH")
    if not path:
        raise RuntimeError(
            "Alpaca credentials not found. Set ALPACA_API_KEY_ID + "
            "ALPACA_API_SECRET_KEY, or point ALPACA_CREDENTIALS_PATH "
            "at a dotenv file containing them.")
    from pathlib import Path
    text = Path(path).read_text(encoding="utf-8")
    env: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    kid = env.get("ALPACA_API_KEY_ID") or env.get("APCA_API_KEY_ID")
    sec = env.get("ALPACA_API_SECRET_KEY") or env.get("APCA_API_SECRET_KEY")
    if not kid or not sec:
        raise RuntimeError(f"Alpaca credentials missing in {path}")
    return kid, sec
