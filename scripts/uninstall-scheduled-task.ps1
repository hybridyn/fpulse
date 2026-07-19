# uninstall-scheduled-task.ps1
#
# Stop and unregister the F-Pulse Scheduled Task. Doesn't touch
# the project files, the data dir, or the venv.
#
# Run from an elevated PowerShell:
#
#     PS> .\scripts\uninstall-scheduled-task.ps1

[CmdletBinding()]
param([string]$TaskName = "F-Pulse")

$ErrorActionPreference = "Stop"

$currentUser = [Security.Principal.WindowsPrincipal]::new(
    [Security.Principal.WindowsIdentity]::GetCurrent()
)
if (-not $currentUser.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "  ERROR: must run as Administrator." -ForegroundColor Red
    exit 1
}

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $existing) {
    Write-Host "  Task '$TaskName' is not registered. Nothing to do." -ForegroundColor Yellow
    exit 0
}

Write-Host "  Stopping '$TaskName' (if running)..." -ForegroundColor Cyan
try { Stop-ScheduledTask -TaskName $TaskName -ErrorAction Stop } catch { }

Write-Host "  Unregistering '$TaskName'..." -ForegroundColor Cyan
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false

Write-Host "  Done. F-Pulse no longer auto-starts at logon." -ForegroundColor Green
Write-Host "  Project files, data dir, and venv are untouched." -ForegroundColor DarkGray
