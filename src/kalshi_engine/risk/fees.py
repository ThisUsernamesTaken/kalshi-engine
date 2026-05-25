"""Kalshi trading-fee math.

Kalshi charges a quadratic fee per trade that peaks at price 50c (maximum
outcome uncertainty) and approaches zero near 1c / 99c. Verified against the
current Kalshi fee schedule: takers pay a 0.07 coefficient, makers pay
0.0175 - exactly a quarter of the taker fee. Settlement is free; only trades
on the book incur a fee.

    fee = round_up(coeff * contracts * P * (1 - P))      P = price as a fraction

Prices are passed in **deci-cents** (0-1000, the engine-wide price unit) and
converted internally to the dollar fraction P = price_decicents / 1000. The
fee result is in whole cents (Kalshi rounds fees up to the next cent).

The standard rate applies to crypto markets; only INX / NASDAQ100 markets
use a different (0.035) coefficient, which is out of scope here.
"""

from __future__ import annotations

import math

TAKER_COEFF = 0.07
MAKER_COEFF = 0.0175  # exactly 25% of the taker coefficient

_VALID_ROLES = ("taker", "maker")


def _fee_cents(price_decicents: int, contracts: float, coeff: float) -> int:
    """Quadratic Kalshi fee in whole cents, rounded UP to the next cent."""
    if not 0 <= price_decicents <= 1000:
        raise ValueError(
            f"price_decicents must be in [0, 1000], got {price_decicents}"
        )
    if contracts < 0:
        raise ValueError(f"contracts must be >= 0, got {contracts}")
    p = price_decicents / 1000.0
    fee_dollars = coeff * contracts * p * (1.0 - p)
    # Round off sub-cent floating-point dust before the ceil, so a fee that
    # is mathematically exact on a cent boundary is not over-charged 1c.
    return math.ceil(round(fee_dollars * 100.0, 9))


def taker_fee(price_decicents: int, contracts: float) -> int:
    """Taker fee in cents for filling `contracts` at `price_decicents`."""
    return _fee_cents(price_decicents, contracts, TAKER_COEFF)


def maker_fee(price_decicents: int, contracts: float) -> int:
    """Maker fee in cents - a quarter of the taker fee."""
    return _fee_cents(price_decicents, contracts, MAKER_COEFF)


def kalshi_fee(price_decicents: int, contracts: float, role: str = "taker") -> int:
    """Trade fee in cents. `role` is 'taker' or 'maker'.

    The fee is independent of YES vs NO side: P*(1-P) is symmetric, so a YES
    contract at 790 dc and its NO complement at 210 dc incur an identical fee.
    """
    if role not in _VALID_ROLES:
        raise ValueError(f"role must be one of {_VALID_ROLES}, got {role!r}")
    coeff = TAKER_COEFF if role == "taker" else MAKER_COEFF
    return _fee_cents(price_decicents, contracts, coeff)


def roundtrip_fee(
    entry_price_decicents: int,
    exit_price_decicents: int,
    contracts: float,
    entry_role: str = "taker",
    exit_role: str = "taker",
    exit_is_settlement: bool = False,
) -> int:
    """Total fee in cents for an entry trade plus its exit.

    When the position is held to settlement pass `exit_is_settlement=True`:
    settlement incurs no fee, so only the entry leg is charged.
    """
    fee = kalshi_fee(entry_price_decicents, contracts, entry_role)
    if not exit_is_settlement:
        fee += kalshi_fee(exit_price_decicents, contracts, exit_role)
    return fee
