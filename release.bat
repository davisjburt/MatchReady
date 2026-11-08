@echo off
rem Publishes a new version to GitHub Releases. Usage: release.bat 1.1.0 "What changed"
cd /d "%~dp0"
.venv\Scripts\python.exe release.py %*
