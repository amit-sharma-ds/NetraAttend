@echo off
rem First-time setup on a new PC. Needs a normal Python 3.12+ from python.org
rem (not the Microsoft Store one - that one cannot run as a background service).
cd /d "%~dp0"
if not exist .venv-service (
  py -3.14 -m venv .venv-service || py -3 -m venv .venv-service
)
".venv-service\Scripts\python.exe" -m pip install --upgrade pip
".venv-service\Scripts\python.exe" -m pip install -r requirements.txt
".venv-service\Scripts\python.exe" download_models.py
if not exist config.py copy config.example.py config.py
echo.
echo Setup done. Fill in config.py, then double-click deploy\INSTALL.bat
pause
