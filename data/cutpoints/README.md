# Phase 4 cutpoints — bundled artefacts

The model `kalshi_engine.strategies.favorite_chase.models.phase4_cutpoints`
loads gate thresholds + per-crypto bps thresholds from a JSON artefact at
runtime. The file lives in the warehouse (not the repo):

```
$KALSHI_ENGINE_WAREHOUSE/models/phase4_cutpoints/<version>/cutpoints.json
```

The engine ships two validated versions:

| Version | Used by | Notes |
|---|---|---|
| `v1` | 1hr live engine (`bin.live_1hr`) | Original Phase-4 thresholds |
| `v3` | 15m live engine (`bin.live`) | Phase 12.5 Rec 3 — recalibrated per-crypto bps thresholds for ETH/SOL/XRP |

Both are bundled in this directory so a fresh-clone install can populate
the warehouse without external dependencies. **See [INSTALL.md §6.5](../../INSTALL.md)
for the copy commands.**

## File schema (shared by v1 and v3)

```json
{
  "version": "phase4_v1",
  "created_at": "2026-05-22T17:50:00Z",
  "source": "Phase 4 expansion analysis on 218-trade BTC-real + alt-synthetic dataset",
  "vol_30m_percentile_skip_above": 0.67,
  "vol_30m_percentile_upsize_below": 0.50,
  "bb_div_skip_above": 0.09,
  "bb_div_upsize_below": -0.03,
  "bps_thresholds": {
    "BTC": 3.95, "ETH": 4.81, "SOL": 6.49, "XRP": 5.92, "DOGE": 7.89
  },
  ...
}
```

Field meanings — see `src/kalshi_engine/strategies/favorite_chase/models/phase4_cutpoints.py`
for the load + use sites. In short:

- `vol_30m_percentile_skip_above` — SKIP entries above this rolling-vol
  percentile (volatility regime hard gate).
- `bb_div_skip_above` / `bb_div_upsize_below` — Brownian-bridge divergence
  band edges (smile-artifact protection + upsize trigger).
- `bps_thresholds` — per-crypto minimum strike-distance in basis points
  (filters out cycles where the strike is too close to spot for the
  favorite-chase thesis to hold).

The hard-gate thresholds (vol, bb_div) are identical across v1 and v3.
**Only the per-crypto `bps_thresholds` differ.**

## Updating

When the Phase 6+ walk-forward recalibration produces a new artefact:

1. Write the new JSON to `$KALSHI_ENGINE_WAREHOUSE/models/phase4_cutpoints/<new_version>/cutpoints.json`
2. Copy it into `data/cutpoints/<new_version>/cutpoints.json` here
3. Update the table above
4. Commit + push
