@echo off
chcp 65001 >nul
cd /d "%~dp0"
call :set_shared_data_dir
if not defined GRT_HOME_FOUND (
  echo [ERROR] Could not locate the GameRecordTool project folder.
  exit /b 1
)
if not defined GRT_DATA_DIR set "GRT_DATA_DIR=%GRT_HOME%\data"
for %%I in ("%GRT_DATA_DIR%") do set "GRT_DATA_DIR=%%~fI"
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
".venv\Scripts\python.exe" task_launcher.py --data-dir "%GRT_DATA_DIR%" --script "run.py"
echo.
echo The program has exited.
echo If this was not intentional, check data\logs\startup.log
echo and run check.bat for a full diagnosis.
pause
exit /b

:set_shared_data_dir
set "GRT_HOME=%~dp0."
set "GRT_HOME_FOUND="
:find_shared_data_home
for %%I in ("%GRT_HOME%") do set "GRT_HOME=%%~fI"
for %%I in ("%GRT_HOME%") do if /i "%%~nxI"=="GameRecordTool" (
  set "GRT_HOME_FOUND=1"
  if not defined GRT_DATA_DIR set "GRT_DATA_DIR=%%~fI\data"
  exit /b 0
)
for %%I in ("%GRT_HOME%\..") do set "GRT_PARENT=%%~fI"
if /i "%GRT_HOME%"=="%GRT_PARENT%" exit /b 1
set "GRT_HOME=%GRT_PARENT%"
goto :find_shared_data_home
