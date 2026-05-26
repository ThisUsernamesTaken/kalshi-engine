"""Phase 13.4: 5tier_v13b_h1h4_loose sizing mode tests.

Identical to ``5tier_v13b_h1h4`` for score >= 4.0 (skip<4, smooth multiplier
7/8/9/10 by tier). Adds a targeted relaxation in the [3.0, 4.0) band: ENTER
3ct IFF the validated bb_div sweet-spot edge is present (bb_div_band=1) AND
vol is sub-mid (vol_pct < 0.5). Everything below score 3.0 still SKIPs.

The loosening is motivated by the live finding that 13/13 cohort wins in the
score 2.5-3.5 band all had bb_div_band=1 — a clean signal worth a tiny size.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from kalshi_engine.core.types import Action, Side
from kalshi_engine.strategies.favorite_chase.models.phase4_cutpoints import (
    ALIGN_MODES,
    H1H4_SCORE_MULT,
    LOOSE_BORDERLINE_HI,
    LOOSE_SCORE_FLOOR,
    LOOSE_SIZE_BORDERLINE,
    LOOSE_VOL_PCT_MAX,
    Phase4CutpointsModel,
    S2_SIZE_AT_6,
)


class _FakeState:
    """Bypasses spot/vol machinery so tests can drive bb_div / vol_pct directly."""

    def __init__(self, bb_div_val: float, vol_pct: float = 0.30):
        self.crypto = "BTC"
        self._bb_div = bb_div_val
        self._vol_pct = vol_pct

    def latest_spot(self):
        return 100_000.0

    def vol_30m(self):
        return 5.0

    def vol_30m_percentile(self, v):
        return self._vol_pct

    def bb_fair(self, spot, strike, sigma, tau):
        return 0.5

    def bb_div(self, fav_mid, bb_fair):
        return self._bb_div


def _eval_loose(side: Side, bb_div_val: float, strike: float,
                fav_mid_dc: float = 200.0, vol_pct: float = 0.30):
    model = Phase4CutpointsModel(align_mode="5tier_v13b_h1h4_loose")
    ts = int(datetime(2026, 5, 24, 20, 0, 0,
                      tzinfo=timezone.utc).timestamp() * 1000)
    return model.evaluate(
        state=_FakeState(bb_div_val, vol_pct=vol_pct),
        ticker="KXBTC15M-T", side=side,
        favorite_mid_decicents=fav_mid_dc, strike=strike,
        now_ms=ts, close_ms=ts + 420_000,
    )


# ---- mode registration ----------------------------------------------------

def test_5tier_v13b_h1h4_loose_in_align_modes():
    assert "5tier_v13b_h1h4_loose" in ALIGN_MODES


def test_5tier_v13b_h1h4_loose_mode_accepted():
    m = Phase4CutpointsModel(align_mode="5tier_v13b_h1h4_loose")
    assert m.align_mode == "5tier_v13b_h1h4_loose"


def test_5tier_v13b_h1h4_loose_constants():
    """Sanity: loose tier constants are the documented values."""
    assert LOOSE_SCORE_FLOOR == 3.0
    assert LOOSE_BORDERLINE_HI == 4.0
    assert LOOSE_VOL_PCT_MAX == 0.5
    assert LOOSE_SIZE_BORDERLINE == 3


# ---- score >= 4.0 — identical to H1H4 sizing ------------------------------

def test_loose_score_4p0_yields_7ct():
    """score=4.0 (div_band=1 + bps_strong=1, YES) -> 7 ct, same as H1H4."""
    d = _eval_loose(side=Side.YES, bb_div_val=-0.05, strike=90_000.0,
                    fav_mid_dc=800.0)
    assert d.action is Action.ENTER
    assert d.diagnostics["score_5tier_v13b_h1h4_loose"] == 4.0
    assert d.size == 7


def test_loose_score_4p5_yields_8ct():
    """score=4.5 (div_band=1 + side_no + super_band, no bps_strong) -> 8 ct."""
    d = _eval_loose(side=Side.NO, bb_div_val=-0.10, strike=99_935.0,
                    fav_mid_dc=200.0)
    assert d.action is Action.ENTER
    assert d.diagnostics["score_5tier_v13b_h1h4_loose"] == 4.5
    assert d.size == 8


def test_loose_score_5p0_yields_9ct():
    """score=5.0 (div_band=1 + bps_strong=1 + super_band=1, YES) -> 9 ct."""
    d = _eval_loose(side=Side.YES, bb_div_val=-0.10, strike=90_000.0,
                    fav_mid_dc=800.0)
    assert d.action is Action.ENTER
    assert d.diagnostics["score_5tier_v13b_h1h4_loose"] == 5.0
    assert d.size == 9


def test_loose_score_5p5_yields_10ct():
    """score=5.5 (div_band=1 + side_no=1 + bps_strong=1) -> 10 ct (capped)."""
    d = _eval_loose(side=Side.NO, bb_div_val=-0.05, strike=90_000.0,
                    fav_mid_dc=200.0)
    assert d.action is Action.ENTER
    assert d.diagnostics["score_5tier_v13b_h1h4_loose"] == 5.5
    assert d.size == 10


def test_loose_score_6p5_yields_10ct_capped():
    """score=6.5 (all four, NO) -> round(11.7)=12 capped to 10."""
    d = _eval_loose(side=Side.NO, bb_div_val=-0.10, strike=90_000.0,
                    fav_mid_dc=200.0)
    assert d.action is Action.ENTER
    assert d.diagnostics["score_5tier_v13b_h1h4_loose"] == 6.5
    assert d.size == S2_SIZE_AT_6 == 10


# ---- borderline tier [3.0, 4.0): targeted loosening ----------------------

def test_loose_score_3p5_div_band_pass_low_vol_enters_3ct():
    """score=3.5 with bb_div_band=1 (bb_div=-0.05) and vol_pct=0.3 -> ENTER 3ct.
    This is the whole point of the loose mode — previously SKIPed by H1H4."""
    d = _eval_loose(side=Side.NO, bb_div_val=-0.05, strike=99_935.0,
                    fav_mid_dc=200.0, vol_pct=0.30)
    assert d.action is Action.ENTER
    assert d.diagnostics["score_5tier_v13b_h1h4_loose"] == 3.5
    assert d.diagnostics["bb_div_band"] == 1
    assert d.size == LOOSE_SIZE_BORDERLINE == 3


def test_loose_score_3p0_div_band_pass_low_vol_enters_3ct():
    """score=3.0 (div_band=1 + super_band=1, YES, no bps_strong) — borderline
    floor case still ENTERs at 3ct."""
    d = _eval_loose(side=Side.YES, bb_div_val=-0.10, strike=99_935.0,
                    fav_mid_dc=800.0, vol_pct=0.30)
    assert d.action is Action.ENTER
    assert d.diagnostics["score_5tier_v13b_h1h4_loose"] == 3.0
    assert d.diagnostics["bb_div_band"] == 1
    assert d.size == LOOSE_SIZE_BORDERLINE


def test_loose_borderline_skip_when_bb_div_band_zero():
    """score=3.5 with bb_div_band=0 (bb_div=+0.05, side_no=1, bps_strong=1)
    -> SKIP. The borderline tier requires the validated edge signal."""
    d = _eval_loose(side=Side.NO, bb_div_val=+0.05, strike=90_000.0,
                    fav_mid_dc=200.0, vol_pct=0.30)
    assert d.action is Action.SKIP
    assert d.diagnostics["score_5tier_v13b_h1h4_loose"] == 3.5
    assert d.diagnostics["bb_div_band"] == 0
    assert "borderline skip" in d.reason
    assert "bb_div_band=0" in d.reason


def test_loose_borderline_skip_when_vol_pct_too_high():
    """score=3.5 with bb_div_band=1 (passes edge) but vol_pct=0.60 >= 0.50
    -> SKIP. The borderline tier requires sub-mid vol."""
    d = _eval_loose(side=Side.NO, bb_div_val=-0.05, strike=99_935.0,
                    fav_mid_dc=200.0, vol_pct=0.60)
    assert d.action is Action.SKIP
    assert d.diagnostics["score_5tier_v13b_h1h4_loose"] == 3.5
    assert d.diagnostics["bb_div_band"] == 1
    assert "borderline skip" in d.reason
    assert "vol_pct" in d.reason


def test_loose_borderline_skip_at_exact_vol_threshold():
    """vol_pct == 0.50 exactly: the gate is `>=`, so this SKIPs."""
    d = _eval_loose(side=Side.NO, bb_div_val=-0.05, strike=99_935.0,
                    fav_mid_dc=200.0, vol_pct=0.50)
    assert d.action is Action.SKIP
    assert "borderline skip" in d.reason


# ---- score < 3.0 — unconditional SKIP ------------------------------------

def test_loose_score_2p0_is_skip():
    """score=2.0 (bps_strong only, YES, no div_band) -> SKIP below floor."""
    d = _eval_loose(side=Side.YES, bb_div_val=+0.05, strike=90_000.0,
                    fav_mid_dc=800.0)
    assert d.action is Action.SKIP
    assert d.diagnostics["score_5tier_v13b_h1h4_loose"] == 2.0
    assert "score=2.0 <" in d.reason


def test_loose_score_1p5_is_skip():
    """score=1.5 (side_no only) -> SKIP below floor."""
    d = _eval_loose(side=Side.NO, bb_div_val=+0.05, strike=99_935.0,
                    fav_mid_dc=200.0)
    assert d.action is Action.SKIP
    assert d.diagnostics["score_5tier_v13b_h1h4_loose"] == 1.5


# ---- hard gate ------------------------------------------------------------

def test_loose_skip_on_s_bps_zero():
    """Hard gate: bps_margin <= 1.5*threshold -> SKIP regardless of score."""
    d = _eval_loose(side=Side.NO, bb_div_val=-0.10, strike=99_995.0,
                    fav_mid_dc=200.0)
    assert d.action is Action.SKIP


# ---- diagnostics / reason -------------------------------------------------

def test_loose_diagnostics_include_score_field():
    """Decisions in loose mode include score_5tier_v13b_h1h4_loose + V13b flags."""
    d = _eval_loose(side=Side.NO, bb_div_val=-0.10, strike=90_000.0,
                    fav_mid_dc=200.0)
    for k in ("score_5tier_v13b_h1h4_loose", "bb_div_band", "side_no",
              "side_yes", "bps_strong", "super_band", "align_mode"):
        assert k in d.diagnostics
    assert d.diagnostics["align_mode"] == "5tier_v13b_h1h4_loose"


def test_loose_reason_format_on_enter():
    """Reason string follows the documented prefix + score=X.X -> Yct format."""
    d = _eval_loose(side=Side.NO, bb_div_val=-0.10, strike=90_000.0,
                    fav_mid_dc=200.0)
    assert d.reason.startswith("5TIER_V13B_H1H4_LOOSE score=")
    assert "-> 10ct" in d.reason


# ---- regression: existing modes unchanged --------------------------------

def test_5tier_v13b_h1h4_unchanged_by_loose_addition():
    """Sanity: ``5tier_v13b_h1h4`` (the original) still SKIPs score 3.5."""
    model = Phase4CutpointsModel(align_mode="5tier_v13b_h1h4")
    ts = int(datetime(2026, 5, 24, 20, 0, 0,
                      tzinfo=timezone.utc).timestamp() * 1000)
    d = model.evaluate(
        state=_FakeState(-0.05), ticker="KXBTC15M-T", side=Side.NO,
        favorite_mid_decicents=200.0, strike=99_935.0,
        now_ms=ts, close_ms=ts + 420_000,
    )
    assert d.action is Action.SKIP
    assert d.diagnostics["score_5tier_v13b_h1h4"] == 3.5
