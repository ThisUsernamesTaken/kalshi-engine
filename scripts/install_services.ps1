<#
.SYNOPSIS
    Install the 5 Kalshi engine processes as Windows services via NSSM.

.DESCRIPTION
    Creates these services (LocalSystem, auto-start, auto-restart on exit):
      - KalshiEngine15m     - 15m favorite-chase live engine (real money)
      - KalshiEngine1hr     - 1hr live engine, Phase 14.9 BTC d_norm gate (real money)
      - KalshiObserver1hr   - 1hr crypto observer (read-only)
      - KalshiEngineInxu    - KXINXU equity-index shim (real money, 1ct/$5cap)
      - KalshiObserverInxu  - KXINXU equity-index observer (read-only)
      - KalshiEngineCommodity - KXGOLDD commodity daily-ladder engine (real money, 1ct/$5cap)

    KalshiCapture is NOT touched - it's already an NSSM service.

    Services are created in stopped state. After installation, review with
      Get-Service Kalshi*
    Then start them manually OR run
      .\start_services.ps1
    once user confirms the install package is correct.

.PARAMETER Force
    If set, removes any existing services with these names before re-installing.

.PARAMETER DryRun
    If set, prints the nssm commands that WOULD be run but doesn't execute them.

.EXAMPLE
    .\install_services.ps1 -DryRun
    .\install_services.ps1
    .\install_services.ps1 -Force
#>
[CmdletBinding()]
param(
    [switch]$Force,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

# ----------------------- Paths + binaries -----------------------

$NssmExe = "C:\Users\coleb\AppData\Local\Microsoft\WinGet\Packages\NSSM.NSSM_Microsoft.Winget.Source_8wekyb3d8bbwe\nssm-2.24-101-g897c7ad\win64\nssm.exe"
$PythonExe = "C:\Users\coleb\AppData\Local\Python\bin\python.exe"
$EngineRoot = "C:\Trading\engine"
$EngineSrc = "C:\Trading\engine\src"
$Warehouse = "D:\Trading\warehouse"
$LogDir = "D:\Trading\warehouse\raw\live_logs"
$KalshiCreds = "C:\Trading\kalshi_btc_gradient_engine\credentials\kalshi.env"
$AlpacaCreds = "C:\Trading\.alpaca\credentials.env"

# Sanity checks
foreach ($path in @($NssmExe, $PythonExe, $EngineRoot, $EngineSrc, $Warehouse, $LogDir, $KalshiCreds)) {
    if (-not (Test-Path $path)) {
        Write-Error "Missing required path: $path"
        exit 1
    }
}
if (-not (Test-Path $AlpacaCreds)) {
    Write-Warning "Alpaca creds not found at $AlpacaCreds - KalshiEngineInxu + KalshiObserverInxu may fail to boot."
}

# ----------------------- Common env block -----------------------

# IMPORTANT: AppEnvironmentExtra must be set with each KEY=VALUE as a
# SEPARATE positional argument to nssm. A space-joined single-string
# variant gets parsed as one mangled env var (the first KEY consumes the
# rest of the line as its value). PYTHONUNBUFFERED=1 forces stdout/stderr
# to flush promptly so service log files receive output in real time.
$CommonEnv = @(
    "PYTHONPATH=$EngineSrc",
    "PYTHONUNBUFFERED=1",
    "KALSHI_API_KEY_PATH=$KalshiCreds",
    "KALSHI_ENGINE_WAREHOUSE=$Warehouse"
)

$EnvWithAlpaca = @(
    "PYTHONPATH=$EngineSrc",
    "PYTHONUNBUFFERED=1",
    "KALSHI_API_KEY_PATH=$KalshiCreds",
    "KALSHI_ENGINE_WAREHOUSE=$Warehouse",
    "ALPACA_CREDENTIALS_PATH=$AlpacaCreds"
)

# ----------------------- Service definitions -----------------------

# IMPORTANT: --daily-cap-cents below uses the PRODUCTION DEFAULTS:
#   15m engine: $10/day (1000 cents)
#   1hr engine: $10/day (1000 cents) - was temporarily overridden to $100 on
#     2026-05-26 for data collection; that override expired at UTC midnight.
#     Edit this script + reinstall if a different cap is desired.
# Phase 14.10 vol_pct cap 0.80 is picked up from cutpoints/v1/cutpoints.json
# automatically; no flag change needed.

$Services = @(
    @{
        Name = "KalshiEngine15m"
        DisplayName = "Kalshi Engine - 15m favorite-chase"
        Description = "Live 15m favorite-chase engine (real money). 5tier_v13b_h1h4_loose align mode, v1 cutpoints, Phase 14.10 vol_pct cap 0.80. Daily cap `$10."
        Module = "kalshi_engine.bin.live"
        Args = @(
            "--strategy", "favorite_chase",
            "--model", "phase4_cutpoints",
            "--cutpoints-version", "v1",
            "--align-mode", "5tier_v13b_h1h4_loose",
            "--max-contracts", "12",
            "--reentry-mode", "disabled",
            "--time-of-day-skip", "enabled",
            "--cryptos", "BTC,ETH,SOL,XRP,DOGE",
            "--spot-source", "bitstamp",
            "--stop-mode", "none",
            "--bps-gate", "enabled",
            "--daily-cap-cents", "1000",
            "--pre-trigger-observation", "enabled",
            "--log-path", "$LogDir\live_favorite_chase_v2_kalshi_engine.jsonl"
        )
        Env = $CommonEnv
    }
    @{
        Name = "KalshiEngine1hr"
        DisplayName = "Kalshi Engine - 1hr (Phase 14.9 BTC d_norm + 14.12 ladder)"
        Description = "Live 1hr engine (real money). BTC d_norm close-strike gate, ETH 1to3 ramp, T+15/30/40/55 triggers. Daily cap `$10. Phase 14.12 BTC ladder companion at T+30 (3 rungs, d_norm>=1.5, 3ct each, `$5/day cap)."
        Module = "kalshi_engine.bin.live_1hr"
        Args = @(
            "--strategy", "favorite_chase",
            "--model", "phase4_cutpoints",
            "--cryptos", "BTC,ETH",
            "--cutpoints-version", "v1",
            "--align-mode", "5tier_v13b_7_10_10",
            "--per-crypto-align-mode", "BTC=5tier_v13b_btc_dnorm_gate,ETH=5tier_v13b_1to3_ramp",
            "--per-crypto-max-contracts", "BTC=10,ETH=3",
            "--max-contracts", "10",
            "--max-favorite-cost-decicents", "920",
            "--daily-cap-cents", "1000",
            "--trigger-minutes", "15,30,40,55",
            "--skip-hours", "13",
            "--spot-source", "bitstamp",
            "--min-entry-d-norm", "1.5",
            "--near-strike-allowed-minute", "55",
            "--stop-mode", "none",
            "--shadow-stop-audit", "enabled",
            "--shadow-stop-bid-decicents", "650",
            "--shadow-stop-min-age-sec", "60",
            "--bps-gate", "enabled",
            # Phase 14.12 ladder (BTC only, 3 rungs, $5/day)
            "--ladder-enabled", "true",
            "--ladder-max-rungs", "3",
            "--ladder-d-norm-min", "1.5",
            "--ladder-rung-size", "3",
            "--ladder-min-bid-size", "3",
            "--ladder-fav-min-dc", "750",
            "--ladder-fav-max-dc", "950",
            "--ladder-daily-cap-cents", "500",
            "--ladder-cryptos", "BTC",
            "--ladder-trigger-minute", "30",
            # Conservative live forward test: early-cycle deep ITM sweeper
            # uses its own $3/day cap and 1ct rungs. It attaches the selected
            # ask as the order limit so the 99c IOC default cannot overpay.
            "--deep-itm-enabled", "true",
            "--deep-itm-trigger-minutes", "5,10",
            "--deep-itm-skip-trigger-minutes", "20,25",
            "--deep-itm-cryptos", "BTC,ETH",
            "--deep-itm-max-rungs", "2",
            "--deep-itm-rung-size", "1",
            "--deep-itm-min-d-norm", "3.0",
            "--deep-itm-min-ask-dc", "900",
            "--deep-itm-max-ask-dc", "970",
            "--deep-itm-min-bid-size", "5",
            "--deep-itm-daily-cap-cents", "300",
            "--log-path", "$LogDir\live_hourglass_trader.jsonl"
        )
        Env = $CommonEnv
    }
    @{
        Name = "KalshiObserver1hr"
        DisplayName = "Kalshi Observer - 1hr crypto"
        Description = "Read-only 1hr observer (BTC,ETH,SOL,XRP,DOGE). Captures pretrigger envelopes with SR+liquidity features. Phase 14.8 cycle-duration filter active."
        Module = "kalshi_engine.bin.observe_1hr"
        Args = @(
            "--cryptos", "BTC,ETH,SOL,XRP,DOGE",
            "--spot-source", "bitstamp",
            "--log-path", "$LogDir\hourglass_observer.jsonl"
        )
        Env = $CommonEnv
    }
    @{
        Name = "KalshiEngineInxu"
        DisplayName = "Kalshi Engine - KXINXU SPX shim"
        Description = "Live KXINXU equity-index shim (real money, 1ct/`$5cap). Crypto-calibrated cutpoints - `$5 daily cap protects against catastrophic loss until equity recalibration."
        Module = "kalshi_engine.bin.live_inxu_v0"
        Args = @(
            "--log-path", "$LogDir\live_inxu_v0.jsonl"
        )
        Env = $EnvWithAlpaca
    }
    @{
        Name = "KalshiObserverInxu"
        DisplayName = "Kalshi Observer - KXINXU SPX"
        Description = "Read-only KXINXU SPX observer (Alpaca SPY spot polling). Captures intra-cycle envelopes T+5..T+55 per Phase 14.4."
        Module = "kalshi_engine.bin.observe_inxu"
        Args = @(
            "--equities", "SPX",
            "--log-path", "$LogDir\inxu_observer.jsonl"
        )
        Env = $EnvWithAlpaca
    }
    @{
        Name = "KalshiEngineCommodity"
        DisplayName = "Kalshi Engine - commodity daily ladder (KXGOLDD)"
        Description = "Live commodity daily-ladder engine (real money, 1ct/`$5/day per product, `$10/day total). Phase 14.16. GOLD only at launch (Pyth Metal.XAU/USD = exact settlement source). BRENT data-blocked (BRENTQ6 dead on Pyth) - framework-ready, disabled. Pyth is keyless so no extra creds. Daily-window controller: chase 60-10 min before 5pm ET settle."
        Module = "kalshi_engine.bin.live_commodity"
        Args = @(
            "--commodities", "GOLD",
            "--align-mode", "5tier_v13b_commodity_1ct_flat",
            "--max-contracts", "1",
            "--daily-cap-cents", "500",
            "--total-daily-cap-cents", "1000",
            "--cutpoints-version", "commodity_v1",
            "--window-open-minutes", "60",
            "--window-close-minutes", "10",
            "--observe-times", "60,45,30,20,15",
            "--time-of-day-skip", "disabled",
            "--log-path", "$LogDir\live_commodity_kalshi_engine.jsonl"
        )
        Env = $CommonEnv
    }
)

# ----------------------- Install helper -----------------------

function Invoke-Nssm {
    param([Parameter(ValueFromRemainingArguments)][string[]]$NssmArgs)
    if ($DryRun) {
        $argStr = ($NssmArgs | ForEach-Object { if ($_ -match '\s') { "`"$_`"" } else { $_ } }) -join ' '
        Write-Host "  [dry-run] nssm $argStr" -ForegroundColor DarkGray
        return
    }
    & $NssmExe @NssmArgs
    if ($LASTEXITCODE -ne 0) {
        throw "nssm $($NssmArgs[0]) failed with exit $LASTEXITCODE"
    }
}

function Test-ServiceExists {
    param([string]$Name)
    $svc = Get-Service -Name $Name -ErrorAction SilentlyContinue
    return [bool]$svc
}

function Install-EngineService {
    param([hashtable]$Svc)
    $name = $Svc.Name
    Write-Host ""
    Write-Host "-- $name --" -ForegroundColor Cyan
    Write-Host "  Display: $($Svc.DisplayName)"
    Write-Host "  Module:  $($Svc.Module)"

    if (Test-ServiceExists $name) {
        if ($Force) {
            Write-Host "  EXISTS; removing first (Force)" -ForegroundColor Yellow
            if (-not $DryRun) {
                Stop-Service -Name $name -Force -ErrorAction SilentlyContinue
                & $NssmExe remove $name confirm | Out-Null
                Start-Sleep -Seconds 1
            }
        } else {
            Write-Host "  EXISTS; skipping (use -Force to recreate)" -ForegroundColor Yellow
            return
        }
    }

    # Build full args: -u -m <module> <module_args>
    $pythonArgs = @("-u", "-m", $Svc.Module) + $Svc.Args
    $argString = ($pythonArgs | ForEach-Object {
        if ($_ -match '\s') { "`"$_`"" } else { $_ }
    }) -join ' '

    Invoke-Nssm install $name $PythonExe $argString

    # Working directory (engine root for path resolution)
    Invoke-Nssm set $name AppDirectory $EngineRoot

    # Display name + description
    Invoke-Nssm set $name DisplayName $Svc.DisplayName
    Invoke-Nssm set $name Description $Svc.Description

    # Environment extras - each KEY=VALUE as a SEPARATE arg to nssm
    # (space-joined single string gets parsed as one mangled env var).
    # Build the command line as an array so we can splat to & $NssmExe.
    $envCmd = @("set", $name, "AppEnvironmentExtra") + $Svc.Env
    if ($DryRun) {
        $envStr = ($envCmd | ForEach-Object {
            if ($_ -match '\s') { "`"$_`"" } else { $_ }
        }) -join ' '
        Write-Host "  [dry-run] nssm $envStr" -ForegroundColor DarkGray
    } else {
        & $NssmExe @envCmd
        if ($LASTEXITCODE -ne 0) {
            throw "nssm set AppEnvironmentExtra failed with exit $LASTEXITCODE"
        }
    }

    # Auto-start (Windows service start type)
    Invoke-Nssm set $name Start SERVICE_AUTO_START

    # Stdout / stderr log files (rotated, dated by NSSM)
    $stdout = "$LogDir\service_$name.stdout.log"
    $stderr = "$LogDir\service_$name.stderr.log"
    Invoke-Nssm set $name AppStdout $stdout
    Invoke-Nssm set $name AppStderr $stderr

    # Log rotation: rotate at 100MB
    Invoke-Nssm set $name AppRotateFiles 1
    Invoke-Nssm set $name AppRotateOnline 1
    Invoke-Nssm set $name AppRotateBytes 104857600
    Invoke-Nssm set $name AppRotateSeconds 0

    # Restart policy: always restart on exit, 5s delay, 10s throttle
    # (throttle = minimum time between restart attempts; prevents hot-loop on persistent failure)
    Invoke-Nssm set $name AppExit Default Restart
    Invoke-Nssm set $name AppRestartDelay 5000
    Invoke-Nssm set $name AppThrottle 10000

    # Don't kill process tree - let the engine handle its own children (asyncio tasks etc.)
    Invoke-Nssm set $name AppKillProcessTree 1

    # Service account - LocalSystem (matches KalshiCapture; has access to C:\Trading and D:\Trading)
    Invoke-Nssm set $name ObjectName LocalSystem

    Write-Host "  OK (stopped state; review then start manually or run start_services.ps1)" -ForegroundColor Green
}

# ----------------------- Main -----------------------

Write-Host "===================================================="
Write-Host " Kalshi engine NSSM service install"
Write-Host "===================================================="
Write-Host "  NSSM:       $NssmExe"
Write-Host "  Python:     $PythonExe"
Write-Host "  Engine src: $EngineSrc"
Write-Host "  Warehouse:  $Warehouse"
Write-Host "  DryRun:     $DryRun"
Write-Host "  Force:      $Force"
Write-Host ""

# Require admin (NSSM service install needs it)
$isAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole] "Administrator")
if (-not $isAdmin -and -not $DryRun) {
    Write-Error "Service install requires Administrator. Re-run from elevated PowerShell."
    exit 1
}

foreach ($svc in $Services) {
    Install-EngineService $svc
}

Write-Host ""
Write-Host "===================================================="
Write-Host " Done. Services created in STOPPED state."
Write-Host "===================================================="
Write-Host ""
Write-Host "Next steps:"
Write-Host "  1. Review services:    Get-Service Kalshi*"
Write-Host "  2. Inspect one:        sc.exe qc KalshiEngine15m"
Write-Host "  3. Start them:         .\start_services.ps1   (or manually one-by-one)"
Write-Host "  4. Tail engine logs to verify clean boot."
Write-Host ""
Write-Host "To undo: .\uninstall_services.ps1"
