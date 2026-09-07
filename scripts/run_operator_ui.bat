@echo off
REM ================================================================
REM RED TV MCRX v4.16.2 — PyQt6 Desktop Operator Client
REM ================================================================
setlocal
cd /d "%~dp0\.."

echo.
echo  [RED TV MCRX v4.16.2] Starting Desktop Operator UI...
echo  API base: http://localhost:8000
echo.

python -m operator_ui.desktop_client --api http://localhost:8000
pause
