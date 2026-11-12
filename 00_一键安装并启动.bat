@echo off
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\windows.ps1" -Action Setup -Interactive
if errorlevel 1 echo Operation failed. Read the error above and rerun this file after correcting it.
pause
