@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion
cd /d "%~dp0"

REM ------------------------------------------------------------
REM setup.bat keeps its OWN log (data\logs\setup_bat.log): one
REM line per step, written as the step happens. Through v1.7 the
REM diagnose tool could not tell whether setup.bat had even been
REM run in a folder, or how far it got. Now it can - check.bat
REM includes this log in its report.
REM ------------------------------------------------------------
if not exist "data\logs" mkdir "data\logs"
set "SETUP_LOG=%~dp0data\logs\setup_bat.log"
echo ==== setup.bat started %DATE% %TIME% ====>> "%SETUP_LOG%"

echo ============================================================
echo  Game Record Tool - Setup
echo ============================================================
echo  This script is safe to run again. Your data folder
echo  (data\) will never be deleted or overwritten.
echo.

REM ------------------------------------------------------------
REM (1) Check Python 3.12+
REM ------------------------------------------------------------
echo [1/7] Checking Python ...
echo [1/7] Checking Python ...>> "%SETUP_LOG%"

where python >nul 2>nul
if errorlevel 1 goto :python_missing
REM W-check B1: keep the WHERE answer in the log. "No python
REM at all" and "only the Microsoft Store stub was found" used
REM to collapse into the same log line; the paths separate them.
for /f "delims=" %%p in ('where python 2^>nul') do echo        where python: %%p>> "%SETUP_LOG%"

set "PYVER="
for /f "tokens=2" %%v in ('python --version 2^>^&1') do set "PYVER=%%v"
if not defined PYVER goto :python_missing

REM W-check B1 (adversarial QA S-1): the Microsoft Store stub
REM also answers to "python" but is not an interpreter. Probe
REM for a REAL interpreter before trusting the version string;
REM a stub fails this probe and gets the same guidance as a
REM missing Python.
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 12) else 3)" >nul 2>nul
if errorlevel 3 goto :python_old
if errorlevel 1 goto :python_missing

for /f "tokens=1,2 delims=." %%a in ("%PYVER%") do (
  set "PYMAJOR=%%a"
  set "PYMINOR=%%b"
)
if not defined PYMAJOR goto :python_missing
if not defined PYMINOR set "PYMINOR=0"

echo        Found Python %PYVER%
echo        Found Python %PYVER%>> "%SETUP_LOG%"

if %PYMAJOR% LSS 3 goto :python_old
if %PYMAJOR% EQU 3 if %PYMINOR% LSS 12 goto :python_old
goto :python_ok

:python_missing
echo [ERROR] step 1: Python not found>> "%SETUP_LOG%"
echo.
echo [ERROR] Python was not found.
echo         Python 3.12 or later is required.
echo.
echo         How to install (run in a NEW Command Prompt):
echo           winget install -e --id Python.Python.3.12
echo.
echo         Or install manually from https://www.python.org/
echo         See SETUP_GUIDE.md section 2 for details.
echo         After installing Python, run setup.bat again.
echo.
pause
exit /b 1

:python_old
echo [ERROR] step 1: Python %PYVER% too old>> "%SETUP_LOG%"
echo.
echo [ERROR] Python %PYVER% is too old. Python 3.12 or later is required.
echo.
echo         How to install (run in a NEW Command Prompt):
echo           winget install -e --id Python.Python.3.12
echo.
echo         Or install manually from https://www.python.org/
echo         See SETUP_GUIDE.md section 2 for details.
echo         After installing Python, run setup.bat again.
echo.
pause
exit /b 1

:python_ok

REM ------------------------------------------------------------
REM (2) Create virtual environment (.venv)
REM ------------------------------------------------------------
echo [2/7] Checking virtual environment (.venv) ...
echo [2/7] Checking virtual environment ...>> "%SETUP_LOG%"

if exist ".venv\Scripts\python.exe" (
  echo        .venv already exists. Skipping creation.
) else (
  python -m venv .venv
  if errorlevel 1 (
    echo [ERROR] step 2: venv creation failed>> "%SETUP_LOG%"
    echo.
    echo [ERROR] Failed to create .venv.
    echo         Check that antivirus software is not blocking it,
    echo         then run setup.bat again.
    echo.
    pause
    exit /b 1
  )
  echo        .venv created.
)
echo        venv OK>> "%SETUP_LOG%"

REM ------------------------------------------------------------
REM (3) Register automatic start.
REM     This step runs BEFORE the package install on purpose:
REM     setup_tasks.py uses only the Python standard library, so
REM     registration no longer depends on pip succeeding first.
REM     It tries, in order: schtasks, PowerShell
REM     Register-ScheduledTask, then Startup-folder shortcuts -
REM     verifying each route, and logging every command output
REM     to data\logs\setup_tasks.log.
REM ------------------------------------------------------------
echo [3/7] Registering automatic start ...
echo [3/7] Registering automatic start ...>> "%SETUP_LOG%"

".venv\Scripts\python.exe" setup_tasks.py
if errorlevel 1 (
  echo [ERROR] step 3: autostart registration failed on all routes>> "%SETUP_LOG%"
  echo.
  echo [ERROR] Automatic-start registration failed on ALL routes.
  echo         Setup is NOT complete: the tool will NOT start
  echo         automatically. The exact errors from each route are
  echo         logged in data\logs\setup_tasks.log - run check.bat
  echo         and send the output to the distributor.
  echo.
  pause
  exit /b 1
)
echo        Automatic start registered and verified.
echo        autostart registered and verified>> "%SETUP_LOG%"

REM ------------------------------------------------------------
REM (4) Install packages from requirements.txt
REM     (free public packages only)
REM ------------------------------------------------------------
echo [4/7] Installing packages (this may take a few minutes) ...
echo [4/7] Installing packages ...>> "%SETUP_LOG%"

".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 (
  echo [ERROR] step 4: pip self-upgrade failed>> "%SETUP_LOG%"
  echo.
  echo [ERROR] Failed to update pip. Check your network connection.
  echo         See SETUP_GUIDE.md section 11 for troubleshooting.
  echo.
  pause
  exit /b 1
)

".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
  echo [ERROR] step 4: pip install failed>> "%SETUP_LOG%"
  echo.
  echo [ERROR] Failed to install packages from requirements.txt.
  echo         Check your network connection and run setup.bat again.
  echo         See SETUP_GUIDE.md section 11 for troubleshooting.
  echo.
  pause
  exit /b 1
)
echo        Installing the free collector browser runtime ...
echo Playwright Chromium setup started>> "%SETUP_LOG%"
".venv\Scripts\python.exe" -m playwright install chromium >> "%SETUP_LOG%" 2>&1
if errorlevel 1 (
  echo [ERROR] Playwright Chromium setup failed>> "%SETUP_LOG%"
  echo.
  echo [ERROR] The catalog collector browser could not be installed.
  echo         See data\logs\setup_bat.log for the complete output.
  echo.
  pause
  exit /b 1
)
echo Playwright Chromium setup completed>> "%SETUP_LOG%"
echo        Packages installed.
echo        packages installed>> "%SETUP_LOG%"

REM ------------------------------------------------------------
REM (5) Create data folder and copy .env.template to data\.env
REM     NEVER overwrite an existing data\.env
REM ------------------------------------------------------------
echo [5/7] Checking data folder and .env ...
echo [5/7] Checking data folder and .env ...>> "%SETUP_LOG%"

if not exist "data" mkdir "data"
if not exist "data" (
  echo [ERROR] step 5: could not create the data folder>> "%SETUP_LOG%"
  echo.
  echo [ERROR] The data folder could not be created.
  echo         Check folder permissions and antivirus, then run setup.bat again.
  echo.
  pause
  exit /b 1
)

if exist "data\.env" (
  echo        data\.env already exists. It was NOT overwritten.
) else (
  if exist ".env.template" (
    copy /Y ".env.template" "data\.env" >nul
    if not exist "data\.env" (
      echo [ERROR] step 5: copying .env.template to data/.env failed>> "%SETUP_LOG%"
      echo.
      echo [ERROR] data\.env could not be created.
      echo         Check that the data folder is writable, then run setup.bat again.
      echo.
      pause
      exit /b 1
    )
    echo        data\.env created from .env.template.
    echo        Next, open data\.env and paste your tokens.
    echo        See SETUP_GUIDE.md sections 4 and 5.
  ) else (
    echo [ERROR] step 5: .env.template not found>> "%SETUP_LOG%"
    echo.
    echo [ERROR] .env.template was not found.
    echo         Check that the ZIP was extracted correctly.
    echo.
    pause
    exit /b 1
  )
)
if exist "data\.env" (
  echo        data/.env OK>> "%SETUP_LOG%"
) else (
  echo [ERROR] step 5: data/.env missing after the copy step>> "%SETUP_LOG%"
)

REM ------------------------------------------------------------
REM (6) Initialize database and import seed data
REM ------------------------------------------------------------
echo [6/7] Initializing database and importing seed data ...
echo [6/7] Initializing database ...>> "%SETUP_LOG%"

set "PYTHONPATH=%~dp0src;%PYTHONPATH%"
if not defined GRT_DATA_DIR set "GRT_DATA_DIR=%~dp0data"

".venv\Scripts\python.exe" -m grt.migrate
if errorlevel 1 (
  echo [ERROR] step 6: database init failed>> "%SETUP_LOG%"
  echo.
  echo [ERROR] Database initialization failed in grt.migrate.
  echo         Check that data\.env is correct and that the data folder
  echo         is writable, then run setup.bat again.
  echo         See SETUP_GUIDE.md section 11 for troubleshooting.
  echo.
  pause
  exit /b 1
)
echo        Database is ready.
echo        database ready>> "%SETUP_LOG%"

REM ------------------------------------------------------------
REM (7) Start the bot NOW and confirm it actually booted.
REM     Setup is complete only when a fresh boot record appears
REM     within 120 seconds (wait_start.py checks startup.log and
REM     the heartbeat). If the tokens are not pasted yet, this
REM     step is skipped with instructions instead of failing.
REM ------------------------------------------------------------
echo [7/7] Starting the bot and confirming it is alive ...
echo [7/7] Starting the bot ...>> "%SETUP_LOG%"

".venv\Scripts\python.exe" setup_tasks.py --check-tokens
if errorlevel 1 (
  echo        tokens not set yet - start check deferred>> "%SETUP_LOG%"
  echo.
  echo        NOTE: your tokens are not in data\.env yet, so the
  echo        bot cannot start. That is the only remaining step:
  echo          1. Paste TELEGRAM_BOT_TOKEN and DISCOGS_TOKEN into
  echo             data\.env. See SETUP_GUIDE.md sections 4 and 5.
  echo          2. Run setup.bat again - earlier steps finish
  echo             quickly, and this start check will then run.
  echo.
  goto :finish
)

".venv\Scripts\python.exe" setup_tasks.py --start
if errorlevel 1 (
  echo [ERROR] immediate start incomplete>> "%SETUP_LOG%"
  echo.
  echo [ERROR] Immediate startup is incomplete. The bot or
  echo         watchdog may already be running. Check:
  echo         data\logs\setup_tasks.log
  echo.
  pause
  exit /b 1
)

".venv\Scripts\python.exe" wait_start.py --timeout 120
if errorlevel 1 (
  echo [ERROR] step 7: bot did not start within 120s>> "%SETUP_LOG%"
  echo.
  echo !!! SETUP IS NOT COMPLETE: the bot did not start. !!!
  echo         Run check.bat and send the output to the
  echo         distributor. Logs: data\logs\startup.log and
  echo         data\logs\setup_tasks.log
  echo.
  pause
  exit /b 1
)
echo        The bot is running now.
echo        BOT RUNNING - setup complete>> "%SETUP_LOG%"

:finish
echo.
echo ============================================================
echo  Setup finished.
echo ============================================================
echo.
echo  Next steps:
echo    1. If setup asked you to paste tokens into data\.env,
echo       do that now (SETUP_GUIDE.md sections 4 and 5) and run
echo       setup.bat once more.
echo    2. In Telegram, send /status to your Bot. A reply means
echo       everything works. (First time only: if the Bot asks
echo       about the quiet-hours exception, tap a button.)
echo.
pause
endlocal
exit /b 0
