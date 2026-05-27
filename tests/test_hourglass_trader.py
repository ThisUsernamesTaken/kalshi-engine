"""Tests for the 1hr Hourglass live trader strategy (Phase 13.1).

The strategy mirrors the observer's registration + event-routing pattern,
but at configured T+M windows it runs the Phase4CutpointsModel evaluation
and returns a Decision. Tests cover:

- Trigger timing (only T+30 / T+50 fire by default)
- Per-cycle dedup (one entry per ticker max)
- Hour-skip filter (default 13Z)
- MAX_FAV_COST gate (fee-trap protection)
- 75c favorite-chase floor
- Per-window evaluate-once semantics
- Defense-in-depth size ceiling
- Settlement clears dedup state
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from kalshi_engine.core.events import BookEvent, SettlementEvent
from kalshi_engine.core.interfaces import Decision
from kalshi_engine.core.types import Action, Crypto, Side, Venue
from kalshi_engine.strategies.favorite_chase.models.phase4_cutpoints import (
    Phase4CutpointsModel,
)
from kalshi_engine.strategies.hourglass_trader import HourglassTraderStrategy


# ---- shared scaffolding ---------------------------------------------------

def _make_log():
    """A LiveLogWriter stub that collects every write() call."""
    log = MagicMock()
    log.writes = []
    def _write(payload):
        log.writes.append(payload)
    log.write = _write
    return log


def _make_model(align_mode: str = "5tier_v13b_1to3_flat") -> Phase4CutpointsModel:
    return Phase4CutpointsModel(align_mode=align_mode)


def _make_book(
    ticker: str = "KXBTCD-26MAY2520-T100000",
    cycle_open_ms: int = 1_700_000_000_000,
    elapsed_min: float = 30.0,
    yes_bid: int = 200, yes_ask: int = 205,
    no_bid: int = 800, no_ask: int = 805,
) -> BookEvent:
    """Book event at (cycle_open + elapsed_min)."""
    recv_ms = int(cycle_open_ms + elapsed_min * 60_000)
    return BookEvent(
        ticker=ticker, ts_ms=recv_ms, recv_ms=recv_ms,
        yes_bid=yes_bid, yes_ask=yes_ask,
        no_bid=no_bid, no_ask=no_ask,
        yes_levels=((yes_bid, 100.0),),
        no_levels=((no_bid, 100.0),),
    )


def _make_strategy(
    align_mode: str = "5tier_v13b_1to3_flat",
    trigger_minutes=(30, 50),
    skip_hours_utc=(13,),
    max_favorite_cost_decicents=920,
    max_contracts=3,
    min_entry_d_norm=0.0,
    near_strike_allowed_minute=55,
):
    log = _make_log()
    model = _make_model(align_mode)
    strat = HourglassTraderStrategy(
        log_writer=log, model=model,
        trigger_minutes=trigger_minutes,
        skip_hours_utc=skip_hours_utc,
        max_favorite_cost_decicents=max_favorite_cost_decicents,
        max_contracts=max_contracts,
        min_entry_d_norm=min_entry_d_norm,
        near_strike_allowed_minute=near_strike_allowed_minute,
    )
    return strat, log


# ---- construction -------------------------------------------------------

def test_construction_requires_log():
    with pytest.raises(ValueError, match="log_writer"):
        HourglassTraderStrategy(log_writer=None, model=_make_model())


def test_construction_requires_model():
    with pytest.raises(ValueError, match="model"):
        HourglassTraderStrategy(log_writer=_make_log(), model=None)


def test_construction_max_contracts_must_be_positive():
    with pytest.raises(ValueError, match="max_contracts"):
        HourglassTraderStrategy(
            log_writer=_make_log(), model=_make_model(), max_contracts=0,
        )


def test_construction_defaults():
    s, _ = _make_strategy()
    assert s.trigger_minutes == (30, 50)
    assert s.skip_hours_utc == frozenset({13})
    assert s.max_favorite_cost_decicents == 920
    assert s.max_contracts == 3


# ---- registration -------------------------------------------------------

def test_register_market_records_meta():
    s, _ = _make_strategy()
    s.register_market("KXBTCD-T", strike=100_000.0,
                       open_ms=1_000_000, close_ms=4_600_000)
    meta = s.markets["KXBTCD-T"]
    assert meta.ticker == "KXBTCD-T"
    assert meta.strike == 100_000.0
    assert meta.open_ms == 1_000_000
    assert meta.close_ms == 4_600_000


# ---- trigger timing -----------------------------------------------------

def test_unregistered_market_yields_none():
    s, _ = _make_strategy()
    book = _make_book(ticker="KXBTCD-UNKNOWN", elapsed_min=30.0)
    assert s.on_event(book) is None


def test_book_outside_trigger_window_yields_none():
    """Book at T+15 (no trigger window there) -> None."""
    s, _ = _make_strategy()
    s.register_market("KXBTCD-T", strike=100_000.0,
                       open_ms=1_700_000_000_000, close_ms=1_700_003_600_000)
    book = _make_book(ticker="KXBTCD-T",
                      cycle_open_ms=1_700_000_000_000, elapsed_min=15.0)
    assert s.on_event(book) is None


def test_book_at_t40_yields_none():
    """T+40 isn't a default trigger minute -> None."""
    s, _ = _make_strategy()
    s.register_market("KXBTCD-T", strike=100_000.0,
                       open_ms=1_700_000_000_000, close_ms=1_700_003_600_000)
    book = _make_book(ticker="KXBTCD-T",
                      cycle_open_ms=1_700_000_000_000, elapsed_min=40.0)
    assert s.on_event(book) is None


def test_book_at_t30_fires_evaluation():
    """T+30 IS a trigger minute -> evaluation runs, returns a Decision."""
    s, log = _make_strategy()
    s.register_market("KXBTCD-T", strike=100_000.0,
                       open_ms=1_700_000_000_000, close_ms=1_700_003_600_000)
    book = _make_book(
        ticker="KXBTCD-T", cycle_open_ms=1_700_000_000_000, elapsed_min=30.0,
        # Favorite NO at 80c (yes_bid=20, no_bid=80) — clears 75c trigger,
        # under 92c MAX_FAV_COST. UTC hour of recv_ms is NOT 13Z by design.
        yes_bid=180, yes_ask=200, no_bid=800, no_ask=820,
    )
    d = s.on_event(book)
    assert d is not None
    # Some decisions reach the model and return ENTER/SKIP — both are valid
    # depending on internal score; the key is that on_event returned a
    # Decision (not None) when the trigger window fires.
    assert d.action in (Action.ENTER, Action.SKIP)
    # The strategy must have logged the decision.
    decision_writes = [w for w in log.writes if w.get("kind") == "decision"]
    assert len(decision_writes) == 1


# ---- skip filters at the trigger gate ----------------------------------

def test_skip_hour_13z_blocks_entry():
    """A book whose recv_ms lands in UTC 13Z is SKIPped with 'hour-skip'."""
    # Pick a cycle_open_ms such that recv_ms = open + 30min lands at 13:30Z
    # on 2026-05-25. 13:00:00Z on 2026-05-25 = 1779706800000 ms.
    # 2026-05-25 13:00:00Z = day 20599 since epoch * 86400 + 13*3600
    open_13z = (20599 * 86400 + 13 * 3600) * 1000  # 1_779_800_400_000
    s, log = _make_strategy()
    s.register_market("KXBTCD-T", strike=100_000.0,
                       open_ms=open_13z, close_ms=open_13z + 3_600_000)
    book = _make_book(
        ticker="KXBTCD-T", cycle_open_ms=open_13z, elapsed_min=30.0,
        yes_bid=180, yes_ask=200, no_bid=800, no_ask=820,
    )
    d = s.on_event(book)
    assert d is not None
    assert d.action is Action.SKIP
    assert "hour-skip 13Z" in d.reason


def test_max_favorite_cost_gate_blocks_entry():
    """fav_mid > MAX_FAV_COST -> SKIP (fee-trap zone)."""
    s, _ = _make_strategy()
    s.register_market("KXBTCD-T", strike=100_000.0,
                       open_ms=1_700_000_000_000, close_ms=1_700_003_600_000)
    book = _make_book(
        ticker="KXBTCD-T", cycle_open_ms=1_700_000_000_000, elapsed_min=30.0,
        # NO is favorite at 95c — over the 92c cap.
        yes_bid=40, yes_ask=50, no_bid=950, no_ask=960,
    )
    d = s.on_event(book)
    assert d is not None
    assert d.action is Action.SKIP
    assert "max_favorite_cost" in d.reason


def test_fav_below_75c_blocks_entry():
    """fav_mid < 75c -> SKIP (favorite-chase trigger floor)."""
    s, _ = _make_strategy()
    s.register_market("KXBTCD-T", strike=100_000.0,
                       open_ms=1_700_000_000_000, close_ms=1_700_003_600_000)
    book = _make_book(
        ticker="KXBTCD-T", cycle_open_ms=1_700_000_000_000, elapsed_min=30.0,
        # YES at 60c, NO at 40c — favorite is YES at 60c, below 75c floor.
        yes_bid=600, yes_ask=610, no_bid=390, no_ask=400,
    )
    d = s.on_event(book)
    assert d is not None
    assert d.action is Action.SKIP
    assert "trigger" in d.reason


def test_empty_skip_hours_allows_13z():
    """Passing skip_hours_utc=() disables the hour-skip filter entirely."""
    # 2026-05-25 13:00:00Z = day 20599 since epoch * 86400 + 13*3600
    open_13z = (20599 * 86400 + 13 * 3600) * 1000  # 1_779_800_400_000
    s, _ = _make_strategy(skip_hours_utc=())
    s.register_market("KXBTCD-T", strike=100_000.0,
                       open_ms=open_13z, close_ms=open_13z + 3_600_000)
    book = _make_book(
        ticker="KXBTCD-T", cycle_open_ms=open_13z, elapsed_min=30.0,
        yes_bid=180, yes_ask=200, no_bid=800, no_ask=820,
    )
    d = s.on_event(book)
    # Should reach model evaluation (no 'hour-skip' reason).
    assert d is not None
    assert "hour-skip" not in d.reason


# ---- per-cycle dedup ----------------------------------------------------

def test_one_entry_per_ticker_per_cycle():
    """After an ENTER fires at T+30, T+50 must not enter the same ticker again."""
    s, _ = _make_strategy()
    open_ms = 1_700_000_000_000
    s.register_market("KXBTCD-T", strike=100_000.0,
                       open_ms=open_ms, close_ms=open_ms + 3_600_000)
    # Install a fake model that always returns ENTER 3ct.
    s._model = MagicMock()
    s._model.evaluate = MagicMock(return_value=Decision(
        ticker="KXBTCD-T", action=Action.ENTER, side=Side.NO,
        size=3, confidence=0.9, reason="forced ENTER", diagnostics={},
    ))

    book_30 = _make_book(
        ticker="KXBTCD-T", cycle_open_ms=open_ms, elapsed_min=30.0,
        yes_bid=180, yes_ask=200, no_bid=800, no_ask=820,
    )
    d1 = s.on_event(book_30)
    assert d1 is not None
    assert d1.action is Action.ENTER
    assert "KXBTCD-T" in s._entered

    book_50 = _make_book(
        ticker="KXBTCD-T", cycle_open_ms=open_ms, elapsed_min=50.0,
        yes_bid=180, yes_ask=200, no_bid=800, no_ask=820,
    )
    d2 = s.on_event(book_50)
    assert d2 is None  # dedup blocks the second evaluation


def test_one_main_entry_per_crypto_cycle_across_strikes():
    """After one BTC hourly entry, other BTC strikes in that cycle are skipped."""
    s, _ = _make_strategy()
    open_ms = 1_700_000_000_000
    s.register_market("KXBTCD-CYC-T100000", strike=100_000.0,
                       open_ms=open_ms, close_ms=open_ms + 3_600_000)
    s.register_market("KXBTCD-CYC-T100100", strike=100_100.0,
                       open_ms=open_ms, close_ms=open_ms + 3_600_000)
    s._model = MagicMock()
    s._model.evaluate = MagicMock(return_value=Decision(
        ticker="KXBTCD-CYC-T100000", action=Action.ENTER, side=Side.NO,
        size=3, confidence=0.9, reason="forced ENTER", diagnostics={},
    ))

    first = _make_book(
        ticker="KXBTCD-CYC-T100000", cycle_open_ms=open_ms, elapsed_min=30.0,
        yes_bid=180, yes_ask=200, no_bid=800, no_ask=820,
    )
    second = _make_book(
        ticker="KXBTCD-CYC-T100100", cycle_open_ms=open_ms, elapsed_min=50.0,
        yes_bid=180, yes_ask=200, no_bid=800, no_ask=820,
    )
    d1 = s.on_event(first)
    d2 = s.on_event(second)
    assert d1 is not None
    assert d1.action is Action.ENTER
    assert d2 is not None
    assert d2.action is Action.SKIP
    assert "already entered BTC cycle" in d2.reason
    assert s._model.evaluate.call_count == 1


def test_one_evaluation_per_window():
    """A second book event within the SAME T+30 window doesn't re-evaluate."""
    s, log = _make_strategy()
    open_ms = 1_700_000_000_000
    s.register_market("KXBTCD-T", strike=100_000.0,
                       open_ms=open_ms, close_ms=open_ms + 3_600_000)
    book_a = _make_book(
        ticker="KXBTCD-T", cycle_open_ms=open_ms, elapsed_min=30.0,
        yes_bid=180, yes_ask=200, no_bid=800, no_ask=820,
    )
    book_b = _make_book(
        ticker="KXBTCD-T", cycle_open_ms=open_ms, elapsed_min=30.1,
        yes_bid=180, yes_ask=200, no_bid=800, no_ask=820,
    )
    d1 = s.on_event(book_a)
    d2 = s.on_event(book_b)
    assert d1 is not None
    assert d2 is None  # window already consumed by the first book


def test_settlement_clears_dedup_state():
    """SettlementEvent for a ticker clears its _entered / _evaluated state."""
    s, _ = _make_strategy()
    s._entered.add("KXBTCD-T")
    s._entered_cycles.add(("BTC", 1_700_000_000_000))
    s.register_market("KXBTCD-T", strike=100_000.0,
                       open_ms=1_700_000_000_000,
                       close_ms=1_700_003_600_000)
    s._evaluated["KXBTCD-T"] = {30}
    ts = 1_700_000_000_000
    settle = SettlementEvent(
        ticker="KXBTCD-T", ts_ms=ts, recv_ms=ts,
        result=Side.YES, settle_value=1.0, determined_ms=ts,
    )
    s.on_event(settle)
    assert "KXBTCD-T" not in s._entered
    assert ("BTC", 1_700_000_000_000) not in s._entered_cycles
    assert "KXBTCD-T" not in s._evaluated


# ---- defense-in-depth size ceiling -------------------------------------

def test_max_contracts_clips_oversize_decisions():
    """If the model returns size > max_contracts, the trader must clip."""
    s, _ = _make_strategy(max_contracts=3)
    open_ms = 1_700_000_000_000
    s.register_market("KXBTCD-T", strike=100_000.0,
                       open_ms=open_ms, close_ms=open_ms + 3_600_000)
    # Install a fake model that returns an oversized ENTER (10 > 3).
    s._model = MagicMock()
    s._model.evaluate = MagicMock(return_value=Decision(
        ticker="KXBTCD-T", action=Action.ENTER, side=Side.NO,
        size=10, confidence=1.0, reason="forced oversize",
        diagnostics={},
    ))
    book = _make_book(
        ticker="KXBTCD-T", cycle_open_ms=open_ms, elapsed_min=30.0,
        yes_bid=180, yes_ask=200, no_bid=800, no_ask=820,
    )
    d = s.on_event(book)
    assert d is not None
    assert d.action is Action.ENTER
    assert d.size == 3  # clipped to max_contracts


def test_min_d_norm_blocks_close_strike_before_allowed_minute():
    s, _ = _make_strategy(
        trigger_minutes=(40,), min_entry_d_norm=1.5,
        near_strike_allowed_minute=55,
    )
    open_ms = 1_700_000_000_000
    s.register_market("KXBTCD-T", strike=100_000.0,
                       open_ms=open_ms, close_ms=open_ms + 3_600_000)
    s._model = MagicMock()
    s._model.evaluate = MagicMock(return_value=Decision(
        ticker="KXBTCD-T", action=Action.ENTER, side=Side.NO,
        size=3, confidence=1.0, reason="forced close strike",
        diagnostics={"d_norm": 1.2},
    ))
    book = _make_book(
        ticker="KXBTCD-T", cycle_open_ms=open_ms, elapsed_min=40.0,
        yes_bid=180, yes_ask=200, no_bid=800, no_ask=820,
    )
    d = s.on_event(book)
    assert d is not None
    assert d.action is Action.SKIP
    assert "too close to strike" in d.reason


def test_min_d_norm_allows_close_strike_at_allowed_minute():
    s, _ = _make_strategy(
        trigger_minutes=(55,), min_entry_d_norm=1.5,
        near_strike_allowed_minute=55,
    )
    open_ms = 1_700_000_000_000
    s.register_market("KXBTCD-T", strike=100_000.0,
                       open_ms=open_ms, close_ms=open_ms + 3_600_000)
    s._model = MagicMock()
    s._model.evaluate = MagicMock(return_value=Decision(
        ticker="KXBTCD-T", action=Action.ENTER, side=Side.NO,
        size=3, confidence=1.0, reason="forced close strike",
        diagnostics={"d_norm": 1.2},
    ))
    book = _make_book(
        ticker="KXBTCD-T", cycle_open_ms=open_ms, elapsed_min=55.0,
        yes_bid=180, yes_ask=200, no_bid=800, no_ask=820,
    )
    d = s.on_event(book)
    assert d is not None
    assert d.action is Action.ENTER


# ---- favorite-side determination ---------------------------------------

def test_yes_favorite_when_yes_mid_higher():
    """When YES mid > NO mid, the trader picks YES as the favorite."""
    s, log = _make_strategy()
    open_ms = 1_700_000_000_000
    s.register_market("KXBTCD-T", strike=100_000.0,
                       open_ms=open_ms, close_ms=open_ms + 3_600_000)
    book = _make_book(
        ticker="KXBTCD-T", cycle_open_ms=open_ms, elapsed_min=30.0,
        yes_bid=800, yes_ask=820, no_bid=180, no_ask=200,
    )
    s.on_event(book)
    decision_writes = [w for w in log.writes if w.get("kind") == "decision"]
    assert len(decision_writes) == 1
    assert decision_writes[0]["side"] == "yes"


def test_no_favorite_when_no_mid_higher():
    s, log = _make_strategy()
    open_ms = 1_700_000_000_000
    s.register_market("KXBTCD-T", strike=100_000.0,
                       open_ms=open_ms, close_ms=open_ms + 3_600_000)
    book = _make_book(
        ticker="KXBTCD-T", cycle_open_ms=open_ms, elapsed_min=30.0,
        yes_bid=180, yes_ask=200, no_bid=800, no_ask=820,
    )
    s.on_event(book)
    decision_writes = [w for w in log.writes if w.get("kind") == "decision"]
    assert len(decision_writes) == 1
    assert decision_writes[0]["side"] == "no"


# ---- custom trigger config ---------------------------------------------

def test_custom_trigger_minutes_respected():
    """trigger_minutes=(45,) only fires at T+45, not T+30/T+50."""
    s, _ = _make_strategy(trigger_minutes=(45,))
    open_ms = 1_700_000_000_000
    s.register_market("KXBTCD-T", strike=100_000.0,
                       open_ms=open_ms, close_ms=open_ms + 3_600_000)
    book_30 = _make_book(ticker="KXBTCD-T", cycle_open_ms=open_ms,
                         elapsed_min=30.0)
    book_45 = _make_book(ticker="KXBTCD-T", cycle_open_ms=open_ms,
                         elapsed_min=45.0,
                         yes_bid=180, yes_ask=200, no_bid=800, no_ask=820)
    book_50 = _make_book(ticker="KXBTCD-T", cycle_open_ms=open_ms,
                         elapsed_min=50.0,
                         yes_bid=180, yes_ask=200, no_bid=800, no_ask=820)
    assert s.on_event(book_30) is None
    assert s.on_event(book_45) is not None
    assert s.on_event(book_50) is None
