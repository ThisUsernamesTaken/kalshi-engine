"""Phase 14.8 — cycle-duration filter for market discovery.

Defect this fixes: Kalshi's KXBTCD / KXETHD / KXINXU series occasionally
contain markets whose cycle is NOT the expected duration. We discovered
this on 2026-05-26 when the 1hr engine entered KXBTCD-26MAY2717-* tickers
that turned out to be 25-hour cycles (open 20:00Z today, close 21:00Z
tomorrow) instead of the assumed 1-hour cycles. The model evaluated them
with τ=0.5h math while the real τ was 24.5h, mispricing every gate.

This module provides a single helper that filters a market list to those
whose cycle duration ≤ a configurable cap.
"""

from __future__ import annotations


def filter_by_cycle_duration(
    markets: list[dict],
    max_duration_minutes: int,
    log_writer=None,
    series_label: str = "",
) -> tuple[list[dict], list[dict]]:
    """Split ``markets`` into (kept, skipped) by cycle duration.

    Each market dict must have integer ``open_ms`` and ``close_ms`` keys.
    Markets with ``close_ms - open_ms <= max_duration_minutes * 60_000`` are
    kept; the rest are recorded as skipped with an explanatory note.

    If ``log_writer`` is provided, a ``discovery_skip_long_cycle`` entry is
    written for each skipped market so the JSONL log carries the audit
    trail.
    """
    cap_ms = int(max_duration_minutes) * 60_000
    kept: list[dict] = []
    skipped: list[dict] = []
    for m in markets:
        try:
            o = int(m.get("open_ms", 0))
            c = int(m.get("close_ms", 0))
        except (TypeError, ValueError):
            o = c = 0
        dur = c - o
        if 0 < dur <= cap_ms:
            kept.append(m)
        else:
            skipped.append({**m, "_skip_dur_ms": dur,
                             "_skip_cap_min": max_duration_minutes})
            if log_writer is not None:
                log_writer.write({
                    "kind": "discovery_skip_long_cycle",
                    "series": series_label,
                    "ticker": m.get("ticker", ""),
                    "duration_ms": dur,
                    "duration_minutes": dur / 60_000.0 if dur > 0 else None,
                    "cap_minutes": max_duration_minutes,
                })
    return kept, skipped
