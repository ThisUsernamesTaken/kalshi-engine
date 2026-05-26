"""Tests for risk/pnl_reconcile.py (Phase 14.7 daily-cap defect fix)."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from kalshi_engine.risk.pnl_reconcile import (
    reconcile_today_realized_cents, utc_midnight_ms,
)


def _ts(year, month, day, hour, minute) -> int:
    return int(datetime(year, month, day, hour, minute,
                          tzinfo=timezone.utc).timestamp() * 1000)


def _write_log(path, entries):
    path.write_text("\n".join(json.dumps(e) for e in entries))


# ---- utc_midnight_ms ----------------------------------------------------

def test_utc_midnight_floor_at_midnight():
    ts = _ts(2026, 5, 26, 0, 0)
    assert utc_midnight_ms(ts) == ts


def test_utc_midnight_floor_during_day():
    ts = _ts(2026, 5, 26, 18, 30)
    expected = _ts(2026, 5, 26, 0, 0)
    assert utc_midnight_ms(ts) == expected


def test_utc_midnight_yesterday_at_2359():
    """23:59 UTC -> midnight of THAT same date (not tomorrow)."""
    ts = _ts(2026, 5, 25, 23, 59)
    expected = _ts(2026, 5, 25, 0, 0)
    assert utc_midnight_ms(ts) == expected


# ---- reconcile_today_realized_cents -------------------------------------

def _fill(ticker, side, yes_price, count, fee, log_ts_ms):
    return {
        "kind": "ws_order_update",
        "raw": {"type": "fill", "msg": {
            "market_ticker": ticker, "side": side.lower(),
            "yes_price_dollars": str(yes_price),
            "count_fp": str(float(count)),
            "fee_cost": str(fee),
        }},
        "log_ts_ms": log_ts_ms,
    }


def _settlement(ticker, settle_value, log_ts_ms):
    return {
        "kind": "settlement", "ticker": ticker,
        "settle_value": settle_value, "log_ts_ms": log_ts_ms,
    }


def test_reconcile_missing_log_returns_zero(tmp_path):
    nonexistent = tmp_path / "nonexistent.jsonl"
    assert reconcile_today_realized_cents(nonexistent, _ts(2026,5,26,12,0)) == 0


def test_reconcile_no_settlements_returns_zero(tmp_path):
    """Entries exist but nothing has settled — realized PnL is 0."""
    log = tmp_path / "engine.jsonl"
    now = _ts(2026, 5, 26, 12, 0)
    _write_log(log, [_fill("KXBTCD-T", "NO", 0.10, 10, 0.05, now)])
    assert reconcile_today_realized_cents(log, now) == 0


def test_reconcile_single_winning_trade(tmp_path):
    """NO at $0.10/ct (yes_price=0.10 -> NO cost = 0.90), 10ct, fee $0.05.
    settle_value=0.0 -> NO wins -> payout 1.0/ct.
    PnL = (1.0 - 0.90) * 10 - 0.05 = $0.95 = 95c."""
    log = tmp_path / "engine.jsonl"
    now = _ts(2026, 5, 26, 12, 0)
    _write_log(log, [
        _fill("KXBTCD-T", "NO", 0.10, 10, 0.05, now - 60_000),
        _settlement("KXBTCD-T", 0.0, now - 30_000),
    ])
    assert reconcile_today_realized_cents(log, now) == 95


def test_reconcile_single_losing_trade(tmp_path):
    """NO @ $0.90 cost, 10ct, fee $0.05. settle_value=1.0 -> YES wins -> NO
    side loses. PnL = (0.0 - 0.90)*10 - 0.05 = -$9.05 = -905c."""
    log = tmp_path / "engine.jsonl"
    now = _ts(2026, 5, 26, 12, 0)
    _write_log(log, [
        _fill("KXBTCD-T", "NO", 0.10, 10, 0.05, now - 60_000),
        _settlement("KXBTCD-T", 1.0, now - 30_000),
    ])
    assert reconcile_today_realized_cents(log, now) == -905


def test_reconcile_yes_winning_trade(tmp_path):
    """YES @ 0.85 cost, 7ct, fee $0.03. settle_value=1.0 -> YES wins.
    PnL = (1 - 0.85)*7 - 0.03 = 1.02 = 102c."""
    log = tmp_path / "engine.jsonl"
    now = _ts(2026, 5, 26, 12, 0)
    _write_log(log, [
        _fill("KXBTCD-T", "YES", 0.85, 7, 0.03, now - 60_000),
        _settlement("KXBTCD-T", 1.0, now - 30_000),
    ])
    assert reconcile_today_realized_cents(log, now) == 102


def test_reconcile_sums_multiple_settled(tmp_path):
    """Two settlements: -905 and +95 -> total -810c."""
    log = tmp_path / "engine.jsonl"
    now = _ts(2026, 5, 26, 12, 0)
    _write_log(log, [
        _fill("KXBTCD-A", "NO", 0.10, 10, 0.05, now - 600_000),
        _settlement("KXBTCD-A", 1.0, now - 580_000),  # loser -$9.05
        _fill("KXBTCD-B", "NO", 0.10, 10, 0.05, now - 300_000),
        _settlement("KXBTCD-B", 0.0, now - 250_000),  # winner +$0.95
    ])
    assert reconcile_today_realized_cents(log, now) == -905 + 95


def test_reconcile_ignores_pre_midnight_entries(tmp_path):
    """Entries from yesterday must NOT count toward today's realized."""
    log = tmp_path / "engine.jsonl"
    yesterday = _ts(2026, 5, 25, 22, 0)
    today_now = _ts(2026, 5, 26, 12, 0)
    _write_log(log, [
        _fill("KXBTCD-OLD", "NO", 0.10, 10, 0.05, yesterday),
        _settlement("KXBTCD-OLD", 1.0, yesterday + 60_000),
    ])
    # All entries are pre-midnight relative to today_now -> 0
    assert reconcile_today_realized_cents(log, today_now) == 0


def test_reconcile_handles_unsettled_opens(tmp_path):
    """Open positions (fills but no settlement) are NOT counted."""
    log = tmp_path / "engine.jsonl"
    now = _ts(2026, 5, 26, 12, 0)
    _write_log(log, [
        _fill("KXBTCD-OPEN", "NO", 0.10, 10, 0.05, now - 60_000),
        # no settlement event
    ])
    assert reconcile_today_realized_cents(log, now) == 0


def test_reconcile_aggregates_multiple_fills_same_ticker(tmp_path):
    """Two fills on the same ticker+side combine at weighted-avg cost."""
    log = tmp_path / "engine.jsonl"
    now = _ts(2026, 5, 26, 12, 0)
    # Two NO fills at YES prices 0.10 and 0.20 -> NO costs 0.90 and 0.80
    # Combined: 10 + 10 = 20ct, total cost = 0.90*10 + 0.80*10 = 17.0
    # Avg cost = 0.85/ct
    _write_log(log, [
        _fill("KXBTCD-T", "NO", 0.10, 10, 0.05, now - 600_000),
        _fill("KXBTCD-T", "NO", 0.20, 10, 0.03, now - 500_000),
        _settlement("KXBTCD-T", 0.0, now - 100_000),  # NO wins
    ])
    # PnL = (1 - 0.85)*20 - 0.08 = 3.0 - 0.08 = $2.92 = 292c
    assert reconcile_today_realized_cents(log, now) == 292


def test_reconcile_ignores_non_crypto_tickers(tmp_path):
    """Default ticker filter excludes non-KX{C}{15M,D} markets."""
    log = tmp_path / "engine.jsonl"
    now = _ts(2026, 5, 26, 12, 0)
    _write_log(log, [
        _fill("KXNBA2HSPREAD-FOO", "NO", 0.10, 10, 0.05, now - 60_000),
        _settlement("KXNBA2HSPREAD-FOO", 1.0, now - 30_000),
    ])
    # Non-crypto ticker — excluded from accounting
    assert reconcile_today_realized_cents(log, now) == 0


def test_reconcile_handles_malformed_lines(tmp_path):
    """Garbage lines must not crash; reconcile just skips them."""
    log = tmp_path / "engine.jsonl"
    now = _ts(2026, 5, 26, 12, 0)
    log.write_text(
        "{not json\n"
        + json.dumps(_fill("KXBTCD-T", "NO", 0.10, 10, 0.05, now - 60_000)) + "\n"
        + "another bad line\n"
        + json.dumps(_settlement("KXBTCD-T", 0.0, now - 30_000)) + "\n"
    )
    assert reconcile_today_realized_cents(log, now) == 95


def test_reconcile_cap_binds_at_threshold():
    """Sanity: -1000c reconciled should trigger the envelope's cap check."""
    from kalshi_engine.risk.envelope import RiskEnvelope, RiskState
    from kalshi_engine.core.interfaces import Decision
    from kalshi_engine.core.types import Action, Side
    env = RiskEnvelope(daily_loss_cap_cents=1000)
    state = RiskState(daily_realized_cents=-1000)
    state.open_positions = set()
    state.last_spot_ms = {"BTC": int(datetime.now(tz=timezone.utc).timestamp()*1000)}
    state.now_ms = state.last_spot_ms["BTC"]
    d = Decision(ticker="KXBTC15M-T", action=Action.ENTER, side=Side.YES,
                  size=1, confidence=1.0, reason="test", diagnostics={})
    out = env.check(d, state)
    assert out.action is Action.SKIP
    assert "daily loss cap" in out.reason


def test_reconcile_cap_unbound_below_threshold():
    """One cent shy of cap -> still allowed (cap is <= threshold check)."""
    from kalshi_engine.risk.envelope import RiskEnvelope, RiskState
    from kalshi_engine.core.interfaces import Decision
    from kalshi_engine.core.types import Action, Side
    env = RiskEnvelope(daily_loss_cap_cents=1000)
    state = RiskState(daily_realized_cents=-999)
    state.last_spot_ms = {"BTC": int(datetime.now(tz=timezone.utc).timestamp()*1000)}
    state.now_ms = state.last_spot_ms["BTC"]
    d = Decision(ticker="KXBTC15M-T", action=Action.ENTER, side=Side.YES,
                  size=1, confidence=1.0, reason="test", diagnostics={})
    out = env.check(d, state)
    # The cap doesn't trigger but spot age may; just check the cap-specific message isn't fired
    assert "daily loss cap" not in (out.reason or "")
