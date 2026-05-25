"""bin/backtest.py end-to-end smoke test on a small quiescent burn-in window."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kalshi_engine.bin import backtest as backtest_mod
from kalshi_engine.config import MODELS_DIR


def test_backtest_main_smoke(burnin_db, tmp_path):
    """End-to-end: bin/backtest.py creates output dir + manifest.json + decisions.jsonl."""
    artifact = MODELS_DIR / "phase4_cutpoints" / "v1" / "cutpoints.json"
    if not artifact.exists():
        pytest.skip(f"cutpoints artifact not present: {artifact}")

    out = tmp_path / "out"
    rc = backtest_mod.main([
        "--from", "2026-05-12",
        "--to", "2026-05-12",
        "--cryptos", "BTC",
        "--burnin-db", burnin_db,
        "--output-dir", str(out),
        "--immutable-db",
    ])
    assert rc == 0
    assert (out / "decisions.jsonl").exists()
    assert (out / "manifest.json").exists()

    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["strategy"] == "favorite_chase"
    assert manifest["model"] == "phase4_cutpoints"
    assert manifest["cryptos"] == ["BTC"]
    assert "summary" in manifest
    assert manifest["summary"]["events_processed"] >= 1
    assert manifest["summary"]["markets_registered"] >= 1


def test_backtest_parse_args_defaults():
    args = backtest_mod.parse_args(["--from", "2026-05-18", "--to", "2026-05-19"])
    assert args.strategy == "favorite_chase"
    assert args.model == "phase4_cutpoints"
    assert args.cryptos == "BTC"
    assert args.max_contracts == 1
    assert args.daily_cap_cents == 1000
    assert args.immutable_db is False


def test_backtest_main_invalid_window():
    rc = backtest_mod.main([
        "--from", "2026-05-19",
        "--to", "2026-05-18",  # before --from
    ])
    assert rc == 2


def test_backtest_main_missing_burnin(tmp_path):
    rc = backtest_mod.main([
        "--from", "2026-05-12",
        "--to", "2026-05-12",
        "--burnin-db", str(tmp_path / "nonexistent.sqlite"),
        "--output-dir", str(tmp_path / "out"),
    ])
    assert rc == 2
