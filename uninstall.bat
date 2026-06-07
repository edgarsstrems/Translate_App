@echo off
setlocal
cd /d "%~dp0"

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\uninstall.ps1" -ProjectRoot "%~dp0" -AskPython

echo.
echo Window can be closed now.
pause
