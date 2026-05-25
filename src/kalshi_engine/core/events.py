"""Immutable market event types consumed by models, strategies, and the
backtest replayer.

PRICE UNITS: every Kalshi price in this module is an integer in **deci-cents**
- tenths of a cent, range 0-1000, where 1000 = $1.00 and 250 = 25.0c. Integer
deci-cents keep Kalshi's "tapered deci-cent" tick exact at every price.
Contract sizes/counts are floats (Kalshi supports fractional contracts).
Underlying spot prices (SpotEvent.price) are dollars, not deci-cents.

Every event carries dual timestamps: ``ts_ms`` is the source/exchange clock
and ``recv_ms`` is local receipt. The gap between them is used for
clock-drift correction during deterministic replay.
"""

from __future__ import annotations

from dataclasses import dataclass

from kalshi_engine.core.types import Crypto, Side, Venue


@dataclass(frozen=True, slots=True)
class BookEvent:
    """Top-of-book quote plus the full price ladder for one Kalshi market.

    All prices are integer deci-cents (0-1000). The Kalshi binary complement
    holds: yes_bid + no_ask == 1000 and yes_ask + no_bid == 1000.
    """

    ticker: str
    ts_ms: int
    recv_ms: int
    yes_bid: int  # deci-cents
    yes_ask: int  # deci-cents
    no_bid: int  # deci-cents
    no_ask: int  # deci-cents
    yes_levels: tuple[tuple[int, float], ...]  # (price_decicents, size_contracts)
    no_levels: tuple[tuple[int, float], ...]


@dataclass(frozen=True, slots=True)
class SpotEvent:
    """A spot/index price print for one crypto from one venue."""

    crypto: Crypto
    venue: Venue
    ts_ms: int
    recv_ms: int
    price: float  # underlying price in dollars (not deci-cents)


@dataclass(frozen=True, slots=True)
class TradeEvent:
    """A public taker print on a Kalshi market."""

    ticker: str
    ts_ms: int
    recv_ms: int
    price: int  # deci-cents
    count: float  # fractional - Kalshi supports fractional contracts
    taker_side: Side


@dataclass(frozen=True, slots=True)
class SettlementEvent:
    """The terminal outcome of a Kalshi market."""

    ticker: str
    ts_ms: int
    recv_ms: int
    result: Side  # YES if close >= strike, else NO
    settle_value: float  # the BRTI mean used to settle (dollars)
    determined_ms: int


@dataclass(frozen=True, slots=True)
class LifecycleEvent:
    """A non-terminal Kalshi market lifecycle transition (open / close / etc.).

    Used by the runtime to register newly-opened markets and track state
    transitions. Terminal outcomes (status='determined') are emitted as
    ``SettlementEvent`` instead.
    """

    ticker: str
    ts_ms: int
    recv_ms: int
    status: str  # created / activated / open / closed / deactivated / settled
    open_ms: int | None
    close_ms: int | None
    strike: float | None


MarketEvent = BookEvent | TradeEvent | SettlementEvent | LifecycleEvent
"""Events scoped to a specific Kalshi market (they carry a ``ticker``)."""

Event = BookEvent | SpotEvent | TradeEvent | SettlementEvent | LifecycleEvent
"""Any event the engine ingests."""
