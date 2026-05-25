"""Deci-cent invariants on burn-in events: the Kalshi binary complement."""

from __future__ import annotations

import itertools
import sqlite3

from kalshi_engine.core.events import BookEvent
from kalshi_engine.warehouse.adapters import BurninReader


def _a_ticker(db: str) -> str:
    con = sqlite3.connect(f"file:{db}?mode=ro&immutable=1", uri=True)
    row = con.execute("SELECT market_ticker FROM kalshi_l2_event LIMIT 1").fetchone()
    con.close()
    return row[0]


def test_decicent_complement_holds(burnin_db):
    """yes_bid + no_ask == 1000 and yes_ask + no_bid == 1000 on real events."""
    ticker = _a_ticker(burnin_db)
    with BurninReader(burnin_db) as r:
        books = [
            e for e in itertools.islice(r.iter_ticker(ticker), 500)
            if isinstance(e, BookEvent)
        ]
    assert books, "expected BookEvents"
    for b in books:
        assert b.yes_bid + b.no_ask == 1000
        assert b.yes_ask + b.no_bid == 1000
        assert 0 <= b.yes_bid <= 1000
        assert 0 <= b.yes_ask <= 1000


def test_ladder_prices_in_decicent_range(burnin_db):
    ticker = _a_ticker(burnin_db)
    with BurninReader(burnin_db) as r:
        book = next(
            e for e in r.iter_ticker(ticker)
            if isinstance(e, BookEvent) and e.yes_levels
        )
    for price, size in book.yes_levels:
        assert 0 <= price <= 1000
        assert isinstance(price, int)
        assert size >= 0.0
