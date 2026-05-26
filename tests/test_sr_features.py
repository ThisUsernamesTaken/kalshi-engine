"""Pure-function tests for research.sr_features.

Validates BB position, pivot levels, and the convenience bundle on
hand-crafted spot histories where the expected math is checkable by hand.
"""

from __future__ import annotations

import pytest

from kalshi_engine.research.sr_features import (
    all_sr_features, bollinger_position, pivot_levels, window_high_low,
)


# ---- bollinger_position ------------------------------------------------

def test_bb_pos_at_mean_returns_zero():
    """Constant price → sd=0 → returns None. Use slight variance instead."""
    history = [(0, 99.0), (60_000, 100.0), (120_000, 101.0)]
    # spot at 100 (the mean) should give bb_pos ≈ 0
    r = bollinger_position(100.0, history, ts_at=120_000, window_ms=180_000)
    assert r is not None
    assert abs(r) < 0.01


def test_bb_pos_at_upper_band_returns_one():
    """spot at mean + 2σ should give bb_pos = +1.0."""
    history = [(0, 90.0), (60_000, 100.0), (120_000, 110.0)]
    # mean = 100, sd = sqrt(((90-100)^2 + 0 + (110-100)^2) / 3) = sqrt(200/3) ≈ 8.165
    # upper band = 100 + 2*8.165 = 116.33
    r = bollinger_position(116.33, history, ts_at=120_000, window_ms=180_000)
    assert r is not None
    assert 0.99 < r < 1.01


def test_bb_pos_at_lower_band_returns_negative_one():
    history = [(0, 90.0), (60_000, 100.0), (120_000, 110.0)]
    r = bollinger_position(83.67, history, ts_at=120_000, window_ms=180_000)
    assert r is not None
    assert -1.01 < r < -0.99


def test_bb_pos_above_upper_band_returns_above_one():
    """Spot beyond +2σ → bb_pos > +1.0 (the loser signature)."""
    history = [(0, 90.0), (60_000, 100.0), (120_000, 110.0)]
    r = bollinger_position(125.0, history, ts_at=120_000, window_ms=180_000)
    assert r is not None
    assert r > 1.0


def test_bb_pos_returns_none_on_empty_history():
    assert bollinger_position(100.0, [], ts_at=0, window_ms=60_000) is None


def test_bb_pos_returns_none_on_single_sample():
    assert bollinger_position(100.0, [(0, 100.0)], ts_at=0,
                               window_ms=60_000) is None


def test_bb_pos_returns_none_when_spot_is_none():
    history = [(0, 90.0), (60_000, 100.0), (120_000, 110.0)]
    assert bollinger_position(None, history, ts_at=120_000,
                               window_ms=180_000) is None


def test_bb_pos_returns_none_on_zero_variance():
    """Flat history (all same price) → sd=0 → return None (degenerate)."""
    history = [(0, 100.0), (60_000, 100.0), (120_000, 100.0)]
    assert bollinger_position(100.0, history, ts_at=120_000,
                               window_ms=180_000) is None


# ---- window_high_low ----------------------------------------------------

def test_window_high_low_basic():
    history = [(0, 90.0), (60_000, 110.0), (120_000, 95.0)]
    hl = window_high_low(history, ts_at=120_000, window_ms=180_000)
    assert hl == (110.0, 90.0)


def test_window_high_low_excludes_outside_window():
    history = [(0, 90.0), (60_000, 110.0), (120_000, 95.0)]
    # Window only 60s wide ending at 120_000 → should see only the latest 2
    hl = window_high_low(history, ts_at=120_000, window_ms=60_000)
    assert hl == (110.0, 95.0)


def test_window_high_low_empty_returns_none():
    assert window_high_low([], ts_at=0, window_ms=60_000) is None


# ---- pivot_levels -------------------------------------------------------

def test_pivot_levels_basic():
    """High=110, Low=90, close=100 → pivot=100, R1=110, S1=90.
    Wait: P = (H+L+C)/3 = (110+90+100)/3 = 100
    R1 = 2P - L = 200 - 90 = 110
    S1 = 2P - H = 200 - 110 = 90"""
    history = [(0, 90.0), (60_000, 110.0), (120_000, 100.0)]
    p = pivot_levels(100.0, history, ts_at=120_000, window_ms=180_000)
    assert p is not None
    assert p["pivot"] == 100.0
    assert p["R1"] == 110.0
    assert p["S1"] == 90.0
    assert p["dist_to_R1"] == 10.0
    assert p["dist_to_S1"] == 10.0
    assert p["window_high"] == 110.0
    assert p["window_low"] == 90.0
    assert p["window_range"] == 20.0


def test_pivot_levels_empty_returns_none():
    assert pivot_levels(100.0, [], ts_at=0, window_ms=60_000) is None


def test_pivot_levels_spot_none_returns_none():
    history = [(0, 90.0), (60_000, 110.0)]
    assert pivot_levels(None, history, ts_at=60_000, window_ms=180_000) is None


# ---- all_sr_features convenience bundle --------------------------------

def test_all_sr_features_returns_full_bundle():
    """Window must contain enough samples (≥2 over the longest window).
    Use 25h worth of samples 1/min for a clean test."""
    history = []
    for i in range(1500):  # 25 hours of 1-min samples
        ts = i * 60_000
        # Sinusoidal price ~ 100 with amplitude 5
        import math
        history.append((ts, 100.0 + 5.0 * math.sin(i * 0.1)))
    ts_at = history[-1][0]
    spot_at = history[-1][1]
    f = all_sr_features(spot_at, history, ts_at)
    # Expect every key populated (no Nones)
    for key in ("bb_pos_1h", "bb_pos_4h", "bb_pos_24h",
                "pivot", "pivot_R1", "pivot_S1",
                "dist_to_R1", "dist_to_S1"):
        assert f.get(key) is not None, f"missing {key}: {f}"


def test_all_sr_features_handles_sparse_history():
    """With only 2 samples (much less than any window), 1h/4h/24h bb_pos
    can still be computed if both samples fit; pivot returns dict."""
    history = [(0, 100.0), (60_000, 105.0)]
    f = all_sr_features(105.0, history, ts_at=60_000)
    # bb_pos requires ≥2 samples — these 2 fit in 1h window
    assert f["bb_pos_1h"] is not None
    assert f["pivot"] is not None
