@echo off
rem Double-click: removes the NetraAttend background service (asks for admin permission).
powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process powershell -Verb RunAs -ArgumentList '-NoProfile -ExecutionPolicy Bypass -NoExit -File \"%~dp0uninstall_service.ps1\"'"
