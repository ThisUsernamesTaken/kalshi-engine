"""SettlementResolver: lifecycle resolution cross-checked against synthetic."""

from __future__ import annotations

import sqlite3

import pytest

from kalshi_engine.core.types import Side
from kalshi_engine.warehouse.settlement import FallbackPolicy, SettlementResolver


def _determined_btc_tickers(db: str) -> list[str]:
    con = sqlite3.connect(f"file:{db}?mode=ro&immutable=1", uri=True)
    rows = con.execute(
        "SELECT DISTINCT market_ticker FROM kalshi_lifecycle_event "
        "WHERE status = 'determined' AND market_ticker LIKE 'KXBTC%'"
    ).fetchall()
    con.close()
    return [r[0] for r in rows]


def test_lifecycle_resolution_is_mechanically_sound(burnin_db_4h):
    tickers = _determined_btc_tickers(burnin_db_4h)
    if not tickers:
        pytest.skip("no determined BTC markets in this burn-in DB")
    with SettlementResolver(burnin_db_4h) as res:
        results = res.resolve_all(tickers[:80])
    resolved = [r for r in results.values() if r is not None]
    assert resolved, "lifecycle resolution produced nothing"
    for r in resolved:
        assert r.source == "lifecycle"
        assert r.event.result in (Side.YES, Side.NO)
        assert r.event.settle_value in (0.0, 1.0)


def test_strict_policy_returns_only_lifecycle(burnin_db_4h):
    tickers = _determined_btc_tickers(burnin_db_4h)
    if not tickers:
        pytest.skip("no determined BTC markets in this burn-in DB")
    with SettlementResolver(burnin_db_4h, policy=FallbackPolicy.STRICT) as res:
        r = res.resolve(tickers[0])
    assert r is not None and r.source == "lifecycle"


def test_synthetic_agrees_with_lifecycle(burnin_db_4h, spot_dir):
    tickers = _determined_btc_tickers(burnin_db_4h)
    if not tickers:
        pytest.skip("no determined BTC markets in this burn-in DB")
    sample = tickers[:120]
    with SettlementResolver(burnin_db_4h, spot_dir) as res:
        lifecycle = res.resolve_all(sample)
    with SettlementResolver(
        burnin_db_4h, spot_dir, policy=FallbackPolicy.FORCED_SYNTHETIC
    ) as res:
        synthetic = res.resolve_all(sample)

    both = [
        t for t in sample
        if lifecycle.get(t) is not None and synthetic.get(t) is not None
    ]
    if not both:
        pytest.skip("no markets resolvable by both lifecycle and synthetic")
    agree = sum(
        lifecycle[t].event.result == synthetic[t].event.result for t in both
    )
    rate = agree / len(both)
    print(f"\nsettlement agreement: {agree}/{len(both)} = {rate:.1%}")
    # Mechanical floor only: a broken synthetic resolver scores ~50% (random).
    # Near-the-line markets cause legitimate statistical disagreement.
    if len(both) >= 20:
        assert rate >= 0.80
    else:
        assert rate >= 0.60
