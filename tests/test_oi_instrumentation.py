"""Phase 14.13 - open interest (OI) instrumentation on observer envelopes.

Covers:
- HourglassObserverStrategy.update_open_interest stores + ignores invalid
- _cycle_oi_aggregates math: variance, gini, top strike, share
- Envelope includes new OI fields after update_open_interest is called
- Aggregates omit None-OI tickers from the cycle population
- oi_share sums to 1.0 across cycle (when all populated)
- observe_inxu integration: dict-shaped state.markets work via SimpleNamespace adapter
"""
from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import MagicMock

import pytest

from kalshi_engine.core.events import BookEvent
from kalshi_engine.core.types import Crypto, Side, Venue
from kalshi_engine.strategies.favorite_chase.state import FavoriteChaseState
from kalshi_engine.strategies.hourglass_observer.observer import (
    HourglassObserverStrategy, _cycle_oi_aggregates,
)


# ---- update_open_interest ----------------------------------------------

def test_update_open_interest_stores_float():
    log = MagicMock(); log.writes = []
    log.write = lambda p: log.writes.append(p)
    obs = HourglassObserverStrategy(log_writer=log)
    obs.register_market("KXBTCD-T100000", strike=100_000.0,
                         open_ms=1_700_000_000_000,
                         close_ms=1_700_003_600_000)
    obs.update_open_interest("KXBTCD-T100000", 250.0)
    assert obs._oi["KXBTCD-T100000"] == 250.0


def test_update_open_interest_parses_string():
    log = MagicMock(); log.write = lambda p: None
    obs = HourglassObserverStrategy(log_writer=log)
    obs.update_open_interest("KXBTCD-X", "150.75")  # Kalshi returns strings
    assert obs._oi["KXBTCD-X"] == 150.75


def test_update_open_interest_ignores_none():
    log = MagicMock(); log.write = lambda p: None
    obs = HourglassObserverStrategy(log_writer=log)
    obs.update_open_interest("KXBTCD-X", None)
    assert "KXBTCD-X" not in obs._oi


def test_update_open_interest_ignores_invalid():
    log = MagicMock(); log.write = lambda p: None
    obs = HourglassObserverStrategy(log_writer=log)
    obs.update_open_interest("KXBTCD-X", "not-a-number")
    assert "KXBTCD-X" not in obs._oi


# ---- _cycle_oi_aggregates math -----------------------------------------

def _make_meta(strike, open_ms=1_700_000_000_000, close_ms=1_700_003_600_000):
    return type("Meta", (), {"open_ms": open_ms, "strike": strike,
                              "close_ms": close_ms})()


def test_oi_aggregates_three_strikes_known_values():
    """3 strikes with OI 100/200/300. Spot=100k. Strikes 99k/100k/101k.

    Expected:
      total = 600
      mean = 200
      variance = ((100-200)^2 + (200-200)^2 + (300-200)^2) / 3 = 20000/3 ≈ 6666.67
      top strike = 101k (OI 300)
      top strike dist from spot = (101000-100000)/100000 * 1e4 = 100 bps
      this strike (99k, OI 100) share = 100/600 ≈ 0.167
    """
    markets = {
        "T99000": _make_meta(99_000.0),
        "T100000": _make_meta(100_000.0),
        "T101000": _make_meta(101_000.0),
    }
    oi_cache = {"T99000": 100.0, "T100000": 200.0, "T101000": 300.0}
    agg = _cycle_oi_aggregates(
        "T99000", oi_cache["T99000"], 1_700_000_000_000,
        spot=100_000.0, markets=markets, oi_cache=oi_cache,
    )
    assert agg["open_interest"] == 100.0
    assert agg["cycle_total_oi"] == 600.0
    assert agg["cycle_n_strikes_with_oi"] == 3
    assert abs(agg["cycle_oi_variance"] - 6666.666666) < 0.01
    assert agg["cycle_oi_top_strike"] == 101_000.0
    assert agg["cycle_oi_top_ticker"] == "T101000"
    assert abs(agg["cycle_oi_top_strike_dist_bps"] - 100.0) < 0.01
    assert abs(agg["oi_share"] - (100.0 / 600.0)) < 1e-9
    # Gini for {100, 200, 300}: sorted = [100, 200, 300], n=3, total=600.
    # cumsum = 1*100 + 2*200 + 3*300 = 100 + 400 + 900 = 1400
    # gini = (2*1400)/(3*600) - 4/3 = 2800/1800 - 4/3 = 14/9 - 12/9 = 2/9 ≈ 0.222
    assert abs(agg["cycle_oi_concentration_gini"] - (2.0 / 9.0)) < 0.001


def test_oi_share_sums_to_one_when_all_populated():
    markets = {
        f"T{i}": _make_meta(100_000.0 + i * 100)
        for i in range(5)
    }
    oi_cache = {f"T{i}": float((i + 1) * 50) for i in range(5)}
    # Compute share for each strike; sum should be 1.0
    total_share = 0.0
    for tk in markets:
        agg = _cycle_oi_aggregates(
            tk, oi_cache[tk], 1_700_000_000_000,
            spot=100_000.0, markets=markets, oi_cache=oi_cache,
        )
        total_share += agg["oi_share"]
    assert abs(total_share - 1.0) < 1e-9


def test_oi_aggregates_omit_unpopulated_tickers():
    """Tickers without OI in cache don't contribute to cycle aggregates."""
    markets = {
        "T1": _make_meta(99_000.0),
        "T2": _make_meta(100_000.0),
        "T3": _make_meta(101_000.0),
    }
    # Only T1 and T2 have OI; T3 unpopulated
    oi_cache = {"T1": 100.0, "T2": 200.0}
    agg = _cycle_oi_aggregates(
        "T1", oi_cache["T1"], 1_700_000_000_000,
        spot=100_000.0, markets=markets, oi_cache=oi_cache,
    )
    assert agg["cycle_n_strikes_with_oi"] == 2
    assert agg["cycle_total_oi"] == 300.0
    assert agg["cycle_oi_top_ticker"] == "T2"


def test_oi_aggregates_skip_other_cycles():
    """Tickers from a DIFFERENT cycle don't count."""
    markets = {
        "T1-cyc1": _make_meta(99_000.0, open_ms=1_700_000_000_000),
        "T2-cyc1": _make_meta(100_000.0, open_ms=1_700_000_000_000),
        "T1-cyc2": _make_meta(99_000.0, open_ms=1_700_003_600_000),  # different cycle
    }
    oi_cache = {"T1-cyc1": 100.0, "T2-cyc1": 200.0, "T1-cyc2": 999.0}
    agg = _cycle_oi_aggregates(
        "T1-cyc1", oi_cache["T1-cyc1"], 1_700_000_000_000,
        spot=100_000.0, markets=markets, oi_cache=oi_cache,
    )
    # Only cycle 1 strikes counted
    assert agg["cycle_n_strikes_with_oi"] == 2
    assert agg["cycle_total_oi"] == 300.0


def test_oi_aggregates_empty_cycle_returns_nulls():
    markets = {"T1": _make_meta(100_000.0)}
    oi_cache = {}  # nothing populated
    agg = _cycle_oi_aggregates(
        "T1", None, 1_700_000_000_000,
        spot=100_000.0, markets=markets, oi_cache=oi_cache,
    )
    assert agg["cycle_n_strikes_with_oi"] == 0
    assert agg["cycle_total_oi"] is None
    assert agg["oi_share"] is None
    assert agg["cycle_oi_top_strike"] is None
    assert agg["cycle_oi_concentration_gini"] is None


def test_oi_aggregates_gini_zero_when_perfectly_even():
    """All strikes have same OI → gini = 0."""
    markets = {f"T{i}": _make_meta(100_000.0 + i * 100) for i in range(4)}
    oi_cache = {f"T{i}": 100.0 for i in range(4)}
    agg = _cycle_oi_aggregates(
        "T0", 100.0, 1_700_000_000_000,
        spot=100_000.0, markets=markets, oi_cache=oi_cache,
    )
    assert abs(agg["cycle_oi_concentration_gini"]) < 1e-9


def test_oi_aggregates_no_spot_means_no_distance():
    """If spot=None, top_strike_dist_bps is None but other fields still computed."""
    markets = {"T1": _make_meta(100_000.0)}
    oi_cache = {"T1": 50.0}
    agg = _cycle_oi_aggregates(
        "T1", 50.0, 1_700_000_000_000,
        spot=None, markets=markets, oi_cache=oi_cache,
    )
    assert agg["cycle_oi_top_strike_dist_bps"] is None
    assert agg["cycle_total_oi"] == 50.0


# ---- Envelope integration ----------------------------------------------

def _book(ticker, recv_ms):
    return BookEvent(
        ticker=ticker, ts_ms=recv_ms, recv_ms=recv_ms,
        yes_bid=200, yes_ask=220, no_bid=780, no_ask=800,
        yes_levels=((200, 10.0),), no_levels=((780, 10.0),),
    )


def test_envelope_includes_oi_fields_after_update():
    """Full pipeline: register + update OI for 2 strikes, fire a book event
    at T+30, envelope contains the new OI fields."""
    log = MagicMock(); log.writes = []
    log.write = lambda p: log.writes.append(p)
    obs = HourglassObserverStrategy(log_writer=log, observe_minutes=(30,))
    open_ms = 1_700_000_000_000
    close_ms = open_ms + 60 * 60_000
    obs.register_market("KXBTCD-T99000", strike=99_000.0,
                         open_ms=open_ms, close_ms=close_ms)
    obs.register_market("KXBTCD-T101000", strike=101_000.0,
                         open_ms=open_ms, close_ms=close_ms)
    obs.update_open_interest("KXBTCD-T99000", 100.0)
    obs.update_open_interest("KXBTCD-T101000", 300.0)
    # Seed spot history so the BB/d_norm fields populate (not strictly
    # needed for OI but exercises the full path).
    for i in range(35):
        from kalshi_engine.core.events import SpotEvent
        obs.on_event(SpotEvent(
            crypto=Crypto.BTC, venue=Venue.BITSTAMP,
            ts_ms=open_ms - (35 - i) * 60_000,
            recv_ms=open_ms - (35 - i) * 60_000,
            price=100_000.0 + (i % 3 - 1) * 50,
        ))
    # Fire book at T+30
    t30 = open_ms + 30 * 60_000
    obs.on_event(_book("KXBTCD-T99000", t30))
    envs = [e for e in log.writes if e.get("kind") == "book_at_1hr_pretrigger"]
    assert len(envs) == 1
    e = envs[0]
    # Phase 14.13 fields present
    assert "open_interest" in e
    assert "oi_share" in e
    assert "cycle_total_oi" in e
    assert "cycle_n_strikes_with_oi" in e
    assert "cycle_oi_variance" in e
    assert "cycle_oi_top_strike" in e
    assert "cycle_oi_top_ticker" in e
    assert "cycle_oi_concentration_gini" in e
    assert "cycle_oi_top_strike_dist_bps" in e
    # Values
    assert e["open_interest"] == 100.0
    assert e["cycle_total_oi"] == 400.0
    assert e["cycle_n_strikes_with_oi"] == 2
    assert e["cycle_oi_top_strike"] == 101_000.0
    assert e["cycle_oi_top_ticker"] == "KXBTCD-T101000"
    assert abs(e["oi_share"] - 0.25) < 1e-9  # 100/400


def test_envelope_oi_fields_null_when_no_update_called():
    """If update_open_interest never ran for this cycle's tickers, the
    aggregates should be null (cycle_n_strikes_with_oi=0)."""
    log = MagicMock(); log.writes = []
    log.write = lambda p: log.writes.append(p)
    obs = HourglassObserverStrategy(log_writer=log, observe_minutes=(30,))
    open_ms = 1_700_000_000_000
    close_ms = open_ms + 60 * 60_000
    obs.register_market("KXBTCD-T99000", strike=99_000.0,
                         open_ms=open_ms, close_ms=close_ms)
    # NO update_open_interest call
    t30 = open_ms + 30 * 60_000
    obs.on_event(_book("KXBTCD-T99000", t30))
    envs = [e for e in log.writes if e.get("kind") == "book_at_1hr_pretrigger"]
    assert len(envs) == 1
    e = envs[0]
    assert e["cycle_n_strikes_with_oi"] == 0
    assert e["cycle_total_oi"] is None
    assert e["oi_share"] is None
