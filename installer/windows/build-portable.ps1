[CmdletBinding()]
param([string]$Python = "python")
$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path "$PSScriptRoot\..\..").Path
$previous = $env:FPULSE_ONEFILE
Push-Location $repoRoot
try {
    Push-Location frontend
    try {
        npm ci
        if ($LASTEXITCODE -ne 0) { throw "npm ci failed" }
        npm run build
        if ($LASTEXITCODE -ne 0) { throw "Frontend build failed" }
    } finally { Pop-Location }
    foreach ($script in @("scripts/stage_frontend.py", "scripts/stage_docs.py")) {
        & $Python $script
        if ($LASTEXITCODE -ne 0) { throw "Staging failed: $script" }
    }
    & $Python tools/package_preflight.py --strict
    if ($LASTEXITCODE -ne 0) { throw "Packaging preflight failed" }
    $env:FPULSE_ONEFILE = "1"
    & $Python -m PyInstaller installer/windows/fpulse.spec --noconfirm --distpath dist/portable --workpath build/portable
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }
    & $Python tools/smoke_distribution.py --exe dist/portable/fpulse.exe
    if ($LASTEXITCODE -ne 0) { throw "Portable smoke test failed" }
    Get-FileHash dist/portable/fpulse.exe -Algorithm SHA256
} finally {
    $env:FPULSE_ONEFILE = $previous
    Pop-Location
}
