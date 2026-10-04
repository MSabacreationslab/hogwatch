@echo off
rem Builds dist\HogWatch.exe: one file, no console window, the dashboard's files packed inside.
rem The GitHub workflows run this same script, so releases never depend on one PC's setup.
cd /d "%~dp0"
set PY=python
if exist ".venv\Scripts\python.exe" set PY=.venv\Scripts\python.exe
%PY% -m pip install --quiet --disable-pip-version-check -r requirements-build.txt || exit /b 1
%PY% -m PyInstaller --noconfirm --clean --onefile --noconsole --name HogWatch ^
  --icon assets\hogwatch.ico --add-data "hogwatch\static;hogwatch\static" hogwatch_app.py || exit /b 1
echo.
echo Built dist\HogWatch.exe
