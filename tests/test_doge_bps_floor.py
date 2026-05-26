"""Phase 13.3: DOGE-specific bps_margin floor tests.

Post-mortem on 67 DOGE trades found cheap-NO losers clustered at
bps in [8.5, 10). The DOGE_BPS_FLOOR=10 hard gate (applied after
the universal bps_margin < threshold gate) drops those.

Tests verify:
1. DOGE entries with bps_margin < 10 are SKIPped with the new reason
2. DOGE entries with bps_margin >= 10 reach the align-mode sizing path
3. Non-DOGE cryptos are unaffected
4. The skip applies to BOTH YES and NO sides (symmetric)
"""
from __future__ import annotations
from datetime import datetime, timezone
import pytest
from kalshi_engine.core.types import Action, Side
from kalshi_engine.strategies.favorite_chase.models.phase4_cutpoints import (
    DOGE_BPS_FLOOR, Phase4CutpointsModel,
)


class _FakeState:
    """Drives crypto + bb_div directly so we can hit DOGE codepaths."""
    def __init__(self, crypto: str, bb_div_val: float):
        self.crypto = crypto
        self._bb_div = bb_div_val
    def latest_spot(self): return 100_000.0 if self.crypto == "BTC" else 0.10
    def vol_30m(self): return 5.0
    def vol_30m_percentile(self, v): return 0.30
    def bb_fair(self, spot, strike, sigma, tau): return 0.5
    def bb_div(self, fav_mid, bb_fair): return self._bb_div


def _eval(crypto: str, side: Side, strike: float, fav_mid_dc: float = 200.0):
    model = Phase4CutpointsModel(align_mode="5tier_v13b_h1h4")
    spot = 100_000.0 if crypto == "BTC" else 0.10
    state = _FakeState(crypto, -0.10)
    ts = int(datetime(2026, 5, 24, 20, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
    return model.evaluate(
        state=state, ticker=f"KX{crypto}15M-T", side=side,
        favorite_mid_decicents=fav_mid_dc, strike=strike,
        now_ms=ts, close_ms=ts + 420_000,
    )


def test_doge_bps_floor_constant():
    assert DOGE_BPS_FLOOR == 10.0


def test_doge_below_floor_skips_no_side():
    """DOGE NO with bps_margin=9.0 (< 10) -> SKIP with floor reason."""
    # spot=0.10, strike=0.10009 -> bps_margin = 0.00009/0.10 * 1e4 = 9.0
    d = _eval("DOGE", Side.NO, strike=0.10009, fav_mid_dc=200.0)
    assert d.action is Action.SKIP
    assert "DOGE bps_margin" in d.reason
    assert "per-crypto floor" in d.reason


def test_doge_below_floor_skips_yes_side():
    """Symmetric: DOGE YES with bps_margin=9.0 also SKIPs."""
    d = _eval("DOGE", Side.YES, strike=0.09991, fav_mid_dc=800.0)
    assert d.action is Action.SKIP
    assert "DOGE bps_margin" in d.reason


def test_doge_above_floor_proceeds():
    """DOGE NO with bps_margin >= 10 reaches model sizing path (not SKIP-floor)."""
    # spot=0.10, strike=0.1002 -> bps_margin = 20.0
    d = _eval("DOGE", Side.NO, strike=0.1002, fav_mid_dc=200.0)
    # May still SKIP for other reasons (score < 4, etc), but reason should
    # NOT mention the per-crypto DOGE floor.
    if d.action is Action.SKIP:
        assert "per-crypto floor" not in d.reason


def test_btc_not_affected_by_doge_floor():
    """BTC with bps_margin=9 should NOT trigger the DOGE-specific gate."""
    # spot=100_000, strike=100_090 -> bps_margin = 9
    d = _eval("BTC", Side.NO, strike=100_090.0, fav_mid_dc=200.0)
    if d.action is Action.SKIP:
        assert "DOGE bps_margin" not in d.reason


def test_doge_floor_applies_before_align_mode():
    """The DOGE floor SKIP fires before per-align-mode logic — verify the
    reason matches the new gate, not 5TIER_V13B_H1H4."""
    d = _eval("DOGE", Side.NO, strike=0.10009, fav_mid_dc=200.0)
    assert d.action is Action.SKIP
    assert "5TIER_V13B_H1H4" not in d.reason
    assert "DOGE bps_margin" in d.reason
