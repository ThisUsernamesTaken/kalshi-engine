"""Equity index / proxy enum.

Separate from ``Crypto`` so the existing crypto code paths remain
unchanged. Each Equity value names a Kalshi 1hr-cycle index plus the
Alpaca proxy ticker we'll poll for the underlying spot.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Equity(str, Enum):
    """Equity index underlying tracked by Kalshi above/below series."""

    SPX = "SPX"  # S&P 500
    NDX = "NDX"  # Nasdaq-100


@dataclass(frozen=True, slots=True)
class EquitySpec:
    """Static mapping from an Equity to its Kalshi series + Alpaca proxy.

    ``alpaca_symbol`` is the ETF ticker used as the spot proxy (free Alpaca
    real-time IEX quotes available for these). ``kalshi_series`` is the
    above/below hourly series ticker.

    Note: Kalshi settles to the index value (SPX/NDX), NOT the ETF. The
    intraday basis between SPY/QQQ and SPX/NDX is small (~0.001%) but
    nonzero. The strategy must factor this in or recalibrate cutpoints to
    accept proxy-driven `bb_div` distributions.
    """

    equity: Equity
    kalshi_series: str
    alpaca_symbol: str
    title: str


SPECS: dict[Equity, EquitySpec] = {
    Equity.SPX: EquitySpec(
        equity=Equity.SPX,
        kalshi_series="KXINXU",
        alpaca_symbol="SPY",
        title="S&P 500 above/below (hourly)",
    ),
    Equity.NDX: EquitySpec(
        equity=Equity.NDX,
        kalshi_series="KXNASDAQ100U",
        alpaca_symbol="QQQ",
        title="Nasdaq-100 above/below (hourly)",
    ),
}
