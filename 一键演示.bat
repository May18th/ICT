@echo off
chcp 65001 >nul
title One-key demo - do not close this window
rem Use the script's own directory so it works on any machine / from a USB stick.
cd /d "%~dp0"

set PY=.venv\Scripts\python.exe
if not exist "%PY%" set PY=python

echo.
echo  ================================================
echo   城市内涝可进化决策智能体 - 一键演示
echo   offline / local only / no cloud API needed
echo  ================================================
echo.
echo  [1] 自动演示（按一次回车，五幕自己跑）
echo  [2] 手动节拍（每一幕按空格推进）
echo  [3] 只自检，不放映
echo.
set /p PICK=请选择 1/2/3 后回车（默认 1）:

if "%PICK%"=="2" goto manual
if "%PICK%"=="3" goto check

:auto
"%PY%" scripts\demo_tour.py --port 8890
goto end

:manual
"%PY%" scripts\demo_tour.py --port 8890 --manual
goto end

:check
"%PY%" scripts\demo_tour.py --port 8890 --check
echo.
pause
goto end

:end
echo.
echo [demo stopped] press any key to close
pause >nul
