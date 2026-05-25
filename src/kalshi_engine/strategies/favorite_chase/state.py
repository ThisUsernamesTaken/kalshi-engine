"""Per-crypto favorite-chase strategy state.

Holds a rolling spot history for the realized-volatility signal and a longer
volatility history for percentile ranking, plus the constant-vol Brownian-
bridge fair value. The volatility computation replicates the Phase 4 analysis
(winner_feature_importance.py: minute_series + rms_vol_bps) exactly, so its
cutpoints transfer unchanged.
"""

from __future__ import annotations

import bisect
import math
from statistics import NormalDist

from kalshi_engine.core.events import BookEvent, SpotEvent

_SPOT_BUFFER_MS = 32 * 60_000            # keep slightly >30m so vol's first sample resolves
_VOL_WINDOW_MS = 30 * 60_000             # realized-vol lookback
_VOL_HISTORY_MS = int(12.5 * 3_600_000)  # 12.5-hour rolling vol-30m history
_VOL_RECORD_INTERVAL_MS = 60_000         # record vol_30m into history at most 1/min
_NORM = NormalDist()


def _spot_at(ts_arr, px_arr, t):
    """Last spot at or before time t (replicates Phase 4 spot_at)."""
    i = bisect.bisect_right(ts_arr, t) - 1
    return px_arr[i] if i >= 0 else None


def _minute_series(ts_arr, px_arr, t0, t1):
    """1-minute spot samples over [t0, t1] (replicates Phase 4 minute_series)."""
    out = []
    t = t0
    while t < t1:
        s = _spot_at(ts_arr, px_arr, t)
        if s is not None:
            out.append((t, s))
        t += 60_000
    se = _spot_at(ts_arr, px_arr, t1)
    if se is not None:
        out.append((t1, se))
    return out


def _rms_vol_bps(series):
    """RMS of 1-minute log returns in bps/min (replicates Phase 4 rms_vol_bps)."""
    if len(series) < 2:
        return None
    rets = []
    for (t0, s0), (t1, s1) in zip(series, series[1:]):
        if s0 > 0 and s1 > 0 and t1 > t0:
            dt_min = (t1 - t0) / 60_000.0
            rets.append(math.log(s1 / s0) / math.sqrt(dt_min))
    if not rets:
        return None
    return math.sqrt(sum(r * r for r in rets) / len(rets)) * 1e4


class FavoriteChaseState:
    """Rolling per-crypto state: spot/vol buffers + Brownian-bridge fair value."""

    def __init__(self, crypto: str) -> None:
        self.crypto = crypto
        self.spot_buffer: list[tuple[int, float]] = []
        self.vol_history_buffer: list[tuple[int, float]] = []
        self.latest_book: BookEvent | None = None
        self._last_vol_record_ms = 0

    # -- ingestion -------------------------------------------------------
    def update_spot(self, spot_event: SpotEvent) -> None:
        """Append a spot tick, trim the buffer, and (<=1/min) record vol_30m."""
        self.spot_buffer.append((spot_event.ts_ms, spot_event.price))
        cutoff = spot_event.ts_ms - _SPOT_BUFFER_MS
        self.spot_buffer = [(t, s) for t, s in self.spot_buffer if t >= cutoff]
        if spot_event.ts_ms - self._last_vol_record_ms >= _VOL_RECORD_INTERVAL_MS:
            vol = self.vol_30m()
            if vol is not None:
                self.vol_history_buffer.append((spot_event.ts_ms, vol))
                vcut = spot_event.ts_ms - _VOL_HISTORY_MS
                self.vol_history_buffer = [
                    (t, v) for t, v in self.vol_history_buffer if t >= vcut
                ]
            self._last_vol_record_ms = spot_event.ts_ms

    def update_book(self, book_event: BookEvent) -> None:
        """Store the latest order-book snapshot."""
        self.latest_book = book_event

    # -- signals ---------------------------------------------------------
    def vol_30m(self) -> float | None:
        """Realized 30-minute volatility in bps/min over the spot buffer.

        RMS of 1-minute log returns x 1e4 - matches the Phase 4 computation.
        Returns None when there is too little spot history.
        """
        if len(self.spot_buffer) < 2:
            return None
        ts = [t for t, _ in self.spot_buffer]
        px = [s for _, s in self.spot_buffer]
        end = ts[-1]
        return _rms_vol_bps(_minute_series(ts, px, end - _VOL_WINDOW_MS, end))

    def vol_30m_percentile(self, value: float) -> float:
        """Percentile rank of `value` within the 12.5h vol-30m history (0-1).

        Returns 0.5 (neutral) when no history has accumulated yet.
        """
        hist = [v for _, v in self.vol_history_buffer]
        if not hist:
            return 0.5
        return sum(1 for v in hist if v < value) / len(hist)

    # -- fair value ------------------------------------------------------
    def bb_fair(self, spot: float, strike: float, sigma: float, tau: float) -> float:
        """Constant-vol Brownian-bridge fair P(YES).

        bb_fair = Phi( ln(spot / strike) / (sigma * sqrt(tau)) ), where `sigma`
        is per-minute fractional volatility and `tau` is minutes to close.
        Degenerate inputs collapse to the deterministic limit (1.0 / 0.0).
        """
        if spot <= 0 or strike <= 0:
            return 0.5
        if sigma <= 0 or tau <= 0:
            return 1.0 if spot >= strike else 0.0
        z = math.log(spot / strike) / (sigma * math.sqrt(tau))
        return _NORM.cdf(z)

    def bb_div(self, favorite_mid_decicents: float, bb_fair: float) -> float:
        """Model-vs-market divergence: market-implied prob minus BB fair.

        `bb_fair` must be the fair probability for the FAVORITE's side.
        Positive => the market prices the favorite richer than the BB model.
        """
        return (favorite_mid_decicents / 1000.0) - bb_fair

    def latest_spot(self) -> float | None:
        """Most recent spot price, or None if no spot tick has been seen."""
        return self.spot_buffer[-1][1] if self.spot_buffer else None
