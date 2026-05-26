"""Phase 13.3: post-trigger book observation tests.

The FavoriteChaseStrategy emits `book_at_post_trigger` envelopes during
T+8 to T+14.5 of each cycle, mirroring `book_at_pre_trigger` but with:
- Different window
- Does NOT skip when ticker is already in self.entered (we want post-T+8
  trajectory for entered cohorts too)
- Carries is_entered flag in payload
"""
from __future__ import annotations
from unittest.mock import MagicMock
import pytest
from kalshi_engine.core.events import BookEvent
from kalshi_engine.strategies.favorite_chase.strategy import FavoriteChaseStrategy


def _make_log():
    log = MagicMock()
    log.writes = []
    def _write(p): log.writes.append(p)
    log.write = _write
    return log


def _make_book(ticker, elapsed_min, open_ms=1_700_000_000_000):
    recv = int(open_ms + elapsed_min * 60_000)
    return BookEvent(
        ticker=ticker, ts_ms=recv, recv_ms=recv,
        yes_bid=200, yes_ask=210, no_bid=790, no_ask=800,
        yes_levels=((200, 5.0),), no_levels=((790, 5.0),),
    )


def _make_strategy(pre_trigger=True):
    log = _make_log()
    s = FavoriteChaseStrategy(
        log_writer=log,
        pre_trigger_observation=pre_trigger,
        pre_trigger_throttle_ms=100,  # low throttle so tests can fire fast
    )
    open_ms = 1_700_000_000_000
    s.register_market("KXBTC15M-T", strike=75_000.0,
                       open_ms=open_ms, close_ms=open_ms + 15 * 60_000)
    return s, log


# ---- window gating ------------------------------------------------------

def test_post_trigger_fires_at_t10():
    """Book at T+10 -> post-trigger envelope emitted."""
    s, log = _make_strategy()
    s.on_event(_make_book("KXBTC15M-T", elapsed_min=10.0))
    post = [w for w in log.writes if w.get("kind") == "book_at_post_trigger"]
    assert len(post) == 1


def test_post_trigger_does_not_fire_at_t7():
    """T+7 is in the PRE-trigger window, not post."""
    s, log = _make_strategy()
    s.on_event(_make_book("KXBTC15M-T", elapsed_min=7.0))
    post = [w for w in log.writes if w.get("kind") == "book_at_post_trigger"]
    assert len(post) == 0


def test_post_trigger_does_not_fire_at_t15():
    """T+15 is past the T+14.5 close — no envelope."""
    s, log = _make_strategy()
    s.on_event(_make_book("KXBTC15M-T", elapsed_min=15.0))
    post = [w for w in log.writes if w.get("kind") == "book_at_post_trigger"]
    assert len(post) == 0


def test_post_trigger_fires_at_t14p4():
    """T+14.4 is still inside the window."""
    s, log = _make_strategy()
    s.on_event(_make_book("KXBTC15M-T", elapsed_min=14.4))
    post = [w for w in log.writes if w.get("kind") == "book_at_post_trigger"]
    assert len(post) == 1


# ---- entered-ticker behavior (key diff vs pre-trigger) -----------------

def test_post_trigger_fires_when_ticker_already_entered():
    """The post-trigger window MUST capture envelopes for entered tickers
    too (the differentiator vs pre-trigger which skips entered)."""
    s, log = _make_strategy()
    s.entered.add("KXBTC15M-T")  # simulate prior entry
    s.on_event(_make_book("KXBTC15M-T", elapsed_min=10.0))
    post = [w for w in log.writes if w.get("kind") == "book_at_post_trigger"]
    assert len(post) == 1
    assert post[0]["is_entered"] is True


def test_post_trigger_is_entered_false_when_not_entered():
    s, log = _make_strategy()
    s.on_event(_make_book("KXBTC15M-T", elapsed_min=10.0))
    post = [w for w in log.writes if w.get("kind") == "book_at_post_trigger"]
    assert len(post) == 1
    assert post[0]["is_entered"] is False


# ---- throttle ----------------------------------------------------------

def test_post_trigger_throttle_blocks_rapid_repeat():
    """Two books within throttle window -> only one envelope."""
    s, log = _make_strategy()
    s.on_event(_make_book("KXBTC15M-T", elapsed_min=10.0))
    # 50 ms later - still inside the 100 ms throttle
    s.on_event(_make_book("KXBTC15M-T", elapsed_min=10.00084))  # +50 ms
    post = [w for w in log.writes if w.get("kind") == "book_at_post_trigger"]
    assert len(post) == 1


# ---- flag-disabled -----------------------------------------------------

def test_post_trigger_disabled_when_observation_off():
    s, log = _make_strategy(pre_trigger=False)
    s.on_event(_make_book("KXBTC15M-T", elapsed_min=10.0))
    post = [w for w in log.writes if w.get("kind") == "book_at_post_trigger"]
    assert len(post) == 0


# ---- envelope schema ---------------------------------------------------

def test_post_trigger_envelope_fields():
    s, log = _make_strategy()
    s.on_event(_make_book("KXBTC15M-T", elapsed_min=10.0))
    post = [w for w in log.writes if w.get("kind") == "book_at_post_trigger"]
    assert len(post) == 1
    e = post[0]
    for k in ("kind", "ticker", "ts_ms", "elapsed_min", "tau_min",
              "yes_bid", "yes_ask", "no_bid", "no_ask",
              "favorite_side", "favorite_mid_decicents", "is_entered"):
        assert k in e, f"missing field {k}"
    assert e["kind"] == "book_at_post_trigger"
    # elapsed_min ~ 10 (allow some float slop)
    assert 9.99 < e["elapsed_min"] < 10.01
