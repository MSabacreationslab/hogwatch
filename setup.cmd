@echo off
rem First-time setup: makes a private Python environment and installs the one
rem dependency (psutil). Needs Python 3.10 or newer.
cd /d "%~dp0"
python -m venv .venv || (
  echo Python 3.10+ is needed: https://www.python.org/downloads/
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt
echo.
echo Setup done. Double-click start-hogwatch.cmd to start monitoring.
pause
