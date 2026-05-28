"""Phase 14.16: live_commodity arg-parsing + pure-helper smoke tests."""

from __future__ import annotations

from kalshi_engine.bin.live_commodity import (
    parse_args,
    parse_minutes_marks,
    parse_orderbook,
    resolve_commodities,
    _strike_from_market,
)
from kalshi_engine.core.commodity import Commodity


def test_parse_args_defaults():
    a = parse_args([])
    assert a.commodities == "GOLD"
    assert a.align_mode == "5tier_v13b_commodity_1ct_flat"
    assert a.max_contracts == 1
    assert a.daily_cap_cents == 500
    assert a.total_daily_cap_cents == 1000
    assert a.window_open_minutes == 60.0
    assert a.window_close_minutes == 10.0
    assert a.observe_times == "60,45,30,20,15"
    assert a.cutpoints_version == "commodity_v1"
    assert a.time_of_day_skip == "disabled"  # hook present, disabled
    assert a.dry_run is False


def test_parse_args_overrides():
    a = parse_args(["--dry-run", "--duration-s", "300",
                    "--daily-cap-cents", "300", "--commodities", "GOLD,BRENT"])
    assert a.dry_run is True
    assert a.duration_s == 300.0
    assert a.daily_cap_cents == 300
    assert a.commodities == "GOLD,BRENT"


def test_parse_minutes_marks():
    assert parse_minutes_marks("60,45,30,20,15") == (60, 45, 30, 20, 15)
    assert parse_minutes_marks("30") == (30,)
    assert parse_minutes_marks("60, 30 ,15") == (60, 30, 15)


def test_resolve_commodities_skips_brent_by_default():
    specs = resolve_commodities("GOLD,BRENT", force=False)
    assert [s.commodity for s in specs] == [Commodity.GOLD]


def test_resolve_commodities_force_includes_brent():
    specs = resolve_commodities("GOLD,BRENT", force=True)
    assert {s.commodity for s in specs} == {Commodity.GOLD, Commodity.BRENT}


def test_resolve_commodities_unknown_skipped():
    specs = resolve_commodities("GOLD,PLATINUM", force=False)
    assert [s.commodity for s in specs] == [Commodity.GOLD]


def test_strike_from_market_floor():
    assert _strike_from_market({"floor_strike": 4479.0, "ticker": "X"}) == 4479.0


def test_strike_from_market_ticker_fallback():
    m = {"floor_strike": None, "ticker": "KXGOLDD-26MAY2817-T4479"}
    assert _strike_from_market(m) == 4479.0


def test_strike_from_market_brent_decimal():
    m = {"floor_strike": None, "ticker": "KXBRENTD-26MAY2817-T92.50"}
    assert _strike_from_market(m) == 92.5


def test_parse_orderbook_two_sided():
    ob = {"orderbook": {
        "yes_dollars": [["0.78", "100"], ["0.80", "200"]],
        "no_dollars": [["0.20", "150"], ["0.25", "300"]],
    }}
    b = parse_orderbook(ob)
    assert b["yes_ask_dc"] == 780
    assert b["no_ask_dc"] == 200
    assert b["yes_bid_dc"] == 800   # 1000 - no_ask(200)
    assert b["no_bid_dc"] == 220    # 1000 - yes_ask(780)


def test_parse_orderbook_empty():
    b = parse_orderbook({})
    assert b["yes_ask_dc"] == 1000
    assert b["no_ask_dc"] == 1000
    assert b["yes_bid_dc"] == 0
    assert b["no_bid_dc"] == 0
