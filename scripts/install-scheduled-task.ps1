# install-scheduled-task.ps1
#
# Register F-Pulse as a Windows Scheduled Task that:
#   - starts at user logon
#   - runs hidden (no terminal window)
#   - restarts on failure (1 minute delay, up to 99 retries)
#   - survives terminal close
#
# This is the simplest "runs without restarting" path on Windows.
# Zero extra dependencies — uses only built-in `schtasks` + `Register-ScheduledTask`.
#
# Run from an elevated PowerShell (Run as Administrator):
#
#     PS> .\scripts\install-scheduled-task.ps1
#
# To uninstall:
#     PS> .\scripts\uninstall-scheduled-task.ps1
#
# To check status:
#     PS> Get-ScheduledTask -TaskName "F-Pulse"
#     PS> Get-ScheduledTaskInfo -TaskName "F-Pulse"
#
# To start/stop manually:
#     PS> Start-ScheduledTask  -TaskName "F-Pulse"
#     PS> Stop-ScheduledTask   -TaskName "F-Pulse"

[CmdletBinding()]
param(
    [string]$TaskName = "F-Pulse",
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [switch]$RunNow
)

$ErrorActionPreference = "Stop"

# Verify elevation — Scheduled Task creation needs admin rights.
$currentUser = [Security.Principal.WindowsPrincipal]::new(
    [Security.Principal.WindowsIdentity]::GetCurrent()
)
if (-not $currentUser.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host ""
    Write-Host "  ERROR: This script must run as Administrator." -ForegroundColor Red
    Write-Host "  Right-click PowerShell -> Run as Administrator, then re-run:" -ForegroundColor Red
    Write-Host "    .\scripts\install-scheduled-task.ps1" -ForegroundColor Red
    Write-Host ""
    exit 1
}

# Resolve the path to start.ps1 — that's what the task will execute.
$startScript = Join-Path $ProjectRoot "start.ps1"
if (-not (Test-Path $startScript)) {
    Write-Host "  ERROR: start.ps1 not found at $startScript" -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "  Registering Scheduled Task '$TaskName' ..." -ForegroundColor Cyan
Write-Host "  Project root : $ProjectRoot" -ForegroundColor DarkGray
Write-Host "  Start script : $startScript"  -ForegroundColor DarkGray
Write-Host ""

# If a task with this name already exists, remove it cleanly first.
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "  Removing existing task '$TaskName'..." -ForegroundColor Yellow
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

# The action: launch PowerShell with our start script, no profile, no window.
# -NoProfile avoids surprises from $PROFILE; -WindowStyle Hidden keeps the
# task truly background.
$action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$startScript`"" `
    -WorkingDirectory $ProjectRoot

# Trigger: at user logon. Add -AtStartup if you want it to run before login
# (but then it can't show desktop notifications).
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

# Run as the current interactive user so DuckDB can read your data dir
# and any per-user credentials in localStorage are still accessible.
$principal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME `
    -LogonType Interactive `
    -RunLevel Highest

# Settings: restart on failure, no hard time limit, allow if on battery,
# wake the box if needed, only one instance at a time.
$settings = New-ScheduledTaskSettingsSet `
    -RestartCount 99 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Hours 0) `
    -DontStopIfGoingOnBatteries `
    -AllowStartIfOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew

Register-ScheduledTask `
    -TaskName $TaskName `
    -Description "F-Pulse OSS — local-first data pipeline orchestrator. Auto-starts at logon, restarts on crash." `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings | Out-Null

Write-Host "  Scheduled Task '$TaskName' registered." -ForegroundColor Green
Write-Host ""
Write-Host "  It will start automatically at every Windows logon." -ForegroundColor White
Write-Host "  Crashes restart 1 minute later, up to 99 retries." -ForegroundColor White
Write-Host ""

if ($RunNow) {
    Write-Host "  Starting now (run with -RunNow)..." -ForegroundColor Cyan
    Start-ScheduledTask -TaskName $TaskName
    Start-Sleep -Seconds 2
    Get-ScheduledTaskInfo -TaskName $TaskName | Format-List LastRunTime, LastTaskResult, NumberOfMissedRuns
}

Write-Host "  Useful commands:"                                                 -ForegroundColor Cyan
Write-Host "    Start-ScheduledTask -TaskName $TaskName"                        -ForegroundColor DarkGray
Write-Host "    Stop-ScheduledTask  -TaskName $TaskName"                        -ForegroundColor DarkGray
Write-Host "    Get-ScheduledTaskInfo -TaskName $TaskName"                      -ForegroundColor DarkGray
Write-Host "    Unregister-ScheduledTask -TaskName $TaskName -Confirm:`$false"  -ForegroundColor DarkGray
Write-Host ""
Write-Host "  App URL after start: http://localhost:5174" -ForegroundColor Cyan
Write-Host ""
