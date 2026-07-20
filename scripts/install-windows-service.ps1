# install-windows-service.ps1
#
# Register F-Pulse as a true Windows Service using NSSM
# (Non-Sucking Service Manager). Survives user logout, starts at boot,
# auto-restarts on crash, runs as LocalSystem or a service account.
#
# Why NSSM and not pure sc.exe?
#   sc.exe can create a service but can't supervise a Python process
#   (it expects a Windows-Service-native binary that responds to
#   SCM control codes). NSSM wraps any executable as a proper
#   service with restart policies, log redirection, and graceful
#   stop semantics — purpose-built for exactly this use case.
#
# Prereq: download NSSM once from https://nssm.cc/download and put
# nssm.exe somewhere on PATH (or in C:\nssm\). The script auto-detects.
#
# Run from elevated PowerShell:
#
#     PS> .\scripts\install-windows-service.ps1
#
# To uninstall:
#     PS> .\scripts\install-windows-service.ps1 -Uninstall

[CmdletBinding()]
param(
    [string]$ServiceName = "FPulse",
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$NssmPath = $null,
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"

# ── Elevation check ──
$currentUser = [Security.Principal.WindowsPrincipal]::new(
    [Security.Principal.WindowsIdentity]::GetCurrent()
)
if (-not $currentUser.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host ""
    Write-Host "  ERROR: This script must run as Administrator." -ForegroundColor Red
    Write-Host "  Right-click PowerShell -> Run as Administrator, then re-run." -ForegroundColor Red
    Write-Host ""
    exit 1
}

# ── Locate NSSM ──
if (-not $NssmPath -or -not (Test-Path $NssmPath)) {
    $candidates = @(
        (Get-Command nssm.exe -ErrorAction SilentlyContinue).Source,
        "C:\nssm\nssm.exe",
        "C:\nssm\win64\nssm.exe",
        "C:\Program Files\nssm\nssm.exe",
        (Join-Path $ProjectRoot "tools\nssm.exe")
    ) | Where-Object { $_ -and (Test-Path $_) }

    if ($candidates) { $NssmPath = $candidates[0] }
}
if (-not $NssmPath -or -not (Test-Path $NssmPath)) {
    Write-Host ""
    Write-Host "  ERROR: nssm.exe not found." -ForegroundColor Red
    Write-Host "  Download from https://nssm.cc/download and either:" -ForegroundColor Red
    Write-Host "    1. Place nssm.exe in C:\nssm\, or" -ForegroundColor Red
    Write-Host "    2. Pass the path explicitly:" -ForegroundColor Red
    Write-Host "       .\install-windows-service.ps1 -NssmPath C:\path\to\nssm.exe" -ForegroundColor Red
    Write-Host ""
    Write-Host "  Alternative without NSSM: use the Scheduled Task script:" -ForegroundColor Yellow
    Write-Host "    .\install-scheduled-task.ps1" -ForegroundColor Yellow
    Write-Host ""
    exit 1
}

# ── Uninstall path ──
if ($Uninstall) {
    $existing = sc.exe query $ServiceName 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  Service '$ServiceName' is not registered. Nothing to do." -ForegroundColor Yellow
        exit 0
    }
    Write-Host "  Stopping service '$ServiceName'..." -ForegroundColor Cyan
    & $NssmPath stop $ServiceName confirm | Out-Null
    Write-Host "  Removing service '$ServiceName'..." -ForegroundColor Cyan
    & $NssmPath remove $ServiceName confirm | Out-Null
    Write-Host "  Done. F-Pulse service uninstalled." -ForegroundColor Green
    exit 0
}

# ── Resolve the Python interpreter the same way start.ps1 does ──
$python = $env:FPULSE_PYTHON
if (-not $python -or -not (Test-Path $python)) {
    if     (Test-Path "$ProjectRoot\.venv\Scripts\python.exe")          { $python = "$ProjectRoot\.venv\Scripts\python.exe" }
    elseif (Test-Path "$ProjectRoot\backend\.venv\Scripts\python.exe")  { $python = "$ProjectRoot\backend\.venv\Scripts\python.exe" }
    else { $python = (Get-Command python.exe -ErrorAction SilentlyContinue).Source }
}
if (-not $python -or -not (Test-Path $python)) {
    Write-Host "  ERROR: Could not find python.exe. Run start.ps1 once first to create the venv." -ForegroundColor Red
    exit 1
}

$backend  = Join-Path $ProjectRoot "backend"
$dataDir  = Join-Path $ProjectRoot "data\samples"
$logDir   = Join-Path $ProjectRoot "logs"
New-Item -ItemType Directory -Path $logDir -Force | Out-Null

Write-Host ""
Write-Host "  Installing Windows Service '$ServiceName' via NSSM..." -ForegroundColor Cyan
Write-Host "  Python  : $python"  -ForegroundColor DarkGray
Write-Host "  Backend : $backend" -ForegroundColor DarkGray
Write-Host "  Data    : $dataDir" -ForegroundColor DarkGray
Write-Host "  Logs    : $logDir"  -ForegroundColor DarkGray
Write-Host ""

# Remove first if it already exists (idempotent install).
& $NssmPath remove $ServiceName confirm 2>$null | Out-Null

# 2026-06-02 hardening: default to 127.0.0.1 bind even for service mode.
# Set $env:FPULSE_ALLOW_LAN=1 BEFORE running this script if you want the
# Windows service to listen on the LAN (typical for on-prem multi-user
# deployments). See docs/install/security-hardening.md.
$bindHost = if ($env:FPULSE_BIND_HOST) {
  $env:FPULSE_BIND_HOST
} elseif ($env:FPULSE_ALLOW_LAN -eq "1") {
  "0.0.0.0"
} else {
  "127.0.0.1"
}
if ($bindHost -ne "127.0.0.1") {
  Write-Host "  [INFO] Service will bind to $bindHost (LAN-visible)." -ForegroundColor Yellow
}

# Install: nssm install <name> <exe> <args>
$args = "-m uvicorn fpulse.main:app --host $bindHost --port 8001"
& $NssmPath install $ServiceName $python $args | Out-Null

# Working directory + env
& $NssmPath set $ServiceName AppDirectory     $backend           | Out-Null
& $NssmPath set $ServiceName AppEnvironmentExtra "PYTHONPATH=$backend" "FPULSE_DATA_DIR=$dataDir" | Out-Null

# Log redirection — both stdout + stderr to rotating files
& $NssmPath set $ServiceName AppStdout        (Join-Path $logDir "fpulse.out.log") | Out-Null
& $NssmPath set $ServiceName AppStderr        (Join-Path $logDir "fpulse.err.log") | Out-Null
& $NssmPath set $ServiceName AppRotateFiles   1            | Out-Null
& $NssmPath set $ServiceName AppRotateBytes   10485760    | Out-Null   # 10 MB

# Restart policy: on any non-zero exit, restart after 5 s, up to 99 times.
& $NssmPath set $ServiceName AppExit Default Restart | Out-Null
& $NssmPath set $ServiceName AppRestartDelay 5000    | Out-Null
& $NssmPath set $ServiceName AppThrottle     1500    | Out-Null

# Display + description
& $NssmPath set $ServiceName DisplayName "F-Pulse OSS"            | Out-Null
& $NssmPath set $ServiceName Description "F-Pulse OSS — local-first data pipeline orchestrator. Auto-restart on crash, starts at boot." | Out-Null

# Startup type: auto
& $NssmPath set $ServiceName Start SERVICE_AUTO_START | Out-Null

Write-Host "  Service '$ServiceName' registered." -ForegroundColor Green
Write-Host ""
Write-Host "  Starting service..." -ForegroundColor Cyan
& $NssmPath start $ServiceName | Out-Null
Start-Sleep -Seconds 2

# Status
$status = (sc.exe query $ServiceName | Select-String "STATE").ToString().Trim()
Write-Host "  $status" -ForegroundColor White
Write-Host ""

Write-Host "  App URL: http://localhost:8001     (API docs: /docs when FPULSE_MODE=dev)" -ForegroundColor Cyan
Write-Host ""
Write-Host "  Useful commands:" -ForegroundColor Cyan
Write-Host "    sc.exe query   $ServiceName"      -ForegroundColor DarkGray
Write-Host "    sc.exe start   $ServiceName"      -ForegroundColor DarkGray
Write-Host "    sc.exe stop    $ServiceName"      -ForegroundColor DarkGray
Write-Host "    Get-Content '$logDir\fpulse.out.log' -Wait -Tail 20" -ForegroundColor DarkGray
Write-Host ""
Write-Host "  Uninstall:" -ForegroundColor Cyan
Write-Host "    .\install-windows-service.ps1 -Uninstall" -ForegroundColor DarkGray
Write-Host ""
