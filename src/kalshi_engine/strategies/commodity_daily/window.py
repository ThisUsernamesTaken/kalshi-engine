"""Daily-window state machine for commodity ladders.

Crypto's ``is_trigger_window(now, cycle_start)`` measures elapsed time *after
open* of a 15-min cycle. A daily commodity contract opens hours/days before
its single 5pm-ET settle, so the window is re-expressed as **minutes before
close**:

    minutes_to_close = (close_ms - now_ms) / 60_000
    WAITING      : minutes_to_close >  open_minutes        (before the window)
    ACTIVE       : close_minutes < minutes_to_close <= open_minutes
    POST_SETTLE  : minutes_to_close <= close_minutes       (too close / settled)

Entries are only sought in ACTIVE. ``close_minutes`` keeps a buffer before the
illiquid final minutes (never enter in the last ``close_minutes``). The whole
module is pure so the transitions are unit-testable without a clock.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class DailyWindowState(str, Enum):
    """Where ``now`` sits relative to a daily contract's entry window."""

    WAITING = "waiting"
    ACTIVE = "active"
    POST_SETTLE = "post_settle"


@dataclass(frozen=True, slots=True)
class DailyWindow:
    """Per-product entry window, expressed in minutes-before-close.

    ``open_minutes`` is when the chase window opens (e.g. 60 => start chasing
    60 min before the 5pm settle). ``close_minutes`` is when it shuts (e.g. 10
    => stop entering 10 min before settle). Requires
    ``open_minutes > close_minutes >= 0``.
    """

    open_minutes: float = 60.0
    close_minutes: float = 10.0

    def __post_init__(self) -> None:
        if self.close_minutes < 0:
            raise ValueError("close_minutes must be >= 0")
        if self.open_minutes <= self.close_minutes:
            raise ValueError(
                "open_minutes must be greater than close_minutes "
                f"(got open={self.open_minutes}, close={self.close_minutes})"
            )

    def minutes_to_close(self, now_ms: int, close_ms: int) -> float:
        """Minutes from ``now`` to the contract's close (negative if past)."""
        return (close_ms - now_ms) / 60_000.0

    def state(self, now_ms: int, close_ms: int) -> DailyWindowState:
        """Classify ``now`` for one contract's close time."""
        mtc = self.minutes_to_close(now_ms, close_ms)
        if mtc > self.open_minutes:
            return DailyWindowState.WAITING
        if mtc > self.close_minutes:
            return DailyWindowState.ACTIVE
        return DailyWindowState.POST_SETTLE

    def in_window(self, now_ms: int, close_ms: int) -> bool:
        """True iff ``now`` is inside the active entry window."""
        return self.state(now_ms, close_ms) is DailyWindowState.ACTIVE


def active_observe_mark(
    now_ms: int,
    close_ms: int,
    marks: tuple[int, ...] | list[int],
    tolerance_s: float = 30.0,
) -> int | None:
    """Return the observe mark (minutes-before-close) currently firing, else None.

    ``marks`` are minutes-before-close, e.g. ``(60, 45, 30, 20, 15)``. As the
    minutes-to-close clock ticks down past a mark ``M``, that mark fires once
    while ``minutes_to_close`` is in ``(M - tolerance, M]``. At most one mark
    matches at a time for sane (spaced) mark sets; the largest match is
    returned. The caller is responsible for firing each (ticker, mark) once.
    """
    mtc = (close_ms - now_ms) / 60_000.0
    tol_min = tolerance_s / 60.0
    hits = [m for m in marks if (m - tol_min) < mtc <= m]
    return max(hits) if hits else None
