"""Minimal smoke tests for the Phase 14.5 live_inxu_v0 shim.

Verifies: imports succeed, args parse, parse_orderbook handles a known
payload shape. The full live behavior (REST polling, decision loop,
order placement) is exercised by the live dry-run smoke, not unit tests."""

from __future__ import annotations

import pytest

from kalshi_engine.bin.live_inxu_v0 import (
    parse_args, parse_orderbook, _strike_from_market,
)


def test_parse_args_defaults():
    args = parse_args([])
    assert args.align_mode == "5tier_v13b_equity_1ct_flat"
    assert args.max_contracts == 1
    assert args.daily_cap_cents == 500
    assert args.observe_times == "30,40,50"
    assert args.max_favorite_cost_decicents == 920
    assert args.cutpoints_version == "v1"
    assert args.dry_run is False
    assert args.duration_s == 0.0


def test_parse_args_dry_run_flag():
    args = parse_args(["--dry-run", "--duration-s", "60"])
    assert args.dry_run is True
    assert args.duration_s == 60.0


def test_parse_args_observe_times_override():
    args = parse_args(["--observe-times", "30,50"])
    assert args.observe_times == "30,50"


def test_parse_orderbook_handles_one_sided_book():
    """A book with NO-side asks only (typical for extreme strike). The
    parser must produce sensible yes_bid (= 1000 - no_ask) and no_bid
    (= 1000 - yes_ask, defaulting to 0 when yes_ask is missing)."""
    ob = {"orderbook_fp": {
        "no_dollars": [["0.9500", "2000"], ["0.9900", "10003"]],
        "yes_dollars": [],
    }}
    b = parse_orderbook(ob)
    # NO ask at $0.95 -> YES bid = 1000 - 950 = 50
    assert b["yes_bid_dc"] == 50
    # YES side empty -> yes_ask defaults to 1000
    assert b["yes_ask_dc"] == 1000
    # YES ask = 1000 -> NO bid = 1000 - 1000 = 0
    assert b["no_bid_dc"] == 0
    assert b["no_ask_dc"] == 950
    # Smallest no-ask is the best for the no-side buyer
    assert b["no_ask_sz"] == 2000


def test_parse_orderbook_handles_two_sided_book():
    ob = {"orderbook": {
        "yes_dollars": [["0.35", "100"], ["0.36", "200"]],
        "no_dollars": [["0.62", "150"], ["0.65", "300"]],
    }}
    b = parse_orderbook(ob)
    # YES ask = 35c -> 350 dc; NO ask = 62c -> 620 dc
    assert b["yes_ask_dc"] == 350
    assert b["no_ask_dc"] == 620
    # YES bid = 1000 - 620 = 380
    assert b["yes_bid_dc"] == 380
    assert b["no_bid_dc"] == 650


def test_parse_orderbook_empty_payload():
    b = parse_orderbook({})
    assert b["yes_ask_dc"] == 1000
    assert b["no_ask_dc"] == 1000
    assert b["yes_bid_dc"] == 0
    assert b["no_bid_dc"] == 0


def test_strike_from_market_floor_strike():
    assert _strike_from_market({"floor_strike": 8000.5, "ticker": "X"}) == 8000.5


def test_strike_from_market_ticker_fallback():
    m = {"floor_strike": None, "ticker": "KXINXU-26MAY26H1100-T8444.9999"}
    assert _strike_from_market(m) == 8444.9999
