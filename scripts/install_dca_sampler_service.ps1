<#
.SYNOPSIS
    Install the Phase 14.18 DCA dense book sampler as a Windows service (NSSM).

.DESCRIPTION
    Creates ONE service, mirroring the LocalSystem / auto-start / auto-restart
    pattern of install_services.ps1 (the 6 existing engine/observer services):

      - KalshiObserverDcaSample - read-only DCA dense book sampler (log-only)

    This is a SIDECAR. It opens read-only Kalshi WS subscriptions + a Bitstamp
    spot poll and logs to its OWN file (dca_book_sample.jsonl). It places no
    orders and does not touch the live 1hr trader (KalshiEngine1hr) or any other
    service. Kept as a standalone install script so this commit adds NEW files
    only; fold the entry into install_services.ps1 on the next service refresh.

.PARAMETER Force
    Remove an existing KalshiObserverDcaSample service before re-installing.

.PARAMETER DryRun
    Print the nssm commands that WOULD run without executing them.

.EXAMPLE
    .\install_dca_sampler_service.ps1 -DryRun
    .\install_dca_sampler_service.ps1
    .\install_dca_sampler_service.ps1 -Force
#>
[CmdletBinding()]
param(
    [switch]$Force,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

# ----------------------- Paths + binaries (match install_services.ps1) -----
$NssmExe = "C:\Users\coleb\AppData\Local\Microsoft\WinGet\Packages\NSSM.NSSM_Microsoft.Winget.Source_8wekyb3d8bbwe\nssm-2.24-101-g897c7ad\win64\nssm.exe"
$PythonExe = "C:\Users\coleb\AppData\Local\Python\bin\python.exe"
$EngineRoot = "C:\Trading\engine"
$EngineSrc = "C:\Trading\engine\src"
$Warehouse = "D:\Trading\warehouse"
$LogDir = "D:\Trading\warehouse\raw\live_logs"
$KalshiCreds = "C:\Trading\kalshi_btc_gradient_engine\credentials\kalshi.env"

foreach ($path in @($NssmExe, $PythonExe, $EngineRoot, $EngineSrc, $Warehouse, $LogDir, $KalshiCreds)) {
    if (-not (Test-Path $path)) {
        Write-Error "Missing required path: $path"
        exit 1
    }
}

# Same env block as the 1hr engine (no Alpaca needed — crypto only).
$CommonEnv = @(
    "PYTHONPATH=$EngineSrc",
    "PYTHONUNBUFFERED=1",
    "KALSHI_API_KEY_PATH=$KalshiCreds",
    "KALSHI_ENGINE_WAREHOUSE=$Warehouse"
)

$Svc = @{
    Name = "KalshiObserverDcaSample"
    DisplayName = "Kalshi Observer - DCA dense book sampler"
    Description = "Read-only Phase 14.18 DCA dense book sampler (BTC,ETH 1hr). Samples favorite/near-favorite markets every ~7s; logs favorite_mid + book depth + V13B score components + sec_into_cycle to dca_book_sample.jsonl. No orders. Does not touch KalshiEngine1hr."
    Module = "kalshi_engine.bin.observe_dca_sample"
    Args = @(
        "--cryptos", "BTC,ETH",
        "--sample-interval-s", "7",
        "--min-favorite-mid-dc", "600",
        "--max-favorite-mid-dc", "970",
        "--cutpoints-version", "v1",
        "--spot-source", "bitstamp",
        "--log-path", "$LogDir\dca_book_sample.jsonl"
    )
    Env = $CommonEnv
}

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
    return [bool](Get-Service -Name $Name -ErrorAction SilentlyContinue)
}

$isAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole] "Administrator")
if (-not $isAdmin -and -not $DryRun) {
    Write-Error "Service install requires Administrator. Re-run from elevated PowerShell."
    exit 1
}

$name = $Svc.Name
Write-Host "-- $name --" -ForegroundColor Cyan

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
        exit 0
    }
}

$pythonArgs = @("-u", "-m", $Svc.Module) + $Svc.Args
$argString = ($pythonArgs | ForEach-Object { if ($_ -match '\s') { "`"$_`"" } else { $_ } }) -join ' '

Invoke-Nssm install $name $PythonExe $argString
Invoke-Nssm set $name AppDirectory $EngineRoot
Invoke-Nssm set $name DisplayName $Svc.DisplayName
Invoke-Nssm set $name Description $Svc.Description

$envCmd = @("set", $name, "AppEnvironmentExtra") + $Svc.Env
if ($DryRun) {
    $envStr = ($envCmd | ForEach-Object { if ($_ -match '\s') { "`"$_`"" } else { $_ } }) -join ' '
    Write-Host "  [dry-run] nssm $envStr" -ForegroundColor DarkGray
} else {
    & $NssmExe @envCmd
    if ($LASTEXITCODE -ne 0) { throw "nssm set AppEnvironmentExtra failed with exit $LASTEXITCODE" }
}

Invoke-Nssm set $name Start SERVICE_AUTO_START
Invoke-Nssm set $name AppStdout "$LogDir\service_$name.stdout.log"
Invoke-Nssm set $name AppStderr "$LogDir\service_$name.stderr.log"
Invoke-Nssm set $name AppRotateFiles 1
Invoke-Nssm set $name AppRotateOnline 1
Invoke-Nssm set $name AppRotateBytes 104857600
Invoke-Nssm set $name AppRotateSeconds 0
Invoke-Nssm set $name AppExit Default Restart
Invoke-Nssm set $name AppRestartDelay 5000
Invoke-Nssm set $name AppThrottle 10000
Invoke-Nssm set $name AppKillProcessTree 1
Invoke-Nssm set $name ObjectName LocalSystem

Write-Host "  OK (created stopped). Start with: nssm start $name" -ForegroundColor Green
