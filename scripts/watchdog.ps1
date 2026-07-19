# F-Pulse Watchdog — process supervisor, health monitoring, auto-restart.
#
# Purpose
# -------
# Polls the F-Pulse backend on /api/health every N seconds. If the backend
# stops responding for M consecutive checks, kills any uvicorn process on
# the configured port and restarts it. A circuit breaker halts the loop
# after K restarts within T minutes so a genuinely broken build doesn't
# get thrashed forever.
#
# Why this exists
# ---------------
# Docker healthchecks cover the containerised production story (Stage 4).
# For native dev / operator-run deploys, nothing watches the process, so
# a DuckDB OOM or a stuck request takes the whole backend down silently
# until someone notices the frontend is broken. This script fills that
# gap. It is safe to run alongside start.ps1 — it only interferes when a
# restart is actually warranted.
#
# Usage
# -----
#   # Default: poll localhost:8001 every 30s
#   .\watchdog.ps1
#
#   # Custom port / poll interval / data dir
#   .\watchdog.ps1 -Port 8001 -IntervalSeconds 30 -DataDir "C:\fpulse-data"
#
#   # Dry-run — log everything, never restart (safe first pass)
#   .\watchdog.ps1 -DryRun
#
# Stop the watchdog: Ctrl-C. Cleanup is automatic — we don't leak jobs.
#
# Log file: $DataDir\watchdog.log (appended, never rotated — external
# logrotate / Event Viewer is the operator's job).

param(
    [int] $Port = 8001,
    [int] $IntervalSeconds = 30,
    [int] $FailuresBeforeRestart = 2,
    [int] $MaxRestartsInWindow = 5,
    [int] $RestartWindowMinutes = 10,
    [string] $DataDir = "",
    [string] $BackendDir = "",
    [switch] $DryRun
)

$ErrorActionPreference = "Stop"

# ── Path resolution ───────────────────────────────────────────────────
$ROOT = $PSScriptRoot
if (-not $BackendDir) { $BackendDir = Join-Path $ROOT "backend" }
if (-not $DataDir)    { $DataDir = Join-Path $ROOT "data" }

if (-not (Test-Path $BackendDir)) {
    Write-Host "ERROR: backend directory not found: $BackendDir" -ForegroundColor Red
    exit 2
}
if (-not (Test-Path $DataDir)) {
    New-Item -ItemType Directory -Path $DataDir -Force | Out-Null
}

$LogFile = Join-Path $DataDir "watchdog.log"

# ── Python resolution (match start.ps1) ──────────────────────────────
# Prefer FPULSE_PYTHON env var, then a project-local venv, then PATH.
$python = $env:FPULSE_PYTHON
if (-not $python -or -not (Test-Path $python)) {
    $root = Split-Path -Parent $PSCommandPath
    if (Test-Path "$root\.venv\Scripts\python.exe") {
        $python = "$root\.venv\Scripts\python.exe"
    } elseif (Test-Path "$root\backend\.venv\Scripts\python.exe") {
        $python = "$root\backend\.venv\Scripts\python.exe"
    } else {
        $python = "python"
    }
}

# ── Helpers ───────────────────────────────────────────────────────────
function Write-Log {
    param([string] $Level, [string] $Message)
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $line = "[$ts] [$Level] $Message"
    Write-Host $line
    try { Add-Content -Path $LogFile -Value $line -Encoding UTF8 } catch { }
}

function Test-BackendHealth {
    param([int] $ProbePort)
    try {
        # Short timeout — a hung uvicorn shouldn't stall the watchdog.
        # 5s is enough for a healthy /api/health round-trip even under
        # load; anything slower means we need to investigate anyway.
        $r = Invoke-WebRequest `
            -Uri "http://localhost:$ProbePort/api/health" `
            -TimeoutSec 5 -UseBasicParsing
        return $r.StatusCode -ge 200 -and $r.StatusCode -lt 500
    } catch {
        return $false
    }
}

function Get-UvicornProcessOnPort {
    param([int] $ProbePort)
    # Find the PID bound to the port via netstat, filter to python/uvicorn.
    # Using netstat instead of Get-NetTCPConnection because the latter
    # requires admin on some Windows builds.
    try {
        $netstat = netstat -ano | Select-String ":$ProbePort\s+.*LISTENING"
        foreach ($line in $netstat) {
            $parts = $line -split '\s+' | Where-Object { $_ -ne "" }
            $pidStr = $parts[-1]
            if ($pidStr -match '^\d+$') {
                $procId = [int] $pidStr
                try {
                    $proc = Get-Process -Id $procId -ErrorAction Stop
                    if ($proc.Name -match "python|uvicorn") {
                        return $proc
                    }
                } catch { }
            }
        }
    } catch { }
    return $null
}

function Stop-BackendProcess {
    param([int] $ProbePort)
    $proc = Get-UvicornProcessOnPort -ProbePort $ProbePort
    if ($proc) {
        Write-Log "INFO" "Stopping uvicorn (PID $($proc.Id)) on port $ProbePort"
        if (-not $DryRun) {
            try {
                # SIGTERM equivalent on Windows: Stop-Process; if the
                # process ignores it, force kill after 3s. Uvicorn's
                # lifespan teardown normally completes in <1s so we
                # don't usually hit the force path.
                $proc | Stop-Process -Force
                Start-Sleep -Milliseconds 500
            } catch {
                Write-Log "WARN" "Stop-Process failed: $_"
            }
        } else {
            Write-Log "DRYRUN" "Would Stop-Process PID $($proc.Id)"
        }
    } else {
        Write-Log "INFO" "No uvicorn process found on port $ProbePort (already down)"
    }
}

function Start-Backend {
    param([int] $BootPort)
    Write-Log "INFO" "Starting backend on port $BootPort (warmup=light)"
    if ($DryRun) {
        Write-Log "DRYRUN" "Would launch: python -m uvicorn fpulse.main:app --port $BootPort"
        return
    }
    # Launch detached. Watchdog's health loop will confirm readiness in
    # the next tick. We inherit any FPULSE_* env vars the operator set
    # in the shell that invoked the watchdog.
    $env:FPULSE_WARMUP_HEAVY = "0"
    # 2026-06-02 hardening: respect the loopback-default convention. The
    # watchdog inherits FPULSE_BIND_HOST / FPULSE_ALLOW_LAN from the
    # shell that started it; loopback is the safe default when neither
    # is set.
    $bindHost = if ($env:FPULSE_BIND_HOST) {
        $env:FPULSE_BIND_HOST
    } elseif ($env:FPULSE_ALLOW_LAN -eq "1") {
        "0.0.0.0"
    } else {
        "127.0.0.1"
    }
    Start-Process -FilePath $python `
        -ArgumentList "-m", "uvicorn", "fpulse.main:app",
                      "--host", $bindHost, "--port", "$BootPort" `
        -WorkingDirectory $BackendDir `
        -WindowStyle Hidden
}

# ── Circuit breaker state ─────────────────────────────────────────────
$restartTimestamps = New-Object System.Collections.Generic.List[datetime]

function Test-RestartBudget {
    # Evict restart timestamps older than the window so we only count
    # restarts within the last RestartWindowMinutes.
    $cutoff = (Get-Date).AddMinutes(-$RestartWindowMinutes)
    while ($restartTimestamps.Count -gt 0 -and $restartTimestamps[0] -lt $cutoff) {
        $restartTimestamps.RemoveAt(0)
    }
    return $restartTimestamps.Count -lt $MaxRestartsInWindow
}

# ── Main loop ─────────────────────────────────────────────────────────
Write-Log "INFO" "F-Pulse watchdog starting — port=$Port interval=${IntervalSeconds}s failures=$FailuresBeforeRestart budget=$MaxRestartsInWindow/${RestartWindowMinutes}min dryRun=$DryRun"

$consecutiveFailures = 0

try {
    while ($true) {
        if (Test-BackendHealth -ProbePort $Port) {
            if ($consecutiveFailures -gt 0) {
                Write-Log "INFO" "Backend recovered after $consecutiveFailures failure(s)"
            }
            $consecutiveFailures = 0
        } else {
            $consecutiveFailures++
            Write-Log "WARN" "Health probe failed ($consecutiveFailures / $FailuresBeforeRestart before restart)"

            if ($consecutiveFailures -ge $FailuresBeforeRestart) {
                if (-not (Test-RestartBudget)) {
                    Write-Log "ERROR" "Restart budget exhausted ($MaxRestartsInWindow restarts in $RestartWindowMinutes min). Halting watchdog — manual intervention required."
                    break
                }
                Write-Log "WARN" "Restarting backend (attempt $($restartTimestamps.Count + 1) in window)"
                Stop-BackendProcess -ProbePort $Port
                Start-Sleep -Seconds 2
                Start-Backend -BootPort $Port
                $restartTimestamps.Add((Get-Date)) | Out-Null
                $consecutiveFailures = 0
                # Give the new process time to come up before the next
                # probe — otherwise we'd immediately count it as failed
                # and thrash toward the budget ceiling.
                Start-Sleep -Seconds 10
            }
        }
        Start-Sleep -Seconds $IntervalSeconds
    }
} finally {
    Write-Log "INFO" "Watchdog stopping"
}
