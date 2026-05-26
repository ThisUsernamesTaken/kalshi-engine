"""Tests for 5tier_v13b_btc_dnorm_gate align mode (Phase 14.9).

Same V13b hard gates + score formula as 5tier_v13b_7_10_10. Two new
gates layered on top:
    score < 4.0                                  -> SKIP (H1 floor)
    score >= 4.0 AND d_norm in [1.5, 2.0]        -> SKIP (danger zone)
    score in [4.0, 5.0)                          -> 7 ct
    score in [5.0, 6.0)                          -> 9 ct
    score >= 6.0                                 -> 10 ct

The d_norm danger zone was identified after the 2026-05-26 KXBTCD
loss cluster: markets where d_norm = bps_margin / (vol_30m * sqrt(tau))
sits in [1.5, 2.0] satisfy the H1 score floor but BB-fair distribution
puts meaningful mass on the wrong side of strike.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from statistics import NormalDist

import pytest

from kalshi_engine.core.types import Action, Side
from kalshi_engine.strategies.favorite_chase.models.phase4_cutpoints import (
    ALIGN_MODES, Phase4CutpointsModel,
    DNORM_GATE_LOW, DNORM_GATE_HIGH, DNORM_GATE_SKIP_BELOW,
    DNORM_GATE_SIZE_AT_4, DNORM_GATE_SIZE_AT_5, DNORM_GATE_SIZE_AT_6,
)


class _StateStub:
    def __init__(self, spot: float, vol: float, bb_div_val: float,
                  vol_pct: float = 0.30):
        self.crypto = "BTC"
        self._spot = spot
        self._vol = vol
        self._bb_div = bb_div_val
        self._vol_pct = vol_pct
    def latest_spot(self): return self._spot
    def vol_30m(self): return self._vol
    def vol_30m_percentile(self, v): return self._vol_pct
    def bb_fair(self, spot, strike, sigma, tau):
        if sigma <= 0 or tau <= 0:
            return 1.0 if spot >= strike else 0.0
        z = math.log(spot / strike) / (sigma * math.sqrt(tau))
        return NormalDist().cdf(z)
    def bb_div(self, fav_mid, bb_fair):
        return self._bb_div


# Fixed test fixture: BTC spot=100_000, vol=5.0 bps/min, tau=30 min.
# Then vol*sqrt(tau) = 5.0 * sqrt(30) = 27.386...
# bps_margin needed for a target d_norm: d_norm * 27.386.
# bps_margin = |spot - strike| / spot * 1e4 (in bps).
# Strike = spot * (1 - bps_margin/1e4) for strike < spot (NO-side favorite).
SPOT = 100_000.0
VOL = 5.0
TAU_MIN = 30.0
_DENOM = VOL * math.sqrt(TAU_MIN)


def _strike_for_dnorm(target_dnorm: float) -> float:
    """Return a strike (below spot) that produces the desired d_norm."""
    bps = target_dnorm * _DENOM
    return SPOT * (1.0 - bps / 1e4)


def _eval(side: Side, bb_div_val: float, strike: float,
           fav_mid_dc: float = 200.0, vol_pct: float = 0.30):
    model = Phase4CutpointsModel(align_mode="5tier_v13b_btc_dnorm_gate")
    ts = int(datetime(2026, 5, 26, 20, 0, 0,
                       tzinfo=timezone.utc).timestamp() * 1000)
    state = _StateStub(spot=SPOT, vol=VOL, bb_div_val=bb_div_val,
                        vol_pct=vol_pct)
    return model.evaluate(
        state=state, ticker="KXBTCD-T", side=side,
        favorite_mid_decicents=fav_mid_dc, strike=strike,
        now_ms=ts, close_ms=ts + int(TAU_MIN * 60_000),
    )


# ---- registration + constants -------------------------------------------

def test_dnorm_gate_registered_in_align_modes():
    assert "5tier_v13b_btc_dnorm_gate" in ALIGN_MODES


def test_dnorm_gate_constants():
    assert DNORM_GATE_LOW == 1.5
    assert DNORM_GATE_HIGH == 2.0
    assert DNORM_GATE_SKIP_BELOW == 4.0
    assert DNORM_GATE_SIZE_AT_4 == 7
    assert DNORM_GATE_SIZE_AT_5 == 9
    assert DNORM_GATE_SIZE_AT_6 == 10


# ---- d_norm danger zone (the new gate) ----------------------------------

def test_dnorm_1p0_enters_below_danger_zone():
    """d_norm=1.0 (below [1.5, 2.0]) + score=4.0 -> ENTER 7ct."""
    strike = _strike_for_dnorm(1.0)
    # score=4: YES + bb_div=-0.05 (div_band=1, super_band=0) + bps_strong=1
    d = _eval(side=Side.YES, bb_div_val=-0.05, strike=strike, fav_mid_dc=200.0)
    assert d.action is Action.ENTER, d.reason
    assert d.size == DNORM_GATE_SIZE_AT_4
    # Confirm d_norm is what we engineered (allow modest float slack).
    assert abs(d.diagnostics["d_norm"] - 1.0) < 0.01


def test_dnorm_1p7_skips_in_danger_zone():
    """d_norm=1.7 (inside [1.5, 2.0]) + score=4.0 -> SKIP danger zone."""
    strike = _strike_for_dnorm(1.7)
    d = _eval(side=Side.YES, bb_div_val=-0.05, strike=strike, fav_mid_dc=200.0)
    assert d.action is Action.SKIP
    assert "danger-zone" in d.reason
    assert "d_norm=" in d.reason
    assert abs(d.diagnostics["d_norm"] - 1.7) < 0.01


def test_dnorm_2p5_enters_above_danger_zone():
    """d_norm=2.5 (above [1.5, 2.0]) + score=4.0 -> ENTER 7ct."""
    strike = _strike_for_dnorm(2.5)
    d = _eval(side=Side.YES, bb_div_val=-0.05, strike=strike, fav_mid_dc=200.0)
    assert d.action is Action.ENTER
    assert d.size == DNORM_GATE_SIZE_AT_4
    assert abs(d.diagnostics["d_norm"] - 2.5) < 0.01


def test_dnorm_just_inside_low_edge_skips():
    """d_norm=1.51 (just above 1.5) -> SKIP. Tests low-edge inclusivity
    without float-precision boundary noise."""
    strike = _strike_for_dnorm(1.51)
    d = _eval(side=Side.YES, bb_div_val=-0.05, strike=strike, fav_mid_dc=200.0)
    assert d.action is Action.SKIP
    assert "danger-zone" in d.reason


def test_dnorm_just_inside_high_edge_skips():
    """d_norm=1.99 (just below 2.0) -> SKIP. Tests high-edge inclusivity
    without float-precision boundary noise."""
    strike = _strike_for_dnorm(1.99)
    d = _eval(side=Side.YES, bb_div_val=-0.05, strike=strike, fav_mid_dc=200.0)
    assert d.action is Action.SKIP
    assert "danger-zone" in d.reason


# ---- score floor (H1) ---------------------------------------------------

def test_score_below_4_skips():
    """Score=3.5 (NO + bps_strong, no div_band) below H1 floor -> SKIP
    (BEFORE the d_norm gate is even checked)."""
    # bb_div=+0.05 makes div_band=0 and super_band=0; NO + bps_strong=1 -> 3.5
    strike = _strike_for_dnorm(1.0)  # well outside danger zone
    d = _eval(side=Side.NO, bb_div_val=+0.05, strike=strike, fav_mid_dc=200.0)
    assert d.action is Action.SKIP
    # The reason should be the score floor, NOT the danger-zone.
    assert "danger-zone" not in d.reason
    assert "score=" in d.reason


# ---- sizing tiers (assumes d_norm outside the danger zone) --------------

def test_score_4p0_yields_7ct():
    """YES + bb_div=-0.05 (div_band=1) + bps_strong=1 -> score=4.0 -> 7ct."""
    strike = _strike_for_dnorm(1.0)
    d = _eval(side=Side.YES, bb_div_val=-0.05, strike=strike, fav_mid_dc=200.0)
    assert d.action is Action.ENTER
    assert d.size == 7
    assert d.diagnostics["score_5tier_v13b_btc_dnorm_gate"] == 4.0


def test_score_5p0_yields_9ct():
    """YES + bb_div=-0.10 (div_band=1, super_band=1) + bps_strong=1 -> 5.0 -> 9ct."""
    strike = _strike_for_dnorm(1.0)
    d = _eval(side=Side.YES, bb_div_val=-0.10, strike=strike, fav_mid_dc=200.0)
    assert d.action is Action.ENTER
    assert d.size == 9
    assert d.diagnostics["score_5tier_v13b_btc_dnorm_gate"] == 5.0


def test_score_6p5_yields_10ct():
    """NO + bb_div=-0.10 + bps_strong=1 -> 6.5 -> 10ct (max tier)."""
    strike = _strike_for_dnorm(2.5)  # outside danger zone
    d = _eval(side=Side.NO, bb_div_val=-0.10, strike=strike, fav_mid_dc=200.0)
    assert d.action is Action.ENTER
    assert d.size == 10
    assert d.diagnostics["score_5tier_v13b_btc_dnorm_gate"] == 6.5


# ---- s_bps hard gate (inherited) ----------------------------------------

def test_skip_when_s_bps_zero():
    """bps_margin between threshold (3.95) and 1.5*threshold (5.92) clears
    the hard bps gate but trips s_bps=0 inside the dnorm_gate branch."""
    # bps_margin = 5.0 -> strike = 100000 * (1 - 5.0/1e4) = 99_950
    d = _eval(side=Side.NO, bb_div_val=-0.10, strike=99_950.0, fav_mid_dc=200.0)
    assert d.action is Action.SKIP
    assert "s_bps=0" in d.reason


# ---- diagnostics + reason ----------------------------------------------

def test_diagnostics_include_score_and_dnorm():
    strike = _strike_for_dnorm(1.0)
    d = _eval(side=Side.YES, bb_div_val=-0.05, strike=strike, fav_mid_dc=200.0)
    for k in ("score_5tier_v13b_btc_dnorm_gate", "bb_div_band", "side_no",
              "bps_strong", "super_band", "align_mode", "d_norm",
              "dnorm_gate_low", "dnorm_gate_high"):
        assert k in d.diagnostics, f"missing diagnostic key {k!r}"
    assert d.diagnostics["align_mode"] == "5tier_v13b_btc_dnorm_gate"
    assert d.diagnostics["dnorm_gate_low"] == 1.5
    assert d.diagnostics["dnorm_gate_high"] == 2.0


def test_reason_format():
    strike = _strike_for_dnorm(2.5)
    d = _eval(side=Side.NO, bb_div_val=-0.10, strike=strike, fav_mid_dc=200.0)
    assert d.reason.startswith("5TIER_V13B_BTC_DNORM_GATE score=")
    assert "d_norm=" in d.reason
    assert "-> 10ct" in d.reason


# ---- per-crypto: same model, different align_mode for ETH ---------------

def test_btc_uses_dnorm_gate_eth_uses_ramp_independently():
    """Sanity: two model instances can hold different align_modes; the
    per_crypto_models dict pattern in HourglassTraderStrategy uses this
    by keying lookups on crypto. Just confirm both modes evaluate
    consistently when instantiated separately."""
    btc_model = Phase4CutpointsModel(align_mode="5tier_v13b_btc_dnorm_gate")
    eth_model = Phase4CutpointsModel(align_mode="5tier_v13b_1to3_ramp")
    assert btc_model.align_mode == "5tier_v13b_btc_dnorm_gate"
    assert eth_model.align_mode == "5tier_v13b_1to3_ramp"
    ts = int(datetime(2026, 5, 26, 20, 0, 0,
                       tzinfo=timezone.utc).timestamp() * 1000)
    strike = _strike_for_dnorm(1.0)
    state = _StateStub(spot=SPOT, vol=VOL, bb_div_val=-0.05)
    d_btc = btc_model.evaluate(
        state=state, ticker="KXBTCD-T", side=Side.YES,
        favorite_mid_decicents=200.0, strike=strike,
        now_ms=ts, close_ms=ts + int(TAU_MIN * 60_000))
    d_eth = eth_model.evaluate(
        state=state, ticker="KXBTCD-T", side=Side.YES,
        favorite_mid_decicents=200.0, strike=strike,
        now_ms=ts, close_ms=ts + int(TAU_MIN * 60_000))
    # Same score (4.0) but different sizing per align_mode.
    assert d_btc.action is Action.ENTER
    assert d_btc.size == 7  # dnorm_gate tier-4
    assert d_eth.action is Action.ENTER
    assert d_eth.size == 1  # 1to3_ramp tier-1
