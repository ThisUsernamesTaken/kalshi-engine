"""Phase 12.12: 5tier_v13b_s2 sizing mode tests.

Same V13b score formula as ``5tier_v13b`` but with a steeper conviction-tiered
size mapping that uses ``--max-contracts 10`` headroom on high-conviction
trades and SKIPs low-conviction noise:

    score < 3.0  -> SKIP
    3.0 <= score < 4.0 -> 3 ct
    4.0 <= score < 5.0 -> 5 ct
    5.0 <= score < 6.0 -> 8 ct
    score >= 6.0       -> 10 ct

Tests cover each tier boundary (using achievable V13b scores) plus the
hard-gate / diagnostics / mode-listing invariants.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from kalshi_engine.core.types import Action, Side
from kalshi_engine.strategies.favorite_chase.models.phase4_cutpoints import (
    ALIGN_MODES,
    Phase4CutpointsModel,
    S2_SIZE_AT_4,
    S2_SIZE_AT_5,
    S2_SIZE_AT_6,
    S2_SKIP_BELOW,
)


class _FakeState:
    """Bypasses spot/vol machinery so tests can drive bb_div directly.

    Mirrors the pattern used by the existing 5tier_v13b tests.
    """

    def __init__(self, bb_div_val: float):
        self.crypto = "BTC"
        self._bb_div = bb_div_val

    def latest_spot(self):
        return 100_000.0

    def vol_30m(self):
        return 5.0

    def vol_30m_percentile(self, v):
        return 0.30  # well below the 0.67 skip gate

    def bb_fair(self, spot, strike, sigma, tau):
        return 0.5

    def bb_div(self, fav_mid, bb_fair):
        return self._bb_div


def _eval_s2(side: Side, bb_div_val: float, strike: float,
             fav_mid_dc: float = 200.0):
    """Run a single S2 evaluation with bb_div + strike controlled directly."""
    model = Phase4CutpointsModel(align_mode="5tier_v13b_s2")
    ts = int(datetime(2026, 5, 24, 20, 0, 0,
                      tzinfo=timezone.utc).timestamp() * 1000)
    return model.evaluate(
        state=_FakeState(bb_div_val), ticker="KXBTC15M-T", side=side,
        favorite_mid_decicents=fav_mid_dc, strike=strike,
        now_ms=ts, close_ms=ts + 420_000,
    )


# ---- mode registration ----------------------------------------------------

def test_5tier_v13b_s2_in_align_modes():
    assert "5tier_v13b_s2" in ALIGN_MODES


def test_5tier_v13b_s2_mode_accepted():
    m = Phase4CutpointsModel(align_mode="5tier_v13b_s2")
    assert m.align_mode == "5tier_v13b_s2"


def test_5tier_v13b_s2_constants():
    """Sanity: the tier-size constants are the documented values."""
    assert S2_SKIP_BELOW == 3.0
    assert S2_SIZE_AT_4 == 5
    assert S2_SIZE_AT_5 == 8
    assert S2_SIZE_AT_6 == 10


# ---- tier-boundary table --------------------------------------------------
# Achievable V13b scores: 0, 1.5, 2.0, 3.0, 3.5, 4.0, 4.5, 5.0, 5.5, 6.5
#
# For each, we pick (side, bb_div, strike, fav_mid_dc) so the score components
# (bb_div_band, side_no, bps_strong, super_band) sum to the target.
#
# Score = 2*bb_div_band + 1.5*side_no + 2*bps_strong + 1*super_band
# Where:
#   bb_div_band = 1 iff -0.20 < bb_div <= 0
#   super_band  = 1 iff -0.14 < bb_div <= -0.09   (implies bb_div_band=1)
#   bps_strong  = 1 iff bps_margin > 2 * crypto_threshold (BTC threshold=3.95)
#   side_no     = 1 iff side is NO

# BTC threshold ≈ 3.95 → bps_strong needs margin > 7.9.
# spot=100_000, strike=90_000 → bps = (100k-90k)/100k * 10000 = 1000 bps. Strong.
# strike=99_950 → bps = 5 bps. Passes hard gate (3.95) but bps_strong=0.

def test_s2_score_3p0_yields_3ct():
    """score=3.0 (div_band=1 + super_band=1, no bps_strong, YES side)
    -> tier [3.0, 4.0) -> 3 ct.

    strike=99_935 -> bps_margin=6.5 bps, in (1.5*3.95, 2*3.95] so s_bps=1
    but bps_strong=0.
    """
    d = _eval_s2(side=Side.YES, bb_div_val=-0.10, strike=99_935.0,
                 fav_mid_dc=800.0)
    assert d.action is Action.ENTER
    assert d.diagnostics["score_5tier_v13b_s2"] == 3.0
    assert d.size == 3


def test_s2_score_3p5_yields_3ct():
    """score=3.5 (div_band=1 + side_no=1) -> tier [3.0, 4.0) -> 3 ct."""
    d = _eval_s2(side=Side.NO, bb_div_val=-0.05, strike=99_935.0,
                 fav_mid_dc=200.0)
    assert d.action is Action.ENTER
    assert d.diagnostics["score_5tier_v13b_s2"] == 3.5
    assert d.size == 3


def test_s2_score_4p0_yields_5ct():
    """score=4.0 (div_band=1 + bps_strong=1) -> tier [4.0, 5.0) -> 5 ct."""
    d = _eval_s2(side=Side.YES, bb_div_val=-0.05, strike=90_000.0,
                 fav_mid_dc=800.0)
    assert d.action is Action.ENTER
    assert d.diagnostics["score_5tier_v13b_s2"] == 4.0
    assert d.size == S2_SIZE_AT_4
    assert d.size == 5


def test_s2_score_4p5_yields_5ct():
    """score=4.5 (div_band=1 + side_no=1 + super_band=1, no bps_strong)
    -> tier [4.0, 5.0) -> 5 ct."""
    d = _eval_s2(side=Side.NO, bb_div_val=-0.10, strike=99_935.0,
                 fav_mid_dc=200.0)
    assert d.action is Action.ENTER
    assert d.diagnostics["score_5tier_v13b_s2"] == 4.5
    assert d.size == 5


def test_s2_score_5p0_yields_8ct():
    """score=5.0 (div_band=1 + bps_strong=1 + super_band=1, YES) -> 8 ct."""
    d = _eval_s2(side=Side.YES, bb_div_val=-0.10, strike=90_000.0,
                 fav_mid_dc=800.0)
    assert d.action is Action.ENTER
    assert d.diagnostics["score_5tier_v13b_s2"] == 5.0
    assert d.size == S2_SIZE_AT_5
    assert d.size == 8


def test_s2_score_5p5_yields_8ct():
    """score=5.5 (div_band=1 + side_no=1 + bps_strong=1, no super_band) -> 8 ct."""
    d = _eval_s2(side=Side.NO, bb_div_val=-0.05, strike=90_000.0,
                 fav_mid_dc=200.0)
    assert d.action is Action.ENTER
    assert d.diagnostics["score_5tier_v13b_s2"] == 5.5
    assert d.size == 8


def test_s2_score_6p5_yields_10ct():
    """score=6.5 (all four components) -> tier >= 6.0 -> 10 ct."""
    d = _eval_s2(side=Side.NO, bb_div_val=-0.10, strike=90_000.0,
                 fav_mid_dc=200.0)
    assert d.action is Action.ENTER
    assert d.diagnostics["score_5tier_v13b_s2"] == 6.5
    assert d.size == S2_SIZE_AT_6
    assert d.size == 10


# ---- SKIP cases -----------------------------------------------------------

def test_s2_score_below_3_is_skip():
    """score=2.0 (bps_strong=1 only, no div_band, YES) -> below 3.0 -> SKIP.

    bb_div = +0.05 makes div_band=0 (need <=0). super_band requires div_band so 0.
    """
    d = _eval_s2(side=Side.YES, bb_div_val=+0.05, strike=90_000.0,
                 fav_mid_dc=800.0)
    assert d.action is Action.SKIP
    assert d.diagnostics["score_5tier_v13b_s2"] == 2.0
    assert "5TIER_V13B_S2 skip" in d.reason
    assert "score=2.0" in d.reason


def test_s2_score_1p5_is_skip():
    """score=1.5 (side_no=1 only, NO + bb_div>0 so div_band=0) -> SKIP.

    strike=99_935 keeps us past the s_bps hard gate so we reach the score
    branch and the SKIP is the 'score < 3.0' one, not a hard-gate skip.
    """
    d = _eval_s2(side=Side.NO, bb_div_val=+0.05, strike=99_935.0,
                 fav_mid_dc=200.0)
    assert d.action is Action.SKIP
    assert d.diagnostics["score_5tier_v13b_s2"] == 1.5


def test_s2_skip_on_s_bps_zero():
    """Hard gate: bps_margin <= 1.5*threshold -> SKIP regardless of score.

    With BTC threshold=3.95, 1.5*threshold=5.92. strike=99_995 gives margin
    ≈ 0.5 bps, far below threshold. The model SKIPs even before scoring on
    ``bps_margin < threshold`` (the universal cutpoint), which is also fine —
    either way it must be SKIP.
    """
    d = _eval_s2(side=Side.NO, bb_div_val=-0.10, strike=99_995.0,
                 fav_mid_dc=200.0)
    assert d.action is Action.SKIP


# ---- diagnostics / reason -------------------------------------------------

def test_s2_diagnostics_include_score_field():
    """Decisions in S2 mode include score_5tier_v13b_s2 + the V13b component
    flags in diagnostics so log-readers can reconstruct sizing decisions."""
    d = _eval_s2(side=Side.NO, bb_div_val=-0.10, strike=90_000.0,
                 fav_mid_dc=200.0)
    for k in ("score_5tier_v13b_s2", "bb_div_band", "side_no",
              "side_yes", "bps_strong", "super_band", "align_mode"):
        assert k in d.diagnostics
    assert d.diagnostics["align_mode"] == "5tier_v13b_s2"


def test_s2_reason_format_includes_score_and_size():
    """The reason string follows the documented prefix + score=X.X -> Yct format."""
    d = _eval_s2(side=Side.NO, bb_div_val=-0.10, strike=90_000.0,
                 fav_mid_dc=200.0)
    assert d.reason.startswith("5TIER_V13B_S2 score=")
    assert "-> 10ct" in d.reason


# ---- regression: 5tier_v13b unchanged ------------------------------------

def test_5tier_v13b_still_caps_at_5ct():
    """Sanity: original ``5tier_v13b`` mode is unchanged by the S2 addition —
    still uses SIZE_CAP_5TIER=5 even at the highest possible score=6.5."""
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
    assert d.size == 5  # SIZE_CAP_5TIER, even though score=6.5


def test_invalid_align_mode_rejected():
    """Sanity: unknown align_mode still rejected (regression on validation)."""
    with pytest.raises(ValueError, match="align_mode"):
        Phase4CutpointsModel(align_mode="5tier_v13b_s3")
