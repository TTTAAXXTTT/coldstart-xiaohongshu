@echo off
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\windows.ps1" -Action Start
if errorlevel 1 echo Operation failed. Read the error above.
pause
