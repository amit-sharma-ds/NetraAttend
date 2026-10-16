@echo off
rem Runs NetraAttend in the background (started by the "NetraAttend" scheduled task at boot).
rem Restarts the app if it ever stops and keeps the log in logs\netraattend.log.
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
if not exist logs mkdir logs
:loop
if exist "logs\netraattend.log" for %%F in ("logs\netraattend.log") do if %%~zF GTR 10485760 move /y "logs\netraattend.log" "logs\netraattend.old.log" >nul
echo [%date% %time%] starting NetraAttend >> "logs\netraattend.log"
".venv-service\Scripts\python.exe" -u app.py >> "logs\netraattend.log" 2>&1
echo [%date% %time%] NetraAttend stopped (exit %errorlevel%) - restarting in 10 s >> "logs\netraattend.log"
rem "timeout" does not work without a console (service), ping is a reliable 10 s pause
ping -n 11 127.0.0.1 >nul
goto loop
