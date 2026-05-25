"""Smoke tests: the core framework imports cleanly and its types construct."""

from __future__ import annotations

from kalshi_engine.core import (
    Action,
    BookEvent,
    Crypto,
    Decision,
    SpotEvent,
    Venue,
)


def test_event_construction():
    book = BookEvent(
        ticker="KXBTC15M-T",
        ts_ms=1,
        recv_ms=2,
        yes_bid=400,
        yes_ask=420,
        no_bid=580,
        no_ask=600,
        yes_levels=((400, 100.0),),
        no_levels=((580, 100.0),),
    )
    assert book.ticker == "KXBTC15M-T"

    spot = SpotEvent(
        crypto=Crypto.BTC, venue=Venue.FUSION, ts_ms=1, recv_ms=2, price=100_000.0
    )
    assert spot.price == 100_000.0


def test_decision_defaults():
    decision = Decision(ticker="KXBTC15M-T", action=Action.SKIP)
    assert decision.side is None
    assert decision.size == 0
    assert decision.diagnostics == {}
