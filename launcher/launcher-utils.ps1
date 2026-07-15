# F-Pulse launcher shared utilities (PowerShell).
#
# Dot-sourced by start.ps1 and stop.ps1.  Provides:
#   - Find-FreePort       : true socket-listen probe (no netstat regex)
#   - Read-RuntimeFile    : load .fpulse/runtime/instance.json
#   - Write-RuntimeFile   : atomic write to that file
#   - Test-OwnedFPulse    : THREE-signal ownership check (PID + port + cmdline)
#   - Stop-OwnedProcess   : kill ONLY if Test-OwnedFPulse returns $true
#   - Get-RuntimeDir / Get-RuntimePath
#
# Design rule (pinned during v2 port-conflict audit, 2026-06-06):
#
#   F-Pulse stops a process if and only if ALL three signals agree:
#     (1) PID is in the launcher's own ownership file
#     (2) PID is currently listening on the port we recorded for it
#     (3) PID's current command line still matches the expected signature
#
# Why three signals: PID alone is recyclable, port alone matches any new
# process on that port, cmdline alone false-positives on unrelated Vite
# / Python processes.  Together they uniquely identify a process this
# launcher started in this checkout.

# --- paths ---

function Get-RuntimeDir {
    param([string]$RepoRoot)
    return (Join-Path $RepoRoot ".fpulse\runtime")
}

function Get-RuntimePath {
    param([string]$RepoRoot)
    return (Join-Path (Get-RuntimeDir $RepoRoot) "instance.json")
}

# --- port probing ---

function Find-FreePort {
    # Try $Preferred first; if busy, scan upward up to $Range ports.
    # Returns the chosen port, or throws if none free in range.
    param(
        [int]$Preferred,
        [int]$Range = 30
    )
    for ($p = $Preferred; $p -lt ($Preferred + $Range); $p++) {
        if (Test-PortFree -Port $p) { return $p }
    }
    throw "No free port found in range $Preferred..$($Preferred + $Range - 1)"
}

function Test-PortFree {
    # True if we can momentarily bind a TCP listener on loopback for $Port.
    # This is more reliable than netstat regex because it asks the kernel.
    param([int]$Port)
    $listener = $null
    try {
        $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, $Port)
        $listener.Start()
        return $true
    } catch {
        return $false
    } finally {
        if ($null -ne $listener) {
            try { $listener.Stop() } catch {}
        }
    }
}

function Get-PortHolder {
    # Returns PID listening on $Port, or 0 if none.
    # Uses Get-NetTCPConnection where available (Win 8+), netstat fallback.
    param([int]$Port)
    try {
        $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop | Select-Object -First 1
        if ($conn) { return [int]$conn.OwningProcess }
    } catch {
        # Fallback for older Windows.
        $hit = & netstat -ano | Select-String -Pattern ":$Port\s.*LISTENING" | Select-Object -First 1
        if ($hit) {
            $parts = ($hit.Line.Trim() -split '\s+')
            return [int]$parts[-1]
        }
    }
    return 0
}

# --- runtime file I/O ---

function Read-RuntimeFile {
    # Returns a PSCustomObject or $null.
    param([string]$RepoRoot)
    $path = Get-RuntimePath $RepoRoot
    if (-not (Test-Path $path)) { return $null }
    try {
        $raw = Get-Content -Path $path -Raw -Encoding UTF8
        return ($raw | ConvertFrom-Json)
    } catch {
        Write-Warning "Runtime file $path is corrupt: $($_.Exception.Message). Ignoring."
        return $null
    }
}

function Write-RuntimeFile {
    # Atomic-ish write to .fpulse/runtime/instance.json.
    param(
        [string]$RepoRoot,
        [hashtable]$Instance
    )
    $dir = Get-RuntimeDir $RepoRoot
    if (-not (Test-Path $dir)) {
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
    }
    $path = Get-RuntimePath $RepoRoot
    $tmp  = "$path.tmp"
    $json = $Instance | ConvertTo-Json -Depth 5
    # Force UTF-8 without BOM (project standard - see feedback_powershell_utf8.md).
    [System.IO.File]::WriteAllText($tmp, $json, [System.Text.UTF8Encoding]::new($false))
    Move-Item -Path $tmp -Destination $path -Force
}

function Remove-RuntimeFile {
    param([string]$RepoRoot)
    $path = Get-RuntimePath $RepoRoot
    if (Test-Path $path) { Remove-Item -Path $path -Force }
}

# --- ownership check (the three signals) ---

# Per-kind cmdline signature.
#
#  backend  : require BOTH "uvicorn" AND "fpulse.main" in the cmdline.
#             That combo is specific to F-Pulse - no other project ships
#             a Python module path called "fpulse.main".
#
#  frontend : "vite" / "npm" / "node" alone match ANY developer's dev
#             server. To prove ownership we ALSO require the absolute
#             repo-root path to appear in the cmdline. That happens
#             naturally because vite is launched via the absolute path
#             "<repo>/frontend/node_modules/vite/bin/vite.js".
function Test-CmdlineMatches {
    param(
        [string]$Kind,        # 'backend' or 'frontend'
        [string]$CommandLine,
        [string]$RepoRoot
    )
    if ([string]::IsNullOrEmpty($CommandLine)) { return $false }
    $cl = $CommandLine.ToLowerInvariant()
    $rrSlash = $RepoRoot.ToLowerInvariant().Replace('\', '/')
    $clSlash = $cl.Replace('\', '/')
    switch ($Kind) {
        'backend' {
            return ($cl -match 'uvicorn' -and $cl -match 'fpulse\.main')
        }
        'frontend' {
            # Repo path REQUIRED for frontend (no exception). "vite" alone
            # is way too generic - the path is the ownership signal.
            if (-not $clSlash.Contains($rrSlash)) { return $false }
            return ($cl -match 'vite' -or $cl -match 'npm' -or $cl -match 'node')
        }
        default { return $false }
    }
}

function Test-OwnedFPulse {
    # THREE-signal ownership check.
    # Returns $true ONLY if every signal agrees:
    #   (1) PID is alive
    #   (2) PID is currently listening on $ExpectedPort
    #   (3) PID's cmdline - or some ancestor's cmdline within 5 hops -
    #       matches the kind+repo signature
    #
    # Signal 3 walks ANCESTORS (parents up the tree) because the listener
    # is the leaf process (node.exe running vite, python.exe running
    # uvicorn). Wrappers (cmd /c, npm) sit further up the chain. As long
    # as ONE of them carries the repo path + kind keyword, we accept it.
    param(
        [int]$ProcessId,
        [int]$ExpectedPort,
        [string]$Kind,        # 'backend' or 'frontend'
        [string]$RepoRoot
    )
    # Signal 1: PID alive?
    $proc = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    if ($null -eq $proc) { return $false }

    # Signal 2: PID currently listening on the expected port?
    $holder = Get-PortHolder -Port $ExpectedPort
    if ($holder -ne $ProcessId) { return $false }

    # Signal 3: cmdline (or any ancestor's cmdline within 5 hops) matches.
    $current = $ProcessId
    for ($i = 0; $i -lt 5; $i++) {
        $cim = Get-CimInstance Win32_Process -Filter "ProcessId=$current" -ErrorAction SilentlyContinue
        if ($null -eq $cim) { break }
        if (Test-CmdlineMatches -Kind $Kind -CommandLine $cim.CommandLine -RepoRoot $RepoRoot) {
            return $true
        }
        if ($cim.ParentProcessId -eq 0 -or [int]$cim.ParentProcessId -eq $current) { break }
        $current = [int]$cim.ParentProcessId
    }
    return $false
}

function Stop-OwnedProcess {
    # Gatekeeper: NEVER kills unless Test-OwnedFPulse returns $true.
    # Also walks one level of children so cmd-hosting-python gets cleaned up.
    param(
        [int]$ProcessId,
        [int]$ExpectedPort,
        [string]$Kind,
        [string]$RepoRoot
    )
    if (-not (Test-OwnedFPulse -ProcessId $ProcessId -ExpectedPort $ExpectedPort -Kind $Kind -RepoRoot $RepoRoot)) {
        return $false
    }
    # Kill children first so they don't get reparented to PID 1.
    $kids = Get-CimInstance Win32_Process -Filter "ParentProcessId=$ProcessId" -ErrorAction SilentlyContinue
    foreach ($k in $kids) {
        Stop-Process -Id $k.ProcessId -Force -ErrorAction SilentlyContinue
    }
    Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue
    return $true
}

# --- discover PID after spawn (we just spawned, we own whatever's on our port) ---

function Wait-ForPortBinding {
    # Poll the kernel until $Port is bound, return the holding PID.
    # Returns 0 on timeout. Used right after spawning child to capture
    # the PID we just created (whoever's on our chosen port within 15s
    # is necessarily our child, since we verified the port was free
    # immediately before spawning).
    param(
        [int]$Port,
        [int]$TimeoutSeconds = 20
    )
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        # NOTE: never name a local "$pid" - that's a PowerShell auto-var
        # for the current PowerShell process ID. Use $holder instead.
        $holder = Get-PortHolder -Port $Port
        if ($holder -gt 0) { return $holder }
        Start-Sleep -Milliseconds 250
    }
    return 0
}

# --- helpers for user-facing output ---

function Write-LauncherInfo  { param([string]$Msg) Write-Host "  $Msg" -ForegroundColor Cyan }
function Write-LauncherOk    { param([string]$Msg) Write-Host "  $Msg" -ForegroundColor Green }
function Write-LauncherWarn  { param([string]$Msg) Write-Host "  $Msg" -ForegroundColor Yellow }
function Write-LauncherError { param([string]$Msg) Write-Host "  $Msg" -ForegroundColor Red }
function Write-LauncherDim   { param([string]$Msg) Write-Host "  $Msg" -ForegroundColor DarkGray }
