"""Phase 14.12 - LadderStrategy unit tests.

Covers:
- Fires at T+30 with N qualifying rungs -> N orders (up to max_rungs)
- d_norm floor, fav-price range, depth filter
- Per-cycle dedup (no re-fire at T+40)
- Crypto allowlist (BTC only)
- Daily-cap binding via settlement
- Disabled by default produces 0 orders
- Sort by d_norm descending (prefer farthest/safest)
"""
from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import MagicMock

import pytest

from kalshi_engine.core.events import BookEvent, SettlementEvent
from kalshi_engine.core.types import Action, Side
from kalshi_engine.strategies.favorite_chase.state import FavoriteChaseState
from kalshi_engine.strategies.hourglass_ladder.ladder import LadderStrategy


@dataclass
class _Meta:
    ticker: str
    strike: float
    open_ms: int
    close_ms: int


def _make_log():
    log = MagicMock()
    log.writes = []
    def _write(p): log.writes.append(p)
    log.write = _write
    return log


def _make_book(ticker, recv_ms, yes_bid=200, yes_ask=220, no_bid=780, no_ask=800,
                yes_size=100, no_size=100):
    return BookEvent(
        ticker=ticker, ts_ms=recv_ms, recv_ms=recv_ms,
        yes_bid=yes_bid, yes_ask=yes_ask, no_bid=no_bid, no_ask=no_ask,
        yes_levels=((yes_bid, float(yes_size)),) if yes_size else (),
        no_levels=((no_bid, float(no_size)),) if no_size else (),
    )


def _spot_state(price=100_000.0, vol_target_bps=5.0, crypto="BTC"):
    """FavoriteChaseState seeded with deterministic walk for given vol_30m.

    Sawtooth: per-minute moves of ±(price * vol_target_bps / 1e4) so RMS
    matches the target. Final price equals starting price.
    """
    state = FavoriteChaseState(crypto)
    from kalshi_engine.core.events import SpotEvent
    from kalshi_engine.core.types import Crypto, Venue
    base = 1_779_800_000_000
    step = price * vol_target_bps / 1e4  # $ move per minute
    cryp = Crypto.BTC if crypto == "BTC" else (
        Crypto.ETH if crypto == "ETH" else Crypto.SOL)
    p = price
    for i in range(35):
        state.update_spot(SpotEvent(
            crypto=cryp, venue=Venue.BITSTAMP,
            ts_ms=base + i * 60_000, recv_ms=base + i * 60_000,
            price=p,
        ))
        p += step if (i % 2 == 0) else -step
    return state


def _wire(ladder, markets_map):
    """Connect a market_lookup callable to a ticker->Meta dict."""
    ladder._market_lookup = lambda t: markets_map.get(t)


# ---- Fire-at-T+30 happy paths -------------------------------------------

def test_ladder_fires_at_t30_with_3_qualifying_rungs():
    log = _make_log()
    state = _spot_state(price=100_000.0)
    ladder = LadderStrategy(
        log_writer=log, per_crypto_states={"BTC": state},
        enabled=True, max_rungs=3, d_norm_min=1.5, rung_size=3,
        min_bid_size=3, fav_min_dc=750.0, fav_max_dc=950.0,
    )
    open_ms = 1_779_800_000_000
    close_ms = open_ms + 60 * 60_000
    # Three far-OTM strikes: spot=100k, strikes at 99.5k, 99.4k, 99.3k
    # bps_margin: ~50, 60, 70 -> with vol~5, sqrt(tau)~5.4 at T+30,
    # d_norm = 50/27 ~ 1.85, 60/27 ~ 2.22, 70/27 ~ 2.59 (all > 1.5)
    markets = {
        "KXBTCD-T99500": _Meta("KXBTCD-T99500", 99500.0, open_ms, close_ms),
        "KXBTCD-T99400": _Meta("KXBTCD-T99400", 99400.0, open_ms, close_ms),
        "KXBTCD-T99300": _Meta("KXBTCD-T99300", 99300.0, open_ms, close_ms),
    }
    _wire(ladder, markets)

    # Prime the book cache with all 3 markets (at T+30 = open_ms + 30min)
    t30 = open_ms + 30 * 60_000
    # NO favorite (strike below spot): no_mid > yes_mid
    # yes_bid=50, yes_ask=70 -> yes_mid=60
    # no_bid=930, no_ask=950 -> no_mid=940
    for ticker in markets:
        ladder.on_event(_make_book(ticker, t30 - 1, yes_bid=50, yes_ask=70,
                                     no_bid=930, no_ask=950))

    # Fire the trigger book event for one of the tickers AT T+30
    decisions = ladder.on_event(_make_book("KXBTCD-T99500", t30,
                                            yes_bid=50, yes_ask=70,
                                            no_bid=930, no_ask=950))
    assert len(decisions) == 3
    # All ENTER, NO side, 3ct each
    assert all(d.action is Action.ENTER for d in decisions)
    assert all(d.side is Side.NO for d in decisions)
    assert all(d.size == 3 for d in decisions)
    # Sorted by d_norm desc: farthest strike (99300) first
    tickers_ordered = [d.ticker for d in decisions]
    assert tickers_ordered == ["KXBTCD-T99300", "KXBTCD-T99400", "KXBTCD-T99500"]
    # Logs include ladder_decision events
    enters = [e for e in log.writes
              if e.get("kind") == "ladder_decision" and e.get("action") == "enter"]
    assert len(enters) == 3


def test_ladder_takes_top_3_when_5_qualify():
    log = _make_log()
    state = _spot_state(price=100_000.0)
    ladder = LadderStrategy(
        log_writer=log, per_crypto_states={"BTC": state},
        enabled=True, max_rungs=3, d_norm_min=1.5, rung_size=3,
        min_bid_size=3, fav_min_dc=750.0, fav_max_dc=950.0,
    )
    open_ms = 1_779_800_000_000
    close_ms = open_ms + 60 * 60_000
    # 5 far-OTM strikes: increasingly far from spot
    strikes = [99500.0, 99400.0, 99300.0, 99200.0, 99100.0]
    markets = {
        f"KXBTCD-T{int(s)}": _Meta(f"KXBTCD-T{int(s)}", s, open_ms, close_ms)
        for s in strikes
    }
    _wire(ladder, markets)
    t30 = open_ms + 30 * 60_000
    for ticker in markets:
        ladder.on_event(_make_book(ticker, t30 - 1, yes_bid=50, yes_ask=70,
                                     no_bid=930, no_ask=950))
    decisions = ladder.on_event(_make_book("KXBTCD-T99500", t30,
                                            yes_bid=50, yes_ask=70,
                                            no_bid=930, no_ask=950))
    assert len(decisions) == 3
    # Top 3 farthest strikes: 99100 (most extreme), 99200, 99300
    tickers_ordered = [d.ticker for d in decisions]
    assert tickers_ordered == ["KXBTCD-T99100", "KXBTCD-T99200", "KXBTCD-T99300"]


def test_ladder_fires_with_only_2_qualifying():
    log = _make_log()
    state = _spot_state(price=100_000.0)
    ladder = LadderStrategy(
        log_writer=log, per_crypto_states={"BTC": state},
        enabled=True, max_rungs=3, d_norm_min=1.5, rung_size=3,
        min_bid_size=3, fav_min_dc=750.0, fav_max_dc=950.0,
    )
    open_ms = 1_779_800_000_000
    close_ms = open_ms + 60 * 60_000
    # 2 qualifying far-OTM + 1 too close (d_norm < 1.5)
    markets = {
        "KXBTCD-T99500": _Meta("KXBTCD-T99500", 99500.0, open_ms, close_ms),
        "KXBTCD-T99400": _Meta("KXBTCD-T99400", 99400.0, open_ms, close_ms),
        "KXBTCD-T99950": _Meta("KXBTCD-T99950", 99950.0, open_ms, close_ms),  # too close (d_norm~0.18)
    }
    _wire(ladder, markets)
    t30 = open_ms + 30 * 60_000
    for ticker in markets:
        ladder.on_event(_make_book(ticker, t30 - 1, yes_bid=50, yes_ask=70,
                                     no_bid=930, no_ask=950))
    decisions = ladder.on_event(_make_book("KXBTCD-T99500", t30,
                                            yes_bid=50, yes_ask=70,
                                            no_bid=930, no_ask=950))
    assert len(decisions) == 2
    tickers = {d.ticker for d in decisions}
    assert tickers == {"KXBTCD-T99500", "KXBTCD-T99400"}


# ---- Filter tests --------------------------------------------------------

def test_ladder_skips_rungs_with_thin_depth():
    log = _make_log()
    state = _spot_state(price=100_000.0)
    ladder = LadderStrategy(
        log_writer=log, per_crypto_states={"BTC": state},
        enabled=True, max_rungs=3, d_norm_min=1.5, rung_size=3,
        min_bid_size=5, fav_min_dc=750.0, fav_max_dc=950.0,
    )
    open_ms = 1_779_800_000_000
    close_ms = open_ms + 60 * 60_000
    markets = {
        "KXBTCD-T99500": _Meta("KXBTCD-T99500", 99500.0, open_ms, close_ms),
        "KXBTCD-T99400": _Meta("KXBTCD-T99400", 99400.0, open_ms, close_ms),
    }
    _wire(ladder, markets)
    t30 = open_ms + 30 * 60_000
    # 99500 has thin depth (no_size=2 < min_bid_size=5)
    ladder.on_event(_make_book("KXBTCD-T99500", t30 - 1, yes_bid=50, yes_ask=70,
                                 no_bid=930, no_ask=950, no_size=2))
    # 99400 has fat depth
    ladder.on_event(_make_book("KXBTCD-T99400", t30 - 1, yes_bid=50, yes_ask=70,
                                 no_bid=930, no_ask=950, no_size=50))
    decisions = ladder.on_event(_make_book("KXBTCD-T99500", t30,
                                            yes_bid=50, yes_ask=70,
                                            no_bid=930, no_ask=950, no_size=2))
    # Only the fat-depth rung survives
    assert len(decisions) == 1
    assert decisions[0].ticker == "KXBTCD-T99400"


def test_ladder_skips_rungs_outside_fav_price_range():
    log = _make_log()
    state = _spot_state(price=100_000.0)
    ladder = LadderStrategy(
        log_writer=log, per_crypto_states={"BTC": state},
        enabled=True, max_rungs=3, d_norm_min=1.5, rung_size=3,
        min_bid_size=3, fav_min_dc=750.0, fav_max_dc=950.0,
    )
    open_ms = 1_779_800_000_000
    close_ms = open_ms + 60 * 60_000
    markets = {
        "KXBTCD-T99500": _Meta("KXBTCD-T99500", 99500.0, open_ms, close_ms),
        "KXBTCD-T99400": _Meta("KXBTCD-T99400", 99400.0, open_ms, close_ms),
    }
    _wire(ladder, markets)
    t30 = open_ms + 30 * 60_000
    # 99500: NO favorite at $0.95 (mid=950) - at cap, accepted
    ladder.on_event(_make_book("KXBTCD-T99500", t30 - 1, yes_bid=50, yes_ask=70,
                                 no_bid=930, no_ask=950))
    # 99400: NO favorite at $0.99 - above cap of 950
    ladder.on_event(_make_book("KXBTCD-T99400", t30 - 1, yes_bid=5, yes_ask=15,
                                 no_bid=985, no_ask=995))
    decisions = ladder.on_event(_make_book("KXBTCD-T99500", t30,
                                            yes_bid=50, yes_ask=70,
                                            no_bid=930, no_ask=950))
    assert len(decisions) == 1
    assert decisions[0].ticker == "KXBTCD-T99500"


# ---- Per-cycle dedup ----------------------------------------------------

def test_ladder_does_not_refire_at_t40_after_t30():
    log = _make_log()
    state = _spot_state(price=100_000.0)
    ladder = LadderStrategy(
        log_writer=log, per_crypto_states={"BTC": state},
        enabled=True, max_rungs=3, d_norm_min=1.5, rung_size=3,
        min_bid_size=3, fav_min_dc=750.0, fav_max_dc=950.0,
        trigger_minute=30,
    )
    open_ms = 1_779_800_000_000
    close_ms = open_ms + 60 * 60_000
    markets = {
        "KXBTCD-T99500": _Meta("KXBTCD-T99500", 99500.0, open_ms, close_ms),
        "KXBTCD-T99400": _Meta("KXBTCD-T99400", 99400.0, open_ms, close_ms),
    }
    _wire(ladder, markets)
    t30 = open_ms + 30 * 60_000
    for tk in markets:
        ladder.on_event(_make_book(tk, t30 - 1, yes_bid=50, yes_ask=70,
                                     no_bid=930, no_ask=950))
    decisions_t30 = ladder.on_event(_make_book("KXBTCD-T99500", t30,
                                                 yes_bid=50, yes_ask=70,
                                                 no_bid=930, no_ask=950))
    assert len(decisions_t30) == 2
    # Now simulate T+40 book event same cycle — must NOT fire
    t40 = open_ms + 40 * 60_000
    decisions_t40 = ladder.on_event(_make_book("KXBTCD-T99500", t40,
                                                 yes_bid=50, yes_ask=70,
                                                 no_bid=930, no_ask=950))
    # trigger_minute=30 means T+40 isn't in window anyway, but verify
    # that even another T+30 book event (e.g., a different ticker arriving
    # at T+30:25) does not re-fire.
    assert decisions_t40 == []
    t30_2 = open_ms + 30 * 60_000 + 25_000
    decisions_t30_2 = ladder.on_event(_make_book("KXBTCD-T99400", t30_2,
                                                   yes_bid=50, yes_ask=70,
                                                   no_bid=930, no_ask=950))
    assert decisions_t30_2 == []


# ---- Crypto allowlist ---------------------------------------------------

def test_ladder_does_not_fire_for_eth_when_btc_only():
    log = _make_log()
    state = _spot_state(price=2000.0, crypto="ETH")
    ladder = LadderStrategy(
        log_writer=log, per_crypto_states={"ETH": state, "BTC": _spot_state()},
        enabled=True, max_rungs=3, d_norm_min=1.5, rung_size=3,
        min_bid_size=3, fav_min_dc=750.0, fav_max_dc=950.0,
        crypto_allowlist=("BTC",),
    )
    open_ms = 1_779_800_000_000
    close_ms = open_ms + 60 * 60_000
    markets = {
        "KXETHD-T1990": _Meta("KXETHD-T1990", 1990.0, open_ms, close_ms),
    }
    _wire(ladder, markets)
    t30 = open_ms + 30 * 60_000
    ladder.on_event(_make_book("KXETHD-T1990", t30 - 1, yes_bid=50, yes_ask=70,
                                 no_bid=930, no_ask=950))
    decisions = ladder.on_event(_make_book("KXETHD-T1990", t30,
                                             yes_bid=50, yes_ask=70,
                                             no_bid=930, no_ask=950))
    assert decisions == []


# ---- Daily cap ----------------------------------------------------------

def test_ladder_skips_when_daily_cap_bound():
    log = _make_log()
    state = _spot_state(price=100_000.0)
    ladder = LadderStrategy(
        log_writer=log, per_crypto_states={"BTC": state},
        enabled=True, max_rungs=3, d_norm_min=1.5, rung_size=3,
        min_bid_size=3, fav_min_dc=750.0, fav_max_dc=950.0,
        daily_cap_cents=500,
    )
    # Manually bind the cap. _daily_utc_date must match the date of the
    # incoming book events (t30 below), not today's wall-clock date.
    from datetime import datetime, timezone
    open_ms = 1_779_800_000_000
    close_ms = open_ms + 60 * 60_000
    t30 = open_ms + 30 * 60_000
    ladder._daily_utc_date = datetime.fromtimestamp(
        t30 / 1000, tz=timezone.utc).date().isoformat()
    ladder._daily_realized_cents = -600  # past $5 cap
    markets = {
        "KXBTCD-T99500": _Meta("KXBTCD-T99500", 99500.0, open_ms, close_ms),
        "KXBTCD-T99400": _Meta("KXBTCD-T99400", 99400.0, open_ms, close_ms),
    }
    _wire(ladder, markets)
    for tk in markets:
        ladder.on_event(_make_book(tk, t30 - 1, yes_bid=50, yes_ask=70,
                                     no_bid=930, no_ask=950))
    decisions = ladder.on_event(_make_book("KXBTCD-T99500", t30,
                                             yes_bid=50, yes_ask=70,
                                             no_bid=930, no_ask=950))
    assert decisions == []
    skip_logs = [e for e in log.writes
                 if e.get("kind") == "ladder_decision"
                 and e.get("action") == "skip_cap_bound"]
    assert len(skip_logs) == 1


def test_ladder_settlement_updates_daily_realized():
    log = _make_log()
    state = _spot_state(price=100_000.0)
    ladder = LadderStrategy(
        log_writer=log, per_crypto_states={"BTC": state},
        enabled=True, max_rungs=3, d_norm_min=1.5, rung_size=3,
        min_bid_size=3, fav_min_dc=750.0, fav_max_dc=950.0,
        daily_cap_cents=500,
    )
    open_ms = 1_779_800_000_000
    close_ms = open_ms + 60 * 60_000
    markets = {
        "KXBTCD-T99500": _Meta("KXBTCD-T99500", 99500.0, open_ms, close_ms),
    }
    _wire(ladder, markets)
    t30 = open_ms + 30 * 60_000
    ladder.on_event(_make_book("KXBTCD-T99500", t30 - 1, yes_bid=50, yes_ask=70,
                                 no_bid=930, no_ask=950))
    decisions = ladder.on_event(_make_book("KXBTCD-T99500", t30,
                                             yes_bid=50, yes_ask=70,
                                             no_bid=930, no_ask=950))
    assert len(decisions) == 1
    assert decisions[0].ticker == "KXBTCD-T99500"
    # Now a losing settlement (settled YES while we bought NO at 940dc)
    sett = SettlementEvent(
        ticker="KXBTCD-T99500", ts_ms=close_ms, recv_ms=close_ms,
        result=Side.YES, settle_value=99600.0, determined_ms=close_ms,
    )
    out = ladder.on_event(sett)
    assert out == []
    # Loss per ct: payout 0 - entry 94c - fee ceil(7*0.94*0.06)=1c = -95c
    # Total loss = -95c * 3ct = -285c
    assert ladder._daily_realized_cents == -285
    sett_logs = [e for e in log.writes if e.get("kind") == "ladder_settlement"]
    assert len(sett_logs) == 1
    assert sett_logs[0]["win"] is False
    assert sett_logs[0]["realized_cents"] == -285


# ---- Disabled -----------------------------------------------------------

def test_ladder_disabled_produces_no_decisions():
    log = _make_log()
    state = _spot_state(price=100_000.0)
    ladder = LadderStrategy(
        log_writer=log, per_crypto_states={"BTC": state},
        enabled=False,  # OFF
        max_rungs=3, d_norm_min=1.5, rung_size=3,
        min_bid_size=3, fav_min_dc=750.0, fav_max_dc=950.0,
    )
    open_ms = 1_779_800_000_000
    close_ms = open_ms + 60 * 60_000
    markets = {
        "KXBTCD-T99500": _Meta("KXBTCD-T99500", 99500.0, open_ms, close_ms),
    }
    _wire(ladder, markets)
    t30 = open_ms + 30 * 60_000
    ladder.on_event(_make_book("KXBTCD-T99500", t30 - 1, yes_bid=50, yes_ask=70,
                                 no_bid=930, no_ask=950))
    decisions = ladder.on_event(_make_book("KXBTCD-T99500", t30,
                                             yes_bid=50, yes_ask=70,
                                             no_bid=930, no_ask=950))
    assert decisions == []
    # No log writes at all (the disabled branch returns before any work)
    assert log.writes == []


# ---- Constructor validation --------------------------------------------

def test_ladder_requires_log_writer():
    with pytest.raises(ValueError, match="log_writer"):
        LadderStrategy(log_writer=None, per_crypto_states={})


def test_ladder_validates_max_rungs():
    log = _make_log()
    with pytest.raises(ValueError, match="max_rungs"):
        LadderStrategy(log_writer=log, max_rungs=0)


def test_ladder_validates_rung_size():
    log = _make_log()
    with pytest.raises(ValueError, match="rung_size"):
        LadderStrategy(log_writer=log, rung_size=0)


# ---- Fee math sanity ----------------------------------------------------

def test_ladder_fee_cents_formula():
    # ceil(7 * c * (1-c))
    assert LadderStrategy._fee_cents(0.0) == 0
    assert LadderStrategy._fee_cents(1.0) == 0
    assert LadderStrategy._fee_cents(0.5) == 2  # 7 * 0.5 * 0.5 = 1.75 -> 2
    assert LadderStrategy._fee_cents(0.94) == 1  # 7*0.94*0.06 = 0.395 -> 1
    assert LadderStrategy._fee_cents(0.99) == 1  # 7*0.99*0.01 = 0.069 -> 1
    assert LadderStrategy._fee_cents(0.80) == 2  # 7*0.8*0.2 = 1.12 -> 2
