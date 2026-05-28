"""Phase 14.16: Pyth Hermes response parsing (fail-closed)."""

from __future__ import annotations

from kalshi_engine.feeds.pyth_spot import (
    PythPrice,
    parse_benchmarks_history,
    parse_latest_price,
)

_FEED = "765d2ba906dbc32ca17cc11f5310a89e9ee1f6420508c63861f2f8ba4ee34bb2"


def _payload(price="4499560", conf="520", expo=-3, publish_time=1_000_000,
             feed=_FEED):
    return {"parsed": [{"id": feed, "price": {
        "price": price, "conf": conf, "expo": expo,
        "publish_time": publish_time}}]}


def test_parse_valid_price():
    pub = 1_000_000
    now_ms = (pub + 5) * 1000  # 5s old
    px = parse_latest_price(_payload(publish_time=pub), _FEED, now_ms,
                            max_stale_s=60)
    assert isinstance(px, PythPrice)
    assert abs(px.price - 4499.560) < 1e-6
    assert abs(px.conf - 0.520) < 1e-6
    assert abs(px.conf_bps - (0.520 / 4499.560 * 1e4)) < 1e-6
    assert px.publish_time_ms == pub * 1000


def test_parse_matches_0x_prefixed_id():
    pub = 1_000_000
    now_ms = (pub + 1) * 1000
    px = parse_latest_price(_payload(publish_time=pub), "0x" + _FEED, now_ms)
    assert px is not None
    assert px.feed_id == _FEED


def test_stale_price_fails_closed():
    pub = 1_000_000
    now_ms = (pub + 120) * 1000  # 120s old > 60s ceiling
    assert parse_latest_price(_payload(publish_time=pub), _FEED, now_ms,
                              max_stale_s=60) is None


def test_zero_price_fails_closed():
    pub = 1_000_000
    now_ms = (pub + 1) * 1000
    assert parse_latest_price(_payload(price="0", publish_time=pub),
                              _FEED, now_ms) is None


def test_publish_time_zero_fails_closed():
    """A never-published feed (the dead BRENTQ6 case) -> None."""
    assert parse_latest_price(_payload(price="0", publish_time=0),
                              _FEED, 1_000_000_000) is None


def test_missing_feed_in_payload():
    now_ms = 1_000_005_000
    assert parse_latest_price(_payload(feed="dead" * 16), _FEED, now_ms) is None


def test_empty_payload():
    assert parse_latest_price({}, _FEED, 1_000_000_000) is None
    assert parse_latest_price({"parsed": []}, _FEED, 1_000_000_000) is None


def test_malformed_price_fields():
    bad = {"parsed": [{"id": _FEED, "price": {
        "price": "notanum", "conf": "1", "expo": -3, "publish_time": 1_000_000}}]}
    assert parse_latest_price(bad, _FEED, 1_000_005_000) is None


def test_benchmarks_history_ok():
    data = {"s": "ok", "t": [1000, 1060, 1120], "c": [4478.7, 4480.1, 4482.3]}
    hist = parse_benchmarks_history(data)
    assert hist == [(1_000_000, 4478.7), (1_060_000, 4480.1), (1_120_000, 4482.3)]


def test_benchmarks_history_not_ok():
    assert parse_benchmarks_history({"s": "no_data"}) == []
    assert parse_benchmarks_history({}) == []
    assert parse_benchmarks_history({"s": "ok", "t": [], "c": []}) == []
