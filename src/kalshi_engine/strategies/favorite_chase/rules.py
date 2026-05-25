"""Pure favorite-chase rule logic - no state, no model, no execution.

These are the mechanical rules of the favorite-chase strategy: when the entry
window is open, which side is the favorite, and how far spot sits from the
strike. Every function here is pure.
"""

from __future__ import annotations

from kalshi_engine.core.events import BookEvent
from kalshi_engine.core.types import Side

CYCLE_MS = 15 * 60_000                # the 15-minute Kalshi crypto cycle
TRIGGER_OPEN_MS = 8 * 60_000          # entries allowed from T+8m into the cycle
FAVORITE_THRESHOLD_DECICENTS = 750    # a side is "the favorite" at bid >= 75.0c


def is_trigger_window(now_ms: int, cycle_start_ms: int) -> bool:
    """True when `now_ms` is inside the favorite-chase entry window.

    The window opens at T+8m into the 15-minute cycle and closes at T+15m.
    """
    elapsed = now_ms - cycle_start_ms
    return TRIGGER_OPEN_MS <= elapsed < CYCLE_MS


def select_favorite(book_event: BookEvent) -> Side | None:
    """Return the favorite side, or None if neither side is bid >= 75c.

    The favorite is whichever side the market prices expensive: its best bid
    is at or above 750 deci-cents. At most one side can clear this (the two
    best bids sum to <= 1000), so the choice is unambiguous.
    """
    if book_event.yes_bid >= FAVORITE_THRESHOLD_DECICENTS:
        return Side.YES
    if book_event.no_bid >= FAVORITE_THRESHOLD_DECICENTS:
        return Side.NO
    return None


def compute_strike_distance_bps(spot: float, strike: float) -> float:
    """Signed distance of spot from strike, in basis points relative to spot.

    Positive when spot is above strike. Matches the Phase 4 analysis, which
    measured the bps gate as |spot - strike| / spot x 1e4.
    """
    if spot <= 0:
        return 0.0
    return (spot - strike) / spot * 1e4
