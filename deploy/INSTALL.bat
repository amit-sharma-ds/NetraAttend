@echo off
rem Double-click: installs / restarts NetraAttend as a background service (asks for admin permission).
powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process powershell -Verb RunAs -ArgumentList '-NoProfile -ExecutionPolicy Bypass -NoExit -File \"%~dp0install_service.ps1\"'"
