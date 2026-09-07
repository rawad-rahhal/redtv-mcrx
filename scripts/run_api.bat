@echo off
REM ================================================================
REM RED TV MCRX v4.16.2 — API Gateway + Playout Core
REM Port: 8000
REM ================================================================
setlocal
cd /d "%~dp0\.."

echo.
echo  [RED TV MCRX v4.16.2] Starting API Gateway + Playout Core...
echo  Dashboard:  http://localhost:8000/dashboard
echo  API Docs:   http://localhost:8000/docs
echo.

python -m uvicorn api_gateway.app:app --host 0.0.0.0 --port 8000 --reload
pause
