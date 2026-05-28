"""Phase 14.16: 5tier_v13b_commodity_1ct_flat scoring + sizing tests.

Same V13b score formula + hard gates as 5tier_v13b; SKIPs score<4 (H1 floor)
and sizes every passing trade at a flat 1 contract. Mirrors the equity
1ct-flat mode but the bps gate genuinely discriminates because the commodity
cutpoints carry real per-product bps_thresholds.

    score = 2*bb_div_band + 1.5*side_no + 2*bps_strong + 1*super_band
    score < 4.0  -> SKIP ;  score >= 4.0 -> ENTER 1ct
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from kalshi_engine.core.types import Action, Side
from kalshi_engine.strategies.favorite_chase.models.phase4_cutpoints import (
    ALIGN_MODES,
    COMMODITY_1CT_FLAT_SIZE,
    COMMODITY_1CT_FLAT_SKIP_BELOW,
    Phase4CutpointsModel,
)

_MODE = "5tier_v13b_commodity_1ct_flat"


class _FakeState:
    """Drives bb_div directly; spot/strike feed the real bps computation."""

    def __init__(self, bb_div_val: float, crypto: str = "BTC",
                 spot: float = 100_000.0):
        self.crypto = crypto
        self._bb_div = bb_div_val
        self._spot = spot

    def latest_spot(self):
        return self._spot

    def vol_30m(self):
        return 5.0

    def vol_30m_percentile(self, v):
        return 0.30  # below the 0.80 skip gate

    def bb_fair(self, spot, strike, sigma, tau):
        return 0.5

    def bb_div(self, fav_mid, bb_fair):
        return self._bb_div


def _eval(side, bb_div_val, strike, *, crypto="BTC", spot=100_000.0,
          fav_mid_dc=800.0, model=None):
    model = model or Phase4CutpointsModel(align_mode=_MODE, time_of_day_skip=False)
    ts = int(datetime(2026, 5, 28, 20, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
    return model.evaluate(
        state=_FakeState(bb_div_val, crypto=crypto, spot=spot),
        ticker="KXGOLDD-T", side=side, favorite_mid_decicents=fav_mid_dc,
        strike=strike, now_ms=ts, close_ms=ts + 30 * 60_000)


# ---- mode registration ----------------------------------------------------

def test_mode_in_align_modes():
    assert _MODE in ALIGN_MODES


def test_mode_accepted():
    assert Phase4CutpointsModel(align_mode=_MODE).align_mode == _MODE


def test_constants():
    assert COMMODITY_1CT_FLAT_SKIP_BELOW == 4.0
    assert COMMODITY_1CT_FLAT_SIZE == 1


# ---- sizing (flat 1ct above the score>=4 floor) --------------------------
# BTC threshold (default v3 cutpoints) = 3.95 -> bps_strong needs margin > 7.9.
# spot=100k, strike=90k -> 1000 bps (strong); strike=99_935 -> 6.5 bps
# (passes s_bps 1.5x=5.92 gate but bps_strong 2x=7.9 = 0).

def test_score_4p0_enters_1ct():
    d = _eval(Side.YES, -0.05, 90_000.0)  # div_band(2)+bps_strong(2)=4.0
    assert d.action is Action.ENTER
    assert d.diagnostics["score_5tier_v13b_commodity_1ct_flat"] == 4.0
    assert d.size == 1


def test_score_6p5_still_1ct():
    d = _eval(Side.NO, -0.10, 90_000.0)  # all four components = 6.5
    assert d.action is Action.ENTER
    assert d.diagnostics["score_5tier_v13b_commodity_1ct_flat"] == 6.5
    assert d.size == 1  # flat regardless of score


def test_score_3p5_skips():
    d = _eval(Side.NO, -0.05, 99_935.0)  # div_band(2)+side_no(1.5)=3.5 < 4
    assert d.action is Action.SKIP
    assert d.diagnostics["score_5tier_v13b_commodity_1ct_flat"] == 3.5
    assert "5TIER_V13B_COMMODITY_1CT_FLAT skip" in d.reason


def test_s_bps_zero_hard_gate_skips():
    d = _eval(Side.NO, -0.10, 99_995.0)  # ~0.5 bps, below threshold
    assert d.action is Action.SKIP


# ---- diagnostics / reason -------------------------------------------------

def test_diagnostics_present():
    d = _eval(Side.NO, -0.10, 90_000.0)
    for k in ("score_5tier_v13b_commodity_1ct_flat", "bb_div_band", "side_no",
              "bps_strong", "super_band", "align_mode"):
        assert k in d.diagnostics
    assert d.diagnostics["align_mode"] == _MODE


def test_reason_format():
    d = _eval(Side.NO, -0.10, 90_000.0)
    assert d.reason.startswith("5TIER_V13B_COMMODITY_1CT_FLAT score=")
    assert "-> 1ct" in d.reason


# ---- commodity bps_threshold genuinely discriminates ----------------------
# Unlike the SPY/SPX equity defect, Pyth is the exact settlement source, so a
# GOLD-keyed state with a real GOLD threshold gates on true distance-to-strike.

def _gold_model(tmp_path):
    cp = {
        "version": "test_commodity",
        "vol_30m_percentile_skip_above": 0.80,
        "vol_30m_percentile_upsize_below": 0.50,
        "bb_div_skip_above": 0.09,
        "bb_div_upsize_below": -0.03,
        "bps_thresholds": {"GOLD": 7.0},
    }
    p = tmp_path / "cutpoints.json"
    p.write_text(json.dumps(cp), encoding="utf-8")
    return Phase4CutpointsModel(cutpoints_path=str(p), align_mode=_MODE,
                                time_of_day_skip=False)


def test_gold_threshold_passes_distant_strike(tmp_path):
    m = _gold_model(tmp_path)
    # spot 4500, strike 4470 -> ~66.7 bps > 2*7 -> bps_strong=1, div_band=1 -> 4.0
    d = _eval(Side.YES, -0.05, 4470.0, crypto="GOLD", spot=4500.0,
              fav_mid_dc=800.0, model=m)
    assert d.action is Action.ENTER
    assert d.size == 1


def test_gold_threshold_rejects_near_strike(tmp_path):
    m = _gold_model(tmp_path)
    # spot 4500, strike 4499 -> ~2.2 bps < threshold 7 -> hard-gate SKIP
    d = _eval(Side.YES, -0.05, 4499.0, crypto="GOLD", spot=4500.0,
              fav_mid_dc=800.0, model=m)
    assert d.action is Action.SKIP
