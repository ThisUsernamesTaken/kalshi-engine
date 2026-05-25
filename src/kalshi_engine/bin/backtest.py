"""Backtest entry point - concept-integrity replay over warehouse events.

Drives the favorite-chase pipeline (strategy + cutpoints model + risk envelope
+ FillSimulator) over a historical window. Writes decisions.jsonl + manifest
.json into ``$KALSHI_ENGINE_WAREHOUSE/backtest_results/<run_id>/`` by default.

    python -m kalshi_engine.bin.backtest --from 2026-05-18 --to 2026-05-19 --cryptos BTC

The burn-in source SQLite (Kalshi book + spot capture) is found in this order:
    1. ``--burnin-db <path>`` CLI flag (highest priority)
    2. ``KALSHI_ENGINE_BURNIN_DB`` environment variable
    3. ``$KALSHI_ENGINE_WAREHOUSE/raw/burnin/burnin.sqlite`` (default location)

If none of these resolve to an existing file the backtest exits with a clear
error.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from kalshi_engine.backtest.fill_simulator import FillSimulator
from kalshi_engine.backtest.replay import Replayer
from kalshi_engine.config import WAREHOUSE_ROOT
from kalshi_engine.core.types import Crypto
from kalshi_engine.risk.envelope import RiskEnvelope
from kalshi_engine.strategies.favorite_chase.models.phase4_cutpoints import (
    Phase4CutpointsModel,
)
from kalshi_engine.strategies.favorite_chase.strategy import FavoriteChaseStrategy
from kalshi_engine.warehouse.adapters import LiveLogWriter


def _default_burnin_db() -> str | None:
    """Resolve burn-in SQLite path from env or warehouse default."""
    env = os.environ.get("KALSHI_ENGINE_BURNIN_DB")
    if env:
        return env
    default = WAREHOUSE_ROOT / "raw" / "burnin" / "burnin.sqlite"
    return str(default) if default.exists() else None


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="kalshi_engine backtest")
    p.add_argument("--strategy", default="favorite_chase",
                   choices=["favorite_chase"])
    p.add_argument("--model", default="phase4_cutpoints",
                   choices=["phase4_cutpoints"])
    p.add_argument("--cryptos", default="BTC",
                   help="comma-separated crypto symbols")
    p.add_argument("--from", dest="from_date", required=True,
                   help="window start, YYYY-MM-DD UTC (inclusive)")
    p.add_argument("--to", dest="to_date", required=True,
                   help="window end, YYYY-MM-DD UTC (inclusive)")
    p.add_argument("--warehouse", default=str(WAREHOUSE_ROOT))
    p.add_argument("--burnin-db", default=None,
                   help="path to burn-in SQLite (default: live capture if present)")
    p.add_argument("--output-dir", default=None,
                   help="default: <warehouse>/backtest_results/<auto run_id>/")
    p.add_argument("--immutable-db", action="store_true",
                   help="open burn-in DB with immutable=1 (safe only when "
                        "the DB is quiescent - NOT the live capture)")
    p.add_argument("--max-contracts", type=int, default=1)
    p.add_argument("--daily-cap-cents", type=int, default=1000)
    return p.parse_args(argv)


def _date_to_ms(s: str, end_of_day: bool = False) -> int:
    """Parse YYYY-MM-DD UTC -> epoch ms. ``end_of_day`` rolls to next-day 00:00."""
    dt = datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
    if end_of_day:
        dt = dt.replace(hour=23, minute=59, second=59, microsecond=999_000)
    return int(dt.timestamp() * 1000)


def _resolve_burnin_db(args: argparse.Namespace, warehouse: Path) -> str | None:
    """Resolve the burn-in DB: --burnin-db, then env, then any sqlite under warehouse."""
    if args.burnin_db:
        return args.burnin_db
    env_db = _default_burnin_db()
    if env_db and Path(env_db).exists():
        return env_db
    # fall back to any sqlite under warehouse/raw/burnin (alphabetical)
    burnin_dir = warehouse / "raw" / "burnin"
    candidates = sorted(burnin_dir.glob("*.sqlite")) if burnin_dir.exists() else []
    return str(candidates[0]) if candidates else None


def main(argv=None) -> int:
    args = parse_args(argv)

    try:
        cryptos = [Crypto(c.strip().upper()) for c in args.cryptos.split(",") if c.strip()]
    except ValueError as exc:
        print(f"ERROR: invalid --cryptos value: {exc}", file=sys.stderr)
        return 2
    if not cryptos:
        print("ERROR: --cryptos must list at least one crypto", file=sys.stderr)
        return 2

    start_ms = _date_to_ms(args.from_date)
    end_ms = _date_to_ms(args.to_date, end_of_day=True)
    if end_ms <= start_ms:
        print("ERROR: --to must be on or after --from", file=sys.stderr)
        return 2

    warehouse = Path(args.warehouse)
    burnin_db = _resolve_burnin_db(args, warehouse)
    if burnin_db is None or not Path(burnin_db).exists():
        print(f"ERROR: burn-in DB not found (tried: {burnin_db})", file=sys.stderr)
        return 2

    spot_dir = warehouse / "derived" / "spot_backfill"

    run_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.output_dir) if args.output_dir else (
        warehouse / "backtest_results" / run_id
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    log_path = out_dir / "decisions.jsonl"
    log = LiveLogWriter(str(log_path))

    model = Phase4CutpointsModel()
    strategy = FavoriteChaseStrategy(model)
    envelope = RiskEnvelope(
        daily_loss_cap_cents=args.daily_cap_cents,
        max_contracts_per_trade=args.max_contracts,
    )
    execution = FillSimulator(log)
    replayer = Replayer(strategy, envelope, execution, log)

    config = {
        "run_id": run_id,
        "strategy": args.strategy,
        "model": args.model,
        "model_cutpoints_version": model.cutpoints.get("version"),
        "model_cutpoints_path": str(model.cutpoints_path),
        "cryptos": [c.value for c in cryptos],
        "from": args.from_date,
        "to": args.to_date,
        "start_ms": start_ms,
        "end_ms": end_ms,
        "burnin_db": str(burnin_db),
        "spot_dir": str(spot_dir),
        "immutable_db": args.immutable_db,
        "max_contracts": args.max_contracts,
        "daily_cap_cents": args.daily_cap_cents,
        "output_dir": str(out_dir),
    }

    print(f"[backtest] run_id={run_id}", file=sys.stderr, flush=True)
    print(f"[backtest] window {args.from_date} -> {args.to_date} "
          f"({(end_ms - start_ms) / 86_400_000:.2f} day(s))",
          file=sys.stderr, flush=True)
    print(f"[backtest] cryptos={[c.value for c in cryptos]}", file=sys.stderr, flush=True)
    print(f"[backtest] burnin_db={burnin_db}", file=sys.stderr, flush=True)
    print(f"[backtest] output={out_dir}", file=sys.stderr, flush=True)

    summary = replayer.replay_window(
        burnin_path=burnin_db,
        spot_dir=str(spot_dir) if spot_dir.exists() else None,
        cryptos=cryptos,
        start_ms=start_ms,
        end_ms=end_ms,
        immutable_db=args.immutable_db,
    )

    manifest = {**config, "summary": summary}
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8",
    )
    print(f"[backtest] done: {summary}", file=sys.stderr, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
