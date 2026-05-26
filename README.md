# kalshi-engine

Trading and research framework for Kalshi's 15-minute crypto binary markets
(BTC / ETH / SOL / XRP / DOGE).

The flagship live strategy is **favorite-chase V13b**: enter the cycle's
≥75¢ favorite at T+8 minutes if a tuned cutpoints model passes its hard
gates, then size by a weighted-evidence "alignment" score from 1 to 10
contracts. The engine ships with a backtest harness and a 1-hour observer
process that captures pre-trigger book data for future analysis.

> ⚠ **Trading real money on Kalshi requires an account and API key, and
> involves risk of loss.** Read the strategy notes below and start in
> ``--dry-run`` mode.

## Requirements

- Python **3.12+**
- A Kalshi account with an API key (RSA-signed, see
  <https://trading-api.readme.io/reference/getting-started>)
- ~500 MB free disk for logs and any local data caches
- (Optional) A Bitstamp or Coinbase Pro spot data source; the engine
  defaults to Bitstamp REST polling and needs no API key for that

## Install

```bash
git clone https://github.com/<you>/kalshi-engine.git
cd kalshi-engine
python -m venv .venv
source .venv/bin/activate          # POSIX
# .venv\Scripts\Activate.ps1       # Windows PowerShell

pip install -e ".[dev]"
```

## Configure

Two environment variables are **required**:

| Variable | Purpose | Example |
|---|---|---|
| `KALSHI_ENGINE_WAREHOUSE` | Writable directory for logs / models / backtest output | `D:\Trading\warehouse` or `~/.kalshi_engine/warehouse` |
| `KALSHI_API_KEY_PATH` | Path to a `.env`-style file containing your API key + PEM path | `~/.kalshi/credentials.env` |

The credentials file must contain two lines:

```env
KALSHI_API_KEY=<your-uuid-api-key>
KALSHI_PRIVATE_KEY_PATH=<absolute-path-to-your-rsa-key.pem>
```

The engine creates subdirectories under `$KALSHI_ENGINE_WAREHOUSE` on first
use (`raw/`, `derived/`, `models/`, `fixtures/`, `backtest_results/`,
`meta/`). The Phase 4 cutpoints artefact must be present at
`<warehouse>/models/phase4_cutpoints/{v1,v3}/cutpoints.json`. The repo
bundles both versions under `data/cutpoints/` — see
[INSTALL.md §7.5](INSTALL.md) for the copy commands a fresh install needs.

## Run

### Dry-run smoke test (no real orders)

```bash
python -m kalshi_engine.bin.live --dry-run --duration-s 60
```

### Live 15m engine (current production V13b config)

```bash
python -m kalshi_engine.bin.live \
    --strategy favorite_chase \
    --model phase4_cutpoints \
    --cutpoints-version v1 \
    --align-mode 5tier_v13b_h1h4 \
    --max-contracts 10 \
    --reentry-mode disabled \
    --time-of-day-skip enabled \
    --cryptos BTC,ETH,SOL,XRP,DOGE \
    --spot-source bitstamp \
    --stop-mode none \
    --bps-gate enabled \
    --daily-cap-cents 1000
```

`--align-mode 5tier_v13b_h1h4` is the current production sizing mode
(Phase 12.13). It combines H1's score floor (skip every cohort losing
tier — score 3.5 was the only tier with losses) with H4's smooth
score-multiplier sizing on what passes:

| score | size |
|---|---|
| `< 4.0` | SKIP |
| `= 4.0` | 7 ct |
| `= 4.5` | 8 ct |
| `= 5.0` | 9 ct |
| `>= 5.5` | 10 ct (capped) |

Sizing formula: `size = min(10, round(score * 1.8))`.

Counterfactual on the n=88 V13b live cohort: **+$37.78** vs S2 +$30.37
(+24%) with 100% WR on the kept set. Earlier modes `5tier_v13b_s2` and
`5tier_v13b` are preserved as backward-compat options.

`--time-of-day-skip enabled` is the validated default. A brief
counterfactual run with the gate disabled was reverted after one
previously-blocked cycle produced a -$3.40 loss; the gate is doing
real protective work.

**Cumulative live performance** (snapshot 2026-05-25, 63.5 h of live
trading): 284 trades, 91.6% WR, **+$22.71 net realized PnL**. The
V13b `max-contracts=10` phase has been the engine's most profitable era
at ~$0.34/trade and 98% WR. See the source-of-truth JSONL log under
`$KALSHI_ENGINE_WAREHOUSE/raw/live_logs/` for full per-trade detail.

A successful boot emits a single `{"kind":"boot",...}` line to the JSONL
log under `$KALSHI_ENGINE_WAREHOUSE/raw/live_logs/`. After that, every
decision, fill, settlement and book event is appended as one JSON line per
event.

### 1-hour observer (Phase 13.0 — read-only)

```bash
python -m kalshi_engine.bin.observe_1hr \
    --cryptos BTC,ETH,SOL,XRP,DOGE \
    --observe-times 30,40,45,50,55
```

Subscribes to KX{BTC,ETH,SOL,XRP,DOGE}D 1-hour markets and emits
`book_at_1hr_pretrigger` envelopes at the configured minute offsets.
Places no orders.

### Backtest

```bash
python -m kalshi_engine.bin.backtest \
    --from 2026-05-18 --to 2026-05-19 \
    --cryptos BTC \
    --burnin-db /path/to/your/burnin.sqlite
```

The burn-in source SQLite can also be supplied via
`KALSHI_ENGINE_BURNIN_DB`, or placed at
`$KALSHI_ENGINE_WAREHOUSE/raw/burnin/burnin.sqlite`.

## Test

```bash
pytest
```

The default suite is ~236 tests and runs in under a minute. Tests skip
gracefully if optional warehouse artefacts are missing.

## Architecture

```
src/kalshi_engine/
├── bin/               CLI entry points (live, observe_1hr, backtest)
├── config/            warehouse root resolution
├── core/              event types, model + strategy protocols
├── feeds/             Kalshi WebSocket feed + Bitstamp/Coinbase spot feed
├── execution/         Kalshi REST client + live order adapter
├── risk/              risk envelope + fee math
├── strategies/
│   ├── favorite_chase/   the 15m strategy (state, rules, V13b model)
│   └── hourglass_observer/  read-only 1hr observer
├── research/          cycle tracker (settlement-time summary)
├── warehouse/         JSONL writer + settlement decode utilities
└── backtest/          fill simulator + replayer
```

The strategy is intentionally pluggable: `core.interfaces` defines the
``Strategy`` and ``Model`` protocols, ``bin/live.py`` is the wiring layer,
and everything else (signals, sizing, gates) is in
``strategies/favorite_chase/``. Adding a new strategy is mostly a new
module under `strategies/` plus a CLI dispatch in `bin/live.py`.

## Strategy notes — V13b

V13b is a hard-gate-then-score conviction model. The score formula is:

```
score = 2 * bb_div_band + 1.5 * side_no + 2 * bps_strong + super_band_bonus
size  = round(score) clipped to [1, 10] if score > 0 else SKIP
```

Hard gates applied BEFORE scoring:

- `vol_pct > 0.67` → SKIP (high-vol regime)
- `bb_div ≤ -0.20` or `bb_div > +0.09` → SKIP (smile-artifact / unfav)
- `bps_margin < per-crypto threshold` → SKIP (strike too close)

The validated thresholds for the four score components live in
``strategies/favorite_chase/models/phase4_cutpoints.py``. A full audit
trail of how the formula was derived (univariate / marginal / joint
factor analysis) is preserved in the git history.

## Known limitations

- Tested primarily on Windows; POSIX paths work but less-exercised.
- The backtest harness expects a Kalshi book-event SQLite ("burn-in DB")
  in a specific schema (see `warehouse/adapters.py::BurninReader`). If you
  don't have one, the live engine still runs from the WebSocket feed; only
  the backtest path requires it.
- The Phase 4 cutpoints artefact is **not** shipped with the package.
  Place a `cutpoints.json` at
  `$KALSHI_ENGINE_WAREHOUSE/models/phase4_cutpoints/v1/cutpoints.json`
  with the schema documented in that module.
- Spot feed: Bitstamp REST polling is the default and most reliable;
  Coinbase WS has a known staleness defect for some pairs.

## License

MIT — see `LICENSE`.
