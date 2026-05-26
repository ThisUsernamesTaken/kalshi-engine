"""Bitstamp orderbook depth poller for liquidity instrumentation.

Polls ``/v2/order_book/{pair}/`` REST endpoint, computes ±0.5%/±1% depth
and top-of-book spread, and caches the result for a configurable TTL
(default 30s). Used by the 1hr observer (Phase 14.2a) to annotate each
``book_at_1hr_pretrigger`` envelope with underlying-spot liquidity data
that can later be backtested as a candidate gate.

Free, no auth. Bitstamp doesn't rate-limit aggressively in practice but
we cache to avoid hammering it (typical observer cadence: 25 envelopes
in a few seconds during T+30 bursts).

Failures (HTTP error, parse error, network timeout) return ``None`` and
the caller logs a ``bitstamp_poll_error`` field in the envelope.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Optional

import aiohttp

BITSTAMP_REST = "https://www.bitstamp.net/api/v2"

_PAIR_FOR_CRYPTO = {
    "BTC": "btcusd",
    "ETH": "ethusd",
    "SOL": "solusd",
    "XRP": "xrpusd",
    "DOGE": "dogeusd",
}


class BitstampDepthPoller:
    """Async-callable depth poller with per-pair TTL cache."""

    def __init__(self, ttl_seconds: float = 30.0,
                 timeout_seconds: float = 5.0) -> None:
        self._ttl = float(ttl_seconds)
        self._timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        self._session: aiohttp.ClientSession | None = None
        self._cache: dict[str, tuple[float, dict | None]] = {}
        # Background-fetch lock per pair so concurrent envelopes don't all
        # spawn a request.
        self._locks: dict[str, asyncio.Lock] = {}

    async def __aenter__(self) -> "BitstampDepthPoller":
        self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    def get_depth(self, crypto: str) -> Optional[dict]:
        """Sync accessor used by the observer (which is sync in `on_event`).
        Returns the most recently cached snapshot, or None if no fetch
        has succeeded yet. The async refresh runs separately.
        """
        return (self._cache.get(crypto.upper(), (0, None))[1])

    async def refresh(self, crypto: str) -> Optional[dict]:
        """Force-refresh one pair's cache. Returns the new snapshot or
        None on failure. Safe to call concurrently — one network request
        per pair at a time via a per-pair lock."""
        crypto = crypto.upper()
        pair = _PAIR_FOR_CRYPTO.get(crypto)
        if not pair:
            return None
        if self._session is None:
            raise RuntimeError("BitstampDepthPoller must be used as a context manager")
        lock = self._locks.setdefault(crypto, asyncio.Lock())
        async with lock:
            now = time.time()
            cached = self._cache.get(crypto, (0, None))
            if cached[1] is not None and now - cached[0] < self._ttl:
                return cached[1]
            try:
                async with self._session.get(f"{BITSTAMP_REST}/order_book/{pair}/") as resp:
                    if resp.status != 200:
                        return cached[1]
                    data = await resp.json()
            except (aiohttp.ClientError, asyncio.TimeoutError, json.JSONDecodeError):
                return cached[1]
            snap = self._compute_depth(data)
            if snap is not None:
                self._cache[crypto] = (time.time(), snap)
                return snap
            return cached[1]

    @staticmethod
    def _compute_depth(book: dict) -> Optional[dict]:
        try:
            bids = [(float(p), float(s)) for p, s in book.get("bids", [])]
            asks = [(float(p), float(s)) for p, s in book.get("asks", [])]
            if not bids or not asks:
                return None
            top_bid = bids[0][0]
            top_ask = asks[0][0]
            mid = (top_bid + top_ask) / 2.0
            spread = top_ask - top_bid
            spread_bps = (spread / mid) * 1e4 if mid > 0 else 0
            def depth(side, pct, comp):
                # Add a tiny tolerance (1 sat ~ 1e-8 relative) so prices that
                # are mathematically exactly on the threshold aren't excluded
                # by float imprecision (e.g. 100.0*1.005 = 100.49999...).
                thr = mid * (1 + (-pct / 100 if comp == "ge" else pct / 100))
                tol = thr * 1e-9
                if comp == "ge":
                    return sum(s for p, s in side if p >= thr - tol)
                else:
                    return sum(s for p, s in side if p <= thr + tol)
            return {
                "mid": mid,
                "spread": spread,
                "spread_bps": spread_bps,
                "bid_depth_0p5pct": depth(bids, 0.5, "ge"),
                "ask_depth_0p5pct": depth(asks, 0.5, "le"),
                "bid_depth_1pct": depth(bids, 1.0, "ge"),
                "ask_depth_1pct": depth(asks, 1.0, "le"),
            }
        except (TypeError, ValueError):
            return None
