"""Phase 14.11 - sleep+retry behavior in KXINXU engines.

When KXINXU has 0 active markets (between RTH-end and next RTH), the
engines must sleep and retry rather than exit with code 3 (which under
NSSM daemonization would restart-loop until throttle-paused).
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from kalshi_engine.bin.observe_inxu import _discover_markets_with_retry
from kalshi_engine.bin.live_inxu_v0 import _refresh_until_markets_with_retry


# ---- observe_inxu helper ------------------------------------------------

@pytest.mark.asyncio
async def test_discover_with_retry_returns_immediately_when_markets_present(
    monkeypatch,
):
    """First discovery returns markets -> no retry, no log, no sleep."""
    log = MagicMock(); log.writes = []
    log.write = lambda p: log.writes.append(p)
    sample = [{"ticker": "KXINXU-X-T1", "strike": 1, "open_ms": 0,
                "close_ms": 1_000, "series": "KXINXU", "equity": "SPX"}]

    async def fake_discover(*args, **kwargs):
        return sample
    monkeypatch.setattr("kalshi_engine.bin.observe_inxu._discover_markets",
                        fake_discover)

    slept = []
    async def fake_sleep(s): slept.append(s)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    result = await _discover_markets_with_retry(
        client=MagicMock(), equities=[], log=log,
        retry_seconds=300, process_label="inxu_observer",
    )
    assert result == sample
    assert slept == []  # no sleeps
    assert not any(e.get("kind") == "no_markets_waiting" for e in log.writes)


@pytest.mark.asyncio
async def test_discover_with_retry_loops_until_markets_appear(monkeypatch):
    """First two attempts return [], third returns real markets.
    Expect 2 no_markets_waiting logs, 2 sleeps, then result."""
    log = MagicMock(); log.writes = []
    log.write = lambda p: log.writes.append(p)
    sample = [{"ticker": "KXINXU-X-T2", "strike": 2, "open_ms": 0,
                "close_ms": 1_000, "series": "KXINXU", "equity": "SPX"}]

    calls = {"n": 0}
    async def fake_discover(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] <= 2:
            return []
        return sample
    monkeypatch.setattr("kalshi_engine.bin.observe_inxu._discover_markets",
                        fake_discover)

    slept = []
    async def fake_sleep(s): slept.append(s)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    result = await _discover_markets_with_retry(
        client=MagicMock(), equities=[], log=log,
        retry_seconds=300, process_label="inxu_observer",
    )
    assert result == sample
    assert calls["n"] == 3
    assert slept == [300, 300]  # exactly two sleep-300s before success
    waits = [e for e in log.writes if e.get("kind") == "no_markets_waiting"]
    assert len(waits) == 2
    assert waits[0]["retry_attempts"] == 1
    assert waits[1]["retry_attempts"] == 2
    assert all(w["process"] == "inxu_observer" for w in waits)
    assert all(w["next_retry_s"] == 300 for w in waits)


@pytest.mark.asyncio
async def test_discover_with_retry_returns_none_when_cancelled(monkeypatch):
    """Cancellation during sleep returns None for graceful shutdown."""
    log = MagicMock(); log.writes = []
    log.write = lambda p: log.writes.append(p)

    async def fake_discover(*args, **kwargs):
        return []
    monkeypatch.setattr("kalshi_engine.bin.observe_inxu._discover_markets",
                        fake_discover)

    async def fake_sleep(s):
        raise asyncio.CancelledError()
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    result = await _discover_markets_with_retry(
        client=MagicMock(), equities=[], log=log,
        retry_seconds=300, process_label="inxu_observer",
    )
    assert result is None
    waits = [e for e in log.writes if e.get("kind") == "no_markets_waiting"]
    assert len(waits) == 1


@pytest.mark.asyncio
async def test_discover_with_retry_respects_process_label(monkeypatch):
    """The process_label parameter flows through to the log envelope."""
    log = MagicMock(); log.writes = []
    log.write = lambda p: log.writes.append(p)
    calls = {"n": 0}

    async def fake_discover(*args, **kwargs):
        calls["n"] += 1
        return [] if calls["n"] == 1 else [{"ticker": "X", "strike": 1,
                                              "open_ms": 0, "close_ms": 1,
                                              "series": "S", "equity": "SPX"}]
    monkeypatch.setattr("kalshi_engine.bin.observe_inxu._discover_markets",
                        fake_discover)
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())

    await _discover_markets_with_retry(
        client=MagicMock(), equities=[], log=log,
        retry_seconds=60, process_label="custom_label",
    )
    waits = [e for e in log.writes if e.get("kind") == "no_markets_waiting"]
    assert waits[0]["process"] == "custom_label"
    assert waits[0]["next_retry_s"] == 60


# ---- live_inxu_v0 helper -------------------------------------------------

@pytest.mark.asyncio
async def test_refresh_until_markets_returns_immediately_when_populated(
    monkeypatch,
):
    """First refresh populates shim.markets - return False (no cancel)."""
    log = MagicMock(); log.writes = []
    log.write = lambda p: log.writes.append(p)
    shim = MagicMock()
    shim.markets = {}

    async def fake_refresh():
        shim.markets = {"KXINXU-1": {"strike": 1}}
    shim.refresh_markets = fake_refresh

    slept = []
    async def fake_sleep(s): slept.append(s)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    cancelled = await _refresh_until_markets_with_retry(
        shim, log, retry_seconds=300, process_label="live_inxu_v0",
    )
    assert cancelled is False
    assert slept == []
    assert not any(e.get("kind") == "no_markets_waiting" for e in log.writes)


@pytest.mark.asyncio
async def test_refresh_until_markets_retries_until_populated(monkeypatch):
    """First two refreshes leave shim.markets empty, third populates.
    Expect 2 no_markets_waiting logs, 2 sleeps."""
    log = MagicMock(); log.writes = []
    log.write = lambda p: log.writes.append(p)
    shim = MagicMock()
    shim.markets = {}

    calls = {"n": 0}
    async def fake_refresh():
        calls["n"] += 1
        if calls["n"] >= 3:
            shim.markets = {"KXINXU-1": {"strike": 1}}
    shim.refresh_markets = fake_refresh

    slept = []
    async def fake_sleep(s): slept.append(s)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    cancelled = await _refresh_until_markets_with_retry(
        shim, log, retry_seconds=300, process_label="live_inxu_v0",
    )
    assert cancelled is False
    assert calls["n"] == 3
    assert slept == [300, 300]
    waits = [e for e in log.writes if e.get("kind") == "no_markets_waiting"]
    assert len(waits) == 2
    assert waits[0]["retry_attempts"] == 1
    assert waits[1]["retry_attempts"] == 2
    assert all(w["process"] == "live_inxu_v0" for w in waits)


@pytest.mark.asyncio
async def test_refresh_until_markets_cancelled_returns_true(monkeypatch):
    """Cancellation during sleep returns True so caller exits cleanly."""
    log = MagicMock(); log.writes = []
    log.write = lambda p: log.writes.append(p)
    shim = MagicMock()
    shim.markets = {}

    async def fake_refresh():
        pass  # never populates
    shim.refresh_markets = fake_refresh

    async def fake_sleep(s):
        raise asyncio.CancelledError()
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    cancelled = await _refresh_until_markets_with_retry(
        shim, log, retry_seconds=300, process_label="live_inxu_v0",
    )
    assert cancelled is True
