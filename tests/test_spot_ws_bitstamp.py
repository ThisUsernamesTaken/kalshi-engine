"""Bitstamp WS spot-feed parse + subscribe-protocol tests.

Phase 10 addition: ``--spot-source bitstamp-ws`` replaces REST polling with
the public ``wss://ws.bitstamp.net`` live_trades stream. These tests cover
the parse path (the dispatch logic that converts a raw WS frame into a
``SpotEvent``) and the subscribe protocol (one ``bts:subscribe`` per pair).
The connect/auto-reconnect outer loop is covered indirectly by the existing
spot_ws coinbase reconnect tests.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from kalshi_engine.core.events import SpotEvent
from kalshi_engine.core.types import Crypto, Venue
from kalshi_engine.feeds.spot_ws import SpotFeed


def _feed(cryptos=(Crypto.BTC, Crypto.ETH, Crypto.SOL, Crypto.XRP, Crypto.DOGE)):
    return SpotFeed(list(cryptos), spot_source="bitstamp-ws")


# ---- subscribe protocol ----

class _RecordingWs:
    """Captures every ``ws.send`` payload as a parsed dict."""
    def __init__(self):
        self.sent: list[dict] = []
    async def send(self, raw):
        self.sent.append(json.loads(raw))


def test_subscribe_sends_one_event_per_crypto_pair():
    feed = _feed()
    ws = _RecordingWs()
    asyncio.run(feed._bitstamp_ws_subscribe(ws))
    assert len(ws.sent) == 5
    channels = {m["data"]["channel"] for m in ws.sent}
    assert channels == {
        "live_trades_btcusd", "live_trades_ethusd", "live_trades_solusd",
        "live_trades_xrpusd", "live_trades_dogeusd",
    }
    assert all(m["event"] == "bts:subscribe" for m in ws.sent)


def test_subscribe_only_for_configured_cryptos():
    feed = _feed(cryptos=(Crypto.BTC, Crypto.XRP))
    ws = _RecordingWs()
    asyncio.run(feed._bitstamp_ws_subscribe(ws))
    channels = {m["data"]["channel"] for m in ws.sent}
    assert channels == {"live_trades_btcusd", "live_trades_xrpusd"}


# ---- parse path ----

def _trade(pair="btcusd", price="75000.50", micro="1779525000123456"):
    return json.dumps({
        "event": "trade",
        "channel": f"live_trades_{pair}",
        "data": {
            "id": 12345, "amount": "0.1", "amount_str": "0.10000000",
            "price": price, "price_str": price,
            "type": 0, "timestamp": "1779525000",
            "microtimestamp": micro,
            "buy_order_id": 1, "sell_order_id": 2,
        },
    })


def test_parse_trade_yields_spot_event():
    feed = _feed()
    ev = feed._bitstamp_ws_parse(_trade(pair="btcusd", price="75000.50"))
    assert isinstance(ev, SpotEvent)
    assert ev.crypto is Crypto.BTC
    assert ev.venue is Venue.BITSTAMP
    assert ev.price == 75000.50
    # microtimestamp 1779525000123456 us -> 1779525000123 ms
    assert ev.ts_ms == 1779525000123


def test_parse_routes_each_pair_to_correct_crypto():
    feed = _feed()
    for pair, crypto in [
        ("btcusd", Crypto.BTC),
        ("ethusd", Crypto.ETH),
        ("solusd", Crypto.SOL),
        ("xrpusd", Crypto.XRP),
        ("dogeusd", Crypto.DOGE),
    ]:
        ev = feed._bitstamp_ws_parse(_trade(pair=pair, price="100.0"))
        assert ev is not None
        assert ev.crypto is crypto


def test_parse_ignores_subscription_ack():
    feed = _feed()
    ack = json.dumps({
        "event": "bts:subscription_succeeded",
        "channel": "live_trades_btcusd", "data": {},
    })
    assert feed._bitstamp_ws_parse(ack) is None


def test_parse_ignores_unknown_event():
    feed = _feed()
    other = json.dumps({"event": "order_created", "data": {}})
    assert feed._bitstamp_ws_parse(other) is None


def test_parse_ignores_unconfigured_crypto():
    """If we subscribed only to BTC, an ETH trade frame doesn't yield."""
    feed = _feed(cryptos=(Crypto.BTC,))
    eth = _trade(pair="ethusd", price="2000.0")
    assert feed._bitstamp_ws_parse(eth) is None


def test_parse_invalid_json_returns_none():
    feed = _feed()
    assert feed._bitstamp_ws_parse("{not json") is None


def test_parse_zero_or_missing_price_returns_none():
    feed = _feed()
    assert feed._bitstamp_ws_parse(_trade(price="0")) is None
    bad = json.dumps({
        "event": "trade", "channel": "live_trades_btcusd",
        "data": {"price": "nope"},
    })
    assert feed._bitstamp_ws_parse(bad) is None


def test_parse_falls_back_to_timestamp_when_microtimestamp_missing():
    feed = _feed()
    frame = json.dumps({
        "event": "trade", "channel": "live_trades_btcusd",
        "data": {"price": "70000", "timestamp": "1779525000"},
    })
    ev = feed._bitstamp_ws_parse(frame)
    assert ev.ts_ms == 1779525000 * 1000


def test_parse_falls_back_to_now_when_no_timestamps():
    """Defensive: bare trade w/o any timestamp uses wall clock, not crash."""
    feed = _feed()
    frame = json.dumps({
        "event": "trade", "channel": "live_trades_btcusd",
        "data": {"price": "70000"},
    })
    ev = feed._bitstamp_ws_parse(frame)
    assert isinstance(ev, SpotEvent)
    assert ev.ts_ms > 0


def test_spot_source_validation_rejects_unknown():
    with pytest.raises(ValueError, match="spot_source"):
        SpotFeed([Crypto.BTC], spot_source="binance")


def test_spot_source_accepts_bitstamp_ws():
    feed = SpotFeed([Crypto.BTC], spot_source="bitstamp-ws")
    assert feed.spot_source == "bitstamp-ws"
