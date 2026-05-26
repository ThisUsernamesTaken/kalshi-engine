"""Support/resistance + range-position features for cycle observability.

Pure functions over a spot-history time series. No state, no I/O.

The features here are used by the 1hr observer (Phase 14.2a) to enrich
``book_at_1hr_pretrigger`` envelopes with chart-pattern observables that
can later be backtested as candidate gates. Stage 1 research at
``_tmp_analysis/eth_sr_liquidity`` found that 4h Bollinger-band position
cleanly separated the n=4 ETH-1hr cohort's one loss from the three wins
(loser at +1.36σ above the upper band, every winner at or below the
mean) — directionally consistent with "ETH stays confined within ranges"
mean-reversion. This module ships the features needed to validate that
hypothesis on a larger cohort.

Inputs:
- ``history``: list of (ts_ms, spot_price) tuples, sorted ascending by ts.
- ``ts_at``: anchor timestamp (typically the envelope's recv_ms).
- ``window_ms``: lookback window relative to ``ts_at``.

All functions are best-effort: return ``None`` when history is empty or
the window contains fewer than 2 samples.
"""

from __future__ import annotations


def _window_prices(history: list[tuple[int, float]], ts_at: int,
                    window_ms: int) -> list[float]:
    """Prices in [ts_at - window_ms, ts_at]. Caller responsible for sort."""
    ts0 = ts_at - window_ms
    return [p for t, p in history if ts0 <= t <= ts_at]


def bollinger_position(spot_at: float | None,
                        history: list[tuple[int, float]],
                        ts_at: int, window_ms: int) -> float | None:
    """Position of ``spot_at`` within the window's Bollinger band, in
    standardized-sigma units. Returns ``None`` when:
    - ``spot_at`` is None
    - The window has < 2 samples
    - The window's standard deviation is zero (degenerate)

    Convention: +1.0 means spot is at the upper band (mean + 2σ), -1.0
    means at the lower band, 0.0 means at the mean. Magnitude > 1.0 means
    outside the band. The "extreme fade" rule looks for |bb_pos| > 1.0.
    """
    if spot_at is None:
        return None
    prices = _window_prices(history, ts_at, window_ms)
    if len(prices) < 2:
        return None
    m = sum(prices) / len(prices)
    var = sum((p - m) ** 2 for p in prices) / len(prices)
    sd = var ** 0.5
    if sd <= 0:
        return None
    # Normalize so +1.0 = upper band (m + 2σ). Halve the sigma denominator.
    return (spot_at - m) / (2 * sd)


def window_high_low(history: list[tuple[int, float]],
                     ts_at: int, window_ms: int) -> tuple[float, float] | None:
    """Highest and lowest spot in the window. Returns None on empty."""
    prices = _window_prices(history, ts_at, window_ms)
    if not prices:
        return None
    return max(prices), min(prices)


def pivot_levels(spot_at: float | None,
                  history: list[tuple[int, float]],
                  ts_at: int, window_ms: int) -> dict[str, float] | None:
    """Classical daily pivot points computed over a window (typically 24h).

    pivot = (high + low + close) / 3
    R1    = 2 * pivot - low
    S1    = 2 * pivot - high

    ``close`` is taken as ``spot_at`` (caller's "now" price). Returns None
    when the window is empty or ``spot_at`` is None.
    """
    if spot_at is None:
        return None
    hl = window_high_low(history, ts_at, window_ms)
    if hl is None:
        return None
    high, low = hl
    pivot = (high + low + spot_at) / 3.0
    r1 = 2 * pivot - low
    s1 = 2 * pivot - high
    return {
        "pivot": pivot,
        "R1": r1,
        "S1": s1,
        "dist_to_R1": r1 - spot_at,
        "dist_to_S1": spot_at - s1,
        "window_high": high,
        "window_low": low,
        "window_range": high - low,
    }


def all_sr_features(spot_at: float | None,
                     history: list[tuple[int, float]],
                     ts_at: int) -> dict[str, float | None]:
    """Convenience: compute the standard feature bundle (1h/4h/24h BB
    positions + 24h pivot levels) for a single anchor."""
    out: dict[str, float | None] = {}
    H1 = 1 * 3_600_000
    H4 = 4 * 3_600_000
    H24 = 24 * 3_600_000
    out["bb_pos_1h"] = bollinger_position(spot_at, history, ts_at, H1)
    out["bb_pos_4h"] = bollinger_position(spot_at, history, ts_at, H4)
    out["bb_pos_24h"] = bollinger_position(spot_at, history, ts_at, H24)
    piv = pivot_levels(spot_at, history, ts_at, H24)
    if piv:
        for k, v in piv.items():
            out[f"pivot_{k}" if k in ("R1", "S1") else k] = v
    else:
        out["pivot"] = None
        out["pivot_R1"] = None
        out["pivot_S1"] = None
        out["dist_to_R1"] = None
        out["dist_to_S1"] = None
    return out
