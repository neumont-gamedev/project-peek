@echo off
REM Double-click to force a full refresh now: collect -> build -> sync to the web app.
REM Loads secrets.local.ps1 for your tokens (GitHub/Trello) and Firebase creds.
cd /d "%~dp0"
echo Running Project Peek sync... (this window will show progress)
echo.
powershell -NoProfile -ExecutionPolicy Bypass -Command ". './secrets.local.ps1'; python run_all.py; python sync_firestore.py"
echo.
echo ============================================================
echo Done. Refresh the web app to see the update. Press any key to close.
pause >nul
