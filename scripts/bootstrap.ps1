param(
    [switch]$FromLauncher
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
        throw "Python is not installed and winget is not available. Install Python 3.11+ from https://www.python.org/downloads/windows/ and run run.bat again."
    }

    Write-Host "Installing Python 3.11 with winget. This can take a few minutes..."
    & $winget.Source install --id Python.Python.3.11 --source winget --accept-package-agreements --accept-source-agreements | Out-Host
    if ($LASTEXITCODE -ne 0) {
        throw "winget could not install Python. Install Python 3.11+ manually from https://www.python.org/downloads/windows/ and run run.bat again."
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
        throw "Python installation finished, but python.exe could not be found. Close this window, open a new Command Prompt, and run run.bat again."
    }
    $python = (Resolve-Path $python).Path
    Write-Host "Using Python: $python"

    Write-Step "Creating virtual environment"
    $venvPython = Join-Path $repoRoot ".venv\Scripts\python.exe"
    if (-not (Test-Path $venvPython)) {
        & $python -m venv .venv
        if ($LASTEXITCODE -ne 0) {
            throw "Could not create the Python virtual environment."
        }
    }

    Write-Step "Installing Python packages"
    & $venvPython -m pip install --upgrade pip
    if ($LASTEXITCODE -ne 0) {
        throw "Could not upgrade pip."
    }
    & $venvPython -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) {
        throw "Could not install required Python packages. Check the error above, then run run.bat again."
    }

    if (-not (Test-Path ".env")) {
        Write-Step "Creating .env file"
        Copy-Item ".env.example" ".env"
        Write-Host "Created .env from .env.example."
        Write-Host "Add GEMINI_API_KEY, GOOGLE_APPLICATION_CREDENTIALS, and optionally OPENAI_API_KEY before real translation/TTS will work." -ForegroundColor Yellow
    }

    Write-Step "Starting Church Sermon Translator"
    Write-Host "OpenAI recognition can start without local Whisper packages."
    Write-Host "Use the in-app Install local Whisper button if this PC should run offline/local recognition."
    & $venvPython run.py
} catch {
    Write-Host ""
    Write-Host "Startup failed:" -ForegroundColor Red
    Write-Host $_.Exception.Message -ForegroundColor Red
    Write-Host ""
    if ($FromLauncher) {
        exit 1
    }
    Write-Host "The window is staying open so you can read the error."
}
