@echo off
chcp 65001 >nul
title Public Demo Tunnel - do not close
rem use the script's own directory so it works on any machine
cd /d "%~dp0"
.venv\Scripts\python.exe scripts\public_tunnel.py --port 18770
echo.
echo [tunnel stopped] press any key to close
pause >nul
