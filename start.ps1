# F-Pulse launcher (v2, 2026-06-06)
#
# Usage:
#   .\start.ps1                  Auto-pick free ports if defaults busy.
#   .\start.ps1 -Force           Auto-stop the previous F-Pulse instance
#                                without prompting (still ownership-checked).
#
# Env-var preferences (optional - start of port scan range, not required):
#   $env:FPULSE_FRONTEND_PORT    default 5174
#   $env:FPULSE_PORT             default 8001
#
# Design rules (pinned in this script and in launcher/launcher-utils.ps1):
#   - Hard-coded ports are never assumed free. Always probe first.
#   - A process is NEVER killed unless we recorded its PID in our own
#     ownership file AND it still passes the 3-signal identity check.
#   - "vite" / "fpulse" in a stranger's command line is NOT enough.
#   - -Force skips ONLY the interactive prompt; ownership rules apply.
#
# What changed from v1 (reviewer audit 2026-06-06):
#   v1 inspected the holder's command line via wmic and offered to kill
#   anything matching "vite|fpulse". That's a heuristic ownership claim,
#   not a real one - another developer's Vite project on 5174 could pass
#   the check and get killed. v2 keeps a runtime ownership file that
#   records the PIDs we ourselves spawned. We only stop those.

[CmdletBinding()]
param(
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
$ROOT = $PSScriptRoot
. (Join-Path $ROOT 'launcher\launcher-utils.ps1')

$BACKEND  = Join-Path $ROOT 'backend'
$FRONTEND = Join-Path $ROOT 'frontend'
$DATA     = Join-Path $ROOT 'data'

Write-Host ""
Write-Host "  ======================================================" -ForegroundColor DarkCyan
Write-Host "   F-Pulse v1.0.0 - Open Source Pipeline Builder" -ForegroundColor Cyan
Write-Host "   by Hybridyn Data Labs" -ForegroundColor DarkCyan
Write-Host "  ======================================================" -ForegroundColor DarkCyan
Write-Host ""

# --- Step 1: preferences + check existing instance ---

$preferredFrontend = if ($env:FPULSE_FRONTEND_PORT) { [int]$env:FPULSE_FRONTEND_PORT } else { 5174 }
$preferredBackend  = if ($env:FPULSE_PORT)          { [int]$env:FPULSE_PORT }          else { 8001 }

Write-LauncherInfo "[1/4] Resolving ports..."

# Is there a previous instance recorded? If so, is it still alive AND ours?
$prev = Read-RuntimeFile -RepoRoot $ROOT
$prevAlive = $false
if ($null -ne $prev) {
    $beAlive = Test-OwnedFPulse -ProcessId $prev.backend_pid  -ExpectedPort $prev.backend_port  -Kind 'backend'  -RepoRoot $ROOT
    $feAlive = Test-OwnedFPulse -ProcessId $prev.frontend_pid -ExpectedPort $prev.frontend_port -Kind 'frontend' -RepoRoot $ROOT
    if ($beAlive -or $feAlive) {
        $prevAlive = $true
        Write-LauncherWarn "  A previous F-Pulse instance is recorded as running:"
        if ($beAlive) { Write-Host "      Backend  PID $($prev.backend_pid)  on port $($prev.backend_port)" -ForegroundColor DarkGray }
        if ($feAlive) { Write-Host "      Frontend PID $($prev.frontend_pid) on port $($prev.frontend_port)" -ForegroundColor DarkGray }
        Write-Host "      Started: $($prev.started_at)" -ForegroundColor DarkGray
        Write-Host ""
        $stopPrev = $false
        if ($Force) {
            Write-LauncherDim "  -Force: stopping the previous instance (ownership-checked)."
            $stopPrev = $true
        } else {
            $ans = Read-Host "  [Y] Stop it and reuse those ports   [N] Start a new instance on different ports   Choice"
            if ($ans -match '^[Yy]') { $stopPrev = $true }
        }
        if ($stopPrev) {
            if ($beAlive) {
                $ok = Stop-OwnedProcess -ProcessId $prev.backend_pid -ExpectedPort $prev.backend_port -Kind 'backend' -RepoRoot $ROOT
                if ($ok) { Write-LauncherOk "  Stopped previous backend (PID $($prev.backend_pid))." }
            }
            if ($feAlive) {
                $ok = Stop-OwnedProcess -ProcessId $prev.frontend_pid -ExpectedPort $prev.frontend_port -Kind 'frontend' -RepoRoot $ROOT
                if ($ok) { Write-LauncherOk "  Stopped previous frontend (PID $($prev.frontend_pid))." }
            }
            Start-Sleep -Seconds 1
            # Adopt the previous instance's ports as our preferences so
            # the URL stays stable across stop+start.
            $preferredFrontend = [int]$prev.frontend_port
            $preferredBackend  = [int]$prev.backend_port
            Remove-RuntimeFile -RepoRoot $ROOT
            $prevAlive = $false
        }
    } else {
        # Stale runtime file (PIDs dead or no longer match). Clean it up.
        Remove-RuntimeFile -RepoRoot $ROOT
    }
}

if ($prevAlive) {
    # User chose [N] - start a fresh instance on a different port pair.
    # Push the scan range past the live instance so we don't collide.
    Write-LauncherDim "  Starting a fresh instance alongside the existing one."
}

# --- Step 2: find free ports (auto-pick if preferred is busy) ---

$frontendPort = Find-FreePort -Preferred $preferredFrontend
$backendPort  = Find-FreePort -Preferred $preferredBackend

if ($frontendPort -ne $preferredFrontend) {
    Write-LauncherWarn "  Port $preferredFrontend in use by another app; using $frontendPort for frontend."
} else {
    Write-LauncherDim "  Frontend port: $frontendPort"
}
if ($backendPort -ne $preferredBackend) {
    Write-LauncherWarn "  Port $preferredBackend in use by another app; using $backendPort for backend."
} else {
    Write-LauncherDim "  Backend port:  $backendPort"
}

# --- Step 3: resolve Python interpreter ---

$python = $env:FPULSE_PYTHON
if (-not $python -or -not (Test-Path $python)) {
    if (Test-Path "$ROOT\.venv\Scripts\python.exe") {
        $python = "$ROOT\.venv\Scripts\python.exe"
    } elseif (Test-Path "$BACKEND\.venv\Scripts\python.exe") {
        $python = "$BACKEND\.venv\Scripts\python.exe"
    } else {
        $python = "python"
    }
}

# Ensure data directory exists.
if (-not (Test-Path $DATA)) { New-Item -ItemType Directory -Path $DATA -Force | Out-Null }

# --- Step 4: dependencies ---

Push-Location $BACKEND
& $python -c "import fastapi, uvicorn, psutil, duckdb, pydantic, pandas, pyarrow, openpyxl, pyodbc, yaml, numpy, anyio, starlette, requests, prometheus_client" 2>$null
if ($LASTEXITCODE -eq 0) {
    Write-LauncherOk "[2/4] Backend dependencies OK"
} else {
    Write-LauncherWarn "[2/4] Installing backend dependencies (first run, may take a few minutes)..."
    & $python -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) {
        Write-LauncherError "ERROR: pip install failed. Run this manually to see the error:"
        Write-LauncherError "  & '$python' -m pip install -r '$BACKEND\requirements.txt'"
        Pop-Location
        exit 1
    }
}
Pop-Location

if (-not (Test-Path "$FRONTEND\node_modules")) {
    Write-LauncherWarn "[3/4] Installing frontend dependencies..."
    Push-Location $FRONTEND
    npm install --silent 2>$null
    Pop-Location
} else {
    Write-LauncherOk "[3/4] Frontend dependencies OK"
}

Write-LauncherInfo "[4/4] Starting servers..."
Write-Host ""

# --- Step 5: write runtime file (ports first, PIDs after spawn) ---

# We write BEFORE spawning so vite.config.ts can read the port even on
# the very first npm-run-dev. PIDs get filled in after the children bind.
$instance = @{
    schema_version = 1
    instance_id    = "fpulse-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
    frontend_port  = $frontendPort
    backend_port   = $backendPort
    frontend_pid   = 0
    backend_pid    = 0
    cwd            = $ROOT
    started_at     = (Get-Date -Format 'yyyy-MM-ddTHH:mm:sszzz')
    pid_owner      = $PID    # launching PowerShell PID, for diagnostics
}
Write-RuntimeFile -RepoRoot $ROOT -Instance $instance

# --- Step 6: environment for children ---

$env:FPULSE_DATA_DIR    = (Join-Path $DATA 'samples')
$env:PYTHONPATH         = $BACKEND
$env:FPULSE_PORT        = "$backendPort"
$env:VITE_FRONTEND_PORT = "$frontendPort"
$env:VITE_BACKEND_PORT  = "$backendPort"

# --- Step 7: spawn backend ---

$bindHost = if ($env:FPULSE_ALLOW_LAN -eq "1") {
    Write-LauncherWarn "  [WARNING] FPULSE_ALLOW_LAN=1 - backend will bind to 0.0.0.0"
    "0.0.0.0"
} else { "127.0.0.1" }

# Enable Swagger UI (/docs) + OpenAPI for the local run by default. The
# startup banner advertises http://localhost:PORT/docs and the frontend
# codegen reads /openapi.json, so without this the advertised URL 404s.
# Auto-enable only on localhost binds so the API surface isn't anonymously
# enumerable off-host; for LAN binds it stays off unless you opt in.
# Override either way with $env:FPULSE_ENABLE_API_DOCS (1 = on, 0 = off).
if ((-not $env:FPULSE_ENABLE_API_DOCS) -and ($bindHost -eq "127.0.0.1")) {
    $env:FPULSE_ENABLE_API_DOCS = "1"
}

$backendProc = Start-Process -FilePath $python `
    -ArgumentList @('-m', 'uvicorn', 'fpulse.main:app', '--host', $bindHost, '--port', "$backendPort") `
    -WorkingDirectory $BACKEND `
    -WindowStyle Normal `
    -PassThru

# --- Step 8: spawn frontend ---

$frontendProc = Start-Process -FilePath 'cmd.exe' `
    -ArgumentList @('/c', "npm run dev") `
    -WorkingDirectory $FRONTEND `
    -WindowStyle Normal `
    -PassThru

# --- Step 9: wait for both to bind, capture true holder PIDs ---

Write-LauncherDim "  Waiting for servers to bind..."
$backendHolderPid  = Wait-ForPortBinding -Port $backendPort  -TimeoutSeconds 30
$frontendHolderPid = Wait-ForPortBinding -Port $frontendPort -TimeoutSeconds 30

# Some spawners (cmd /c) host a child node/python that's the real
# listener. Get-PortHolder returns the listener PID, which is the one
# we actually need to track. Fall back to Start-Process PID if the
# poll timed out (rare - dependency install path or slow machine).
$backendPidToTrack  = if ($backendHolderPid  -gt 0) { $backendHolderPid }  else { $backendProc.Id  }
$frontendPidToTrack = if ($frontendHolderPid -gt 0) { $frontendHolderPid } else { $frontendProc.Id }

# --- Step 10: rewrite runtime file with PIDs ---

$instance.frontend_pid = $frontendPidToTrack
$instance.backend_pid  = $backendPidToTrack
Write-RuntimeFile -RepoRoot $ROOT -Instance $instance

# --- Step 11: print the single canonical URL ---

Write-Host ""
Write-Host "  ======================================================" -ForegroundColor DarkGreen
Write-Host "   F-Pulse is ready" -ForegroundColor Green
Write-Host "  ======================================================" -ForegroundColor DarkGreen
Write-Host "   UI:   http://localhost:$frontendPort" -ForegroundColor White
Write-Host "   API:  http://localhost:$backendPort/docs" -ForegroundColor White
Write-Host "   Inst: $($instance.instance_id)" -ForegroundColor DarkGray
Write-Host ""
Write-Host "   To stop: .\stop.ps1   (or .\stop.bat)" -ForegroundColor DarkGray
Write-Host "   Runtime state: .fpulse\runtime\instance.json" -ForegroundColor DarkGray
Write-Host ""

# --- Step 12: open the UI in the default browser (like `fpulse open`) ---
# So a double-clicked desktop shortcut brings the user straight to the
# app instead of a console window with a URL to copy. Suppress with
# $env:FPULSE_NO_OPEN=1 for headless / CI / remote launches.
if (-not $env:FPULSE_NO_OPEN) {
    try {
        Start-Process "http://localhost:$frontendPort" | Out-Null
        Write-Host "   Opened http://localhost:$frontendPort in your browser." -ForegroundColor DarkGray
    } catch {
        Write-Host "   (Could not auto-open a browser - open the UI link above manually.)" -ForegroundColor DarkYellow
    }
}
Write-Host "   Tip: run .\Create-Shortcut.ps1 once for a desktop icon." -ForegroundColor DarkGray
Write-Host ""
