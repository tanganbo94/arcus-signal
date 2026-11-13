@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
set PYTHONUNBUFFERED=1
title Arcus Wyckoff Watch
echo Keep this window open. Press Ctrl+C to stop.
echo Live trading is ON. For preview only, use start_watch_dry.bat
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -u watch.py %*
) else (
  py -u watch.py %*
)
echo.
echo Exited. See logs\watch.log
pause
