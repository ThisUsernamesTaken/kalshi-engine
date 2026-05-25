"""FavoriteChaseState: vol_30m, bb_fair, bb_div, vol percentile."""

from __future__ import annotations

import math

from kalshi_engine.core.events import SpotEvent
from kalshi_engine.core.types import Crypto, Venue
from kalshi_engine.strategies.favorite_chase.state import FavoriteChaseState


def _spot(ts: int, price: float) -> SpotEvent:
    return SpotEvent(
        crypto=Crypto.BTC, venue=Venue.FUSION, ts_ms=ts, recv_ms=ts, price=price
    )


def test_vol_30m_flat_series_is_zero():
    st = FavoriteChaseState("BTC")
    for i in range(31):
        st.update_spot(_spot(i * 60_000, 100_000.0))  # flat -> zero log-returns
    vol = st.vol_30m()
    assert vol is not None
    assert vol < 1e-6


def test_vol_30m_known_constant_return():
    # a constant per-minute log-return r gives rms_vol_bps == |r| * 1e4
    st = FavoriteChaseState("BTC")
    r = 0.0010  # 0.1% per minute
    price = 100_000.0
    for i in range(31):
        st.update_spot(_spot(i * 60_000, price))
        price *= math.exp(r)
    vol = st.vol_30m()
    assert vol is not None
    assert abs(vol - r * 1e4) < 0.5  # expect ~10 bps/min


def test_bb_fair_at_the_money_is_half():
    st = FavoriteChaseState("BTC")
    assert abs(st.bb_fair(100_000.0, 100_000.0, 0.001, 7.0) - 0.5) < 1e-9


def test_bb_fair_monotonic_in_spot():
    st = FavoriteChaseState("BTC")
    low = st.bb_fair(99_000.0, 100_000.0, 0.001, 7.0)
    high = st.bb_fair(101_000.0, 100_000.0, 0.001, 7.0)
    assert low < 0.5 < high


def test_bb_fair_degenerate_tau_is_deterministic():
    st = FavoriteChaseState("BTC")
    assert st.bb_fair(101_000.0, 100_000.0, 0.001, 0.0) == 1.0
    assert st.bb_fair(99_000.0, 100_000.0, 0.001, 0.0) == 0.0


def test_bb_div_sign():
    st = FavoriteChaseState("BTC")
    # market prices the favorite at 0.85, BB fair 0.70  -> divergence +0.15
    assert abs(st.bb_div(850, 0.70) - 0.15) < 1e-9
    # market 0.70, BB fair 0.85  -> divergence -0.15
    assert abs(st.bb_div(700, 0.85) + 0.15) < 1e-9


def test_vol_percentile_rank():
    st = FavoriteChaseState("BTC")
    st.vol_history_buffer = [(i, float(i)) for i in range(100)]  # values 0..99
    assert st.vol_30m_percentile(50.0) == 0.5
    assert st.vol_30m_percentile(-1.0) == 0.0
    assert st.vol_30m_percentile(200.0) == 1.0
