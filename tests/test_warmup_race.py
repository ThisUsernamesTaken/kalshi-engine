"""Warmup-race regression.

``SpotFeed.bootstrap_warmup_into`` must populate every crypto's strategy
state before any BookEvent can arrive, so vol_30m is available on the very
first decision. Validates the Phase-4 tail-patch.
"""

from __future__ import annotations

import math
import time

from kalshi_engine.core.events import BookEvent, SpotEvent
from kalshi_engine.core.types import Action, Crypto, Venue
from kalshi_engine.feeds.spot_ws import SpotFeed
from kalshi_engine.risk.envelope import RiskState
from kalshi_engine.strategies.favorite_chase.models.phase4_cutpoints import (
    Phase4CutpointsModel,
)
from kalshi_engine.strategies.favorite_chase.strategy import FavoriteChaseStrategy


def _make_strategy_no_tod():
    """Test strategy with time-of-day SKIP disabled.
    Wall-clock timestamps used by these tests can incidentally land in the
    14-17Z window. Tests here are not about TOD behavior — disable it."""
    return FavoriteChaseStrategy(Phase4CutpointsModel(time_of_day_skip=False))


async def _fake_fetch_candles(self, session, crypto, start, end):
    """30 one-minute candles with constant 0.1%/min log return (~10 bps/min vol)."""
    out = []
    for i in range(30):
        ts_s = start + i * 60
        price = 100.0 * math.exp(i * 0.001)
        out.append(SpotEvent(
            crypto=crypto, venue=Venue.COINBASE,
            ts_ms=ts_s * 1000, recv_ms=ts_s * 1000, price=price,
        ))
    return out


def _book_in_trigger_window(ticker: str = "KXBTC15M-T"):
    now_ms = int(time.time() * 1000)
    open_ms = now_ms
    close_ms = open_ms + 15 * 60_000
    recv_ms = open_ms + 10 * 60_000  # T+10m, inside [8m, 15m)
    book = BookEvent(
        ticker=ticker, ts_ms=recv_ms, recv_ms=recv_ms,
        yes_bid=800, yes_ask=820, no_bid=180, no_ask=200,
        yes_levels=(), no_levels=(),
    )
    return book, open_ms, close_ms


async def test_bootstrap_populates_every_crypto(monkeypatch):
    """All 5 cryptos must have populated spot buffers + computable vol after bootstrap."""
    monkeypatch.setattr(SpotFeed, "_fetch_candles", _fake_fetch_candles)
    feed = SpotFeed(list(Crypto))  # all 5
    strategy = _make_strategy_no_tod()
    risk_state = RiskState()

    n = await feed.bootstrap_warmup_into(strategy, risk_state)
    assert n == 5 * 30

    for c in Crypto:
        assert c.value in risk_state.last_spot_ms
        state = strategy.states.get(c.value)
        assert state is not None
        assert len(state.spot_buffer) >= 2
        vol = state.vol_30m()
        assert vol is not None
        assert vol > 0


async def test_first_book_after_bootstrap_has_vol(monkeypatch):
    """Race-elimination: a BookEvent immediately after bootstrap produces a
    Decision whose diagnostics carry a non-None vol_30m."""
    monkeypatch.setattr(SpotFeed, "_fetch_candles", _fake_fetch_candles)
    feed = SpotFeed([Crypto.BTC])
    strategy = _make_strategy_no_tod()
    risk_state = RiskState()
    await feed.bootstrap_warmup_into(strategy, risk_state)

    book, open_ms, close_ms = _book_in_trigger_window()
    strategy.register_market(
        book.ticker, strike=100.0, open_ms=open_ms, close_ms=close_ms,
    )
    decision = strategy.on_event(book)
    assert decision is not None
    assert decision.diagnostics.get("vol_30m") is not None
    assert decision.diagnostics["vol_30m"] > 0


def test_first_book_without_bootstrap_skips_no_history():
    """Negative: without bootstrap, the first BookEvent SKIPs no-history."""
    strategy = _make_strategy_no_tod()
    book, open_ms, close_ms = _book_in_trigger_window()
    strategy.register_market(
        book.ticker, strike=100.0, open_ms=open_ms, close_ms=close_ms,
    )
    decision = strategy.on_event(book)
    assert decision is not None
    assert decision.action is Action.SKIP
    assert "no spot" in decision.reason.lower()


async def test_warmup_handles_single_crypto_fetch_failure(monkeypatch):
    """One crypto's _fetch_candles failure must not poison the others."""

    async def _half_fail(self, session, crypto, start, end):
        # SOL fails (returns empty); the rest get 30 candles each.
        if crypto is Crypto.SOL:
            return []
        return await _fake_fetch_candles(self, session, crypto, start, end)

    monkeypatch.setattr(SpotFeed, "_fetch_candles", _half_fail)
    feed = SpotFeed(list(Crypto))
    strategy = _make_strategy_no_tod()
    risk_state = RiskState()

    n = await feed.bootstrap_warmup_into(strategy, risk_state)
    assert n == 4 * 30  # SOL contributed zero; others contributed 30 each
    # SOL has no state populated; the others do
    assert strategy.states.get("SOL") is None or len(
        strategy.states["SOL"].spot_buffer
    ) == 0
    for c in (Crypto.BTC, Crypto.ETH, Crypto.XRP, Crypto.DOGE):
        assert len(strategy.states[c.value].spot_buffer) >= 2
