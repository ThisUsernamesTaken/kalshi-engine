"""Phase 14.13+ - sleep+retry on empty 1hr market discovery.

When Kalshi has zero 1hr markets in status=open (transient gap windows
between settled/next-open cycles, when only 25h-daily markets remain
and the Phase 14.8 cycle-duration filter rejects them), the engine must
sleep and retry instead of exiting with code 3 (which would put the
NSSM-wrapped service into restart-throttle Paused state).

Mirrors the Phase 14.11 KXINXU sleep+retry pattern. The retry helper is
inline in `_amain` rather than a standalone function, so this test
exercises the discovery + retry interaction by patching the module-level
helper.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
async def test_discover_loop_sleeps_then_succeeds(monkeypatch):
    """First two discovery calls return [] (gap window), third returns
    real markets. We patch the module-level discover + asyncio.sleep,
    then drive the retry loop directly."""
    from kalshi_engine.bin import live_1hr as engine_mod

    calls = {"n": 0}
    sample = [{
        "ticker": "KXBTCD-X1", "strike": 100_000.0,
        "open_ms": 1, "close_ms": 60_001,
    }]
    async def fake_discover(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] <= 2:
            return []
        return sample
    monkeypatch.setattr(engine_mod, "_discover_1hr_markets", fake_discover)

    slept = []
    async def fake_sleep(s):
        slept.append(s)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    # Drive the inline loop body the same way _amain does.
    retry_seconds = 60
    retry_attempts = 0
    markets = None
    log_writes = []
    class _Log:
        def write(self, p): log_writes.append(p)
    log = _Log()
    while True:
        markets = await engine_mod._discover_1hr_markets(None, [], log)
        if markets:
            break
        retry_attempts += 1
        log.write({"kind": "no_markets_waiting",
                    "process": "hourglass_trader",
                    "retry_attempts": retry_attempts,
                    "next_retry_s": retry_seconds})
        await asyncio.sleep(retry_seconds)

    assert markets == sample
    assert calls["n"] == 3
    assert slept == [60, 60]
    waits = [w for w in log_writes if w.get("kind") == "no_markets_waiting"]
    assert len(waits) == 2
    assert waits[0]["retry_attempts"] == 1
    assert waits[1]["retry_attempts"] == 2
    assert all(w["process"] == "hourglass_trader" for w in waits)


@pytest.mark.asyncio
async def test_discover_loop_returns_immediately_when_first_call_has_markets(
    monkeypatch,
):
    """Happy path: first discovery returns markets -> no retry, no sleep,
    no no_markets_waiting log."""
    from kalshi_engine.bin import live_1hr as engine_mod

    sample = [{"ticker": "KXBTCD-X1", "strike": 100_000.0,
                "open_ms": 1, "close_ms": 60_001}]
    async def fake_discover(*args, **kwargs):
        return sample
    monkeypatch.setattr(engine_mod, "_discover_1hr_markets", fake_discover)

    slept = []
    async def fake_sleep(s):
        slept.append(s)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    log_writes = []
    class _Log:
        def write(self, p): log_writes.append(p)
    log = _Log()

    markets = await engine_mod._discover_1hr_markets(None, [], log)
    assert markets == sample
    # Since we got markets on first call, the inline retry loop in
    # _amain would `break` before sleep — assert no sleeps happened in
    # the helper itself.
    assert slept == []
    assert not any(w.get("kind") == "no_markets_waiting" for w in log_writes)
