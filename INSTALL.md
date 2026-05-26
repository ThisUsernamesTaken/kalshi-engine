# AI-Followable Install Guide

This guide installs `kalshi-engine` from scratch on a clean machine. It is
written for another AI agent (or a careful human) to follow step-by-step.
Each step has the exact command to run, the expected output, and a
troubleshooting note for the failure modes that have actually been seen.

Markers used:

- **HUMAN STEP** — cannot be automated (signing into a third party,
  downloading a private key, etc.). Stop and ask the user.
- **VERIFY** — a check the agent must run before proceeding.
- **STOP** — a destructive or irreversible action; ask the user to confirm.

Throughout this guide, the placeholder `<warehouse>` means an absolute
path the user chooses for engine data. On Windows this is typically
`D:\Trading\warehouse`; on POSIX something like `~/.kalshi_engine/warehouse`.

---

## 0. Prerequisites

Check that the host has what it needs before going further.

```bash
python --version              # POSIX
py --version                  # Windows
git --version
```

Expected:
- Python **3.12 or later** (3.12, 3.13, and 3.14 all verified)
- Git **2.30+**

VERIFY: ~1 GB free disk for the warehouse directory.

Troubleshooting:
- *"Python was not found"* on Windows → use the `py` launcher (ships with
  Python from python.org) instead of `python`. If `py` is also missing,
  install Python 3.12+ from <https://www.python.org/downloads/>.
- *Python too old* (<3.12) → install a newer Python; do not try to back-port.

---

## 1. Clone the repo

```bash
git clone https://github.com/ThisUsernamesTaken/kalshi-engine.git
cd kalshi-engine
```

VERIFY: The directory should contain `LICENSE`, `README.md`, `pyproject.toml`,
`src/`, `tests/`, `INSTALL.md`.

```bash
ls
# LICENSE  README.md  INSTALL.md  pyproject.toml  src  tests
```

Troubleshooting:
- *Repo is private and the clone fails with auth* → ask the user for a GitHub
  Personal Access Token or to run `gh auth login` first.

---

## 2. Create + activate a virtualenv

A clean venv is required; do not install into the system Python.

**Windows (PowerShell):**
```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
```

**Windows (Git Bash / MSYS):**
```bash
py -m venv .venv
source .venv/Scripts/activate
```

**POSIX (Linux/macOS):**
```bash
python -m venv .venv
source .venv/bin/activate
```

VERIFY: The shell prompt should now start with `(.venv)` and
`python --version` should match the Python you used to create the venv.

---

## 3. Install the package

```bash
python -m pip install --upgrade pip setuptools wheel
python -m pip install -e ".[dev]"
```

Expected:
- pip upgrade: 2–5 seconds, finishes with `Successfully installed pip-... setuptools-... wheel-...`.
- Editable install: **30–90 seconds** on a fast connection; downloads
  numpy, scipy, pandas, pyarrow, pydantic, cryptography, aiohttp,
  websockets, pytest, ruff, black. Finishes with a long
  `Successfully installed ... kalshi_engine-0.1.0` line.

Troubleshooting:
- *numpy / scipy fail to build wheels* → almost always means no
  pre-built wheel is available for your Python version. Use a Python
  that has wheels (3.12 is the safest). On Linux you may need
  `build-essential` and a Fortran compiler; on macOS Xcode CLT.
- *Network blocked / corporate proxy* → set `HTTPS_PROXY` and
  `HTTP_PROXY` env vars before `pip install`.
- *SSL CERTIFICATE_VERIFY_FAILED on Windows* → install certifi
  (`pip install certifi`) and/or update Python.

---

## 4. Run the test suite (smoke check)

The engine config requires an environment variable even for the test
suite (the tests build paths under it). Point it anywhere writable for
this step — a throwaway directory is fine.

**Windows (PowerShell):**
```powershell
$env:KALSHI_ENGINE_WAREHOUSE = "$HOME\.kalshi_engine\warehouse"
mkdir -Force $env:KALSHI_ENGINE_WAREHOUSE | Out-Null
python -m pytest -q
```

**POSIX:**
```bash
export KALSHI_ENGINE_WAREHOUSE="$HOME/.kalshi_engine/warehouse"
mkdir -p "$KALSHI_ENGINE_WAREHOUSE"
python -m pytest -q
```

Expected (takes 2–4 minutes):
```
........................................................................ [ 30%]
........................................................................ [ 60%]
........................................................................ [ 90%]
......................                                                   [100%]
238 passed, 1 skipped in 186.24s
```

The single skip is `test_calibrate_heston` — it depends on a research
script that is intentionally not shipped with the distribution.

Troubleshooting:
- *Tests fail with `ModuleNotFoundError: kalshi_engine.X`* → editable
  install did not register; re-run step 3 from inside the activated venv.
- *Collection error in `test_calibrate_heston`* → you have an old
  checkout. Run `git pull` and retry.

---

## 5. Get Kalshi API credentials

**HUMAN STEP — cannot be automated.**

1. Sign in at <https://kalshi.com>.
2. Go to Settings → API Keys (or <https://kalshi.com/account/api-keys>).
3. Create a new API key. Kalshi will give you:
   - An API key UUID (string like `a1b2c3d4-...`)
   - A `.pem` private key file to download. Save it somewhere only your
     user can read; this guide assumes `~/.kalshi/kalshi_key.pem`.
4. Note the key UUID — Kalshi will not show it again.

STOP: do not share the API key UUID or the `.pem` file. Treat them like
a password.

---

## 6. Create `credentials.env`

Put your credentials in a file the engine can read. Format is plain
`KEY=value` lines, no quotes needed.

**Windows (PowerShell):**
```powershell
$credsDir = "$HOME\.kalshi"
New-Item -ItemType Directory -Force -Path $credsDir | Out-Null
@"
KALSHI_API_KEY=<your-api-key-uuid>
KALSHI_PRIVATE_KEY_PATH=$credsDir\kalshi_key.pem
"@ | Out-File -Encoding utf8 "$credsDir\credentials.env"
```

**POSIX:**
```bash
mkdir -p ~/.kalshi
cat > ~/.kalshi/credentials.env <<EOF
KALSHI_API_KEY=<your-api-key-uuid>
KALSHI_PRIVATE_KEY_PATH=$HOME/.kalshi/kalshi_key.pem
EOF
chmod 600 ~/.kalshi/credentials.env ~/.kalshi/kalshi_key.pem
```

VERIFY: both files exist and `KALSHI_PRIVATE_KEY_PATH` inside
`credentials.env` is an absolute path to the `.pem`.

---

## 7. Set the engine environment variables

The engine reads two env vars on startup. Set them in every shell that
will run the engine.

**Windows (PowerShell):**
```powershell
$env:KALSHI_ENGINE_WAREHOUSE = "<warehouse>"            # e.g. D:\Trading\warehouse
$env:KALSHI_API_KEY_PATH    = "$HOME\.kalshi\credentials.env"
```

**POSIX:**
```bash
export KALSHI_ENGINE_WAREHOUSE="<warehouse>"
export KALSHI_API_KEY_PATH="$HOME/.kalshi/credentials.env"
```

To make these persistent, add the `export` / `$env:` lines to your
shell profile (`.bashrc`, `.zshrc`, or PowerShell `$PROFILE`).

VERIFY: the warehouse path is writable.

```bash
mkdir -p "$KALSHI_ENGINE_WAREHOUSE/raw/live_logs"
```

---

## 7.5. Install the Phase 4 cutpoints artefact

The model loads gate thresholds from a JSON artefact at
`$KALSHI_ENGINE_WAREHOUSE/models/phase4_cutpoints/<version>/cutpoints.json`.
The repo bundles two versions under `data/cutpoints/`:

- **v1** — original Phase-4 thresholds (used by the 1hr engine, `bin.live_1hr`)
- **v3** — Phase 12.5 Rec 3 recalibration (used by the 15m engine, `bin.live`)

Copy both into the warehouse on a fresh install.

**Windows (PowerShell):**
```powershell
New-Item -ItemType Directory -Force `
  -Path "$env:KALSHI_ENGINE_WAREHOUSE\models\phase4_cutpoints\v1",
        "$env:KALSHI_ENGINE_WAREHOUSE\models\phase4_cutpoints\v3" | Out-Null
Copy-Item data\cutpoints\v1\cutpoints.json `
  "$env:KALSHI_ENGINE_WAREHOUSE\models\phase4_cutpoints\v1\cutpoints.json"
Copy-Item data\cutpoints\v3\cutpoints.json `
  "$env:KALSHI_ENGINE_WAREHOUSE\models\phase4_cutpoints\v3\cutpoints.json"
```

**POSIX:**
```bash
mkdir -p "$KALSHI_ENGINE_WAREHOUSE/models/phase4_cutpoints/"{v1,v3}
cp data/cutpoints/v1/cutpoints.json \
  "$KALSHI_ENGINE_WAREHOUSE/models/phase4_cutpoints/v1/cutpoints.json"
cp data/cutpoints/v3/cutpoints.json \
  "$KALSHI_ENGINE_WAREHOUSE/models/phase4_cutpoints/v3/cutpoints.json"
```

VERIFY:
```bash
python -c "import json,os; p=os.environ['KALSHI_ENGINE_WAREHOUSE']+'/models/phase4_cutpoints/v1/cutpoints.json'; print('OK', json.load(open(p))['version'])"
# Should print: OK phase4_v1
```

Troubleshooting:
- *`FileNotFoundError: cutpoints.json`* on engine boot — re-run the copy
  commands above. The engine looks for `<version>/cutpoints.json` under
  the warehouse, NOT the repo's `data/cutpoints/`.

---

## 8. Verify config loads (no orders, no network)

This step boots only the config module to confirm env vars resolve.

```bash
python -c "from kalshi_engine.config import WAREHOUSE_ROOT, RAW_DIR; print('OK', WAREHOUSE_ROOT, RAW_DIR)"
```

Expected:
```
OK <warehouse> <warehouse>/raw
```

Troubleshooting:
- *`kalshi_engine.config.ConfigError: Missing required environment variable
  'KALSHI_ENGINE_WAREHOUSE'`* → step 7 did not export the var in this shell.
  Repeat step 7.
- *Path with spaces fails on Windows* → wrap the path in double quotes
  when setting `$env:KALSHI_ENGINE_WAREHOUSE`.

---

## 9. Dry-run (paper mode, no real orders)

The dry-run boots the full engine, subscribes to the Kalshi WebSocket,
warms up spot data, and prints decisions to the log — but no orders are
placed.

```bash
python -m kalshi_engine.bin.live --dry-run --duration-s 60
```

Expected (interleaved on stderr):
```
[diag] amain entered
[diag] creds loaded; pem=<N>B key_id_len=36
[diag] entering KalshiClient context
[diag] KalshiClient ready; constructing LiveExecution
[diag] discovery start; cryptos=['BTC', 'ETH', 'SOL', 'XRP', 'DOGE']
[diag] discovery done; markets=<N>
[diag] boot reconcile from /portfolio/positions ...
[diag] boot reconcile done; local positions=<N>
[diag] draining spot warmup into strategy + risk_state ...
[diag] warmup drained; <N> spot events
[diag] writing boot event
[diag] constructing kalshi_ws + entering run_loop
```

After ~60 s it returns to the prompt cleanly.

A single `{"kind":"boot",...}` line is appended to
`<warehouse>/raw/live_logs/live_favorite_chase_v2.jsonl` (or the
`--log-path` you specify).

Troubleshooting:
- *`ERROR: KALSHI_API_KEY_PATH ... file is missing`* → step 6 did not
  produce a readable file; re-run step 6.
- *`ERROR: bad credentials`* → the API key UUID is wrong, or the `.pem`
  path inside `credentials.env` is wrong, or the `.pem` has been
  corrupted (must start with `-----BEGIN RSA PRIVATE KEY-----`).
- *Hangs at `discovery start`* → outbound HTTPS to
  `https://trading-api.kalshi.com` is blocked. Test with `curl
  https://trading-api.kalshi.com/trade-api/v2/exchange/status`.
- *Hangs at `constructing kalshi_ws`* → outbound WSS to
  `wss://api.elections.kalshi.com/trade-api/ws/v2` is blocked. Some
  corporate firewalls block WebSockets entirely.

---

## 10. Run live (REAL ORDERS)

STOP — this places real orders against your Kalshi account and risks
real money. Confirm the user wants to proceed before running this step.

```bash
python -m kalshi_engine.bin.live \
    --strategy favorite_chase \
    --model phase4_cutpoints \
    --cutpoints-version v1 \
    --align-mode 5tier_v13b_h1h4_loose \
    --max-contracts 10 \
    --reentry-mode disabled \
    --time-of-day-skip enabled \
    --cryptos BTC,ETH,SOL,XRP,DOGE \
    --spot-source bitstamp \
    --stop-mode none \
    --bps-gate enabled \
    --daily-cap-cents 1000
```

Note on `--align-mode 5tier_v13b_h1h4_loose` (Phase 13.4): H1+H4 mix on
the V13b score formula with a targeted relaxation in the [3.0, 4.0)
band. For score >= 4.0, sizing is identical to `5tier_v13b_h1h4`:
SKIPs score < 4 (the only cohort-losing tier was score 3.5 pre-relax),
then sizes by `min(10, round(score * 1.8))` → 4.0→7ct, 4.5→8ct,
5.0→9ct, 5.5+→10ct. The loose extension ENTERs at 3ct in the [3.0, 4.0)
band IFF `bb_div_band=1` (validated bb_div sweet-spot) AND
`vol_pct < 0.5` (calm vol). Motivated by 13/13 historical wins in
score 2.5-3.5 all sharing those two flags. The earlier `5tier_v13b_h1h4`
(no loosening), `5tier_v13b_s2` (3/5/8/10 by tier, includes score 3-4
trades), and `5tier_v13b` (caps at 5 ct) remain selectable as
backward-compat options.

Note on `--time-of-day-skip enabled`: an experimental removal of the
TOD-skip gate (briefly run as `--time-of-day-skip disabled`) was
reverted after a single previously-blocked cycle produced a -$3.40
loss. The gate is doing real protective work; leave it enabled.

The Phase 4 cutpoints artefact is bundled in the repo at
`data/cutpoints/{v1,v3}/cutpoints.json`. Section 7.5 above shows how
to copy it into the warehouse. If the file is missing the engine will
crash with `FileNotFoundError` on boot — re-run that step.

Expected:
- Process stays running (no exit).
- A single `{"kind":"boot",...}` line followed by per-cycle
  `{"kind":"decision",...}` and (when entries fire) `{"kind":"order_intent"}`
  + `{"kind":"order_filled"}` lines in the JSONL log.

### 10b. 1-hour live engine (current production T6 config)

The 1hr engine trades the KX{C}D digital markets (BTC + ETH only — the
other 1hr crypto series have insufficient book depth at extreme strikes
for reliable 7-10ct fills). Runs alongside the 15m engine, separate
process, separate $10/day risk envelope, separate log file.

STOP — real orders. Worst single-trade loss is ~$9.20 (10ct * 92c).
Daily cap binds after one max-tier loss.

```bash
python -m kalshi_engine.bin.live_1hr \
    --strategy favorite_chase \
    --model phase4_cutpoints \
    --cutpoints-version v1 \
    --align-mode 5tier_v13b_7_10_10 \
    --max-contracts 10 \
    --reentry-mode disabled \
    --trigger-minutes 15,30,50 \
    --skip-hours 13 \
    --max-favorite-cost-decicents 920 \
    --cryptos BTC,ETH \
    --spot-source bitstamp \
    --stop-mode none \
    --bps-gate enabled \
    --daily-cap-cents 1000
```

Notes:
- `--align-mode 5tier_v13b_7_10_10` (T6): same V13b score formula as
  the 15m engine, but the sizing schedule is `skip<4, 7ct@score[4,5),
  10ct@score[5,6), 10ct@score>=6`. From the 1hr observer tier sweep:
  95% of T3's PnL with a tighter score-4 tail (-$4.90 vs -$7.00 worst
  trade).
- `--trigger-minutes 15,30,50`: 1hr cycles get evaluated at T+15, T+30,
  and T+50 (mins into the hour). The T+15 scan catches favorites
  before they pin near 99¢ — observer data showed 95%+ of T+50
  favorites already sat at 99¢ where Kalshi fees eat the edge. T+45
  is intentionally absent (was -$5.79 in the sweep).
- `--skip-hours 13`: UTC 13Z blocked (37.5% WR / -$84.17 in the sweep).
- `--max-favorite-cost-decicents 920`: hard cap on entry-side cost at
  92¢ — protects against the Kalshi fee trap that eats edge above 95¢.
  In live this filters ~98% of decisions because 1hr favorites tend to
  pin near 99¢ before T+50.

All other flags are 15m-shared and behave identically.

---

## 11. Monitor

Tail the live log:

**Windows (PowerShell):**
```powershell
Get-Content "$env:KALSHI_ENGINE_WAREHOUSE\raw\live_logs\live_favorite_chase_v2.jsonl" -Wait -Tail 20
```

**POSIX:**
```bash
tail -F "$KALSHI_ENGINE_WAREHOUSE/raw/live_logs/live_favorite_chase_v2.jsonl"
```

Watch for:

| Event kind        | Means                                                       |
|-------------------|-------------------------------------------------------------|
| `boot`            | Engine started, config dumped, ready                        |
| `decision`        | Strategy evaluated a cycle; `action` is `enter` or `skip`   |
| `order_intent`    | About to send an order                                      |
| `order_filled`    | Kalshi confirmed the fill                                   |
| `order_rejected`  | Order denied (book moved, risk envelope tripped, etc.)      |
| `stop_triggered`  | Price stop fired (only relevant with `--stop-mode price`)   |
| `settlement`      | Cycle resolved (YES/NO + payout)                            |
| `shutdown`        | Engine exited cleanly                                       |
| `fatal` / `error` | Something broke — read the message                          |

Health rule of thumb: at least one `decision` line should land every
~15 minutes once the engine is past the warm-up window. Silence beyond
that means the WebSocket dropped — restart.

---

## 12. Stop safely

The engine is a single Python process. Send SIGINT (Ctrl-C) in its
console, or kill the PID.

**Windows (PowerShell):**
```powershell
# Find the PID
Get-Process python | Where-Object { $_.CommandLine -match "kalshi_engine.bin.live" } | Select-Object Id, StartTime

# Stop it
Stop-Process -Id <pid>
```

**POSIX:**
```bash
pkill -INT -f "kalshi_engine.bin.live"
```

STOP — before stopping, confirm there are no open positions you wanted
the engine to manage. The engine does not flatten positions on exit.
Check the Kalshi web UI or the `settlement` lines in the log.

The engine writes a final `{"kind":"shutdown",...}` line on graceful
exit. If you `Stop-Process -Force` (Windows) or `kill -9` (POSIX), that
line will be missing — restart will reconcile from the Kalshi
`/portfolio/positions` endpoint on the next boot, so this is recoverable
but noisy.

---

## Appendix A — Backtest

If you have a Kalshi book-event SQLite (a "burn-in DB") for the date
range you want to test, you can replay it through the same strategy
code:

```bash
python -m kalshi_engine.bin.backtest \
    --from 2026-05-18 --to 2026-05-19 \
    --cryptos BTC \
    --burnin-db /path/to/your/burnin.sqlite
```

The burn-in DB schema is documented in
`src/kalshi_engine/warehouse/adapters.py::BurninReader`. The DB is not
shipped with the package.

## Appendix B — 1-hour observer (read-only)

```bash
python -m kalshi_engine.bin.observe_1hr \
    --cryptos BTC,ETH,SOL,XRP,DOGE \
    --observe-times 30,40,45,50,55
```

Subscribes to the 1-hour digital markets; emits pre-trigger book
envelopes; places no orders. Runs as a separate process from the live
engine and is safe to run concurrently.
