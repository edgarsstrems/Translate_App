@echo off
setlocal
cd /d "%~dp0"

echo ==============================================================================
echo       Church Sermon Translator - 1-Click Standalone App Builder
echo ==============================================================================
echo.
echo This script will:
echo   1. Check for Python (or automatically install Python 3.11 via winget)
echo   2. Set up an isolated build environment (.venv)
echo   3. Install all required audio, AI, and GUI packages
echo   4. Compile a standalone Windows .exe in: dist-app\ChurchTranslator\
echo.
echo Please wait while the build runs...
echo.

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\build_app.ps1"

if %ERRORLEVEL% neq 0 (
    echo.
    echo ==============================================================================
    echo [ERROR] Build encountered an error. Please see details above.
    echo ==============================================================================
    echo.
    pause
    exit /b %ERRORLEVEL%
)

echo.
echo ==============================================================================
echo [SUCCESS] Standalone Windows application built successfully!
echo.
echo Location:
echo   %~dp0dist-app\ChurchTranslator\ChurchTranslator.exe
echo.
echo You can run the app anytime by double-clicking 'run.bat' or opening:
echo   dist-app\ChurchTranslator\ChurchTranslator.exe
echo ==============================================================================
echo.
pause
