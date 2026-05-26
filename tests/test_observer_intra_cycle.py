"""Phase 14.4 — intra-cycle observer coverage + discovery loop.

Verifies:
- New default --observe-times schedules (10 windows for crypto observer,
  8 for equity observer) match the Phase 14.4 spec.
- HourglassObserverStrategy correctly fires envelopes at the new early
  windows (T+5, T+10, T+15, T+20, T+25).
- observe_inxu's _InxuObserverState window detection handles the
  expanded schedule.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from kalshi_engine.core.events import BookEvent, SpotEvent
from kalshi_engine.core.types import Crypto, Venue
from kalshi_engine.strategies.hourglass_observer import HourglassObserverStrategy

from kalshi_engine.bin.observe_inxu import _InxuObserverState
from kalshi_engine.core.equity import Equity


class _CollectingLog:
    def __init__(self):
        self.events: list[dict] = []
    def write(self, env: dict) -> None:
        self.events.append(env)


def _warmup_spot(obs, base_ms, crypto=Crypto.BTC, price=75000.0):
    for i in range(50):
        obs.on_event(SpotEvent(
            crypto=crypto, venue=Venue.BITSTAMP,
            ts_ms=base_ms + i * 60_000, recv_ms=base_ms + i * 60_000,
            price=price + i * 0.1,
        ))


def _book(ticker, ts_ms, yes_bid=400, yes_ask=420, no_bid=580, no_ask=600):
    return BookEvent(
        ticker=ticker, ts_ms=ts_ms, recv_ms=ts_ms,
        yes_bid=yes_bid, yes_ask=yes_ask, no_bid=no_bid, no_ask=no_ask,
        yes_levels=(), no_levels=(),
    )


# ---- crypto observer: new early-window firing ---------------------------

def test_crypto_observer_fires_at_t5():
    """Phase 14.4: T+5 must produce an envelope when configured."""
    base = 1_700_000_000_000
    log = _CollectingLog()
    obs = HourglassObserverStrategy(
        log_writer=log,
        observe_minutes=(5, 10, 15, 20, 25, 30, 40, 45, 50, 55),
    )
    _warmup_spot(obs, base)
    open_ms = base + 60 * 60_000
    close_ms = open_ms + 60 * 60_000
    obs.register_market("KXBTCD-T", strike=74900.0,
                         open_ms=open_ms, close_ms=close_ms)
    # Book at exactly T+5:00 (first sampling window in the new schedule)
    obs.on_event(_book("KXBTCD-T", open_ms + 5 * 60_000))
    envs = [e for e in log.events if e["kind"] == "book_at_1hr_pretrigger"]
    assert len(envs) == 1
    assert envs[0]["window_label"] == "T+5"


def test_crypto_observer_fires_at_t10_t15_t20_t25():
    """Each early window fires once."""
    base = 1_700_000_000_000
    log = _CollectingLog()
    obs = HourglassObserverStrategy(
        log_writer=log,
        observe_minutes=(5, 10, 15, 20, 25, 30, 40, 45, 50, 55),
    )
    _warmup_spot(obs, base)
    open_ms = base + 60 * 60_000
    close_ms = open_ms + 60 * 60_000
    obs.register_market("KXBTCD-T", strike=74900.0,
                         open_ms=open_ms, close_ms=close_ms)
    for m in (10, 15, 20, 25):
        obs.on_event(_book("KXBTCD-T", open_ms + m * 60_000))
    envs = [e for e in log.events if e["kind"] == "book_at_1hr_pretrigger"]
    labels = {e["window_label"] for e in envs}
    assert labels == {"T+10", "T+15", "T+20", "T+25"}


def test_crypto_observer_full_schedule_one_envelope_per_window():
    """Across all 10 windows the observer fires exactly 10 envelopes."""
    base = 1_700_000_000_000
    log = _CollectingLog()
    schedule = (5, 10, 15, 20, 25, 30, 40, 45, 50, 55)
    obs = HourglassObserverStrategy(
        log_writer=log, observe_minutes=schedule)
    _warmup_spot(obs, base)
    open_ms = base + 60 * 60_000
    close_ms = open_ms + 60 * 60_000
    obs.register_market("KXBTCD-T", strike=74900.0,
                         open_ms=open_ms, close_ms=close_ms)
    for m in schedule:
        obs.on_event(_book("KXBTCD-T", open_ms + m * 60_000))
    envs = [e for e in log.events if e["kind"] == "book_at_1hr_pretrigger"]
    assert len(envs) == len(schedule)
    assert {e["window_label"] for e in envs} == {f"T+{m}" for m in schedule}


def test_crypto_observer_dedup_per_window_across_multiple_books():
    """Two book events both inside the same window only emit one envelope."""
    base = 1_700_000_000_000
    log = _CollectingLog()
    obs = HourglassObserverStrategy(
        log_writer=log, observe_minutes=(5, 10, 30))
    _warmup_spot(obs, base)
    open_ms = base + 60 * 60_000
    close_ms = open_ms + 60 * 60_000
    obs.register_market("KXBTCD-T", strike=74900.0,
                         open_ms=open_ms, close_ms=close_ms)
    # Two book events inside T+5 window
    obs.on_event(_book("KXBTCD-T", open_ms + 5 * 60_000))
    obs.on_event(_book("KXBTCD-T", open_ms + 5 * 60_000 + 10_000))  # +10s
    envs = [e for e in log.events if e["kind"] == "book_at_1hr_pretrigger"]
    assert len(envs) == 1


# ---- equity observer: new schedule + tolerance --------------------------

def test_inxu_state_recognizes_t5_window():
    state = _InxuObserverState(observe_minutes=(5, 10, 15, 20, 25, 30, 40, 50))
    assert state.window_label(5.0) == "T+5"
    assert state.window_label(5.3) == "T+5"
    assert state.window_label(4.7) == "T+5"


def test_inxu_state_recognizes_t25():
    state = _InxuObserverState(observe_minutes=(5, 10, 15, 20, 25, 30, 40, 50))
    assert state.window_label(25.0) == "T+25"


def test_inxu_state_skips_t35_when_not_in_schedule():
    """T+35 is NOT in the Phase 14.4 default — must return None."""
    state = _InxuObserverState(observe_minutes=(5, 10, 15, 20, 25, 30, 40, 50))
    assert state.window_label(35.0) is None


def test_inxu_state_skips_t45_when_not_in_schedule():
    """T+45 is NOT in the Phase 14.4 default — must return None."""
    state = _InxuObserverState(observe_minutes=(5, 10, 15, 20, 25, 30, 40, 50))
    assert state.window_label(45.0) is None


def test_inxu_state_register_handles_cycle_rollover():
    """Phase 14.4 fix: state.register can be called for new cycles after
    boot (discovery loop scenario). Subsequent registrations don't
    clobber prior fired-window state for unrelated tickers."""
    state = _InxuObserverState(observe_minutes=(5, 30))
    state.register("KXINXU-CYC1-T6000", strike=6000.0,
                    open_ms=1_700_000_000_000,
                    close_ms=1_700_000_000_000 + 60*60_000,
                    series="KXINXU", equity=Equity.SPX)
    # Simulate firing the CYC1 window
    state.fired.add(("KXINXU-CYC1-T6000", "T+5"))
    # New cycle rolls in
    state.register("KXINXU-CYC2-T6050", strike=6050.0,
                    open_ms=1_700_000_000_000 + 60*60_000,
                    close_ms=1_700_000_000_000 + 120*60_000,
                    series="KXINXU", equity=Equity.SPX)
    # The first cycle's fired entry is intact; the new cycle's fresh
    assert ("KXINXU-CYC1-T6000", "T+5") in state.fired
    assert ("KXINXU-CYC2-T6050", "T+5") not in state.fired
    assert state.markets["KXINXU-CYC2-T6050"]["strike"] == 6050.0


# ---- parse_args defaults ------------------------------------------------

def test_observe_1hr_default_includes_early_windows():
    """Phase 14.4: the new crypto observer default includes T+5–T+25."""
    from kalshi_engine.bin.observe_1hr import parse_args
    args = parse_args([])
    times = [int(x) for x in args.observe_times.split(",")]
    for t in (5, 10, 15, 20, 25, 30, 40, 45, 50, 55):
        assert t in times, f"T+{t} missing from default observe_times"
    assert len(times) == 10


def test_observe_inxu_default_includes_early_windows():
    """Phase 14.4: equity observer default includes T+5–T+25."""
    from kalshi_engine.bin.observe_inxu import parse_args
    args = parse_args([])
    times = [int(x) for x in args.observe_times.split(",")]
    for t in (5, 10, 15, 20, 25, 30, 40, 50):
        assert t in times
    assert len(times) == 8


def test_observe_inxu_has_discovery_interval_arg():
    """Phase 14.4: the new --discovery-interval-s flag exists with a
    sensible default (300s)."""
    from kalshi_engine.bin.observe_inxu import parse_args
    args = parse_args([])
    assert args.discovery_interval_s == 300.0
