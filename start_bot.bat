@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] .venv not found. Run setup.bat first.
  pause
  exit /b 1
)
echo ============================================================
echo  Game Record Tool - manual start
echo  The bot runs while THIS window stays open.
echo  To stop it, close this window or press Ctrl+C.
echo ============================================================
".venv\Scripts\python.exe" run.py
echo.
echo The program has exited.
echo If this was not intentional, check data\logs\startup.log
echo and run check.bat for a full diagnosis.
pause
