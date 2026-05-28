<#
.SYNOPSIS
    Uninstall the 6 Kalshi engine NSSM services created by install_services.ps1.

.DESCRIPTION
    Stops then removes:
      KalshiEngine15m, KalshiEngine1hr, KalshiObserver1hr,
      KalshiEngineInxu, KalshiObserverInxu, KalshiEngineCommodity.

    KalshiCapture is NOT touched - that's a pre-existing service.

    Stops services gracefully (NSSM AppKillProcessTree honored), then removes
    the service registration. Log files in D:\Trading\warehouse\raw\live_logs\
    are left in place.

.PARAMETER DryRun
    If set, prints what WOULD be removed without executing.
#>
[CmdletBinding()]
param(
    [switch]$DryRun
)

$ErrorActionPreference = "Continue"

$NssmExe = "C:\Users\coleb\AppData\Local\Microsoft\WinGet\Packages\NSSM.NSSM_Microsoft.Winget.Source_8wekyb3d8bbwe\nssm-2.24-101-g897c7ad\win64\nssm.exe"

$Services = @(
    "KalshiEngine15m",
    "KalshiEngine1hr",
    "KalshiObserver1hr",
    "KalshiEngineInxu",
    "KalshiObserverInxu",
    "KalshiEngineCommodity"
)

$isAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole] "Administrator")
if (-not $isAdmin -and -not $DryRun) {
    Write-Error "Service uninstall requires Administrator. Re-run from elevated PowerShell."
    exit 1
}

Write-Host "===================================================="
Write-Host " Kalshi engine NSSM service UNINSTALL"
Write-Host "===================================================="

foreach ($name in $Services) {
    $svc = Get-Service -Name $name -ErrorAction SilentlyContinue
    if (-not $svc) {
        Write-Host "  $name : not installed, skipping" -ForegroundColor DarkGray
        continue
    }

    if ($DryRun) {
        Write-Host "  $name : [dry-run] would stop + remove (current status: $($svc.Status))" -ForegroundColor Yellow
        continue
    }

    if ($svc.Status -eq "Running") {
        Write-Host "  $name : stopping..." -ForegroundColor Yellow
        Stop-Service -Name $name -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 2
    }

    Write-Host "  $name : removing service registration..." -ForegroundColor Yellow
    & $NssmExe remove $name confirm | Out-Null
    if ($LASTEXITCODE -eq 0) {
        Write-Host "  $name : REMOVED" -ForegroundColor Green
    } else {
        Write-Host "  $name : remove failed (exit $LASTEXITCODE)" -ForegroundColor Red
    }
}

Write-Host ""
Write-Host "===================================================="
Write-Host " Uninstall complete."
Write-Host "===================================================="
Write-Host ""
Write-Host "KalshiCapture was NOT touched (pre-existing service)."
Write-Host "Log files retained in D:\Trading\warehouse\raw\live_logs\"
