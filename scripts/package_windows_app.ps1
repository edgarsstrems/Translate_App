param(
    [switch]$Public
)

$ErrorActionPreference = "Stop"

function Write-Step {
    param([string]$Message)
    Write-Host ""
    Write-Host "== $Message ==" -ForegroundColor Cyan
}

$projectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$pyinstaller = Join-Path $projectRoot ".venv\Scripts\pyinstaller.exe"
$distRoot = Join-Path $projectRoot "dist-app"
$appRoot = Join-Path $distRoot "ChurchTranslator"
$zipPath = Join-Path $distRoot "ChurchTranslator-Windows.zip"

if (-not (Test-Path $python)) {
    throw "Missing .venv Python. Run ChurchTranslator.exe or run.bat once in the project folder first."
}

Write-Step "Preparing build dependencies"
& $python -m pip install -r (Join-Path $projectRoot "requirements.txt")
if ($LASTEXITCODE -ne 0) {
    throw "Could not install base requirements."
}
& $python -m pip install -r (Join-Path $projectRoot "requirements-local-whisper.txt")
if ($LASTEXITCODE -ne 0) {
    throw "Could not install local Whisper build requirements."
}
& $python -m pip install pyinstaller
if ($LASTEXITCODE -ne 0) {
    throw "Could not install PyInstaller."
}

Write-Step "Building icon"
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $projectRoot "scripts\build_launcher.ps1")
if ($LASTEXITCODE -ne 0) {
    throw "Could not build app icon."
}

Write-Step "Cleaning previous bundled build"
if (Test-Path $distRoot) {
    Remove-Item -LiteralPath $distRoot -Recurse -Force
}
New-Item -ItemType Directory -Path $distRoot | Out-Null

Write-Step "Bundling Windows app"
$buildPath = Join-Path $projectRoot "build\pyinstaller"
$specPath = Join-Path $projectRoot "build\spec"
New-Item -ItemType Directory -Path $buildPath -Force | Out-Null
New-Item -ItemType Directory -Path $specPath -Force | Out-Null

& $pyinstaller `
    --noconfirm `
    --clean `
    --windowed `
    --name ChurchTranslator `
    --icon (Join-Path $projectRoot "assets\app.ico") `
    --distpath $distRoot `
    --workpath $buildPath `
    --specpath $specPath `
    --collect-all shiboken6 `
    --collect-all faster_whisper `
    --collect-all ctranslate2 `
    --collect-all huggingface_hub `
    --collect-all tokenizers `
    --hidden-import PySide6.QtCore `
    --hidden-import PySide6.QtGui `
    --hidden-import PySide6.QtWidgets `
    --hidden-import PySide6.QtNetwork `
    --hidden-import google.cloud.texttospeech `
    --hidden-import google.cloud.translate_v2 `
    --hidden-import google.generativeai `
    --hidden-import openai `
    (Join-Path $projectRoot "run.py")
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller build failed."
}

Write-Step "Adding editable app files"
$externalItems = @(
    "README.md",
    ".env.example",
    "glossary.json",
    "icon.png",
    "assets"
)

if ($Public) {
    New-Item -ItemType Directory -Path (Join-Path $appRoot "credentials") -Force | Out-Null
    Set-Content -Path (Join-Path $appRoot "credentials\.gitkeep") -Value "" -Encoding ASCII
} else {
    $publicEnvPath = Join-Path $appRoot ".env"
    $envTemplate = Get-Content -LiteralPath (Join-Path $projectRoot ".env.example") -Raw
    New-Item -ItemType File -Path $publicEnvPath -Force | Out-Null
    Set-Content -LiteralPath $publicEnvPath -Value $envTemplate -Encoding ASCII
    if (Test-Path (Join-Path $projectRoot "credentials")) {
        $externalItems += "credentials"
    }
}

foreach ($item in $externalItems) {
    $source = Join-Path $projectRoot $item
    if (Test-Path $source) {
        Copy-Item -LiteralPath $source -Destination $appRoot -Recurse -Force
    }
}

Write-Step "Creating zip"
if (Test-Path $zipPath) {
    Remove-Item -LiteralPath $zipPath -Force
}
Compress-Archive -Path $appRoot -DestinationPath $zipPath -Force

Write-Host "Bundled app folder: $appRoot"
Write-Host "Bundled zip: $zipPath"
if ($Public) {
    Write-Host "Public build: .env and credentials were not included."
} else {
    Write-Host "Private build: local .env and credentials were included."
}
