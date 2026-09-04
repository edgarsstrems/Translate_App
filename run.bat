@echo off
setlocal
cd /d "%~dp0"

if exist "%~dp0dist-app\ChurchTranslator\ChurchTranslator.exe" (
    start "" "%~dp0dist-app\ChurchTranslator\ChurchTranslator.exe"
    exit /b 0
)

echo ==============================================================================
echo       Church Sermon Translator - Standalone App Launcher
echo ==============================================================================
echo.
echo The standalone executable (dist-app\ChurchTranslator\ChurchTranslator.exe)
echo has not been compiled yet on this computer.
echo.
echo Launching automated build now (build.bat)...
echo.
call "%~dp0build.bat"

if exist "%~dp0dist-app\ChurchTranslator\ChurchTranslator.exe" (
    echo.
    echo Launching Church Sermon Translator...
    start "" "%~dp0dist-app\ChurchTranslator\ChurchTranslator.exe"
)
