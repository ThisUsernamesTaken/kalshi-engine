from __future__ import annotations

from datetime import datetime, timezone

from kalshi_engine.core.types import Action, Side
from kalshi_engine.strategies.favorite_chase.models.phase4_cutpoints import (
    Phase4CutpointsModel,
)


class _FakeState:
    def __init__(self, crypto: str, *, spot: float, vol_pct: float = 0.30):
        self.crypto = crypto
        self._spot = spot
        self._vol_pct = vol_pct

    def latest_spot(self):
        return self._spot

    def vol_30m(self):
        return 5.0

    def vol_30m_percentile(self, value):
        return self._vol_pct

    def bb_fair(self, spot, strike, sigma, tau):
        return 0.5

    def bb_div(self, favorite_mid_decicents, bb_fair):
        return -0.10


def _ts(hour: int, minute: int = 23) -> int:
    return int(
        datetime(2026, 5, 27, hour, minute, tzinfo=timezone.utc).timestamp()
        * 1000
    )


def _eval(
    crypto: str,
    side: Side,
    *,
    spot: float,
    strike: float,
    fav_mid_dc: float,
    vol_pct: float = 0.30,
    hour: int = 12,
    minute: int = 23,
):
    model = Phase4CutpointsModel(align_mode="5tier_v13b_h1h4_loose")
    now_ms = _ts(hour, minute)
    return model.evaluate(
        state=_FakeState(crypto, spot=spot, vol_pct=vol_pct),
        ticker=f"KX{crypto}15M-T",
        side=side,
        favorite_mid_decicents=fav_mid_dc,
        strike=strike,
        now_ms=now_ms,
        close_ms=now_ms + 420_000,
    )


def test_doge_no_weak_cushion_is_blocked_with_shadow_opposite():
    decision = _eval(
        "DOGE", Side.NO,
        spot=0.1000, strike=0.10012, fav_mid_dc=720.0,
    )

    assert decision.action is Action.SKIP
    assert "DOGE/XRP contrarian guard" in decision.reason
    assert decision.diagnostics["contrarian_shadow_side"] == "yes"
    assert "DOGE NO bps_margin" in decision.reason


def test_doge_yes_is_not_blocked_by_doge_no_gate():
    decision = _eval(
        "DOGE", Side.YES,
        spot=0.1000, strike=0.0998, fav_mid_dc=720.0,
    )

    assert "DOGE/XRP contrarian guard" not in decision.reason


def test_xrp_yes_late_high_vol_is_blocked():
    decision = _eval(
        "XRP", Side.YES,
        spot=1.0000, strike=0.9980, fav_mid_dc=820.0,
        vol_pct=0.60, hour=12, minute=53,
    )

    assert decision.action is Action.SKIP
    assert "XRP YES late/high-vol" in decision.reason
    assert decision.diagnostics["contrarian_shadow_side"] == "no"


def test_xrp_no_low_vol_is_blocked():
    decision = _eval(
        "XRP", Side.NO,
        spot=1.0000, strike=1.0015, fav_mid_dc=780.0,
        vol_pct=0.18,
    )

    assert decision.action is Action.SKIP
    assert "XRP NO low-vol" in decision.reason
    assert decision.diagnostics["contrarian_shadow_side"] == "yes"


def test_bad_doge_xrp_utc_hour_blocks_otherwise_passing_trade():
    decision = _eval(
        "XRP", Side.NO,
        spot=1.0000, strike=1.0020, fav_mid_dc=820.0,
        vol_pct=0.35, hour=18,
    )

    assert decision.action is Action.SKIP
    assert "18Z" in decision.reason
    assert decision.diagnostics["doge_xrp_contrarian_guard"] is True
