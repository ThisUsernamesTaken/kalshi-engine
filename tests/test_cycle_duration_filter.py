"""Phase 14.8 — cycle-duration filter for market discovery.

Defect being fixed: KXBTCD/KXETHD/KXINXU occasionally contain markets
whose cycle is NOT the expected duration. On 2026-05-26 the 1hr engine
entered KXBTCD-26MAY2717-* tickers that were 25h cycles instead of 1h.

This test validates the helper at
``kalshi_engine.research.cycle_duration_filter`` that splits markets into
(kept, skipped) by ``close_ms - open_ms <= max_minutes * 60_000``.
"""

from __future__ import annotations

import pytest

from kalshi_engine.research.cycle_duration_filter import filter_by_cycle_duration


HOUR_MS = 60 * 60_000
DAY_MS = 24 * HOUR_MS


def _mk(ticker: str, dur_ms: int, open_ms: int = 1_700_000_000_000) -> dict:
    return {
        "ticker": ticker,
        "open_ms": open_ms,
        "close_ms": open_ms + dur_ms,
    }


def test_filter_keeps_1hr_rejects_25h():
    """The exact defect case from 2026-05-26: KXBTCD mix of 1hr + 25h."""
    markets = [
        _mk("KXBTCD-26MAY2620-T100000", HOUR_MS),
        _mk("KXBTCD-26MAY2621-T100000", HOUR_MS),
        _mk("KXBTCD-26MAY2717-T100000", 25 * HOUR_MS),  # the bad one
    ]
    kept, skipped = filter_by_cycle_duration(markets, max_duration_minutes=90)
    assert len(kept) == 2
    assert len(skipped) == 1
    assert skipped[0]["ticker"] == "KXBTCD-26MAY2717-T100000"
    assert skipped[0]["_skip_dur_ms"] == 25 * HOUR_MS
    assert skipped[0]["_skip_cap_min"] == 90


def test_filter_keeps_exactly_at_cap():
    """Boundary: a market whose duration EQUALS the cap should be kept
    (<=, not <)."""
    markets = [_mk("EXACT-90M", 90 * 60_000)]
    kept, skipped = filter_by_cycle_duration(markets, max_duration_minutes=90)
    assert len(kept) == 1
    assert len(skipped) == 0


def test_filter_rejects_one_minute_over_cap():
    markets = [_mk("ONE-OVER", 91 * 60_000)]
    kept, skipped = filter_by_cycle_duration(markets, max_duration_minutes=90)
    assert len(kept) == 0
    assert len(skipped) == 1


def test_filter_rejects_zero_or_negative_duration():
    """close <= open means invalid market — must NOT be kept."""
    base = 1_700_000_000_000
    markets = [
        {"ticker": "ZERO", "open_ms": base, "close_ms": base},
        {"ticker": "NEG", "open_ms": base + 1000, "close_ms": base},
    ]
    kept, skipped = filter_by_cycle_duration(markets, max_duration_minutes=90)
    assert len(kept) == 0
    assert len(skipped) == 2


def test_filter_handles_missing_keys_as_zero():
    markets = [
        {"ticker": "NO-OPEN", "close_ms": 1_700_000_000_000},
        {"ticker": "NO-CLOSE", "open_ms": 1_700_000_000_000},
        {"ticker": "EMPTY"},
    ]
    kept, skipped = filter_by_cycle_duration(markets, max_duration_minutes=90)
    assert len(kept) == 0
    assert len(skipped) == 3


def test_filter_handles_bad_types_gracefully():
    markets = [
        {"ticker": "STR-OPEN", "open_ms": "not-a-number", "close_ms": 1},
    ]
    kept, skipped = filter_by_cycle_duration(markets, max_duration_minutes=90)
    assert kept == []
    assert len(skipped) == 1


class _Collecting:
    def __init__(self):
        self.events: list[dict] = []
    def write(self, env):
        self.events.append(env)


def test_filter_emits_skip_log_entries():
    log = _Collecting()
    markets = [
        _mk("KXBTCD-OK", HOUR_MS),
        _mk("KXBTCD-25H", 25 * HOUR_MS),
    ]
    kept, skipped = filter_by_cycle_duration(
        markets, max_duration_minutes=90,
        log_writer=log, series_label="KXBTCD",
    )
    assert len(kept) == 1
    assert len(skipped) == 1
    skip_logs = [e for e in log.events
                  if e.get("kind") == "discovery_skip_long_cycle"]
    assert len(skip_logs) == 1
    e = skip_logs[0]
    assert e["series"] == "KXBTCD"
    assert e["ticker"] == "KXBTCD-25H"
    assert e["duration_ms"] == 25 * HOUR_MS
    assert e["duration_minutes"] == 25 * 60.0
    assert e["cap_minutes"] == 90


def test_filter_15m_cap_rejects_1hr_market():
    """The 15m engine uses a tighter 18-minute cap. A 1hr KXBTCD market
    must be rejected."""
    markets = [
        _mk("KXBTC15M-OK", 15 * 60_000),
        _mk("KXBTCD-1HR", HOUR_MS),
    ]
    kept, skipped = filter_by_cycle_duration(markets, max_duration_minutes=18)
    assert len(kept) == 1
    assert kept[0]["ticker"] == "KXBTC15M-OK"
    assert len(skipped) == 1


def test_filter_inxu_90m_cap_rejects_daily():
    """A daily KXINXU market would be 24h+ — must be rejected by the 90m cap."""
    markets = [
        _mk("KXINXU-1HR", HOUR_MS),
        _mk("KXINXU-DAILY", DAY_MS),
    ]
    kept, skipped = filter_by_cycle_duration(markets, max_duration_minutes=90)
    assert len(kept) == 1
    assert kept[0]["ticker"] == "KXINXU-1HR"
    assert skipped[0]["ticker"] == "KXINXU-DAILY"


def test_filter_preserves_market_metadata():
    """Extra keys on the input dicts must round-trip into the kept output."""
    m = {
        "ticker": "KX-X",
        "open_ms": 1_700_000_000_000,
        "close_ms": 1_700_000_000_000 + HOUR_MS,
        "strike": 100_000.0,
        "series": "KXBTCD",
        "custom_field": "preserved",
    }
    kept, skipped = filter_by_cycle_duration([m], max_duration_minutes=90)
    assert kept[0]["strike"] == 100_000.0
    assert kept[0]["series"] == "KXBTCD"
    assert kept[0]["custom_field"] == "preserved"
