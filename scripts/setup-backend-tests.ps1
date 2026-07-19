#requires -Version 5.1
<#
.SYNOPSIS
    Create a backend test venv and run pytest.

.DESCRIPTION
    The end-to-end validation (audit P2) flagged that backend tests
    couldn't run because pytest wasn't installed in the active Python
    environment and there was no documented dev-environment recipe.

    This script:
      1. Creates `backend\.venv` if it doesn't exist (using the Python
         interpreter found via py.exe / python.exe — whichever resolves).
      2. Installs `requirements.txt` + `requirements-dev.txt` into it.
      3. Runs pytest with the "not stress and not external" markers so
         the day-to-day developer suite runs offline.

    Re-running the script reuses the existing venv (the pip install step
    is idempotent — pip won't reinstall already-satisfied requirements).

.PARAMETER Reinstall
    Force a fresh venv. Deletes backend\.venv first.

.PARAMETER PytestArgs
    Extra args appended to the pytest invocation. Example:
      .\setup-backend-tests.ps1 -PytestArgs "-k test_node_conformance"

.NOTES
    Requires py.exe (Windows Python launcher) OR python.exe on PATH.
    If neither is available, the script fails with a clear message.
#>

[CmdletBinding()]
param(
    [switch]$Reinstall,
    [string]$PytestArgs = ""
)

$ErrorActionPreference = 'Stop'

$BackendDir = (Resolve-Path (Join-Path $PSScriptRoot '..\backend')).Path
$VenvDir = Join-Path $BackendDir '.venv'
$VenvPython = Join-Path $VenvDir 'Scripts\python.exe'
$ReqRuntime = Join-Path $BackendDir 'requirements.txt'
$ReqDev = Join-Path $BackendDir 'requirements-dev.txt'

Write-Host "==> Backend dir: $BackendDir" -ForegroundColor Cyan
Write-Host "    venv:        $VenvDir" -ForegroundColor Gray

if ($Reinstall -and (Test-Path $VenvDir)) {
    Write-Host "==> -Reinstall set; deleting existing venv..." -ForegroundColor Yellow
    Remove-Item -Recurse -Force $VenvDir
}

# 1. Locate a usable Python interpreter for venv creation.
$pythonCmd = $null
foreach ($candidate in @('py', 'python', 'python3')) {
    try {
        $found = Get-Command $candidate -ErrorAction Stop
        $pythonCmd = $found.Source
        break
    } catch {
        continue
    }
}
if (-not $pythonCmd) {
    Write-Error @"
No Python interpreter found on PATH. Install Python 3.11+ and either:
  * Add it to PATH (so `python` resolves), OR
  * Install the Windows Python launcher (`py`) which ships with the official installer.
"@
    exit 1
}
Write-Host "==> Using Python: $pythonCmd" -ForegroundColor Cyan

# 2. Create venv if missing.
if (-not (Test-Path $VenvPython)) {
    Write-Host "==> Creating venv at $VenvDir ..." -ForegroundColor Cyan
    & $pythonCmd -m venv $VenvDir
    if ($LASTEXITCODE -ne 0) {
        Write-Error "venv creation failed (exit $LASTEXITCODE)."
        exit $LASTEXITCODE
    }
} else {
    Write-Host "==> Reusing existing venv (use -Reinstall to start fresh)." -ForegroundColor Gray
}

# 3. Upgrade pip + install requirements.
Write-Host "==> Upgrading pip ..." -ForegroundColor Cyan
& $VenvPython -m pip install --upgrade pip --quiet
if ($LASTEXITCODE -ne 0) { Write-Error "pip upgrade failed"; exit $LASTEXITCODE }

if (Test-Path $ReqRuntime) {
    Write-Host "==> Installing requirements.txt ..." -ForegroundColor Cyan
    & $VenvPython -m pip install -r $ReqRuntime --quiet
    if ($LASTEXITCODE -ne 0) { Write-Error "runtime requirements install failed"; exit $LASTEXITCODE }
}

if (Test-Path $ReqDev) {
    Write-Host "==> Installing requirements-dev.txt ..." -ForegroundColor Cyan
    & $VenvPython -m pip install -r $ReqDev --quiet
    if ($LASTEXITCODE -ne 0) { Write-Error "dev requirements install failed"; exit $LASTEXITCODE }
} else {
    Write-Host "==> requirements-dev.txt missing at $ReqDev — installing pytest only." -ForegroundColor Yellow
    & $VenvPython -m pip install pytest pytest-asyncio pytest-mock --quiet
}

# 4. Run pytest. The "not stress and not external" filter keeps the
#    day-to-day suite offline-friendly.
Write-Host "==> Running pytest (excluding stress + external markers)..." -ForegroundColor Cyan
Push-Location $BackendDir
try {
    $args = @('-m', 'pytest', '--tb=short', '-m', 'not stress and not external')
    if ($PytestArgs) {
        # User-supplied args go AFTER the markers so they can override.
        $args += $PytestArgs.Split(' ')
    }
    & $VenvPython @args
    $exitCode = $LASTEXITCODE
} finally {
    Pop-Location
}

if ($exitCode -eq 0) {
    Write-Host "" -ForegroundColor Green
    Write-Host "==> pytest passed." -ForegroundColor Green
} else {
    Write-Host "" -ForegroundColor Red
    Write-Host "==> pytest exited with code $exitCode — see output above." -ForegroundColor Red
}
exit $exitCode
