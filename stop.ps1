# F-Pulse stop script (v2, 2026-06-06)
#
# Usage:
#   .\stop.ps1
#
# What it does:
#   1. Reads .fpulse\runtime\instance.json (the ownership file written
#      by start.ps1 when this checkout last booted).
#   2. For each recorded PID, runs the 3-signal ownership check in
#      launcher\launcher-utils.ps1 (PID alive + still on recorded port
#      + cmdline still matches uvicorn/vite signature). All three must
#      pass.
#   3. Stops only those PIDs. Anything else - even processes on
#      5174/8001 - is left alone.
#   4. Removes the runtime file.
#
# This script CANNOT kill a process that wasn't started by this
# checkout's start.ps1. If you want to free a port held by something
# else, stop that something else yourself (see what it is with:
# Get-NetTCPConnection -LocalPort PORT | Get-Process).

$ErrorActionPreference = 'Stop'
$ROOT = $PSScriptRoot
. (Join-Path $ROOT 'launcher\launcher-utils.ps1')

Write-Host ""
Write-Host "  F-Pulse - clean shutdown" -ForegroundColor Cyan
Write-Host "  ========================" -ForegroundColor DarkCyan
Write-Host ""

$prev = Read-RuntimeFile -RepoRoot $ROOT
if ($null -eq $prev) {
    Write-LauncherDim "  No F-Pulse instance is recorded as running."
    Write-LauncherDim "  (Runtime file .fpulse\runtime\instance.json doesn't exist.)"
    Write-Host ""
    Write-LauncherDim "  If you see a port held by something else, that's a foreign"
    Write-LauncherDim "  process - we won't touch it. To inspect:"
    Write-LauncherDim "    Get-NetTCPConnection -LocalPort 5174 | Get-Process"
    Write-Host ""
    exit 0
}

Write-LauncherInfo "  Found recorded instance: $($prev.instance_id)"
Write-LauncherDim   "  Started: $($prev.started_at)"
Write-LauncherDim   "  Ports:   frontend=$($prev.frontend_port), backend=$($prev.backend_port)"
Write-Host ""

$stoppedAny = $false
$skippedAny = $false

# Backend
if ($prev.backend_pid -gt 0) {
    $ok = Stop-OwnedProcess -ProcessId $prev.backend_pid -ExpectedPort $prev.backend_port -Kind 'backend' -RepoRoot $ROOT
    if ($ok) {
        Write-LauncherOk "  Stopped backend  (PID $($prev.backend_pid), port $($prev.backend_port))"
        $stoppedAny = $true
    } else {
        Write-LauncherDim "  Backend PID $($prev.backend_pid) is no longer ours (PID recycled, died on its own, or signature changed). Skipping."
        $skippedAny = $true
    }
}

# Frontend
if ($prev.frontend_pid -gt 0) {
    $ok = Stop-OwnedProcess -ProcessId $prev.frontend_pid -ExpectedPort $prev.frontend_port -Kind 'frontend' -RepoRoot $ROOT
    if ($ok) {
        Write-LauncherOk "  Stopped frontend (PID $($prev.frontend_pid), port $($prev.frontend_port))"
        $stoppedAny = $true
    } else {
        Write-LauncherDim "  Frontend PID $($prev.frontend_pid) is no longer ours (PID recycled, died on its own, or signature changed). Skipping."
        $skippedAny = $true
    }
}

# Remove the runtime file - whether we stopped anything or not, the
# previously-recorded instance is no longer trustworthy.
Remove-RuntimeFile -RepoRoot $ROOT

Write-Host ""
if ($stoppedAny) {
    Write-LauncherOk "  Done. You can re-run start.bat / start.ps1 anytime."
} else {
    Write-LauncherDim "  Nothing to stop - all recorded processes had already exited."
}
if ($skippedAny) {
    Write-Host ""
    Write-LauncherDim "  Note: some recorded PIDs were skipped because the 3-signal"
    Write-LauncherDim "  ownership check (PID alive + still on recorded port + cmdline"
    Write-LauncherDim "  matches uvicorn/vite signature) didn't all pass. This is the"
    Write-LauncherDim "  safety mechanism that prevents accidentally killing recycled"
    Write-LauncherDim "  PIDs or unrelated apps."
}
Write-Host ""
