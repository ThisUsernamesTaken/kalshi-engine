"""RiskEnvelope: loss cap, concurrency, fail-closed, downsize, exit pass-through."""

from __future__ import annotations

from kalshi_engine.core.interfaces import Decision
from kalshi_engine.core.types import Action, Side
from kalshi_engine.risk.envelope import RiskEnvelope, RiskState


def _enter(ticker: str = "KXBTC15M-X", size: int = 1) -> Decision:
    return Decision(
        ticker=ticker, action=Action.ENTER, side=Side.NO,
        limit_cents=80, size=size, reason="signal",
    )


def test_clean_enter_passes():
    env = RiskEnvelope()
    st = RiskState(now_ms=1_000_000, last_spot_ms={"BTC": 1_000_000})
    out = env.check(_enter(), st)
    assert out.action is Action.ENTER
    assert out.size == 1


def test_oversized_enter_is_downsized():
    """Default max_contracts_per_trade is 10 (Phase-12.7 lift)."""
    env = RiskEnvelope()
    st = RiskState(now_ms=1_000_000, last_spot_ms={"BTC": 1_000_000})
    out = env.check(_enter(size=15), st)
    assert out.action is Action.ENTER
    assert out.size == 10
    assert "downsized" in out.reason


def test_oversized_enter_downsized_to_custom_cap():
    """Custom max_contracts_per_trade still works."""
    env = RiskEnvelope(max_contracts_per_trade=1)
    st = RiskState(now_ms=1_000_000, last_spot_ms={"BTC": 1_000_000})
    out = env.check(_enter(size=9), st)
    assert out.action is Action.ENTER
    assert out.size == 1
    assert "downsized 9->1" in out.reason


def test_daily_loss_cap_skips():
    env = RiskEnvelope()
    st = RiskState(now_ms=1_000_000, last_spot_ms={"BTC": 1_000_000},
                   daily_realized_cents=-1000)
    out = env.check(_enter(), st)
    assert out.action is Action.SKIP
    assert "loss cap" in out.reason


def test_concurrency_cap_skips_new_position():
    env = RiskEnvelope()
    st = RiskState(now_ms=1_000_000, last_spot_ms={"BTC": 1_000_000},
                   open_positions={"A", "B", "C", "D", "E"})
    out = env.check(_enter(ticker="KXBTC15M-NEW"), st)
    assert out.action is Action.SKIP
    assert "concurrent" in out.reason


def test_existing_position_not_concurrency_capped():
    env = RiskEnvelope()
    st = RiskState(now_ms=1_000_000, last_spot_ms={"BTC": 1_000_000},
                   open_positions={"KXBTC15M-X", "B", "C", "D", "E"})
    out = env.check(_enter(ticker="KXBTC15M-X"), st)
    assert out.action is Action.ENTER  # scaling an already-open position is allowed


def test_fail_closed_on_missing_spot():
    env = RiskEnvelope()
    st = RiskState(now_ms=1_000_000)  # no spot tick recorded for BTC
    out = env.check(_enter(), st)
    assert out.action is Action.SKIP
    assert "no spot" in out.reason


def test_fail_closed_on_stale_spot():
    env = RiskEnvelope()
    st = RiskState(now_ms=1_000_000, last_spot_ms={"BTC": 1_000_000 - 60_000})
    out = env.check(_enter(), st)
    assert out.action is Action.SKIP
    assert "stale spot" in out.reason


def test_exit_never_blocked():
    env = RiskEnvelope()
    # cap blown and no spot data - an exit must still pass through
    st = RiskState(now_ms=1_000_000, daily_realized_cents=-9999)
    ex = Decision(ticker="KXBTC15M-X", action=Action.EXIT, side=Side.NO,
                  size=1, reason="stop")
    out = env.check(ex, st)
    assert out.action is Action.EXIT
