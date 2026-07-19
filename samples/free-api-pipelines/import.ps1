# Imports the 10 sample pipelines into a running F-Pulse OSS instance,
# wiring them to an existing SQL Server connection.
#
# Usage:
#   1. Make sure F-Pulse OSS backend is running (default: http://localhost:8001)
#   2. Run:   .\import.ps1
#      You'll be prompted for your F-Pulse email + password (the same one
#      you log into the UI with).
#   3. Or pass them inline:
#         .\import.ps1 -Email admin@example.com -Password 'yourpwd'
#
# The script logs into /api/auth/login, gets a bearer token, finds the
# workspace that owns 'fpulse_test', then POSTs the 10 pipelines into it.

[CmdletBinding()]
param(
    [string]$BaseUrl        = "http://localhost:8001",
    [string]$Email          = "",
    [string]$Password       = "",
    [string]$Token          = "",
    [string]$ConnectionName = "fpulse_test",
    [string]$ConnectionId   = ""
)

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path

Write-Host "F-Pulse OSS sample importer" -ForegroundColor Cyan
Write-Host "  base url      : $BaseUrl"
if ($ConnectionId) {
    Write-Host "  target conn   : id=$ConnectionId"
} else {
    Write-Host "  target conn   : name=$ConnectionName"
}
Write-Host ""

# ---- 0. Get a session token ------------------------------------------------
# Three modes, in order of preference:
#   1. -Token <value>   : reuse a token from your browser session
#   2. -Email/-Password : log in via /api/auth/login
#   3. interactive prompt
$token = $Token
$workspaces = @()

if ($token) {
    Write-Host "[0/3] Using token supplied via -Token" -ForegroundColor Yellow
    # Validate the token and pull workspaces via /me
    try {
        $me = Invoke-RestMethod -Method GET -Uri "$BaseUrl/api/auth/me" `
            -Headers @{ "Authorization" = "Bearer $token" } -TimeoutSec 15
        Write-Host "  OK: token valid for $($me.email)" -ForegroundColor Green
        if ($me.workspaces) { $workspaces = @($me.workspaces) }
    } catch {
        Write-Host "  WARN: /api/auth/me check failed ($($_.Exception.Message)). Continuing anyway." -ForegroundColor Yellow
    }
    if (-not $workspaces -or $workspaces.Count -eq 0) {
        # Fallback: assume a single 'default' workspace
        $workspaces = @(@{ id = "default"; name = "Default" })
    }
} else {
    if (-not $Email) {
        $Email = Read-Host "F-Pulse email"
    }
    if (-not $Password) {
        $secure = Read-Host "F-Pulse password" -AsSecureString
        $bstr = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
        $Password = [System.Runtime.InteropServices.Marshal]::PtrToStringAuto($bstr)
    }

    Write-Host ""
    Write-Host "[0/3] Logging in as $Email ..." -ForegroundColor Yellow
    $loginBody = @{ email = $Email; password = $Password } | ConvertTo-Json
    try {
        $login = Invoke-RestMethod -Method POST -Uri "$BaseUrl/api/auth/login" `
            -Headers @{ "Content-Type" = "application/json" } `
            -Body $loginBody -TimeoutSec 15
    } catch {
        Write-Host "  ERROR: login failed -- $($_.Exception.Message)" -ForegroundColor Red
        Write-Host "  Alternative: grab the token from your browser DevTools and pass" -ForegroundColor Yellow
        Write-Host "  it via .\import.ps1 -Token <value>" -ForegroundColor Yellow
        exit 1
    }
    $token = $login.token
    if (-not $token) {
        Write-Host "  ERROR: login response did not include a token." -ForegroundColor Red
        exit 1
    }
    Write-Host "  OK: logged in as $($login.user.email), tier=$($login.tier)" -ForegroundColor Green
    $workspaces = @($login.workspaces)
}

Write-Host "  workspaces visible: $($workspaces.Count)"

# Auth-bearing headers for all subsequent calls
$authHeaders = @{
    "Content-Type"  = "application/json"
    "Authorization" = "Bearer $token"
}

# ---- 1. Find the workspace that owns fpulse_test ---------------------------
Write-Host ""
Write-Host "[1/3] Locating '$ConnectionName' across visible workspaces..." -ForegroundColor Yellow

$match = $null
$matchWorkspace = $null

foreach ($w in $workspaces) {
    $wid = $w.id
    $headersScoped = @{
        "Content-Type"   = "application/json"
        "Authorization"  = "Bearer $token"
        "X-Workspace-Id" = $wid
    }
    try {
        $conns = Invoke-RestMethod -Method GET -Uri "$BaseUrl/api/connections" -Headers $headersScoped -TimeoutSec 15
    } catch {
        Write-Host ("  workspace {0,-20} ({1}) -- {2}" -f $w.name, $wid, $_.Exception.Message) -ForegroundColor Yellow
        continue
    }

    if ($conns -is [System.Collections.IEnumerable] -and -not ($conns -is [string])) {
        $list = @($conns)
    } elseif ($conns.connections) {
        $list = @($conns.connections)
    } else {
        $list = @($conns)
    }

    if ($ConnectionId) {
        $found = $list | Where-Object { $_.id -eq $ConnectionId } | Select-Object -First 1
    } else {
        $found = $list | Where-Object { $_.id -eq $ConnectionName } | Select-Object -First 1
        if (-not $found) {
            $found = $list | Where-Object { $_.name -ieq $ConnectionName } | Select-Object -First 1
        }
    }
    Write-Host ("  workspace {0,-20} ({1}): {2} connections" -f $w.name, $wid, $list.Count)
    if ($found) {
        $match = $found
        $matchWorkspace = $w
        break
    }
}

if (-not $match) {
    Write-Host ""
    Write-Host "  ERROR: '$ConnectionName' not found in any visible workspace." -ForegroundColor Red
    Write-Host "  Check the Connections page in the UI; copy the exact name or id and re-run with:" -ForegroundColor Red
    Write-Host "    .\import.ps1 -ConnectionId <id>" -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "  OK: found connection." -ForegroundColor Green
Write-Host ("       id        = {0}" -f $match.id) -ForegroundColor Green
Write-Host ("       name      = {0}" -f $match.name) -ForegroundColor Green
Write-Host ("       type      = {0}" -f $match.type) -ForegroundColor Green
Write-Host ("       workspace = {0} ({1})" -f $matchWorkspace.name, $matchWorkspace.id) -ForegroundColor Green

if ($match.type -ne "mssql") {
    Write-Host "  WARNING: connection type is '$($match.type)', not 'mssql'. Pipelines may fail." -ForegroundColor Yellow
    Start-Sleep -Seconds 2
}

$realId = $match.id
$realWorkspaceId = $matchWorkspace.id

# ---- 2. Import pipelines ---------------------------------------------------
$pipeDir = Join-Path $here "pipelines"
$files = Get-ChildItem -LiteralPath $pipeDir -Filter "*.json" | Sort-Object Name
Write-Host ""
Write-Host "[2/3] Importing $($files.Count) pipelines into workspace '$($matchWorkspace.name)'..." -ForegroundColor Yellow
Write-Host "       (swapping placeholder conn_mssql_prod -> $realId)"
Write-Host "       (swapping placeholder workspace_id 'default' -> $realWorkspaceId)"

$wfHeaders = @{
    "Content-Type"   = "application/json"
    "Authorization"  = "Bearer $token"
    "X-Workspace-Id" = $realWorkspaceId
}

$ok = 0; $fail = 0
foreach ($f in $files) {
    $raw = Get-Content -Raw -LiteralPath $f.FullName
    $body = $raw -replace "conn_mssql_prod", $realId
    # Pin workspace_id inside the payload too, in case the API uses body over header
    $body = $body -replace '"workspace_id"\s*:\s*"default"', ('"workspace_id":"' + $realWorkspaceId + '"')

    try {
        $resp = Invoke-RestMethod -Method POST -Uri "$BaseUrl/api/workflows" -Headers $wfHeaders -Body $body -TimeoutSec 30
        Write-Host "  + $($f.Name)" -ForegroundColor Green
        $ok++
    } catch {
        Write-Host "  ! $($f.Name) -- $($_.Exception.Message)" -ForegroundColor Red
        $fail++
    }
}

# ---- 3. Summary ------------------------------------------------------------
Write-Host ""
Write-Host "[3/3] Done. $ok imported, $fail failed." -ForegroundColor Cyan
if ($ok -gt 0) {
    Write-Host "Open the UI > Workflows and find pipelines starting with '01 - JSONPlaceholder Posts'." -ForegroundColor Cyan
}
