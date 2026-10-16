@echo off
rem Run NetraAttend in this window (for testing). For daily use install the service: deploy\INSTALL.bat
cd /d "%~dp0"
title NetraAttend
set PYTHONUTF8=1
:loop
".venv-service\Scripts\python.exe" app.py
echo NetraAttend stopped - restarting in 5 seconds...
timeout /t 5 /nobreak >nul
goto loop
