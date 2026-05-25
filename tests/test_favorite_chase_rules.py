"""favorite_chase.rules: trigger window, favorite selection, strike distance."""

from __future__ import annotations

from kalshi_engine.core.events import BookEvent
from kalshi_engine.core.types import Side
from kalshi_engine.strategies.favorite_chase.rules import (
    compute_strike_distance_bps,
    is_trigger_window,
    select_favorite,
)

_MIN = 60_000


def _book(yes_bid: int, no_bid: int) -> BookEvent:
    return BookEvent(
        ticker="KXBTC15M-T", ts_ms=0, recv_ms=0,
        yes_bid=yes_bid, yes_ask=yes_bid + 10,
        no_bid=no_bid, no_ask=no_bid + 10,
        yes_levels=(), no_levels=(),
    )


def test_trigger_window_boundaries():
    start = 1_000_000
    assert not is_trigger_window(start + 7 * _MIN, start)   # before T+8m
    assert is_trigger_window(start + 8 * _MIN, start)       # opens exactly at T+8m
    assert is_trigger_window(start + 14 * _MIN, start)      # mid-window
    assert not is_trigger_window(start + 15 * _MIN, start)  # closed at T+15m
    assert not is_trigger_window(start - _MIN, start)       # before the cycle


def test_select_favorite_yes_side():
    assert select_favorite(_book(yes_bid=800, no_bid=180)) is Side.YES


def test_select_favorite_no_side():
    assert select_favorite(_book(yes_bid=180, no_bid=800)) is Side.NO


def test_select_favorite_none_below_threshold():
    assert select_favorite(_book(yes_bid=740, no_bid=240)) is None
    assert select_favorite(_book(yes_bid=749, no_bid=241)) is None


def test_select_favorite_exactly_at_threshold():
    # 750 deci-cents (75.0c) is the favorite threshold - inclusive
    assert select_favorite(_book(yes_bid=750, no_bid=240)) is Side.YES


def test_strike_distance_sign():
    assert compute_strike_distance_bps(101_000.0, 100_000.0) > 0   # spot above strike
    assert compute_strike_distance_bps(99_000.0, 100_000.0) < 0    # spot below strike
    assert compute_strike_distance_bps(100_000.0, 100_000.0) == 0.0
