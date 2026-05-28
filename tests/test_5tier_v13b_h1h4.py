"""Phase 12.13: 5tier_v13b_h1h4 sizing mode tests.

Same V13b score formula as ``5tier_v13b``. SKIPs everything below score=4.0
(H1's score-floor — every cohort loss to date sat at score 3.5; score >= 4
was 58/58 wins). On what passes, sizes by H4's smooth score multiplier:

    score < 4.0        -> SKIP
    size = min(12, round(score * 1.8))
    # score 4.0 -> 7 ct
    # score 4.5 -> 8 ct
    # score 5.0 -> 9 ct
    # score 5.5 -> 10 ct (round(9.9), below the 12 cap)
    # score 6.5 -> 12 ct (round(11.7)=12, at the cap)

Tests cover each achievable V13b score (>= 4) -> expected size, plus the
score-floor SKIP at 3.5, the s_bps hard gate, diagnostics, reason format,
backward-compat for the existing modes.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from kalshi_engine.core.types import Action, Side
from kalshi_engine.strategies.favorite_chase.models.phase4_cutpoints import (
    ALIGN_MODES,
    H1H4_SCORE_MULT,
    H1H4_SKIP_BELOW,
    Phase4CutpointsModel,
    S2_SIZE_AT_6,
)


class _FakeState:
    """Bypasses spot/vol machinery so tests can drive bb_div directly."""

    def __init__(self, bb_div_val: float):
        self.crypto = "BTC"
        self._bb_div = bb_div_val

    def latest_spot(self):
        return 100_000.0

    def vol_30m(self):
        return 5.0

    def vol_30m_percentile(self, v):
        return 0.30

    def bb_fair(self, spot, strike, sigma, tau):
        return 0.5

    def bb_div(self, fav_mid, bb_fair):
        return self._bb_div


def _eval_h1h4(side: Side, bb_div_val: float, strike: float,
               fav_mid_dc: float = 200.0):
    model = Phase4CutpointsModel(align_mode="5tier_v13b_h1h4")
    ts = int(datetime(2026, 5, 24, 20, 0, 0,
                      tzinfo=timezone.utc).timestamp() * 1000)
    return model.evaluate(
        state=_FakeState(bb_div_val), ticker="KXBTC15M-T", side=side,
        favorite_mid_decicents=fav_mid_dc, strike=strike,
        now_ms=ts, close_ms=ts + 420_000,
    )


# ---- mode registration ----------------------------------------------------

def test_5tier_v13b_h1h4_in_align_modes():
    assert "5tier_v13b_h1h4" in ALIGN_MODES


def test_5tier_v13b_h1h4_mode_accepted():
    m = Phase4CutpointsModel(align_mode="5tier_v13b_h1h4")
    assert m.align_mode == "5tier_v13b_h1h4"


def test_5tier_v13b_h1h4_constants():
    """Sanity: the tier constants are the documented values."""
    assert H1H4_SKIP_BELOW == 4.0
    assert H1H4_SCORE_MULT == 1.8


# ---- per-tier size table --------------------------------------------------
# Achievable scores in the V13b cohort: 0, 1.5, 2.0, 3.0, 3.5, 4.0, 4.5,
# 5.0, 5.5, 6.5. H1H4 SKIPs scores < 4.0. For the rest:
#     size = min(10, round(score * 1.8))
#     4.0 -> 7, 4.5 -> 8, 5.0 -> 9, 5.5 -> 10, 6.5 -> 10

def test_h1h4_score_4p0_yields_7ct():
    """score=4.0 (div_band=1 + bps_strong=1, YES, no super_band) -> 7 ct."""
    d = _eval_h1h4(side=Side.YES, bb_div_val=-0.05, strike=90_000.0,
                   fav_mid_dc=800.0)
    assert d.action is Action.ENTER
    assert d.diagnostics["score_5tier_v13b_h1h4"] == 4.0
    assert d.size == 7


def test_h1h4_score_4p5_yields_8ct():
    """score=4.5 (div_band=1 + side_no=1 + super_band=1, no bps_strong) -> 8 ct.

    strike=99_935 -> bps_margin=6.5 bps; passes 1.5*threshold gate but doesn't
    trigger bps_strong (needs 2*threshold).
    """
    d = _eval_h1h4(side=Side.NO, bb_div_val=-0.10, strike=99_935.0,
                   fav_mid_dc=200.0)
    assert d.action is Action.ENTER
    assert d.diagnostics["score_5tier_v13b_h1h4"] == 4.5
    assert d.size == 8


def test_h1h4_score_5p0_yields_9ct():
    """score=5.0 (div_band=1 + bps_strong=1 + super_band=1, YES) -> 9 ct."""
    d = _eval_h1h4(side=Side.YES, bb_div_val=-0.10, strike=90_000.0,
                   fav_mid_dc=800.0)
    assert d.action is Action.ENTER
    assert d.diagnostics["score_5tier_v13b_h1h4"] == 5.0
    assert d.size == 9


def test_h1h4_score_5p5_yields_10ct():
    """score=5.5 (div_band=1 + side_no=1 + bps_strong=1, no super_band)
    -> round(9.9)=10 -> 10 ct (below the S2_SIZE_AT_6=12 cap)."""
    d = _eval_h1h4(side=Side.NO, bb_div_val=-0.05, strike=90_000.0,
                   fav_mid_dc=200.0)
    assert d.action is Action.ENTER
    assert d.diagnostics["score_5tier_v13b_h1h4"] == 5.5
    assert d.size == 10


def test_h1h4_score_6p5_yields_12ct():
    """score=6.5 (all four components, NO side) -> round(11.7)=12, cap=12 (not reduced)."""
    d = _eval_h1h4(side=Side.NO, bb_div_val=-0.10, strike=90_000.0,
                   fav_mid_dc=200.0)
    assert d.action is Action.ENTER
    assert d.diagnostics["score_5tier_v13b_h1h4"] == 6.5
    assert d.size == S2_SIZE_AT_6
    assert d.size == 12


# ---- SKIP cases (score < 4.0) --------------------------------------------

def test_h1h4_score_3p5_is_skip():
    """score=3.5 — every cohort loss sits here. H1H4 SKIPs."""
    d = _eval_h1h4(side=Side.NO, bb_div_val=-0.05, strike=99_935.0,
                   fav_mid_dc=200.0)
    assert d.action is Action.SKIP
    assert d.diagnostics["score_5tier_v13b_h1h4"] == 3.5
    assert "5TIER_V13B_H1H4 skip" in d.reason
    assert "score=3.5" in d.reason


def test_h1h4_score_3p0_is_skip():
    """score=3.0 -> SKIP (below the 4.0 floor)."""
    d = _eval_h1h4(side=Side.YES, bb_div_val=-0.10, strike=99_935.0,
                   fav_mid_dc=800.0)
    assert d.action is Action.SKIP
    assert d.diagnostics["score_5tier_v13b_h1h4"] == 3.0


def test_h1h4_score_2p0_is_skip():
    """score=2.0 (bps_strong only, YES, no div_band) -> SKIP."""
    d = _eval_h1h4(side=Side.YES, bb_div_val=+0.05, strike=90_000.0,
                   fav_mid_dc=800.0)
    assert d.action is Action.SKIP
    assert d.diagnostics["score_5tier_v13b_h1h4"] == 2.0


def test_h1h4_skip_on_s_bps_zero():
    """Hard gate: bps_margin <= 1.5*threshold -> SKIP regardless of score."""
    d = _eval_h1h4(side=Side.NO, bb_div_val=-0.10, strike=99_995.0,
                   fav_mid_dc=200.0)
    assert d.action is Action.SKIP


# ---- diagnostics / reason -------------------------------------------------

def test_h1h4_diagnostics_include_score_field():
    """Decisions in H1H4 mode include score_5tier_v13b_h1h4 + V13b component
    flags so log-readers can reconstruct sizing decisions."""
    d = _eval_h1h4(side=Side.NO, bb_div_val=-0.10, strike=90_000.0,
                   fav_mid_dc=200.0)
    for k in ("score_5tier_v13b_h1h4", "bb_div_band", "side_no",
              "side_yes", "bps_strong", "super_band", "align_mode"):
        assert k in d.diagnostics
    assert d.diagnostics["align_mode"] == "5tier_v13b_h1h4"


def test_h1h4_reason_format():
    """Reason string follows the documented prefix + score=X.X -> Yct format."""
    d = _eval_h1h4(side=Side.NO, bb_div_val=-0.10, strike=90_000.0,
                   fav_mid_dc=200.0)
    assert d.reason.startswith("5TIER_V13B_H1H4 score=")
    assert "-> 12ct" in d.reason


# ---- regression: existing modes unchanged --------------------------------

def test_5tier_v13b_s2_still_works_after_h1h4():
    """Sanity: ``5tier_v13b_s2`` still gives 3ct at score 3-4 even with H1H4
    added — backward compat."""
    model = Phase4CutpointsModel(align_mode="5tier_v13b_s2")
    ts = int(datetime(2026, 5, 24, 20, 0, 0,
                      tzinfo=timezone.utc).timestamp() * 1000)
    d = model.evaluate(
        state=_FakeState(-0.10), ticker="KXBTC15M-T", side=Side.YES,
        favorite_mid_decicents=800.0, strike=99_935.0,
        now_ms=ts, close_ms=ts + 420_000,
    )
    assert d.action is Action.ENTER
    assert d.diagnostics["score_5tier_v13b_s2"] == 3.0
    assert d.size == 3


def test_5tier_v13b_still_caps_at_5ct_after_h1h4():
    """Sanity: ``5tier_v13b`` still uses SIZE_CAP_5TIER=5 unchanged."""
    model = Phase4CutpointsModel(align_mode="5tier_v13b")
    ts = int(datetime(2026, 5, 24, 20, 0, 0,
                      tzinfo=timezone.utc).timestamp() * 1000)
    d = model.evaluate(
        state=_FakeState(-0.10), ticker="KXBTC15M-T", side=Side.NO,
        favorite_mid_decicents=200.0, strike=90_000.0,
        now_ms=ts, close_ms=ts + 420_000,
    )
    assert d.action is Action.ENTER
    assert d.diagnostics["score_5tier_v13b"] == 6.5
    assert d.size == 5


def test_invalid_align_mode_rejected():
    """Regression: unknown align_mode still rejected."""
    with pytest.raises(ValueError, match="align_mode"):
        Phase4CutpointsModel(align_mode="5tier_v13b_h1h4_v2")
