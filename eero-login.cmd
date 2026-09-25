@echo off
rem One-time: connect HogWatch to the eero. Your eero account must be an admin
rem on the network (the owner adds you in the eero app). A running HogWatch
rem picks up the login automatically -- no restart needed.
cd /d "%~dp0"
".venv\Scripts\python.exe" -m hogwatch eero-login
echo.
pause
