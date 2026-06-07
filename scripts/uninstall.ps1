param(
    [string]$ProjectRoot,
    [switch]$AskPython,
    [switch]$FromApp,
    [switch]$RemoveVenv,
    [switch]$RemoveCache,
    [switch]$RemoveEnv,
    [switch]$RemoveSetupScripts,
    [switch]$RemovePython
)

$ErrorActionPreference = "Continue"

function Write-Step {
    param([string]$Message)
    Write-Host ""
    Write-Host "== $Message ==" -ForegroundColor Cyan
}

function Remove-PathSafe {
    param(
        [string]$PathToRemove,
        [string]$AllowedRoot
    )

    if (-not $PathToRemove) {
        return
    }

    $resolved = Resolve-Path $PathToRemove -ErrorAction SilentlyContinue
    if (-not $resolved) {
        Write-Host "Not found: $PathToRemove"
        return
    }

    if ($AllowedRoot) {
        $allowed = (Resolve-Path $AllowedRoot -ErrorAction SilentlyContinue).Path
        if (-not $allowed -or -not $resolved.Path.StartsWith($allowed, [System.StringComparison]::OrdinalIgnoreCase)) {
            Write-Host "Refusing to remove outside allowed root: $($resolved.Path)" -ForegroundColor Red
            return
        }
    }

    Write-Host "Removing $($resolved.Path)"
    Remove-Item -LiteralPath $resolved.Path -Recurse -Force -ErrorAction Continue
}

function Show-ItemStatus {
    param(
        [string]$Label,
        [string]$PathToCheck
    )
    if (Test-Path $PathToCheck) {
        Write-Host "[found]     $Label`: $PathToCheck" -ForegroundColor Green
    } else {
        Write-Host "[not found] $Label`: $PathToCheck" -ForegroundColor DarkGray
    }
}

if (-not $ProjectRoot) {
    $ProjectRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
} else {
    $ProjectRoot = Resolve-Path $ProjectRoot
}

if ($FromApp) {
    Write-Host "Waiting for the app to close..."
    Start-Sleep -Seconds 4
}

$ProjectRoot = (Resolve-Path $ProjectRoot).Path
$appData = Join-Path $env:LOCALAPPDATA "ChurchTranslator"
$venvPath = Join-Path $ProjectRoot ".venv"
$envFile = Join-Path $ProjectRoot ".env"
$scriptsPath = Join-Path $ProjectRoot "scripts"
$runBat = Join-Path $ProjectRoot "run.bat"
$uninstallBat = Join-Path $ProjectRoot "uninstall.bat"

Write-Step "Installed items"
Show-ItemStatus "Virtual environment/packages" $venvPath
Show-ItemStatus "Whisper models and app cache" $appData
Show-ItemStatus ".env settings/API file" $envFile
Show-ItemStatus "Setup scripts folder" $scriptsPath
Show-ItemStatus "run.bat" $runBat
Show-ItemStatus "uninstall.bat" $uninstallBat

if (-not ($RemoveVenv -or $RemoveCache -or $RemoveEnv -or $RemoveSetupScripts -or $RemovePython -or $AskPython)) {
    Write-Host ""
    Write-Host "No uninstall options were selected, so nothing was removed." -ForegroundColor Yellow
    Write-Host "Use the app's Uninstall setup button to choose items, or pass -RemoveVenv/-RemoveCache/etc."
    exit 0
}

if ($RemoveVenv) {
    Write-Step "Removing app-installed Python packages"
    Remove-PathSafe $venvPath $ProjectRoot
}

if ($RemoveCache) {
    Write-Step "Removing downloaded Whisper models and app cache"
    Remove-PathSafe $appData (Split-Path $appData -Parent)
}

if ($RemoveEnv) {
    Write-Step "Removing .env"
    if (Test-Path $envFile) {
        Remove-Item -LiteralPath $envFile -Force -ErrorAction Continue
        Write-Host "Removed .env"
    }
}

if ($RemovePython -or $AskPython) {
    Write-Step "Python uninstall"
    Write-Host "Python may be used by other programs. Only uninstall it if this app is the only reason you installed it." -ForegroundColor Yellow
    $removePythonNow = $RemovePython
    if (-not $removePythonNow) {
        $answer = Read-Host "Uninstall Python 3.11 installed by winget? Type YES to uninstall"
        $removePythonNow = ($answer -eq "YES")
    }
    if ($removePythonNow) {
        $winget = Get-Command winget -ErrorAction SilentlyContinue
        if ($winget) {
            & $winget.Source uninstall --id Python.Python.3.11 --source winget --accept-source-agreements | Out-Host
        } else {
            Write-Host "winget is not available. Remove Python from Windows Settings > Apps if needed."
        }
    } else {
        Write-Host "Kept Python installed."
    }
}

if ($RemoveSetupScripts) {
    Write-Step "Removing setup helper scripts"
    Remove-PathSafe $scriptsPath $ProjectRoot
    if (Test-Path $runBat) {
        Remove-Item -LiteralPath $runBat -Force -ErrorAction Continue
        Write-Host "Removed run.bat"
    }
    if (Test-Path $uninstallBat) {
        Remove-Item -LiteralPath $uninstallBat -Force -ErrorAction Continue
        Write-Host "Removed uninstall.bat"
    }
}

Write-Step "Done"
Write-Host "Selected uninstall actions finished."
