"""BurninReader: event types, receipt-time ordering, range and ticker seek."""

from __future__ import annotations

import itertools
import sqlite3

from kalshi_engine.core.events import BookEvent, SettlementEvent, TradeEvent
from kalshi_engine.warehouse.adapters import BurninReader

_MARKET_EVENTS = (BookEvent, TradeEvent, SettlementEvent)


def _a_ticker(db: str) -> str:
    con = sqlite3.connect(f"file:{db}?mode=ro&immutable=1", uri=True)
    row = con.execute("SELECT market_ticker FROM kalshi_l2_event LIMIT 1").fetchone()
    con.close()
    return row[0]


def test_iter_ticker_types_and_order(burnin_db):
    ticker = _a_ticker(burnin_db)
    with BurninReader(burnin_db) as r:
        events = list(itertools.islice(r.iter_ticker(ticker), 300))
    assert events, "expected events for the ticker"
    last = -1
    for ev in events:
        assert isinstance(ev, _MARKET_EVENTS)
        assert ev.ticker == ticker
        assert ev.recv_ms >= last  # receipt-time ordered
        last = ev.recv_ms


def test_book_event_schema(burnin_db):
    ticker = _a_ticker(burnin_db)
    with BurninReader(burnin_db) as r:
        book = next(e for e in r.iter_ticker(ticker) if isinstance(e, BookEvent))
    assert 0 <= book.yes_bid <= 1000
    assert 0 <= book.yes_ask <= 1000
    # Kalshi binary complement: NO price = 1000 - YES price (deci-cents)
    assert book.no_bid + book.yes_ask == 1000
    assert book.no_ask + book.yes_bid == 1000
    assert isinstance(book.yes_levels, tuple)
    if book.yes_levels:
        price, size = book.yes_levels[0]
        assert isinstance(price, int)
        assert isinstance(size, float)


def test_iter_range_window(burnin_db):
    con = sqlite3.connect(f"file:{burnin_db}?mode=ro&immutable=1", uri=True)
    start = con.execute("SELECT MIN(received_ts_ms) FROM kalshi_l2_event").fetchone()[0]
    con.close()
    end = start + 120_000
    with BurninReader(burnin_db) as r:
        windowed = list(itertools.islice(r.iter_range(start, end), 1000))
    assert windowed, "expected events in the window"
    for ev in windowed:
        assert start <= ev.recv_ms <= end
