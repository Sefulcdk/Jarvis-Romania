@echo off
setlocal
cd /d "%~dp0" || exit /b 1
py -3.12-64 -c "import tkinter" >nul 2>&1
if errorlevel 1 (
    py install 3.12
    py -3.12-64 -c "import tkinter" >nul 2>&1
    if errorlevel 1 goto error
)
if not exist ".venv312\Scripts\python.exe" (
    py -3.12-64 -m venv ".venv312"
    if errorlevel 1 goto error
)
".venv312\Scripts\python.exe" -m pip install --only-binary=:all: "faster-whisper>=1.2.1,<2" sounddevice edge-tts
if errorlevel 1 goto error
".venv312\Scripts\python.exe" -c "import faster_whisper, sounddevice, av, edge_tts"
if errorlevel 1 goto error
echo Vocea este instalata. Deschide Porneste-Jarvis.bat.
pause
exit /b 0
:error
echo Instalarea vocii a esuat. Verifica mesajul de mai sus.
pause
exit /b 1
