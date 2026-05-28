"""Smoke tests for observe_inxu — envelope emission + window dedup.

Covers the InxuObserverState logic directly without spinning up the
Kalshi WS or async Alpaca client. The integration paths (real WS, real
Alpaca) are covered by the underlying components' tests.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from kalshi_engine.core.equity import Equity, SPECS
from kalshi_engine.core.events import BookEvent
from kalshi_engine.feeds.alpaca_spot import EquityTrade


# ---- imports of internal helpers ----------------------------------------

from kalshi_engine.bin.observe_inxu import (
    _InxuObserverState, _handle_book, _strike_from_market, _run_loop,
    _sample_due_windows, _rest_orderbook_to_book,
)


def _make_book(ticker: str, recv_ms: int) -> BookEvent:
    return BookEvent(
        ticker=ticker, ts_ms=recv_ms, recv_ms=recv_ms,
        yes_bid=480, yes_ask=520, no_bid=480, no_ask=520,
        yes_levels=((500, 100.0),), no_levels=((500, 100.0),),
    )


def _make_log():
    log = MagicMock()
    log.writes = []
    def _write(p): log.writes.append(p)
    log.write = _write
    return log


def _fresh_state(observe_minutes=(30, 40, 50)):
    state = _InxuObserverState(observe_minutes=observe_minutes)
    open_ms = 1_779_800_000_000
    close_ms = open_ms + 60 * 60_000
    state.register("KXINXU-CYC1-T6000", strike=6000.0, open_ms=open_ms,
                    close_ms=close_ms, series="KXINXU", equity=Equity.SPX)
    return state, open_ms


# ---- window detection ----------------------------------------------------

def test_window_label_at_t30():
    state, _ = _fresh_state()
    assert state.window_label(30.0) == "T+30"

def test_window_label_at_t40():
    state, _ = _fresh_state()
    assert state.window_label(40.0) == "T+40"

def test_window_label_at_t50():
    state, _ = _fresh_state()
    assert state.window_label(50.0) == "T+50"

def test_window_label_misses_t45():
    """Default observe minutes are 30/40/50 — T+45 must NOT match."""
    state, _ = _fresh_state()
    assert state.window_label(45.0) is None

def test_window_label_tolerance_below_60s():
    """Within 1 minute of a target counts as the window."""
    state, _ = _fresh_state()
    assert state.window_label(30.5) == "T+30"
    assert state.window_label(29.5) == "T+30"

def test_window_label_outside_tolerance_misses():
    state, _ = _fresh_state()
    assert state.window_label(31.5) is None
    assert state.window_label(28.5) is None


# ---- envelope emission --------------------------------------------------

def test_handle_book_emits_envelope_at_t30():
    state, open_ms = _fresh_state()
    log = _make_log()
    alpaca = AsyncMock()
    alpaca.get_last_trade.return_value = EquityTrade(
        symbol="SPY", price=502.34, ts_ms=open_ms + 30*60_000,
        recv_ms=open_ms + 30*60_000, exchange="V",
    )
    ev = _make_book("KXINXU-CYC1-T6000", recv_ms=open_ms + 30*60_000)
    asyncio.run(_handle_book(ev, state, alpaca, log))
    env = [w for w in log.writes if w.get("kind") == "book_at_inxu_pretrigger"]
    assert len(env) == 1
    assert env[0]["window_label"] == "T+30"
    assert env[0]["spot"] == 502.34
    assert env[0]["alpaca_symbol"] == "SPY"
    assert env[0]["equity"] == "SPX"


def test_handle_book_dedup_one_envelope_per_window():
    """Two book events within the same window emit only ONE envelope."""
    state, open_ms = _fresh_state()
    log = _make_log()
    alpaca = AsyncMock()
    alpaca.get_last_trade.return_value = EquityTrade(
        symbol="SPY", price=500.0, ts_ms=0, recv_ms=0, exchange="V")
    ev1 = _make_book("KXINXU-CYC1-T6000", recv_ms=open_ms + 30*60_000)
    ev2 = _make_book("KXINXU-CYC1-T6000", recv_ms=open_ms + int(30.3 * 60_000))
    asyncio.run(_handle_book(ev1, state, alpaca, log))
    asyncio.run(_handle_book(ev2, state, alpaca, log))
    env = [w for w in log.writes if w.get("kind") == "book_at_inxu_pretrigger"]
    assert len(env) == 1


def test_handle_book_separate_windows_emit_separately():
    """T+30 and T+40 on the same ticker each emit once."""
    state, open_ms = _fresh_state()
    log = _make_log()
    alpaca = AsyncMock()
    alpaca.get_last_trade.return_value = EquityTrade(
        symbol="SPY", price=500.0, ts_ms=0, recv_ms=0, exchange="V")
    ev30 = _make_book("KXINXU-CYC1-T6000", recv_ms=open_ms + 30*60_000)
    ev40 = _make_book("KXINXU-CYC1-T6000", recv_ms=open_ms + 40*60_000)
    asyncio.run(_handle_book(ev30, state, alpaca, log))
    asyncio.run(_handle_book(ev40, state, alpaca, log))
    env = [w for w in log.writes if w.get("kind") == "book_at_inxu_pretrigger"]
    assert len(env) == 2
    assert {e["window_label"] for e in env} == {"T+30", "T+40"}


def test_handle_book_ignores_unknown_ticker():
    state, open_ms = _fresh_state()
    log = _make_log()
    alpaca = AsyncMock()
    ev = _make_book("KXINXU-OTHER-T7000", recv_ms=open_ms + 30*60_000)
    asyncio.run(_handle_book(ev, state, alpaca, log))
    assert not log.writes
    alpaca.get_last_trade.assert_not_called()


def test_handle_book_outside_window_no_envelope():
    state, open_ms = _fresh_state()
    log = _make_log()
    alpaca = AsyncMock()
    ev = _make_book("KXINXU-CYC1-T6000", recv_ms=open_ms + 10*60_000)
    asyncio.run(_handle_book(ev, state, alpaca, log))
    assert not log.writes
    alpaca.get_last_trade.assert_not_called()


def test_handle_book_alpaca_returns_none_logs_skip():
    """When Alpaca returns None (market closed / poll failed), log
    spot_poll_skip and mark window fired so we don't retry endlessly."""
    state, open_ms = _fresh_state()
    log = _make_log()
    alpaca = AsyncMock()
    alpaca.get_last_trade.return_value = None
    ev = _make_book("KXINXU-CYC1-T6000", recv_ms=open_ms + 30*60_000)
    asyncio.run(_handle_book(ev, state, alpaca, log))
    skips = [w for w in log.writes if w.get("kind") == "spot_poll_skip"]
    assert len(skips) == 1
    assert skips[0]["window_label"] == "T+30"
    # Subsequent event in same window must not re-poll
    ev2 = _make_book("KXINXU-CYC1-T6000", recv_ms=open_ms + int(30.3*60_000))
    asyncio.run(_handle_book(ev2, state, alpaca, log))
    assert alpaca.get_last_trade.call_count == 1


# ---- strike parse -------------------------------------------------------

def test_strike_from_market_floor_strike():
    assert _strike_from_market({"floor_strike": 5500.0, "ticker": "X"}) == 5500.0

def test_strike_from_market_ticker_fallback():
    m = {"floor_strike": None, "ticker": "KXINXU-26MAY26H1100-T5499.9999"}
    assert _strike_from_market(m) == 5499.9999

def test_strike_from_market_returns_zero_on_bad():
    assert _strike_from_market({"ticker": "no-strike-here"}) == 0.0


# ---- registration -------------------------------------------------------

def test_register_market_persists_metadata():
    state, _ = _fresh_state()
    m = state.markets["KXINXU-CYC1-T6000"]
    assert m["strike"] == 6000.0
    assert m["series"] == "KXINXU"
    assert m["equity"] == Equity.SPX


# ---- spec lookup --------------------------------------------------------

def test_spec_lookup_for_spx():
    spec = SPECS[Equity.SPX]
    assert spec.kalshi_series == "KXINXU"
    assert spec.alpaca_symbol == "SPY"

def test_spec_lookup_for_ndx():
    spec = SPECS[Equity.NDX]
    assert spec.kalshi_series == "KXNASDAQ100U"
    assert spec.alpaca_symbol == "QQQ"


# ---- run_loop deadline enforcement ---------------------------------------

class _SilentWs:
    """WS stub whose events() yields nothing, simulating a quiet equity
    market with no book updates inside the duration window."""
    async def events(self):
        # Block indefinitely. The deadline must override this.
        await asyncio.Event().wait()
        if False:
            yield None  # pragma: no cover — satisfies async-iter contract

def test_run_loop_honors_deadline_when_no_events_arrive():
    """Regression: an earlier version of the loop used `async for ev in
    ws.events()` and only checked the deadline on each event. With a quiet
    feed the loop blocked past --duration-s. The fix uses asyncio.wait_for
    around a queue.get(), so the deadline fires deterministically."""
    state, _ = _fresh_state()
    log = _make_log()
    alpaca = AsyncMock()
    ws = _SilentWs()
    import time as _time
    start = _time.time()
    asyncio.run(_run_loop(ws, state, alpaca, log, duration_s=0.5))
    elapsed = _time.time() - start
    # Should exit within ~1.5s (0.5s deadline + cleanup slack). The pre-fix
    # version blocked indefinitely.
    assert elapsed < 1.5, f"run_loop overshot deadline: {elapsed:.2f}s"
    # No envelopes emitted (no events arrived)
    assert not [w for w in log.writes if w.get("kind") == "book_at_inxu_pretrigger"]


class _OneEventWs:
    """WS that yields one book event then stalls. Used to verify the
    handler still fires when an event DOES arrive before the deadline."""
    def __init__(self, ev):
        self._ev = ev
    async def events(self):
        yield self._ev
        await asyncio.Event().wait()  # then stall

def test_run_loop_dispatches_event_then_honors_deadline():
    state, open_ms = _fresh_state()
    log = _make_log()
    alpaca = AsyncMock()
    alpaca.get_last_trade.return_value = EquityTrade(
        symbol="SPY", price=500.0, ts_ms=0, recv_ms=0, exchange="V")
    ev = _make_book("KXINXU-CYC1-T6000", recv_ms=open_ms + 30*60_000)
    ws = _OneEventWs(ev)
    import time as _time
    start = _time.time()
    asyncio.run(_run_loop(ws, state, alpaca, log, duration_s=0.5))
    elapsed = _time.time() - start
    assert elapsed < 1.5
    env = [w for w in log.writes if w.get("kind") == "book_at_inxu_pretrigger"]
    assert len(env) == 1


# ---- Phase 14.17: timer-driven sampling (decoupled from WS event timing) -

def test_sample_due_windows_emits_from_cached_book():
    """The core fix: a window fires from a CACHED book + wall-clock timer,
    with no WS event landing inside the +/-60s window. This is what the
    purely event-gated observer could never do (0 envelopes in 49.5h)."""
    state, open_ms = _fresh_state()
    log = _make_log()
    alpaca = AsyncMock()
    alpaca.get_last_trade.return_value = EquityTrade(
        symbol="SPY", price=503.5, ts_ms=0, recv_ms=0, exchange="V")
    # A book arrived at T+2 (well before the window) and was cached.
    state.update_book(_make_book("KXINXU-CYC1-T6000", recv_ms=open_ms + 2 * 60_000))
    now_ms = open_ms + 30 * 60_000  # synthetic wall-clock at T+30
    n = asyncio.run(_sample_due_windows(state, alpaca, log, now_ms, client=None))
    assert n == 1
    env = [w for w in log.writes if w.get("kind") == "book_at_inxu_pretrigger"]
    assert len(env) == 1
    assert env[0]["window_label"] == "T+30"
    assert env[0]["sample_source"] == "timer"
    assert env[0]["spot"] == 503.5
    assert env[0]["equity"] == "SPX"


def test_sample_due_windows_dedup_across_ticks():
    """Two timer ticks inside the same window emit only ONE envelope."""
    state, open_ms = _fresh_state()
    log = _make_log()
    alpaca = AsyncMock()
    alpaca.get_last_trade.return_value = EquityTrade(
        symbol="SPY", price=500.0, ts_ms=0, recv_ms=0, exchange="V")
    state.update_book(_make_book("KXINXU-CYC1-T6000", recv_ms=open_ms))
    now_ms = open_ms + 30 * 60_000
    asyncio.run(_sample_due_windows(state, alpaca, log, now_ms))
    asyncio.run(_sample_due_windows(state, alpaca, log, now_ms + 5_000))
    env = [w for w in log.writes if w.get("kind") == "book_at_inxu_pretrigger"]
    assert len(env) == 1


def test_sample_due_windows_rest_fallback_when_no_cached_book():
    """When no WS book was cached, the timer REST-fetches the book (mirroring
    the trader's REST path that logged 5,708 decisions) and still emits."""
    state, open_ms = _fresh_state()
    log = _make_log()
    alpaca = AsyncMock()
    alpaca.get_last_trade.return_value = EquityTrade(
        symbol="SPY", price=500.0, ts_ms=0, recv_ms=0, exchange="V")
    client = AsyncMock()
    client._request.return_value = {
        "orderbook": {"yes_dollars": [["0.95", "100"]],
                      "no_dollars": [["0.04", "200"]]}
    }
    now_ms = open_ms + 30 * 60_000
    n = asyncio.run(_sample_due_windows(state, alpaca, log, now_ms, client=client))
    assert n == 1
    env = [w for w in log.writes if w.get("kind") == "book_at_inxu_pretrigger"]
    assert len(env) == 1
    assert env[0]["sample_source"] == "timer"
    client._request.assert_awaited()


def test_sample_due_windows_no_book_no_client_logs_waiting_once():
    """No cached book and no REST client -> log a single waiting diagnostic
    (not spammed every tick), emit nothing, do NOT mark fired."""
    state, open_ms = _fresh_state()
    log = _make_log()
    alpaca = AsyncMock()
    now_ms = open_ms + 30 * 60_000
    asyncio.run(_sample_due_windows(state, alpaca, log, now_ms, client=None))
    asyncio.run(_sample_due_windows(state, alpaca, log, now_ms + 3_000, client=None))
    waits = [w for w in log.writes if w.get("kind") == "pretrigger_waiting_no_book"]
    assert len(waits) == 1  # deduped across ticks
    assert not [w for w in log.writes if w.get("kind") == "book_at_inxu_pretrigger"]
    alpaca.get_last_trade.assert_not_called()
    # window not marked fired -> a book arriving later can still emit
    assert ("KXINXU-CYC1-T6000", "T+30") not in state.fired


def test_sample_due_windows_outside_window_no_emit():
    state, open_ms = _fresh_state()
    log = _make_log()
    alpaca = AsyncMock()
    state.update_book(_make_book("KXINXU-CYC1-T6000", recv_ms=open_ms))
    now_ms = open_ms + 35 * 60_000  # T+35 is not in (30,40,50)
    n = asyncio.run(_sample_due_windows(state, alpaca, log, now_ms))
    assert n == 0
    assert not log.writes
    alpaca.get_last_trade.assert_not_called()


def test_rest_orderbook_to_book_complement():
    """REST orderbook parse derives bids via the Kalshi binary complement."""
    ob = {"orderbook": {"yes_dollars": [["0.95", "10"], ["0.96", "5"]],
                        "no_dollars": [["0.04", "20"]]}}
    b = _rest_orderbook_to_book(ob, "T", now_ms=123)
    assert b is not None
    assert b.yes_ask == 950  # best (lowest) yes offer
    assert b.no_ask == 40    # best (lowest) no offer
    assert b.yes_bid == 960  # 1000 - no_ask
    assert b.no_bid == 50    # 1000 - yes_ask


def test_rest_orderbook_to_book_empty_book():
    b = _rest_orderbook_to_book({"orderbook": {}}, "T", now_ms=1)
    assert b is not None
    assert b.yes_bid == 0 and b.no_bid == 0
    assert b.yes_ask == 1000 and b.no_ask == 1000
