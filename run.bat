@echo off
REM ---------------------------------------------------------------
REM  Arabic by M.S - Stremio subtitle addon (Phase 4)
REM  Starts the FastAPI server with uvicorn on port 8787.
REM ---------------------------------------------------------------

setlocal
title Arabic by M.S - Phase 4

REM Move to the directory this script lives in so relative paths work.
cd /d "%~dp0"

REM Create a local virtual environment on first run.
if not exist ".venv" (
    echo Creating virtual environment...
    python -m venv .venv
)

call .venv\Scripts\activate.bat

REM Install / refresh dependencies.
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

REM Launch the addon.
python -m uvicorn backend.main:app --host 0.0.0.0 --port 8787 --reload

endlocal
