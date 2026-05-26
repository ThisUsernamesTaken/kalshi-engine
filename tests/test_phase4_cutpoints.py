"""Phase4CutpointsModel: all 8 corners of the cutpoint space, plus upsize."""

from __future__ import annotations

import itertools

import pytest

from kalshi_engine.config import MODELS_DIR
from kalshi_engine.core.types import Action, Side
from kalshi_engine.strategies.favorite_chase.models.phase4_cutpoints import (
    Phase4CutpointsModel,
)


class FakeState:
    """Test double: returns preset signal values to the model."""

    def __init__(self, crypto="BTC", spot=100_000.0, vol=5.0,
                 vol_pct=0.55, bb_div=0.0):
        self.crypto = crypto
        self._spot, self._vol = spot, vol
        self._vol_pct, self._bb_div = vol_pct, bb_div

    def latest_spot(self):
        return self._spot

    def vol_30m(self):
        return self._vol

    def vol_30m_percentile(self, value):
        return self._vol_pct

    def bb_fair(self, spot, strike, sigma, tau):
        return 0.5

    def bb_div(self, favorite_mid_decicents, bb_fair):
        return self._bb_div


# strike far from spot -> large bps margin (passes the gate);
# strike on spot -> zero margin (fails any per-crypto threshold)
_STRIKE_FAR = 90_000.0
_STRIKE_NEAR = 100_000.0


@pytest.fixture(scope="module")
def model():
    artifact = MODELS_DIR / "phase4_cutpoints" / "v1" / "cutpoints.json"
    if not artifact.exists():
        pytest.skip(f"cutpoints artifact not present: {artifact}")
    return Phase4CutpointsModel()


def _evaluate(model, state, strike):
    return model.evaluate(
        state=state, ticker="KXBTC15M-T", side=Side.YES,
        favorite_mid_decicents=800.0, strike=strike,
        now_ms=0, close_ms=420_000,
    )


@pytest.mark.parametrize(
    "vol_skip,bbdiv_skip,bps_skip",
    list(itertools.product([False, True], repeat=3)),
)
def test_eight_corners(model, vol_skip, bbdiv_skip, bps_skip):
    state = FakeState(
        vol_pct=0.90 if vol_skip else 0.55,    # 0.90 > 0.80 skip; 0.55 neither
        bb_div=0.15 if bbdiv_skip else 0.0,    # 0.15 > 0.09 skip; 0.0 neither
    )
    strike = _STRIKE_NEAR if bps_skip else _STRIKE_FAR
    decision = _evaluate(model, state, strike)
    if vol_skip or bbdiv_skip or bps_skip:
        assert decision.action is Action.SKIP
        assert decision.size == 0
        assert decision.confidence == 0.0
    else:
        assert decision.action is Action.ENTER
        assert decision.size == 1  # vol_pct 0.55 / bb_div 0.0 -> no upsize


def test_upsize_corner(model):
    state = FakeState(vol_pct=0.40, bb_div=-0.05)  # low vol AND model-cheap
    decision = _evaluate(model, state, _STRIKE_FAR)
    assert decision.action is Action.ENTER
    assert decision.size == 2
    assert decision.confidence == 0.9


def test_skip_reason_identifies_cause(model):
    assert "vol_pct" in _evaluate(model, FakeState(vol_pct=0.90), _STRIKE_FAR).reason
    assert "bb_div" in _evaluate(model, FakeState(bb_div=0.20), _STRIKE_FAR).reason
    assert "bps_margin" in _evaluate(model, FakeState(), _STRIKE_NEAR).reason


def test_diagnostics_complete(model):
    diag = _evaluate(model, FakeState(), _STRIKE_FAR).diagnostics
    for key in ("vol_30m", "vol_30m_pct", "bb_div", "bps_margin",
                "bps_threshold", "bb_yes", "spot", "strike"):
        assert key in diag


def test_phase_14_10_v1_vol_pct_cap_at_080():
    """Phase 14.10 — v1 cutpoints raise vol_30m_percentile_skip_above 0.67 -> 0.80.

    Loaded explicitly because the 15m engine launches with --cutpoints-version v1.
    Loosened based on full-universe sweep showing ETH/SOL/DOGE significant
    positive Δ at 95% CI. See
    C:/Trading/_tmp_analysis/full_universe_sweep_15m/HONEST_VERDICT.md.
    """
    v1_path = MODELS_DIR / "phase4_cutpoints" / "v1" / "cutpoints.json"
    if not v1_path.exists():
        pytest.skip(f"v1 cutpoints not present: {v1_path}")
    m = Phase4CutpointsModel(cutpoints_path=str(v1_path))
    # Cap itself
    assert m.vol_skip_above == 0.80
    # Just below cap → ENTER (assuming other gates pass)
    just_below = _evaluate(m, FakeState(vol_pct=0.79), _STRIKE_FAR)
    assert just_below.action is Action.ENTER, just_below.reason
    # Just above cap → SKIP
    just_above = _evaluate(m, FakeState(vol_pct=0.81), _STRIKE_FAR)
    assert just_above.action is Action.SKIP
    assert "vol_pct" in just_above.reason
    # 0.67 (the prior cap) → ENTER under the new loosened gate
    prior_cap = _evaluate(m, FakeState(vol_pct=0.67), _STRIKE_FAR)
    assert prior_cap.action is Action.ENTER, prior_cap.reason
