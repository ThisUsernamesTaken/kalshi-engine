"""Phase 14.16: daily-window state-machine transitions."""

from __future__ import annotations

import pytest

from kalshi_engine.strategies.commodity_daily.window import (
    DailyWindow,
    DailyWindowState,
    active_observe_mark,
)

_CLOSE_MS = 2_000_000_000_000  # arbitrary fixed close


def _now_for_mtc(minutes_to_close: float) -> int:
    """now_ms that yields the given minutes-to-close against _CLOSE_MS."""
    return int(_CLOSE_MS - minutes_to_close * 60_000)


def _state(mtc: float, w: DailyWindow | None = None) -> DailyWindowState:
    w = w or DailyWindow(open_minutes=60, close_minutes=10)
    return w.state(_now_for_mtc(mtc), _CLOSE_MS)


# ---- state transitions ----------------------------------------------------

def test_waiting_before_window():
    assert _state(70) is DailyWindowState.WAITING
    assert _state(120) is DailyWindowState.WAITING


def test_active_at_open_boundary():
    # Window opens AT open_minutes (inclusive).
    assert _state(60) is DailyWindowState.ACTIVE


def test_active_inside_window():
    assert _state(45) is DailyWindowState.ACTIVE
    assert _state(11) is DailyWindowState.ACTIVE


def test_post_settle_at_close_boundary():
    # Window shuts AT close_minutes (we never enter the final close_minutes).
    assert _state(10) is DailyWindowState.POST_SETTLE
    assert _state(5) is DailyWindowState.POST_SETTLE


def test_post_settle_past_close():
    assert _state(-3) is DailyWindowState.POST_SETTLE


def test_in_window_matches_active():
    w = DailyWindow(60, 10)
    assert w.in_window(_now_for_mtc(30), _CLOSE_MS) is True
    assert w.in_window(_now_for_mtc(70), _CLOSE_MS) is False
    assert w.in_window(_now_for_mtc(5), _CLOSE_MS) is False


def test_custom_window_bounds():
    w = DailyWindow(open_minutes=30, close_minutes=5)
    assert _state(40, w) is DailyWindowState.WAITING
    assert _state(20, w) is DailyWindowState.ACTIVE
    assert _state(3, w) is DailyWindowState.POST_SETTLE


# ---- validation -----------------------------------------------------------

def test_invalid_open_le_close():
    with pytest.raises(ValueError):
        DailyWindow(open_minutes=10, close_minutes=10)
    with pytest.raises(ValueError):
        DailyWindow(open_minutes=5, close_minutes=10)


def test_invalid_negative_close():
    with pytest.raises(ValueError):
        DailyWindow(open_minutes=60, close_minutes=-1)


# ---- observe marks --------------------------------------------------------

_MARKS = (60, 45, 30, 20, 15)


def _mark(mtc: float):
    return active_observe_mark(_now_for_mtc(mtc), _CLOSE_MS, _MARKS,
                               tolerance_s=30.0)


def test_mark_fires_at_exact():
    assert _mark(60.0) == 60
    assert _mark(45.0) == 45
    assert _mark(30.0) == 30


def test_mark_fires_just_after_crossing():
    # 30s tolerance = 0.5 min; mark fires while mtc in (M-0.5, M].
    assert _mark(29.8) == 30
    assert _mark(44.7) == 45


def test_mark_silent_before_crossing():
    # mtc still above the mark -> not yet.
    assert _mark(30.3) is None
    assert _mark(61.0) is None


def test_mark_silent_between_marks():
    assert _mark(37.0) is None
    assert _mark(25.0) is None


def test_mark_none_outside_all():
    assert _mark(200.0) is None
    assert _mark(5.0) is None
