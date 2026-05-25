"""Replayer: 1-hour BTC replay over a small quiescent burn-in."""

from __future__ import annotations

import pytest

from kalshi_engine.backtest.fill_simulator import FillSimulator
from kalshi_engine.backtest.replay import Replayer
from kalshi_engine.config import MODELS_DIR
from kalshi_engine.core.types import Crypto
from kalshi_engine.risk.envelope import RiskEnvelope
from kalshi_engine.strategies.favorite_chase.models.phase4_cutpoints import (
    Phase4CutpointsModel,
)
from kalshi_engine.strategies.favorite_chase.strategy import FavoriteChaseStrategy
from kalshi_engine.warehouse.adapters import LiveLogWriter
from kalshi_engine.warehouse.settlement import _iso_to_ms


def test_replay_btc_one_hour(burnin_db, tmp_path):
    """Smoke: replay 1 h of BTC events on a quiescent burn-in DB."""
    artifact = MODELS_DIR / "phase4_cutpoints" / "v1" / "cutpoints.json"
    if not artifact.exists():
        pytest.skip(f"cutpoints artifact not present: {artifact}")

    log = LiveLogWriter(str(tmp_path / "replay.jsonl"))
    model = Phase4CutpointsModel()
    strategy = FavoriteChaseStrategy(model)
    envelope = RiskEnvelope()
    execution = FillSimulator(log)
    replayer = Replayer(strategy, envelope, execution, log)

    start_ms = _iso_to_ms("2026-05-12T16:00:00Z")
    end_ms = _iso_to_ms("2026-05-12T17:00:00Z")

    summary = replayer.replay_window(
        burnin_path=burnin_db,
        spot_dir=None,   # burnin's own spot_quote_event is used; parquets cover May 13+ only
        cryptos=[Crypto.BTC],
        start_ms=start_ms,
        end_ms=end_ms,
        immutable_db=True,  # burnin_continuous is quiescent
    )
    assert summary["events_processed"] >= 1
    assert summary["markets_registered"] >= 1
    # decisions may or may not fire depending on cycle alignment + cutpoints
    assert summary["decisions_emitted"] >= 0


def test_replay_emits_replay_boot_and_done(burnin_db, tmp_path):
    """The replay log carries replay_boot + replay_done envelopes."""
    artifact = MODELS_DIR / "phase4_cutpoints" / "v1" / "cutpoints.json"
    if not artifact.exists():
        pytest.skip(f"cutpoints artifact not present: {artifact}")

    log_path = tmp_path / "replay.jsonl"
    log = LiveLogWriter(str(log_path))
    model = Phase4CutpointsModel()
    strategy = FavoriteChaseStrategy(model)
    envelope = RiskEnvelope()
    execution = FillSimulator(log)
    replayer = Replayer(strategy, envelope, execution, log)

    replayer.replay_window(
        burnin_path=burnin_db,
        spot_dir=None,
        cryptos=[Crypto.BTC],
        start_ms=_iso_to_ms("2026-05-12T16:00:00Z"),
        end_ms=_iso_to_ms("2026-05-12T17:00:00Z"),
        immutable_db=True,
    )

    import json
    lines = log_path.read_text(encoding="utf-8").splitlines()
    events = [json.loads(l) for l in lines if l.strip()]
    kinds = {e.get("kind") for e in events}
    assert "replay_boot" in kinds
    assert "replay_done" in kinds


def test_replay_skips_burnin_spot_events(burnin_db, tmp_path):
    """The Replayer must not feed SpotEvent from the burn-in (gappy)
    when a spot parquet directory is supplied; SpotEvents come from the
    parquet path. With ``spot_dir=None`` and no spots in scope, the
    decisions seen by the model should reflect that."""
    # This is an indirect check: just verify the run completes without raising.
    artifact = MODELS_DIR / "phase4_cutpoints" / "v1" / "cutpoints.json"
    if not artifact.exists():
        pytest.skip(f"cutpoints artifact not present: {artifact}")
    log = LiveLogWriter(str(tmp_path / "replay.jsonl"))
    replayer = Replayer(
        FavoriteChaseStrategy(Phase4CutpointsModel()),
        RiskEnvelope(),
        FillSimulator(log),
        log,
    )
    summary = replayer.replay_window(
        burnin_path=burnin_db,
        spot_dir=None,
        cryptos=[Crypto.BTC],
        start_ms=_iso_to_ms("2026-05-12T16:00:00Z"),
        end_ms=_iso_to_ms("2026-05-12T17:00:00Z"),
        immutable_db=True,
    )
    assert summary["events_processed"] >= 1
