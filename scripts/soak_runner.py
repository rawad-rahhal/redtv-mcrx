
"""scripts.soak_runner
Runs a soak probe against api_gateway for N minutes and reports anomalies.

Usage:
  python -m scripts.soak_runner --base http://127.0.0.1:8000 --minutes 60
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request


def _get(url: str, timeout: float = 2.0) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", errors="replace"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8000", help="api_gateway base url")
    ap.add_argument("--minutes", type=int, default=10)
    ap.add_argument("--drift_ms", type=float, default=35.0)
    ap.add_argument("--frame_age_s", type=float, default=1.5)
    ap.add_argument("--max_restarts", type=int, default=1)
    args = ap.parse_args()

    base = args.base.rstrip("/")
    end = time.time() + args.minutes * 60
    anomalies = []

    last_restarts = None

    while time.time() < end:
        try:
            st = _get(f"{base}/api/status", timeout=2.0)
            rt = _get(f"{base}/api/debug/runtime", timeout=2.0)
        except Exception as e:
            anomalies.append({"ts": time.time(), "type": "HTTP_ERROR", "error": str(e)})
            time.sleep(1.0)
            continue

        eng = (rt.get("engine") or {})
        drift = float((eng.get("timing") or {}).get("tick_drift_ms") or 0.0)
        if drift > args.drift_ms:
            anomalies.append({"ts": time.time(), "type": "DRIFT_SPIKE", "drift_ms": drift})

        renderer = rt.get("renderer") or {}
        if isinstance(renderer, dict):
            restarts = int(renderer.get("encoder_restarts") or 0)
            if last_restarts is None:
                last_restarts = restarts
            elif restarts - last_restarts > args.max_restarts:
                anomalies.append({"ts": time.time(), "type": "RENDERER_RESTARTS", "count": restarts, "prev": last_restarts})
                last_restarts = restarts
            lfts = float(renderer.get("last_frame_ts") or 0.0)
            if lfts and (time.time() - lfts) > args.frame_age_s:
                anomalies.append({"ts": time.time(), "type": "RENDERER_FRAME_STALE", "age_s": time.time() - lfts})

        live = rt.get("live_ingest") or {}
        slots = (live.get("slots") or []) if isinstance(live, dict) else []
        for s in slots:
            if s.get("is_on_air") and float(s.get("frame_age_s") or 0.0) > args.frame_age_s:
                anomalies.append({"ts": time.time(), "type": "LIVE_ONAIR_STALE", "slot_id": s.get("slot_id"), "age_s": s.get("frame_age_s")})

        time.sleep(1.0)

    if anomalies:
        print(json.dumps({"ok": False, "anomalies": anomalies}, ensure_ascii=False, indent=2))
        return 2
    print(json.dumps({"ok": True, "minutes": args.minutes}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
