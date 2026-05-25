"""Engine configuration: warehouse paths and runtime settings.

All filesystem state lives under one root, configured via the
``KALSHI_ENGINE_WAREHOUSE`` environment variable. The engine creates the
required subdirectories on first use; you only need to point the env var at
a writable directory.

Example (PowerShell)::

    $env:KALSHI_ENGINE_WAREHOUSE = "D:\\Trading\\warehouse"

Example (POSIX shell)::

    export KALSHI_ENGINE_WAREHOUSE=~/.kalshi_engine/warehouse

Subdirectory layout::

    <root>/
        raw/         live JSONL logs, captured book/spot events
        derived/     materialised parquet datasets
        models/      versioned model artefacts (cutpoints, etc.)
        fixtures/    test fixtures
        backtest_results/  per-run replay outputs
        meta/        small bookkeeping files (versions, manifests)

Trading credentials live OUTSIDE the warehouse and are configured via
``KALSHI_API_KEY_PATH`` (a .env-style file with ``KALSHI_API_KEY=...`` and
``KALSHI_PRIVATE_KEY_PATH=...``).
"""

from __future__ import annotations

import os
from pathlib import Path


class ConfigError(RuntimeError):
    """Raised when a required environment variable is missing."""


def _require_env(name: str, hint: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise ConfigError(
            f"Missing required environment variable {name!r}. {hint}\n"
            f"Set it before importing kalshi_engine.config."
        )
    return val


WAREHOUSE_ROOT = Path(_require_env(
    "KALSHI_ENGINE_WAREHOUSE",
    "Point this at a writable directory where the engine will store "
    "logs, models, and backtest output. Example: "
    "'D:\\\\Trading\\\\warehouse' on Windows or "
    "'~/.kalshi_engine/warehouse' on POSIX.",
))

RAW_DIR = WAREHOUSE_ROOT / "raw"
DERIVED_DIR = WAREHOUSE_ROOT / "derived"
MODELS_DIR = WAREHOUSE_ROOT / "models"
FIXTURES_DIR = WAREHOUSE_ROOT / "fixtures"
BACKTEST_RESULTS_DIR = WAREHOUSE_ROOT / "backtest_results"
META_DIR = WAREHOUSE_ROOT / "meta"
