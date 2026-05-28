"""Phase 13.1: 5tier_v13b_1to3_flat compressed-sizing mode tests.

Same V13b score formula and hard gates as ``5tier_v13b``. SKIPs everything
below score=4.0 and sizes ALL passing trades at a flat 3 contracts. This is
the T3 ("all-in >=4") winner from the 1hr observer tier sweep, scaled to a
1-3 ceiling for the unproven 1hr regime.

    score < 4.0  -> SKIP
    score >= 4.0 -> 3 ct (flat)

Tests cover each achievable V13b score (>= 4 enters at 3 ct, < 4 skips),
hard gates, diagnostics, reason format, and regression on existing modes.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from kalshi_engine.core.types import Action, Side
from kalshi_engine.strategies.favorite_chase.models.phase4_cutpoints import (
    ALIGN_MODES,
    H1TO3_FLAT_SIZE,
    H1TO3_FLAT_SKIP_BELOW,
    Phase4CutpointsModel,
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


def _eval_flat(side: Side, bb_div_val: float, strike: float,
               fav_mid_dc: float = 200.0):
    model = Phase4CutpointsModel(align_mode="5tier_v13b_1to3_flat")
    ts = int(datetime(2026, 5, 24, 20, 0, 0,
                      tzinfo=timezone.utc).timestamp() * 1000)
    return model.evaluate(
        state=_FakeState(bb_div_val), ticker="KXBTC15M-T", side=side,
        favorite_mid_decicents=fav_mid_dc, strike=strike,
        now_ms=ts, close_ms=ts + 420_000,
    )


# ---- mode registration ----------------------------------------------------

def test_5tier_v13b_1to3_flat_in_align_modes():
    assert "5tier_v13b_1to3_flat" in ALIGN_MODES


def test_5tier_v13b_1to3_flat_mode_accepted():
    m = Phase4CutpointsModel(align_mode="5tier_v13b_1to3_flat")
    assert m.align_mode == "5tier_v13b_1to3_flat"


def test_5tier_v13b_1to3_flat_constants():
    """Sanity: skip-floor and flat size match the documented values."""
    assert H1TO3_FLAT_SKIP_BELOW == 4.0
    assert H1TO3_FLAT_SIZE == 3


# ---- every passing score yields 3 ct -------------------------------------
# Achievable V13b scores: 0, 1.5, 2.0, 3.0, 3.5, 4.0, 4.5, 5.0, 5.5, 6.5.
# Flat mode: scores < 4 SKIP, all others -> 3 ct.

def test_flat_score_4p0_yields_3ct():
    """score=4.0 (div_band=1 + bps_strong=1, YES, no super_band) -> 3 ct."""
    d = _eval_flat(side=Side.YES, bb_div_val=-0.05, strike=90_000.0,
                   fav_mid_dc=800.0)
    assert d.action is Action.ENTER
    assert d.diagnostics["score_5tier_v13b_1to3_flat"] == 4.0
    assert d.size == H1TO3_FLAT_SIZE
    assert d.size == 3


def test_flat_score_4p5_yields_3ct():
    """score=4.5 (div_band=1 + side_no=1 + super_band=1) -> 3 ct (flat)."""
    d = _eval_flat(side=Side.NO, bb_div_val=-0.10, strike=99_935.0,
                   fav_mid_dc=200.0)
    assert d.action is Action.ENTER
    assert d.diagnostics["score_5tier_v13b_1to3_flat"] == 4.5
    assert d.size == 3


def test_flat_score_5p0_yields_3ct():
    """score=5.0 (div_band=1 + bps_strong=1 + super_band=1, YES) -> 3 ct."""
    d = _eval_flat(side=Side.YES, bb_div_val=-0.10, strike=90_000.0,
                   fav_mid_dc=800.0)
    assert d.action is Action.ENTER
    assert d.diagnostics["score_5tier_v13b_1to3_flat"] == 5.0
    assert d.size == 3


def test_flat_score_5p5_yields_3ct():
    """score=5.5 -> 3 ct (flat)."""
    d = _eval_flat(side=Side.NO, bb_div_val=-0.05, strike=90_000.0,
                   fav_mid_dc=200.0)
    assert d.action is Action.ENTER
    assert d.diagnostics["score_5tier_v13b_1to3_flat"] == 5.5
    assert d.size == 3


def test_flat_score_6p5_yields_3ct():
    """score=6.5 (all four components) -> 3 ct (flat - same as score=4)."""
    d = _eval_flat(side=Side.NO, bb_div_val=-0.10, strike=90_000.0,
                   fav_mid_dc=200.0)
    assert d.action is Action.ENTER
    assert d.diagnostics["score_5tier_v13b_1to3_flat"] == 6.5
    assert d.size == 3


# ---- SKIP cases (score < 4.0) --------------------------------------------

def test_flat_score_3p5_is_skip():
    """score=3.5 — every V13b cohort loss sits here. Flat mode SKIPs."""
    d = _eval_flat(side=Side.NO, bb_div_val=-0.05, strike=99_935.0,
                   fav_mid_dc=200.0)
    assert d.action is Action.SKIP
    assert d.diagnostics["score_5tier_v13b_1to3_flat"] == 3.5
    assert "5TIER_V13B_1TO3_FLAT skip" in d.reason


def test_flat_score_3p0_is_skip():
    """score=3.0 -> SKIP (below the 4.0 floor)."""
    d = _eval_flat(side=Side.YES, bb_div_val=-0.10, strike=99_935.0,
                   fav_mid_dc=800.0)
    assert d.action is Action.SKIP
    assert d.diagnostics["score_5tier_v13b_1to3_flat"] == 3.0


def test_flat_score_2p0_is_skip():
    """score=2.0 (bps_strong only, YES, no div_band) -> SKIP."""
    d = _eval_flat(side=Side.YES, bb_div_val=+0.05, strike=90_000.0,
                   fav_mid_dc=800.0)
    assert d.action is Action.SKIP
    assert d.diagnostics["score_5tier_v13b_1to3_flat"] == 2.0


def test_flat_skip_on_s_bps_zero():
    """Hard gate: bps_margin <= 1.5*threshold -> SKIP regardless of score."""
    d = _eval_flat(side=Side.NO, bb_div_val=-0.10, strike=99_995.0,
                   fav_mid_dc=200.0)
    assert d.action is Action.SKIP


# ---- diagnostics / reason -------------------------------------------------

def test_flat_diagnostics_include_score_field():
    """Decisions include score_5tier_v13b_1to3_flat + V13b component flags."""
    d = _eval_flat(side=Side.NO, bb_div_val=-0.10, strike=90_000.0,
                   fav_mid_dc=200.0)
    for k in ("score_5tier_v13b_1to3_flat", "bb_div_band", "side_no",
              "side_yes", "bps_strong", "super_band", "align_mode"):
        assert k in d.diagnostics
    assert d.diagnostics["align_mode"] == "5tier_v13b_1to3_flat"


def test_flat_reason_format():
    """Reason string follows the documented prefix + score=X.X -> 3ct format."""
    d = _eval_flat(side=Side.NO, bb_div_val=-0.10, strike=90_000.0,
                   fav_mid_dc=200.0)
    assert d.reason.startswith("5TIER_V13B_1TO3_FLAT score=")
    assert "-> 3ct" in d.reason


def test_flat_max_size_is_3():
    """Sanity: no input combination produces size > 3 in this mode."""
    d = _eval_flat(side=Side.NO, bb_div_val=-0.10, strike=90_000.0,
                   fav_mid_dc=200.0)
    assert d.diagnostics["score_5tier_v13b_1to3_flat"] == 6.5
    assert d.size <= 3


# ---- regression: existing modes unchanged --------------------------------

def test_5tier_v13b_h1h4_unchanged_after_flat():
    """Sanity: H1H4 still gives 12ct at score 6.5."""
    model = Phase4CutpointsModel(align_mode="5tier_v13b_h1h4")
    ts = int(datetime(2026, 5, 24, 20, 0, 0,
                      tzinfo=timezone.utc).timestamp() * 1000)
    d = model.evaluate(
        state=_FakeState(-0.10), ticker="KXBTC15M-T", side=Side.NO,
        favorite_mid_decicents=200.0, strike=90_000.0,
        now_ms=ts, close_ms=ts + 420_000,
    )
    assert d.action is Action.ENTER
    assert d.diagnostics["score_5tier_v13b_h1h4"] == 6.5
    assert d.size == 12


def test_invalid_align_mode_still_rejected():
    """Regression: unknown align_mode still rejected."""
    with pytest.raises(ValueError, match="align_mode"):
        Phase4CutpointsModel(align_mode="5tier_v13b_1to3_flat_v2")
