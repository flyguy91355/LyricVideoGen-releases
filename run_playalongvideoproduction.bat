@echo off
rem Launches PlayAlongVideoProduction on Windows. Double-click this file, or
rem run it from a terminal. Linux/macOS users run run_playalongvideoproduction.sh.
rem Calls the venv's own python.exe directly (no activate) for the same reason
rem the .sh launcher does -- see the comment there.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo No .venv found. Create it first with Python 3.11:
    echo     uv venv --python 3.11 .venv ^&^& uv pip install --python .venv\Scripts\python.exe -r requirements.txt
    pause
    exit /b 1
)
".venv\Scripts\python.exe" -m lyricvideo.gui
if errorlevel 1 pause
