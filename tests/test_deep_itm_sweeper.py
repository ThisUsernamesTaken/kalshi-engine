from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import MagicMock

from kalshi_engine.core.events import BookEvent
from kalshi_engine.core.types import Action, Side
from kalshi_engine.strategies.favorite_chase.state import FavoriteChaseState
from kalshi_engine.strategies.hourglass_ladder.ladder import DeepItmSweeperStrategy


@dataclass
class _Meta:
    ticker: str
    strike: float
    open_ms: int
    close_ms: int


def _make_log():
    log = MagicMock()
    log.writes = []
    log.write = lambda payload: log.writes.append(payload)
    return log


def _book(
    ticker: str,
    recv_ms: int,
    *,
    yes_bid: int,
    yes_ask: int,
    no_bid: int,
    no_ask: int,
    yes_size: int = 100,
    no_size: int = 100,
):
    return BookEvent(
        ticker=ticker,
        ts_ms=recv_ms,
        recv_ms=recv_ms,
        yes_bid=yes_bid,
        yes_ask=yes_ask,
        no_bid=no_bid,
        no_ask=no_ask,
        yes_levels=((yes_bid, float(yes_size)),),
        no_levels=((no_bid, float(no_size)),),
    )


def _spot_state(price=100_000.0, vol_target_bps=5.0):
    from kalshi_engine.core.events import SpotEvent
    from kalshi_engine.core.types import Crypto, Venue

    state = FavoriteChaseState("BTC")
    base = 1_779_800_000_000
    step = price * vol_target_bps / 1e4
    p = price
    for i in range(35):
        state.update_spot(SpotEvent(
            crypto=Crypto.BTC,
            venue=Venue.BITSTAMP,
            ts_ms=base + i * 60_000,
            recv_ms=base + i * 60_000,
            price=p,
        ))
        p += step if i % 2 == 0 else -step
    return state


def test_deep_itm_sweeper_fires_early_with_ask_limit():
    log = _make_log()
    open_ms = 1_779_800_000_000
    close_ms = open_ms + 60 * 60_000
    t5 = open_ms + 5 * 60_000
    markets = {
        "KXBTCD-T98500": _Meta("KXBTCD-T98500", 98_500.0, open_ms, close_ms),
        "KXBTCD-T98400": _Meta("KXBTCD-T98400", 98_400.0, open_ms, close_ms),
        "KXBTCD-T97000": _Meta("KXBTCD-T97000", 97_000.0, open_ms, close_ms),
    }
    sweeper = DeepItmSweeperStrategy(
        log_writer=log,
        per_crypto_states={"BTC": _spot_state()},
        market_lookup=lambda ticker: markets.get(ticker),
        enabled=True,
        max_rungs=2,
        rung_size=1,
        min_d_norm=3.0,
        min_fav_ask_dc=900,
        max_fav_ask_dc=970,
        trigger_minutes=(5, 10),
        crypto_allowlist=("BTC",),
    )

    sweeper.on_event(_book(
        "KXBTCD-T98500", t5 - 1,
        yes_bid=940, yes_ask=960, no_bid=40, no_ask=60,
    ))
    sweeper.on_event(_book(
        "KXBTCD-T98400", t5 - 1,
        yes_bid=930, yes_ask=950, no_bid=50, no_ask=70,
    ))
    sweeper.on_event(_book(
        "KXBTCD-T97000", t5 - 1,
        yes_bid=990, yes_ask=1000, no_bid=0, no_ask=10,
    ))

    decisions = sweeper.on_event(_book(
        "KXBTCD-T98500", t5,
        yes_bid=940, yes_ask=960, no_bid=40, no_ask=60,
    ))

    assert len(decisions) == 2
    assert all(d.action is Action.ENTER for d in decisions)
    assert all(d.side is Side.YES for d in decisions)
    assert {d.diagnostics["limit_price_decicents"] for d in decisions} == {950, 960}
    assert all(d.size == 1 for d in decisions)
    assert all("DEEP_ITM_SWEEP" in d.reason for d in decisions)


def test_deep_itm_sweeper_dedups_cycle_after_first_trigger():
    log = _make_log()
    open_ms = 1_779_800_000_000
    close_ms = open_ms + 60 * 60_000
    t5 = open_ms + 5 * 60_000
    markets = {
        "KXBTCD-T98500": _Meta("KXBTCD-T98500", 98_500.0, open_ms, close_ms),
    }
    sweeper = DeepItmSweeperStrategy(
        log_writer=log,
        per_crypto_states={"BTC": _spot_state()},
        market_lookup=lambda ticker: markets.get(ticker),
        enabled=True,
        max_rungs=1,
        min_d_norm=3.0,
        trigger_minutes=(5, 10),
        crypto_allowlist=("BTC",),
    )
    book = _book(
        "KXBTCD-T98500", t5,
        yes_bid=940, yes_ask=960, no_bid=40, no_ask=60,
    )
    assert len(sweeper.on_event(book)) == 1
    assert sweeper.on_event(book) == []


def test_deep_itm_sweeper_rejects_99c_fee_trap():
    log = _make_log()
    open_ms = 1_779_800_000_000
    close_ms = open_ms + 60 * 60_000
    t5 = open_ms + 5 * 60_000
    markets = {
        "KXBTCD-T97000": _Meta("KXBTCD-T97000", 97_000.0, open_ms, close_ms),
    }
    sweeper = DeepItmSweeperStrategy(
        log_writer=log,
        per_crypto_states={"BTC": _spot_state()},
        market_lookup=lambda ticker: markets.get(ticker),
        enabled=True,
        max_rungs=1,
        max_fav_ask_dc=970,
        trigger_minutes=(5,),
        crypto_allowlist=("BTC",),
    )

    decisions = sweeper.on_event(_book(
        "KXBTCD-T97000", t5,
        yes_bid=990, yes_ask=1000, no_bid=0, no_ask=10,
    ))

    assert decisions == []
    assert log.writes[-1]["action"] == "no_rungs"
