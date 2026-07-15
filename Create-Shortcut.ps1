# Create-Shortcut.ps1 - add a branded "F-Pulse" shortcut so users can
# launch the app with one double-click (2026-06-08).
#
# Run once from the repo root:
#     .\Create-Shortcut.ps1
#
# Creates a "F-Pulse" icon (with the logo) on your Desktop and in the
# Start Menu, pointing at the local launcher (start.bat). Double-clicking
# it starts F-Pulse and opens the UI in your browser.
#
# Remove the shortcuts again:
#     .\Create-Shortcut.ps1 -Remove
#
# Options: -NoDesktop / -NoStartMenu to skip either location.
#
# OneDrive note: when OneDrive backs up the Desktop, a machine can have
# BOTH %USERPROFILE%\Desktop and %OneDrive%\Desktop, and which one the
# user actually sees varies. We write to EVERY real Desktop folder so the
# icon shows up wherever their desktop truly is (the early "where is it?"
# bug came from writing to only one of them).
#
# NOTE: kept ASCII-only on purpose. Windows PowerShell 5.1 reads a
# BOM-less script as ANSI, so a stray non-ASCII char in a string literal
# can corrupt into a phantom quote and break parsing.
[CmdletBinding()]
param(
    [switch]$Remove,
    [switch]$NoDesktop,
    [switch]$NoStartMenu
)
$ErrorActionPreference = 'Stop'
$ROOT = $PSScriptRoot

$target  = Join-Path $ROOT 'start.bat'
$icon    = Join-Path $ROOT 'installer\windows\icons\fpulse.ico'
$lnkName = 'F-Pulse.lnk'

# Every real Desktop folder on this profile (deduped, case-insensitive,
# only ones that actually exist - we never create a phantom Desktop).
function Get-DesktopDirs {
    $dirs = New-Object System.Collections.Generic.List[string]
    $candidates = @(
        [Environment]::GetFolderPath('Desktop'),
        (Join-Path $env:USERPROFILE 'Desktop')
    )
    if ($env:OneDrive)           { $candidates += (Join-Path $env:OneDrive 'Desktop') }
    if ($env:OneDriveConsumer)   { $candidates += (Join-Path $env:OneDriveConsumer 'Desktop') }
    if ($env:OneDriveCommercial) { $candidates += (Join-Path $env:OneDriveCommercial 'Desktop') }
    foreach ($d in $candidates) {
        if ($d -and (Test-Path $d) -and ($dirs -notcontains $d)) { $dirs.Add($d) }
    }
    return $dirs
}

$desktopDirs  = if ($NoDesktop) { @() } else { Get-DesktopDirs }
$startMenuDir = Join-Path ([Environment]::GetFolderPath('Programs')) 'F-Pulse'

$targets = @()
foreach ($d in $desktopDirs) { $targets += (Join-Path $d $lnkName) }
if (-not $NoStartMenu) { $targets += (Join-Path $startMenuDir $lnkName) }

if ($Remove) {
    foreach ($p in $targets) {
        if (Test-Path $p) { Remove-Item $p -Force; Write-Host "Removed $p" }
    }
    if ((-not $NoStartMenu) -and (Test-Path $startMenuDir)) {
        Remove-Item $startMenuDir -Recurse -Force -ErrorAction SilentlyContinue
    }
    Write-Host "Done. F-Pulse shortcuts removed." -ForegroundColor Green
    return
}

if (-not (Test-Path $target)) {
    throw "start.bat not found at '$target'. Run this from the F-Pulse repo root."
}
if (-not $NoStartMenu -and -not (Test-Path $startMenuDir)) {
    New-Item -ItemType Directory -Path $startMenuDir -Force | Out-Null
}
if (-not (Test-Path $icon)) {
    Write-Host "Note: icon not found at $icon - run installer\windows\make-icon.ps1 first for the branded icon. Creating shortcut with the default icon for now." -ForegroundColor DarkYellow
}

$wsh = New-Object -ComObject WScript.Shell
foreach ($p in $targets) {
    $sc = $wsh.CreateShortcut($p)
    $sc.TargetPath       = $target
    $sc.WorkingDirectory = $ROOT
    $sc.WindowStyle      = 7   # 7 = minimized: the launcher console stays out of the way
    $sc.Description       = 'F-Pulse - local data pipeline builder by Hybridyn'
    if (Test-Path $icon) { $sc.IconLocation = "$icon,0" }
    $sc.Save()
    Write-Host "Created $p"
}

Write-Host ""
Write-Host "  Double-click the F-Pulse icon to start the app." -ForegroundColor Green
Write-Host "  Can't see it? Press the Windows key and type 'F-Pulse'." -ForegroundColor DarkGray
Write-Host "  (Remove later with:  .\Create-Shortcut.ps1 -Remove )" -ForegroundColor DarkGray
