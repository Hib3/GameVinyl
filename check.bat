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
