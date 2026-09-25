@echo off
rem Starts HogWatch in the background with administrator rights (Windows only
rem shares "which program is using the network" with admin tools), then opens
rem the dashboard. Windows will ask "Do you want to allow this app to make
rem changes?" -- click Yes. Closing the browser does NOT stop HogWatch; use
rem stop-hogwatch.cmd for that.
cd /d "%~dp0"
if not exist ".venv\Scripts\pythonw.exe" (
  echo HogWatch isn't set up yet. Double-click setup.cmd first.
  pause
  exit /b 1
)
rem The dashboard is opened from this (non-admin) side so the browser doesn't run as admin.
powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath '%~dp0.venv\Scripts\pythonw.exe' -ArgumentList '-m','hogwatch','run','--no-browser' -WorkingDirectory '%~dp0' -Verb RunAs; Start-Sleep -Seconds 5; Start-Process 'http://127.0.0.1:8765/'"
