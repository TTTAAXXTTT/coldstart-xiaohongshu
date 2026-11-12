@echo off
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\windows.ps1" -Action BootstrapPython
if errorlevel 1 echo Preparation failed. Read the error above.
pause
