@echo off
rem Runs MatchReady from source.
cd /d "%~dp0"
start "" .venv\Scripts\pythonw.exe MatchReady.py
