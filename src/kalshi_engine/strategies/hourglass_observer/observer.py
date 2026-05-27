"""Hourglass 1hr observer strategy.

Subscribes to KX{C}D 1hr digital markets and emits `book_at_1hr_pretrigger`
envelopes at configurable τ minutes into each cycle. Pure observation: no
decisions, no orders, no risk-envelope interaction.

Envelope schema (per emit):
    {
      kind:                      "book_at_1hr_pretrigger",
      ticker:                    str,
      ts_ms:                     int,         # book.recv_ms
      cycle_open_ms:             int,
      cycle_close_ms:             int,
      elapsed_min:               float,       # since cycle_open
      tau_min:                   float,       # to cycle_close
      window_label:              str,         # one of {"T+30","T+40","T+45","T+50","T+55"}
      yes_bid: yes_ask: no_bid: no_ask:  int (decicents)
      spot:                      float | None
      vol_30m:                   float | None
      bb_div:                    float | None  # constant-vol BB, best-effort
      bps_margin:                float | None
      favorite_side:             "yes" | "no"
      favorite_mid_decicents:    float
    }
"""

from __future__ import annotations

from dataclasses import dataclass
from math import log
from statistics import NormalDist

from kalshi_engine.core.events import BookEvent, LifecycleEvent, SettlementEvent, SpotEvent
from kalshi_engine.research.sr_features import all_sr_features
from kalshi_engine.risk.envelope import crypto_of_ticker
from kalshi_engine.strategies.favorite_chase.state import FavoriteChaseState

_NORM = NormalDist()

# 24h+1h slack retention for the S/R feature buffers. Independent of
# FavoriteChaseState.spot_buffer (32m) so the long-window BB/pivot features
# have enough history at envelope time.
_LONG_BUFFER_MS = 25 * 3_600_000


@dataclass(frozen=True)
class HourMarketMeta:
    ticker: str
    strike: float
    open_ms: int
    close_ms: int


def _cycle_oi_aggregates(this_ticker: str, this_oi: float | None,
                          cycle_open_ms: int, spot: float | None,
                          markets: dict, oi_cache: dict) -> dict:
    """Phase 14.13 - per-cycle OI aggregates computed from the cache of all
    same-cycle strikes. Returns a dict of fields to merge into the envelope.
    Missing values (no OI yet for a ticker) are simply omitted from the
    cycle population - we compute aggregates over whatever subset is known.
    """
    cycle = []
    for tk, meta in markets.items():
        if int(meta.open_ms) != int(cycle_open_ms):
            continue
        oi = oi_cache.get(tk)
        if oi is None:
            continue
        cycle.append((tk, float(meta.strike), float(oi)))
    if not cycle:
        return {
            "open_interest": this_oi,
            "oi_share": None,
            "cycle_total_oi": None,
            "cycle_n_strikes_with_oi": 0,
            "cycle_oi_variance": None,
            "cycle_oi_top_strike": None,
            "cycle_oi_top_ticker": None,
            "cycle_oi_concentration_gini": None,
            "cycle_oi_top_strike_dist_bps": None,
        }
    total = sum(oi for _, _, oi in cycle)
    n = len(cycle)
    # Variance (population, since we have the full cycle population)
    if n >= 2:
        mean = total / n
        variance = sum((oi - mean) ** 2 for _, _, oi in cycle) / n
    else:
        variance = 0.0
    # Top strike by OI
    cycle_sorted = sorted(cycle, key=lambda x: -x[2])
    top_ticker, top_strike, top_oi = cycle_sorted[0]
    # Gini concentration (0 = perfectly even, 1 = all OI on one strike)
    if total > 0:
        ois_sorted = sorted(oi for _, _, oi in cycle)
        cumsum = sum((i + 1) * oi for i, oi in enumerate(ois_sorted))
        gini = (2 * cumsum) / (n * total) - (n + 1) / n
    else:
        gini = None
    # This ticker's share of the cycle total
    if this_oi is not None and total > 0:
        share = float(this_oi) / total
    else:
        share = None
    # Distance from spot to top-OI strike (bps)
    if spot is not None and spot > 0:
        dist_bps = (top_strike - spot) / spot * 1e4
    else:
        dist_bps = None
    return {
        "open_interest": this_oi,
        "oi_share": share,
        "cycle_total_oi": total,
        "cycle_n_strikes_with_oi": n,
        "cycle_oi_variance": variance,
        "cycle_oi_top_strike": top_strike,
        "cycle_oi_top_ticker": top_ticker,
        "cycle_oi_concentration_gini": gini,
        "cycle_oi_top_strike_dist_bps": dist_bps,
    }


class HourglassObserverStrategy:
    """1hr observer — pure observability, no orders.

    Sampling windows (default): T+30, T+40, T+45, T+50, T+55 minutes into the
    cycle. The first book event whose elapsed-minutes lands within ±15s of
    each target triggers a single envelope per window per ticker.
    """

    # Half-width of each sampling window around the target minute (seconds).
    # E.g., T+30 is sampled by the first book at elapsed >= 30:00 and
    # < 30:30 that hasn't been sampled yet.
    _WINDOW_HALF_S = 30

    def __init__(
        self,
        log_writer,
        observe_minutes: tuple[int, ...] = (30, 40, 45, 50, 55),
        liquidity_poller=None,
    ) -> None:
        if log_writer is None:
            raise ValueError("HourglassObserverStrategy requires a log_writer")
        self._log = log_writer
        self.observe_minutes = tuple(observe_minutes)
        self.markets: dict[str, HourMarketMeta] = {}
        # Per-crypto rolling state (for spot/vol/bb_div diagnostics).
        self._states: dict[str, FavoriteChaseState] = {}
        # Per-ticker per-window: True iff already emitted for that window
        # in this cycle. Cleared on settlement.
        self._sampled: dict[str, set[int]] = {}
        # Phase 14.2a — long-window spot history (24h+) per crypto for S/R
        # features. FavoriteChaseState.spot_buffer caps at 32m which is too
        # short for 4h/24h Bollinger / pivot windows. List of (ts_ms, price).
        self._long_spot_history: dict[str, list[tuple[int, float]]] = {}
        # Optional liquidity poller — must expose .get_depth(crypto) -> dict
        # with keys bid_depth, ask_depth, spread_bps (or any subset). None
        # means liquidity fields will be omitted from envelopes.
        self._liquidity_poller = liquidity_poller
        # Phase 14.13 - per-ticker latest open_interest (refreshed by
        # discovery loop). Per-cycle aggregates are computed at envelope time.
        self._oi: dict[str, float] = {}

    # -- registration ---------------------------------------------------------
    def register_market(self, ticker: str, strike: float,
                         open_ms: int, close_ms: int) -> None:
        self.markets[ticker] = HourMarketMeta(
            ticker, float(strike), int(open_ms), int(close_ms),
        )
        self._sampled.setdefault(ticker, set())

    def update_open_interest(self, ticker: str, oi: float | None) -> None:
        """Phase 14.13 - refresh per-ticker open_interest from REST discovery.
        Called by the observer entrypoint each time it polls /markets."""
        if oi is None:
            return
        try:
            self._oi[ticker] = float(oi)
        except (TypeError, ValueError):
            pass

    def _state(self, crypto: str) -> FavoriteChaseState:
        if crypto not in self._states:
            self._states[crypto] = FavoriteChaseState(crypto)
        return self._states[crypto]

    # -- event routing --------------------------------------------------------
    def on_event(self, event, model=None):
        """Route one event. Always returns None — never emits Decisions."""
        if isinstance(event, SpotEvent):
            crypto = event.crypto.value
            self._state(crypto).update_spot(event)
            # Phase 14.2a — also append to the long buffer for S/R features.
            buf = self._long_spot_history.setdefault(crypto, [])
            buf.append((event.ts_ms, event.price))
            cutoff = event.ts_ms - _LONG_BUFFER_MS
            # Trim in-place (last-touched timestamp is monotonic).
            while buf and buf[0][0] < cutoff:
                buf.pop(0)
            return None
        if isinstance(event, BookEvent):
            self._on_book(event)
            return None
        if isinstance(event, LifecycleEvent):
            # Lifecycle metadata can register a market if discovery missed it.
            if event.strike and event.open_ms and event.close_ms:
                self.register_market(
                    event.ticker, event.strike, event.open_ms, event.close_ms,
                )
            return None
        if isinstance(event, SettlementEvent):
            # Clear sampling state for this cycle so the next cycle of the
            # same series starts fresh (tickers are per-cycle so this is
            # belt-and-suspenders).
            self._sampled.pop(event.ticker, None)
            return None
        return None

    # -- the actual observation logic ----------------------------------------
    def _on_book(self, book: BookEvent) -> None:
        meta = self.markets.get(book.ticker)
        if meta is None:
            return  # unregistered market — no cycle timing available
        elapsed_ms = book.recv_ms - meta.open_ms
        elapsed_min = elapsed_ms / 60_000.0
        # Identify which sampling window this book lands in (if any).
        window_min = None
        for m in self.observe_minutes:
            # Window: [m, m + 0.5)  i.e. first 30 seconds of minute m
            if m <= elapsed_min < m + (self._WINDOW_HALF_S / 60.0):
                window_min = m
                break
        if window_min is None:
            return
        sampled = self._sampled.setdefault(book.ticker, set())
        if window_min in sampled:
            return  # already emitted for this window in this cycle
        sampled.add(window_min)
        self._emit_envelope(book, meta, window_min, elapsed_min)

    def _emit_envelope(
        self, book: BookEvent, meta: HourMarketMeta,
        window_min: int, elapsed_min: float,
    ) -> None:
        crypto = crypto_of_ticker(book.ticker)
        state = self._state(crypto)
        spot = state.latest_spot()
        vol = state.vol_30m()
        # Favorite by mid (NOT the 75c rule — we want observability even
        # before a side hits 75c).
        yes_mid = (book.yes_bid + book.yes_ask) / 2.0
        no_mid = (book.no_bid + book.no_ask) / 2.0
        if yes_mid >= no_mid:
            fav_side, fav_mid = "yes", yes_mid
        else:
            fav_side, fav_mid = "no", no_mid
        # bb_div: constant-vol Brownian bridge (best-effort; None if no spot/vol).
        bb_div = None
        bps_margin = None
        # Phase 14.7 - d_norm = bps_margin / (vol_30m * sqrt(tau_min)). The
        # vol-normalized Brownian-bridge distance to strike. Distance analysis
        # at _tmp_analysis/distance_per_rung_1hr/ found d_norm in [1.5, 2.0]
        # is the "looks safe but isn't" loser-cluster band; instrumenting
        # here so future ENTERs can be backtested + (Phase 14.8) gated.
        d_norm = None
        if spot is not None and vol is not None and meta.strike > 0:
            sigma = vol / 1e4
            tau = (meta.close_ms - book.recv_ms) / 60_000.0
            if sigma > 0 and tau > 0:
                try:
                    bb_yes = _NORM.cdf(
                        log(spot / meta.strike) / (sigma * (tau ** 0.5))
                    )
                    bb_fav = bb_yes if fav_side == "yes" else 1.0 - bb_yes
                    bb_div = float(fav_mid) / 1000.0 - bb_fav
                except (ValueError, ZeroDivisionError):
                    pass
            bps_margin = abs(spot - meta.strike) / meta.strike * 1e4
            if vol > 0 and tau > 0:
                # bps_margin is already in bps; vol*sqrt(tau) yields bps too
                # (vol is bps/min, tau is min).
                d_norm = bps_margin / (vol * (tau ** 0.5))
        # Phase 13.3: top-of-book depth from the level ladders. Each
        # `yes_levels` / `no_levels` entry is (price_decicents, size_contracts);
        # the bid is the entry whose price matches `yes_bid`, the ask is
        # the matching `yes_ask`. Best-effort lookup — None if not present.
        def _size_at(levels, price):
            for p, sz in levels:
                if p == price:
                    return float(sz)
            return None
        yes_bid_size = _size_at(book.yes_levels, book.yes_bid)
        yes_ask_size = _size_at(book.yes_levels, book.yes_ask)
        no_bid_size = _size_at(book.no_levels, book.no_bid)
        no_ask_size = _size_at(book.no_levels, book.no_ask)
        # Phase 14.2a — S/R features from the long spot history.
        long_hist = self._long_spot_history.get(crypto, [])
        sr = all_sr_features(spot, long_hist, book.recv_ms)
        # Optional liquidity poll (Bitstamp depth). Cached/throttled by the
        # poller itself — we just ask for fresh-ish data here.
        liq = {}
        if self._liquidity_poller is not None:
            try:
                d = self._liquidity_poller.get_depth(crypto)
                if d:
                    liq = {
                        "bitstamp_bid_depth_0p5pct": d.get("bid_depth_0p5pct"),
                        "bitstamp_ask_depth_0p5pct": d.get("ask_depth_0p5pct"),
                        "bitstamp_bid_depth_1pct": d.get("bid_depth_1pct"),
                        "bitstamp_ask_depth_1pct": d.get("ask_depth_1pct"),
                        "bitstamp_spread_bps": d.get("spread_bps"),
                        "bitstamp_mid": d.get("mid"),
                    }
            except Exception as exc:
                liq = {"bitstamp_poll_error": repr(exc)[:80]}
        # Phase 14.13 - OI aggregates over the cycle's strikes.
        oi_features = _cycle_oi_aggregates(
            book.ticker, self._oi.get(book.ticker),
            meta.open_ms, spot, self.markets, self._oi,
        )
        self._log.write({
            "kind": "book_at_1hr_pretrigger",
            "ticker": book.ticker,
            "ts_ms": book.recv_ms,
            "cycle_open_ms": meta.open_ms,
            "cycle_close_ms": meta.close_ms,
            "elapsed_min": elapsed_min,
            "tau_min": (meta.close_ms - book.recv_ms) / 60_000.0,
            "window_label": f"T+{window_min}",
            "yes_bid": book.yes_bid, "yes_ask": book.yes_ask,
            "no_bid": book.no_bid, "no_ask": book.no_ask,
            "yes_bid_size_fp": yes_bid_size,
            "yes_ask_size_fp": yes_ask_size,
            "no_bid_size_fp": no_bid_size,
            "no_ask_size_fp": no_ask_size,
            "spot": spot,
            "vol_30m": vol,
            "bb_div": bb_div,
            "bps_margin": bps_margin,
            "d_norm": d_norm,
            "favorite_side": fav_side,
            "favorite_mid_decicents": fav_mid,
            "strike": meta.strike,
            # Phase 14.2a S/R features
            "bb_pos_1h": sr.get("bb_pos_1h"),
            "bb_pos_4h": sr.get("bb_pos_4h"),
            "bb_pos_24h": sr.get("bb_pos_24h"),
            "pivot": sr.get("pivot"),
            "pivot_R1": sr.get("pivot_R1"),
            "pivot_S1": sr.get("pivot_S1"),
            "dist_to_R1": sr.get("dist_to_R1"),
            "dist_to_S1": sr.get("dist_to_S1"),
            "window_24h_high": sr.get("window_high"),
            "window_24h_low": sr.get("window_low"),
            "long_history_n": len(long_hist),
            **liq,
            # Phase 14.13 OI features
            **oi_features,
        })
