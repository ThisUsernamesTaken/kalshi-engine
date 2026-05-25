"""Phase-11A: per-cycle diagnostic snapshot tests.

When ``snapshot_interval_ms`` > 0 and a ``log_writer`` is supplied, the
strategy emits a ``snapshot`` envelope at the configured cadence during
each market's trigger window. Snapshots are independent of decision-locking
- they keep firing even after the strategy has emitted its one decision per
market, so we can study how diagnostics evolve from T+8 to T+15.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kalshi_engine.core.events import BookEvent, SpotEvent
from kalshi_engine.core.types import Crypto, Side, Venue
from kalshi_engine.strategies.favorite_chase.strategy import (
    FavoriteChaseStrategy,
)
from kalshi_engine.warehouse.adapters import LiveLogWriter


# Trigger window: open_ms+8m .. open_ms+15m (per favorite_chase rules)
OPEN_MS = 1000_000_000_000   # arbitrary
CLOSE_MS = OPEN_MS + 15 * 60_000    # 15-min cycle
T8_MS = OPEN_MS + 8 * 60_000


def _make_book(ticker, ts_ms, yes_bid=750, yes_ask=760):
    """A book event with ts/recv = ts_ms. yes_bid >= 750 makes YES the favorite."""
    no_bid = 1000 - yes_ask
    no_ask = 1000 - yes_bid
    return BookEvent(
        ticker=ticker, ts_ms=ts_ms, recv_ms=ts_ms,
        yes_bid=yes_bid, yes_ask=yes_ask,
        no_bid=no_bid, no_ask=no_ask,
        yes_levels=((yes_bid, 1.0),),
        no_levels=((no_bid, 1.0),),
    )


def _make_spot(ts_ms, price=75000.0):
    return SpotEvent(
        crypto=Crypto.BTC, venue=Venue.BITSTAMP,
        ts_ms=ts_ms, recv_ms=ts_ms, price=price,
    )


def _read_snapshots(log_path: Path) -> list[dict]:
    if not log_path.exists():
        return []
    out = []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        if rec.get("kind") == "snapshot":
            out.append(rec)
    return out


def _register_and_warm(strat, ticker, warmup_spots: int = 30):
    """Register market + feed enough spot events that vol_30m can compute."""
    strat.register_market(ticker, strike=75100.0,
                           open_ms=OPEN_MS, close_ms=CLOSE_MS)
    # vol_30m needs >= 30 minutes of spot data; sim by feeding 30 spots
    # spaced 60 s apart leading up to OPEN_MS.
    base = OPEN_MS - 30 * 60_000
    for i in range(warmup_spots):
        strat.on_event(_make_spot(base + i * 60_000, 75000.0 + i * 0.5))


def test_snapshot_disabled_emits_nothing(tmp_path):
    """snapshot_interval_ms=0 (default) -> never emits snapshots."""
    log = LiveLogWriter(str(tmp_path / "live.jsonl"))
    strat = FavoriteChaseStrategy(log_writer=log, snapshot_interval_ms=0)
    _register_and_warm(strat, "KXBTC15M-T")
    # Drive several book events inside the trigger window
    for offset in (0, 30_000, 60_000):
        strat.on_event(_make_book("KXBTC15M-T", T8_MS + offset))
    snaps = _read_snapshots(tmp_path / "live.jsonl")
    assert snaps == []


def test_snapshot_no_log_writer_no_op(tmp_path):
    """Without a log writer, snapshots are silently skipped."""
    strat = FavoriteChaseStrategy(log_writer=None, snapshot_interval_ms=5000)
    _register_and_warm(strat, "KXBTC15M-T")
    strat.on_event(_make_book("KXBTC15M-T", T8_MS))
    # No log file, no snapshots — just ensure no crash.


def test_snapshot_fires_at_cadence(tmp_path):
    """With interval=5000, snapshots fire every 5s in the trigger window."""
    log_path = tmp_path / "live.jsonl"
    log = LiveLogWriter(str(log_path))
    strat = FavoriteChaseStrategy(log_writer=log, snapshot_interval_ms=5000)
    _register_and_warm(strat, "KXBTC15M-T")
    # 11 book events spaced 1s apart starting at T+8m → 11 sec of window
    # Cadence 5000 ms → expect 3 snapshots: t=0, t=5000, t=10000
    for offset_ms in range(0, 11_000, 1_000):
        strat.on_event(_make_book("KXBTC15M-T", T8_MS + offset_ms))
    snaps = _read_snapshots(log_path)
    assert len(snaps) == 3
    # All snapshots are for the same ticker
    assert all(s["ticker"] == "KXBTC15M-T" for s in snaps)
    # Each has diagnostics populated by the model
    for s in snaps:
        d = s["diagnostics"]
        assert "bb_yes" in d
        assert "bb_div" in d
        assert "vol_30m_pct" in d
        assert "bps_margin" in d
        assert s["would_action"] in ("enter", "skip")


def test_snapshot_fires_outside_decided_lock(tmp_path):
    """Decision locks ``self.decided`` but snapshots keep firing after."""
    log_path = tmp_path / "live.jsonl"
    log = LiveLogWriter(str(log_path))
    strat = FavoriteChaseStrategy(log_writer=log, snapshot_interval_ms=5000)
    _register_and_warm(strat, "KXBTC15M-T")
    # First event: emits decision AND first snapshot
    d1 = strat.on_event(_make_book("KXBTC15M-T", T8_MS))
    assert d1 is not None  # decision fired
    # Many later events: decisions blocked, but snapshots continue
    for offset_ms in range(5_000, 30_000, 5_000):
        d = strat.on_event(_make_book("KXBTC15M-T", T8_MS + offset_ms))
        assert d is None  # decided lock holds
    snaps = _read_snapshots(log_path)
    # Snapshots: t=0, t=5000, t=10000, t=15000, t=20000, t=25000 → 6
    assert len(snaps) == 6
    # The first snapshot reports already_decided=False; later ones True
    assert snaps[0]["already_decided"] is False
    assert all(s["already_decided"] for s in snaps[1:])


def test_snapshot_outside_trigger_window_no_emit(tmp_path):
    """Snapshots only emit inside T+8 → T+15."""
    log_path = tmp_path / "live.jsonl"
    log = LiveLogWriter(str(log_path))
    strat = FavoriteChaseStrategy(log_writer=log, snapshot_interval_ms=1000)
    _register_and_warm(strat, "KXBTC15M-T")
    # T+5min (before trigger window)
    strat.on_event(_make_book("KXBTC15M-T", OPEN_MS + 5 * 60_000))
    # T+7min (still before)
    strat.on_event(_make_book("KXBTC15M-T", OPEN_MS + 7 * 60_000))
    snaps = _read_snapshots(log_path)
    assert snaps == []


def test_snapshot_per_ticker_independent_cadence(tmp_path):
    """Each ticker tracks its own snapshot cadence independently."""
    log_path = tmp_path / "live.jsonl"
    log = LiveLogWriter(str(log_path))
    strat = FavoriteChaseStrategy(log_writer=log, snapshot_interval_ms=5000)
    _register_and_warm(strat, "KXBTC15M-A")
    strat.register_market("KXBTC15M-B", strike=75100.0,
                          open_ms=OPEN_MS, close_ms=CLOSE_MS)
    # T+8 + 0, +6000: A gets snap@0, snap@6000; B gets snap@0
    strat.on_event(_make_book("KXBTC15M-A", T8_MS + 0))
    strat.on_event(_make_book("KXBTC15M-A", T8_MS + 6_000))
    strat.on_event(_make_book("KXBTC15M-B", T8_MS + 0))
    snaps = _read_snapshots(log_path)
    by_ticker = {}
    for s in snaps:
        by_ticker.setdefault(s["ticker"], []).append(s)
    assert len(by_ticker.get("KXBTC15M-A", [])) == 2
    assert len(by_ticker.get("KXBTC15M-B", [])) == 1
