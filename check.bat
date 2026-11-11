@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ============================================================
echo  Game Record Tool - Check (diagnose)
echo ============================================================
echo.

if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] .venv was not found.
  echo         Please run setup.bat first.
  echo.
  pause
  exit /b 1
)

".venv\Scripts\python.exe" diagnose.py

echo.
pause
endlocal
