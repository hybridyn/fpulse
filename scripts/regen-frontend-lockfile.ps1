#requires -Version 5.1
<#
.SYNOPSIS
    Regenerate frontend/package-lock.json so it matches package.json.

.DESCRIPTION
    The end-to-end validation pass (docs/END_TO_END_VALIDATION_2026_05_22.md,
    audit P1) flagged that frontend/package.json is ahead of
    frontend/package-lock.json — package.json declares vitest,
    @testing-library/react, @testing-library/jest-dom, jsdom, and
    openapi-typescript but the lockfile root version is still 0.1.0
    and missing those entries. `npm ci` fails as a result, which
    breaks CI and any fresh developer setup.

    This script:
      1. Backs up the existing package-lock.json (best-effort).
      2. Deletes the stale lockfile.
      3. Runs `npm install --no-audit --no-fund` to regenerate it.
      4. Verifies the four missing deps are now present in node_modules.
      5. Reports the regenerated lock's root version (should match
         package.json's version).

    Run from the repo root, or `cd scripts; .\regen-frontend-lockfile.ps1`.

    The operator commits the regenerated package-lock.json after the
    script reports success.

.NOTES
    Does NOT touch package.json. The lockfile is the single artifact
    that gets regenerated. If npm install fails for a non-lockfile
    reason (e.g. a transitive dep removed upstream), the backup is
    restored.
#>

[CmdletBinding()]
param(
    [string]$FrontendDir
)

$ErrorActionPreference = 'Stop'

# Resolve the frontend dir relative to the script location.
if (-not $FrontendDir) {
    $FrontendDir = Join-Path $PSScriptRoot '..\frontend'
}
$FrontendDir = (Resolve-Path -Path $FrontendDir).Path

$pkgPath = Join-Path $FrontendDir 'package.json'
$lockPath = Join-Path $FrontendDir 'package-lock.json'
$nodeModules = Join-Path $FrontendDir 'node_modules'

if (-not (Test-Path $pkgPath)) {
    Write-Error "package.json not found at $pkgPath"
    exit 1
}

Write-Host "==> Frontend dir: $FrontendDir" -ForegroundColor Cyan

# Read package.json version + the four expected dev deps so we can
# verify post-install.
$pkg = Get-Content $pkgPath -Raw | ConvertFrom-Json
$expectedVersion = $pkg.version
$expectedDevDeps = @(
    'vitest',
    '@testing-library/react',
    '@testing-library/jest-dom',
    'jsdom',
    'openapi-typescript'
)

Write-Host "    package.json version: $expectedVersion" -ForegroundColor Gray
Write-Host "    expected dev deps:" -ForegroundColor Gray
foreach ($d in $expectedDevDeps) { Write-Host "      - $d" -ForegroundColor Gray }

# 1. Back up the existing lockfile.
$backupPath = $null
if (Test-Path $lockPath) {
    $backupPath = "$lockPath.bak.$(Get-Date -Format yyyyMMdd-HHmmss)"
    Copy-Item -Path $lockPath -Destination $backupPath -Force
    Write-Host "==> Backed up old lockfile to: $backupPath" -ForegroundColor Cyan
}

# 2. Delete the stale lockfile.
if (Test-Path $lockPath) {
    Remove-Item -Path $lockPath -Force
    Write-Host "==> Deleted stale package-lock.json" -ForegroundColor Cyan
}

# 3. Run npm install. We DON'T touch node_modules — npm install will
#    reconcile what's there with the new lockfile. If you want a fully
#    clean install, delete node_modules manually and re-run.
Write-Host "==> Running: npm install --no-audit --no-fund" -ForegroundColor Cyan
Push-Location $FrontendDir
try {
    # Use npm.cmd on Windows. PowerShell calls cmd shim correctly.
    & npm.cmd install --no-audit --no-fund
    $exitCode = $LASTEXITCODE
} finally {
    Pop-Location
}

if ($exitCode -ne 0) {
    Write-Host "==> npm install failed (exit $exitCode). Restoring backup..." -ForegroundColor Red
    if ($backupPath -and (Test-Path $backupPath)) {
        Copy-Item -Path $backupPath -Destination $lockPath -Force
        Write-Host "    Restored $lockPath from $backupPath" -ForegroundColor Yellow
    }
    Write-Error "npm install failed — lockfile NOT regenerated. See npm output above."
    exit $exitCode
}

# 4. Verify the four missing deps are now present.
$missing = @()
foreach ($d in $expectedDevDeps) {
    $depDir = Join-Path $nodeModules $d
    if (-not (Test-Path $depDir)) { $missing += $d }
}
if ($missing.Count -gt 0) {
    Write-Host "==> WARNING: these expected dev deps are still missing from node_modules:" -ForegroundColor Yellow
    foreach ($m in $missing) { Write-Host "      - $m" -ForegroundColor Yellow }
    Write-Host "    Check package.json devDependencies and re-run." -ForegroundColor Yellow
} else {
    Write-Host "==> All expected dev deps present in node_modules." -ForegroundColor Green
}

# 5. Report regenerated lock's root version.
if (Test-Path $lockPath) {
    $lock = Get-Content $lockPath -Raw | ConvertFrom-Json
    $lockVersion = $lock.version
    Write-Host "==> Regenerated package-lock.json version: $lockVersion" -ForegroundColor Cyan
    if ($lockVersion -ne $expectedVersion) {
        Write-Host "    WARNING: lock version $lockVersion != package.json version $expectedVersion" -ForegroundColor Yellow
    }
}

Write-Host "" -ForegroundColor Green
Write-Host "==> DONE. Verify with:" -ForegroundColor Green
Write-Host "      cd $FrontendDir" -ForegroundColor Gray
Write-Host "      npm.cmd ci --no-audit --no-fund" -ForegroundColor Gray
Write-Host "      npm.cmd test -- --run" -ForegroundColor Gray
Write-Host "      npm.cmd run build" -ForegroundColor Gray
Write-Host "==> Commit the regenerated package-lock.json after the three commands succeed." -ForegroundColor Green
