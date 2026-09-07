#!/usr/bin/env bash
# RED TV MCRX MASTER CONTROL v4.16.2 — Linux/Mac startup
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo ""
echo "  *** RED TV MCRX MASTER CONTROL v4.16.2 ***"
echo ""

command -v ffmpeg >/dev/null 2>&1 || echo "WARNING: FFmpeg not found — live preview will fail"

echo "[1/4] Live Ingest Service (port 8020)..."
python -m live_ingest.main &
sleep 0.5

echo "[2/4] API Gateway + Playout Core (port 8000)..."
python -m uvicorn api_gateway.app:app --host 0.0.0.0 --port 8000 &
sleep 1

echo "[3/4] Automation AI (port 8001)..."
python -m automation_ai.main &

echo "[4/4] Media Factory..."
python -m media_factory.main &

echo ""
echo "  MultiView Dashboard: http://localhost:8000/dashboard"
echo "  API Docs:            http://localhost:8000/docs"
echo "  Live Ingest:         http://localhost:8020/docs"
echo ""
echo "  Press Ctrl+C to stop all services."
wait
