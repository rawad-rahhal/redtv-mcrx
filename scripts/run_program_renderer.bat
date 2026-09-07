@echo off
setlocal
cd /d "%~dp0\.."
echo Starting Program Renderer (v4.3) on port 8030...
python -m uvicorn program_renderer.service:app --host 0.0.0.0 --port 8030
