"""Phase 13.0 — Hourglass 1hr observer tests."""

from __future__ import annotations

from kalshi_engine.core.events import BookEvent, SpotEvent
from kalshi_engine.core.types import Crypto, Venue
from kalshi_engine.strategies.hourglass_observer import HourglassObserverStrategy


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


def test_observer_constructs():
    log = _CollectingLog()
    obs = HourglassObserverStrategy(log_writer=log)
    assert obs.observe_minutes == (30, 40, 45, 50, 55)


def test_observer_requires_log_writer():
    import pytest
    with pytest.raises(ValueError, match="log_writer"):
        HourglassObserverStrategy(log_writer=None)


def test_envelope_fires_at_T30():
    base = 1_000_000_000_000
    log = _CollectingLog()
    obs = HourglassObserverStrategy(log_writer=log)
    _warmup_spot(obs, base)
    open_ms = base + 60 * 60_000
    close_ms = open_ms + 60 * 60_000  # 1hr cycle
    ticker = "KXBTCD-T"
    obs.register_market(ticker, strike=74900.0, open_ms=open_ms, close_ms=close_ms)
    # Book event at exactly T+30:00 (start of first sampling window)
    obs.on_event(_book(ticker, open_ms + 30 * 60_000))
    envs = [e for e in log.events if e["kind"] == "book_at_1hr_pretrigger"]
    assert len(envs) == 1
    e = envs[0]
    assert e["ticker"] == ticker
    assert e["window_label"] == "T+30"
    for k in ("yes_bid", "yes_ask", "no_bid", "no_ask",
              "spot", "vol_30m", "bb_div", "bps_margin",
              "favorite_side", "favorite_mid_decicents",
              "cycle_open_ms", "cycle_close_ms", "elapsed_min", "tau_min"):
        assert k in e, f"missing key {k}"


def test_envelopes_fire_at_each_window():
    """All 5 default sampling windows should fire."""
    base = 1_000_000_000_000
    log = _CollectingLog()
    obs = HourglassObserverStrategy(log_writer=log)
    _warmup_spot(obs, base)
    open_ms = base + 60 * 60_000
    close_ms = open_ms + 60 * 60_000
    ticker = "KXBTCD-T"
    obs.register_market(ticker, strike=74900.0, open_ms=open_ms, close_ms=close_ms)
    # One book per window
    for m in (30, 40, 45, 50, 55):
        obs.on_event(_book(ticker, open_ms + m * 60_000 + 1000))  # +1s into window
    envs = [e for e in log.events if e["kind"] == "book_at_1hr_pretrigger"]
    assert len(envs) == 5
    labels = sorted(e["window_label"] for e in envs)
    assert labels == ["T+30", "T+40", "T+45", "T+50", "T+55"]


def test_envelope_not_fired_outside_windows():
    """Books at T+5, T+20, T+35, T+59 (outside all windows) → no envelopes."""
    base = 1_000_000_000_000
    log = _CollectingLog()
    obs = HourglassObserverStrategy(log_writer=log)
    _warmup_spot(obs, base)
    open_ms = base + 60 * 60_000
    close_ms = open_ms + 60 * 60_000
    ticker = "KXBTCD-T"
    obs.register_market(ticker, strike=74900.0, open_ms=open_ms, close_ms=close_ms)
    for m in (5, 20, 35, 59):
        obs.on_event(_book(ticker, open_ms + m * 60_000 + 1000))
    envs = [e for e in log.events if e["kind"] == "book_at_1hr_pretrigger"]
    assert len(envs) == 0


def test_throttle_one_per_window_per_ticker():
    """3 book events at T+30:05, T+30:15, T+30:25 → only first emits."""
    base = 1_000_000_000_000
    log = _CollectingLog()
    obs = HourglassObserverStrategy(log_writer=log)
    _warmup_spot(obs, base)
    open_ms = base + 60 * 60_000
    close_ms = open_ms + 60 * 60_000
    ticker = "KXBTCD-T"
    obs.register_market(ticker, strike=74900.0, open_ms=open_ms, close_ms=close_ms)
    obs.on_event(_book(ticker, open_ms + 30 * 60_000 + 5_000))
    obs.on_event(_book(ticker, open_ms + 30 * 60_000 + 15_000))
    obs.on_event(_book(ticker, open_ms + 30 * 60_000 + 25_000))
    envs = [e for e in log.events if e["kind"] == "book_at_1hr_pretrigger"]
    assert len(envs) == 1


def test_observer_never_returns_decisions():
    """on_event always returns None — observer NEVER places orders."""
    base = 1_000_000_000_000
    log = _CollectingLog()
    obs = HourglassObserverStrategy(log_writer=log)
    _warmup_spot(obs, base)
    open_ms = base + 60 * 60_000
    close_ms = open_ms + 60 * 60_000
    ticker = "KXBTCD-T"
    obs.register_market(ticker, strike=74900.0, open_ms=open_ms, close_ms=close_ms)
    # Try book inside and outside window
    for offset in (5, 30, 50):
        result = obs.on_event(_book(ticker, open_ms + offset * 60_000 + 1000))
        assert result is None, f"observer returned non-None at T+{offset}"
    # Spot events
    assert obs.on_event(SpotEvent(
        crypto=Crypto.BTC, venue=Venue.BITSTAMP,
        ts_ms=open_ms + 60_000, recv_ms=open_ms + 60_000, price=75000.0,
    )) is None


def test_favorite_side_inference():
    """When yes_mid > no_mid, favorite_side = yes."""
    base = 1_000_000_000_000
    log = _CollectingLog()
    obs = HourglassObserverStrategy(log_writer=log)
    _warmup_spot(obs, base)
    open_ms = base + 60 * 60_000
    close_ms = open_ms + 60 * 60_000
    ticker = "KXBTCD-T"
    obs.register_market(ticker, strike=74900.0, open_ms=open_ms, close_ms=close_ms)
    # YES higher mid: 80/82 → yes_mid 81
    obs.on_event(_book(
        ticker, open_ms + 30 * 60_000 + 1000,
        yes_bid=800, yes_ask=820, no_bid=180, no_ask=200,
    ))
    envs = [e for e in log.events if e["kind"] == "book_at_1hr_pretrigger"]
    assert len(envs) == 1
    assert envs[0]["favorite_side"] == "yes"
    assert envs[0]["favorite_mid_decicents"] == 810.0


def test_custom_observe_times():
    log = _CollectingLog()
    obs = HourglassObserverStrategy(
        log_writer=log, observe_minutes=(45, 55),  # custom subset
    )
    assert obs.observe_minutes == (45, 55)
    base = 1_000_000_000_000
    _warmup_spot(obs, base)
    open_ms = base + 60 * 60_000
    close_ms = open_ms + 60 * 60_000
    ticker = "KXBTCD-T"
    obs.register_market(ticker, strike=74900.0, open_ms=open_ms, close_ms=close_ms)
    # T+30 should NOT fire (not in custom set); T+45 and T+55 should
    obs.on_event(_book(ticker, open_ms + 30 * 60_000 + 1000))
    obs.on_event(_book(ticker, open_ms + 45 * 60_000 + 1000))
    obs.on_event(_book(ticker, open_ms + 55 * 60_000 + 1000))
    envs = [e for e in log.events if e["kind"] == "book_at_1hr_pretrigger"]
    labels = sorted(e["window_label"] for e in envs)
    assert labels == ["T+45", "T+55"]


def test_unregistered_market_no_emit():
    """Book for an unregistered ticker → no envelope."""
    base = 1_000_000_000_000
    log = _CollectingLog()
    obs = HourglassObserverStrategy(log_writer=log)
    _warmup_spot(obs, base)
    open_ms = base + 60 * 60_000
    obs.on_event(_book("KXBTCD-UNREGISTERED", open_ms + 30 * 60_000 + 1000))
    envs = [e for e in log.events if e["kind"] == "book_at_1hr_pretrigger"]
    assert len(envs) == 0
