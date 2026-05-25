"""LiveExecution: submits decisions as marketable IOC orders against Kalshi.

- ``dry_run=True`` logs every order intent but never calls the REST API.
- ``stop_mode`` must be ``"none"``; passing ``"price"`` raises at construction
  - stops are intentionally disabled in this build (see the favorite-chase
  reconciliation memo).
- Fills are tracked via the authenticated ``fill`` WS channel; run
  ``run_order_update_listener`` as a background task to stream them.
- ``reconcile()`` diffs local position state against ``/portfolio/positions``
  and writes ``orphan_local`` / ``orphan_account`` warnings to the live log
  (the 112-unreconciled-entries problem from the old engine).
"""

from __future__ import annotations

import time
from typing import Any

from kalshi_engine.core.interfaces import Decision
from kalshi_engine.core.types import Action
from kalshi_engine.execution.kalshi_client import (
    BUY_PRICE_DECICENTS,
    SELL_PRICE_DECICENTS,
    KalshiClient,
)
from kalshi_engine.warehouse.adapters import LiveLogWriter


def _safe_int_count(value: Any, default: int = 0) -> int:
    """Coerce a Kalshi numeric field to int, tolerating decimal strings.

    Kalshi returns count fields (``filled_count``, ``count_fp``, ``position``)
    as decimal strings like ``"1.00"`` or ``"0.00"``. ``int("1.00")`` raises
    ``ValueError`` -- this helper goes through ``float`` first so the parse
    survives both raw ints and decimal-string responses. ``None`` / unparseable
    -> ``default``. Truncates toward zero (partial fill of 0.99 -> 0).
    """
    if value is None or value == "":
        return default
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


class LiveExecution:
    """Implements the Execution protocol against the live Kalshi API."""

    def __init__(
        self,
        client: KalshiClient,
        log_writer: LiveLogWriter,
        dry_run: bool = False,
        stop_mode: str = "none",
    ) -> None:
        if stop_mode != "none":
            raise ValueError(
                f"stop_mode must be 'none' (stops are disabled in this build); "
                f"got {stop_mode!r}"
            )
        self.client = client
        self.log = log_writer
        self.dry_run = dry_run
        self.stop_mode = stop_mode
        # local view of what we think we own: ticker -> {side, count, order_id, ...}
        self.open_positions: dict[str, dict] = {}
        # all orders the client has acknowledged (for audit)
        self.orders: list[dict] = []

    async def submit(self, decision: Decision) -> None:
        if decision.action is Action.SKIP or decision.action is Action.HOLD:
            return
        if decision.action is Action.ENTER:
            await self._place(decision, action="buy", price_dc=BUY_PRICE_DECICENTS)
        elif decision.action is Action.EXIT:
            await self._place(decision, action="sell", price_dc=SELL_PRICE_DECICENTS)

    async def _place(self, decision: Decision, action: str, price_dc: int) -> None:
        if decision.side is None or decision.size <= 0:
            return
        self.log.write({
            "kind": "order_intent",
            "action": action,
            "ticker": decision.ticker,
            "side": decision.side.value,
            "price_decicents": price_dc,
            "size": decision.size,
            "confidence": decision.confidence,
            "reason": decision.reason,
            "dry_run": self.dry_run,
            "diagnostics": decision.diagnostics,
        })
        if self.dry_run:
            return
        try:
            order = await self.client.place_limit_order(
                ticker=decision.ticker,
                side=decision.side.value,
                action=action,
                price_decicents=price_dc,
                count=decision.size,
            )
        except Exception as exc:
            self.log.write({
                "kind": "order_error",
                "ticker": decision.ticker,
                "error": repr(exc),
            })
            return

        self.orders.append(order)
        try:
            raw_filled = order.get("filled_count") or order.get("fill_count_fp") or 0
            filled = _safe_int_count(raw_filled, default=0)
            order_id = order.get("order_id") or order.get("id")
        except Exception as exc:
            # A malformed REST response must not crash the engine.
            self.log.write({
                "kind": "order_parse_error",
                "ticker": decision.ticker,
                "error": repr(exc),
                "raw": str(order)[:500],
            })
            return
        if action == "buy" and filled > 0:
            self.open_positions[decision.ticker] = {
                "side": decision.side.value,
                "count": filled,
                "order_id": order_id,
                "entered_at_ms": int(time.time() * 1000),
            }
            self.log.write({
                "kind": "order_filled",
                "ticker": decision.ticker,
                "filled": filled,
                "order_id": order_id,
            })
        elif action == "sell" and filled > 0:
            self.open_positions.pop(decision.ticker, None)
            self.log.write({
                "kind": "exit_filled",
                "ticker": decision.ticker,
                "filled": filled,
                "order_id": order_id,
            })
        else:
            self.log.write({
                "kind": "order_unfilled",
                "ticker": decision.ticker,
                "action": action,
                "order_id": order_id,
            })

    async def reconcile(self) -> None:
        """Diff local position state against the real account; log orphans."""
        try:
            positions = await self.client.get_positions()
        except Exception as exc:
            self.log.write({"kind": "reconcile_error", "error": repr(exc)})
            return
        real: dict[str, dict] = {}
        for p in positions:
            count = _safe_int_count(p.get("position"), default=0)
            if count != 0:
                real[p.get("ticker", "")] = p
        # local has it, account doesn't
        for ticker in list(self.open_positions):
            if ticker not in real:
                self.log.write({
                    "kind": "orphan_local",
                    "ticker": ticker,
                    "local": self.open_positions[ticker],
                })
        # account has it, local doesn't
        for ticker, position in real.items():
            if ticker not in self.open_positions:
                self.log.write({
                    "kind": "orphan_account",
                    "ticker": ticker,
                    "position": position,
                })
        self.log.write({
            "kind": "reconcile_done",
            "local_count": len(self.open_positions),
            "account_count": len(real),
        })

    async def reconcile_from_account_at_boot(self, strategy) -> None:
        """Boot-time reconciliation: import existing positions from the account.

        For any market the account already holds a position in, populate the
        local ``open_positions`` AND mark the ticker as ``decided`` on the
        strategy so we never re-enter a market we already traded in this cycle
        (the SOL phantom-re-entry hazard after a 2026-05-22 crash). Writes a
        ``boot_reconcile`` envelope summarising what was imported.
        """
        try:
            positions = await self.client.get_positions()
        except Exception as exc:
            self.log.write({"kind": "boot_reconcile_error", "error": repr(exc)})
            return
        imported: list[dict] = []
        for p in positions:
            ticker = p.get("ticker") or ""
            count = _safe_int_count(p.get("position"), default=0)
            if not ticker or count == 0:
                continue
            # Side: Kalshi returns position as a signed int -- positive => YES.
            side = "yes" if count > 0 else "no"
            self.open_positions[ticker] = {
                "side": side,
                "count": abs(count),
                "order_id": None,
                "entered_at_ms": int(time.time() * 1000),
                "source": "boot_reconcile",
            }
            # Prevent the strategy from re-evaluating this market in this cycle.
            try:
                strategy.decided.add(ticker)
            except AttributeError:
                pass
            imported.append({"ticker": ticker, "side": side, "count": abs(count)})
        self.log.write({
            "kind": "boot_reconcile",
            "imported_count": len(imported),
            "imported": imported,
        })

    async def run_order_update_listener(self) -> None:
        """Background task: consume the WS ``fill`` channel and apply fills.

        In ``dry_run`` mode no orders are placed, so this loop runs without
        producing local-position updates - useful as a connection-health
        check during the dry-run validation.
        """
        async for msg in self.client.subscribe_order_updates():
            try:
                self.log.write({"kind": "ws_order_update", "raw": msg})
                payload = msg.get("msg") if isinstance(msg.get("msg"), dict) else msg
                mtype = (msg.get("type") or payload.get("type") or "").lower()
                if "fill" not in mtype:
                    continue
                ticker = payload.get("ticker") or payload.get("market_ticker")
                if not ticker:
                    continue
                count = _safe_int_count(
                    payload.get("count") or payload.get("count_fp"), default=0
                )
                if count == 0:
                    continue
                action = (payload.get("action") or "").lower()
                side = (payload.get("side") or "").lower()
                if action == "buy":
                    self.open_positions[ticker] = {
                        "side": side,
                        "count": count,
                        "filled_at_ms": int(time.time() * 1000),
                    }
                elif action == "sell":
                    self.open_positions.pop(ticker, None)
            except Exception as exc:
                # WS payload parse failures must never kill the listener.
                self.log.write({
                    "kind": "ws_order_parse_error",
                    "error": repr(exc),
                    "raw": str(msg)[:500],
                })
                continue
