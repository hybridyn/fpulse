@echo off
:: F-Pulse start (v2, 2026-06-06)
::
:: Thin wrapper around start.ps1 - all the real logic (auto-pick free
:: port, ownership file, 3-signal kill check) lives in PowerShell.
:: Keeping cmd as a one-liner avoids drift between the two launchers.
::
:: Usage:
::   start.bat              Normal launch (auto-picks free port if needed)
::   start.bat --force      Auto-stop previous F-Pulse instance without prompt
::
:: Env-var preferences (optional - start of port scan range, not required):
::   set FPULSE_FRONTEND_PORT=5180   then run start.bat
::   set FPULSE_PORT=8010
::
:: Bypassing ExecutionPolicy here is safe: the script being run is a
:: file inside this repo that the user just cloned/installed, not a
:: download.

setlocal
set FORCE_FLAG=
if /i "%~1"=="--force" set FORCE_FLAG=-Force
if /i "%~1"=="/force"  set FORCE_FLAG=-Force
if /i "%~1"=="-f"      set FORCE_FLAG=-Force

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %FORCE_FLAG%
set EXITCODE=%ERRORLEVEL%
echo.
echo   The two server windows (F-Pulse Backend, F-Pulse Frontend) are
echo   running detached. Close them, or run stop.bat from this directory,
echo   to shut F-Pulse down cleanly.
echo.
pause
endlocal
exit /b %EXITCODE%
