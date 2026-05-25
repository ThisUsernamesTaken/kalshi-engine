"""KalshiWebSocketFeed: snapshot + delta math, complement invariant, trade/lifecycle."""

from __future__ import annotations

from kalshi_engine.core.events import (
    BookEvent,
    LifecycleEvent,
    SettlementEvent,
    TradeEvent,
)
from kalshi_engine.core.types import Side
from kalshi_engine.feeds.kalshi_ws import KalshiWebSocketFeed


class _DummySigner:
    """Stand-in signer for dispatch-only tests (no WS connection)."""

    def headers(self, method, path):
        return {}


def _feed(tickers=("KXBTC15M-T",)) -> KalshiWebSocketFeed:
    return KalshiWebSocketFeed(signer=_DummySigner(), tickers=list(tickers))


def _snapshot(seq=1, ts_ms=1000):
    return {
        "type": "orderbook_snapshot",
        "msg": {
            "market_ticker": "KXBTC15M-T",
            "seq": seq,
            "ts_ms": ts_ms,
            "yes_dollars_fp": [["0.2200", "1000"], ["0.2300", "500"]],
            "no_dollars_fp": [["0.7700", "800"], ["0.7800", "400"]],
        },
    }


def _delta(side, price_dollars, delta_fp, seq, ts_ms=1100):
    return {
        "type": "orderbook_delta",
        "msg": {
            "market_ticker": "KXBTC15M-T",
            "seq": seq,
            "ts_ms": ts_ms,
            "side": side,
            "price_dollars": price_dollars,
            "delta_fp": delta_fp,
        },
    }


def test_snapshot_builds_book_event():
    feed = _feed()
    events = list(feed._dispatch(_snapshot()))
    assert len(events) == 1
    book = events[0]
    assert isinstance(book, BookEvent)
    # yes ladder: max yes bid = 230 dc, no ladder: max no bid = 780 dc
    assert book.yes_bid == 230
    assert book.no_bid == 780
    # Kalshi binary complement
    assert book.yes_bid + book.no_ask == 1000
    assert book.yes_ask + book.no_bid == 1000


def test_delta_additive_resize():
    feed = _feed()
    list(feed._dispatch(_snapshot()))  # establish state
    # shrink yes 0.2300 by 100  (500 -> 400, max still 230)
    events = list(feed._dispatch(_delta("yes", "0.2300", -100, seq=2)))
    assert len(events) == 1
    book = events[0]
    levels = dict(book.yes_levels)
    assert levels[230] == 400.0
    assert book.yes_bid == 230  # max unchanged


def test_delta_removes_empty_level():
    feed = _feed()
    list(feed._dispatch(_snapshot()))
    # delete yes 0.2300 entirely (delta -500 zeroes the level)
    events = list(feed._dispatch(_delta("yes", "0.2300", -500, seq=2)))
    assert len(events) == 1
    book = events[0]
    levels = dict(book.yes_levels)
    assert 230 not in levels  # level removed
    assert book.yes_bid == 220  # next best yes bid


def test_delta_zero_is_noop_not_swallowed():
    """A delta of 0 must still be processed (regression for the `or` chain bug)."""
    feed = _feed()
    list(feed._dispatch(_snapshot()))
    # delta_fp = 0 -> existing size unchanged; the level remains at 500
    events = list(feed._dispatch(_delta("yes", "0.2300", 0, seq=2)))
    assert len(events) == 1
    book = events[0]
    levels = dict(book.yes_levels)
    assert levels[230] == 500.0


def test_duplicate_seq_is_ignored():
    feed = _feed()
    list(feed._dispatch(_snapshot(seq=5)))
    # apply a delta with seq=5 (duplicate) -> book unchanged, duplicates++
    list(feed._dispatch(_delta("yes", "0.2200", 100, seq=5)))
    book_state = feed.books["KXBTC15M-T"]
    assert book_state["duplicates"] == 1
    # the duplicate delta did not mutate the level
    assert book_state["yes_bids"][220] == 1000.0


def test_gap_recorded_not_repaired():
    feed = _feed()
    list(feed._dispatch(_snapshot(seq=10)))
    # next delta jumps to seq 15 -> gap of 4
    list(feed._dispatch(_delta("yes", "0.2200", -100, seq=15)))
    book_state = feed.books["KXBTC15M-T"]
    assert book_state["gaps"] == 4
    assert book_state["last_seq"] == 15  # advanced anyway


def test_trade_event_emission():
    feed = _feed()
    payload = {
        "type": "trade",
        "msg": {
            "market_ticker": "KXBTC15M-T",
            "ts_ms": 2000,
            "taker_side": "yes",
            "yes_price_dollars": "0.2300",
            "count_fp": "34.5",
        },
    }
    events = list(feed._dispatch(payload))
    assert len(events) == 1
    trade = events[0]
    assert isinstance(trade, TradeEvent)
    assert trade.price == 230  # 0.2300 -> 230 dc
    assert trade.count == 34.5  # fractional preserved
    assert trade.taker_side is Side.YES


def test_settlement_event_on_determined():
    feed = _feed()
    payload = {
        "type": "market_lifecycle_v2",
        "msg": {
            "market_ticker": "KXBTC15M-T",
            "ts_ms": 3000,
            "status": "determined",
            "result": "yes",
            "settlement_value": "1.0000",
            "determination_ts": 1778587000,  # seconds
        },
    }
    events = list(feed._dispatch(payload))
    assert len(events) == 1
    s = events[0]
    assert isinstance(s, SettlementEvent)
    assert s.result is Side.YES
    assert s.settle_value == 1.0
    assert s.determined_ms == 1778587000 * 1000


def test_add_tickers_extends_list_and_closes_active_ws():
    """add_tickers must extend self._tickers and close the live WS so the
    outer reconnect loop re-subscribes with the full ticker list.

    Empirical reason (2026-05-23 verification): Kalshi's update_subscription
    add_markets command did not stream orderbook_delta for newly-added
    tickers, so we rely on a clean reconnect instead.
    """
    import asyncio

    class _FakeWs:
        def __init__(self):
            self.closed = False
        async def close(self):
            self.closed = True

    feed = _feed(("KXBTC15M-T",))
    fake_ws = _FakeWs()
    feed._ws = fake_ws

    added = asyncio.run(feed.add_tickers(["KXBTC15M-U", "KXBTC15M-T", "KXETH15M-V"]))
    # Existing "KXBTC15M-T" is skipped; two new tickers added.
    assert added == 2
    assert "KXBTC15M-U" in feed.tickers
    assert "KXETH15M-V" in feed.tickers
    # The active WS must be closed so the reconnect picks up the new list.
    assert fake_ws.closed


def test_add_tickers_with_no_active_ws_still_records():
    """Without an active WS, add_tickers still updates the list for the next
    reconnect. No exception even though there's nothing to close."""
    import asyncio
    feed = _feed(("KXBTC15M-T",))
    assert feed._ws is None
    added = asyncio.run(feed.add_tickers(["KXBTC15M-U"]))
    assert added == 1
    assert "KXBTC15M-U" in feed.tickers


def test_add_tickers_no_op_when_all_known():
    """Re-adding a known ticker must NOT close the WS (avoids needless flap)."""
    import asyncio

    class _FakeWs:
        def __init__(self):
            self.closed = False
        async def close(self):
            self.closed = True

    feed = _feed(("KXBTC15M-T",))
    fake_ws = _FakeWs()
    feed._ws = fake_ws
    added = asyncio.run(feed.add_tickers(["KXBTC15M-T"]))
    assert added == 0
    assert not fake_ws.closed


def test_lifecycle_open_emits_lifecycle_event():
    feed = _feed()
    payload = {
        "type": "market_lifecycle_v2",
        "msg": {
            "market_ticker": "KXBTC15M-T",
            "ts_ms": 1000,
            "status": "open",
            "open_ts": 1000,
            "close_ts": 1900,
            "floor_strike": 80000.0,
        },
    }
    events = list(feed._dispatch(payload))
    assert len(events) == 1
    ev = events[0]
    assert isinstance(ev, LifecycleEvent)
    assert ev.status == "open"
    assert ev.open_ms == 1000 * 1000  # seconds -> ms
    assert ev.close_ms == 1900 * 1000
    assert ev.strike == 80000.0
