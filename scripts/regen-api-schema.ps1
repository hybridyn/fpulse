#requires -Version 5.1
<#
.SYNOPSIS
    Regenerate frontend/src/api/schema.d.ts from the live backend
    OpenAPI document.

.DESCRIPTION
    The end-to-end validation (audit P3) flagged that
    `frontend/src/api/schema.d.ts` is still a stub — there's no
    automated way to keep the frontend API client typed against the
    backend's actual contract, so frontend/backend drift goes
    unnoticed until a request 422s in production.

    This script:
      1. Verifies the backend is reachable at the given URL (default
         http://127.0.0.1:8001).
      2. Fetches /openapi.json from it.
      3. Runs `npx openapi-typescript` on the fetched document and
         writes the generated TS to frontend/src/api/schema.d.ts.
      4. Reports the byte size + endpoint count of the generated
         file so the operator can spot-check.

    Prerequisites:
      * Backend running on the target URL (start with `uvicorn` or
        the bundled launch script).
      * frontend/node_modules contains openapi-typescript. If you
         just regenerated the lockfile via regen-frontend-lockfile.ps1,
         npm install will have pulled it in.

.PARAMETER BackendUrl
    The base URL where the backend is running. Default
    http://127.0.0.1:8001.

.PARAMETER OutputPath
    Override the generated file path. Default
    frontend/src/api/schema.d.ts.
#>

[CmdletBinding()]
param(
    [string]$BackendUrl = 'http://127.0.0.1:8001',
    [string]$OutputPath
)

$ErrorActionPreference = 'Stop'

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$FrontendDir = Join-Path $RepoRoot 'frontend'
if (-not $OutputPath) {
    $OutputPath = Join-Path $FrontendDir 'src\api\schema.d.ts'
}

Write-Host "==> Backend URL: $BackendUrl" -ForegroundColor Cyan
Write-Host "    Output:      $OutputPath" -ForegroundColor Gray

# 1. Verify backend is reachable.
$openapiUrl = "$BackendUrl/openapi.json"
Write-Host "==> Probing $openapiUrl ..." -ForegroundColor Cyan
try {
    $probe = Invoke-WebRequest -Uri $openapiUrl -UseBasicParsing -TimeoutSec 10
    if ($probe.StatusCode -ne 200) {
        Write-Error "Backend probe returned HTTP $($probe.StatusCode)."
        exit 1
    }
} catch {
    Write-Error @"
Backend unreachable at $openapiUrl.

Start the backend first, e.g.:
  cd backend
  .\.venv\Scripts\python -m uvicorn fpulse.main:app --port 8001

Or set -BackendUrl to point at your running instance.

Original error: $_
"@
    exit 1
}

$openapiJsonSize = $probe.Content.Length
Write-Host "==> Got OpenAPI document ($openapiJsonSize bytes)." -ForegroundColor Green

# 2. Verify openapi-typescript is available.
$npmExe = Get-Command npx.cmd -ErrorAction SilentlyContinue
if (-not $npmExe) {
    $npmExe = Get-Command npx -ErrorAction SilentlyContinue
}
if (-not $npmExe) {
    Write-Error "npx not found on PATH. Install Node.js so npx.cmd resolves."
    exit 1
}

# 3. Run openapi-typescript via npx. Working dir = frontend so it
#    picks up the locally-installed openapi-typescript from
#    node_modules/.bin.
Write-Host "==> Generating TypeScript types ..." -ForegroundColor Cyan
Push-Location $FrontendDir
try {
    # Ensure output dir exists
    $outDir = Split-Path $OutputPath -Parent
    if (-not (Test-Path $outDir)) {
        New-Item -ItemType Directory -Path $outDir | Out-Null
    }

    # `openapi-typescript <url> -o <out>` is the canonical invocation.
    & $npmExe.Source openapi-typescript $openapiUrl -o $OutputPath
    $exitCode = $LASTEXITCODE
} finally {
    Pop-Location
}

if ($exitCode -ne 0) {
    Write-Error "openapi-typescript failed (exit $exitCode). Is it installed? Try: cd frontend; npm install openapi-typescript --save-dev"
    exit $exitCode
}

# 4. Spot-check the generated file.
if (-not (Test-Path $OutputPath)) {
    Write-Error "Output file not written — openapi-typescript exited 0 but produced no file."
    exit 1
}
$generated = Get-Item $OutputPath
$contents = Get-Content $OutputPath -Raw
$pathCount = ([regex]::Matches($contents, "\""\/api\/")).Count
Write-Host "" -ForegroundColor Green
Write-Host "==> DONE." -ForegroundColor Green
Write-Host "    Wrote: $($generated.FullName)" -ForegroundColor Green
Write-Host "    Size:  $($generated.Length) bytes" -ForegroundColor Gray
Write-Host "    Approx /api/* path entries: $pathCount" -ForegroundColor Gray
Write-Host "" -ForegroundColor Green
Write-Host "==> Commit the regenerated schema.d.ts after a build + diff review." -ForegroundColor Green
