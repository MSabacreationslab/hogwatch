@echo off
rem Asks HogWatch to shut down cleanly (it also stops its Windows event trace).
curl -s -X POST -H "X-HogWatch: 1" http://127.0.0.1:8765/api/shutdown >nul 2>&1 && (echo HogWatch stopped.) || (echo HogWatch wasn't running.)
timeout /t 3 >nul
