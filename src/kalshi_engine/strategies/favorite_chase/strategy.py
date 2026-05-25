"""Favorite-chase strategy - event-in, Decision-out.

Wires one FavoriteChaseState per crypto to the Phase 4 cutpoints model. On a
book snapshot inside the T+8..15m entry window, if a side is the favorite
(bid >= 75c), the model is evaluated once for that market and a Decision
returned. Spot ticks update rolling state. No execution logic lives here.

Markets must be registered (``register_market``) before their events can
trigger, because the strike and cycle timing are not carried by the events -
they come from market discovery (or, in backtest, from market_dim).
"""

from __future__ import annotations

from dataclasses import dataclass

from kalshi_engine.core.events import BookEvent, SpotEvent
from kalshi_engine.core.interfaces import Decision
from kalshi_engine.core.types import Action, Side
from kalshi_engine.risk.envelope import crypto_of_ticker
from kalshi_engine.strategies.favorite_chase.models.phase4_cutpoints import (
    Phase4CutpointsModel,
)
from kalshi_engine.strategies.favorite_chase.rules import (
    is_trigger_window,
    select_favorite,
)
from kalshi_engine.strategies.favorite_chase.state import FavoriteChaseState


@dataclass(frozen=True)
class MarketMeta:
    """Per-market metadata the events do not carry (strike + cycle timing)."""

    ticker: str
    strike: float
    open_ms: int
    close_ms: int


REENTRY_MODES = ("disabled", "polling")


class FavoriteChaseStrategy:
    """Favorite-chase strategy: routes events, emits one Decision per market.

    With ``snapshot_interval_ms`` > 0 and a ``log_writer`` supplied, the
    strategy also emits ``snapshot`` envelopes during each market's trigger
    window at the configured cadence. Snapshots capture the full cutpoints
    model evaluation (would_action / would_size + diagnostics) so we can
    analyse how the gates evolve from T+8 to T+15 without making a decision.

    **Re-entry polling (Phase 12.3):** with ``reentry_mode="polling"`` the
    strategy keeps re-evaluating skipped tickers within the trigger window
    until ``cycle_close_ms - reentry_cutoff_ms``. Each ticker is throttled to
    one evaluation every ``reentry_throttle_ms`` to avoid log spam. ENTER
    decisions still lock the ticker for the remainder of the cycle (no
    add-to-position). With ``reentry_mode="disabled"`` (legacy) the strategy
    falls back to single-shot dedup -- SKIP also locks the ticker, matching
    the Phase 4 calibration baseline.
    """

    def __init__(
        self,
        model: Phase4CutpointsModel | None = None,
        log_writer=None,
        snapshot_interval_ms: int = 0,
        reentry_mode: str = "disabled",
        reentry_cutoff_ms: int = 120_000,
        reentry_throttle_ms: int = 30_000,
        pre_trigger_observation: bool = False,
        pre_trigger_throttle_ms: int = 30_000,
    ) -> None:
        self.model = model if model is not None else Phase4CutpointsModel()
        self.states: dict[str, FavoriteChaseState] = {}
        self.markets: dict[str, MarketMeta] = {}
        # Legacy single-shot dedup (Phase 4 baseline, when reentry disabled).
        # Locks on both ENTER and SKIP.
        self.decided: set[str] = set()
        # Phase-12.3: lock only on ENTER (re-entry mode). Always populated so
        # external observers can introspect; only used as gate when
        # reentry_mode == "polling".
        self.entered: set[str] = set()
        self._log = log_writer
        self._snapshot_interval_ms = int(snapshot_interval_ms)
        self._last_snapshot_ms: dict[str, int] = {}
        # Phase-11C: per-ticker in-memory snapshot history, consumed by
        # ``CycleTracker.on_settlement`` and then cleared. Populated only
        # when snapshots are enabled.
        self.snapshot_history: dict[str, list[dict]] = {}
        # Phase-12.3 re-entry polling config.
        if reentry_mode not in REENTRY_MODES:
            raise ValueError(
                f"reentry_mode must be one of {REENTRY_MODES}, "
                f"got {reentry_mode!r}"
            )
        self.reentry_mode = reentry_mode
        self.reentry_cutoff_ms = int(reentry_cutoff_ms)
        self.reentry_throttle_ms = int(reentry_throttle_ms)
        # Per-ticker last-evaluation timestamp (for throttle).
        self._last_eval_ms: dict[str, int] = {}
        # Per-ticker first-evaluation timestamp (to flag is_reentry).
        self._first_eval_ms: dict[str, int] = {}
        # Phase-12.6 — per-ticker bb_yes at first evaluation (for d_bb_yes
        # diagnostic). Direct logging of trajectory delta enables future
        # defensive-gate research without post-hoc reconstruction from the
        # decision-event chain.
        self._first_bb_yes: dict[str, float] = {}
        # Phase-12.8 — pre-trigger book observation. When enabled, fires a
        # `book_at_pre_trigger` envelope every `pre_trigger_throttle_ms` per
        # ticker during T+5 to T+8 of the cycle (before the trigger window
        # opens). Pure observability — no decision interaction. Enables future
        # research on "could we have triggered earlier?" without polluting
        # production behaviour.
        self.pre_trigger_observation = bool(pre_trigger_observation)
        self.pre_trigger_throttle_ms = int(pre_trigger_throttle_ms)
        # Per-ticker last pre-trigger sample timestamp.
        self._last_pre_trigger_ms: dict[str, int] = {}

    def register_market(
        self, ticker: str, strike: float, open_ms: int, close_ms: int
    ) -> None:
        """Register a market's strike and cycle timing (from discovery)."""
        self.markets[ticker] = MarketMeta(
            ticker, float(strike), int(open_ms), int(close_ms)
        )

    def _state(self, crypto: str) -> FavoriteChaseState:
        if crypto not in self.states:
            self.states[crypto] = FavoriteChaseState(crypto)
        return self.states[crypto]

    def on_event(self, event, model=None) -> Decision | None:
        """Route one event; return a Decision only on a fresh favorite trigger.

        The `model` argument from the Strategy protocol is accepted but
        ignored - this strategy owns its Phase 4 model.
        """
        if isinstance(event, SpotEvent):
            self._state(event.crypto.value).update_spot(event)
            return None
        if isinstance(event, BookEvent):
            return self._on_book(event)
        return None  # TradeEvent / SettlementEvent: no Phase-3 action

    def _on_book(self, book: BookEvent) -> Decision | None:
        crypto = crypto_of_ticker(book.ticker)
        state = self._state(crypto)
        state.update_book(book)

        meta = self.markets.get(book.ticker)
        if meta is None:
            return None  # unregistered market - strike / timing unknown
        # Phase-12.8: pre-trigger observation (T+5 to T+8 window).
        # Fires BEFORE the trigger-window gate so we capture book state in
        # the pre-window period for "could we trigger earlier?" research.
        self._maybe_pre_trigger_observation(book, meta, state)
        if not is_trigger_window(book.recv_ms, meta.open_ms):
            return None
        favorite = select_favorite(book)
        if favorite is None:
            return None  # no >=75c favorite in the book yet

        # ---- snapshot logging (independent of decision-locking) ----
        self._maybe_snapshot(book, meta, favorite, state)

        # ---- gate: have we already entered this ticker? ----
        # ``self.entered`` always blocks (no add-to-position). In disabled
        # mode, ``self.decided`` also blocks (legacy single-shot semantic).
        if book.ticker in self.entered:
            return None
        if self.reentry_mode == "disabled" and book.ticker in self.decided:
            return None

        # ---- re-entry mode: cutoff + throttle ----
        if self.reentry_mode == "polling":
            # End-of-window cutoff: skip re-evaluation in the last N ms.
            time_to_close = meta.close_ms - book.recv_ms
            if time_to_close < self.reentry_cutoff_ms:
                return None
            # Per-ticker throttle: rate-limit to one eval per throttle_ms.
            last_eval = self._last_eval_ms.get(book.ticker)
            if (last_eval is not None
                    and book.recv_ms - last_eval < self.reentry_throttle_ms):
                return None

        # Track first-eval ts so subsequent evals know they're a re-entry.
        first_eval = self._first_eval_ms.get(book.ticker)
        is_reentry = first_eval is not None and book.recv_ms > first_eval
        if first_eval is None:
            self._first_eval_ms[book.ticker] = book.recv_ms
        self._last_eval_ms[book.ticker] = book.recv_ms

        if favorite is Side.YES:
            fav_bid, fav_ask = book.yes_bid, book.yes_ask
        else:
            fav_bid, fav_ask = book.no_bid, book.no_ask
        favorite_mid = (fav_bid + fav_ask) / 2.0

        decision = self.model.evaluate(
            state=state,
            ticker=book.ticker,
            side=favorite,
            favorite_mid_decicents=favorite_mid,
            strike=meta.strike,
            now_ms=book.recv_ms,
            close_ms=meta.close_ms,
        )
        # Phase-12.3 diagnostics: stamp is_reentry so post-hoc analysis can
        # split first-shot vs polling-revival WR.
        if isinstance(decision.diagnostics, dict):
            decision.diagnostics["is_reentry"] = is_reentry
            decision.diagnostics["reentry_mode"] = self.reentry_mode
            # Phase-12.6 — direct d_bb_yes instrumentation.
            # Record bb_yes at first eval for this ticker; emit delta on
            # subsequent evals. Pure observability (no behaviour change).
            cur_bb_yes = decision.diagnostics.get("bb_yes")
            if cur_bb_yes is not None:
                first_bby = self._first_bb_yes.get(book.ticker)
                if first_bby is None:
                    self._first_bb_yes[book.ticker] = cur_bb_yes
                    decision.diagnostics["d_bb_yes"] = 0.0
                else:
                    decision.diagnostics["d_bb_yes"] = cur_bb_yes - first_bby
        # Lock only on ENTER (so polling can keep evaluating after SKIPs).
        if decision.action is Action.ENTER:
            self.entered.add(book.ticker)
        # Legacy: still populate ``decided`` for back-compat with anything
        # that watches it; in disabled mode this is the lock.
        self.decided.add(book.ticker)
        return decision

    # Pre-trigger window: T+5 to T+8 of the 15-min cycle.
    _PRE_TRIGGER_OPEN_MS = 5 * 60_000
    _PRE_TRIGGER_CLOSE_MS = 8 * 60_000   # = TRIGGER_OPEN_MS

    def _maybe_pre_trigger_observation(
        self,
        book: BookEvent,
        meta: MarketMeta,
        state: FavoriteChaseState,
    ) -> None:
        """Emit `book_at_pre_trigger` envelope during T+5 to T+8 window.

        Pure observability — does NOT influence trading decisions. Fires at
        most once per `pre_trigger_throttle_ms` per ticker. Skips after the
        ticker has been entered (post-trade has its own logging).
        """
        if not self.pre_trigger_observation or self._log is None:
            return
        if book.ticker in self.entered:
            return
        elapsed = book.recv_ms - meta.open_ms
        if not (self._PRE_TRIGGER_OPEN_MS <= elapsed < self._PRE_TRIGGER_CLOSE_MS):
            return
        last = self._last_pre_trigger_ms.get(book.ticker)
        if last is not None and book.recv_ms - last < self.pre_trigger_throttle_ms:
            return
        # Tentative favorite = side with higher mid (NOT the >=75c rule —
        # we want to observe the pre-trigger book even if no favorite yet).
        yes_mid = (book.yes_bid + book.yes_ask) / 2.0
        no_mid = (book.no_bid + book.no_ask) / 2.0
        if yes_mid >= no_mid:
            fav_side, fav_mid = "yes", yes_mid
        else:
            fav_side, fav_mid = "no", no_mid
        # Compute spot / vol / bb_div / bps best-effort (may be None pre-warmup).
        spot = state.latest_spot()
        vol = state.vol_30m()
        bb_div = None; bps_margin = None
        if spot is not None and vol is not None:
            sigma = vol / 1e4
            tau = (meta.close_ms - book.recv_ms) / 60_000.0
            if sigma > 0 and tau > 0 and meta.strike > 0:
                try:
                    from math import log
                    from statistics import NormalDist
                    bb_yes = NormalDist().cdf(
                        log(spot / meta.strike) / (sigma * (tau ** 0.5))
                    )
                    bb_fav = bb_yes if fav_side == "yes" else 1.0 - bb_yes
                    bb_div = float(fav_mid) / 1000.0 - bb_fav
                except (ValueError, ZeroDivisionError):
                    pass
            if meta.strike > 0:
                bps_margin = abs(spot - meta.strike) / meta.strike * 1e4
        self._log.write({
            "kind": "book_at_pre_trigger",
            "ticker": book.ticker,
            "ts_ms": book.recv_ms,
            "elapsed_min": elapsed / 60_000.0,
            "tau_min": (meta.close_ms - book.recv_ms) / 60_000.0,
            "yes_bid": book.yes_bid,
            "yes_ask": book.yes_ask,
            "no_bid": book.no_bid,
            "no_ask": book.no_ask,
            "spot": spot,
            "vol_30m": vol,
            "bb_div": bb_div,
            "bps_margin": bps_margin,
            "favorite_side": fav_side,
            "favorite_mid_decicents": fav_mid,
        })
        self._last_pre_trigger_ms[book.ticker] = book.recv_ms

    def _maybe_snapshot(
        self,
        book: BookEvent,
        meta: MarketMeta,
        favorite: Side,
        state: FavoriteChaseState,
    ) -> None:
        """Emit a ``snapshot`` envelope if the cadence threshold has elapsed.

        The snapshot is independent of ``self.decided``, so we see how the
        gates evolve across the full trigger window even after the strategy
        has locked its first decision. Calls ``model.evaluate`` for its
        diagnostics; ``would_action``/``would_size`` reflect what the model
        WOULD return at this moment (in case the strategy hadn't already
        locked).
        """
        if self._log is None or self._snapshot_interval_ms <= 0:
            return
        last = self._last_snapshot_ms.get(book.ticker)
        if last is not None and book.recv_ms - last < self._snapshot_interval_ms:
            return
        if favorite is Side.YES:
            fav_bid, fav_ask = book.yes_bid, book.yes_ask
        else:
            fav_bid, fav_ask = book.no_bid, book.no_ask
        favorite_mid = (fav_bid + fav_ask) / 2.0
        try:
            decision = self.model.evaluate(
                state=state,
                ticker=book.ticker,
                side=favorite,
                favorite_mid_decicents=favorite_mid,
                strike=meta.strike,
                now_ms=book.recv_ms,
                close_ms=meta.close_ms,
            )
        except Exception as exc:
            self._log.write({
                "kind": "snapshot_error",
                "ticker": book.ticker,
                "error": repr(exc),
            })
            self._last_snapshot_ms[book.ticker] = book.recv_ms
            return
        snap = {
            "kind": "snapshot",
            "ticker": book.ticker,
            "side": favorite.value,
            "would_action": decision.action.value,
            "would_size": decision.size,
            "would_reason": decision.reason,
            "diagnostics": decision.diagnostics,
            "already_decided": book.ticker in self.decided,
            "recv_ms": book.recv_ms,
        }
        self._log.write(snap)
        # Retain for CycleTracker consumption on settlement (Phase 11C).
        self.snapshot_history.setdefault(book.ticker, []).append(snap)
        self._last_snapshot_ms[book.ticker] = book.recv_ms
