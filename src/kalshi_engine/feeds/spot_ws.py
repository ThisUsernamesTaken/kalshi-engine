"""Crypto spot price feed.

Live sources (selectable via ``spot_source``):

- ``coinbase`` (RECOMMENDED, Phase-10 verified): Coinbase Exchange WS,
  ``ticker`` channel. Sub-100ms publish->receive latency, per-match updates,
  best_bid/best_ask in every frame. 2026-05-23 probe: 295 ticker frames over
  30 s across all 5 pairs (BTC 5.5/s, DOGE 0.3/s). The Phase-4 deferred
  defect (no messages streaming) is resolved - either an upstream Coinbase
  change or a transient infra issue. Falls back to ``bitstamp`` REST poll
  after 3 consecutive WS failures.
- ``bitstamp`` (Phase-4 closeout default): Bitstamp REST polling at ~2-3 s
  per crypto. Works but degrades under load - 2026-05-23 saw 13 s / 29.7 s
  stale-spot risk-skips on BTC / SOL during a macro selloff.
- ``bitstamp-ws`` (Phase-10 alternate; not recommended for live): Bitstamp
  public WS ``live_trades_<pair>`` channels. Sub-second on liquid pairs,
  but trade-only stream gaps on illiquid pairs (DOGE produced zero events
  in a 30 s probe window). Kept as infrastructure for possible future
  multi-source fusion; do not use as the sole live spot source.

Warmup is decoupled from the live stream via ``bootstrap_warmup_into`` (the
Phase-4 race fix). ``SpotEvent.price`` is in dollars - underlying prices,
not Kalshi book prices.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime
from typing import AsyncIterator, Iterable

import aiohttp
import websockets

from kalshi_engine.core.events import SpotEvent
from kalshi_engine.core.types import Crypto, Venue

COINBASE_WS_URL = "wss://ws-feed.exchange.coinbase.com"
COINBASE_REST_BASE = "https://api.exchange.coinbase.com"
BITSTAMP_REST_BASE = "https://www.bitstamp.net/api/v2"
BITSTAMP_WS_URL = "wss://ws.bitstamp.net"

_BACKOFF_BASE_S = 1.0
_BACKOFF_CAP_S = 30.0
_VALID_SOURCES = ("bitstamp", "bitstamp-ws", "coinbase")

_COINBASE_PRODUCT = {
    Crypto.BTC: "BTC-USD",
    Crypto.ETH: "ETH-USD",
    Crypto.SOL: "SOL-USD",
    Crypto.XRP: "XRP-USD",
    Crypto.DOGE: "DOGE-USD",
}
_BITSTAMP_PAIR = {
    Crypto.BTC: "btcusd",
    Crypto.ETH: "ethusd",
    Crypto.SOL: "solusd",
    Crypto.XRP: "xrpusd",
    Crypto.DOGE: "dogeusd",
}


def _utc_now_ms() -> int:
    return int(time.time() * 1000)


def _parse_iso_ms(s) -> int:
    if not s:
        return _utc_now_ms()
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return int(dt.timestamp() * 1000)
    except (ValueError, TypeError):
        return _utc_now_ms()


class SpotFeed:
    """Spot price feed - Bitstamp REST default, Coinbase WS optional alternate."""

    def __init__(
        self,
        cryptos: Iterable[Crypto],
        spot_source: str = "bitstamp",
        max_coinbase_failures: int = 3,
        coinbase_retry_after_s: float = 120.0,
        bitstamp_poll_s: float = 1.0,
        warmup_minutes: int = 30,
    ) -> None:
        self.cryptos = list(cryptos)
        if not self.cryptos:
            raise ValueError("SpotFeed needs at least one crypto")
        if spot_source not in _VALID_SOURCES:
            raise ValueError(
                f"spot_source must be one of {_VALID_SOURCES}, got {spot_source!r}"
            )
        self.spot_source = spot_source
        self.max_coinbase_failures = max_coinbase_failures
        self.coinbase_retry_after_s = coinbase_retry_after_s
        self.bitstamp_poll_s = bitstamp_poll_s
        self.warmup_minutes = warmup_minutes
        self._product_to_crypto = {_COINBASE_PRODUCT[c]: c for c in self.cryptos}

    # -- bootstrap warmup (one-shot, awaited before live stream) --------
    async def _fetch_candles(
        self, session, crypto: Crypto, start: int, end: int,
    ) -> list[SpotEvent]:
        product = _COINBASE_PRODUCT[crypto]
        url = f"{COINBASE_REST_BASE}/products/{product}/candles"
        params = {"granularity": 60, "start": start, "end": end}
        try:
            async with session.get(url, params=params) as resp:
                if resp.status >= 400:
                    return []
                candles = await resp.json()
        except Exception:
            return []
        if not isinstance(candles, list):
            return []
        out: list[SpotEvent] = []
        for entry in candles:
            try:
                ts_s = int(entry[0])
                price = float(entry[4])
            except (TypeError, ValueError, IndexError):
                continue
            if price <= 0:
                continue
            out.append(SpotEvent(
                crypto=crypto, venue=Venue.COINBASE,
                ts_ms=ts_s * 1000, recv_ms=ts_s * 1000,
                price=price,
            ))
        return out

    async def _warmup_events(self, session) -> list[SpotEvent]:
        if self.warmup_minutes <= 0:
            return []
        end = int(time.time())
        start = end - self.warmup_minutes * 60
        results = await asyncio.gather(*[
            self._fetch_candles(session, c, start, end) for c in self.cryptos
        ])
        events: list[SpotEvent] = [ev for batch in results for ev in batch]
        events.sort(key=lambda e: e.ts_ms)
        return events

    async def bootstrap_warmup_into(self, strategy, risk_state) -> int:
        """Drain warmup candles into strategy + risk_state. Call BEFORE WS."""
        if self.warmup_minutes <= 0:
            return 0
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=10),
        ) as session:
            events = await self._warmup_events(session)
        for ev in events:
            strategy.on_event(ev)
            risk_state.last_spot_ms[ev.crypto.value] = ev.ts_ms
            risk_state.now_ms = max(risk_state.now_ms, ev.ts_ms)
        return len(events)

    # -- live event stream ----------------------------------------------
    async def events(self) -> AsyncIterator[SpotEvent]:
        """Stream live SpotEvents per the configured `spot_source`."""
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=10),
        ) as session:
            async for ev in self._main_loop(session):
                yield ev

    async def _main_loop(self, session) -> AsyncIterator[SpotEvent]:
        if self.spot_source == "bitstamp":
            async for ev in self._bitstamp_primary(session):
                yield ev
        elif self.spot_source == "bitstamp-ws":
            async for ev in self._bitstamp_ws_primary():
                yield ev
        else:
            async for ev in self._coinbase_primary(session):
                yield ev

    # -- bitstamp paths -------------------------------------------------
    async def _bitstamp_primary(self, session) -> AsyncIterator[SpotEvent]:
        """Bitstamp REST polling - DEFAULT primary path (Phase-4 closeout,
        Phase-10.1 hardened with parallel polling).

        Phase-10.1 change: polls all configured pairs **concurrently** via
        ``asyncio.gather`` instead of sequentially. Combined with a 1 s
        outer interval (was 2.5 s), each pair gets a fresh tick approximately
        every second, with no per-pair stagger backpressure. The Phase-10.1
        multi-source probe showed sequential polling produced 3.4 s max gaps
        per pair under calm regime; parallel polling cuts that to ~1 s.
        Per-pair HTTP failures stay silent and the loop never raises;
        Bitstamp outages still fall through to the risk envelope's stale
        spot fail-closed behaviour.
        """
        while True:
            t0 = time.monotonic()
            results = await asyncio.gather(*[
                self._bitstamp_poll_one(session, c) for c in self.cryptos
            ], return_exceptions=True)
            for r in results:
                if isinstance(r, SpotEvent):
                    yield r
                # Exceptions from per-pair fetches are swallowed silently
                # (consistent with sequential path's silent failure mode).
            elapsed = time.monotonic() - t0
            sleep_s = max(0.0, self.bitstamp_poll_s - elapsed)
            await asyncio.sleep(sleep_s)

    async def _bitstamp_poll_until(
        self, session, deadline: float,
    ) -> AsyncIterator[SpotEvent]:
        """Bitstamp polling until `deadline` - used as fallback when
        spot_source='coinbase' and Coinbase WS fails persistently. Uses the
        same parallel-poll pattern as ``_bitstamp_primary``.
        """
        while time.time() < deadline:
            t0 = time.monotonic()
            results = await asyncio.gather(*[
                self._bitstamp_poll_one(session, c) for c in self.cryptos
            ], return_exceptions=True)
            for r in results:
                if isinstance(r, SpotEvent):
                    yield r
            elapsed = time.monotonic() - t0
            sleep_s = max(0.0, self.bitstamp_poll_s - elapsed)
            await asyncio.sleep(sleep_s)

    async def _bitstamp_poll_one(self, session, crypto) -> SpotEvent | None:
        """Poll Bitstamp REST for one crypto. Returns None on any failure."""
        pair = _BITSTAMP_PAIR[crypto]
        url = f"{BITSTAMP_REST_BASE}/ticker/{pair}/"
        try:
            async with session.get(url) as resp:
                if resp.status >= 400:
                    return None
                data = await resp.json()
        except Exception:
            return None
        try:
            bid = float(data.get("bid", 0) or 0)
            ask = float(data.get("ask", 0) or 0)
            if bid > 0 and ask > 0:
                price = (bid + ask) / 2.0
            else:
                price = float(data.get("last", 0) or 0)
        except (TypeError, ValueError, AttributeError):
            return None
        if price <= 0:
            return None
        ts_raw = data.get("timestamp") if isinstance(data, dict) else None
        try:
            ts_ms = int(ts_raw) * 1000 if ts_raw else _utc_now_ms()
        except (TypeError, ValueError):
            ts_ms = _utc_now_ms()
        return SpotEvent(
            crypto=crypto, venue=Venue.BITSTAMP,
            ts_ms=ts_ms, recv_ms=_utc_now_ms(), price=price,
        )

    # -- bitstamp WS path (Phase 10 - replaces REST polling) -----------
    async def _bitstamp_ws_primary(self) -> AsyncIterator[SpotEvent]:
        """Bitstamp public WS, ``live_trades_<pair>`` channels per crypto.

        One WS connection covers all 5 cryptos via per-pair subscribes. Each
        trade message yields a ``SpotEvent`` with the last-trade price. No
        polling, no rate limits, no per-pair stagger. The risk envelope's
        10 s stale-spot threshold becomes effectively impossible to trip
        under normal Bitstamp uptime - in liquid pairs trades arrive several
        per second.

        Auto-reconnects with exponential backoff (1 s -> 30 s cap) on any
        connection failure. Subscription is re-sent on every (re)connect.
        """
        attempt = 0
        while True:
            try:
                async with websockets.connect(
                    BITSTAMP_WS_URL,
                    ping_interval=20,
                    ping_timeout=20,
                ) as ws:
                    attempt = 0
                    await self._bitstamp_ws_subscribe(ws)
                    async for raw in ws:
                        ev = self._bitstamp_ws_parse(raw)
                        if ev is not None:
                            yield ev
            except asyncio.CancelledError:
                raise
            except Exception:
                attempt += 1
                delay = min(_BACKOFF_BASE_S * (2 ** attempt), _BACKOFF_CAP_S)
                await asyncio.sleep(delay)

    async def _bitstamp_ws_subscribe(self, ws) -> None:
        """Send one ``bts:subscribe`` per crypto channel.

        Bitstamp does not support multi-channel subscribe in a single
        message; one event per channel is the documented protocol.
        """
        for crypto in self.cryptos:
            pair = _BITSTAMP_PAIR[crypto]
            await ws.send(json.dumps({
                "event": "bts:subscribe",
                "data": {"channel": f"live_trades_{pair}"},
            }))

    def _bitstamp_ws_parse(self, raw) -> SpotEvent | None:
        """Parse a Bitstamp WS frame into a ``SpotEvent`` or ``None``.

        Trade messages look like::

            {"event": "trade",
             "channel": "live_trades_btcusd",
             "data": {"price": 75000.0, "microtimestamp": "1700000000000000", ...}}

        Subscription acks and non-trade events are silently ignored.
        """
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if msg.get("event") != "trade":
            return None
        channel = msg.get("channel", "")
        if not channel.startswith("live_trades_"):
            return None
        pair = channel[len("live_trades_"):]
        crypto = next(
            (c for c, p in _BITSTAMP_PAIR.items() if p == pair and c in self.cryptos),
            None,
        )
        if crypto is None:
            return None
        data = msg.get("data") or {}
        try:
            price = float(data.get("price", 0) or 0)
        except (TypeError, ValueError):
            return None
        if price <= 0:
            return None
        # microtimestamp is microseconds since epoch as a string; fall back
        # to timestamp (seconds) or wall clock.
        ts_ms = _utc_now_ms()
        micro = data.get("microtimestamp")
        if micro:
            try:
                ts_ms = int(micro) // 1000
            except (TypeError, ValueError):
                pass
        else:
            sec = data.get("timestamp")
            if sec:
                try:
                    ts_ms = int(sec) * 1000
                except (TypeError, ValueError):
                    pass
        return SpotEvent(
            crypto=crypto, venue=Venue.BITSTAMP,
            ts_ms=ts_ms, recv_ms=_utc_now_ms(), price=price,
        )

    # -- coinbase paths (alternate primary; known streaming defect) -----
    async def _coinbase_primary(self, session) -> AsyncIterator[SpotEvent]:
        """Coinbase WS primary with Bitstamp REST fallback.

        KNOWN DEFECT: WS connects + subscribes successfully but ticker
        messages do not stream into the engine. Phase-4 deferred item; use
        ``spot_source='bitstamp'`` (default) until it is diagnosed and fixed.
        """
        failure_count = 0
        while True:
            had_event = False
            try:
                async for ev in self._coinbase_ws_session():
                    had_event = True
                    yield ev
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            if had_event:
                failure_count = 0
            else:
                failure_count += 1
            if failure_count >= self.max_coinbase_failures:
                deadline = time.time() + self.coinbase_retry_after_s
                async for ev in self._bitstamp_poll_until(session, deadline):
                    yield ev
                failure_count = 0
            else:
                delay = min(_BACKOFF_BASE_S * (2 ** failure_count), _BACKOFF_CAP_S)
                await asyncio.sleep(delay)

    async def _coinbase_ws_session(self) -> AsyncIterator[SpotEvent]:
        products = [_COINBASE_PRODUCT[c] for c in self.cryptos]
        async with websockets.connect(
            COINBASE_WS_URL,
            ping_interval=20,
            ping_timeout=20,
        ) as ws:
            await ws.send(json.dumps({
                "type": "subscribe",
                "product_ids": products,
                "channels": ["ticker"],
            }))
            async for raw in ws:
                try:
                    m = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if m.get("type") != "ticker":
                    continue
                product = m.get("product_id", "")
                crypto = self._product_to_crypto.get(product)
                if crypto is None:
                    continue
                bb = m.get("best_bid")
                ba = m.get("best_ask")
                try:
                    if bb and ba:
                        price = (float(bb) + float(ba)) / 2.0
                    else:
                        price = float(m.get("price", 0) or 0)
                except (TypeError, ValueError):
                    continue
                if price <= 0:
                    continue
                yield SpotEvent(
                    crypto=crypto,
                    venue=Venue.COINBASE,
                    ts_ms=_parse_iso_ms(m.get("time")),
                    recv_ms=_utc_now_ms(),
                    price=price,
                )
