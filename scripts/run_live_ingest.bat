@echo off
setlocal
cd /d "%~dp0\.."
echo Starting LIVE INGEST service on 8020...
python -m live_ingest.service
