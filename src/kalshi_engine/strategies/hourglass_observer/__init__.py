"""Hourglass 1hr observer — Phase 13.0.

Pure-observation strategy for KX{C}D 1hr digital markets. Captures book +
spot diagnostics at T+30/40/45/50/55 of each cycle. NO orders, NO risk
envelope. Data feeds future real-backtest of 1hr V13b port.
"""

from kalshi_engine.strategies.hourglass_observer.observer import (
    HourglassObserverStrategy,
)

__all__ = ["HourglassObserverStrategy"]
