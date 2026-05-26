"""Reconcile today's realized PnL from a JSONL engine log.

Phase 14.7 — fixes the daily-cap defect. The previous defect was that
``RiskState.daily_realized_cents`` was never updated after settlements,
so the ``daily_loss_cap_cents`` check at ``risk/envelope.py:87`` was
checking a counter that always stayed at 0. The cap existed in code
but was unenforced in practice.

This module reads the engine's own JSONL log (which records every
``ws_order_update`` fill and every ``settlement`` event) and computes
the running total realized PnL since today's UTC midnight. The live
engines call ``reconcile_today_realized_cents()`` at boot and
periodically thereafter to keep ``RiskState.daily_realized_cents``
in sync with reality.

Why post-processing instead of event-driven accounting: settlement
events don't carry the entry cost — they only carry ``settle_value``
(0.0 or 1.0). To compute PnL we need to match settlements against
prior ``ws_order_update`` fills on the same ticker, which is exactly
what the JSONL log already records. Re-deriving from the log avoids
threading entry-cost state through the typed-event pipeline.

Worst-case lag: the periodic-reconcile interval (default 30s). For a
hard daily cap this is acceptable — at -$10 cap the engine is at most
one trade's loss away from cap-binding regardless.
"""

from __future__ import annotations

import json
import math
import re
from collections import defaultdict
from datetime import datetime, time as dtime, timezone
from pathlib import Path

_RE_CRYPTO_TICKER = re.compile(r"^KX(BTC|ETH|SOL|XRP|DOGE)(15M|D)-")


def utc_midnight_ms(now_ms: int) -> int:
    """Return the unix-ms timestamp of today's UTC midnight relative
    to ``now_ms``."""
    dt = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc)
    midnight = datetime.combine(dt.date(), dtime.min, tzinfo=timezone.utc)
    return int(midnight.timestamp() * 1000)


def reconcile_today_realized_cents(
    log_path: str | Path,
    now_ms: int | None = None,
    ticker_filter=_RE_CRYPTO_TICKER,
) -> int:
    """Scan ``log_path`` for fills + settlements since today's UTC midnight.
    Return the total realized PnL in cents (negative = net loss).

    ``ticker_filter``: regex applied to the ticker; only matching tickers
    are reconciled. Default catches every KX-prefixed crypto market on
    Kalshi's 15m and 1hr binary series. Pass ``None`` to disable filtering.
    """
    import time as _time
    if now_ms is None:
        now_ms = int(_time.time() * 1000)
    cutoff_ms = utc_midnight_ms(now_ms)
    p = Path(log_path)
    if not p.exists():
        return 0

    # Track entry cost per (ticker, side). A ticker can have multiple fills
    # combining at different yes_prices; we want the weighted-average cost.
    entries: dict[tuple[str, str], dict] = defaultdict(
        lambda: {"count": 0, "cost_sum": 0.0, "fee_sum": 0.0}
    )
    settlements: dict[str, float] = {}

    with p.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            log_ts = d.get("log_ts_ms", 0) or 0
            if log_ts < cutoff_ms:
                continue
            kind = d.get("kind", "")
            if kind == "ws_order_update":
                raw = d.get("raw") or {}
                if raw.get("type") != "fill":
                    continue
                msg = raw.get("msg") or {}
                ticker = msg.get("market_ticker", "")
                if ticker_filter is not None and not ticker_filter.match(ticker):
                    continue
                side = (msg.get("side") or "").upper()
                if side not in ("YES", "NO"):
                    continue
                try:
                    yes_price = float(msg.get("yes_price_dollars", "0") or "0")
                    count = int(float(msg.get("count_fp", "0") or "0"))
                    fee = float(msg.get("fee_cost", "0") or "0")
                except (TypeError, ValueError):
                    continue
                cost_per_ct = yes_price if side == "YES" else (1.0 - yes_price)
                e = entries[(ticker, side)]
                e["count"] += count
                e["cost_sum"] += cost_per_ct * count
                e["fee_sum"] += fee
            elif kind == "settlement":
                ticker = d.get("ticker", "")
                if ticker_filter is not None and not ticker_filter.match(ticker):
                    continue
                sv = d.get("settle_value")
                if sv is None:
                    continue
                try:
                    settlements[ticker] = float(sv)
                except (TypeError, ValueError):
                    continue

    # Compute realized PnL for every entry whose ticker has settled.
    total_cents = 0
    for (ticker, side), e in entries.items():
        if e["count"] <= 0:
            continue
        sv = settlements.get(ticker)
        if sv is None:
            continue  # still open; not realized yet
        avg_cost = e["cost_sum"] / e["count"]
        if side == "YES":
            payout_per_ct = 1.0 if sv >= 0.5 else 0.0
        else:
            payout_per_ct = 1.0 if sv < 0.5 else 0.0
        pnl_dollars = (payout_per_ct - avg_cost) * e["count"] - e["fee_sum"]
        total_cents += int(round(pnl_dollars * 100))
    return total_cents
