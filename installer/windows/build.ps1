# installer/windows/build.ps1
#
# One-command build for the Windows installer.
#
# Prereqs (one-time setup):
#   1. Inno Setup 6 — https://jrsoftware.org/isinfo.php
#      Add ISCC.exe to PATH or use -InnoPath to point at it.
#   2. Python venv at <repo>\.venv with pyinstaller installed.
#   3. Node.js + npm for the frontend build.
#
# Usage:
#   .\installer\windows\build.ps1
#   .\installer\windows\build.ps1 -Sign -CertThumbprint "<sha1>"
#
# Output:
#   installer\windows\output\FPulse-Setup-<version>.exe

[CmdletBinding()]
param(
    [string]$InnoPath = "ISCC.exe",
    [switch]$Sign,
    [string]$CertThumbprint = $env:FPULSE_WIN_CERT_THUMBPRINT,
    [string]$TimestampUrl = "http://timestamp.digicert.com"
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path "$PSScriptRoot\..\..").Path
$installerDir = $PSScriptRoot

Write-Host ""
Write-Host "  F-Pulse Windows installer build" -ForegroundColor Cyan
Write-Host "  Repo root    : $repoRoot"
Write-Host "  Installer dir: $installerDir"
Write-Host ""

# ── 1. Build the frontend ──
Write-Host "  [1/4] Building frontend (npm run build)..." -ForegroundColor Yellow
Push-Location "$repoRoot\frontend"
try {
    npm install --silent
    if ($LASTEXITCODE -ne 0) { throw "npm install failed" }
    npm run build
    if ($LASTEXITCODE -ne 0) { throw "npm run build failed" }
}
finally { Pop-Location }
Write-Host "  Frontend built." -ForegroundColor Green

# ── 2. Freeze the backend with PyInstaller ──
Write-Host ""
Write-Host "  [2/4] Freezing backend with PyInstaller..." -ForegroundColor Yellow
$python = "$repoRoot\.venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    throw "Python venv not found at $python. Run from repo root: python -m venv .venv && .venv\Scripts\pip install -e .[dev] pyinstaller"
}
Push-Location $repoRoot
try {
    # Use the spec (installer\windows\fpulse.spec) — it collects the data
    # files + dynamically-imported submodules that the old inline
    # --collect-all command missed (pandas/pyarrow/tzdata/reportlab/etc.).
    & $python -m PyInstaller --noconfirm --clean "$installerDir\fpulse.spec"
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

    # Self-validate the freeze BEFORE packaging: import the full server
    # stack + build the app. Starts no server, writes no data. If a module
    # is missing the build fails here (loud) instead of shipping a broken
    # installer.
    Write-Host "  Validating freeze (fpulse.exe selftest)..." -ForegroundColor Yellow
    & "$repoRoot\dist\fpulse\fpulse.exe" selftest
    if ($LASTEXITCODE -ne 0) {
        throw "Frozen build failed selftest - bundle is incomplete (see [X] lines above)"
    }
}
finally { Pop-Location }
Write-Host "  Backend frozen + selftest passed: $repoRoot\dist\fpulse\" -ForegroundColor Green

# ── 3. Compile the installer ──
Write-Host ""
Write-Host "  [3/4] Compiling installer with Inno Setup..." -ForegroundColor Yellow
if (-not (Get-Command $InnoPath -ErrorAction SilentlyContinue)) {
    throw "ISCC.exe not on PATH. Install Inno Setup 6 from https://jrsoftware.org/isinfo.php"
}
& $InnoPath "$installerDir\fpulse.iss"
if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed" }
$exe = Get-ChildItem "$installerDir\output\FPulse-Setup-*.exe" | Select-Object -First 1
Write-Host "  Installer built: $($exe.FullName)" -ForegroundColor Green

# ── 4. Optional code signing ──
if ($Sign) {
    Write-Host ""
    Write-Host "  [4/4] Signing the installer..." -ForegroundColor Yellow
    if (-not $CertThumbprint) {
        throw "Pass -CertThumbprint <SHA1> or set FPULSE_WIN_CERT_THUMBPRINT"
    }
    & signtool.exe sign `
        /sha1 $CertThumbprint `
        /tr $TimestampUrl `
        /td sha256 /fd sha256 `
        /d "F-Pulse OSS by Hybridyn Data Labs" `
        $exe.FullName
    if ($LASTEXITCODE -ne 0) { throw "signtool failed" }
    Write-Host "  Signed." -ForegroundColor Green
} else {
    Write-Host ""
    Write-Host "  [4/4] Skipping code-signing (-Sign not passed)." -ForegroundColor DarkGray
    Write-Host "  Unsigned installers trigger SmartScreen warnings on end-user machines." -ForegroundColor DarkGray
}

Write-Host ""
Write-Host "  Done. Output:" -ForegroundColor Cyan
Write-Host "    $($exe.FullName)" -ForegroundColor White
Write-Host ""
