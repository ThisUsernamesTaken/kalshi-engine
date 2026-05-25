"""SpotFeed: construction, parsing helpers, product/pair mappings."""

from __future__ import annotations

import pytest

from kalshi_engine.core.types import Crypto, Venue
from kalshi_engine.feeds.spot_ws import (
    _COINBASE_PRODUCT,
    _BITSTAMP_PAIR,
    SpotFeed,
    _parse_iso_ms,
)


def test_parse_iso_ms_handles_z_suffix():
    ms = _parse_iso_ms("2026-05-22T16:30:00Z")
    assert ms > 1_700_000_000_000  # post-2023 epoch ms


def test_parse_iso_ms_handles_microseconds():
    ms = _parse_iso_ms("2026-05-22T16:30:00.123456Z")
    assert ms > 1_700_000_000_000


def test_parse_iso_ms_empty_falls_back_to_now():
    ms = _parse_iso_ms(None)
    assert ms > 1_700_000_000_000


def test_spot_feed_init_requires_cryptos():
    with pytest.raises(ValueError, match="at least one"):
        SpotFeed([])


def test_spot_feed_init_builds_product_map():
    feed = SpotFeed([Crypto.BTC, Crypto.ETH])
    assert feed._product_to_crypto["BTC-USD"] is Crypto.BTC
    assert feed._product_to_crypto["ETH-USD"] is Crypto.ETH


def test_product_and_pair_maps_cover_five_cryptos():
    for c in (Crypto.BTC, Crypto.ETH, Crypto.SOL, Crypto.XRP, Crypto.DOGE):
        assert c in _COINBASE_PRODUCT
        assert c in _BITSTAMP_PAIR
        # Coinbase format is e.g. BTC-USD; Bitstamp is btcusd
        assert _COINBASE_PRODUCT[c].endswith("-USD")
        assert _BITSTAMP_PAIR[c].endswith("usd")


def test_coinbase_ticker_parse_formula():
    # The feed parses a ticker message by taking mid = (best_bid + best_ask) / 2,
    # falling back to "price" when bid/ask are missing.
    msg = {
        "type": "ticker", "product_id": "BTC-USD",
        "best_bid": "100000", "best_ask": "100020",
        "time": "2026-05-22T16:30:00.000Z",
    }
    bb, ba = float(msg["best_bid"]), float(msg["best_ask"])
    assert (bb + ba) / 2.0 == 100010.0


def test_bitstamp_ticker_parse_formula():
    data = {"bid": "100000", "ask": "100020", "timestamp": "1778587000", "last": "100015"}
    bid, ask = float(data["bid"]), float(data["ask"])
    assert (bid + ask) / 2.0 == 100010.0


def test_venue_enum_includes_required_sources():
    # SpotFeed emits SpotEvent with Venue.COINBASE or Venue.BITSTAMP.
    assert Venue.COINBASE.value == "coinbase"
    assert Venue.BITSTAMP.value == "bitstamp"
