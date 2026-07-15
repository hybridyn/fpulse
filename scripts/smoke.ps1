# scripts/smoke.ps1
# ────────────────────────────────────────────────────────────────────
# 5-minute health check against a running F-Pulse backend.
#
# Run after `docker compose up -d` (or against the native uvicorn dev
# server). Hits the endpoints that have to work for any other check to
# be meaningful — health, readiness, node catalog, connector matrix.
#
# Does NOT run the full pytest / build pipeline. For that use:
#   scripts/validate.ps1
#
# Defaults assume http://127.0.0.1:8001. Override:
#   .\scripts\smoke.ps1 -BaseUrl http://localhost:5174
# ────────────────────────────────────────────────────────────────────

param(
    [string]$BaseUrl = 'http://127.0.0.1:8001'
)

$ErrorActionPreference = 'Stop'
$failed = @()

function Probe {
    param([string]$Path, [scriptblock]$Assert)
    Write-Host -NoNewline "  $Path ... "
    try {
        $resp = Invoke-WebRequest -Uri "$BaseUrl$Path" -UseBasicParsing -TimeoutSec 5
        $body = $resp.Content | ConvertFrom-Json -ErrorAction Stop
        & $Assert $resp $body
        Write-Host "ok" -ForegroundColor Green
    } catch {
        Write-Host "FAIL ($_)" -ForegroundColor Red
        $script:failed += $Path
    }
}

Write-Host "Smoke-testing $BaseUrl"
Write-Host ""

Probe '/api/health' {
    param($resp, $body)
    if ($resp.StatusCode -ne 200) { throw "status $($resp.StatusCode)" }
    if ($body.status -ne 'ok') { throw "status field = $($body.status)" }
}

Probe '/api/health/ready' {
    param($resp, $body)
    if ($resp.StatusCode -ne 200) { throw "status $($resp.StatusCode)" }
}

Probe '/api/node-types' {
    param($resp, $body)
    if ($resp.StatusCode -ne 200) { throw "status $($resp.StatusCode)" }
    # Body shape varies — could be { types: [...] } or a bare array.
    $count = if ($body -is [System.Array]) { $body.Length } elseif ($body.types) { $body.types.Length } else { 0 }
    if ($count -lt 1) { throw "no node types returned" }
}

Probe '/api/connectors/cert-matrix' {
    param($resp, $body)
    if ($resp.StatusCode -ne 200) { throw "status $($resp.StatusCode)" }
    # Don't gate on count — just that the endpoint resolves to JSON.
}

Write-Host ""
if ($failed.Count -eq 0) {
    Write-Host "All smoke checks passed." -ForegroundColor Green
    exit 0
} else {
    Write-Host "Failed: $($failed -join ', ')" -ForegroundColor Red
    Write-Host "  Likely causes:"
    Write-Host "    - backend not running (try: cd backend; uvicorn fpulse.main:app --port 8001)"
    Write-Host "    - lifespan failed (check backend logs for the 'Backup:' line)"
    Write-Host "    - wrong port (override via -BaseUrl)"
    exit 1
}
