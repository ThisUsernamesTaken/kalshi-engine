"""LiveLogReader / LiveLogWriter: append round-trip, range and ticker seek."""

from __future__ import annotations

from kalshi_engine.warehouse.adapters import LiveLogReader, LiveLogWriter


def test_write_read_roundtrip(tmp_path):
    path = tmp_path / "live.jsonl"
    writer = LiveLogWriter(str(path))
    events = [
        {"kind": "boot", "log_ts_ms": 1000},
        {"kind": "entry", "ticker": "KXBTC15M-X", "side": "no", "log_ts_ms": 2000},
        {"kind": "exit", "ticker": "KXBTC15M-X", "reason": "stop", "log_ts_ms": 3000},
    ]
    for ev in events:
        writer.write(ev)
    back = list(LiveLogReader(str(path)).iter())
    assert back == events


def test_range_and_ticker_seek(tmp_path):
    path = tmp_path / "live.jsonl"
    writer = LiveLogWriter(str(path))
    for ev in [
        {"kind": "boot", "log_ts_ms": 100},
        {"kind": "entry", "ticker": "AAA", "log_ts_ms": 200},
        {"kind": "entry", "ticker": "BBB", "log_ts_ms": 300},
        {"kind": "exit", "ticker": "BBB", "log_ts_ms": 400},
    ]:
        writer.write(ev)
    reader = LiveLogReader(str(path))
    assert [e["log_ts_ms"] for e in reader.iter_range(150, 350)] == [200, 300]
    assert [e["log_ts_ms"] for e in reader.iter_ticker("BBB")] == [300, 400]


def test_writer_stamps_log_ts(tmp_path):
    path = tmp_path / "live.jsonl"
    rec = LiveLogWriter(str(path)).write({"kind": "boot"})
    assert rec["log_ts_ms"] > 0
