@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
set PYTHONUNBUFFERED=1
title Arcus Wyckoff Watch Dry Run
echo Preview only, no orders. Press Ctrl+C to stop.
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -u watch.py --dry-run %*
) else (
  py -u watch.py --dry-run %*
)
echo.
echo Exited. See logs\watch.log
pause
