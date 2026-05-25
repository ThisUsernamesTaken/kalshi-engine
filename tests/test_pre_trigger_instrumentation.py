"""Phase 12.8: pre-trigger book observation envelope tests.

Verifies the `book_at_pre_trigger` envelope:
- Fires during T+5 to T+8 of the cycle
- Does NOT fire during T+0 to T+5 (too early)
- Does NOT fire after T+8 (trigger window owns this period)
- Does NOT fire after entry (post-trade)
- Schema correctness (all fields present)
- 30s throttle per ticker
"""

from __future__ import annotations

from kalshi_engine.core.events import BookEvent, SpotEvent
from kalshi_engine.core.types import Crypto, Side, Venue
from kalshi_engine.strategies.favorite_chase.models.phase4_cutpoints import (
    Phase4CutpointsModel,
)
from kalshi_engine.strategies.favorite_chase.strategy import FavoriteChaseStrategy


class _CollectingLog:
    """Test double — collects envelopes."""

    def __init__(self):
        self.events: list[dict] = []

    def write(self, env: dict) -> None:
        self.events.append(env)


def _make_strategy(pre_trigger_observation=True):
    log = _CollectingLog()
    strat = FavoriteChaseStrategy(
        Phase4CutpointsModel(time_of_day_skip=False, align_mode="5tier_v13b"),
        log_writer=log,
        pre_trigger_observation=pre_trigger_observation,
    )
    return strat, log


def _warmup_btc(strat, base_ms):
    """Populate 50 minutes of spot history for BTC so vol_30m is computable."""
    for i in range(50):
        strat.on_event(SpotEvent(
            crypto=Crypto.BTC, venue=Venue.BITSTAMP,
            ts_ms=base_ms + i * 60_000, recv_ms=base_ms + i * 60_000,
            price=75000.0 + i * 0.1,
        ))


def _book(ticker, ts_ms, yes_bid=300, yes_ask=320, no_bid=680, no_ask=700):
    return BookEvent(
        ticker=ticker, ts_ms=ts_ms, recv_ms=ts_ms,
        yes_bid=yes_bid, yes_ask=yes_ask, no_bid=no_bid, no_ask=no_ask,
        yes_levels=(), no_levels=(),
    )


def test_pre_trigger_fires_in_5_to_8_window():
    base = 1_000_000_000_000
    strat, log = _make_strategy(pre_trigger_observation=True)
    _warmup_btc(strat, base)
    open_ms = base + 60 * 60_000
    close_ms = open_ms + 15 * 60_000
    ticker = "KXBTC15M-T"
    strat.register_market(ticker, strike=74900.0, open_ms=open_ms, close_ms=close_ms)
    # Book event at T+5:30
    strat.on_event(_book(ticker, open_ms + 5 * 60_000 + 30_000))
    matched = [e for e in log.events if e.get("kind") == "book_at_pre_trigger"]
    assert len(matched) == 1
    e = matched[0]
    assert e["ticker"] == ticker
    assert "ts_ms" in e and "elapsed_min" in e and "tau_min" in e
    for k in ("yes_bid", "yes_ask", "no_bid", "no_ask",
              "spot", "vol_30m", "bb_div", "bps_margin",
              "favorite_side", "favorite_mid_decicents"):
        assert k in e, f"missing field {k}"
    assert 5.0 <= e["elapsed_min"] < 8.0


def test_pre_trigger_does_not_fire_before_5min():
    base = 1_000_000_000_000
    strat, log = _make_strategy(pre_trigger_observation=True)
    _warmup_btc(strat, base)
    open_ms = base + 60 * 60_000
    close_ms = open_ms + 15 * 60_000
    ticker = "KXBTC15M-T"
    strat.register_market(ticker, strike=74900.0, open_ms=open_ms, close_ms=close_ms)
    # Book at T+3 (too early)
    strat.on_event(_book(ticker, open_ms + 3 * 60_000))
    matched = [e for e in log.events if e.get("kind") == "book_at_pre_trigger"]
    assert len(matched) == 0


def test_pre_trigger_does_not_fire_after_8min():
    base = 1_000_000_000_000
    strat, log = _make_strategy(pre_trigger_observation=True)
    _warmup_btc(strat, base)
    open_ms = base + 60 * 60_000
    close_ms = open_ms + 15 * 60_000
    ticker = "KXBTC15M-T"
    strat.register_market(ticker, strike=74900.0, open_ms=open_ms, close_ms=close_ms)
    # Book at T+9 (already in trigger window)
    strat.on_event(_book(ticker, open_ms + 9 * 60_000))
    matched = [e for e in log.events if e.get("kind") == "book_at_pre_trigger"]
    assert len(matched) == 0


def test_pre_trigger_disabled_emits_nothing():
    base = 1_000_000_000_000
    strat, log = _make_strategy(pre_trigger_observation=False)
    _warmup_btc(strat, base)
    open_ms = base + 60 * 60_000
    close_ms = open_ms + 15 * 60_000
    ticker = "KXBTC15M-T"
    strat.register_market(ticker, strike=74900.0, open_ms=open_ms, close_ms=close_ms)
    strat.on_event(_book(ticker, open_ms + 6 * 60_000))
    matched = [e for e in log.events if e.get("kind") == "book_at_pre_trigger"]
    assert len(matched) == 0


def test_pre_trigger_throttle_30s():
    """Two book events within 30s → only the first creates an envelope."""
    base = 1_000_000_000_000
    strat, log = _make_strategy(pre_trigger_observation=True)
    _warmup_btc(strat, base)
    open_ms = base + 60 * 60_000
    close_ms = open_ms + 15 * 60_000
    ticker = "KXBTC15M-T"
    strat.register_market(ticker, strike=74900.0, open_ms=open_ms, close_ms=close_ms)
    # First book at T+5:30 → fires
    strat.on_event(_book(ticker, open_ms + 5 * 60_000 + 30_000))
    # Second book at T+5:45 (15s later) → throttled
    strat.on_event(_book(ticker, open_ms + 5 * 60_000 + 45_000))
    # Third book at T+6:01 (31s after first) → fires
    strat.on_event(_book(ticker, open_ms + 5 * 60_000 + 61_000))
    matched = [e for e in log.events if e.get("kind") == "book_at_pre_trigger"]
    assert len(matched) == 2


def test_pre_trigger_does_not_fire_after_entry():
    """Once ticker is in self.entered (post-trade), pre-trigger stops firing."""
    base = 1_000_000_000_000
    strat, log = _make_strategy(pre_trigger_observation=True)
    _warmup_btc(strat, base)
    open_ms = base + 60 * 60_000
    close_ms = open_ms + 15 * 60_000
    ticker = "KXBTC15M-T"
    strat.register_market(ticker, strike=74900.0, open_ms=open_ms, close_ms=close_ms)
    # Manually mark entered (simulating post-trade state)
    strat.entered.add(ticker)
    # Book at T+6 should be suppressed
    strat.on_event(_book(ticker, open_ms + 6 * 60_000))
    matched = [e for e in log.events if e.get("kind") == "book_at_pre_trigger"]
    assert len(matched) == 0


def test_pre_trigger_favorite_inference_no_75c_yet():
    """At pre-trigger, no side has bid >= 75c. favorite_side picked by mid."""
    base = 1_000_000_000_000
    strat, log = _make_strategy(pre_trigger_observation=True)
    _warmup_btc(strat, base)
    open_ms = base + 60 * 60_000
    close_ms = open_ms + 15 * 60_000
    ticker = "KXBTC15M-T"
    strat.register_market(ticker, strike=74900.0, open_ms=open_ms, close_ms=close_ms)
    # NO is the higher-mid side: yes 30/32, no 68/70 → no_mid 69, yes_mid 31
    strat.on_event(_book(
        ticker, open_ms + 6 * 60_000,
        yes_bid=300, yes_ask=320, no_bid=680, no_ask=700,
    ))
    matched = [e for e in log.events if e.get("kind") == "book_at_pre_trigger"]
    assert len(matched) == 1
    assert matched[0]["favorite_side"] == "no"
    assert matched[0]["favorite_mid_decicents"] == 690.0
