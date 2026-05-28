<#
.SYNOPSIS
    Start the 5 Kalshi engine NSSM services in safe sequence.

.DESCRIPTION
    Boot order matters for first-time deploy:
      1. Observers first (read-only, no order risk) - give them ~30s to warm up.
      2. INXU shim (smallest order risk: 1ct, $5 daily cap).
      3. 15m engine.
      4. 1hr engine.

    Each service is started, then we wait briefly + verify it's running before
    moving on. If any service fails to start, halt and surface.

.PARAMETER WaitBetweenSeconds
    Seconds to wait between starts (default 15).

.PARAMETER SkipConfirm
    If set, don't prompt for "are you sure?" before starting real-money engines.
#>
[CmdletBinding()]
param(
    [int]$WaitBetweenSeconds = 15,
    [switch]$SkipConfirm
)

$ErrorActionPreference = "Stop"

# Boot order: low-risk  ->  high-risk
$StartOrder = @(
    "KalshiObserver1hr",
    "KalshiObserverInxu",
    "KalshiEngineInxu",       # 1ct / $5cap - smallest real-money exposure
    "KalshiEngineCommodity",  # 1ct / $5cap per product / $10 total (GOLD only)
    "KalshiEngine15m",        # 10ct / $10cap
    "KalshiEngine1hr"         # up to 10ct / $10cap (or override)
)

$isAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole] "Administrator")
if (-not $isAdmin) {
    Write-Error "Service start requires Administrator. Re-run from elevated PowerShell."
    exit 1
}

if (-not $SkipConfirm) {
    Write-Host "About to start 6 services in this order:"
    $StartOrder | ForEach-Object { Write-Host "  $_" }
    Write-Host ""
    Write-Host "4 of these (Engine15m, Engine1hr, EngineInxu, EngineCommodity) place REAL MONEY orders."
    $reply = Read-Host "Continue? (y/N)"
    if ($reply -notmatch '^[yY]') {
        Write-Host "Aborted."
        exit 0
    }
}

Write-Host ""
foreach ($name in $StartOrder) {
    $svc = Get-Service -Name $name -ErrorAction SilentlyContinue
    if (-not $svc) {
        Write-Error "  $name : not installed. Run install_services.ps1 first."
        exit 1
    }
    if ($svc.Status -eq "Running") {
        Write-Host "  $name : already running, skipping" -ForegroundColor DarkGray
        continue
    }

    Write-Host "  $name : starting..." -ForegroundColor Cyan
    Start-Service -Name $name
    Start-Sleep -Seconds 3
    $svc = Get-Service -Name $name
    if ($svc.Status -ne "Running") {
        Write-Error "  $name : failed to start (status: $($svc.Status))"
        Write-Host "  Check D:\Trading\warehouse\raw\live_logs\service_$name.stderr.log"
        exit 1
    }
    Write-Host "  $name : RUNNING" -ForegroundColor Green

    if ($name -ne $StartOrder[-1]) {
        Write-Host "    waiting $WaitBetweenSeconds s before next..." -ForegroundColor DarkGray
        Start-Sleep -Seconds $WaitBetweenSeconds
    }
}

Write-Host ""
Write-Host "===================================================="
Write-Host " All 6 services started."
Write-Host "===================================================="
Write-Host ""
Write-Host "Verify with:"
Write-Host "  Get-Service Kalshi*"
Write-Host "  Get-Content D:\Trading\warehouse\raw\live_logs\live_favorite_chase_v2_kalshi_engine.jsonl -Tail 5"
Write-Host "  Get-Content D:\Trading\warehouse\raw\live_logs\live_hourglass_trader.jsonl -Tail 5"
