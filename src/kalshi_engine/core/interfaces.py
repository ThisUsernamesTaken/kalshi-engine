"""Core framework protocols: the contracts that model, strategy, execution,
and risk components implement.

Signatures and docstrings only - concrete implementations land in later
phases. Components are wired together by the engine runtime and the
backtest replayer, both of which depend only on these protocols.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from kalshi_engine.core.events import Event
from kalshi_engine.core.types import Action, Side


@dataclass(frozen=True, slots=True)
class Decision:
    """A strategy's intent for one market at one instant.

    ``diagnostics`` carries model internals (fair value, edge, volatility,
    etc.) for logging and post-hoc analysis; it never affects execution.
    """

    ticker: str
    action: Action
    side: Side | None = None
    limit_cents: int | None = None
    size: int = 0
    confidence: float = 0.0
    reason: str = ""
    diagnostics: dict = field(default_factory=dict)


@runtime_checkable
class Model(Protocol):
    """Produces a fair YES probability from accumulated market state."""

    def update(self, event: Event) -> None:
        """Ingest one event to refresh internal state."""
        ...

    def fair_yes(self, ticker: str, now_ms: int) -> float | None:
        """Return P(market resolves YES), or None if data is insufficient."""
        ...


@runtime_checkable
class Strategy(Protocol):
    """Maps market state plus model output to a trading ``Decision``."""

    def on_event(self, event: Event, model: Model) -> Decision | None:
        """Return a ``Decision``, or None to take no action on this event."""
        ...


@runtime_checkable
class Execution(Protocol):
    """Places orders and keeps internal state reconciled with the account.

    Live execution is inherently async (signed HTTP + websockets), so both
    methods are async. A purely-in-memory mock execution can implement them
    as async no-ops.
    """

    async def submit(self, decision: Decision) -> None:
        """Place, cancel, or exit orders to realize a ``Decision``."""
        ...

    async def reconcile(self) -> None:
        """Sync internal positions against real Kalshi fills and settlements."""
        ...


@runtime_checkable
class RiskGuard(Protocol):
    """Vetoes or resizes decisions that breach sizing or the daily cap."""

    def check(self, decision: Decision) -> Decision:
        """Return the decision unchanged, downsized, or converted to SKIP."""
        ...
