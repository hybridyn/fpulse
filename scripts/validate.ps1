# scripts/validate.ps1
# ────────────────────────────────────────────────────────────────────
# Full release gate. Runs every blocking check a PR has to pass.
# Use this before opening a PR or tagging a release.
#
# Tiers (matches .github/workflows/ci.yml):
#   1. Preflight — check deps exist (npm, python, vitest, pytest, duckdb)
#   2. Frontend  — npm ci (if needed), tsc -b, vitest, vite build
#   3. Backend   — pytest fast gate (markers: not stress, not external)
#   4. Storage   — local-table smoke (DuckDB write + read round-trip)
#   5. Image     — docker compose build (skipped if -SkipDocker)
#
# Exit code 0 only if every step passes. Each step prints its own
# elapsed time so the slow steps are visible without running with -v.
#
# 2026-05-23 (P0 Day 1) — Preflight + Storage smoke added so the
# script fails LOUD on missing dev dependencies instead of running
# half the pipeline and erroring opaquely.
#
# Usage:
#   .\scripts\validate.ps1                  # full gate
#   .\scripts\validate.ps1 -SkipDocker      # skip docker compose build
#   .\scripts\validate.ps1 -InstallMissing  # auto-install missing pip deps
# ────────────────────────────────────────────────────────────────────

param(
    [switch]$SkipDocker,
    [switch]$InstallMissing
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
Push-Location $repoRoot

function Run-Step {
    param([string]$Name, [scriptblock]$Block)
    Write-Host ""
    Write-Host "── $Name ──" -ForegroundColor Cyan
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    try {
        & $Block
        $sw.Stop()
        Write-Host "  ok ($([math]::Round($sw.Elapsed.TotalSeconds, 1))s)" -ForegroundColor Green
    } catch {
        $sw.Stop()
        Write-Host "  FAILED ($([math]::Round($sw.Elapsed.TotalSeconds, 1))s)" -ForegroundColor Red
        throw
    }
}

# Returns $true if a Python import succeeds.
function Test-PyImport {
    param([string]$Module)
    & python -c "import $Module" 2>$null | Out-Null
    return $LASTEXITCODE -eq 0
}

# Returns $true if a Node binary exists under frontend/node_modules.
function Test-NodeBin {
    param([string]$BinName)
    $candidates = @(
        "$repoRoot\frontend\node_modules\.bin\$BinName.cmd",
        "$repoRoot\frontend\node_modules\.bin\$BinName.ps1",
        "$repoRoot\frontend\node_modules\.bin\$BinName"
    )
    foreach ($p in $candidates) {
        if (Test-Path $p) { return $true }
    }
    return $false
}

try {
    # ── 1. Preflight ────────────────────────────────────────────────
    # Surface ALL missing deps at once before running any step. Saves
    # the developer from a 90-second build cycle followed by "pytest
    # not found".
    Run-Step "Preflight: dependency check" {
        $missing = @()

        # Node + npm
        $npmCmd = Get-Command npm.cmd -ErrorAction SilentlyContinue
        if (-not $npmCmd) { $missing += "npm (node toolchain)" }

        # Python
        $pyCmd = Get-Command python -ErrorAction SilentlyContinue
        if (-not $pyCmd) { $missing += "python (3.11+ recommended)" }

        # Frontend deps installed? (node_modules + key binaries)
        $nodeModulesPresent = Test-Path "$repoRoot\frontend\node_modules"
        if (-not $nodeModulesPresent) {
            Write-Host "  note: frontend/node_modules missing — frontend step will run npm ci" -ForegroundColor Yellow
        } else {
            if (-not (Test-NodeBin 'vitest')) { $missing += "vitest (run: cd frontend && npm ci)" }
            if (-not (Test-NodeBin 'vite'))   { $missing += "vite (run: cd frontend && npm ci)" }
            if (-not (Test-NodeBin 'tsc'))    { $missing += "typescript (run: cd frontend && npm ci)" }
        }

        # Backend deps — only check if python is present
        if ($pyCmd) {
            $needPyDeps = @()
            if (-not (Test-PyImport 'pytest'))   { $needPyDeps += 'pytest' }
            if (-not (Test-PyImport 'duckdb'))   { $needPyDeps += 'duckdb' }
            if (-not (Test-PyImport 'fastapi'))  { $needPyDeps += 'fastapi' }
            if (-not (Test-PyImport 'pydantic')) { $needPyDeps += 'pydantic' }

            if ($needPyDeps.Count -gt 0) {
                if ($InstallMissing) {
                    Write-Host "  installing missing Python deps: $($needPyDeps -join ', ')" -ForegroundColor Yellow
                    Push-Location "$repoRoot\backend"
                    python -m pip install -q -r requirements.txt -r requirements-dev.txt 2>&1 | Out-Host
                    if ($LASTEXITCODE -ne 0) { throw "pip install failed" }
                    Pop-Location
                } else {
                    foreach ($d in $needPyDeps) {
                        $missing += "$d (run: cd backend && pip install -r requirements.txt -r requirements-dev.txt, or pass -InstallMissing)"
                    }
                }
            }
        }

        # Docker (only if not skipped)
        if (-not $SkipDocker) {
            $dockerCmd = Get-Command docker -ErrorAction SilentlyContinue
            if (-not $dockerCmd) {
                Write-Host "  note: docker not found — pass -SkipDocker to suppress the image build step" -ForegroundColor Yellow
                $missing += "docker (or use -SkipDocker)"
            }
        }

        if ($missing.Count -gt 0) {
            Write-Host ""
            Write-Host "  Missing dependencies:" -ForegroundColor Red
            foreach ($m in $missing) { Write-Host "    - $m" -ForegroundColor Red }
            throw "preflight failed — install the listed dependencies and rerun"
        }
    }

    # ── 2. Frontend ─────────────────────────────────────────────────
    Run-Step "Frontend: install (npm ci if needed)" {
        Push-Location "$repoRoot\frontend"
        if (-not (Test-Path "node_modules\.package-lock.json") -or -not (Test-NodeBin 'vitest')) {
            npm.cmd ci --no-audit --no-fund 2>&1 | Out-Host
            if ($LASTEXITCODE -ne 0) { throw "npm ci failed" }
        } else {
            Write-Host "  node_modules looks healthy — skipping ci"
        }
        Pop-Location
    }

    Run-Step "Frontend: tsc -b + vite build" {
        Push-Location "$repoRoot\frontend"
        npm.cmd run build 2>&1 | Out-Host
        if ($LASTEXITCODE -ne 0) { throw "frontend build failed" }
        Pop-Location
    }

    Run-Step "Frontend: vitest" {
        Push-Location "$repoRoot\frontend"
        npm.cmd test -- --run 2>&1 | Out-Host
        if ($LASTEXITCODE -ne 0) { throw "vitest failed" }
        Pop-Location
    }

    # ── 3. Backend ──────────────────────────────────────────────────
    Run-Step "Backend: pytest (fast gate)" {
        Push-Location "$repoRoot\backend"
        $env:FPULSE_MODE = 'dev'
        python -m pytest --tb=short -m "not stress and not external" 2>&1 | Out-Host
        if ($LASTEXITCODE -ne 0) { throw "pytest fast gate failed" }
        Pop-Location
    }

    # ── 4. Storage smoke — DuckDB local-table round-trip ────────────
    # Catches the class of bug where the Storage page builds and tests
    # pass but the actual managed-table read/write path fails at the
    # DuckDB layer (missing duckdb extension, sandbox path issue, etc).
    Run-Step "Storage: local-table smoke (DuckDB)" {
        $smokePy = @'
import os, sys, tempfile, uuid
os.environ.setdefault('FPULSE_MODE', 'dev')
import duckdb
tmp = tempfile.mkdtemp(prefix='fpulse-smoke-')
parquet_path = os.path.join(tmp, f'part-000-{uuid.uuid4().hex[:8]}.parquet')
conn = duckdb.connect()
try:
    conn.sql("CREATE TABLE t AS SELECT i AS id, 'row-' || i AS label FROM generate_series(1, 50) tbl(i)")
    conn.sql(f"COPY t TO '{parquet_path}' (FORMAT 'parquet')")
    n = conn.sql(f"SELECT COUNT(*) FROM read_parquet('{parquet_path}')").fetchone()[0]
    assert n == 50, f"expected 50 rows, got {n}"
    cols = conn.sql(f"SELECT * FROM read_parquet('{parquet_path}') LIMIT 0").columns
    assert list(cols) == ['id', 'label'], f"unexpected columns: {cols}"
finally:
    conn.close()
print(f"  wrote + read {n} rows from {parquet_path}")
'@
        python -c $smokePy 2>&1 | Out-Host
        if ($LASTEXITCODE -ne 0) { throw "storage smoke failed — DuckDB local-table round-trip broken" }
    }

    # ── 5. Image (optional) ─────────────────────────────────────────
    if (-not $SkipDocker) {
        Run-Step "Docker: compose build" {
            docker compose build fpulse 2>&1 | Out-Host
            if ($LASTEXITCODE -ne 0) { throw "docker compose build failed" }
        }
    } else {
        Write-Host ""
        Write-Host "── Docker: compose build ──" -ForegroundColor Cyan
        Write-Host "  skipped (-SkipDocker)" -ForegroundColor Yellow
    }

    Write-Host ""
    Write-Host "Validation passed." -ForegroundColor Green
    exit 0
} catch {
    Write-Host ""
    Write-Host "Validation FAILED: $_" -ForegroundColor Red
    Write-Host ""
    Write-Host "Common fixes:" -ForegroundColor Yellow
    Write-Host "  - pytest / duckdb missing:  .\scripts\validate.ps1 -InstallMissing"
    Write-Host "  - vitest missing:           cd frontend && npm ci"
    Write-Host "  - docker not available:     .\scripts\validate.ps1 -SkipDocker"
    exit 1
} finally {
    Pop-Location
}
