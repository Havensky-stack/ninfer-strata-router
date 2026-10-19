@echo off
rem Strata's own server, on a port of its own (8080 is often taken by another web UI).
rem Set STRATA_DIR to your Strata checkout; the port here must match "base" in router.json.
set "STRATA_DIR=C:\path\to\Strata"
if not exist "%STRATA_DIR%\.venv\Scripts\python.exe" (
    echo Strata not found at %STRATA_DIR% - edit STRATA_DIR in this file.
    exit /b 1
)
cd /d "%STRATA_DIR%"
".venv\Scripts\python.exe" "serve\server.py" --engine strata --config "strata-iq3_s.json" --port 8082
