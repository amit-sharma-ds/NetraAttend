@echo off
rem Double-click: shows the public https link (starts the Cloudflare tunnel if needed).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0public_link.ps1"
pause
