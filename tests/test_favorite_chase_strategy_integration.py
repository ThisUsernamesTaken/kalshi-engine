"""FavoriteChaseStrategy integration: replay ~1h of burn-in BTC events."""

from __future__ import annotations

import json
import sqlite3

import pytest

from kalshi_engine.config import MODELS_DIR
from kalshi_engine.core.events import BookEvent, SpotEvent
from kalshi_engine.core.interfaces import Decision
from kalshi_engine.strategies.favorite_chase.models.phase4_cutpoints import (
    Phase4CutpointsModel,
)
from kalshi_engine.strategies.favorite_chase.strategy import FavoriteChaseStrategy
from kalshi_engine.warehouse.adapters import BurninReader
from kalshi_engine.warehouse.settlement import _iso_to_ms


def _btc_markets(db: str):
    con = sqlite3.connect(f"file:{db}?mode=ro&immutable=1", uri=True)
    rows = con.execute(
        "SELECT ticker, open_time, close_time, raw_json FROM market_dim "
        "WHERE ticker LIKE 'KXBTC%'"
    ).fetchall()
    con.close()
    out = []
    for ticker, ot, ct, rj in rows:
        om, cm = _iso_to_ms(ot), _iso_to_ms(ct)
        try:
            strike = float(json.loads(rj).get("floor_strike"))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if om is not None and cm is not None and strike:
            out.append((ticker, strike, om, cm))
    return out


def test_strategy_replay_burnin(burnin_db):
    artifact = MODELS_DIR / "phase4_cutpoints" / "v1" / "cutpoints.json"
    if not artifact.exists():
        pytest.skip(f"cutpoints artifact not present: {artifact}")
    markets = _btc_markets(burnin_db)
    if not markets:
        pytest.skip("no BTC markets with metadata in this burn-in DB")

    # Burn-in BTC ticker timestamps happen to land in the new 14-17Z TOD
    # window; disable that gate here since we're testing replay mechanics,
    # not time-of-day filtering.
    strat = FavoriteChaseStrategy(
        Phase4CutpointsModel(time_of_day_skip=False),
    )
    for ticker, strike, open_ms, close_ms in markets:
        strat.register_market(ticker, strike, open_ms, close_ms)

    start = min(om for _, _, om, _ in markets)
    end = start + 3_600_000  # ~1 hour slice

    decisions: list[Decision] = []
    with BurninReader(burnin_db) as reader:
        for event in reader.iter_range(start, end):
            if isinstance(event, (BookEvent, SpotEvent)):
                out = strat.on_event(event)
                if out is not None:
                    decisions.append(out)

    print(f"\nfavorite-chase replay: {len(decisions)} decisions / "
          f"{len(markets)} BTC markets registered")
    assert decisions, "expected at least one favorite-chase decision"
    for d in decisions:
        assert isinstance(d, Decision)
        assert d.ticker.startswith("KXBTC")
        assert d.reason  # human-readable reason populated
        assert d.diagnostics  # signal diagnostics populated
        # an evaluated decision carries the full signal set unless it
        # short-circuited on missing spot/vol history
        assert "bb_div" in d.diagnostics or "no spot" in d.reason
