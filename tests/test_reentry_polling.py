"""Phase 12.3: re-entry polling tests.

Validates the polling-mode contract:
- Skipping at T+8 does NOT lock the ticker; re-eval allowed at T+9+.
- ENTER at any point locks the ticker (no add-to-position).
- Within ``reentry_cutoff_ms`` of close, evaluation stops.
- Per-ticker throttle ``reentry_throttle_ms`` prevents back-to-back evals.
- ``reentry_mode='disabled'`` preserves legacy single-shot dedup (SKIP locks).
- ``diagnostics['is_reentry']`` tags subsequent evaluations.
"""

from __future__ import annotations

import pytest

from kalshi_engine.core.events import BookEvent, SpotEvent
from kalshi_engine.core.types import Action, Crypto, Side, Venue
from kalshi_engine.strategies.favorite_chase.strategy import (
    FavoriteChaseStrategy,
    REENTRY_MODES,
)

OPEN_MS = 1_000_000_000_000
CLOSE_MS = OPEN_MS + 15 * 60_000
T8_MS = OPEN_MS + 8 * 60_000
T14_MS = OPEN_MS + 14 * 60_000  # 1 min before close


def _book(ticker, ts_ms, yes_bid=750, yes_ask=760):
    no_bid = 1000 - yes_ask
    no_ask = 1000 - yes_bid
    return BookEvent(
        ticker=ticker, ts_ms=ts_ms, recv_ms=ts_ms,
        yes_bid=yes_bid, yes_ask=yes_ask,
        no_bid=no_bid, no_ask=no_ask,
        yes_levels=((yes_bid, 1.0),),
        no_levels=((no_bid, 1.0),),
    )


def _make_state(strat, ticker, n_spots=40):
    strat.register_market(ticker, strike=75100.0,
                          open_ms=OPEN_MS, close_ms=CLOSE_MS)
    base = OPEN_MS - 30 * 60_000
    for i in range(n_spots):
        strat.on_event(SpotEvent(
            crypto=Crypto.BTC, venue=Venue.BITSTAMP,
            ts_ms=base + i * 60_000, recv_ms=base + i * 60_000,
            price=75000.0 + i * 0.5,
        ))


def test_polling_mode_validation_rejects_unknown():
    with pytest.raises(ValueError, match="reentry_mode"):
        FavoriteChaseStrategy(reentry_mode="aggressive")


def test_reentry_modes_constants():
    assert REENTRY_MODES == ("disabled", "polling")


def test_disabled_mode_locks_on_skip_legacy():
    """In disabled mode, a SKIP locks the ticker just like an ENTER (legacy)."""
    strat = FavoriteChaseStrategy(reentry_mode="disabled")
    _make_state(strat, "KXBTC15M-T")
    # First book: produces a decision (could be enter or skip)
    d1 = strat.on_event(_book("KXBTC15M-T", T8_MS))
    assert d1 is not None  # SOMETHING fired
    # Second book 30s later: must return None (legacy dedup)
    d2 = strat.on_event(_book("KXBTC15M-T", T8_MS + 30_000))
    assert d2 is None


def test_polling_mode_skip_does_not_lock(monkeypatch):
    """In polling mode, a SKIP must NOT lock; later books still evaluate."""
    strat = FavoriteChaseStrategy(reentry_mode="polling",
                                  reentry_throttle_ms=0)
    _make_state(strat, "KXBTC15M-T")
    # Force model to SKIP by patching its evaluate
    from kalshi_engine.core.interfaces import Decision
    skip_d = Decision(
        ticker="KXBTC15M-T", action=Action.SKIP, side=Side.YES,
        size=0, reason="SKIP: forced", diagnostics={"forced": True},
    )
    monkeypatch.setattr(strat.model, "evaluate", lambda **kw: skip_d)
    d1 = strat.on_event(_book("KXBTC15M-T", T8_MS))
    assert d1 is not None and d1.action is Action.SKIP
    # 1 minute later: should still evaluate (no lock on skip)
    d2 = strat.on_event(_book("KXBTC15M-T", T8_MS + 60_000))
    assert d2 is not None  # got a fresh evaluation
    assert d2.action is Action.SKIP  # still skipped (forced)


def test_polling_mode_enter_locks_ticker(monkeypatch):
    """An ENTER decision locks the ticker -- no further evaluations."""
    strat = FavoriteChaseStrategy(reentry_mode="polling",
                                  reentry_throttle_ms=0)
    _make_state(strat, "KXBTC15M-T")
    from kalshi_engine.core.interfaces import Decision
    enter_d = Decision(
        ticker="KXBTC15M-T", action=Action.ENTER, side=Side.YES,
        size=1, reason="ENTER: forced", diagnostics={"forced": True},
    )
    monkeypatch.setattr(strat.model, "evaluate", lambda **kw: enter_d)
    d1 = strat.on_event(_book("KXBTC15M-T", T8_MS))
    assert d1 is not None and d1.action is Action.ENTER
    # Subsequent book: locked
    d2 = strat.on_event(_book("KXBTC15M-T", T8_MS + 60_000))
    assert d2 is None


def test_polling_mode_throttle_blocks_back_to_back(monkeypatch):
    """Two book events within ``reentry_throttle_ms`` -> only first evaluates."""
    strat = FavoriteChaseStrategy(reentry_mode="polling",
                                  reentry_throttle_ms=30_000)
    _make_state(strat, "KXBTC15M-T")
    from kalshi_engine.core.interfaces import Decision
    skip_d = Decision(
        ticker="KXBTC15M-T", action=Action.SKIP, side=Side.YES,
        size=0, reason="SKIP: forced", diagnostics={"forced": True},
    )
    monkeypatch.setattr(strat.model, "evaluate", lambda **kw: skip_d)
    d1 = strat.on_event(_book("KXBTC15M-T", T8_MS))
    assert d1 is not None
    # 10 s later: throttled, returns None
    d2 = strat.on_event(_book("KXBTC15M-T", T8_MS + 10_000))
    assert d2 is None
    # 31 s later: past throttle, evaluates again
    d3 = strat.on_event(_book("KXBTC15M-T", T8_MS + 31_000))
    assert d3 is not None


def test_polling_mode_cutoff_blocks_last_2min(monkeypatch):
    """Within reentry_cutoff_ms of close, no re-evaluation."""
    strat = FavoriteChaseStrategy(reentry_mode="polling",
                                  reentry_cutoff_ms=120_000,
                                  reentry_throttle_ms=0)
    _make_state(strat, "KXBTC15M-T")
    from kalshi_engine.core.interfaces import Decision
    skip_d = Decision(
        ticker="KXBTC15M-T", action=Action.SKIP, side=Side.YES,
        size=0, reason="SKIP", diagnostics={"forced": True},
    )
    monkeypatch.setattr(strat.model, "evaluate", lambda **kw: skip_d)
    # T+8m: evaluates normally
    d1 = strat.on_event(_book("KXBTC15M-T", T8_MS))
    assert d1 is not None
    # T+14m (1 min before close, inside 2-min cutoff): blocked
    d2 = strat.on_event(_book("KXBTC15M-T", T14_MS))
    assert d2 is None
    # T+12:59m (2 min 1 sec before close = just outside cutoff): allowed
    d3 = strat.on_event(_book("KXBTC15M-T", CLOSE_MS - 121_000))
    assert d3 is not None


def test_is_reentry_diagnostic_stamps_correctly(monkeypatch):
    """First evaluation: is_reentry=False; subsequent: is_reentry=True."""
    strat = FavoriteChaseStrategy(reentry_mode="polling",
                                  reentry_throttle_ms=0)
    _make_state(strat, "KXBTC15M-T")
    from kalshi_engine.core.interfaces import Decision

    # Force SKIP on the first call so we can re-evaluate later.
    calls = []
    def _skip(**kw):
        diag = {"forced": True}
        calls.append(kw)
        return Decision(
            ticker="KXBTC15M-T", action=Action.SKIP, side=Side.YES,
            size=0, reason="SKIP", diagnostics=diag,
        )
    monkeypatch.setattr(strat.model, "evaluate", _skip)
    d1 = strat.on_event(_book("KXBTC15M-T", T8_MS))
    assert d1.diagnostics["is_reentry"] is False
    assert d1.diagnostics["reentry_mode"] == "polling"
    d2 = strat.on_event(_book("KXBTC15M-T", T8_MS + 60_000))
    assert d2.diagnostics["is_reentry"] is True


def test_polling_mode_default_off_in_strategy_constructor():
    """Strategy default is disabled (back-compat with prior callers)."""
    strat = FavoriteChaseStrategy()
    assert strat.reentry_mode == "disabled"
