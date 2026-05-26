"""Phase 13.3: 1hr observer captures book-depth fields in envelopes.

The `book_at_1hr_pretrigger` envelope now includes `yes_bid_size_fp`,
`yes_ask_size_fp`, `no_bid_size_fp`, `no_ask_size_fp` so backtests can
distinguish paper-fill assumptions from depth-real fills.
"""
from __future__ import annotations
from unittest.mock import MagicMock
from kalshi_engine.core.events import BookEvent
from kalshi_engine.strategies.hourglass_observer.observer import (
    HourglassObserverStrategy,
)


def _log():
    L = MagicMock(); L.writes = []
    def _w(p): L.writes.append(p)
    L.write = _w
    return L


def _book(yes_levels=((200, 13.0),), no_levels=((795, 27.0),),
          yes_bid=200, yes_ask=210, no_bid=790, no_ask=795,
          elapsed_min=30.0, open_ms=1_700_000_000_000):
    recv = int(open_ms + elapsed_min * 60_000)
    return BookEvent(
        ticker="KXBTCD-T", ts_ms=recv, recv_ms=recv,
        yes_bid=yes_bid, yes_ask=yes_ask,
        no_bid=no_bid, no_ask=no_ask,
        yes_levels=yes_levels, no_levels=no_levels,
    )


def _strategy():
    s = HourglassObserverStrategy(log_writer=_log())
    s.register_market("KXBTCD-T", strike=75_000.0,
                       open_ms=1_700_000_000_000, close_ms=1_700_003_600_000)
    return s


def test_envelope_includes_depth_fields():
    s = _strategy()
    s.on_event(_book(
        yes_bid=200, yes_ask=210, no_bid=790, no_ask=795,
        yes_levels=((200, 13.0), (210, 4.0)),
        no_levels=((790, 21.0), (795, 27.0)),
    ))
    envs = [w for w in s._log.writes if w.get("kind") == "book_at_1hr_pretrigger"]
    assert len(envs) == 1
    e = envs[0]
    for k in ("yes_bid_size_fp", "yes_ask_size_fp",
              "no_bid_size_fp", "no_ask_size_fp"):
        assert k in e, f"missing {k}"


def test_depth_sizes_match_top_of_book():
    s = _strategy()
    s.on_event(_book(
        yes_bid=200, yes_ask=210, no_bid=790, no_ask=795,
        yes_levels=((200, 13.0), (210, 4.0)),
        no_levels=((790, 21.0), (795, 27.0)),
    ))
    envs = [w for w in s._log.writes if w.get("kind") == "book_at_1hr_pretrigger"]
    e = envs[0]
    assert e["yes_bid_size_fp"] == 13.0   # size at the bid (200)
    assert e["yes_ask_size_fp"] == 4.0    # size at the ask (210)
    assert e["no_bid_size_fp"] == 21.0    # size at the no_bid (790)
    assert e["no_ask_size_fp"] == 27.0    # size at the no_ask (795)


def test_missing_depth_returns_none_not_crash():
    """If a level's price isn't in the ladder, depth lookup returns None."""
    s = _strategy()
    s.on_event(_book(
        yes_bid=200, yes_ask=210, no_bid=790, no_ask=795,
        # Ladder doesn't contain the ask price
        yes_levels=((200, 13.0),),  # no 210 entry
        no_levels=((790, 21.0),),    # no 795 entry
    ))
    envs = [w for w in s._log.writes if w.get("kind") == "book_at_1hr_pretrigger"]
    e = envs[0]
    assert e["yes_bid_size_fp"] == 13.0
    assert e["yes_ask_size_fp"] is None
    assert e["no_bid_size_fp"] == 21.0
    assert e["no_ask_size_fp"] is None
