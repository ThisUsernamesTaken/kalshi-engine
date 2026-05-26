"""Phase-4 cutpoints model - the favorite-chase decision policy.

Applies the volatility / model-divergence / strike-margin cutpoints from the
Phase 4 expansion analysis to turn a favorite-chase trigger into a sized
Decision. Cutpoints load from a versioned warehouse artifact.

This model is a decision *policy*, not a continuous fair-value model. It
nominally satisfies the ``Model`` protocol (``update`` / ``fair_yes``) but its
real entry point is ``evaluate()``. ``evaluate`` is given strike / now_ms /
close_ms in addition to the protocol-implied args, because the Brownian-bridge
fair (computed via FavoriteChaseState.bb_fair) genuinely needs strike and the
time-to-close - the events themselves do not carry the strike.

**Phase 12 alignment mode (Phase 12.1 / 12.2):** the sizing logic
switches from the original UPSIZE_2X / ENTER_1X policy to an
alignment-count tiered scheme derived from 24h of live data analysis. The
3 strong-favor signals are:
    s_vol = (vol_30m_pct < ALIGN_VOL_THRESHOLD)
    s_div = (bb_div     < ALIGN_DIV_THRESHOLD)
    s_bps = (bps_margin > ALIGN_BPS_MULT * crypto_threshold)
align = s_vol + s_div + s_bps   # 0..3

``align_mode="2tier"`` (Phase 12.1, conservative):
    align <= 1 -> SKIP
    align == 2 -> ENTER 1ct
    align == 3 -> ENTER 2ct

``align_mode="3tier"`` (Phase 12.2, full scheme):
    align == 0 -> SKIP
    align == 1 -> ENTER 1ct
    align == 2 -> ENTER 2ct
    align == 3 -> ENTER 3ct

``align_mode="5tier"`` (Phase 12.4, validated conviction scheme — Scheme B):
    Hard-gate skip on s_bps=0 or bb_div<=-0.20 (smile artifact) or bb_div>+0.09
    or vol_pct>0.67. Otherwise sizing follows the weighted score:
        score = 2*bb_div_band + 1*s_vol + 1.5*side_yes + 2*bps_strong
        size  = round(score) clipped to [1, 5]
    where:
        bb_div_band = 1 iff -0.20 < bb_div <= 0   (empirical sweet spot)
        bps_strong  = 1 iff bps_margin > 2*threshold (max-conviction marker)
    Validated on 156 live + 218 Phase-4 trades: +$18.98 / +$20.27 vs +$2.26 /
    -$12.66 baseline. The 5ct top tier was 31/31 wins on live and 25/26 on
    Phase 4 (96.2% WR).

``align_mode="disabled"`` reverts to the prior UPSIZE_2X / ENTER_1X policy
for safety/reversibility.

Skip-veto gates (vol_pct > skip threshold, bb_div > skip threshold,
bps_margin < threshold, bb_div <= deep-tail threshold) remain in effect
across all modes.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from kalshi_engine.config import MODELS_DIR
from kalshi_engine.core.interfaces import Decision
from kalshi_engine.core.types import Action, Side
from kalshi_engine.strategies.favorite_chase.rules import compute_strike_distance_bps
from kalshi_engine.strategies.favorite_chase.state import FavoriteChaseState

_DEFAULT_CUTPOINTS = MODELS_DIR / "phase4_cutpoints" / "v3" / "cutpoints.json"

# Phase 12.5: time-of-day SKIP window (UTC hours). Validated on LIVE n=21
# (71.4% WR, -$2.68) + Phase4 n=100 (69.0% WR, -$11.63) — combined -$14.31.
# US-AM regime (14-17Z = 10-13 ET = pre-NYSE-open through morning open).
TOD_SKIP_HOURS = frozenset([14, 15, 16, 17])

# Phase 12.1 strong-favor zone thresholds (per the alignment research).
# Stricter than the existing UPSIZE_2X (-0.03) and SKIP (0.67) gates so they
# differentiate the "strong-favor" zone from the merely-passing zone.
ALIGN_VOL_THRESHOLD = 0.50
ALIGN_DIV_THRESHOLD = -0.05
ALIGN_BPS_MULT = 1.5

# Phase 12.4 (Scheme B) — empirically-validated conviction thresholds.
# bb_div lower bound: the deep-negative tail (<= -0.20) inverts (smile
# artifact, 14 losses spread across 18 h validated multi-crypto/multi-side).
# bb_div sweet-spot upper bound: (-0.20, 0] wins 96%+; (-0.05) cutoff used
# previously was too permissive.
DEEP_DIV_SKIP = -0.20
DIV_BAND_UPPER = 0.0
BPS_STRONG_MULT = 2.0   # bps_strong = bps_margin > 2 * crypto_threshold
SIZE_CAP_5TIER = 5

# Phase 12.6 (V13b) — validated optimization of the 5tier score formula.
# Backtested on combined LIVE n=210 + Phase 4 n=218 = 428 trades.
# Two changes vs V12:
#   (a) side weighting FLIPPED from +1.5*side_yes to +1.5*side_no
#       NO trades outperform YES after the hard gate filters out the
#       smile/high-vol/low-bps cohort. 28/28 NO trades perfect; YES side
#       hosted all 1-2 losses in the polling-disabled cohort.
#   (b) s_vol weight DROPPED. Univariate analysis showed it as essentially
#       noise (-0.3pp WR lift); removing it tightens the entry set
#       without sacrificing PnL. 100% WR achieved on 71 LIVE trades.
#   (c) super-band BONUS (+1) when bb_div in (-0.14, -0.09]. Concentrated
#       sweet-spot of bb_div, validated as the strongest WR sub-band.
# Bootstrap CI 99.7% V13b > V12.
SUPER_BAND_LOW = -0.14
SUPER_BAND_HIGH = -0.09

# Phase 12.12 (V13b S2) — conviction-tiered sizing on the V13b score formula.
# Same hard gates and score formula as 5tier_v13b, but the size mapping is
# steeper to make use of --max-contracts 10 headroom on high-conviction trades
# while SKIPping low-score noise:
#     score < 3.0  -> SKIP
#     3.0 <= score < 4.0 -> 3 ct
#     4.0 <= score < 5.0 -> 5 ct
#     5.0 <= score < 6.0 -> 8 ct
#     score >= 6.0       -> 10 ct
# Counterfactual on n=74 live V13b trades: +$28.12 vs baseline +$20.42,
# worst-trade loss reduced (-$2.55 vs -$3.40 — sizes the score=3.5 DOGE
# loser DOWN from 4ct to 3ct). See research notes.
S2_SKIP_BELOW = 3.0
S2_SIZE_AT_4 = 5
S2_SIZE_AT_5 = 8
S2_SIZE_AT_6 = 10

# Phase 12.13 (V13b H1+H4 mix) — high-yield variant of S2. Same V13b score
# formula and hard gates. SKIPs everything below score=4.0 (H1's score-floor:
# every cohort loss to date sits at score 3.5 — score >= 4 was 58/58 wins).
# Above the floor, sizes by H4's smooth score-multiplier (round(score * 1.8),
# capped at MAX_CONTRACTS=10):
#     score < 4.0        -> SKIP
#     score = 4.0        -> 7 ct
#     score = 4.5        -> 8 ct
#     score = 5.0        -> 9 ct
#     score >= 5.5       -> 10 ct (capped)
# Counterfactual on n=88 V13b cohort: +$37.78 vs S2 +$30.37 (+24%) and
# H1-pure +$30.44 (+24%). WR 100% on kept set (skips the only losing tier).
# Tail-risk per trade ~40% higher than H1 at the score=4 tier (7 ct vs 5 ct)
# but $10 daily cap still binds well below worst-case. See research notes.
H1H4_SKIP_BELOW = 4.0
H1H4_SCORE_MULT = 1.8

# Phase 13.4 (V13b H1H4 LOOSE) — targeted score-floor relaxation. Same V13b
# score formula + H1H4 sizing for score >= 4.0. For score in [3.0, 4.0)
# (the "borderline" band), ENTERs at 3 ct IFF the existing validated edge
# is present: bb_div_band == 1 AND vol_30m_pct < 0.5. Otherwise SKIPs.
#
# Calibrated from a 15m-engine post-mortem on n=13 historical score-3.0
# winners: every single one had bb_div_band=1, every single one was a
# WIN (+$3.26 / 100% WR). The 3 historical score<2.5 losers all had
# vol_30m_pct >= 0.41 — the vol gate excludes that regime conservatively.
# Projected lift: +~$7/week on 15m engine. n=13/0 fragile; ship as a
# selectable mode (not default) so rollback is a flag flip.
LOOSE_SCORE_FLOOR = 3.0
LOOSE_BORDERLINE_HI = 4.0
LOOSE_VOL_PCT_MAX = 0.5
LOOSE_SIZE_BORDERLINE = 3

# Phase 13.1 (V13b 1to3 flat) — compressed-sizing variant for the new 1hr
# live engine. Same V13b score formula and hard gates as 5tier_v13b. SKIPs
# everything below score=4.0 (H1 floor — every V13b cohort loss sits at
# score 3.5; score >= 4 was 58/58 wins on 15m). Sizes ALL passing trades
# at a flat 3 contracts (the T3 / all-in≥4 winner from the 1hr tier sweep,
# scaled to a 1-3 ceiling for the unproven 1hr regime):
#     score < 4.0  -> SKIP
#     score >= 4.0 -> 3 ct (flat)
# At 3 ct * $0.92 max-fav-cost the worst single-trade loss is ~$2.76.
# The $10/day cap covers ~3-4 max-tier losses. Use only for the 1hr engine
# pilot. Independent default; not validated on 15m.
H1TO3_FLAT_SKIP_BELOW = 4.0
H1TO3_FLAT_SIZE = 3

# Phase 14.0 (V13b EQUITY 1ct flat) — minimum-risk launch sizing for the
# first live equity-index engine (KXINXU). Same V13b score formula + hard
# gates as 5tier_v13b. SKIPs score<4 (H1 floor). All passing trades at a
# flat 1 contract — tightest possible cap until equity-regime cutpoints
# are recalibrated from observer data.
#
# Rationale: the model + cutpoints are crypto-calibrated. We do NOT yet
# know if score>=4 carries the same WR in equities. 1ct caps single-trade
# downside at $0.92 (MAX_FAV_COST=920). Combined with --daily-cap-cents=500,
# the wallet exposure binds at ~5 losses. Promote to 3ct only after >=100
# live equity trades show projected EV holding.
EQUITY_1CT_FLAT_SKIP_BELOW = 4.0
EQUITY_1CT_FLAT_SIZE = 1

# Phase 13.2 (V13b 10-flat) — scaled-up sizing variant of 1to3_flat for the
# 1hr live engine. Same V13b score formula and hard gates. SKIPs <4, sizes
# ALL passing trades at flat 10 contracts. The T3 ("all-in >=4") winner
# from the 1hr observer tier sweep at FULL 10ct size. Worst single-trade
# loss ~$9.20 (10ct * $0.92) — ~92% of the $10/day cap, so the cap binds
# after a single max-tier loss. Use only when book depth supports 10ct
# fills (KXBTCD 463ct + KXETHD 217ct near 50c both qualify; SOL/XRP/DOGE/
# HYPE/BNB do not).
TEN_FLAT_SKIP_BELOW = 4.0
TEN_FLAT_SIZE = 10

# Phase 13.2 (V13b T6 7/10/10) — risk-balanced scale-up for the 1hr live
# engine. Same V13b score formula and hard gates. SKIPs <4, sizes by tier:
#     score < 4.0        -> SKIP
#     4.0 <= score < 5.0 -> 7 ct
#     5.0 <= score < 6.0 -> 10 ct
#     score >= 6.0       -> 10 ct
# The T6 ("asymmetric 7/10/10") variant from the 1hr observer tier sweep
# captures 95% of the T3 (all-flat-10ct) PnL while capping the marginal
# score=4 tier at 7ct — meaningful tail-risk reduction (-$4.90 worst-
# trade vs -$7.00 for T3 in the sweep). Worst single-trade loss is
# bounded at 10ct * $0.92 = ~$9.20.
T6_SKIP_BELOW = 4.0
T6_SIZE_AT_4 = 7
T6_SIZE_AT_5 = 10
T6_SIZE_AT_6 = 10

# Phase 13.3 — DOGE-specific minimum bps_margin. Calibrated to the loser
# distribution from the DOGE post-mortem (n=67): every cheap-NO loser
# clustered at bps in [8.5, 10), with a YES-side loser at bps=8.34.
# Bumping the floor to 10.0 catches 5/7 historical losers and forfeits
# 9 winners worth $1.93 — net +$4.49 backtest improvement.
DOGE_BPS_FLOOR = 10.0

ALIGN_MODES = ("disabled", "2tier", "3tier", "5tier",
               "5tier_v13b", "5tier_v13b_s2", "5tier_v13b_h1h4",
               "5tier_v13b_1to3_flat", "5tier_v13b_10_flat",
               "5tier_v13b_7_10_10", "5tier_v13b_h1h4_loose",
               "5tier_v13b_equity_1ct_flat")


class Phase4CutpointsModel:
    """Favorite-chase decision policy driven by the Phase 4 cutpoints artifact."""

    def __init__(
        self,
        cutpoints_path: str | None = None,
        align_mode: str = "disabled",
        time_of_day_skip: bool = True,
    ) -> None:
        path = Path(cutpoints_path) if cutpoints_path else _DEFAULT_CUTPOINTS
        self.cutpoints_path = path
        self.cutpoints = json.loads(path.read_text(encoding="utf-8"))
        self.vol_skip_above = self.cutpoints["vol_30m_percentile_skip_above"]
        self.vol_upsize_below = self.cutpoints["vol_30m_percentile_upsize_below"]
        self.bb_div_skip_above = self.cutpoints["bb_div_skip_above"]
        self.bb_div_upsize_below = self.cutpoints["bb_div_upsize_below"]
        self.bps_thresholds = self.cutpoints["bps_thresholds"]
        if align_mode not in ALIGN_MODES:
            raise ValueError(
                f"align_mode must be one of {ALIGN_MODES}, got {align_mode!r}"
            )
        self.align_mode = align_mode
        self.time_of_day_skip = bool(time_of_day_skip)

    # -- Model protocol (state lives in FavoriteChaseState, not the model) ----
    def update(self, event) -> None:
        """No-op: per-crypto state is held by FavoriteChaseState."""

    def fair_yes(self, ticker: str, now_ms: int) -> float | None:
        """Not provided - this is a cutpoint policy, not a fair-value model.
        The Brownian-bridge fair is computed inside ``evaluate()``."""
        return None

    # -- the real entry point ------------------------------------------------
    def evaluate(
        self,
        state: FavoriteChaseState,
        ticker: str,
        side: Side,
        favorite_mid_decicents: float,
        strike: float,
        now_ms: int,
        close_ms: int,
    ) -> Decision:
        """Apply the Phase 4 cutpoints to a favorite-chase trigger -> Decision."""
        spot = state.latest_spot()
        vol = state.vol_30m()
        utc_hour = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).hour
        diag: dict = {
            "ticker": ticker,
            "side": side.value,
            "favorite_mid_decicents": favorite_mid_decicents,
            "strike": strike,
            "spot": spot,
            "vol_30m": vol,
            "utc_hour": utc_hour,
            "time_of_day_skip_enabled": self.time_of_day_skip,
        }
        # Phase 12.5 — Rec 2: time-of-day SKIP gate (US-AM weak window).
        # Validated: combined LIVE+P4 dropped n=100 at 69% WR / -$14.31 cumulative.
        if self.time_of_day_skip and utc_hour in TOD_SKIP_HOURS:
            diag["time_of_day_skip"] = True
            return self._skip(
                ticker, side,
                f"time-of-day {utc_hour:02d}Z in {sorted(TOD_SKIP_HOURS)} window",
                diag)
        diag["time_of_day_skip"] = False
        if spot is None or vol is None:
            return self._skip(ticker, side, "no spot / vol history yet", diag)

        vol_pct = state.vol_30m_percentile(vol)
        sigma = vol / 1e4                       # bps/min -> per-minute fraction
        tau = (close_ms - now_ms) / 60_000.0    # minutes to close
        bb_yes = state.bb_fair(spot, strike, sigma, tau)
        bb_fav = bb_yes if side is Side.YES else 1.0 - bb_yes
        bb_div = state.bb_div(favorite_mid_decicents, bb_fav)
        bps_margin = abs(compute_strike_distance_bps(spot, strike))
        threshold = float(self.bps_thresholds.get(state.crypto, 0.0))

        # Phase-12.1 strong-favor zone flags (always recorded for analysis,
        # only used for sizing when align_mode != "disabled").
        s_vol = 1 if vol_pct < ALIGN_VOL_THRESHOLD else 0
        s_div = 1 if bb_div < ALIGN_DIV_THRESHOLD else 0
        s_bps = 1 if bps_margin > ALIGN_BPS_MULT * threshold else 0
        alignment_count = s_vol + s_div + s_bps

        diag.update({
            "vol_30m_pct": vol_pct,
            "sigma_per_min": sigma,
            "tau_min": tau,
            "bb_yes": bb_yes,
            "bb_fav_fair": bb_fav,
            "bb_div": bb_div,
            "bps_margin": bps_margin,
            "bps_threshold": threshold,
            "s_vol": s_vol,
            "s_div": s_div,
            "s_bps": s_bps,
            "alignment_count": alignment_count,
            "align_mode": self.align_mode,
        })

        # ---- cutpoints: skip gates (hardest veto first; all modes) ----
        if vol_pct > self.vol_skip_above:
            return self._skip(
                ticker, side,
                f"vol_pct {vol_pct:.2f} > {self.vol_skip_above}", diag)
        if bb_div > self.bb_div_skip_above:
            return self._skip(
                ticker, side,
                f"bb_div {bb_div:+.3f} > {self.bb_div_skip_above}", diag)
        # Phase 12.4: deep-negative bb_div is a smile-artifact zone where
        # the constant-vol BB overstates "cheap favorite" and the market
        # is correctly pricing additional risk. Validated multi-crypto/
        # multi-side on 14 live losses. Applies to ALL align_modes.
        if bb_div <= DEEP_DIV_SKIP:
            return self._skip(
                ticker, side,
                f"bb_div {bb_div:+.3f} <= {DEEP_DIV_SKIP} (smile zone)", diag)
        if bps_margin < threshold:
            return self._skip(
                ticker, side,
                f"bps_margin {bps_margin:.2f} < threshold {threshold:.2f}", diag)
        # Phase 13.3 — DOGE-specific bps floor. Post-mortem on 67 DOGE
        # trades found a structural cheap-NO loss cluster at bps in
        # [8.5, 10): 4 of 6 historical losses (legacy strategies) live
        # there, the YES-side loser also fell at bps=8.34. Net +$4.49
        # backtest improvement across cohorts. Applied symmetrically to
        # both sides; cap doesn't trip non-DOGE cryptos.
        if state.crypto == "DOGE" and bps_margin < DOGE_BPS_FLOOR:
            return self._skip(
                ticker, side,
                f"DOGE bps_margin {bps_margin:.2f} < per-crypto floor "
                f"{DOGE_BPS_FLOOR:.1f}", diag)

        # ---- sizing: depends on align_mode ----
        if self.align_mode == "2tier":
            # Phase 12.1 (conservative): align<=1 skip, =2 1ct, =3 2ct.
            if alignment_count <= 1:
                return self._skip(
                    ticker, side,
                    f"ALIGN_TIERED_2T skip: alignment {alignment_count} "
                    f"(s_vol={s_vol} s_div={s_div} s_bps={s_bps})",
                    diag)
            if alignment_count == 2:
                return Decision(
                    ticker=ticker, action=Action.ENTER, side=side, size=1,
                    confidence=0.7,
                    reason=(f"ALIGN_TIERED_2T 2/3: s_vol={s_vol} s_div={s_div} "
                            f"s_bps={s_bps} -> 1ct"),
                    diagnostics=diag)
            # alignment_count == 3
            return Decision(
                ticker=ticker, action=Action.ENTER, side=side, size=2,
                confidence=0.9,
                reason=(f"ALIGN_TIERED_2T 3/3: s_vol={s_vol} s_div={s_div} "
                        f"s_bps={s_bps} -> 2ct"),
                diagnostics=diag)

        if self.align_mode == "3tier":
            # Phase 12.2 (full scheme): =0 skip, =1 1ct, =2 2ct, =3 3ct.
            if alignment_count == 0:
                return self._skip(
                    ticker, side,
                    f"ALIGN_TIERED_3T skip: alignment 0 "
                    f"(s_vol={s_vol} s_div={s_div} s_bps={s_bps})",
                    diag)
            if alignment_count == 1:
                return Decision(
                    ticker=ticker, action=Action.ENTER, side=side, size=1,
                    confidence=0.5,
                    reason=(f"ALIGN_TIERED_3T 1/3: s_vol={s_vol} s_div={s_div} "
                            f"s_bps={s_bps} -> 1ct"),
                    diagnostics=diag)
            if alignment_count == 2:
                return Decision(
                    ticker=ticker, action=Action.ENTER, side=side, size=2,
                    confidence=0.7,
                    reason=(f"ALIGN_TIERED_3T 2/3: s_vol={s_vol} s_div={s_div} "
                            f"s_bps={s_bps} -> 2ct"),
                    diagnostics=diag)
            # alignment_count == 3
            return Decision(
                ticker=ticker, action=Action.ENTER, side=side, size=3,
                confidence=0.9,
                reason=(f"ALIGN_TIERED_3T 3/3: s_vol={s_vol} s_div={s_div} "
                        f"s_bps={s_bps} -> 3ct"),
                diagnostics=diag)

        if self.align_mode == "5tier":
            # Phase 12.4 — validated conviction scheme (Scheme B).
            # s_bps is the OOS-robust hard gate; the deep-div SKIP above
            # protects the smile-artifact zone. By the time we get here:
            #   s_bps == 1  (else SKIP above on bps_margin < threshold ...
            #     actually 1.5x check below) -- recompute as hard gate:
            if s_bps == 0:
                return self._skip(
                    ticker, side,
                    f"5TIER skip: s_bps=0 (bps_margin {bps_margin:.2f} <= "
                    f"{ALIGN_BPS_MULT}*{threshold:.2f})", diag)
            bb_div_band = 1 if (DEEP_DIV_SKIP < bb_div <= DIV_BAND_UPPER) else 0
            side_yes = 1 if side is Side.YES else 0
            bps_strong = 1 if bps_margin > BPS_STRONG_MULT * threshold else 0
            score = (2.0 * bb_div_band + 1.0 * s_vol
                     + 1.5 * side_yes + 2.0 * bps_strong)
            size = max(1, min(SIZE_CAP_5TIER, int(round(score)))) if score > 0 else 1
            diag.update({
                "bb_div_band": bb_div_band,
                "bps_strong": bps_strong,
                "side_yes": side_yes,
                "score_5tier": score,
            })
            # Confidence proxy = score / max_possible_score (6.5).
            conf = min(1.0, score / 6.5)
            return Decision(
                ticker=ticker, action=Action.ENTER, side=side, size=size,
                confidence=conf,
                reason=(f"5TIER score={score:.1f} -> {size}ct "
                        f"(bps_strong={bps_strong} div_band={bb_div_band} "
                        f"vol={s_vol} yes={side_yes})"),
                diagnostics=diag)

        if self.align_mode == "5tier_v13b":
            # Phase 12.6 — V13b score formula. Changes vs 5tier:
            #   - side_yes(+1.5) -> side_no(+1.5)   [side flip]
            #   - s_vol weight dropped              [noise removal]
            #   - super_band(+1) bonus on bb_div in (SUPER_BAND_LOW, SUPER_BAND_HIGH]
            # See SUPER_BAND_* / V13b doc-comments at top of module.
            if s_bps == 0:
                return self._skip(
                    ticker, side,
                    f"5TIER_V13B skip: s_bps=0 (bps_margin {bps_margin:.2f} <= "
                    f"{ALIGN_BPS_MULT}*{threshold:.2f})", diag)
            bb_div_band = 1 if (DEEP_DIV_SKIP < bb_div <= DIV_BAND_UPPER) else 0
            side_no = 1 if side is Side.NO else 0
            side_yes = 1 - side_no
            bps_strong = 1 if bps_margin > BPS_STRONG_MULT * threshold else 0
            super_band = 1 if (SUPER_BAND_LOW < bb_div <= SUPER_BAND_HIGH) else 0
            score = (2.0 * bb_div_band + 1.5 * side_no
                     + 2.0 * bps_strong + 1.0 * super_band)
            if score <= 0:
                return self._skip(
                    ticker, side,
                    f"5TIER_V13B skip: score=0 "
                    f"(div_band={bb_div_band} side_no={side_no} "
                    f"bps_strong={bps_strong} super_band={super_band})", diag)
            size = max(1, min(SIZE_CAP_5TIER, int(round(score))))
            diag.update({
                "bb_div_band": bb_div_band,
                "bps_strong": bps_strong,
                "side_yes": side_yes,
                "side_no": side_no,
                "super_band": super_band,
                "score_5tier_v13b": score,
            })
            # Max possible score = 2 + 1.5 + 2 + 1 = 6.5
            conf = min(1.0, score / 6.5)
            return Decision(
                ticker=ticker, action=Action.ENTER, side=side, size=size,
                confidence=conf,
                reason=(f"5TIER_V13B score={score:.1f} -> {size}ct "
                        f"(div_band={bb_div_band} side_no={side_no} "
                        f"bps_strong={bps_strong} super_band={super_band})"),
                diagnostics=diag)

        if self.align_mode == "5tier_v13b_s2":
            # Phase 12.12 — V13b S2 sizing. Same V13b score formula; steeper
            # conviction-tiered sizing to use --max-contracts 10 headroom.
            # See module docstring for the tier table and research summary.
            if s_bps == 0:
                return self._skip(
                    ticker, side,
                    f"5TIER_V13B_S2 skip: s_bps=0 (bps_margin {bps_margin:.2f} <= "
                    f"{ALIGN_BPS_MULT}*{threshold:.2f})", diag)
            bb_div_band = 1 if (DEEP_DIV_SKIP < bb_div <= DIV_BAND_UPPER) else 0
            side_no = 1 if side is Side.NO else 0
            side_yes = 1 - side_no
            bps_strong = 1 if bps_margin > BPS_STRONG_MULT * threshold else 0
            super_band = 1 if (SUPER_BAND_LOW < bb_div <= SUPER_BAND_HIGH) else 0
            score = (2.0 * bb_div_band + 1.5 * side_no
                     + 2.0 * bps_strong + 1.0 * super_band)
            diag.update({
                "bb_div_band": bb_div_band,
                "bps_strong": bps_strong,
                "side_yes": side_yes,
                "side_no": side_no,
                "super_band": super_band,
                "score_5tier_v13b_s2": score,
            })
            if score < S2_SKIP_BELOW:
                return self._skip(
                    ticker, side,
                    f"5TIER_V13B_S2 skip: score={score:.1f} < {S2_SKIP_BELOW} "
                    f"(div_band={bb_div_band} side_no={side_no} "
                    f"bps_strong={bps_strong} super_band={super_band})", diag)
            if score < 4.0:
                size = 3
            elif score < 5.0:
                size = S2_SIZE_AT_4
            elif score < 6.0:
                size = S2_SIZE_AT_5
            else:
                size = S2_SIZE_AT_6
            conf = min(1.0, score / 6.5)
            return Decision(
                ticker=ticker, action=Action.ENTER, side=side, size=size,
                confidence=conf,
                reason=(f"5TIER_V13B_S2 score={score:.1f} -> {size}ct "
                        f"(div_band={bb_div_band} side_no={side_no} "
                        f"bps_strong={bps_strong} super_band={super_band})"),
                diagnostics=diag)

        if self.align_mode == "5tier_v13b_h1h4":
            # Phase 12.13 — V13b H1+H4 mix. Same V13b score formula. H1's
            # score-floor (skip<4.0) + H4's smooth score-multiplier sizing.
            # See module docstring + research notes for derivation.
            if s_bps == 0:
                return self._skip(
                    ticker, side,
                    f"5TIER_V13B_H1H4 skip: s_bps=0 (bps_margin {bps_margin:.2f} <= "
                    f"{ALIGN_BPS_MULT}*{threshold:.2f})", diag)
            bb_div_band = 1 if (DEEP_DIV_SKIP < bb_div <= DIV_BAND_UPPER) else 0
            side_no = 1 if side is Side.NO else 0
            side_yes = 1 - side_no
            bps_strong = 1 if bps_margin > BPS_STRONG_MULT * threshold else 0
            super_band = 1 if (SUPER_BAND_LOW < bb_div <= SUPER_BAND_HIGH) else 0
            score = (2.0 * bb_div_band + 1.5 * side_no
                     + 2.0 * bps_strong + 1.0 * super_band)
            diag.update({
                "bb_div_band": bb_div_band,
                "bps_strong": bps_strong,
                "side_yes": side_yes,
                "side_no": side_no,
                "super_band": super_band,
                "score_5tier_v13b_h1h4": score,
            })
            if score < H1H4_SKIP_BELOW:
                return self._skip(
                    ticker, side,
                    f"5TIER_V13B_H1H4 skip: score={score:.1f} < {H1H4_SKIP_BELOW} "
                    f"(div_band={bb_div_band} side_no={side_no} "
                    f"bps_strong={bps_strong} super_band={super_band})", diag)
            size = min(S2_SIZE_AT_6, int(round(score * H1H4_SCORE_MULT)))
            conf = min(1.0, score / 6.5)
            return Decision(
                ticker=ticker, action=Action.ENTER, side=side, size=size,
                confidence=conf,
                reason=(f"5TIER_V13B_H1H4 score={score:.1f} -> {size}ct "
                        f"(div_band={bb_div_band} side_no={side_no} "
                        f"bps_strong={bps_strong} super_band={super_band})"),
                diagnostics=diag)

        if self.align_mode == "5tier_v13b_h1h4_loose":
            # Phase 13.4 — H1H4 with a targeted relaxation in [3.0, 4.0).
            # For score >= 4.0, identical to 5tier_v13b_h1h4 (skip<4, smooth
            # multiplier). For score in [3.0, 4.0), ENTERs 3ct IFF the
            # validated bb_div sweet-spot edge is present AND vol is sub-mid.
            if s_bps == 0:
                return self._skip(
                    ticker, side,
                    f"5TIER_V13B_H1H4_LOOSE skip: s_bps=0 (bps_margin "
                    f"{bps_margin:.2f} <= {ALIGN_BPS_MULT}*{threshold:.2f})", diag)
            bb_div_band = 1 if (DEEP_DIV_SKIP < bb_div <= DIV_BAND_UPPER) else 0
            side_no = 1 if side is Side.NO else 0
            side_yes = 1 - side_no
            bps_strong = 1 if bps_margin > BPS_STRONG_MULT * threshold else 0
            super_band = 1 if (SUPER_BAND_LOW < bb_div <= SUPER_BAND_HIGH) else 0
            score = (2.0 * bb_div_band + 1.5 * side_no
                     + 2.0 * bps_strong + 1.0 * super_band)
            diag.update({
                "bb_div_band": bb_div_band,
                "bps_strong": bps_strong,
                "side_yes": side_yes,
                "side_no": side_no,
                "super_band": super_band,
                "score_5tier_v13b_h1h4_loose": score,
            })
            if score < LOOSE_SCORE_FLOOR:
                return self._skip(
                    ticker, side,
                    f"5TIER_V13B_H1H4_LOOSE skip: score={score:.1f} < "
                    f"{LOOSE_SCORE_FLOOR} (div_band={bb_div_band} "
                    f"side_no={side_no} bps_strong={bps_strong} "
                    f"super_band={super_band})", diag)
            if score < LOOSE_BORDERLINE_HI:
                # Borderline tier [3.0, 4.0): gate on validated edge + vol.
                if bb_div_band != 1:
                    return self._skip(
                        ticker, side,
                        f"5TIER_V13B_H1H4_LOOSE borderline skip: "
                        f"score={score:.1f} but bb_div_band=0 (bb_div="
                        f"{bb_div:+.3f} outside ({DEEP_DIV_SKIP}, "
                        f"{DIV_BAND_UPPER}])", diag)
                if vol_pct >= LOOSE_VOL_PCT_MAX:
                    return self._skip(
                        ticker, side,
                        f"5TIER_V13B_H1H4_LOOSE borderline skip: "
                        f"score={score:.1f} but vol_pct={vol_pct:.2f} >= "
                        f"{LOOSE_VOL_PCT_MAX} (validated edge requires sub-mid vol)",
                        diag)
                size = LOOSE_SIZE_BORDERLINE
            else:
                # Identical to H1H4 above this point.
                size = min(S2_SIZE_AT_6, int(round(score * H1H4_SCORE_MULT)))
            conf = min(1.0, score / 6.5)
            return Decision(
                ticker=ticker, action=Action.ENTER, side=side, size=size,
                confidence=conf,
                reason=(f"5TIER_V13B_H1H4_LOOSE score={score:.1f} -> {size}ct "
                        f"(div_band={bb_div_band} side_no={side_no} "
                        f"bps_strong={bps_strong} super_band={super_band})"),
                diagnostics=diag)

        if self.align_mode == "5tier_v13b_7_10_10":
            # Phase 13.2 — V13b T6 asymmetric (7/10/10) for 1hr live scale-up.
            # Same V13b score + hard gates. SKIPs <4, then 7/10/10 by tier.
            if s_bps == 0:
                return self._skip(
                    ticker, side,
                    f"5TIER_V13B_7_10_10 skip: s_bps=0 (bps_margin {bps_margin:.2f} <= "
                    f"{ALIGN_BPS_MULT}*{threshold:.2f})", diag)
            bb_div_band = 1 if (DEEP_DIV_SKIP < bb_div <= DIV_BAND_UPPER) else 0
            side_no = 1 if side is Side.NO else 0
            side_yes = 1 - side_no
            bps_strong = 1 if bps_margin > BPS_STRONG_MULT * threshold else 0
            super_band = 1 if (SUPER_BAND_LOW < bb_div <= SUPER_BAND_HIGH) else 0
            score = (2.0 * bb_div_band + 1.5 * side_no
                     + 2.0 * bps_strong + 1.0 * super_band)
            diag.update({
                "bb_div_band": bb_div_band,
                "bps_strong": bps_strong,
                "side_yes": side_yes,
                "side_no": side_no,
                "super_band": super_band,
                "score_5tier_v13b_7_10_10": score,
            })
            if score < T6_SKIP_BELOW:
                return self._skip(
                    ticker, side,
                    f"5TIER_V13B_7_10_10 skip: score={score:.1f} < "
                    f"{T6_SKIP_BELOW} (div_band={bb_div_band} "
                    f"side_no={side_no} bps_strong={bps_strong} "
                    f"super_band={super_band})", diag)
            if score < 5.0:
                size = T6_SIZE_AT_4
            elif score < 6.0:
                size = T6_SIZE_AT_5
            else:
                size = T6_SIZE_AT_6
            conf = min(1.0, score / 6.5)
            return Decision(
                ticker=ticker, action=Action.ENTER, side=side, size=size,
                confidence=conf,
                reason=(f"5TIER_V13B_7_10_10 score={score:.1f} -> {size}ct "
                        f"(div_band={bb_div_band} side_no={side_no} "
                        f"bps_strong={bps_strong} super_band={super_band})"),
                diagnostics=diag)

        if self.align_mode == "5tier_v13b_10_flat":
            # Phase 13.2 — V13b flat-10ct sizing for 1hr scale-up.
            # Same V13b score + hard gates. SKIPs <4, sizes ALL passing at 10.
            if s_bps == 0:
                return self._skip(
                    ticker, side,
                    f"5TIER_V13B_10_FLAT skip: s_bps=0 (bps_margin {bps_margin:.2f} <= "
                    f"{ALIGN_BPS_MULT}*{threshold:.2f})", diag)
            bb_div_band = 1 if (DEEP_DIV_SKIP < bb_div <= DIV_BAND_UPPER) else 0
            side_no = 1 if side is Side.NO else 0
            side_yes = 1 - side_no
            bps_strong = 1 if bps_margin > BPS_STRONG_MULT * threshold else 0
            super_band = 1 if (SUPER_BAND_LOW < bb_div <= SUPER_BAND_HIGH) else 0
            score = (2.0 * bb_div_band + 1.5 * side_no
                     + 2.0 * bps_strong + 1.0 * super_band)
            diag.update({
                "bb_div_band": bb_div_band,
                "bps_strong": bps_strong,
                "side_yes": side_yes,
                "side_no": side_no,
                "super_band": super_band,
                "score_5tier_v13b_10_flat": score,
            })
            if score < TEN_FLAT_SKIP_BELOW:
                return self._skip(
                    ticker, side,
                    f"5TIER_V13B_10_FLAT skip: score={score:.1f} < "
                    f"{TEN_FLAT_SKIP_BELOW} (div_band={bb_div_band} "
                    f"side_no={side_no} bps_strong={bps_strong} "
                    f"super_band={super_band})", diag)
            size = TEN_FLAT_SIZE
            conf = min(1.0, score / 6.5)
            return Decision(
                ticker=ticker, action=Action.ENTER, side=side, size=size,
                confidence=conf,
                reason=(f"5TIER_V13B_10_FLAT score={score:.1f} -> {size}ct "
                        f"(div_band={bb_div_band} side_no={side_no} "
                        f"bps_strong={bps_strong} super_band={super_band})"),
                diagnostics=diag)

        if self.align_mode == "5tier_v13b_1to3_flat":
            # Phase 13.1 — V13b flat-3ct sizing for the new 1hr engine.
            # Same V13b score + hard gates. SKIPs <4, sizes ALL passing at 3.
            if s_bps == 0:
                return self._skip(
                    ticker, side,
                    f"5TIER_V13B_1TO3_FLAT skip: s_bps=0 (bps_margin {bps_margin:.2f} <= "
                    f"{ALIGN_BPS_MULT}*{threshold:.2f})", diag)
            bb_div_band = 1 if (DEEP_DIV_SKIP < bb_div <= DIV_BAND_UPPER) else 0
            side_no = 1 if side is Side.NO else 0
            side_yes = 1 - side_no
            bps_strong = 1 if bps_margin > BPS_STRONG_MULT * threshold else 0
            super_band = 1 if (SUPER_BAND_LOW < bb_div <= SUPER_BAND_HIGH) else 0
            score = (2.0 * bb_div_band + 1.5 * side_no
                     + 2.0 * bps_strong + 1.0 * super_band)
            diag.update({
                "bb_div_band": bb_div_band,
                "bps_strong": bps_strong,
                "side_yes": side_yes,
                "side_no": side_no,
                "super_band": super_band,
                "score_5tier_v13b_1to3_flat": score,
            })
            if score < H1TO3_FLAT_SKIP_BELOW:
                return self._skip(
                    ticker, side,
                    f"5TIER_V13B_1TO3_FLAT skip: score={score:.1f} < "
                    f"{H1TO3_FLAT_SKIP_BELOW} (div_band={bb_div_band} "
                    f"side_no={side_no} bps_strong={bps_strong} "
                    f"super_band={super_band})", diag)
            size = H1TO3_FLAT_SIZE
            conf = min(1.0, score / 6.5)
            return Decision(
                ticker=ticker, action=Action.ENTER, side=side, size=size,
                confidence=conf,
                reason=(f"5TIER_V13B_1TO3_FLAT score={score:.1f} -> {size}ct "
                        f"(div_band={bb_div_band} side_no={side_no} "
                        f"bps_strong={bps_strong} super_band={super_band})"),
                diagnostics=diag)

        if self.align_mode == "5tier_v13b_equity_1ct_flat":
            # Phase 14.0 — minimum-risk launch sizing for the first live
            # equity-index engine. Same V13b score + hard gates. SKIPs <4,
            # sizes ALL passing trades at flat 1 contract. Worst per-trade
            # loss ~$0.92 at MAX_FAV_COST=920; daily cap binds at ~5 losses.
            # Cutpoints are crypto-calibrated — the boot envelope must warn.
            if s_bps == 0:
                return self._skip(
                    ticker, side,
                    f"5TIER_V13B_EQUITY_1CT_FLAT skip: s_bps=0 "
                    f"(bps_margin {bps_margin:.2f} <= "
                    f"{ALIGN_BPS_MULT}*{threshold:.2f})", diag)
            bb_div_band = 1 if (DEEP_DIV_SKIP < bb_div <= DIV_BAND_UPPER) else 0
            side_no = 1 if side is Side.NO else 0
            side_yes = 1 - side_no
            bps_strong = 1 if bps_margin > BPS_STRONG_MULT * threshold else 0
            super_band = 1 if (SUPER_BAND_LOW < bb_div <= SUPER_BAND_HIGH) else 0
            score = (2.0 * bb_div_band + 1.5 * side_no
                     + 2.0 * bps_strong + 1.0 * super_band)
            diag.update({
                "bb_div_band": bb_div_band,
                "bps_strong": bps_strong,
                "side_yes": side_yes,
                "side_no": side_no,
                "super_band": super_band,
                "score_5tier_v13b_equity_1ct_flat": score,
            })
            if score < EQUITY_1CT_FLAT_SKIP_BELOW:
                return self._skip(
                    ticker, side,
                    f"5TIER_V13B_EQUITY_1CT_FLAT skip: score={score:.1f} < "
                    f"{EQUITY_1CT_FLAT_SKIP_BELOW} (div_band={bb_div_band} "
                    f"side_no={side_no} bps_strong={bps_strong} "
                    f"super_band={super_band})", diag)
            size = EQUITY_1CT_FLAT_SIZE
            conf = min(1.0, score / 6.5)
            return Decision(
                ticker=ticker, action=Action.ENTER, side=side, size=size,
                confidence=conf,
                reason=(f"5TIER_V13B_EQUITY_1CT_FLAT score={score:.1f} -> {size}ct "
                        f"(div_band={bb_div_band} side_no={side_no} "
                        f"bps_strong={bps_strong} super_band={super_band})"),
                diagnostics=diag)

        # ---- align_mode == "disabled": original UPSIZE_2X / ENTER_1X ----
        if vol_pct < self.vol_upsize_below and bb_div < self.bb_div_upsize_below:
            return Decision(
                ticker=ticker, action=Action.ENTER, side=side, size=2,
                confidence=0.9,
                reason=(f"UPSIZE_2X: vol_pct {vol_pct:.2f} < {self.vol_upsize_below} "
                        f"and bb_div {bb_div:+.3f} < {self.bb_div_upsize_below}"),
                diagnostics=diag)
        return Decision(
            ticker=ticker, action=Action.ENTER, side=side, size=1,
            confidence=0.6, reason="ENTER_1X: passed all cutpoints",
            diagnostics=diag)

    @staticmethod
    def _skip(ticker: str, side: Side, why: str, diag: dict) -> Decision:
        return Decision(
            ticker=ticker, action=Action.SKIP, side=side, size=0,
            confidence=0.0, reason=f"SKIP: {why}", diagnostics=diag)
