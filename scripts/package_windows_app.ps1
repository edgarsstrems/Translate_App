param(
    [switch]$Public,
    [switch]$SkipDependencies
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

if (-not $SkipDependencies) {
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

}

Write-Step "Building icon"
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $projectRoot "scripts\build_launcher.ps1")
if ($LASTEXITCODE -ne 0) {
    throw "Could not build app icon."
}

Write-Step "Cleaning generated binaries (preserving local settings and credentials)"
$allowedRoot = (Resolve-Path -LiteralPath $projectRoot).Path.TrimEnd('\') + '\'
$generated = @(
    (Join-Path $appRoot "_internal"),
    (Join-Path $appRoot "ChurchTranslator.exe"),
    (Join-Path $projectRoot "build\pyinstaller"),
    (Join-Path $projectRoot "build\spec"),
    (Join-Path $projectRoot "build\bundle-dist"),
    $zipPath
)
foreach ($artifact in $generated) {
    if (Test-Path -LiteralPath $artifact) {
        $resolvedArtifact = (Resolve-Path -LiteralPath $artifact).Path
        if (-not $resolvedArtifact.StartsWith($allowedRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "Refusing cleanup outside repository: $resolvedArtifact"
        }
        if ((Get-Item -LiteralPath $resolvedArtifact).Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw "Refusing cleanup through a reparse point: $resolvedArtifact"
        }
        Remove-Item -LiteralPath $resolvedArtifact -Recurse -Force
    }
}
New-Item -ItemType Directory -Path $distRoot -Force | Out-Null

Write-Step "Bundling Windows app"
# Resolve native dependencies from Windows/Python, not unrelated software on
# the invoking shell's PATH (e.g. Poppler's incompatible ICU DLL shadows the
# Windows ICU API expected by Qt). Never modify the user's persistent PATH.
$basePythonRoot = (& $python -c "import sys; print(sys.base_prefix)").Trim()
$env:PATH = "$basePythonRoot;$(Join-Path $projectRoot '.venv\Scripts');$env:SystemRoot\System32;$env:SystemRoot;$env:SystemRoot\System32\Wbem;$env:SystemRoot\System32\WindowsPowerShell\v1.0"
$bundleDist = Join-Path $projectRoot "build\bundle-dist"
New-Item -ItemType Directory -Path $bundleDist -Force | Out-Null
$buildPath = Join-Path $projectRoot "build\pyinstaller"
$specPath = Join-Path $projectRoot "build\spec"
New-Item -ItemType Directory -Path $buildPath -Force | Out-Null
New-Item -ItemType Directory -Path $specPath -Force | Out-Null

& $python -m PyInstaller `
    --noconfirm `
    --clean `
    --windowed `
    --name ChurchTranslator `
    --icon (Join-Path $projectRoot "assets\app.ico") `
    --distpath $bundleDist `
    --workpath $buildPath `
    --specpath $specPath `
    --collect-all sounddevice `
    --collect-all soundfile `
    --collect-all shiboken6 `
    --collect-all faster_whisper `
    --collect-all ctranslate2 `
    --collect-all huggingface_hub `
    --collect-all tokenizers `
    --collect-all google.genai `
    --hidden-import PySide6.QtCore `
    --hidden-import PySide6.QtGui `
    --hidden-import PySide6.QtWidgets `
    --hidden-import PySide6.QtNetwork `
    --hidden-import sounddevice `
    --hidden-import soundfile `
    --hidden-import numpy `
    --hidden-import dotenv `
    --hidden-import google.cloud.texttospeech `
    --hidden-import google.generativeai `
    --hidden-import google.genai `
    --hidden-import openai `
    (Join-Path $projectRoot "run.py")
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller build failed."
}

New-Item -ItemType Directory -Path $appRoot -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $bundleDist "ChurchTranslator\ChurchTranslator.exe") -Destination $appRoot -Force
Copy-Item -LiteralPath (Join-Path $bundleDist "ChurchTranslator\_internal") -Destination $appRoot -Recurse -Force

Write-Step "Adding editable app files"
$externalItems = @(
    "README.md",
    "AUDIT_REPORT.md",
    ".env.example",
    "glossary.json",
    "icon.png",
    "assets",
    "scripts"
)

New-Item -ItemType Directory -Path (Join-Path $appRoot "credentials") -Force | Out-Null
Set-Content -Path (Join-Path $appRoot "credentials\.gitkeep") -Value "" -Encoding ASCII

$appEnvPath = Join-Path $appRoot ".env"
if (-not (Test-Path -LiteralPath $appEnvPath)) {
    Copy-Item -LiteralPath (Join-Path $projectRoot ".env.example") -Destination $appEnvPath
}

foreach ($item in $externalItems) {
    $source = Join-Path $projectRoot $item
    if ($item -eq "glossary.json" -and (Test-Path (Join-Path $appRoot $item))) { continue }
    if (Test-Path $source) {
        Copy-Item -LiteralPath $source -Destination $appRoot -Recurse -Force
    }
}

Write-Step "Creating public zip (never includes local secrets)"
& $python (Join-Path $projectRoot "scripts\public_zip.py") $appRoot $zipPath
if ($LASTEXITCODE -ne 0) { throw "Zip creation failed" }
Write-Host "Bundled app folder: $appRoot"
Write-Host "Public zip: $zipPath"
Write-Host "Existing local .env, credentials, and glossary were preserved."
