"""Phase 14.12 — LadderStrategy for 1hr engine (BTC only).

Companion strategy to HourglassTraderStrategy. Where the trader picks a
SINGLE near-the-money strike per cycle (V13b score-best), the ladder
picks the TOP-N FAR-out-of-money strikes per cycle (top by d_norm).
The stacking-overlap analysis (n=19 cycles, 95% CI excludes zero,
all-positive cycles) showed the two strategies pick non-overlapping
strikes at 87% of the universe, and the ladder adds ~+30c/cycle on top
of the engine's PnL with no observed negative cycles.

The ladder is isolated for clean rollback:
- Its own per-cycle dedup (fires once per cycle at T+30 only)
- Its own daily-cap tracker (default $5/day, separate from engine's $10)
- Its own crypto allowlist (BTC only at launch)
- Disabled by default via the `enabled` flag

On each BookEvent the ladder maintains a per-ticker book cache. When a
T+30 event arrives for an allowed crypto's unfired cycle, it enumerates
ALL OTHER STRIKES in the cycle using the cached books, computes d_norm
per strike from the shared FavoriteChaseState, applies the filters
(d_norm floor, fav-price range, bid-side depth), sorts by d_norm desc,
takes top-N, and returns one ENTER Decision per rung at the configured
rung_size (default 3ct).

SettlementEvent updates the ladder's own daily-PnL tracker so the cap
binds independently of the main engine's RiskState. UTC midnight resets
the counter.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone

from kalshi_engine.core.events import (
    BookEvent,
    LifecycleEvent,
    SettlementEvent,
    SpotEvent,
)
from kalshi_engine.core.interfaces import Decision
from kalshi_engine.core.types import Action, Side
from kalshi_engine.risk.envelope import crypto_of_ticker
from kalshi_engine.strategies.favorite_chase.state import FavoriteChaseState


@dataclass
class LadderRungEntry:
    """One in-flight ladder entry tracked for settlement-time PnL."""
    ticker: str
    side: str
    entry_dc: float
    size: int


class LadderStrategy:
    """Top-N-by-d_norm rung selector for 1hr BTC cycles.

    Composed alongside HourglassTraderStrategy in the 1hr engine. Shares
    the trader's per-crypto FavoriteChaseState (spot/vol history) and
    market registry but maintains its own per-cycle dedup, book cache,
    and daily-PnL tracker.

    Configuration mirrors the user spec for Phase 14.12:
      - max_rungs=3, d_norm_min=1.5, rung_size=3
      - fav_min_dc=750 (favorite-chase trigger), fav_max_dc=950
        (room above the V13b favorite cap of 920 because ladder rungs
        at $0.93-$0.95 are the highest-WR far-OTM zone)
      - trigger_minute=30 (fires once at T+30, ignores T+40/T+50)
      - crypto_allowlist=("BTC",) — ETH stays on 1to3_ramp per the 88%-
        signal-on-BTC finding in the stacking analysis
      - daily_cap_cents=500 ($5)
    """

    _WINDOW_HALF_S = 30  # 30-second window around trigger_minute

    def __init__(
        self,
        log_writer,
        per_crypto_states: dict[str, FavoriteChaseState] | None = None,
        market_lookup=None,
        *,
        enabled: bool = False,
        max_rungs: int = 3,
        d_norm_min: float = 1.5,
        rung_size: int = 3,
        min_bid_size: int = 3,
        trigger_minute: int = 30,
        crypto_allowlist: tuple[str, ...] = ("BTC",),
        fav_min_dc: float = 750.0,
        fav_max_dc: float = 950.0,
        daily_cap_cents: int = 500,
    ) -> None:
        if log_writer is None:
            raise ValueError("LadderStrategy requires a log_writer")
        if max_rungs < 1:
            raise ValueError(f"max_rungs must be >= 1, got {max_rungs}")
        if rung_size < 1:
            raise ValueError(f"rung_size must be >= 1, got {rung_size}")
        if d_norm_min < 0:
            raise ValueError(f"d_norm_min must be >= 0, got {d_norm_min}")
        self._log = log_writer
        # Shared state with HourglassTraderStrategy: avoids duplicating spot
        # history. Caller passes the same dict by reference.
        self._states: dict[str, FavoriteChaseState] = (
            per_crypto_states if per_crypto_states is not None else {})
        # market_lookup(ticker) -> HourMarketMeta or None. Lets us enumerate
        # cycle siblings without a hard import dependency on HourglassTrader.
        self._market_lookup = market_lookup or (lambda t: None)
        self.enabled = bool(enabled)
        self.max_rungs = int(max_rungs)
        self.d_norm_min = float(d_norm_min)
        self.rung_size = int(rung_size)
        self.min_bid_size = int(min_bid_size)
        self.trigger_minute = int(trigger_minute)
        self.crypto_allowlist = tuple(c.upper() for c in crypto_allowlist)
        self.fav_min_dc = float(fav_min_dc)
        self.fav_max_dc = float(fav_max_dc)
        self.daily_cap_cents = int(daily_cap_cents)
        # Per-ticker latest book cache. Needed because the ladder fires on
        # ticker X's T+30 event but needs the current books of tickers Y, Z.
        self._latest_book: dict[str, BookEvent] = {}
        # Cycles for which the ladder has already fired. Key: (crypto, open_ms).
        self._fired_cycles: set[tuple[str, int]] = set()
        # In-flight ladder positions (ticker -> entry record). Used by the
        # settlement handler to update the daily-PnL tracker. NOT a position-
        # reconciliation source of truth — that lives on the Kalshi account.
        self._open_positions: dict[str, LadderRungEntry] = {}
        # Daily realized PnL (cents). Separate from the engine's RiskState
        # so the ladder cap binds independently.
        self._daily_realized_cents: int = 0
        self._daily_utc_date: str | None = None

    # ---- daily-cap helpers --------------------------------------------------

    def _check_daily_reset(self, now_ms: int) -> None:
        today = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).date().isoformat()
        if self._daily_utc_date != today:
            self._daily_utc_date = today
            self._daily_realized_cents = 0

    def daily_cap_bound(self, now_ms: int) -> bool:
        self._check_daily_reset(now_ms)
        return self._daily_realized_cents <= -abs(self.daily_cap_cents)

    # ---- event routing -----------------------------------------------------

    def on_event(self, event) -> list[Decision]:
        if not self.enabled:
            return []
        if isinstance(event, SpotEvent):
            return []  # spot history lives in shared FavoriteChaseState
        if isinstance(event, LifecycleEvent):
            return []
        if isinstance(event, BookEvent):
            self._latest_book[event.ticker] = event
            return self._maybe_fire(event)
        if isinstance(event, SettlementEvent):
            self._handle_settlement(event)
            return []
        return []

    # ---- settlement handling -----------------------------------------------

    def _handle_settlement(self, ev: SettlementEvent) -> None:
        entry = self._open_positions.pop(ev.ticker, None)
        if entry is None:
            return  # not one of our ladder positions
        # SettlementEvent.result is a Side enum; compare to entry's string form
        result_val = ev.result.value if hasattr(ev.result, "value") else str(ev.result)
        win = (entry.side == result_val)
        # Per-contract realized = payout - entry_cents - fee
        payout_cents = 100 if win else 0
        entry_cents = entry.entry_dc / 10.0
        fee_cents = self._fee_cents(entry.entry_dc / 1000.0)
        per_ct = payout_cents - entry_cents - fee_cents
        realized = per_ct * entry.size
        # Convert to integer cents (round half away from zero)
        realized_int = int(realized) if realized >= 0 else -int(-realized + 0.5)
        # Reset daily counter if UTC date changed since last update
        ts_ms = ev.ts_ms if hasattr(ev, "ts_ms") and ev.ts_ms else int(
            datetime.now(tz=timezone.utc).timestamp() * 1000)
        self._check_daily_reset(ts_ms)
        self._daily_realized_cents += realized_int
        self._log.write({
            "kind": "ladder_settlement",
            "ticker": ev.ticker,
            "side": entry.side,
            "result": result_val,
            "win": win,
            "entry_dc": entry.entry_dc,
            "size": entry.size,
            "realized_cents": realized_int,
            "daily_realized_cents": self._daily_realized_cents,
            "daily_cap_cents": self.daily_cap_cents,
            "daily_cap_bound": self.daily_cap_bound(ts_ms),
        })

    # ---- main entry: T+30 trigger fan-out ----------------------------------

    def _maybe_fire(self, book: BookEvent) -> list[Decision]:
        meta = self._market_lookup(book.ticker)
        if meta is None:
            return []  # unregistered market
        # Window check: only fire at T+30 (configurable)
        elapsed_min = (book.recv_ms - meta.open_ms) / 60_000.0
        lo = self.trigger_minute
        hi = lo + (self._WINDOW_HALF_S / 60.0)
        if not (lo <= elapsed_min < hi):
            return []
        # Crypto allowlist
        try:
            crypto = crypto_of_ticker(book.ticker)
        except Exception:
            return []
        if crypto.upper() not in self.crypto_allowlist:
            return []
        cycle_key = (crypto.upper(), int(meta.open_ms))
        if cycle_key in self._fired_cycles:
            return []
        # Daily cap pre-check
        if self.daily_cap_bound(book.recv_ms):
            self._fired_cycles.add(cycle_key)
            self._log.write({
                "kind": "ladder_decision",
                "ticker": book.ticker,
                "cycle_open_ms": int(meta.open_ms),
                "action": "skip_cap_bound",
                "daily_realized_cents": self._daily_realized_cents,
                "daily_cap_cents": self.daily_cap_cents,
            })
            return []
        # State for d_norm computation
        state = self._states.get(crypto.upper())
        if state is None:
            return []  # no spot history yet
        spot = state.latest_spot()
        vol = state.vol_30m()
        if spot is None or vol is None or vol <= 0:
            return []

        # Mark cycle fired BEFORE candidate evaluation — even if we end up
        # picking 0 rungs, we don't re-fire at the next T+30 book event.
        self._fired_cycles.add(cycle_key)

        # Build candidate set: every cached book whose market belongs to
        # this cycle (same crypto + same open_ms).
        candidates = []
        for ticker, b in self._latest_book.items():
            m = self._market_lookup(ticker)
            if m is None:
                continue
            try:
                c = crypto_of_ticker(ticker)
            except Exception:
                continue
            if c.upper() != crypto.upper():
                continue
            if int(m.open_ms) != int(meta.open_ms):
                continue
            # Favorite + price
            yes_mid = (b.yes_bid + b.yes_ask) / 2.0
            no_mid = (b.no_bid + b.no_ask) / 2.0
            if yes_mid >= no_mid:
                fav_side, fav_dc = Side.YES, yes_mid
            else:
                fav_side, fav_dc = Side.NO, no_mid
            if not (self.fav_min_dc <= fav_dc <= self.fav_max_dc):
                continue
            # d_norm = bps_margin / (vol_30m * sqrt(tau_min))
            tau_min = (m.close_ms - b.recv_ms) / 60_000.0
            if tau_min <= 0:
                continue
            bps_margin = abs(spot - m.strike) / spot * 1e4
            d_norm = bps_margin / (vol * math.sqrt(tau_min))
            if d_norm < self.d_norm_min:
                continue
            # Depth check on the favored side's BID (proxy for exit-able)
            if fav_side is Side.YES:
                top_size = b.yes_levels[0][1] if b.yes_levels else None
            else:
                top_size = b.no_levels[0][1] if b.no_levels else None
            # If levels were not delivered with this BookEvent (top_size is
            # None) we cannot enforce depth — let it through; live engine
            # will discover depth at order time. If levels ARE present but
            # too thin, skip.
            if top_size is not None and top_size < self.min_bid_size:
                continue
            candidates.append({
                "ticker": ticker, "side": fav_side, "fav_dc": fav_dc,
                "d_norm": d_norm, "strike": m.strike,
                "bid_size": top_size, "tau_min": tau_min,
            })

        # Sort by d_norm desc — prefer farthest/safest
        candidates.sort(key=lambda r: -r["d_norm"])
        chosen = candidates[:self.max_rungs]
        decisions: list[Decision] = []
        for r in chosen:
            diag = {
                "ticker": r["ticker"],
                "side": r["side"].value,
                "favorite_mid_decicents": r["fav_dc"],
                "strike": r["strike"],
                "d_norm": r["d_norm"],
                "tau_min": r["tau_min"],
                "bid_size_top": r["bid_size"],
                "cycle_open_ms": int(meta.open_ms),
                "ladder_max_rungs": self.max_rungs,
                "ladder_d_norm_min": self.d_norm_min,
                "ladder_rung_size": self.rung_size,
                "ladder_min_bid_size": self.min_bid_size,
                "ladder_fav_range_dc": [self.fav_min_dc, self.fav_max_dc],
                "ladder_strategy": "hourglass_ladder",
            }
            d = Decision(
                ticker=r["ticker"], action=Action.ENTER, side=r["side"],
                size=self.rung_size,
                confidence=min(1.0, r["d_norm"] / 5.0),
                reason=(f"LADDER d_norm={r['d_norm']:.2f} fav={r['fav_dc']:.0f}dc "
                        f"-> {self.rung_size}ct (rung pick from cycle "
                        f"{int(meta.open_ms)})"),
                diagnostics=diag,
            )
            decisions.append(d)
            # Track for settlement
            self._open_positions[r["ticker"]] = LadderRungEntry(
                ticker=r["ticker"], side=r["side"].value,
                entry_dc=r["fav_dc"], size=self.rung_size,
            )
            self._log.write({
                "kind": "ladder_decision",
                "ticker": r["ticker"],
                "cycle_open_ms": int(meta.open_ms),
                "action": "enter",
                "side": r["side"].value,
                "size": self.rung_size,
                "fav_dc": r["fav_dc"],
                "d_norm": r["d_norm"],
                "bid_size_top": r["bid_size"],
                "rung_rank": len(decisions),  # 1-indexed
                "n_candidates_total": len(candidates),
                "n_candidates_chosen": len(chosen),
                "daily_realized_cents": self._daily_realized_cents,
                "daily_cap_cents": self.daily_cap_cents,
            })
        # Log a cycle-summary even when 0 rungs chosen
        if not chosen:
            self._log.write({
                "kind": "ladder_decision",
                "ticker": book.ticker,  # trigger ticker
                "cycle_open_ms": int(meta.open_ms),
                "action": "no_rungs",
                "n_candidates_total": len(candidates),
                "reason": "0 candidates passed d_norm + fav_range + depth filters",
                "daily_realized_cents": self._daily_realized_cents,
                "daily_cap_cents": self.daily_cap_cents,
            })
        return decisions

    @staticmethod
    def _fee_cents(cost_dollars: float) -> int:
        """Kalshi taker fee per contract: ceil(7 * c * (1-c)) cents."""
        c = max(0.0, min(1.0, cost_dollars))
        return math.ceil(7 * c * (1 - c))
