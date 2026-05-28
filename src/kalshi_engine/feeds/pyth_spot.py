"""On-demand Pyth Hermes spot polling for commodity settlement feeds.

Pyth Hermes is the *exact Kalshi settlement source* for the daily commodity
ladders (e.g. ``Metal.XAU/USD`` settles ``KXGOLDD``). It is free, keyless,
REST + SSE, and ships a confidence interval alongside each price. Like the
equity Alpaca poller (and unlike the streaming crypto feeds) this is
**request-driven**: callers poll ``get_latest(feed_id)`` at the dense
minutes-to-close observe marks of the daily window. At ~7-15s cadence over a
~1h daily window this is a trivial request volume.

Endpoints (no auth):
  - latest price:   ``hermes.pyth.network/v2/updates/price/latest?ids[]=<id>``
  - 1-min history:  ``benchmarks.pyth.network/v1/shims/tradingview/history``
    (used once on boot to seed the 30-min realized-vol buffer)

**Fail-closed.** ``get_latest`` returns ``None`` — never a stale or zero
price — when the feed is unreachable, malformed, reports ``price<=0`` /
``publish_time==0`` (an unpublished feed), or the publish timestamp is older
than ``max_stale_s``. The caller skips rather than scoring on bad data
(the commodity analogue of the crypto stale-spot rule). This is exactly why
the dead Brent (BRENTQ6) feed is safe even if mis-enabled: it fails closed.
"""

from __future__ import annotations

import asyncio
import time as _time
from dataclasses import dataclass
from typing import Optional

import aiohttp

HERMES_BASE = "https://hermes.pyth.network"
BENCHMARKS_BASE = "https://benchmarks.pyth.network"

# Default ceiling on Pyth update age before we treat the feed as stale and
# fail closed. The metals/energy feeds update ~every 7s; 60s is a generous
# liveness floor that still rejects a silently-dead feed.
DEFAULT_MAX_STALE_S = 60.0


@dataclass(frozen=True, slots=True)
class PythPrice:
    """A single parsed Pyth Hermes price update.

    ``price`` and ``conf`` are in the feed's quote units (USD for the
    commodity feeds). ``conf_bps`` is the confidence interval as a fraction of
    price in basis points — a data-quality discriminator with no crypto
    analogue (wide conf => the bb_div / bps_margin signals are less
    trustworthy). ``publish_time_ms`` is the Pyth publisher clock.
    """

    feed_id: str
    price: float
    conf: float
    conf_bps: float
    expo: int
    publish_time_ms: int
    recv_ms: int


def _scaled(mantissa: str | int, expo: int) -> float:
    """Pyth fixed-point (mantissa, expo) -> float, e.g. ('4499560', -3)=4499.56."""
    return int(mantissa) * (10.0 ** expo)


def parse_latest_price(
    data: dict,
    feed_id: str,
    now_ms: int,
    max_stale_s: float = DEFAULT_MAX_STALE_S,
) -> Optional[PythPrice]:
    """Parse a ``/v2/updates/price/latest`` payload for one feed, fail-closed.

    Returns ``None`` (never a degraded price) when the feed entry is missing,
    malformed, reports a non-positive price or ``publish_time==0`` (never
    published), or the update is older than ``max_stale_s``.
    """
    parsed = (data or {}).get("parsed") or []
    fid = feed_id.lower().removeprefix("0x")
    entry = None
    for p in parsed:
        if str(p.get("id", "")).lower().removeprefix("0x") == fid:
            entry = p
            break
    if entry is None:
        return None
    px = entry.get("price") or {}
    raw_price = px.get("price")
    raw_conf = px.get("conf")
    expo = px.get("expo")
    pub = px.get("publish_time")
    if raw_price is None or expo is None or pub in (None, 0):
        return None
    try:
        price = _scaled(raw_price, int(expo))
        conf = _scaled(raw_conf, int(expo)) if raw_conf is not None else 0.0
    except (TypeError, ValueError):
        return None
    if price <= 0:
        return None
    pub_ms = int(pub) * 1000
    if (now_ms - pub_ms) > max_stale_s * 1000:
        return None
    conf_bps = (conf / price) * 1e4 if price > 0 else 0.0
    return PythPrice(
        feed_id=fid,
        price=price,
        conf=conf,
        conf_bps=conf_bps,
        expo=int(expo),
        publish_time_ms=pub_ms,
        recv_ms=now_ms,
    )


def parse_benchmarks_history(data: dict) -> list[tuple[int, float]]:
    """Parse a TradingView-shim ``/history`` payload to [(ts_ms, close), ...].

    Returns [] on a non-ok status or malformed payload (best-effort boot seed).
    """
    if not isinstance(data, dict) or data.get("s") != "ok":
        return []
    ts = data.get("t") or []
    closes = data.get("c") or []
    out: list[tuple[int, float]] = []
    for t, c in zip(ts, closes):
        try:
            out.append((int(t) * 1000, float(c)))
        except (TypeError, ValueError):
            continue
    return out


class PythSpotPoller:
    """REST-only Pyth Hermes client for on-demand latest-price polling.

    Usage::

        async with PythSpotPoller() as p:
            px = await p.get_latest(GOLD_XAU_USD_FEED)
            if px is not None:
                print(px.price, px.conf_bps)
    """

    def __init__(
        self,
        timeout_s: float = 5.0,
        hermes_base: str = HERMES_BASE,
        benchmarks_base: str = BENCHMARKS_BASE,
        max_stale_s: float = DEFAULT_MAX_STALE_S,
    ) -> None:
        self._timeout = aiohttp.ClientTimeout(total=timeout_s)
        self._hermes = hermes_base.rstrip("/")
        self._benchmarks = benchmarks_base.rstrip("/")
        self._max_stale_s = max_stale_s
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> "PythSpotPoller":
        self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def get_latest(self, feed_id: str) -> Optional[PythPrice]:
        """Single REST call for the most recent price of one feed. Fail-closed
        (returns None) on HTTP error, timeout, stale, or unpublished feed."""
        if self._session is None:
            raise RuntimeError("PythSpotPoller must be used as a context manager")
        fid = feed_id.lower().removeprefix("0x")
        url = f"{self._hermes}/v2/updates/price/latest"
        params = [("ids[]", fid), ("parsed", "true")]
        try:
            async with self._session.get(url, params=params) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return None
        return parse_latest_price(
            data, fid, int(_time.time() * 1000), self._max_stale_s,
        )

    async def get_many(self, feed_ids: list[str]) -> dict[str, Optional[PythPrice]]:
        """Poll several feeds concurrently. Keys are normalized (no-0x) ids."""
        results = await asyncio.gather(
            *(self.get_latest(f) for f in feed_ids),
            return_exceptions=True,
        )
        out: dict[str, Optional[PythPrice]] = {}
        for fid, res in zip(feed_ids, results):
            key = fid.lower().removeprefix("0x")
            out[key] = res if isinstance(res, PythPrice) else None
        return out

    async def bootstrap_history(
        self, symbol: str, minutes: int = 60,
    ) -> list[tuple[int, float]]:
        """Fetch the last ``minutes`` of 1-min candles from the Pyth
        Benchmarks TradingView shim to seed the realized-vol buffer on boot.
        Best-effort: returns [] on any failure. ``symbol`` is the Pyth feed
        symbol, e.g. ``Metal.XAU/USD``."""
        if self._session is None:
            raise RuntimeError("PythSpotPoller must be used as a context manager")
        now = int(_time.time())
        url = f"{self._benchmarks}/v1/shims/tradingview/history"
        params = {
            "symbol": symbol,
            "resolution": "1",
            "from": str(now - minutes * 60),
            "to": str(now),
        }
        try:
            async with self._session.get(url, params=params) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return []
        return parse_benchmarks_history(data)
