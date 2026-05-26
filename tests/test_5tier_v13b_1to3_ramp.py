"""Tests for 5tier_v13b_1to3_ramp align mode (Phase 14.3).

Score < 4    → SKIP
4 ≤ score < 5 → 1ct
5 ≤ score < 6 → 2ct
score >= 6   → 3ct

Hard gates (vol/bb_div/bps) identical to other V13b modes.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from kalshi_engine.core.types import Action, Side
from kalshi_engine.strategies.favorite_chase.models.phase4_cutpoints import (
    ALIGN_MODES, Phase4CutpointsModel,
    RAMP_SIZE_TIER1, RAMP_SIZE_TIER2, RAMP_SIZE_TIER3, RAMP_SKIP_BELOW,
)


class _FakeState:
    def __init__(self, bb_div_val: float, vol_pct: float = 0.30):
        self.crypto = "BTC"
        self._bb_div = bb_div_val
        self._vol_pct = vol_pct
    def latest_spot(self): return 100_000.0
    def vol_30m(self): return 5.0
    def vol_30m_percentile(self, v): return self._vol_pct
    def bb_fair(self, spot, strike, sigma, tau): return 0.5
    def bb_div(self, fav_mid, bb_fair): return self._bb_div


def _eval(side, bb_div_val, strike, fav_mid_dc=200.0, vol_pct=0.30):
    model = Phase4CutpointsModel(align_mode="5tier_v13b_1to3_ramp")
    ts = int(datetime(2026, 5, 26, 20, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
    return model.evaluate(
        state=_FakeState(bb_div_val, vol_pct=vol_pct),
        ticker="KXBTC15M-T", side=side,
        favorite_mid_decicents=fav_mid_dc, strike=strike,
        now_ms=ts, close_ms=ts + 420_000,
    )


def test_ramp_registered_in_align_modes():
    assert "5tier_v13b_1to3_ramp" in ALIGN_MODES


def test_ramp_constants():
    assert RAMP_SKIP_BELOW == 4.0
    assert RAMP_SIZE_TIER1 == 1
    assert RAMP_SIZE_TIER2 == 2
    assert RAMP_SIZE_TIER3 == 3


def test_ramp_score_4p0_yields_1ct():
    """score=4.0 (div_band=1 + bps_strong=1, YES) -> 1ct (tier1)."""
    d = _eval(side=Side.YES, bb_div_val=-0.05, strike=90_000.0, fav_mid_dc=800.0)
    assert d.action is Action.ENTER
    assert d.size == 1
    assert d.diagnostics["score_5tier_v13b_1to3_ramp"] == 4.0


def test_ramp_score_4p5_yields_1ct():
    """score=4.5 (div_band=1 + side_no + super_band) -> still 1ct (< 5.0)."""
    d = _eval(side=Side.NO, bb_div_val=-0.10, strike=99_935.0, fav_mid_dc=200.0)
    assert d.action is Action.ENTER
    assert d.size == 1
    assert d.diagnostics["score_5tier_v13b_1to3_ramp"] == 4.5


def test_ramp_score_5p0_yields_2ct():
    """score=5.0 (div_band=1 + bps_strong=1 + super_band=1, YES) -> 2ct."""
    d = _eval(side=Side.YES, bb_div_val=-0.10, strike=90_000.0, fav_mid_dc=800.0)
    assert d.action is Action.ENTER
    assert d.size == 2
    assert d.diagnostics["score_5tier_v13b_1to3_ramp"] == 5.0


def test_ramp_score_5p5_yields_2ct():
    """score=5.5 -> still 2ct (< 6.0)."""
    d = _eval(side=Side.NO, bb_div_val=-0.05, strike=90_000.0, fav_mid_dc=200.0)
    assert d.action is Action.ENTER
    assert d.size == 2
    assert d.diagnostics["score_5tier_v13b_1to3_ramp"] == 5.5


def test_ramp_score_6p5_yields_3ct():
    """score=6.5 (all four components, NO) -> 3ct (tier3 ceiling)."""
    d = _eval(side=Side.NO, bb_div_val=-0.10, strike=90_000.0, fav_mid_dc=200.0)
    assert d.action is Action.ENTER
    assert d.size == 3
    assert d.diagnostics["score_5tier_v13b_1to3_ramp"] == 6.5


def test_ramp_score_3p5_is_skip():
    """score=3.5 (cohort losing tier) -> SKIP below the floor."""
    d = _eval(side=Side.NO, bb_div_val=-0.05, strike=99_935.0, fav_mid_dc=200.0)
    assert d.action is Action.SKIP
    assert "1TO3_RAMP skip" in d.reason
    assert "score=3.5" in d.reason


def test_ramp_score_3p0_is_skip():
    """score=3.0 → SKIP below the 4.0 floor."""
    d = _eval(side=Side.YES, bb_div_val=-0.10, strike=99_935.0, fav_mid_dc=800.0)
    assert d.action is Action.SKIP


def test_ramp_skip_on_s_bps_zero():
    """bps_margin too tight → s_bps=0 → SKIP regardless of score."""
    d = _eval(side=Side.NO, bb_div_val=-0.10, strike=99_995.0, fav_mid_dc=200.0)
    assert d.action is Action.SKIP


def test_ramp_diagnostics_include_score():
    d = _eval(side=Side.NO, bb_div_val=-0.10, strike=90_000.0, fav_mid_dc=200.0)
    for k in ("score_5tier_v13b_1to3_ramp", "bb_div_band", "side_no",
              "bps_strong", "super_band", "align_mode"):
        assert k in d.diagnostics
    assert d.diagnostics["align_mode"] == "5tier_v13b_1to3_ramp"


def test_ramp_reason_format():
    d = _eval(side=Side.NO, bb_div_val=-0.10, strike=90_000.0, fav_mid_dc=200.0)
    assert d.reason.startswith("5TIER_V13B_1TO3_RAMP score=")
    assert "-> 3ct" in d.reason
