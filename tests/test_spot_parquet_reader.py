"""SpotParquetReader: OHLC schema, 1-minute spacing, range seek."""

from __future__ import annotations

import itertools

from kalshi_engine.core.events import SpotEvent
from kalshi_engine.warehouse.adapters import SpotParquetReader


def test_ohlc_schema_and_spacing(spot_dir):
    reader = SpotParquetReader("BTC", "fusion", spot_dir)
    df = reader.frame()
    assert set(df.columns) == {"ts_ms", "open", "high", "low", "close", "volume"}
    assert len(df) > 0

    events = list(itertools.islice(reader.iter(), 100))
    assert all(isinstance(e, SpotEvent) for e in events)
    # exact 1-minute spacing, no gaps in the windowed view
    ts = [e.ts_ms for e in events]
    spacings = {b - a for a, b in zip(ts, ts[1:])}
    assert spacings == {60_000}


def test_iter_range(spot_dir):
    reader = SpotParquetReader("BTC", "fusion", spot_dir)
    first = next(reader.iter())
    end = first.ts_ms + 600_000  # ten minutes
    window = list(reader.iter_range(first.ts_ms, end))
    assert len(window) == 11  # minutes 0..10 inclusive
    for ev in window:
        assert first.ts_ms <= ev.ts_ms <= end
        assert ev.price > 0
