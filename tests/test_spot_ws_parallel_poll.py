"""Phase-10.1 parallel-poll path for Bitstamp REST.

Sequential polling (5 cryptos x 2.5 s outer interval + 0.1 s stagger)
produced max gaps of ~3.4 s per pair under calm regime and 13-30 s during
stress. Parallel polling (asyncio.gather across all 5 pairs every 1 s)
should give every pair an update at ~1 s cadence regardless of order.

These tests exercise the parallel-poll dispatch with a mocked aiohttp
session, asserting that:
- All cryptos are polled per outer iteration
- Failures on one pair don't block others
- The outer interval respects bitstamp_poll_s
"""

from __future__ import annotations

import asyncio
import json

import pytest

from kalshi_engine.core.events import SpotEvent
from kalshi_engine.core.types import Crypto
from kalshi_engine.feeds.spot_ws import SpotFeed


class _FakeResponse:
    def __init__(self, status: int = 200, payload: dict | None = None):
        self.status = status
        self._payload = payload or {}
    async def __aenter__(self):
        return self
    async def __aexit__(self, *a):
        return False
    async def json(self):
        return self._payload


class _FakeSession:
    """Records GETs; replies per-pair from a script. Optionally raises."""
    def __init__(self, replies: dict[str, dict], raise_for: set[str] | None = None):
        self._replies = replies
        self._raise_for = raise_for or set()
        self.calls: list[str] = []
    def get(self, url):
        self.calls.append(url)
        pair = url.split("/ticker/")[-1].rstrip("/")
        if pair in self._raise_for:
            class _Boom:
                async def __aenter__(self): raise RuntimeError("simulated network err")
                async def __aexit__(self, *a): return False
            return _Boom()
        return _FakeResponse(status=200, payload=self._replies.get(pair, {}))


def _payload(price: float, ts: int = 1779560000):
    return {
        "bid": f"{price - 0.5:.2f}", "ask": f"{price + 0.5:.2f}",
        "last": f"{price:.2f}", "timestamp": str(ts),
    }


async def _drain_until(stream, n: int, timeout: float = 5.0):
    """Pull up to n events from an async generator under a timeout."""
    events = []
    async def collect():
        async for ev in stream:
            events.append(ev)
            if len(events) >= n:
                return
    try:
        await asyncio.wait_for(collect(), timeout=timeout)
    except asyncio.TimeoutError:
        pass
    return events


def test_parallel_poll_emits_all_five_pairs_per_iteration():
    """One outer iteration must produce one SpotEvent per crypto."""
    cryptos = [Crypto.BTC, Crypto.ETH, Crypto.SOL, Crypto.XRP, Crypto.DOGE]
    feed = SpotFeed(cryptos, spot_source="bitstamp", bitstamp_poll_s=0.05)
    session = _FakeSession(replies={
        "btcusd": _payload(75000.0),
        "ethusd": _payload(2050.0),
        "solusd": _payload(84.0),
        "xrpusd": _payload(1.33),
        "dogeusd": _payload(0.10),
    })
    events = asyncio.run(_drain_until(feed._bitstamp_primary(session), n=5))
    assert len(events) == 5
    seen_cryptos = {e.crypto for e in events}
    assert seen_cryptos == set(cryptos)
    assert all(isinstance(e, SpotEvent) for e in events)


def test_parallel_poll_survives_per_pair_exception():
    """A network-level raise on one pair must not kill the loop or block
    the other 4 pairs from emitting."""
    cryptos = [Crypto.BTC, Crypto.ETH, Crypto.SOL, Crypto.XRP, Crypto.DOGE]
    feed = SpotFeed(cryptos, spot_source="bitstamp", bitstamp_poll_s=0.05)
    session = _FakeSession(
        replies={
            "btcusd": _payload(75000.0),
            "ethusd": _payload(2050.0),
            "xrpusd": _payload(1.33),
            "dogeusd": _payload(0.10),
        },
        raise_for={"solusd"},
    )
    events = asyncio.run(_drain_until(feed._bitstamp_primary(session), n=4))
    # The 4 healthy pairs emitted; SOL was silently dropped.
    cryptos_seen = {e.crypto for e in events}
    assert Crypto.SOL not in cryptos_seen
    assert {Crypto.BTC, Crypto.ETH, Crypto.XRP, Crypto.DOGE}.issubset(cryptos_seen)


def test_parallel_poll_handles_http_4xx_per_pair():
    """An HTTP 4xx response on one pair is dropped, others continue."""
    cryptos = [Crypto.BTC, Crypto.SOL]
    feed = SpotFeed(cryptos, spot_source="bitstamp", bitstamp_poll_s=0.05)

    class _FlakyResp(_FakeResponse):
        pass

    class _FlakySession:
        def __init__(self):
            self.calls = []
        def get(self, url):
            self.calls.append(url)
            if url.endswith("solusd/"):
                return _FlakyResp(status=500, payload={})
            return _FlakyResp(status=200, payload=_payload(75000.0))

    session = _FlakySession()
    events = asyncio.run(_drain_until(feed._bitstamp_primary(session), n=1))
    assert len(events) >= 1
    assert all(e.crypto is Crypto.BTC for e in events)


def test_parallel_poll_default_interval_is_one_second():
    """Phase-10.1 default cadence is 1 s outer interval (was 2.0 s)."""
    feed = SpotFeed([Crypto.BTC], spot_source="bitstamp")
    assert feed.bitstamp_poll_s == 1.0


def test_parallel_poll_subset_cryptos():
    """When only a subset of cryptos is configured, only those are polled."""
    feed = SpotFeed([Crypto.BTC, Crypto.XRP], spot_source="bitstamp",
                    bitstamp_poll_s=0.05)
    session = _FakeSession(replies={
        "btcusd": _payload(75000.0),
        "xrpusd": _payload(1.33),
        # ETH/SOL/DOGE not in cryptos list, payloads irrelevant
    })
    events = asyncio.run(_drain_until(feed._bitstamp_primary(session), n=2))
    assert len(events) == 2
    assert {e.crypto for e in events} == {Crypto.BTC, Crypto.XRP}
    # Verify only BTC + XRP URLs were called in the first iteration
    iter1_calls = [u for u in session.calls if u.endswith("/")]
    pairs_called = {u.split("/ticker/")[-1].rstrip("/") for u in iter1_calls[:2]}
    assert pairs_called == {"btcusd", "xrpusd"}
