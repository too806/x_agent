@echo off
setlocal
cd /d "%~dp0"
title X_Agent dev launcher

REM ===========================================================================
REM  X_Agent local dev stack: PostgreSQL -> backend (FastAPI) -> frontend (Vite)
REM
REM  Order matters: the backend connects to the DB on startup, so a cold DB
REM  shows up as a wall of OperationalError.
REM
REM  NOTE: this file is deliberately pure ASCII. cmd.exe reads a .bat by byte
REM  offset under the *active* codepage, so non-ASCII text (or a chcp switch
REM  in the middle of the file) makes it mis-parse and drop characters.
REM  Keep it ASCII, keep the messages in English.
REM
REM  Assumes neither the repo path nor the conda path contains a space - the
REM  child windows get their command as one quoted cmd /k "..." string.
REM
REM  Conda is located automatically (see :resolve_conda). To pin it by hand,
REM  set CONDA_ROOT below, without a trailing backslash.
REM ===========================================================================

set "REPO=%CD%"
set "CONDA_ENV=2026-agent"
set "CONDA_ROOT="
set "CONDA_ACTIVATE="
set "DOCKER_DESKTOP=C:\Program Files\Docker\Docker\Docker Desktop.exe"
set "PG_CONTAINER=xagent-postgres"

REM ---------------------------------------------------------------------------
REM  Clear SSL_CERT_FILE *before* conda activate. This is not cosmetic.
REM
REM  conda's own activate.d\openssl_activate.bat only writes the correct path
REM  (%CONDA_PREFIX%\Library\ssl\cacert.pem) if the variable is EMPTY. If it is
REM  already set, activate leaves it alone.
REM
REM  Sourcing activate in Git Bash exports the Unix layout instead
REM  (.../envs/<env>/ssl/cacert.pem), which does not exist on Windows. Carried
REM  into the backend, every outbound HTTPS call dies with FileNotFoundError,
REM  rewrapped as an LLMError that looks like an LLM or X API outage while the
REM  config and credentials are fine. Clearing it here means activate sets the
REM  right value, and the child windows inherit the cleared environment.
REM ---------------------------------------------------------------------------
set "SSL_CERT_FILE="

echo.
echo   ================ X_Agent dev environment ================
echo   repo       : %REPO%
echo   conda env  : %CONDA_ENV%
echo.

REM ---------------------------------------------------------------------------
REM  0. Preflight
REM ---------------------------------------------------------------------------
call :resolve_conda
if not defined CONDA_ACTIVATE goto :bad_conda
set "PYTHON=%CONDA_ROOT%\envs\%CONDA_ENV%\python.exe"
if not exist "%PYTHON%" goto :bad_python
echo   [1/4] python     : %PYTHON%

if not exist "%REPO%\frontend\package.json" goto :no_frontend
where npm >nul 2>&1
if errorlevel 1 echo   [WARN] npm not on PATH - the frontend window will fail
if not exist "%REPO%\frontend\node_modules" echo   [WARN] frontend\node_modules missing - run `npm install` there first
echo   [1/4] frontend   : ok

netstat -an | findstr /i "LISTENING" | findstr /c:":8000" >nul
if not errorlevel 1 echo   [WARN] port 8000 already in use - run.py will exit
netstat -an | findstr /i "LISTENING" | findstr /c:":5173" >nul
if not errorlevel 1 echo   [WARN] port 5173 already in use - vite has strictPort and will fail
echo.

REM ---------------------------------------------------------------------------
REM  1. PostgreSQL (the postgres container is mapped to 5433 here; 5432 may
REM     belong to another project on this machine - it is an .env setting)
REM ---------------------------------------------------------------------------
echo   [2/4] postgres ...
where docker >nul 2>&1
if errorlevel 1 goto :no_docker

docker info >nul 2>&1
if not errorlevel 1 goto :daemon_ok
echo         docker daemon is down, starting Docker Desktop (up to 90s) ...
if exist "%DOCKER_DESKTOP%" start "" "%DOCKER_DESKTOP%"
call :wait_docker
if errorlevel 1 goto :no_daemon
:daemon_ok
docker compose up -d postgres >nul 2>&1
if errorlevel 1 echo         [WARN] `docker compose up -d postgres` failed - check docker
docker start %PG_CONTAINER% >nul 2>&1
call :wait_db
if errorlevel 1 echo         [WARN] postgres did not come up healthy
echo.

REM ---------------------------------------------------------------------------
REM  2 + 3. Backend :8000 and frontend :5173, one window each
REM ---------------------------------------------------------------------------
call :launch
goto :summary

REM ===========================================================================
REM  Subroutines
REM ===========================================================================

REM  Locate the conda install root without hardcoding any machine's path.
REM  Order: an explicit CONDA_ROOT, then the usual per-user installs, then the
REM  machine-wide ones. The first one that actually has Scripts\activate.bat
REM  wins, so a stale directory left behind by an uninstall cannot win.
:resolve_conda
if defined CONDA_ROOT exit /b 0
call :conda_candidate "%USERPROFILE%\miniconda3"
call :conda_candidate "%USERPROFILE%\anaconda3"
call :conda_candidate "%USERPROFILE%\miniforge3"
call :conda_candidate "%LOCALAPPDATA%\miniconda3"
call :conda_candidate "%LOCALAPPDATA%\anaconda3"
call :conda_candidate "%LOCALAPPDATA%\Continuum\anaconda3"
call :conda_candidate "%ProgramData%\miniconda3"
call :conda_candidate "%ProgramData%\anaconda3"
call :conda_candidate "C:\miniconda3"
call :conda_candidate "C:\anaconda3"
exit /b 0

:conda_candidate
if defined CONDA_ACTIVATE exit /b 0
if "%~1"=="" exit /b 0
if not exist "%~1\Scripts\activate.bat" exit /b 0
set "CONDA_ROOT=%~1"
set "CONDA_ACTIVATE=%~1\Scripts\activate.bat"
exit /b 0

:wait_docker
set /a _t=0
:wait_docker_loop
docker info >nul 2>&1
if not errorlevel 1 exit /b 0
call :sleep 5
set /a _t+=5
if %_t% lss 90 goto :wait_docker_loop
exit /b 1

:wait_db
set /a _t=0
:wait_db_loop
set "_st="
for /f "delims=" %%s in ('docker inspect --format "{{.State.Health.Status}}" %PG_CONTAINER% 2^>nul') do set "_st=%%s"
if /i "%_st%"=="healthy" exit /b 0
call :sleep 3
set /a _t+=3
if %_t% lss 60 goto :wait_db_loop
echo         [WARN] %PG_CONTAINER% not healthy after 60s, state: %_st%
exit /b 1

REM  ping, not `timeout`: timeout refuses to run with redirected stdin
REM  ("Input redirection is not supported"), which breaks the loop timing
REM  whenever this script is launched from another script.
:sleep
set /a _n=%1+1
ping -n %_n% 127.0.0.1 >nul
exit /b 0

:launch
echo   [3/4] backend  : http://127.0.0.1:8000
start "X-Agent Backend :8000" cmd /k "call %CONDA_ACTIVATE% %CONDA_ENV% && cd /d %REPO% && python run.py"
echo   [4/4] frontend : http://127.0.0.1:5173
start "X-Agent Frontend :5173" cmd /k "cd /d %REPO%\frontend && npm run dev"
exit /b 0

:summary
echo.
echo   ---------------------------------------------------------------
echo     backend  : http://127.0.0.1:8000   health : /health   docs : /docs
echo     frontend : http://127.0.0.1:5173   the browser only talks to vite,
echo                and vite proxies /api to the backend on 8000
echo     database : see DATABASE_URL in .env (this repo uses 5433)
echo   ---------------------------------------------------------------
echo   Each service has its own window; closing that window stops it.
echo.
call :sleep 5
start "" http://127.0.0.1:5173
echo   Opened the console in your browser. This window can be closed.
echo.
echo   Press any key to exit.
pause >nul
exit /b 0

REM ===========================================================================
REM  Failure exits
REM ===========================================================================
:bad_conda
echo.
echo   [FATAL] no conda install found (looked for Scripts\activate.bat under
echo           USERPROFILE, LOCALAPPDATA and ProgramData).
echo           Set CONDA_ROOT at the top of this file to your conda root
echo           (the folder that contains Scripts\activate.bat).
goto :fail

:bad_python
echo.
echo   [FATAL] no python.exe in conda env %CONDA_ENV%:
echo           %PYTHON%
echo           Create it (conda create -n %CONDA_ENV% python=3.12) or point
echo           CONDA_ENV at the env you actually use.
goto :fail

:no_frontend
echo.
echo   [FATAL] %REPO%\frontend\package.json not found - wrong repo layout?
goto :fail

:no_docker
echo         [WARN] docker not on PATH, skipping the database. The backend will
echo                start but cannot reach postgres.
echo.
goto :launch_then_fail

:no_daemon
echo         [WARN] docker did not come up within 90s, skipping the database.
echo                Start Docker Desktop by hand and re-run this script.
echo         [HINT] a down daemon is what turns pytest into ~555 OperationalError
echo                failures that look like a mass regression.
echo.

:launch_then_fail
call :launch
echo.
echo   Windows are open, but there is no database behind them.
goto :fail

:fail
echo.
echo   Press any key to exit.
pause >nul
exit /b 1
