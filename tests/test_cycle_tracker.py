"""Phase 11C: CycleTracker tests.

CycleTracker maintains a per-crypto rolling spot buffer and writes a
``cycle_summary`` envelope on each crypto settlement, capturing snapshots
(from FavoriteChaseStrategy.snapshot_history) + recent spot trajectory +
our position outcome.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kalshi_engine.core.events import SettlementEvent, SpotEvent
from kalshi_engine.core.types import Crypto, Side, Venue
from kalshi_engine.research.cycle_tracker import CycleTracker
from kalshi_engine.warehouse.adapters import LiveLogWriter


class _FakeStrategy:
    def __init__(self):
        self.snapshot_history: dict[str, list[dict]] = {}


class _FakeExecution:
    def __init__(self):
        self.open_positions: dict[str, dict] = {}


def _read_summaries(path: Path) -> list[dict]:
    out = []
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        if rec.get("kind") == "cycle_summary":
            out.append(rec)
    return out


def _spot(crypto: Crypto, ts_ms: int, price: float):
    return SpotEvent(crypto=crypto, venue=Venue.BITSTAMP,
                     ts_ms=ts_ms, recv_ms=ts_ms, price=price)


def _settle(ticker: str, result: Side, settle_value: float, recv_ms: int):
    return SettlementEvent(
        ticker=ticker, ts_ms=recv_ms, recv_ms=recv_ms,
        result=result, settle_value=settle_value, determined_ms=recv_ms,
    )


def test_settlement_emits_cycle_summary(tmp_path):
    """A KX*15M settlement triggers a cycle_summary envelope with the
    snapshot history + spot trajectory + position info."""
    log = LiveLogWriter(str(tmp_path / "live.jsonl"))
    strategy = _FakeStrategy()
    strategy.snapshot_history["KXBTC15M-T"] = [
        {"kind": "snapshot", "ticker": "KXBTC15M-T",
         "diagnostics": {"bb_yes": 0.85}, "recv_ms": 100},
        {"kind": "snapshot", "ticker": "KXBTC15M-T",
         "diagnostics": {"bb_yes": 0.88}, "recv_ms": 5_100},
    ]
    execution = _FakeExecution()
    execution.open_positions["KXBTC15M-T"] = {
        "side": "yes", "count": 1, "order_id": "ord_x", "entered_at_ms": 500,
    }
    tracker = CycleTracker(log, strategy, execution, [Crypto.BTC])
    # Feed 3 BTC spot ticks
    settle_ms = 1779560000000
    tracker.on_event(_spot(Crypto.BTC, settle_ms - 120_000, 75000.0))
    tracker.on_event(_spot(Crypto.BTC, settle_ms - 60_000, 75100.0))
    tracker.on_event(_spot(Crypto.BTC, settle_ms - 10_000, 75050.0))
    tracker.on_event(_settle("KXBTC15M-T", Side.YES, 1.0, settle_ms))

    summaries = _read_summaries(tmp_path / "live.jsonl")
    assert len(summaries) == 1
    s = summaries[0]
    assert s["ticker"] == "KXBTC15M-T"
    assert s["crypto"] == "BTC"
    assert s["result"] == "yes"
    assert s["settle_value"] == 1.0
    assert s["our_position"]["side"] == "yes"
    assert s["n_snapshots"] == 2
    assert len(s["snapshots"]) == 2
    assert s["snapshots"][0]["diagnostics"]["bb_yes"] == 0.85
    # All 3 spot ticks within the 3-min recent window
    assert len(s["spot_trajectory_recent"]) == 3


def test_settlement_outside_3min_window_excluded_from_trajectory(tmp_path):
    """Spot ticks older than recent_trajectory_minutes (default 3) are
    not included in spot_trajectory_recent."""
    log = LiveLogWriter(str(tmp_path / "live.jsonl"))
    strategy = _FakeStrategy()
    execution = _FakeExecution()
    tracker = CycleTracker(log, strategy, execution, [Crypto.BTC])
    settle_ms = 1779560000000
    # Two ticks, one inside 3 min, one outside
    tracker.on_event(_spot(Crypto.BTC, settle_ms - 240_000, 75000.0))  # 4 min ago - excluded
    tracker.on_event(_spot(Crypto.BTC, settle_ms - 60_000, 75100.0))    # 1 min ago - included
    tracker.on_event(_settle("KXBTC15M-T", Side.NO, 0.0, settle_ms))

    s = _read_summaries(tmp_path / "live.jsonl")[0]
    assert len(s["spot_trajectory_recent"]) == 1
    assert s["spot_trajectory_recent"][0]["price"] == 75100.0


def test_settlement_clears_snapshot_history(tmp_path):
    """After settlement, the ticker's snapshot history is freed."""
    log = LiveLogWriter(str(tmp_path / "live.jsonl"))
    strategy = _FakeStrategy()
    strategy.snapshot_history["KXBTC15M-T"] = [{"kind": "snapshot"}]
    execution = _FakeExecution()
    tracker = CycleTracker(log, strategy, execution, [Crypto.BTC])
    tracker.on_event(_settle("KXBTC15M-T", Side.NO, 0.0, 1779560000000))
    assert "KXBTC15M-T" not in strategy.snapshot_history


def test_non_crypto_settlement_ignored(tmp_path):
    """A settlement for KXMLB / KXNBA / etc. doesn't fire a summary."""
    log = LiveLogWriter(str(tmp_path / "live.jsonl"))
    strategy = _FakeStrategy()
    execution = _FakeExecution()
    tracker = CycleTracker(log, strategy, execution, [Crypto.BTC])
    tracker.on_event(_settle("KXMLBHR-TEAM-PLAYER1", Side.YES, 1.0, 1779560000000))
    assert _read_summaries(tmp_path / "live.jsonl") == []


def test_disabled_no_op(tmp_path):
    """enabled=False skips all event handling silently."""
    log = LiveLogWriter(str(tmp_path / "live.jsonl"))
    strategy = _FakeStrategy()
    execution = _FakeExecution()
    tracker = CycleTracker(log, strategy, execution, [Crypto.BTC], enabled=False)
    tracker.on_event(_spot(Crypto.BTC, 100, 75000.0))
    tracker.on_event(_settle("KXBTC15M-T", Side.YES, 1.0, 1779560000000))
    assert _read_summaries(tmp_path / "live.jsonl") == []


def test_spot_buffer_bounded_by_history_window(tmp_path):
    """Spot buffer drops events older than spot_history_minutes."""
    log = LiveLogWriter(str(tmp_path / "live.jsonl"))
    strategy = _FakeStrategy()
    execution = _FakeExecution()
    tracker = CycleTracker(log, strategy, execution, [Crypto.BTC],
                            spot_history_minutes=1)
    # Old tick (90s ago) and fresh tick
    tracker.on_event(_spot(Crypto.BTC, 0, 70000.0))
    tracker.on_event(_spot(Crypto.BTC, 90_000, 75000.0))
    # The deque should hold only the fresh tick (90s after 1-min window)
    d = tracker._spot_history["BTC"]
    assert len(d) == 1
    assert d[0][1] == 75000.0


def test_no_position_recorded_when_we_didnt_trade(tmp_path):
    """Cycle summary still fires when we held no position - our_position=None."""
    log = LiveLogWriter(str(tmp_path / "live.jsonl"))
    strategy = _FakeStrategy()
    execution = _FakeExecution()  # no open_positions
    tracker = CycleTracker(log, strategy, execution, [Crypto.BTC])
    tracker.on_event(_settle("KXBTC15M-T", Side.NO, 0.0, 1779560000000))
    s = _read_summaries(tmp_path / "live.jsonl")[0]
    assert s["our_position"] is None
