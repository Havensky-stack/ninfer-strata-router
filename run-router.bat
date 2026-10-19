@echo off
rem Keep this window open: closing it stops the router and every model it started.
title ninfer-strata router
cd /d "%~dp0"
if not exist "router.json" (
    echo router.json not found. Copy router.example.json to router.json and edit it.
    pause
    exit /b 1
)
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" router.py
) else (
    python router.py
)
pause
