"""Shared pytest fixtures.

Engine-code fixtures resolve warehouse data paths from ``kalshi_engine.config``.
Data-backed fixtures *skip* (not fail) when the warehouse file is absent, so
the suite stays green on a machine without the full D: warehouse.
"""

from __future__ import annotations

import pytest

from kalshi_engine.config import DERIVED_DIR, FIXTURES_DIR, RAW_DIR


@pytest.fixture(scope="session")
def fixtures_dir():
    """Absolute path to the warehouse fixtures directory."""
    return FIXTURES_DIR


@pytest.fixture(scope="session")
def burnin_db():
    """Small burn-in SQLite (burnin_continuous) for reader tests."""
    p = RAW_DIR / "burnin" / "burnin_continuous.sqlite"
    if not p.exists():
        pytest.skip(f"burn-in DB not present: {p}")
    return str(p)


@pytest.fixture(scope="session")
def burnin_db_4h():
    """Larger burn-in SQLite (burnin_4h) - more determined markets."""
    p = RAW_DIR / "burnin" / "burnin_4h.sqlite"
    if not p.exists():
        pytest.skip(f"burn-in DB not present: {p}")
    return str(p)


@pytest.fixture(scope="session")
def capture_dir():
    """Gradient-engine 4-stream JSONL capture directory."""
    p = RAW_DIR / "captures" / "2026-05-18"
    if not p.is_dir():
        pytest.skip(f"capture dir not present: {p}")
    return str(p)


@pytest.fixture(scope="session")
def spot_dir():
    """Spot-backfill parquet directory."""
    p = DERIVED_DIR / "spot_backfill"
    if not p.is_dir():
        pytest.skip(f"spot backfill dir not present: {p}")
    return str(p)
