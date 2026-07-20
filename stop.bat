@echo off
:: F-Pulse stop (v2, 2026-06-06)
::
:: Thin wrapper around stop.ps1. Reads the runtime ownership file
:: written by start.ps1 and stops ONLY the PIDs recorded there - and
:: only if all three signals still agree (PID alive + still on
:: recorded port + cmdline still matches uvicorn/vite signature).
::
:: Will NOT touch a foreign process on the F-Pulse ports. If you see
:: one and want to stop it yourself, inspect what it is:
::   powershell -Command "Get-NetTCPConnection -LocalPort 5174 | Get-Process"

setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop.ps1"
set EXITCODE=%ERRORLEVEL%
pause
endlocal
exit /b %EXITCODE%
