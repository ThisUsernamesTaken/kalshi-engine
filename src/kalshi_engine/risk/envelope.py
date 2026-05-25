"""Risk envelope: the hard limits every trading Decision must clear.

The envelope is the engine's primary safety net under live forward-
verification - small fixed sizing plus a hard daily loss cap. ``check()``
may veto a decision (convert it to SKIP) or downsize it; it never enlarges
one and never blocks an exit.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace

from pydantic import BaseModel, Field

from kalshi_engine.core.interfaces import Decision
from kalshi_engine.core.types import Action

_CRYPTOS = ("BTC", "ETH", "SOL", "XRP", "DOGE")


def crypto_of_ticker(ticker: str) -> str:
    """Best-effort crypto symbol from a Kalshi ticker ('KXBTC15M-...' -> 'BTC')."""
    head = ticker.split("-", 1)[0]
    for c in _CRYPTOS:
        if c in head:
            return c
    return head


@dataclass
class RiskState:
    """Mutable running risk state, updated as fills and spot ticks arrive."""

    daily_realized_cents: int = 0
    open_positions: set[str] = field(default_factory=set)
    last_spot_ms: dict[str, int] = field(default_factory=dict)
    now_ms: int = field(default_factory=lambda: int(time.time() * 1000))

    def spot_age_ms(self, crypto: str) -> int | None:
        """Age of the most recent spot tick for `crypto`, or None if never seen."""
        ts = self.last_spot_ms.get(crypto)
        return None if ts is None else self.now_ms - ts


def _tag(reason: str, note: str) -> str:
    return f"{reason} | {note}" if reason else note


def _skip(decision: Decision, why: str) -> Decision:
    """Convert a decision into a risk-vetoed SKIP."""
    return replace(
        decision,
        action=Action.SKIP,
        side=None,
        size=0,
        reason=_tag(decision.reason, f"RISK-SKIP: {why}"),
    )


class RiskEnvelope(BaseModel):
    """Hard risk limits. Defaults reflect the live small-sized config."""

    daily_loss_cap_cents: int = Field(default=1000, ge=0)
    # Phase-12.7: lifted 5 -> 10. User authorization to scale per-trade
    # exposure after V13b achieved 100% WR on n=71 backtest and live deploy
    # showing clean signal. Worst single-trade loss now ~$9.50 (10ct * 95c)
    # — a single expensive-favorite loss could near-fully consume the $10
    # daily cap in one trade. Max simultaneous exposure across 5 cryptos
    # rises to ~$47.50 worst case. The $10 daily cap remains unchanged and
    # halts trading on cumulative realized loss.
    max_contracts_per_trade: int = Field(default=10, ge=1)
    max_concurrent_positions: int = Field(default=5, ge=1)
    fail_closed_on_data_gap: bool = True
    max_spot_age_ms: int = Field(default=10_000, ge=0)

    def check(self, decision: Decision, state: RiskState) -> Decision:
        """Return the decision unchanged, downsized, or converted to SKIP.

        Only ENTER decisions are gated - exits, holds and skips pass through
        untouched, since risk control must never block closing a position.
        Checks are ordered hardest-veto first; a single failure short-circuits.
        """
        if decision.action is not Action.ENTER:
            return decision

        if state.daily_realized_cents <= -self.daily_loss_cap_cents:
            return _skip(
                decision,
                f"daily loss cap reached ({state.daily_realized_cents}c "
                f"<= -{self.daily_loss_cap_cents}c)",
            )

        is_new = decision.ticker not in state.open_positions
        if is_new and len(state.open_positions) >= self.max_concurrent_positions:
            return _skip(
                decision,
                f"max concurrent positions ({self.max_concurrent_positions}) reached",
            )

        if self.fail_closed_on_data_gap:
            crypto = crypto_of_ticker(decision.ticker)
            age = state.spot_age_ms(crypto)
            if age is None:
                return _skip(decision, f"no spot tick for {crypto} (fail-closed)")
            if age > self.max_spot_age_ms:
                return _skip(
                    decision,
                    f"stale spot for {crypto}: {age}ms > {self.max_spot_age_ms}ms "
                    f"(fail-closed)",
                )

        if decision.size > self.max_contracts_per_trade:
            return replace(
                decision,
                size=self.max_contracts_per_trade,
                reason=_tag(
                    decision.reason,
                    f"downsized {decision.size}->{self.max_contracts_per_trade}",
                ),
            )

        return decision
