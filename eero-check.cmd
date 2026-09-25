@echo off
rem Shows what the eero is reporting for every device right now.
cd /d "%~dp0"
".venv\Scripts\python.exe" -m hogwatch eero-check
echo.
pause
