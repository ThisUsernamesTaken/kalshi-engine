"""Hourglass 1hr live trader strategy (Phase 13.1).

Subscribes to KX{C}D 1hr digital markets and ENTERS at configured T+M
windows (default T+30 and T+50). Sits on top of the same Phase4CutpointsModel
score machinery as the 15m engine but with a flat compressed sizing scheme
(5tier_v13b_1to3_flat by default — skip score<4, flat 3ct otherwise).

Key differences vs the 15m FavoriteChaseStrategy:

  * Trigger times are explicit minute marks (T+30, T+50) rather than the
    15m engine's T+8 "favorite-established" gate.
  * Per-cycle dedup is one-entry-per-ticker (no re-entry on a ticker
    we've already entered, regardless of subsequent window evaluations).
  * Hour-skip filter (default {13Z}) applied at the trigger gate — the
    1hr observer sweep flagged 13Z as catastrophic.
  * MAX_FAV_COST decicent cap (default 920 = $0.92) — fee-trap protection.
    96% of 1hr envelopes hit favorite_mid ≈ $1.00 where Kalshi taker fees
    `ceil(7·c·(1-c))¢` eliminate any edge.
  * Defense-in-depth: every ENTER Decision is also clipped to
    max_contracts before being returned.

Envelopes also emitted at the configured trigger windows so log readers
can reconstruct the decision after the fact.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone

from kalshi_engine.core.events import (
    BookEvent,
    LifecycleEvent,
    SettlementEvent,
    SpotEvent,
)
from kalshi_engine.core.interfaces import Decision
from kalshi_engine.core.types import Action, Crypto, Side
from kalshi_engine.risk.envelope import crypto_of_ticker
from kalshi_engine.strategies.favorite_chase.models.phase4_cutpoints import (
    Phase4CutpointsModel,
)
from kalshi_engine.strategies.favorite_chase.state import FavoriteChaseState


@dataclass(frozen=True)
class HourMarketMeta:
    ticker: str
    strike: float
    open_ms: int
    close_ms: int


# Hard floor: favorite-chase only enters when the favorite is >= 75c. This
# is fundamental to the favorite-chase thesis and is enforced inside the
# trader regardless of any model output.
FAV_CHASE_TRIGGER_DECICENTS = 750.0


class HourglassTraderStrategy:
    """1hr live trader. Returns Decision on book events that land in a
    configured trigger window; otherwise returns None.

    Trigger windows are minute offsets into the cycle. The first book event
    whose ``elapsed_min`` lands in [m, m + _WINDOW_HALF_S/60) for any
    configured m fires evaluation (provided the ticker hasn't already been
    entered this cycle).
    """

    _WINDOW_HALF_S = 30  # 30-second window around each minute mark

    def __init__(
        self,
        log_writer,
        model: Phase4CutpointsModel,
        trigger_minutes: tuple[int, ...] = (30, 50),
        skip_hours_utc: tuple[int, ...] = (13,),
        max_favorite_cost_decicents: int = 920,
        max_contracts: int = 3,
        per_crypto_max_contracts: dict[str, int] | None = None,
    ) -> None:
        if log_writer is None:
            raise ValueError("HourglassTraderStrategy requires a log_writer")
        if model is None:
            raise ValueError("HourglassTraderStrategy requires a model")
        if max_contracts < 1:
            raise ValueError(f"max_contracts must be >= 1, got {max_contracts}")
        self._log = log_writer
        self._model = model
        self.trigger_minutes = tuple(int(m) for m in trigger_minutes)
        self.skip_hours_utc = frozenset(int(h) for h in skip_hours_utc)
        self.max_favorite_cost_decicents = int(max_favorite_cost_decicents)
        self.max_contracts = int(max_contracts)
        # Phase 13.6 — per-crypto sizing overrides. Values clip BEFORE the
        # global max_contracts ceiling. Missing crypto => use global cap.
        # Example: {"ETH": 1} caps ETH at 1ct while leaving BTC/SOL/etc. at
        # whatever the align_mode returns (subject to max_contracts).
        if per_crypto_max_contracts is None:
            self.per_crypto_max_contracts: dict[str, int] = {}
        else:
            self.per_crypto_max_contracts = {
                str(k).upper(): int(v) for k, v in per_crypto_max_contracts.items()
            }
        for k, v in self.per_crypto_max_contracts.items():
            if v < 1:
                raise ValueError(
                    f"per_crypto_max_contracts[{k}]={v} must be >= 1")
        self.markets: dict[str, HourMarketMeta] = {}
        # Per-crypto rolling state (spot/vol/bb_div). Shared FavoriteChaseState
        # implementation since the math is identical for 1hr cycles — the only
        # thing that changes is τ (= close - now).
        self._states: dict[str, FavoriteChaseState] = {}
        # Per-ticker: True iff we've already ENTERED this cycle. Dedup is
        # at-most-once-per-ticker regardless of how many trigger windows fire.
        self._entered: set[str] = set()
        # Per-ticker per-window: True iff we've already EVALUATED that window.
        # Distinct from _entered: a SKIP at T+30 still consumes the window.
        self._evaluated: dict[str, set[int]] = {}

    # -- registration ---------------------------------------------------------
    def register_market(
        self, ticker: str, strike: float, open_ms: int, close_ms: int,
    ) -> None:
        self.markets[ticker] = HourMarketMeta(
            ticker, float(strike), int(open_ms), int(close_ms),
        )
        self._evaluated.setdefault(ticker, set())

    def _state(self, crypto: str) -> FavoriteChaseState:
        if crypto not in self._states:
            self._states[crypto] = FavoriteChaseState(crypto)
        return self._states[crypto]

    # -- event routing --------------------------------------------------------
    def on_event(self, event, model=None) -> Decision | None:
        """Route one event. Returns a Decision when a trigger window fires
        and the ticker hasn't already been entered this cycle. Returns None
        otherwise.
        """
        if isinstance(event, SpotEvent):
            self._state(event.crypto.value).update_spot(event)
            return None
        if isinstance(event, BookEvent):
            return self._on_book(event)
        if isinstance(event, LifecycleEvent):
            # Lifecycle metadata can register a market if discovery missed it.
            if event.strike and event.open_ms and event.close_ms:
                self.register_market(
                    event.ticker, event.strike, event.open_ms, event.close_ms,
                )
            return None
        if isinstance(event, SettlementEvent):
            # Clear dedup so the next cycle of the same ticker starts fresh
            # (tickers are per-cycle in Kalshi's KX{C}D series so this is
            # belt-and-suspenders).
            self._entered.discard(event.ticker)
            self._evaluated.pop(event.ticker, None)
            return None
        return None

    # -- main logic -----------------------------------------------------------
    def _on_book(self, book: BookEvent) -> Decision | None:
        meta = self.markets.get(book.ticker)
        if meta is None:
            return None  # unregistered market — no cycle timing available
        if book.ticker in self._entered:
            return None  # one entry per ticker per cycle
        elapsed_min = (book.recv_ms - meta.open_ms) / 60_000.0
        window_min = None
        for m in self.trigger_minutes:
            if m <= elapsed_min < m + (self._WINDOW_HALF_S / 60.0):
                window_min = m
                break
        if window_min is None:
            return None
        evaluated = self._evaluated.setdefault(book.ticker, set())
        if window_min in evaluated:
            return None
        evaluated.add(window_min)

        # Determine the favorite side from the book.
        yes_mid = (book.yes_bid + book.yes_ask) / 2.0
        no_mid = (book.no_bid + book.no_ask) / 2.0
        if yes_mid >= no_mid:
            fav_side, fav_mid = Side.YES, yes_mid
        else:
            fav_side, fav_mid = Side.NO, no_mid

        ts_utc = datetime.fromtimestamp(book.recv_ms / 1000, tz=timezone.utc)
        diag_base = {
            "ticker": book.ticker,
            "side": fav_side.value,
            "favorite_mid_decicents": fav_mid,
            "strike": meta.strike,
            "elapsed_min": elapsed_min,
            "tau_min": (meta.close_ms - book.recv_ms) / 60_000.0,
            "window_label": f"T+{window_min}",
            "utc_hour": ts_utc.hour,
            "trigger_minutes": list(self.trigger_minutes),
            "skip_hours_utc": sorted(self.skip_hours_utc),
            "max_favorite_cost_decicents": self.max_favorite_cost_decicents,
            "max_contracts": self.max_contracts,
        }

        # Hour-skip filter — sweep flagged 13Z catastrophic.
        if ts_utc.hour in self.skip_hours_utc:
            return self._skip_decision(
                book.ticker, fav_side,
                f"hour-skip {ts_utc.hour:02d}Z in {sorted(self.skip_hours_utc)}",
                diag_base,
            )

        # 75c favorite-chase trigger (fundamental).
        if fav_mid < FAV_CHASE_TRIGGER_DECICENTS:
            return self._skip_decision(
                book.ticker, fav_side,
                f"fav_mid {fav_mid:.0f}dc < trigger {FAV_CHASE_TRIGGER_DECICENTS:.0f}dc",
                diag_base,
            )

        # MAX_FAV_COST gate — fee-trap protection.
        if fav_mid > self.max_favorite_cost_decicents:
            return self._skip_decision(
                book.ticker, fav_side,
                f"fav_mid {fav_mid:.0f}dc > max_favorite_cost "
                f"{self.max_favorite_cost_decicents}dc (fee-trap zone)",
                diag_base,
            )

        # Run the V13b score + sizing through the Phase4CutpointsModel.
        try:
            crypto = crypto_of_ticker(book.ticker)
        except Exception:
            return self._skip_decision(
                book.ticker, fav_side,
                f"could not determine crypto for ticker {book.ticker}",
                diag_base,
            )
        state = self._state(crypto)
        decision = self._model.evaluate(
            state=state,
            ticker=book.ticker,
            side=fav_side,
            favorite_mid_decicents=fav_mid,
            strike=meta.strike,
            now_ms=book.recv_ms,
            close_ms=meta.close_ms,
        )

        # Per-crypto cap (Phase 13.6) applied BEFORE the global ceiling.
        if decision.action == Action.ENTER:
            per_cap = self.per_crypto_max_contracts.get(crypto.upper())
            if per_cap is not None and decision.size > per_cap:
                decision = replace(decision, size=per_cap)
        # Defense-in-depth: clip any ENTER to the max_contracts ceiling even
        # if a future align-mode somehow returned a larger size. The
        # 5tier_v13b_1to3_flat default never returns >3 so this is normally
        # a no-op.
        if decision.action == Action.ENTER and decision.size > self.max_contracts:
            decision = replace(decision, size=self.max_contracts)

        # Mark as entered (dedup) BEFORE returning so a concurrent book event
        # within the same window doesn't double-fire.
        if decision.action == Action.ENTER:
            self._entered.add(book.ticker)

        # Trader-specific diagnostics merge with whatever the model produced.
        if decision.diagnostics is None:
            merged_diag = dict(diag_base)
        else:
            merged_diag = {**diag_base, **decision.diagnostics}
        decision_with_diag = replace(decision, diagnostics=merged_diag)
        self._log_decision(decision_with_diag)
        return decision_with_diag

    # -- helpers --------------------------------------------------------------
    def _skip_decision(
        self, ticker: str, side: Side, why: str, diag: dict,
    ) -> Decision:
        d = Decision(
            ticker=ticker, action=Action.SKIP, side=side, size=0,
            confidence=0.0, reason=f"SKIP: {why}", diagnostics=diag,
        )
        self._log_decision(d)
        return d

    def _log_decision(self, decision: Decision) -> None:
        self._log.write({
            "kind": "decision",
            "ticker": decision.ticker,
            "action": decision.action.value,
            "side": decision.side.value,
            "size": decision.size,
            "confidence": decision.confidence,
            "reason": decision.reason,
            "diagnostics": decision.diagnostics,
        })
