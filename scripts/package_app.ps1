param(
    [switch]$IncludeLocalSecrets
)

$ErrorActionPreference = "Stop"

function Write-Step {
    param([string]$Message)
    Write-Host ""
    Write-Host "== $Message ==" -ForegroundColor Cyan
}

$projectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$distRoot = Join-Path $projectRoot "dist"
$packageRoot = Join-Path $distRoot "ChurchTranslator"
$zipPath = Join-Path $distRoot "ChurchTranslator.zip"

Write-Step "Building launcher"
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $projectRoot "scripts\build_launcher.ps1")
if ($LASTEXITCODE -ne 0) {
    throw "Could not build launcher."
}

Write-Step "Preparing package folder"
if (Test-Path $packageRoot) {
    $resolvedPackage = (Resolve-Path $packageRoot).Path
    $resolvedDist = if (Test-Path $distRoot) { (Resolve-Path $distRoot).Path } else { $distRoot }
    if (-not $resolvedPackage.StartsWith($resolvedDist, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to remove unexpected package path: $resolvedPackage"
    }
    Remove-Item -LiteralPath $resolvedPackage -Recurse -Force
}
New-Item -ItemType Directory -Path $packageRoot | Out-Null

$items = @(
    "ChurchTranslator.exe",
    "run.py",
    "requirements.txt",
    "requirements-local-whisper.txt",
    "glossary.json",
    ".env.example",
    "README.md",
    "icon.png",
    "church_translator",
    "scripts",
    "assets"
)

if ($IncludeLocalSecrets -and (Test-Path (Join-Path $projectRoot ".env"))) {
    $items += ".env"
}

if ($IncludeLocalSecrets -and (Test-Path (Join-Path $projectRoot "credentials"))) {
    $items += "credentials"
}

foreach ($item in $items) {
    $source = Join-Path $projectRoot $item
    if (Test-Path $source) {
        Copy-Item -LiteralPath $source -Destination $packageRoot -Recurse -Force
    }
}

if (-not $IncludeLocalSecrets) {
    Copy-Item -LiteralPath (Join-Path $projectRoot ".env.example") -Destination (Join-Path $packageRoot ".env") -Force
}

if (-not (Test-Path (Join-Path $packageRoot "credentials"))) {
    New-Item -ItemType Directory -Path (Join-Path $packageRoot "credentials") | Out-Null
    Set-Content -Path (Join-Path $packageRoot "credentials\.gitkeep") -Value "" -Encoding ASCII
}

$cleanupDirs = @(
    (Join-Path $packageRoot "church_translator\__pycache__"),
    (Join-Path $packageRoot "scripts\__pycache__")
)
foreach ($dir in $cleanupDirs) {
    if (Test-Path $dir) {
        Remove-Item -LiteralPath $dir -Recurse -Force
    }
}

Write-Step "Creating zip"
if (Test-Path $zipPath) {
    Remove-Item -LiteralPath $zipPath -Force
}
Compress-Archive -Path $packageRoot -DestinationPath $zipPath -Force

Write-Host "Package folder: $packageRoot"
Write-Host "Zip file: $zipPath"
