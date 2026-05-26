"""Tests for per-crypto align-mode override (Phase 14.3).

Validates that the trader can route different cryptos to different
align modes (per-crypto Phase4CutpointsModel instances), and that the
mechanism falls back to the global model when no override is registered.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from kalshi_engine.core.events import BookEvent, SpotEvent
from kalshi_engine.core.types import Action, Crypto, Side, Venue
from kalshi_engine.strategies.favorite_chase.models.phase4_cutpoints import (
    Phase4CutpointsModel,
)
from kalshi_engine.strategies.hourglass_trader import HourglassTraderStrategy


def _make_log():
    log = MagicMock()
    log.writes = []
    log.write = lambda p: log.writes.append(p)
    return log


def _make_strategy(default_align="5tier_v13b_7_10_10",
                    per_crypto_align=None):
    """Construct trader with a global model + optional per-crypto models."""
    log = _make_log()
    global_model = Phase4CutpointsModel(align_mode=default_align)
    per_crypto_models = None
    if per_crypto_align:
        per_crypto_models = {
            c: Phase4CutpointsModel(align_mode=am)
            for c, am in per_crypto_align.items()
        }
    return HourglassTraderStrategy(
        log_writer=log, model=global_model,
        trigger_minutes=(30, 50), skip_hours_utc=(13,),
        max_favorite_cost_decicents=920, max_contracts=10,
        per_crypto_models=per_crypto_models,
    ), log


def _warmup_state(strat, crypto_str, base_ms=1_700_000_000_000, price=100_000.0):
    """Inject 35 min of synthetic spot ticks so vol_30m is computable."""
    from types import SimpleNamespace
    state = strat._state(crypto_str)
    for i in range(36):
        state.update_spot(SimpleNamespace(
            ts_ms=base_ms - (36 - i) * 60_000, price=price))


def _fire_entry(strat, ticker, side, fav_mid_dc, strike,
                cycle_open_ms=1_700_000_000_000):
    """Simulate a book event at T+30 of a registered cycle."""
    close_ms = cycle_open_ms + 60 * 60_000
    strat.register_market(ticker, strike, cycle_open_ms, close_ms)
    if side is Side.YES:
        b = BookEvent(
            ticker=ticker, ts_ms=cycle_open_ms + 30*60_000,
            recv_ms=cycle_open_ms + 30*60_000,
            yes_bid=int(fav_mid_dc), yes_ask=int(fav_mid_dc),
            no_bid=int(1000-fav_mid_dc), no_ask=int(1000-fav_mid_dc),
            yes_levels=((int(fav_mid_dc), 100.0),),
            no_levels=((int(1000-fav_mid_dc), 100.0),),
        )
    else:
        b = BookEvent(
            ticker=ticker, ts_ms=cycle_open_ms + 30*60_000,
            recv_ms=cycle_open_ms + 30*60_000,
            yes_bid=int(1000-fav_mid_dc), yes_ask=int(1000-fav_mid_dc),
            no_bid=int(fav_mid_dc), no_ask=int(fav_mid_dc),
            yes_levels=((int(1000-fav_mid_dc), 100.0),),
            no_levels=((int(fav_mid_dc), 100.0),),
        )
    return strat._on_book(b)


# ---- constructor ----------------------------------------------------

def test_default_empty_per_crypto_models():
    s, _ = _make_strategy(per_crypto_align=None)
    assert s.per_crypto_models == {}


def test_explicit_per_crypto_models_uppercased():
    s, _ = _make_strategy(per_crypto_align={"btc": "5tier_v13b_7_10_10",
                                             "eth": "5tier_v13b_1to3_ramp"})
    assert "BTC" in s.per_crypto_models
    assert "ETH" in s.per_crypto_models
    assert s.per_crypto_models["ETH"].align_mode == "5tier_v13b_1to3_ramp"


# ---- routing --------------------------------------------------------

def test_btc_routes_to_global_model_when_no_override():
    """BTC ticker with no per-crypto override uses global model."""
    s, _ = _make_strategy(default_align="5tier_v13b_7_10_10",
                           per_crypto_align=None)
    _warmup_state(s, "BTC")
    d = _fire_entry(s, "KXBTCD-CYC1-T100100", Side.NO, fav_mid_dc=850.0,
                     strike=100_100.0)
    if d is not None and d.action is Action.ENTER:
        # Global = T6 schedule: score in [4,5) -> 7ct, [5,6) -> 10ct, ≥6 -> 10ct
        assert d.size in (7, 10)


def test_eth_routes_to_per_crypto_model_when_override_set():
    """ETH ticker with --per-crypto-align-mode ETH=1to3_ramp uses ramp."""
    s, _ = _make_strategy(default_align="5tier_v13b_7_10_10",
                           per_crypto_align={"ETH": "5tier_v13b_1to3_ramp"})
    _warmup_state(s, "ETH", price=2000.0)
    d = _fire_entry(s, "KXETHD-CYC1-T2050", Side.NO, fav_mid_dc=850.0,
                     strike=2050.0)
    if d is not None and d.action is Action.ENTER:
        # Ramp ceiling at 3ct regardless of score
        assert d.size in (1, 2, 3)


def test_btc_unaffected_by_eth_override():
    """ETH override must NOT affect BTC sizing."""
    s, _ = _make_strategy(default_align="5tier_v13b_7_10_10",
                           per_crypto_align={"ETH": "5tier_v13b_1to3_ramp"})
    _warmup_state(s, "BTC")
    d = _fire_entry(s, "KXBTCD-CYC2-T100100", Side.NO, fav_mid_dc=850.0,
                     strike=100_100.0)
    if d is not None and d.action is Action.ENTER:
        # BTC uses T6 schedule, so size in (7, 10)
        assert d.size in (7, 10), f"BTC unexpectedly sized at {d.size}"


def test_per_crypto_diagnostics_show_correct_align_mode():
    """The diagnostics block must reflect the model that actually evaluated,
    not just the global. After routing through a per-crypto model, the
    diagnostics should reference the per-crypto align_mode."""
    s, _ = _make_strategy(default_align="5tier_v13b_7_10_10",
                           per_crypto_align={"ETH": "5tier_v13b_1to3_ramp"})
    _warmup_state(s, "ETH", price=2000.0)
    d = _fire_entry(s, "KXETHD-CYC3-T2050", Side.NO, fav_mid_dc=850.0,
                     strike=2050.0)
    if d is not None:
        # The model writes its own align_mode into diagnostics
        assert d.diagnostics.get("align_mode") == "5tier_v13b_1to3_ramp"


def test_max_contracts_still_clips_per_crypto_align():
    """Per-crypto model returns up to its native max; the global
    --max-contracts ceiling still applies on top."""
    s, _ = _make_strategy(default_align="5tier_v13b_7_10_10",
                           per_crypto_align={"BTC": "5tier_v13b_10_flat"})
    # 5tier_v13b_10_flat sizes at 10ct on score>=4. With global max=10,
    # final size is 10. Reduce global to 3 → final must be 3.
    s.max_contracts = 3
    _warmup_state(s, "BTC")
    d = _fire_entry(s, "KXBTCD-CYC4-T100100", Side.NO, fav_mid_dc=850.0,
                     strike=100_100.0)
    if d is not None and d.action is Action.ENTER:
        assert d.size <= 3


def test_per_crypto_align_combines_with_per_crypto_cap():
    """Both overrides active: ETH uses ramp (1/2/3), capped at 1 by
    per_crypto_max_contracts. Final size <= 1."""
    log = _make_log()
    s = HourglassTraderStrategy(
        log_writer=log,
        model=Phase4CutpointsModel(align_mode="5tier_v13b_7_10_10"),
        trigger_minutes=(30, 50), skip_hours_utc=(13,),
        max_favorite_cost_decicents=920, max_contracts=10,
        per_crypto_max_contracts={"ETH": 1},
        per_crypto_models={"ETH": Phase4CutpointsModel(align_mode="5tier_v13b_1to3_ramp")},
    )
    _warmup_state(s, "ETH", price=2000.0)
    d = _fire_entry(s, "KXETHD-CYC5-T2050", Side.NO, fav_mid_dc=850.0,
                     strike=2050.0)
    if d is not None and d.action is Action.ENTER:
        assert d.size == 1
