@echo off
REM ================================================================
REM RED TV MCRX MASTER CONTROL v4.16.2 — Start All Services
REM ================================================================
REM
REM Starts:
REM   [1] API Gateway + Playout Core    — port 8000
REM   [2] Live Ingest Service           — port 8020
REM   [3] Automation AI (Nashra)        — port 8001
REM   [4] Media Factory                 — background worker
REM
REM Requirements:
REM   pip install -e .
REM   FFmpeg on PATH (or set FFMPEG_PATH)
REM
REM ================================================================
setlocal

SET ROOT=%~dp0..
SET PYTHON=python

echo.
echo  *** RED TV MCRX MASTER CONTROL v4.16.2 ***
echo.

REM Check Python
%PYTHON% --version >nul 2>&1
IF ERRORLEVEL 1 (
    echo ERROR: Python not found on PATH. Install Python 3.10+.
    pause
    exit /b 1
)

REM Check FFmpeg
ffmpeg -version >nul 2>&1
IF ERRORLEVEL 1 (
    echo WARNING: FFmpeg not found on PATH.
    echo          Live ingest preview will not work without it.
    echo          Download: https://ffmpeg.org/download.html
    echo.
)

REM Validate config
%PYTHON% -c "from shared_contracts.config_validator import validate_config; validate_config()" 2>nul
IF ERRORLEVEL 1 (
    echo WARNING: Config validation failed. Check config\redtv.yaml
)

echo.
echo [1/4] Starting Live Ingest Service (port 8020)...
start "REDTV Live Ingest" cmd /k "cd /d %ROOT% && %PYTHON% -m live_ingest.main"
timeout /t 1 /nobreak >nul

echo [2/4] Starting API Gateway + Playout Core (port 8000)...
start "REDTV API Gateway" cmd /k "cd /d %ROOT% && %PYTHON% -m uvicorn api_gateway.app:app --host 0.0.0.0 --port 8000"
timeout /t 2 /nobreak >nul

echo [3/4] Starting Automation AI - Nashra (port 8001)...
start "REDTV Automation AI" cmd /k "cd /d %ROOT% && %PYTHON% -m automation_ai.main"
timeout /t 1 /nobreak >nul

echo [4/4] Starting Media Factory...
start "REDTV Media Factory" cmd /k "cd /d %ROOT% && %PYTHON% -m media_factory.main"

echo.
echo  ================================================================
echo   Services started.
echo  ================================================================
echo   MultiView Dashboard:  http://localhost:8000/dashboard
echo   API Docs:             http://localhost:8000/docs
echo   Live Ingest:          http://localhost:8020/docs
echo   Automation AI:        http://localhost:8001/docs
echo  ================================================================
echo.
echo  Run scripts\run_operator_ui.bat to open the desktop client.
echo  To stop: close each service window.
echo.
pause
