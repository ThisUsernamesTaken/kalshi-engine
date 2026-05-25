"""Tests for the periodic REST market re-discovery loop.

Without re-discovery, the engine sees only boot-cycle markets and goes idle
after they settle (the 03:30 / 03:45 silent-cycle bug surfaced 2026-05-23).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from kalshi_engine.bin.live import _market_discovery_loop
from kalshi_engine.core.types import Crypto
from kalshi_engine.strategies.favorite_chase.strategy import FavoriteChaseStrategy
from kalshi_engine.warehouse.adapters import LiveLogWriter


class _ScriptedClient:
    """Returns a different markets payload on each ``list_markets`` call."""

    def __init__(self, scripts: dict[str, list[list[dict]]]):
        # scripts[series_ticker] = [response_for_call_1, response_for_call_2, ...]
        self._scripts = scripts
        self._indices: dict[str, int] = {}
        self.call_log: list[str] = []

    async def list_markets(self, series_ticker, status=None, limit=200):
        self.call_log.append(series_ticker)
        idx = self._indices.get(series_ticker, 0)
        responses = self._scripts.get(series_ticker, [])
        if not responses:
            return []
        out = responses[min(idx, len(responses) - 1)]
        self._indices[series_ticker] = idx + 1
        return out


class _FlakyClient(_ScriptedClient):
    """Like _ScriptedClient but raises on the Nth call to a given series."""

    def __init__(self, scripts, raise_on: dict[str, int]):
        super().__init__(scripts)
        self._raise_on = raise_on

    async def list_markets(self, series_ticker, status=None, limit=200):
        idx = self._indices.get(series_ticker, 0)
        if self._raise_on.get(series_ticker) == idx:
            self._indices[series_ticker] = idx + 1
            raise RuntimeError("simulated REST failure")
        return await super().list_markets(series_ticker, status=status, limit=limit)


class _MockKalshiWs:
    def __init__(self):
        self.added: list[str] = []

    async def add_tickers(self, tickers):
        added_now = [t for t in tickers if t not in self.added]
        self.added.extend(added_now)
        return len(added_now)


def _make_market(ticker, strike=80000.0, open_iso="2026-05-23T03:00:00Z",
                  close_iso="2026-05-23T03:15:00Z"):
    return {
        "ticker": ticker,
        "floor_strike": strike,
        "open_time": open_iso,
        "close_time": close_iso,
    }


def _read_log(path: Path) -> list[dict]:
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


async def _run_for(loop_coro, seconds: float):
    """Run an async task for ``seconds`` of real time, then cancel + collect."""
    task = asyncio.create_task(loop_coro)
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=seconds)
    except asyncio.TimeoutError:
        pass
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        pass


def test_loop_registers_new_markets(tmp_path):
    """First iteration discovers the boot cycle; second adds the next cycle."""
    log_path = tmp_path / "live.jsonl"
    log = LiveLogWriter(str(log_path))
    strategy = FavoriteChaseStrategy()
    client = _ScriptedClient({
        "KXBTC15M": [
            [_make_market("KXBTC15M-26MAY230300-00", 80000.0,
                          "2026-05-23T03:00:00Z", "2026-05-23T03:15:00Z")],
            [_make_market("KXBTC15M-26MAY230300-00", 80000.0,
                          "2026-05-23T03:00:00Z", "2026-05-23T03:15:00Z"),
             _make_market("KXBTC15M-26MAY230315-15", 80100.0,
                          "2026-05-23T03:15:00Z", "2026-05-23T03:30:00Z")],
        ],
    })
    ws = _MockKalshiWs()
    asyncio.run(_run_for(
        _market_discovery_loop(client, strategy, [Crypto.BTC], log,
                               interval_seconds=0.05, kalshi_ws=ws),
        seconds=0.3,
    ))
    assert "KXBTC15M-26MAY230300-00" in strategy.markets
    assert "KXBTC15M-26MAY230315-15" in strategy.markets
    assert "KXBTC15M-26MAY230300-00" in ws.added
    assert "KXBTC15M-26MAY230315-15" in ws.added
    events = _read_log(log_path)
    discoveries = [e for e in events if e["kind"] == "market_discovery"]
    # First iteration registers 1, second registers 1 more.
    counts = [d["newly_registered_count"] for d in discoveries]
    assert sum(counts) == 2
    # Each event should reflect totals at write-time.
    assert any(d["total_registered"] >= 2 for d in discoveries)


def test_loop_does_not_re_register_known_markets(tmp_path):
    """If the same ticker is returned twice, ``register_market`` runs once."""
    log = LiveLogWriter(str(tmp_path / "live.jsonl"))
    strategy = FavoriteChaseStrategy()
    client = _ScriptedClient({
        "KXBTC15M": [
            [_make_market("KXBTC15M-X")],
            [_make_market("KXBTC15M-X")],  # same ticker again
            [_make_market("KXBTC15M-X")],
        ],
    })
    ws = _MockKalshiWs()
    asyncio.run(_run_for(
        _market_discovery_loop(client, strategy, [Crypto.BTC], log,
                               interval_seconds=0.05, kalshi_ws=ws),
        seconds=0.35,
    ))
    # add_tickers receives the ticker exactly once across iterations.
    assert ws.added == ["KXBTC15M-X"]


def test_loop_survives_rest_error(tmp_path):
    """A REST error on one call logs and the loop continues."""
    log_path = tmp_path / "live.jsonl"
    log = LiveLogWriter(str(log_path))
    strategy = FavoriteChaseStrategy()
    client = _FlakyClient(
        scripts={
            "KXBTC15M": [
                [],  # 1st call returns nothing
                [_make_market("KXBTC15M-Y")],  # 2nd call succeeds with a market
            ],
        },
        raise_on={"KXBTC15M": 0},  # 1st call raises before the empty list is read
    )
    ws = _MockKalshiWs()
    asyncio.run(_run_for(
        _market_discovery_loop(client, strategy, [Crypto.BTC], log,
                               interval_seconds=0.05, kalshi_ws=ws),
        seconds=0.3,
    ))
    events = _read_log(log_path)
    assert any(e["kind"] == "discovery_error" for e in events)
    # Subsequent successful iteration registered the new market.
    assert "KXBTC15M-Y" in strategy.markets


def test_loop_skips_markets_without_strike(tmp_path):
    """Markets missing strike/open/close are silently skipped (no half-state)."""
    log = LiveLogWriter(str(tmp_path / "live.jsonl"))
    strategy = FavoriteChaseStrategy()
    client = _ScriptedClient({
        "KXBTC15M": [[
            {"ticker": "KXBTC15M-A", "floor_strike": None, "open_time": None,
             "close_time": "2026-05-23T03:15:00Z"},
            {"ticker": "KXBTC15M-B", "floor_strike": 80000.0,
             "open_time": "2026-05-23T03:00:00Z",
             "close_time": "2026-05-23T03:15:00Z"},
        ]],
    })
    ws = _MockKalshiWs()
    asyncio.run(_run_for(
        _market_discovery_loop(client, strategy, [Crypto.BTC], log,
                               interval_seconds=0.05, kalshi_ws=ws),
        seconds=0.15,
    ))
    assert "KXBTC15M-A" not in strategy.markets
    assert "KXBTC15M-B" in strategy.markets


def test_loop_works_without_ws(tmp_path):
    """Discovery still registers markets when no WS feed is supplied."""
    log = LiveLogWriter(str(tmp_path / "live.jsonl"))
    strategy = FavoriteChaseStrategy()
    client = _ScriptedClient({
        "KXBTC15M": [[_make_market("KXBTC15M-Z")]],
    })
    asyncio.run(_run_for(
        _market_discovery_loop(client, strategy, [Crypto.BTC], log,
                               interval_seconds=0.05, kalshi_ws=None),
        seconds=0.15,
    ))
    assert "KXBTC15M-Z" in strategy.markets
