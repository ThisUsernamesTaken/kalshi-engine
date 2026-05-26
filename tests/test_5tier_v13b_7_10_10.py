"""Phase 13.2: 5tier_v13b_7_10_10 (T6 asymmetric) sizing mode tests.

Same V13b score formula and hard gates as 5tier_v13b. SKIPs <4. Sizes:

    score < 4.0        -> SKIP
    4.0 <= score < 5.0 -> 7 ct
    5.0 <= score < 6.0 -> 10 ct
    score >= 6.0       -> 10 ct
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from kalshi_engine.core.types import Action, Side
from kalshi_engine.strategies.favorite_chase.models.phase4_cutpoints import (
    ALIGN_MODES,
    T6_SIZE_AT_4,
    T6_SIZE_AT_5,
    T6_SIZE_AT_6,
    T6_SKIP_BELOW,
    Phase4CutpointsModel,
)


class _FakeState:
    def __init__(self, bb_div_val: float):
        self.crypto = "BTC"
        self._bb_div = bb_div_val
    def latest_spot(self): return 100_000.0
    def vol_30m(self): return 5.0
    def vol_30m_percentile(self, v): return 0.30
    def bb_fair(self, spot, strike, sigma, tau): return 0.5
    def bb_div(self, fav_mid, bb_fair): return self._bb_div


def _eval_t6(side: Side, bb_div_val: float, strike: float,
             fav_mid_dc: float = 200.0):
    model = Phase4CutpointsModel(align_mode="5tier_v13b_7_10_10")
    ts = int(datetime(2026, 5, 24, 20, 0, 0,
                      tzinfo=timezone.utc).timestamp() * 1000)
    return model.evaluate(
        state=_FakeState(bb_div_val), ticker="KXBTC15M-T", side=side,
        favorite_mid_decicents=fav_mid_dc, strike=strike,
        now_ms=ts, close_ms=ts + 420_000,
    )


def test_5tier_v13b_7_10_10_in_align_modes():
    assert "5tier_v13b_7_10_10" in ALIGN_MODES


def test_5tier_v13b_7_10_10_mode_accepted():
    m = Phase4CutpointsModel(align_mode="5tier_v13b_7_10_10")
    assert m.align_mode == "5tier_v13b_7_10_10"


def test_5tier_v13b_7_10_10_constants():
    assert T6_SKIP_BELOW == 4.0
    assert T6_SIZE_AT_4 == 7
    assert T6_SIZE_AT_5 == 10
    assert T6_SIZE_AT_6 == 10


def test_t6_score_4p0_yields_7ct():
    d = _eval_t6(side=Side.YES, bb_div_val=-0.05, strike=90_000.0,
                 fav_mid_dc=800.0)
    assert d.action is Action.ENTER
    assert d.diagnostics["score_5tier_v13b_7_10_10"] == 4.0
    assert d.size == T6_SIZE_AT_4
    assert d.size == 7


def test_t6_score_4p5_yields_7ct():
    """score=4.5 is in [4,5) -> 7 ct."""
    d = _eval_t6(side=Side.NO, bb_div_val=-0.10, strike=99_935.0,
                 fav_mid_dc=200.0)
    assert d.action is Action.ENTER
    assert d.diagnostics["score_5tier_v13b_7_10_10"] == 4.5
    assert d.size == 7


def test_t6_score_5p0_yields_10ct():
    d = _eval_t6(side=Side.YES, bb_div_val=-0.10, strike=90_000.0,
                 fav_mid_dc=800.0)
    assert d.action is Action.ENTER
    assert d.diagnostics["score_5tier_v13b_7_10_10"] == 5.0
    assert d.size == T6_SIZE_AT_5
    assert d.size == 10


def test_t6_score_5p5_yields_10ct():
    d = _eval_t6(side=Side.NO, bb_div_val=-0.05, strike=90_000.0,
                 fav_mid_dc=200.0)
    assert d.action is Action.ENTER
    assert d.diagnostics["score_5tier_v13b_7_10_10"] == 5.5
    assert d.size == 10


def test_t6_score_6p5_yields_10ct():
    d = _eval_t6(side=Side.NO, bb_div_val=-0.10, strike=90_000.0,
                 fav_mid_dc=200.0)
    assert d.action is Action.ENTER
    assert d.diagnostics["score_5tier_v13b_7_10_10"] == 6.5
    assert d.size == T6_SIZE_AT_6
    assert d.size == 10


def test_t6_score_3p5_is_skip():
    d = _eval_t6(side=Side.NO, bb_div_val=-0.05, strike=99_935.0,
                 fav_mid_dc=200.0)
    assert d.action is Action.SKIP
    assert d.diagnostics["score_5tier_v13b_7_10_10"] == 3.5
    assert "5TIER_V13B_7_10_10 skip" in d.reason


def test_t6_score_3p0_is_skip():
    d = _eval_t6(side=Side.YES, bb_div_val=-0.10, strike=99_935.0,
                 fav_mid_dc=800.0)
    assert d.action is Action.SKIP


def test_t6_score_2p0_is_skip():
    d = _eval_t6(side=Side.YES, bb_div_val=+0.05, strike=90_000.0,
                 fav_mid_dc=800.0)
    assert d.action is Action.SKIP


def test_t6_skip_on_s_bps_zero():
    d = _eval_t6(side=Side.NO, bb_div_val=-0.10, strike=99_995.0,
                 fav_mid_dc=200.0)
    assert d.action is Action.SKIP


def test_t6_diagnostics_include_score_field():
    d = _eval_t6(side=Side.NO, bb_div_val=-0.10, strike=90_000.0,
                 fav_mid_dc=200.0)
    for k in ("score_5tier_v13b_7_10_10", "bb_div_band", "side_no",
              "side_yes", "bps_strong", "super_band", "align_mode"):
        assert k in d.diagnostics
    assert d.diagnostics["align_mode"] == "5tier_v13b_7_10_10"


def test_t6_reason_format():
    d = _eval_t6(side=Side.NO, bb_div_val=-0.10, strike=90_000.0,
                 fav_mid_dc=200.0)
    assert d.reason.startswith("5TIER_V13B_7_10_10 score=")
    assert "-> 10ct" in d.reason


def test_t6_max_size_is_10():
    d = _eval_t6(side=Side.NO, bb_div_val=-0.10, strike=90_000.0,
                 fav_mid_dc=200.0)
    assert d.size <= 10


def test_t6_min_entry_size_is_7():
    d = _eval_t6(side=Side.YES, bb_div_val=-0.05, strike=90_000.0,
                 fav_mid_dc=800.0)
    assert d.action is Action.ENTER
    assert d.size >= 7


def test_5tier_v13b_10_flat_unchanged_after_t6():
    """Sanity: 10_flat still gives 10ct at score 4.0 (backward compat)."""
    model = Phase4CutpointsModel(align_mode="5tier_v13b_10_flat")
    ts = int(datetime(2026, 5, 24, 20, 0, 0,
                      tzinfo=timezone.utc).timestamp() * 1000)
    d = model.evaluate(
        state=_FakeState(-0.05), ticker="KXBTC15M-T", side=Side.YES,
        favorite_mid_decicents=800.0, strike=90_000.0,
        now_ms=ts, close_ms=ts + 420_000,
    )
    assert d.action is Action.ENTER
    assert d.diagnostics["score_5tier_v13b_10_flat"] == 4.0
    assert d.size == 10


def test_invalid_align_mode_still_rejected():
    with pytest.raises(ValueError, match="align_mode"):
        Phase4CutpointsModel(align_mode="5tier_v13b_7_10_10_v2")
