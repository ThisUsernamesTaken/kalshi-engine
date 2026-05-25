"""Shared enums and value types for the kalshi_engine framework."""

from __future__ import annotations

from enum import Enum


class Side(str, Enum):
    """Which side of a Kalshi binary market a position or outcome is on."""

    YES = "yes"
    NO = "no"


class Crypto(str, Enum):
    """Underlying crypto tracked by the 15-minute Kalshi markets."""

    BTC = "BTC"
    ETH = "ETH"
    SOL = "SOL"
    XRP = "XRP"
    DOGE = "DOGE"


class Venue(str, Enum):
    """Spot price venues used to build the underlying index."""

    COINBASE = "coinbase"
    KRAKEN = "kraken"
    BITSTAMP = "bitstamp"
    FUSION = "fusion"


class Action(str, Enum):
    """The action a strategy ``Decision`` expresses."""

    ENTER = "enter"
    EXIT = "exit"
    HOLD = "hold"
    SKIP = "skip"


class LiveLogKind(str, Enum):
    """Event kinds recorded in the live engine's JSONL log.

    Mirrors the schema the live engine writes and ``LiveLogReader`` reads.
    """

    BOOT = "boot"
    DISCOVER = "discover"
    DISCOVERY = "discovery"
    WS_SUBSCRIBE = "ws_subscribe"
    WS_ERROR = "ws_error"
    ENTRY = "entry"
    EXIT = "exit"
    PNL_RECORDED = "pnl_recorded"
    LIVE_ENTRY_PLACED = "live_entry_placed"
    LIVE_ENTRY_ERROR = "live_entry_error"
    LIVE_STOP_PLACED = "live_stop_placed"
    TRIGGER_NO_BOOK = "trigger_no_book"
    SKIP_MIN_STRIKE_BPS = "skip_min_strike_bps"
    SKIP_MAX_SLIP = "skip_max_slip"
    SKIP_MAX_PRICE = "skip_max_price"
    SKIP_NO_SPOT = "skip_no_spot"
    SPOT_POLL_ERROR = "spot_poll_error"
