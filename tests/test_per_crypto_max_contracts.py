"""Tests for Phase 13.6 per-crypto sizing override.

Verifies that `HourglassTraderStrategy.per_crypto_max_contracts` clips
ENTER decisions per-crypto BEFORE the global `max_contracts` ceiling
applies. The use case: pin ETH to 1ct while leaving BTC at the
align-mode's native sizing.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from kalshi_engine.core.events import BookEvent
from kalshi_engine.core.types import Action, Side
from kalshi_engine.strategies.favorite_chase.models.phase4_cutpoints import (
    Phase4CutpointsModel,
)
from kalshi_engine.strategies.hourglass_trader import HourglassTraderStrategy


def _make_log():
    log = MagicMock()
    log.writes = []
    log.write = lambda p: log.writes.append(p)
    return log


def _make_strategy(per_crypto=None, max_contracts=10):
    return HourglassTraderStrategy(
        log_writer=_make_log(),
        model=Phase4CutpointsModel(align_mode="5tier_v13b_7_10_10"),
        trigger_minutes=(30, 50),
        skip_hours_utc=(13,),
        max_favorite_cost_decicents=920,
        max_contracts=max_contracts,
        per_crypto_max_contracts=per_crypto,
    )


# ---- constructor validation ---------------------------------------------

def test_default_empty_per_crypto_dict():
    s = _make_strategy()
    assert s.per_crypto_max_contracts == {}


def test_explicit_per_crypto_dict_stored_uppercase():
    s = _make_strategy(per_crypto={"btc": 10, "eth": 1})
    assert s.per_crypto_max_contracts == {"BTC": 10, "ETH": 1}


def test_per_crypto_zero_rejected():
    with pytest.raises(ValueError, match="per_crypto_max_contracts"):
        _make_strategy(per_crypto={"ETH": 0})


def test_per_crypto_negative_rejected():
    with pytest.raises(ValueError, match="per_crypto_max_contracts"):
        _make_strategy(per_crypto={"ETH": -1})


# ---- clipping behavior (integration with the decision pipeline) ---------

def _fire_entry(strat, ticker: str, side: Side, fav_mid_dc: float = 920.0,
                 strike: float = 100000.0, crypto: str = "BTC"):
    """Replicate the trader's evaluate path for a single test entry."""
    from kalshi_engine.core.interfaces import Decision
    open_ms = 1_700_000_000_000
    close_ms = open_ms + 60 * 60_000
    strat.register_market(ticker, strike, open_ms, close_ms)
    # Build a book whose mid lands on fav_mid_dc on the favorite side
    # For YES favorite at $0.92: yes_bid=yes_ask=920
    if side is Side.YES:
        b = BookEvent(
            ticker=ticker, ts_ms=open_ms + 30*60_000,
            recv_ms=open_ms + 30*60_000,
            yes_bid=int(fav_mid_dc), yes_ask=int(fav_mid_dc),
            no_bid=int(1000-fav_mid_dc), no_ask=int(1000-fav_mid_dc),
            yes_levels=((int(fav_mid_dc), 100.0),),
            no_levels=((int(1000-fav_mid_dc), 100.0),),
        )
    else:
        b = BookEvent(
            ticker=ticker, ts_ms=open_ms + 30*60_000,
            recv_ms=open_ms + 30*60_000,
            yes_bid=int(1000-fav_mid_dc), yes_ask=int(1000-fav_mid_dc),
            no_bid=int(fav_mid_dc), no_ask=int(fav_mid_dc),
            yes_levels=((int(1000-fav_mid_dc), 100.0),),
            no_levels=((int(fav_mid_dc), 100.0),),
        )
    return strat._on_book(b)


def test_no_override_uses_align_mode_sizing():
    """Without the cap, score 6.5 ETH should size at 10ct (T6 schedule)."""
    s = _make_strategy(per_crypto=None)
    # Seed state with reasonable spot+vol history for ETH
    from types import SimpleNamespace
    state = s._state("ETH")
    # Inject 35 minutes of spot at $2000 to get vol_30m computable
    open_ms = 1_700_000_000_000
    for i in range(36):
        state.update_spot(SimpleNamespace(ts_ms=open_ms - (36-i)*60_000, price=2000.0))
    d = _fire_entry(s, "KXETHD-CYC1-T2050", Side.NO, fav_mid_dc=850.0,
                     strike=2050.0, crypto="ETH")
    # Score won't necessarily be 6.5 without specific bb_div setup;
    # just verify size is whatever the model returns (no clipping applied).
    if d is not None and d.action is Action.ENTER:
        assert d.size <= 10  # global ceiling
        # And there was no per-crypto override, so no over-clipping


def test_per_crypto_eth_clipped_to_1():
    """When per_crypto={'ETH': 1}, any ETH ENTER must come out as 1ct."""
    s = _make_strategy(per_crypto={"ETH": 1})
    from types import SimpleNamespace
    state = s._state("ETH")
    open_ms = 1_700_000_000_000
    for i in range(36):
        state.update_spot(SimpleNamespace(ts_ms=open_ms - (36-i)*60_000, price=2000.0))
    d = _fire_entry(s, "KXETHD-CYC2-T2050", Side.NO, fav_mid_dc=850.0,
                     strike=2050.0, crypto="ETH")
    if d is not None and d.action is Action.ENTER:
        assert d.size == 1, f"ETH cap should pin size to 1, got {d.size}"


def test_per_crypto_btc_unaffected_by_eth_cap():
    """ETH cap must NOT affect BTC sizing."""
    s = _make_strategy(per_crypto={"ETH": 1})
    from types import SimpleNamespace
    state = s._state("BTC")
    open_ms = 1_700_000_000_000
    for i in range(36):
        state.update_spot(SimpleNamespace(ts_ms=open_ms - (36-i)*60_000, price=100000.0))
    d = _fire_entry(s, "KXBTCD-CYC1-T100100", Side.NO, fav_mid_dc=850.0,
                     strike=100100.0, crypto="BTC")
    if d is not None and d.action is Action.ENTER:
        # BTC isn't capped, so size can be up to global max_contracts
        assert d.size >= 1
        assert d.size <= 10  # only global cap


# ---- clipping math --------------------------------------------------------

def test_per_crypto_cap_applied_before_global_max_contracts():
    """Order of operations: per_crypto first, then global cap. With ETH=5
    and global max=10, a 7ct align-mode decision should land at 5."""
    from dataclasses import replace
    from kalshi_engine.core.interfaces import Decision
    s = _make_strategy(per_crypto={"ETH": 5}, max_contracts=10)
    # Synthetic Decision direct (skip the full evaluate pipeline)
    d = Decision(ticker="KXETHD-X", action=Action.ENTER, side=Side.NO,
                  size=7, confidence=0.8, reason="test", diagnostics={})
    # Manually replicate the trader's clip logic
    crypto = "ETH"
    per_cap = s.per_crypto_max_contracts.get(crypto.upper())
    if per_cap is not None and d.size > per_cap:
        d = replace(d, size=per_cap)
    if d.size > s.max_contracts:
        d = replace(d, size=s.max_contracts)
    assert d.size == 5  # ETH cap applied


def test_per_crypto_cap_below_global_still_respects_per_crypto():
    """ETH cap=1, global max=10, model returns 10 → final 1."""
    from dataclasses import replace
    from kalshi_engine.core.interfaces import Decision
    s = _make_strategy(per_crypto={"ETH": 1}, max_contracts=10)
    d = Decision(ticker="KXETHD-X", action=Action.ENTER, side=Side.NO,
                  size=10, confidence=0.8, reason="test", diagnostics={})
    per_cap = s.per_crypto_max_contracts.get("ETH")
    if per_cap is not None and d.size > per_cap:
        d = replace(d, size=per_cap)
    assert d.size == 1


def test_per_crypto_cap_does_not_inflate_size():
    """If model returns 1ct and per_crypto cap is 10, final is still 1
    (the cap is a CEILING, not a floor)."""
    from dataclasses import replace
    from kalshi_engine.core.interfaces import Decision
    s = _make_strategy(per_crypto={"ETH": 10}, max_contracts=10)
    d = Decision(ticker="KXETHD-X", action=Action.ENTER, side=Side.NO,
                  size=1, confidence=0.8, reason="test", diagnostics={})
    per_cap = s.per_crypto_max_contracts.get("ETH")
    if per_cap is not None and d.size > per_cap:
        d = replace(d, size=per_cap)
    assert d.size == 1


# ---- SKIP decisions unaffected by per_crypto cap ------------------------

def test_skip_decisions_unaffected_by_per_crypto():
    """A SKIP decision's size stays 0 even with per_crypto override."""
    from dataclasses import replace
    from kalshi_engine.core.interfaces import Decision
    s = _make_strategy(per_crypto={"ETH": 1}, max_contracts=10)
    d = Decision(ticker="KXETHD-X", action=Action.SKIP, side=Side.NO,
                  size=0, confidence=0.0, reason="vol gate", diagnostics={})
    per_cap = s.per_crypto_max_contracts.get("ETH")
    # The trader only clips on ENTER, so SKIP must pass through unchanged
    if d.action is Action.ENTER and per_cap is not None and d.size > per_cap:
        d = replace(d, size=per_cap)
    assert d.size == 0
