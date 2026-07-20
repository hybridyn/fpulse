# make-icon.ps1 - generate installer/windows/icons/fpulse.ico from the
# F-Pulse logo mark (2026-06-08).
#
# Produces a multi-size (16 / 32 / 48 / 256) PNG-in-ICO. Modern Windows
# (7+) Explorer renders PNG-compressed icon entries at every size, which
# is what shortcut icons (.lnk / .url IconLocation) and the Inno Setup
# SetupIconFile use. Re-run this whenever the logo changes - the .ico is
# a generated asset, this script is the source of truth.
#
#   .\installer\windows\make-icon.ps1
#
# NOTE: ASCII-only on purpose (Windows PowerShell 5.1 reads BOM-less
# scripts as ANSI; non-ASCII in a string literal can break parsing).
[CmdletBinding()]
param(
    [string]$Source,
    [string]$OutFile
)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Drawing

if (-not $Source)  { $Source  = Join-Path $PSScriptRoot '..\..\frontend\public\fpulse-logo-mark.png' }
if (-not $OutFile) { $OutFile = Join-Path $PSScriptRoot 'icons\fpulse.ico' }

$Source = (Resolve-Path $Source).Path
$outDir = Split-Path $OutFile -Parent
if (-not (Test-Path $outDir)) { New-Item -ItemType Directory -Path $outDir -Force | Out-Null }

$sizes = 16, 32, 48, 256
$src = [System.Drawing.Image]::FromFile($Source)
$images = @()
try {
    foreach ($s in $sizes) {
        $bmp = New-Object System.Drawing.Bitmap($s, $s)
        $g = [System.Drawing.Graphics]::FromImage($bmp)
        $g.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
        $g.SmoothingMode     = [System.Drawing.Drawing2D.SmoothingMode]::HighQuality
        $g.PixelOffsetMode   = [System.Drawing.Drawing2D.PixelOffsetMode]::HighQuality
        $g.Clear([System.Drawing.Color]::Transparent)
        $g.DrawImage($src, 0, 0, $s, $s)
        $g.Dispose()
        $ms = New-Object System.IO.MemoryStream
        $bmp.Save($ms, [System.Drawing.Imaging.ImageFormat]::Png)
        $bmp.Dispose()
        $images += , ($ms.ToArray())
        $ms.Dispose()
    }
} finally {
    $src.Dispose()
}

$n = $images.Count
$out = New-Object System.IO.MemoryStream
$bw = New-Object System.IO.BinaryWriter($out)
# ICONDIR header
$bw.Write([UInt16]0)    # reserved
$bw.Write([UInt16]1)    # type = 1 (icon)
$bw.Write([UInt16]$n)   # image count
# ICONDIRENTRY table
$offset = 6 + (16 * $n)
for ($i = 0; $i -lt $n; $i++) {
    $s = $sizes[$i]
    $data = $images[$i]
    $dim = if ($s -ge 256) { 0 } else { $s }   # 0 means 256 in the ICO spec
    $bw.Write([Byte]$dim)             # width
    $bw.Write([Byte]$dim)             # height
    $bw.Write([Byte]0)               # color count (0 = truecolor)
    $bw.Write([Byte]0)               # reserved
    $bw.Write([UInt16]1)             # color planes
    $bw.Write([UInt16]32)            # bits per pixel
    $bw.Write([UInt32]$data.Length)  # size of image data
    $bw.Write([UInt32]$offset)       # offset of image data
    $offset += $data.Length
}
# image data, concatenated
foreach ($data in $images) { $bw.Write($data) }
$bw.Flush()
[System.IO.File]::WriteAllBytes($OutFile, $out.ToArray())
$bw.Dispose()
$out.Dispose()

$bytes = (Get-Item $OutFile).Length
Write-Host "Wrote $OutFile ($bytes bytes; sizes $($sizes -join ', '))"

# Also emit a 256x256 PNG next to the .ico. The Linux installers
# (build-deb.sh / build-appimage.sh) already copy
# installer/windows/icons/fpulse.png as the hicolor app icon, so this
# one asset brands the Windows .ico AND the Linux packages.
$pngOut = Join-Path $outDir 'fpulse.png'
$srcPng = [System.Drawing.Image]::FromFile($Source)
try {
    $pbmp = New-Object System.Drawing.Bitmap(256, 256)
    $pg = [System.Drawing.Graphics]::FromImage($pbmp)
    $pg.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
    $pg.SmoothingMode     = [System.Drawing.Drawing2D.SmoothingMode]::HighQuality
    $pg.PixelOffsetMode   = [System.Drawing.Drawing2D.PixelOffsetMode]::HighQuality
    $pg.Clear([System.Drawing.Color]::Transparent)
    $pg.DrawImage($srcPng, 0, 0, 256, 256)
    $pg.Dispose()
    $pbmp.Save($pngOut, [System.Drawing.Imaging.ImageFormat]::Png)
    $pbmp.Dispose()
} finally {
    $srcPng.Dispose()
}
Write-Host "Wrote $pngOut (256x256 PNG for the Linux packages)"
