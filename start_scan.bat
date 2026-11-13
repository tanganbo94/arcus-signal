@echo off
chcp 65001 >nul
cd /d "%~dp0"
title Arcus Wyckoff Scan
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" scan.py %*
) else (
  py scan.py %*
)
pause
