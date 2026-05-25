"""CaptureReader: schema-version validation and 4-stream iteration."""

from __future__ import annotations

import itertools
import json

import pytest

from kalshi_engine.warehouse.adapters import CaptureReader


def test_capture_iter(capture_dir):
    reader = CaptureReader(capture_dir)
    records = list(itertools.islice(reader.iter(), 200))
    assert records, "expected capture records"
    for rec in records:
        assert rec["schema_version"] == CaptureReader.SCHEMA_VERSION
        assert rec["_stream"] in ("raw_events", "decisions", "paper_fills")


def test_capture_summaries(capture_dir):
    reader = CaptureReader(capture_dir)
    summaries = reader.summaries()
    assert summaries, "expected summary files"
    assert all(s["schema_version"] == CaptureReader.SCHEMA_VERSION for s in summaries)


def test_schema_version_mismatch_rejected_loudly(tmp_path):
    bad = tmp_path / "raw_events_29990101_000000.jsonl"
    bad.write_text(
        json.dumps({"schema_version": 999, "wall_clock_ts_ms": 1}) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="schema_version"):
        CaptureReader(str(tmp_path))
