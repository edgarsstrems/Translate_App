$ErrorActionPreference = "Stop"

function Write-Step {
    param([string]$Message)
    Write-Host ""
    Write-Host "== $Message ==" -ForegroundColor Cyan
}

function Convert-PngToIcon {
    param(
        [string]$Source,
        [string]$Path
    )

    Add-Type -AssemblyName System.Drawing
    $dir = Split-Path -Parent $Path
    if (-not (Test-Path $dir)) {
        New-Item -ItemType Directory -Path $dir | Out-Null
    }

    $bitmap = New-Object System.Drawing.Bitmap 256, 256
    $sourceBitmap = [System.Drawing.Image]::FromFile($Source)
    $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
    $graphics.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
    $graphics.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
    $graphics.PixelOffsetMode = [System.Drawing.Drawing2D.PixelOffsetMode]::HighQuality
    $graphics.Clear([System.Drawing.Color]::Transparent)

    $scale = [Math]::Min(256 / $sourceBitmap.Width, 256 / $sourceBitmap.Height)
    $width = [int][Math]::Round($sourceBitmap.Width * $scale)
    $height = [int][Math]::Round($sourceBitmap.Height * $scale)
    $x = [int][Math]::Floor((256 - $width) / 2)
    $y = [int][Math]::Floor((256 - $height) / 2)
    $destRect = New-Object System.Drawing.Rectangle $x, $y, $width, $height
    $graphics.DrawImage($sourceBitmap, $destRect)

    $stream = New-Object System.IO.MemoryStream
    $bitmap.Save($stream, [System.Drawing.Imaging.ImageFormat]::Png)
    $png = $stream.ToArray()

    $icon = New-Object System.IO.MemoryStream
    $writer = New-Object System.IO.BinaryWriter $icon
    $writer.Write([UInt16]0)
    $writer.Write([UInt16]1)
    $writer.Write([UInt16]1)
    $writer.Write([Byte]0)
    $writer.Write([Byte]0)
    $writer.Write([Byte]0)
    $writer.Write([Byte]0)
    $writer.Write([UInt16]1)
    $writer.Write([UInt16]32)
    $writer.Write([UInt32]$png.Length)
    $writer.Write([UInt32]22)
    $writer.Write($png)
    [System.IO.File]::WriteAllBytes($Path, $icon.ToArray())

    $writer.Dispose()
    $icon.Dispose()
    $stream.Dispose()
    $graphics.Dispose()
    $sourceBitmap.Dispose()
    $bitmap.Dispose()
}

function Find-CSharpCompiler {
    $candidates = @(
        "$env:WINDIR\Microsoft.NET\Framework64\v4.0.30319\csc.exe",
        "$env:WINDIR\Microsoft.NET\Framework\v4.0.30319\csc.exe"
    )

    foreach ($candidate in $candidates) {
        if (Test-Path $candidate) {
            return $candidate
        }
    }

    $command = Get-Command csc.exe -ErrorAction SilentlyContinue
    if ($command) {
        return $command.Source
    }

    throw "Could not find csc.exe. Install the .NET Framework developer tools or build on a Windows machine that includes csc.exe."
}

$projectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$source = Join-Path $projectRoot "launcher\ChurchTranslatorLauncher.cs"
$iconSource = Join-Path $projectRoot "icon.png"
$icon = Join-Path $projectRoot "assets\app.ico"
$output = Join-Path $projectRoot "ChurchTranslator.exe"

Write-Step "Creating app icon"
if (-not (Test-Path $iconSource)) {
    throw "Could not find icon.png in the project root."
}
Convert-PngToIcon $iconSource $icon
Write-Host "Icon: $icon"

Write-Step "Compiling launcher"
$csc = Find-CSharpCompiler
& $csc /nologo /target:winexe /out:$output /win32icon:$icon /reference:System.dll /reference:System.Drawing.dll /reference:System.Windows.Forms.dll $source
if ($LASTEXITCODE -ne 0) {
    throw "Launcher compilation failed."
}

Write-Host "Built: $output"
