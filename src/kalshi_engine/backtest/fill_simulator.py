"""FillSimulator - simulates Kalshi marketable-IOC fills for backtest replay.

A synchronous drop-in for ``LiveExecution`` in the backtest pipeline. The
runtime feeds it ``on_book(BookEvent)`` to maintain per-ticker top-of-book
state, ``submit(Decision)`` to simulate a fill against the current book, and
``on_settlement(SettlementEvent)`` to close any open position and compute PnL.

Decisions, fills and settlements are written to the same JSONL log format the
live engine produces, so backtest output and live output are interchangeable
downstream.

Slippage model is configurable but defaults to zero - the baseline assumes
fills at top-of-book ask. Phase 6 stress runs can tighten via the
``SlippageModel`` knobs (``phantom_pct``, ``markdown_dc``).
"""

from __future__ import annotations

from dataclasses import dataclass

from kalshi_engine.core.events import BookEvent, SettlementEvent
from kalshi_engine.core.interfaces import Decision
from kalshi_engine.core.types import Action, Side
from kalshi_engine.execution.kalshi_client import BUY_PRICE_DECICENTS
from kalshi_engine.risk.fees import taker_fee
from kalshi_engine.warehouse.adapters import LiveLogWriter


@dataclass
class SlippageModel:
    """Configurable slippage applied to simulated fills.

    ``phantom_pct``: fraction of displayed top-of-book depth that's phantom
    (default 0 - displayed depth is honoured).
    ``markdown_dc``: extra deci-cents added unfavourably to the fill price
    (default 0). The fill is capped at ``BUY_PRICE_DECICENTS`` (990 dc).
    """

    phantom_pct: float = 0.0
    markdown_dc: int = 0


@dataclass
class _Position:
    side: str               # "yes" / "no"
    count: int              # contracts filled (engine-wide cap is 1 in Phase 4/5)
    fill_price_dc: int      # average fill price in deci-cents
    entry_fee_cents: int    # taker fee on entry
    opened_ts_ms: int


class FillSimulator:
    """Synthetic Execution for backtest replay.

    Maintains per-ticker book state. On a Decision in ENTER state, simulates
    a marketable IOC at the favourite-side top-of-book (capped at 990 dc for
    a buy), tracks the resulting position, and closes it on settlement.
    """

    def __init__(
        self,
        log_writer: LiveLogWriter,
        slippage: SlippageModel | None = None,
    ) -> None:
        self.log = log_writer
        self.slippage = slippage or SlippageModel()
        self.book_state: dict[str, BookEvent] = {}
        self.open_positions: dict[str, _Position] = {}
        self.realized_pnl_cents: float = 0.0
        self.n_fills: int = 0
        self.n_settled: int = 0
        self.n_wins: int = 0

    # -- runtime hooks ---------------------------------------------------
    def on_book(self, book: BookEvent) -> None:
        """Update the local top-of-book snapshot for fill simulation."""
        self.book_state[book.ticker] = book

    def submit(self, decision: Decision) -> None:
        """Simulate a marketable-IOC fill from a Decision."""
        if decision.action is not Action.ENTER:
            return
        if decision.side is None or decision.size <= 0:
            return
        book = self.book_state.get(decision.ticker)
        if book is None:
            self.log.write({
                "kind": "fill_skip",
                "ticker": decision.ticker,
                "reason": "no book state at submit time",
                "diagnostics": decision.diagnostics,
            })
            return

        ask_dc = book.yes_ask if decision.side is Side.YES else book.no_ask
        if ask_dc <= 0 or ask_dc >= 1000:
            self.log.write({
                "kind": "fill_skip",
                "ticker": decision.ticker,
                "reason": f"degenerate book (ask_dc={ask_dc})",
                "diagnostics": decision.diagnostics,
            })
            return

        # Marketable IOC at the buy cap; fill at min(book_ask + markdown, cap)
        fill_dc = min(ask_dc + self.slippage.markdown_dc, BUY_PRICE_DECICENTS)
        # Phantom-liquidity does not currently downsize beyond size=1 in this
        # phase; reserved for stress runs.
        size = decision.size
        fee_cents = taker_fee(fill_dc, size)
        ts_ms = book.recv_ms

        self.open_positions[decision.ticker] = _Position(
            side=decision.side.value, count=size,
            fill_price_dc=fill_dc, entry_fee_cents=fee_cents,
            opened_ts_ms=ts_ms,
        )
        self.n_fills += 1
        self.log.write({
            "kind": "fill",
            "ticker": decision.ticker,
            "side": decision.side.value,
            "action": "buy",
            "fill_price_decicents": fill_dc,
            "fill_price_cents": fill_dc / 10.0,
            "size": size,
            "fee_cents": fee_cents,
            "ts_ms": ts_ms,
            "diagnostics": decision.diagnostics,
        })

    def on_settlement(self, event: SettlementEvent) -> None:
        """Close any open position on the settling ticker; record PnL."""
        pos = self.open_positions.pop(event.ticker, None)
        if pos is None:
            return
        win = pos.side == event.result.value
        # Gross PnL in deci-cents per contract: (1000 - fill_dc) if win else -fill_dc
        gross_dc_per_contract = (1000 - pos.fill_price_dc) if win else -pos.fill_price_dc
        gross_pnl_cents = gross_dc_per_contract * pos.count / 10.0
        net_pnl_cents = gross_pnl_cents - pos.entry_fee_cents
        self.realized_pnl_cents += net_pnl_cents
        self.n_settled += 1
        if win:
            self.n_wins += 1
        self.log.write({
            "kind": "settle",
            "ticker": event.ticker,
            "result": event.result.value,
            "position_side": pos.side,
            "win": win,
            "fill_price_decicents": pos.fill_price_dc,
            "size": pos.count,
            "entry_fee_cents": pos.entry_fee_cents,
            "gross_pnl_cents": gross_pnl_cents,
            "net_pnl_cents": net_pnl_cents,
            "realized_pnl_cents_running": self.realized_pnl_cents,
            "settle_ts_ms": event.determined_ms,
        })

    # -- summary ---------------------------------------------------------
    def summary(self) -> dict:
        return {
            "n_fills": self.n_fills,
            "n_settled": self.n_settled,
            "n_wins": self.n_wins,
            "win_rate": (self.n_wins / self.n_settled) if self.n_settled else 0.0,
            "realized_pnl_cents": self.realized_pnl_cents,
            "open_positions": len(self.open_positions),
        }
