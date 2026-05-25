"""FillSimulator: book-state, fill at top-of-book, settlement PnL."""

from __future__ import annotations

import pytest

from kalshi_engine.backtest.fill_simulator import FillSimulator, SlippageModel
from kalshi_engine.core.events import BookEvent, SettlementEvent
from kalshi_engine.core.interfaces import Decision
from kalshi_engine.core.types import Action, Side
from kalshi_engine.risk.fees import taker_fee
from kalshi_engine.warehouse.adapters import LiveLogReader, LiveLogWriter


def _book(yes_ask: int, no_ask: int, ticker: str = "KXBTC15M-T") -> BookEvent:
    """Synthetic book with explicit asks; bids derived via the 1000-complement."""
    yes_bid = 1000 - no_ask
    no_bid = 1000 - yes_ask
    return BookEvent(
        ticker=ticker, ts_ms=1000, recv_ms=1000,
        yes_bid=yes_bid, yes_ask=yes_ask,
        no_bid=no_bid, no_ask=no_ask,
        yes_levels=(), no_levels=(),
    )


def _enter(side: Side, ticker: str = "KXBTC15M-T") -> Decision:
    return Decision(
        ticker=ticker, action=Action.ENTER, side=side,
        size=1, confidence=0.6, reason="signal",
        diagnostics={"vol_30m": 5.0},
    )


def test_buy_yes_fills_at_top_of_book(tmp_path):
    sim = FillSimulator(LiveLogWriter(str(tmp_path / "log.jsonl")))
    sim.on_book(_book(yes_ask=830, no_ask=180))
    sim.submit(_enter(Side.YES))
    assert sim.n_fills == 1
    pos = sim.open_positions["KXBTC15M-T"]
    assert pos.side == "yes"
    assert pos.count == 1
    assert pos.fill_price_dc == 830  # top-of-book yes_ask, no markdown
    assert pos.entry_fee_cents == taker_fee(830, 1)


def test_buy_no_fills_at_no_ask(tmp_path):
    sim = FillSimulator(LiveLogWriter(str(tmp_path / "log.jsonl")))
    sim.on_book(_book(yes_ask=200, no_ask=820))
    sim.submit(_enter(Side.NO))
    pos = sim.open_positions["KXBTC15M-T"]
    assert pos.side == "no"
    assert pos.fill_price_dc == 820


def test_slippage_markdown_increases_fill_price(tmp_path):
    sim = FillSimulator(
        LiveLogWriter(str(tmp_path / "log.jsonl")),
        slippage=SlippageModel(markdown_dc=50),
    )
    sim.on_book(_book(yes_ask=800, no_ask=210))
    sim.submit(_enter(Side.YES))
    pos = sim.open_positions["KXBTC15M-T"]
    assert pos.fill_price_dc == 850  # 800 + 50 markdown


def test_slippage_markdown_clamped_at_buy_cap(tmp_path):
    sim = FillSimulator(
        LiveLogWriter(str(tmp_path / "log.jsonl")),
        slippage=SlippageModel(markdown_dc=500),  # would push past 990 cap
    )
    sim.on_book(_book(yes_ask=950, no_ask=60))
    sim.submit(_enter(Side.YES))
    pos = sim.open_positions["KXBTC15M-T"]
    assert pos.fill_price_dc == 990  # capped


def test_submit_skips_with_no_book(tmp_path):
    log_path = str(tmp_path / "log.jsonl")
    sim = FillSimulator(LiveLogWriter(log_path))
    sim.submit(_enter(Side.YES))  # no on_book called
    assert sim.n_fills == 0
    events = list(LiveLogReader(log_path).iter())
    assert any(e["kind"] == "fill_skip" for e in events)


def test_skip_decision_is_a_noop(tmp_path):
    sim = FillSimulator(LiveLogWriter(str(tmp_path / "log.jsonl")))
    sim.on_book(_book(yes_ask=830, no_ask=180))
    skip = Decision(
        ticker="KXBTC15M-T", action=Action.SKIP, side=Side.YES,
        size=0, reason="RISK-SKIP", diagnostics={},
    )
    sim.submit(skip)
    assert sim.n_fills == 0
    assert sim.open_positions == {}


def test_settlement_win_realises_positive_pnl(tmp_path):
    sim = FillSimulator(LiveLogWriter(str(tmp_path / "log.jsonl")))
    sim.on_book(_book(yes_ask=800, no_ask=210))
    sim.submit(_enter(Side.YES))
    sim.on_settlement(SettlementEvent(
        ticker="KXBTC15M-T", ts_ms=2000, recv_ms=2000,
        result=Side.YES, settle_value=1.0, determined_ms=2000,
    ))
    assert sim.n_settled == 1
    assert sim.n_wins == 1
    # gross = (1000 - 800)/10 = 20.0c per contract; fee = taker_fee(800,1) = 2c
    expected_net = 20.0 - taker_fee(800, 1)
    assert sim.realized_pnl_cents == pytest.approx(expected_net)


def test_settlement_loss_realises_negative_pnl(tmp_path):
    sim = FillSimulator(LiveLogWriter(str(tmp_path / "log.jsonl")))
    sim.on_book(_book(yes_ask=800, no_ask=210))
    sim.submit(_enter(Side.YES))  # bet YES
    sim.on_settlement(SettlementEvent(
        ticker="KXBTC15M-T", ts_ms=2000, recv_ms=2000,
        result=Side.NO, settle_value=0.0, determined_ms=2000,
    ))
    assert sim.n_settled == 1
    assert sim.n_wins == 0
    # gross = -800/10 = -80.0c, minus fee
    expected_net = -80.0 - taker_fee(800, 1)
    assert sim.realized_pnl_cents == pytest.approx(expected_net)


def test_summary_after_full_cycle(tmp_path):
    sim = FillSimulator(LiveLogWriter(str(tmp_path / "log.jsonl")))
    sim.on_book(_book(yes_ask=800, no_ask=210, ticker="KXBTC15M-A"))
    sim.submit(_enter(Side.YES, ticker="KXBTC15M-A"))
    sim.on_settlement(SettlementEvent(
        ticker="KXBTC15M-A", ts_ms=2000, recv_ms=2000,
        result=Side.YES, settle_value=1.0, determined_ms=2000,
    ))
    s = sim.summary()
    assert s["n_fills"] == 1
    assert s["n_settled"] == 1
    assert s["n_wins"] == 1
    assert s["win_rate"] == 1.0
    assert s["open_positions"] == 0
    assert s["realized_pnl_cents"] > 0
