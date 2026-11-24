@echo off
setlocal
cd /d "%~dp0" || exit /b 1
if exist ".venv312\Scripts\python.exe" (
    ".venv312\Scripts\python.exe" jarvis.py
) else (
    py -3 jarvis.py
)
if errorlevel 1 pause
