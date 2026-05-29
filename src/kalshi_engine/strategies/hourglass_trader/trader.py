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

# Phase 14.19 — BTC 1hr alpha capture (4-day cohort deep dive 2026-05-29,
# _tmp_analysis/btc_1hr_deep_dive). THREE BTC-1hr-ONLY levers; ETH and every
# other crypto are untouched. See ACTIONABLE.md / SYNTHESIS.md for the CIs.
#
# A (size-tilt UP, Rank 1 — the only CI-clean lever): when the v13b score is
#   >= BTC_SIZE_TILT_SCORE OR the cycle is >= BTC_SIZE_TILT_MINUTE in, lift the
#   size to BTC_SIZE_TILT_CONTRACTS. Cohort n=34, WR 100%, per-ct +$0.049 CI
#   [+0.035, +0.066]; it traded ~9ct, so 15ct captures +$12-23/wk.
BTC_SIZE_TILT_SCORE = 6.0
BTC_SIZE_TILT_MINUTE = 40.0
BTC_SIZE_TILT_CONTRACTS = 15
# B (size DOWN, Rank 2 — variance reducer): when d_norm < BTC_DOWNSIZE_DNORM,
#   clamp to BTC_DOWNSIZE_CONTRACTS regardless of what the tier ladder / tilt
#   produced. The d_norm<1.5 tail held the entire -$21.35 current-config drag
#   (per-ct -$0.127, CI straddles 0): still collects the high-WR win but stops
#   betting big on weak strike separation.
BTC_DOWNSIZE_DNORM = 1.5
BTC_DOWNSIZE_CONTRACTS = 2
# C (lower fav-cost cap, Rank 3 — directional): a BTC-only cap on the favorite
#   ASK, tighter than the global --max-favorite-cost-decicents mid cap. Refuses
#   the 0.88-0.92 fee-trap entries (fav_mid 910-920 cohort -$4.50).
BTC_MAX_FAV_ASK_DECICENTS = 880


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
        per_crypto_models: dict[str, Phase4CutpointsModel] | None = None,
        min_entry_d_norm: float = 0.0,
        near_strike_allowed_minute: int = 55,
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
        self.min_entry_d_norm = float(min_entry_d_norm)
        self.near_strike_allowed_minute = int(near_strike_allowed_minute)
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
        # Phase 14.3 — per-crypto align-mode override. Each value is a
        # fully-constructed Phase4CutpointsModel pre-configured with the
        # desired align_mode. Missing crypto => fall back to the global
        # ``model``. Use case: BTC keeps T6 (7/10/10), ETH uses 1to3_ramp
        # at 1/2/3-by-score while we collect ETH cohort data.
        if per_crypto_models is None:
            self.per_crypto_models: dict[str, Phase4CutpointsModel] = {}
        else:
            self.per_crypto_models = {
                str(k).upper(): m for k, m in per_crypto_models.items()
            }
        self.markets: dict[str, HourMarketMeta] = {}
        # Per-crypto rolling state (spot/vol/bb_div). Shared FavoriteChaseState
        # implementation since the math is identical for 1hr cycles — the only
        # thing that changes is τ (= close - now).
        self._states: dict[str, FavoriteChaseState] = {}
        # Per-ticker: True iff we've already ENTERED this ticker. Dedup is
        # at-most-once-per-ticker regardless of how many trigger windows fire.
        self._entered: set[str] = set()
        # Per crypto/cycle: the main trader is a single-strike selector. The
        # ladder strategy is the only component allowed to fan out rungs.
        self._entered_cycles: set[tuple[str, int]] = set()
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
            meta = self.markets.get(event.ticker)
            if meta is not None:
                try:
                    crypto = crypto_of_ticker(event.ticker)
                    self._entered_cycles.discard((crypto.upper(), meta.open_ms))
                except Exception:
                    pass
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
        fav_ask = book.yes_ask if fav_side is Side.YES else book.no_ask

        ts_utc = datetime.fromtimestamp(book.recv_ms / 1000, tz=timezone.utc)
        diag_base = {
            "ticker": book.ticker,
            "side": fav_side.value,
            "favorite_mid_decicents": fav_mid,
            "favorite_ask_decicents": fav_ask,
            "strike": meta.strike,
            "elapsed_min": elapsed_min,
            "tau_min": (meta.close_ms - book.recv_ms) / 60_000.0,
            "window_label": f"T+{window_min}",
            "utc_hour": ts_utc.hour,
            "trigger_minutes": list(self.trigger_minutes),
            "skip_hours_utc": sorted(self.skip_hours_utc),
            "max_favorite_cost_decicents": self.max_favorite_cost_decicents,
            "max_contracts": self.max_contracts,
            "min_entry_d_norm": self.min_entry_d_norm,
            "near_strike_allowed_minute": self.near_strike_allowed_minute,
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
        # Phase 14.19 Change C — BTC-only tighter favorite-cost cap on the ASK.
        if crypto.upper() == "BTC" and fav_ask > BTC_MAX_FAV_ASK_DECICENTS:
            return self._skip_decision(
                book.ticker, fav_side,
                f"BTC fav_ask {fav_ask:.0f}dc > Phase14.19 cap "
                f"{BTC_MAX_FAV_ASK_DECICENTS}dc (fee-trap zone)",
                {**diag_base,
                 "btc_max_fav_ask_decicents": BTC_MAX_FAV_ASK_DECICENTS},
            )
        cycle_key = (crypto.upper(), meta.open_ms)
        if cycle_key in self._entered_cycles:
            return self._skip_decision(
                book.ticker, fav_side,
                f"main trader already entered {crypto.upper()} cycle "
                f"open_ms={meta.open_ms}",
                diag_base,
            )
        state = self._state(crypto)
        # Phase 14.3 — per-crypto align-mode override. Use a crypto-specific
        # model if one was registered; else fall back to the global model.
        active_model = self.per_crypto_models.get(crypto.upper(), self._model)
        decision = active_model.evaluate(
            state=state,
            ticker=book.ticker,
            side=fav_side,
            favorite_mid_decicents=fav_mid,
            strike=meta.strike,
            now_ms=book.recv_ms,
            close_ms=meta.close_ms,
        )

        if (decision.action == Action.ENTER
                and self.min_entry_d_norm > 0
                and window_min < self.near_strike_allowed_minute):
            d_norm = (decision.diagnostics or {}).get("d_norm")
            if d_norm is None:
                return self._skip_decision(
                    book.ticker, fav_side,
                    f"d_norm unavailable before T+{self.near_strike_allowed_minute}; "
                    "near-strike gate fail-closed",
                    {**diag_base, **(decision.diagnostics or {})},
                )
            try:
                d_norm_value = float(d_norm)
            except (TypeError, ValueError):
                return self._skip_decision(
                    book.ticker, fav_side,
                    f"invalid d_norm={d_norm!r} before "
                    f"T+{self.near_strike_allowed_minute}; near-strike gate "
                    "fail-closed",
                    {**diag_base, **(decision.diagnostics or {})},
                )
            if d_norm_value < self.min_entry_d_norm:
                return self._skip_decision(
                    book.ticker, fav_side,
                    f"d_norm {d_norm_value:.3f} < {self.min_entry_d_norm:.3f} "
                    f"before T+{self.near_strike_allowed_minute}; "
                    "too close to strike",
                    {**diag_base, **(decision.diagnostics or {})},
                )

        # Phase 14.19 Changes A+B — BTC-only size-tilt-up / downsize levers.
        # Applied BEFORE the per-crypto + global ceilings so those ceilings
        # (raised to BTC_SIZE_TILT_CONTRACTS in the BTC config) stay consistent
        # no-ops for the tilt cohort rather than silently clipping it back.
        if decision.action == Action.ENTER and crypto.upper() == "BTC":
            decision = self._apply_btc_size_levers(decision, elapsed_min)

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
            self._entered_cycles.add(cycle_key)

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

    # -- Phase 14.19 BTC-1hr sizing levers ------------------------------------
    @staticmethod
    def _safe_float(value) -> float | None:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _v13b_score(diag: dict | None) -> float | None:
        """Pull the active align-mode's V13b score out of model diagnostics.

        Each align mode stamps exactly one ``score_*`` key (BTC's production
        d_norm-gate mode uses ``score_5tier_v13b_btc_dnorm_gate``); return the
        first numeric one so this stays robust if the BTC mode is reshaped.
        """
        if not diag:
            return None
        for key, val in diag.items():
            if key.startswith("score_") and isinstance(val, (int, float)):
                return float(val)
        return None

    def _apply_btc_size_levers(
        self, decision: Decision, elapsed_min: float,
    ) -> Decision:
        """Phase 14.19 BTC-1hr-only sizing levers (2026-05-29 deep dive).

        A (size-tilt UP): on the only CI-clean cohort — v13b score >=
          BTC_SIZE_TILT_SCORE OR elapsed >= BTC_SIZE_TILT_MINUTE — lift to
          BTC_SIZE_TILT_CONTRACTS.
        B (size DOWN): on the low-conviction tail — d_norm < BTC_DOWNSIZE_DNORM
          — clamp to BTC_DOWNSIZE_CONTRACTS. Applied AFTER A so weak strike
          separation always wins, regardless of tier ladder or tilt.
        """
        diag = decision.diagnostics or {}
        score = self._v13b_score(diag)
        d_norm = self._safe_float(diag.get("d_norm"))
        size = decision.size

        if (score is not None and score >= BTC_SIZE_TILT_SCORE) \
                or elapsed_min >= BTC_SIZE_TILT_MINUTE:
            size = BTC_SIZE_TILT_CONTRACTS

        if (d_norm is not None and d_norm < BTC_DOWNSIZE_DNORM
                and size > BTC_DOWNSIZE_CONTRACTS):
            size = BTC_DOWNSIZE_CONTRACTS

        if size == decision.size:
            return decision
        note = (f"P14.19 BTC size {decision.size}->{size} "
                f"(score={score} elapsed_min={elapsed_min:.1f} d_norm={d_norm})")
        new_diag = {
            **diag,
            "phase_14_19_size_before": decision.size,
            "phase_14_19_size_after": size,
        }
        return replace(
            decision, size=size,
            reason=f"{decision.reason} | {note}", diagnostics=new_diag,
        )

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
