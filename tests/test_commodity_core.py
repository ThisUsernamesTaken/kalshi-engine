"""Phase 14.16: commodity enum + spec invariants."""

from __future__ import annotations

from kalshi_engine.core.commodity import (
    BRENT_CURRENT_CONTRACT,
    BRENT_MONTH_FEEDS,
    GOLD_XAU_USD_FEED,
    SPECS,
    Commodity,
    feed_for_brent_contract,
    live_specs,
)


def test_specs_cover_enum():
    assert set(SPECS) == set(Commodity)


def test_gold_spec_is_live_and_exact_feed():
    g = SPECS[Commodity.GOLD]
    assert g.kalshi_series == "KXGOLDD"
    assert g.pyth_feed_id == GOLD_XAU_USD_FEED
    assert g.pyth_symbol == "Metal.XAU/USD"
    assert g.pyth_live is True
    assert g.live_enabled is True
    assert g.contract_rolls is False
    assert g.bps_threshold > 0


def test_brent_spec_is_data_blocked():
    """Brent's settlement feed (BRENTQ6) is dead on Pyth -> must not be live."""
    b = SPECS[Commodity.BRENT]
    assert b.kalshi_series == "KXBRENTD"
    assert b.pyth_live is False
    assert b.live_enabled is False
    assert b.contract_rolls is True
    # Spec points at the current settlement contract month.
    assert b.pyth_feed_id == BRENT_MONTH_FEEDS[BRENT_CURRENT_CONTRACT]


def test_live_specs_excludes_brent():
    live = [s.commodity for s in live_specs()]
    assert Commodity.GOLD in live
    assert Commodity.BRENT not in live


def test_feed_for_brent_contract():
    assert feed_for_brent_contract("BRENTQ6") == BRENT_MONTH_FEEDS["BRENTQ6"]
    assert feed_for_brent_contract("brentq6") == BRENT_MONTH_FEEDS["BRENTQ6"]
    assert feed_for_brent_contract("BRENTNOPE") is None


def test_brent_feed_ids_are_unique_hex():
    ids = list(BRENT_MONTH_FEEDS.values())
    assert len(ids) == len(set(ids))
    for fid in ids:
        assert len(fid) == 64
        int(fid, 16)  # raises if not hex
