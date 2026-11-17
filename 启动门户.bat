@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

set "PY=.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo [warn] project venv not found, falling back to system python
    set "PY=python"
)

"%PY%" start_portal.py %*
echo.
echo server stopped.
pause
endlocal
