param(
    [switch]$LaunchWhenDone
)

$ErrorActionPreference = "Stop"

function Write-Step {
    param([string]$Message)
    Write-Host ""
    Write-Host "== $Message ==" -ForegroundColor Cyan
}

function Find-Python {
    $localPython = Join-Path $PSScriptRoot "..\.venv\Scripts\python.exe"
    if (Test-Path $localPython) {
        return (Resolve-Path $localPython).Path
    }

    $commands = @(
        @{ Name = "py"; Args = @("-3", "-c", "import sys; print(sys.executable)") },
        @{ Name = "python"; Args = @("-c", "import sys; print(sys.executable)") },
        @{ Name = "python3"; Args = @("-c", "import sys; print(sys.executable)") }
    )

    foreach ($command in $commands) {
        try {
            $exe = Get-Command $command.Name -ErrorAction Stop
            if ($exe.Source -like "*WindowsApps*") {
                continue
            }
            $output = & $exe.Source @($command.Args) 2>$null
            if ($LASTEXITCODE -eq 0 -and $output) {
                $candidate = ($output | Select-Object -First 1).Trim()
                if ((Test-Path $candidate) -and $candidate -notlike "*WindowsApps*") {
                    return $candidate
                }
            }
        } catch {
            continue
        }
    }

    $knownPaths = @(
        "$env:LocalAppData\Programs\Python\Python311\python.exe",
        "$env:LocalAppData\Programs\Python\Python312\python.exe",
        "$env:ProgramFiles\Python311\python.exe",
        "$env:ProgramFiles\Python312\python.exe",
        "${env:ProgramFiles(x86)}\Python311\python.exe",
        "${env:ProgramFiles(x86)}\Python312\python.exe"
    )

    foreach ($path in $knownPaths) {
        if (Test-Path $path) {
            return $path
        }
    }

    return $null
}

function Install-Python {
    Write-Step "Python was not found. Trying automatic install"

    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if (-not $winget) {
        throw "Python is not installed and winget is not available. Install Python 3.11+ from https://www.python.org/downloads/windows/ and run build.bat again."
    }

    Write-Host "Installing Python 3.11 with winget. This can take a few minutes..."
    & $winget.Source install --id Python.Python.3.11 --source winget --accept-package-agreements --accept-source-agreements | Out-Host
    if ($LASTEXITCODE -ne 0) {
        throw "winget could not install Python. Install Python 3.11+ manually from https://www.python.org/downloads/windows/ and run build.bat again."
    }

    $paths = @(
        "$env:LocalAppData\Programs\Python\Python311\python.exe",
        "$env:ProgramFiles\Python311\python.exe",
        "${env:ProgramFiles(x86)}\Python311\python.exe"
    )

    foreach ($path in $paths) {
        if (Test-Path $path) {
            return $path
        }
    }

    return Find-Python
}

try {
    $repoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
    Set-Location $repoRoot

    Write-Step "Checking Python"
    $python = Find-Python
    if (-not $python) {
        $python = Install-Python
    }
    if ($python -is [array]) {
        $python = $python | Where-Object { $_ -and (Test-Path $_) } | Select-Object -Last 1
    }
    if (-not $python) {
        throw "Python installation finished, but python.exe could not be found. Restart your terminal and run build.bat again."
    }
    $python = (Resolve-Path $python).Path
    Write-Host "Using Python: $python"

    Write-Step "Setting up build environment (.venv)"
    $venvPython = Join-Path $repoRoot ".venv\Scripts\python.exe"
    if (-not (Test-Path $venvPython)) {
        & $python -m venv .venv
        if ($LASTEXITCODE -ne 0) {
            throw "Could not create the Python virtual environment."
        }
    }

    Write-Step "Installing build tools & packages"
    & $venvPython -m pip install --upgrade pip
    & $venvPython -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) {
        throw "Failed installing requirements.txt."
    }
    & $venvPython -m pip install -r requirements-local-whisper.txt
    if ($LASTEXITCODE -ne 0) {
        throw "Failed installing requirements-local-whisper.txt."
    }
    & $venvPython -m pip install pyinstaller
    if ($LASTEXITCODE -ne 0) {
        throw "Failed installing pyinstaller."
    }

    Write-Step "Packaging standalone Windows executable"
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $repoRoot "scripts\package_windows_app.ps1") -Public
    if ($LASTEXITCODE -ne 0) {
        throw "Application packaging failed."
    }

    $builtExe = Join-Path $repoRoot "dist-app\ChurchTranslator\ChurchTranslator.exe"
    if (-not (Test-Path $builtExe)) {
        throw "ChurchTranslator.exe was not found at expected location: $builtExe"
    }

    Write-Host ""
    Write-Host "========================================================" -ForegroundColor Green
    Write-Host "   BUILD SUCCESSFUL! Standalone App is Ready!         " -ForegroundColor Green
    Write-Host "========================================================" -ForegroundColor Green
    Write-Host "Standalone executable: $builtExe"
    Write-Host ""
    Write-Host "To run the app:"
    Write-Host "  1. Double click 'run.bat' in the project folder, OR"
    Write-Host "  2. Directly open: dist-app\ChurchTranslator\ChurchTranslator.exe"
    Write-Host ""

    if ($LaunchWhenDone) {
        Write-Step "Launching Church Sermon Translator..."
        Start-Process -FilePath $builtExe
    }
} catch {
    Write-Host ""
    Write-Host "Build failed:" -ForegroundColor Red
    Write-Host $_.Exception.Message -ForegroundColor Red
    Write-Host ""
    exit 1
}
