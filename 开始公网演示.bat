@echo off
chcp 65001 >nul
title Public Demo Tunnel - do not close
cd /d D:\华为ICT\agent_poc
.venv\Scripts\python.exe scripts\public_tunnel.py --port 18770
echo.
echo [tunnel stopped] press any key to close
pause >nul
