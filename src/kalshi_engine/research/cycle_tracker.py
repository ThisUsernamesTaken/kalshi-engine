"""Phase 11C: per-cycle post-mortem.

On each crypto settlement, writes a ``cycle_summary`` envelope containing:
- The full snapshot history for that ticker (from FavoriteChaseStrategy)
- The recent spot trajectory for that crypto (rolling deque)
- Whether we held a position and our entry diagnostics
- The settlement result and value

This single envelope captures everything needed for offline post-mortem of
that cycle - did the gates evolve favorably? Was bb_div drifting against us
when settled? Were we entered with a stale spot or healthy spot?

Memory bound: per-crypto spot deque trimmed to ``spot_history_minutes`` of
ticks (~5 min default). Per-ticker snapshot history cleared on settlement.
"""

from __future__ import annotations

import re
from collections import deque
from typing import Iterable

from kalshi_engine.core.events import SettlementEvent, SpotEvent
from kalshi_engine.core.types import Crypto

_CRYPTO_PREFIX_RE = re.compile(r"^KX(BTC|ETH|SOL|XRP|DOGE)15M-")


class CycleTracker:
    """Maintains per-cycle bookkeeping; emits cycle_summary on settlement.

    Wire via ``on_event`` from the live run loop after standard routing -
    every event flows through here so spot history and settlement summaries
    are populated. No-op when ``log_writer`` is None or ``enabled`` is False.
    """

    def __init__(
        self,
        log_writer,
        strategy,
        execution,
        cryptos: Iterable[Crypto],
        spot_history_minutes: int = 5,
        recent_trajectory_minutes: int = 3,
        enabled: bool = True,
    ) -> None:
        self._log = log_writer
        self._strategy = strategy
        self._execution = execution
        self._cryptos = [c.value for c in cryptos]
        self._spot_history_ms = spot_history_minutes * 60_000
        self._recent_ms = recent_trajectory_minutes * 60_000
        self.enabled = enabled and (log_writer is not None)
        # Per-crypto rolling deque of (ts_ms, price)
        self._spot_history: dict[str, deque] = {
            c: deque() for c in self._cryptos
        }

    def on_event(self, event) -> None:
        """Hook called for every event the run loop dispatches."""
        if not self.enabled:
            return
        if isinstance(event, SpotEvent):
            self._on_spot(event)
        elif isinstance(event, SettlementEvent):
            self._on_settlement(event)

    def _on_spot(self, ev: SpotEvent) -> None:
        d = self._spot_history.get(ev.crypto.value)
        if d is None:
            return
        d.append((ev.ts_ms, ev.price))
        cutoff = ev.ts_ms - self._spot_history_ms
        while d and d[0][0] < cutoff:
            d.popleft()

    def _on_settlement(self, ev: SettlementEvent) -> None:
        m = _CRYPTO_PREFIX_RE.match(ev.ticker)
        if not m:
            return
        crypto = m.group(1)
        snaps = self._strategy.snapshot_history.get(ev.ticker, [])
        position = self._execution.open_positions.get(ev.ticker)
        # Recent spot trajectory (last ``recent_trajectory_minutes``).
        all_spots = list(self._spot_history.get(crypto, []))
        cutoff = ev.recv_ms - self._recent_ms
        recent = [
            {"ts_ms": ts, "price": p}
            for ts, p in all_spots
            if ts >= cutoff
        ]
        self._log.write({
            "kind": "cycle_summary",
            "ticker": ev.ticker,
            "crypto": crypto,
            "result": ev.result.value,
            "settle_value": ev.settle_value,
            "settle_ts_ms": ev.recv_ms,
            "our_position": position,
            "n_snapshots": len(snaps),
            "snapshots": snaps,
            "spot_trajectory_recent": recent,
        })
        # Free memory: clear this ticker's snapshot history. The spot deque
        # is shared across the crypto's lifetime and stays bounded by
        # ``_spot_history_ms``.
        self._strategy.snapshot_history.pop(ev.ticker, None)
