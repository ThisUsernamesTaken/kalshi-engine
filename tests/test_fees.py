"""Kalshi fee math - hand-computed reference cases (prices in deci-cents)."""

from __future__ import annotations

import pytest

from kalshi_engine.risk.fees import kalshi_fee, maker_fee, roundtrip_fee, taker_fee


def test_taker_fee_hand_cases():
    # 0.07 * 1 * 0.50 * 0.50 = 0.017500$ = 1.7500c -> ceil -> 2
    assert taker_fee(500, 1) == 2
    # 0.07 * 1 * 0.79 * 0.21 = 0.011613$ = 1.1613c -> ceil -> 2
    assert taker_fee(790, 1) == 2
    # 0.07 * 1 * 0.01 * 0.99 = 0.000693$ = 0.0693c -> ceil -> 1
    assert taker_fee(10, 1) == 1
    # 0.07 * 100 * 0.50 * 0.50 = 1.7500$ = 175.00c (exact - no float over-charge)
    assert taker_fee(500, 100) == 175


def test_maker_is_quarter_of_taker():
    # 0.0175 * 1 * 0.50 * 0.50 = 0.004375$ = 0.4375c -> ceil -> 1
    assert maker_fee(500, 1) == 1
    # before rounding the maker fee is exactly 25% of the taker fee
    assert maker_fee(500, 1000) == round(taker_fee(500, 1000) * 0.25)


def test_kalshi_fee_dispatch():
    assert kalshi_fee(500, 100, "taker") == taker_fee(500, 100)
    assert kalshi_fee(500, 100, "maker") == maker_fee(500, 100)


def test_fee_symmetric_in_side():
    # a YES contract at 790 dc and its NO complement at 210 dc incur the same fee
    assert taker_fee(790, 10) == taker_fee(210, 10)


def test_roundtrip_held_to_settlement():
    # favorite-chase: 790 dc entry held to a $1.00 settlement -> only the entry fee
    assert roundtrip_fee(790, 1000, 1, exit_is_settlement=True) == taker_fee(790, 1)


def test_roundtrip_with_exit_trade():
    assert roundtrip_fee(790, 600, 1) == taker_fee(790, 1) + taker_fee(600, 1)


def test_invalid_inputs():
    with pytest.raises(ValueError):
        taker_fee(1001, 1)  # above the 1000 deci-cent max
    with pytest.raises(ValueError):
        taker_fee(500, -1)
    with pytest.raises(ValueError):
        kalshi_fee(500, 1, "bogus")
