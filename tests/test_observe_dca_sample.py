"""Tests for observe_dca_sample — the Phase 14.18 DCA dense book sampler.

Covers the pure sample builder (schema + V13B component math), the
``emit_samples`` polling routine against a mocked book feed (favorite-mid
floor, cycle-window gating, missing-book skip), strike parsing, and the
run-loop deadline enforcement. No live Kalshi WS / Bitstamp poll is spun up;
the integration feeds have their own tests.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import MagicMock

from kalshi_engine.core.events import BookEvent, SpotEvent
from kalshi_engine.core.types import Crypto, Venue

from kalshi_engine.bin.observe_dca_sample import (
    _DcaSampleState,
    _run_loop,
    _strike_from_market,
    build_dca_sample,
    emit_samples,
)
from kalshi_engine.strategies.favorite_chase.state import FavoriteChaseState

# Matches cutpoints v1 (what the live 1hr trader uses).
BPS_THRESHOLDS = {"BTC": 3.9491400035563857, "ETH": 4.807712853473733}


def _make_log():
    log = MagicMock()
    log.writes = []
    log.write = lambda rec: log.writes.append(rec)
    return log


def _no_favorite_book(ticker: str, recv_ms: int) -> BookEvent:
    """NO side rich at ~$0.80 — the favorite by mid and by the 75c rule."""
    return BookEvent(
        ticker=ticker, ts_ms=recv_ms, recv_ms=recv_ms,
        yes_bid=190, yes_ask=210, no_bid=790, no_ask=810,
        yes_levels=((190, 12.0), (210, 8.0)),
        no_levels=((790, 33.0), (810, 21.0)),
    )


def _flat_otm_book(ticker: str, recv_ms: int) -> BookEvent:
    """Both sides near 50c — undecided, favorite mid 500 is below the band."""
    return BookEvent(
        ticker=ticker, ts_ms=recv_ms, recv_ms=recv_ms,
        yes_bid=480, yes_ask=520, no_bid=480, no_ask=520,
        yes_levels=((480, 5.0), (520, 5.0)),
        no_levels=((480, 5.0), (520, 5.0)),
    )


def _deep_itm_book(ticker: str, recv_ms: int) -> BookEvent:
    """YES side decided at ~$0.995 — favorite mid 995 is ABOVE the band (no DCA
    dip left to catch). A single floor would NOT exclude this; the upper bound
    must."""
    return BookEvent(
        ticker=ticker, ts_ms=recv_ms, recv_ms=recv_ms,
        yes_bid=990, yes_ask=1000, no_bid=0, no_ask=10,
        yes_levels=((990, 838.0),), no_levels=((10, 5.0),),
    )


def _warm_state(crypto: str, base_price: float) -> FavoriteChaseState:
    """A FavoriteChaseState with ~31 min of 1-min spot history so vol_30m and
    the Brownian-bridge fair resolve. Prices oscillate slightly so vol > 0."""
    state = FavoriteChaseState(crypto)
    t0 = 1_800_000_000_000
    for i in range(32):
        px = base_price + (5.0 if i % 2 == 0 else -5.0)
        state.update_spot(SpotEvent(
            crypto=Crypto(crypto), venue=Venue.BITSTAMP,
            ts_ms=t0 + i * 60_000, recv_ms=t0 + i * 60_000, price=px,
        ))
    return state


# ---- pure sample builder -------------------------------------------------

def _meta(open_ms: int, close_ms: int, strike: float = 100_500.0) -> dict:
    return {"strike": strike, "open_ms": open_ms, "close_ms": close_ms,
            "series": "KXBTCD"}


def test_build_sample_schema_has_all_fields():
    open_ms = 1_800_000_000_000
    sample_ms = open_ms + 12 * 60_000
    close_ms = open_ms + 60 * 60_000
    book = _no_favorite_book("KXBTCD-CYC-T100500", sample_ms)
    state = _warm_state("BTC", 100_000.0)
    rec = build_dca_sample(book, _meta(open_ms, close_ms),
                           state, BPS_THRESHOLDS["BTC"], sample_ms)
    expected = {
        "kind", "ticker", "crypto", "series", "ts_ms", "book_recv_ms",
        "book_age_ms", "cycle_open_ms", "cycle_close_ms", "sec_into_cycle",
        "elapsed_min", "tau_min", "strike", "yes_bid", "yes_ask", "no_bid",
        "no_ask", "yes_bid_size_fp", "yes_ask_size_fp", "no_bid_size_fp",
        "no_ask_size_fp", "favorite_side", "favorite_mid_decicents",
        "favorite_75c_side", "book_size_top_fav_bid_fp", "spot", "vol_30m",
        "vol_30m_pct", "bb_div", "bps_margin", "d_norm", "bps_threshold",
        "bb_div_band", "side_no", "side_yes", "bps_strong", "super_band",
        "s_bps", "v13b_score",
    }
    assert expected.issubset(rec.keys())
    assert rec["kind"] == "dca_book_sample"
    assert rec["crypto"] == "BTC"
    assert rec["series"] == "KXBTCD"


def test_build_sample_favorite_is_no_side():
    open_ms = 1_800_000_000_000
    sample_ms = open_ms + 5 * 60_000
    book = _no_favorite_book("KXBTCD-CYC-T100500", sample_ms)
    state = _warm_state("BTC", 100_000.0)
    rec = build_dca_sample(book, _meta(open_ms, open_ms + 3_600_000),
                           state, BPS_THRESHOLDS["BTC"], sample_ms)
    assert rec["favorite_side"] == "no"
    assert rec["favorite_mid_decicents"] == 800.0
    assert rec["favorite_75c_side"] == "no"
    # Top-of-book depth at the favorite (NO) bid = 790 -> 33 contracts.
    assert rec["book_size_top_fav_bid_fp"] == 33.0
    assert rec["no_bid_size_fp"] == 33.0
    assert rec["yes_bid_size_fp"] == 12.0


def test_build_sample_sec_into_cycle():
    open_ms = 1_800_000_000_000
    sample_ms = open_ms + 137_000  # 137 s in
    book = _no_favorite_book("KXBTCD-CYC-T100500", sample_ms)
    state = _warm_state("BTC", 100_000.0)
    rec = build_dca_sample(book, _meta(open_ms, open_ms + 3_600_000),
                           state, BPS_THRESHOLDS["BTC"], sample_ms)
    assert rec["sec_into_cycle"] == 137.0
    assert abs(rec["elapsed_min"] - 137.0 / 60.0) < 1e-9


def test_build_sample_v13b_components_warmed():
    open_ms = 1_800_000_000_000
    sample_ms = open_ms + 12 * 60_000
    close_ms = sample_ms + 30 * 60_000  # tau = 30 min
    book = _no_favorite_book("KXBTCD-CYC-T100500", sample_ms)
    state = _warm_state("BTC", 100_000.0)  # spot ~100k, strike 100.5k
    rec = build_dca_sample(book, _meta(open_ms, close_ms),
                           state, BPS_THRESHOLDS["BTC"], sample_ms)
    # spot/vol warmed -> these resolve.
    assert rec["spot"] is not None
    assert rec["vol_30m"] is not None and rec["vol_30m"] > 0
    assert rec["bb_div"] is not None
    assert rec["d_norm"] is not None
    # bps_margin is deterministic from spot/strike (vol-independent): spot is
    # ~100k, strike 100.5k -> |spot - strike| / spot * 1e4 ~ 50 bps.
    assert 49.0 < rec["bps_margin"] < 52.0
    # NO favorite -> side_no flag set.
    assert rec["side_no"] == 1
    assert rec["side_yes"] == 0
    # 50 bps clears both bps gates given BTC threshold ~3.95.
    assert rec["bps_strong"] == 1
    assert rec["s_bps"] == 1
    # Internal consistency of the V13B score formula.
    expected_score = (2.0 * rec["bb_div_band"] + 1.5 * rec["side_no"]
                      + 2.0 * rec["bps_strong"] + 1.0 * rec["super_band"])
    assert rec["v13b_score"] == expected_score


def test_build_sample_no_spot_history_components_none():
    """Cold state (no spot ticks): favorite/book fields still resolve, but the
    V13B/spot-derived fields are None rather than raising."""
    open_ms = 1_800_000_000_000
    sample_ms = open_ms + 5 * 60_000
    book = _no_favorite_book("KXBTCD-CYC-T100500", sample_ms)
    state = FavoriteChaseState("BTC")  # cold
    rec = build_dca_sample(book, _meta(open_ms, open_ms + 3_600_000),
                           state, BPS_THRESHOLDS["BTC"], sample_ms)
    assert rec["favorite_side"] == "no"
    assert rec["favorite_mid_decicents"] == 800.0
    assert rec["spot"] is None
    assert rec["vol_30m"] is None
    assert rec["bb_div"] is None
    assert rec["bps_margin"] is None
    assert rec["v13b_score"] is None
    assert rec["bb_div_band"] is None


# ---- emit_samples (mocked book feed) -------------------------------------

def _state_with_market(ticker, open_ms, close_ms, strike=100_500.0,
                       crypto="BTC", base_price=100_000.0):
    state = _DcaSampleState()
    state.register(ticker, strike, open_ms, close_ms, "KXBTCD")
    state._states[crypto] = _warm_state(crypto, base_price)
    return state


def test_emit_samples_writes_for_active_favorite_market():
    open_ms = 1_800_000_000_000
    close_ms = open_ms + 60 * 60_000
    sample_ms = open_ms + 10 * 60_000
    ticker = "KXBTCD-CYC-T100500"
    state = _state_with_market(ticker, open_ms, close_ms)
    state.on_book(_no_favorite_book(ticker, sample_ms))
    log = _make_log()
    n = emit_samples(state, BPS_THRESHOLDS, log, sample_ms,
                     min_favorite_mid_dc=600.0, max_favorite_mid_dc=970.0)
    assert n == 1
    assert len(log.writes) == 1
    assert log.writes[0]["kind"] == "dca_book_sample"
    assert log.writes[0]["ticker"] == ticker


def test_emit_samples_skips_below_favorite_band():
    """A market with both sides near 50c (favorite mid 500 < 600 band low) is
    not logged — undecided ~50/50 noise."""
    open_ms = 1_800_000_000_000
    close_ms = open_ms + 60 * 60_000
    sample_ms = open_ms + 10 * 60_000
    ticker = "KXBTCD-CYC-T100500"
    state = _state_with_market(ticker, open_ms, close_ms)
    state.on_book(_flat_otm_book(ticker, sample_ms))
    log = _make_log()
    n = emit_samples(state, BPS_THRESHOLDS, log, sample_ms,
                     min_favorite_mid_dc=600.0, max_favorite_mid_dc=970.0)
    assert n == 0
    assert log.writes == []


def test_emit_samples_skips_above_favorite_band():
    """A deep-ITM market (favorite mid 995 > 970 band high) is not logged —
    decided, no DCA dip left. Critical: its favorite side sits near 1000, so a
    single floor would wrongly KEEP it; the upper bound excludes it."""
    open_ms = 1_800_000_000_000
    close_ms = open_ms + 60 * 60_000
    sample_ms = open_ms + 10 * 60_000
    ticker = "KXBTCD-CYC-T100500"
    state = _state_with_market(ticker, open_ms, close_ms)
    state.on_book(_deep_itm_book(ticker, sample_ms))
    log = _make_log()
    n = emit_samples(state, BPS_THRESHOLDS, log, sample_ms,
                     min_favorite_mid_dc=600.0, max_favorite_mid_dc=970.0)
    assert n == 0
    assert log.writes == []


def test_emit_samples_skips_market_outside_cycle_window():
    open_ms = 1_800_000_000_000
    close_ms = open_ms + 60 * 60_000
    ticker = "KXBTCD-CYC-T100500"
    state = _state_with_market(ticker, open_ms, close_ms)
    # Sample BEFORE the cycle opens and AFTER it closes.
    log = _make_log()
    state.on_book(_no_favorite_book(ticker, open_ms - 1))
    assert emit_samples(state, BPS_THRESHOLDS, log, open_ms - 1, 600.0, 970.0) == 0
    assert emit_samples(state, BPS_THRESHOLDS, log, close_ms + 1, 600.0, 970.0) == 0
    assert log.writes == []


def test_emit_samples_skips_market_with_no_book():
    open_ms = 1_800_000_000_000
    close_ms = open_ms + 60 * 60_000
    sample_ms = open_ms + 10 * 60_000
    state = _state_with_market("KXBTCD-CYC-T100500", open_ms, close_ms)
    log = _make_log()  # no on_book call -> no latest_book
    assert emit_samples(state, BPS_THRESHOLDS, log, sample_ms, 600.0, 970.0) == 0
    assert log.writes == []


# ---- strike parse --------------------------------------------------------

def test_strike_from_market_floor_strike():
    assert _strike_from_market({"floor_strike": 100500.0, "ticker": "X"}) == 100500.0


def test_strike_from_market_ticker_fallback():
    m = {"floor_strike": None, "ticker": "KXDOGED-26MAY2617-T0.1949999"}
    assert _strike_from_market(m) == 0.1949999


def test_strike_from_market_returns_zero_on_bad():
    assert _strike_from_market({"ticker": "no-strike-here"}) == 0.0


# ---- run_loop deadline enforcement ---------------------------------------

class _SilentKalshiWs:
    """WS stub whose events() blocks forever — the deadline must override it."""
    async def events(self):
        await asyncio.Event().wait()
        if False:
            yield None  # pragma: no cover — async-iter contract


class _SilentSpotFeed:
    async def events(self):
        await asyncio.Event().wait()
        if False:
            yield None  # pragma: no cover


def test_run_loop_honors_deadline_and_samples():
    """With quiet feeds, _run_loop must still exit at the deadline AND the
    sampler timer must have emitted a sample for the pre-loaded active book."""
    now_ms = int(time.time() * 1000)
    open_ms = now_ms - 10 * 60_000
    close_ms = now_ms + 50 * 60_000
    ticker = "KXBTCD-CYC-T100500"
    state = _state_with_market(ticker, open_ms, close_ms)
    state.on_book(_no_favorite_book(ticker, now_ms))
    log = _make_log()
    start = time.time()
    asyncio.run(_run_loop(
        state, BPS_THRESHOLDS, _SilentKalshiWs(), _SilentSpotFeed(), log,
        sample_interval_s=0.2, min_favorite_mid_dc=600.0,
        max_favorite_mid_dc=970.0, duration_s=0.6,
    ))
    elapsed = time.time() - start
    assert elapsed < 2.0, f"run_loop overshot deadline: {elapsed:.2f}s"
    samples = [w for w in log.writes if w.get("kind") == "dca_book_sample"]
    assert len(samples) >= 1
