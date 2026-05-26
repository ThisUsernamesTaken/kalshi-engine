"""Tests for BitstampDepthPoller (Phase 14.2a liquidity instrumentation)."""

from __future__ import annotations

import asyncio

import pytest

from kalshi_engine.feeds.bitstamp_depth import BitstampDepthPoller


def test_compute_depth_basic():
    """Hand-crafted orderbook with known bids/asks."""
    book = {
        "bids": [["99.50", "100"], ["99.00", "200"], ["98.00", "500"]],
        "asks": [["100.50", "150"], ["101.00", "250"], ["102.00", "400"]],
    }
    d = BitstampDepthPoller._compute_depth(book)
    assert d is not None
    assert d["mid"] == 100.0  # (99.50 + 100.50)/2
    assert d["spread"] == 1.0
    assert d["spread_bps"] == pytest.approx(100.0, abs=0.1)  # 1/100*10000
    # ±0.5% of 100 = [99.5, 100.5] → only top bid + top ask
    assert d["bid_depth_0p5pct"] == 100.0
    assert d["ask_depth_0p5pct"] == 150.0
    # ±1.0% of 100 = [99, 101] → top 2 bids + top 2 asks
    assert d["bid_depth_1pct"] == 300.0
    assert d["ask_depth_1pct"] == 400.0


def test_compute_depth_empty_returns_none():
    assert BitstampDepthPoller._compute_depth({"bids": [], "asks": []}) is None
    assert BitstampDepthPoller._compute_depth({}) is None


def test_compute_depth_malformed_returns_none():
    assert BitstampDepthPoller._compute_depth({"bids": [["not-a-number"]], "asks": []}) is None


def test_get_depth_returns_none_before_first_refresh():
    p = BitstampDepthPoller()
    assert p.get_depth("BTC") is None


def test_get_depth_returns_cached_after_refresh():
    p = BitstampDepthPoller()
    # Directly seed the cache to simulate a successful refresh
    import time as _time
    p._cache["BTC"] = (_time.time(), {"mid": 100.0, "bid_depth_0p5pct": 50.0,
                                        "spread_bps": 5.0})
    d = p.get_depth("BTC")
    assert d is not None
    assert d["mid"] == 100.0


def test_get_depth_unknown_crypto_returns_none():
    p = BitstampDepthPoller()
    assert p.get_depth("UNKNOWN") is None


def test_constructor_defaults():
    p = BitstampDepthPoller()
    assert p._ttl == 30.0
    assert p._session is None
    assert p._cache == {}


def test_refresh_requires_context_manager():
    p = BitstampDepthPoller()
    with pytest.raises(RuntimeError, match="context manager"):
        asyncio.run(p.refresh("BTC"))


def test_refresh_unknown_crypto_returns_none():
    """Unknown crypto symbol → returns None without hitting network."""
    async def run():
        async with BitstampDepthPoller() as p:
            return await p.refresh("FOOBAR")
    assert asyncio.run(run()) is None
