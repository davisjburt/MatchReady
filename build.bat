@echo off
rem Builds dist\MatchReady.exe (single file, no console). Run from this folder.
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
    echo Creating virtual environment...
    py -3.12 -m venv .venv || python -m venv .venv
)
.venv\Scripts\python.exe -m pip install -q -r requirements.txt || exit /b 1
.venv\Scripts\python.exe build.py %* || exit /b 1
echo.
echo Done. Edit dist\targets.json to change servers.
