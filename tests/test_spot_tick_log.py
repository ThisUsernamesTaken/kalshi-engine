"""Phase 11B: spot tick recorder tests.

When ``--log-spot-ticks`` is set, every SpotEvent that flows through the
run loop's _route function is written as a ``spot_tick`` envelope to the
JSONL. Volume is high (~25 ticks/sec across all 5 cryptos = ~2M/day);
intended for research, not production.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kalshi_engine.bin.live import _route
from kalshi_engine.core.events import BookEvent, SpotEvent
from kalshi_engine.core.types import Crypto, Venue
from kalshi_engine.risk.envelope import RiskState
from kalshi_engine.strategies.favorite_chase.strategy import (
    FavoriteChaseStrategy,
)
from kalshi_engine.warehouse.adapters import LiveLogWriter


def _spot(crypto=Crypto.BTC, ts_ms=1_000_000_000_000, price=75000.0):
    return SpotEvent(
        crypto=crypto, venue=Venue.BITSTAMP,
        ts_ms=ts_ms, recv_ms=ts_ms, price=price,
    )


def _book(ts_ms=1_000_000_000_000):
    return BookEvent(
        ticker="KXBTC15M-T", ts_ms=ts_ms, recv_ms=ts_ms,
        yes_bid=750, yes_ask=760, no_bid=240, no_ask=250,
        yes_levels=(), no_levels=(),
    )


def _read_spot_ticks(log_path: Path) -> list[dict]:
    if not log_path.exists():
        return []
    out = []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        if rec.get("kind") == "spot_tick":
            out.append(rec)
    return out


def test_spot_ticks_logged_when_flag_set(tmp_path):
    """log_spot_ticks=True writes a spot_tick envelope per SpotEvent."""
    log = LiveLogWriter(str(tmp_path / "live.jsonl"))
    strategy = FavoriteChaseStrategy()
    risk_state = RiskState()
    _route(_spot(Crypto.BTC, 100, 75000.0), strategy, risk_state, log,
           cycle_tracker=None, log_spot_ticks=True)
    _route(_spot(Crypto.ETH, 200, 2050.0), strategy, risk_state, log,
           cycle_tracker=None, log_spot_ticks=True)
    _route(_spot(Crypto.SOL, 300, 84.0), strategy, risk_state, log,
           cycle_tracker=None, log_spot_ticks=True)
    ticks = _read_spot_ticks(tmp_path / "live.jsonl")
    assert len(ticks) == 3
    assert {t["crypto"] for t in ticks} == {"BTC", "ETH", "SOL"}
    assert ticks[0]["price"] == 75000.0
    assert ticks[0]["venue"] == "bitstamp"
    assert ticks[0]["ts_ms"] == 100


def test_spot_ticks_not_logged_when_flag_unset(tmp_path):
    """log_spot_ticks=False (default) writes no spot_tick envelopes."""
    log = LiveLogWriter(str(tmp_path / "live.jsonl"))
    strategy = FavoriteChaseStrategy()
    risk_state = RiskState()
    _route(_spot(Crypto.BTC, 100, 75000.0), strategy, risk_state, log,
           cycle_tracker=None, log_spot_ticks=False)
    ticks = _read_spot_ticks(tmp_path / "live.jsonl")
    assert ticks == []


def test_book_events_never_logged_as_spot_tick(tmp_path):
    """BookEvent must not produce a spot_tick envelope."""
    log = LiveLogWriter(str(tmp_path / "live.jsonl"))
    strategy = FavoriteChaseStrategy()
    risk_state = RiskState()
    _route(_book(100), strategy, risk_state, log,
           cycle_tracker=None, log_spot_ticks=True)
    assert _read_spot_ticks(tmp_path / "live.jsonl") == []
