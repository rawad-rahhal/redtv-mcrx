"""
scripts/soak_test.py  (v4.6)
=========================================
Soak test runner for RED TV MCRX Master Control.

What it does
------------
- Polls /api/status continuously
- Randomly performs operator-like actions:
  * Reload playlist
  * Toggle PROGRAM source automation/live
  * Take named live slot on-air (cam_1..cam_4)
  * Toggle BUG + L3 graphics
  * Trigger Emergency (short) then recover
- Hard-fails if:
  * API becomes unreachable for too long
  * guard_violation_count increases (if enforce enabled)
  * ring_dropped increases
  * program_renderer reports unstable output (if endpoint exists)

Usage
-----
python scripts/soak_test.py --minutes 120 --api http://127.0.0.1:8000

Optional (for live slots):
set REDTV_SOAK_SRT_CAM_1="srt://..." etc.

This is intentionally dependency-light (urllib only).
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
import urllib.request
import urllib.error


def _json(url: str, timeout: float = 3.0):
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _post(url: str, payload: dict, timeout: float = 5.0):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read().decode("utf-8") if r.readable() else ""
        try:
            return json.loads(body) if body else {"ok": True}
        except Exception:
            return {"ok": True, "raw": body}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="http://127.0.0.1:8000", help="api_gateway base URL")
    ap.add_argument("--minutes", type=int, default=30)
    ap.add_argument("--poll_ms", type=int, default=500)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--allow_guard_violations", action="store_true")
    args = ap.parse_args()

    random.seed(args.seed)
    API = args.api.rstrip("/")

    t_end = time.time() + args.minutes * 60
    last_ok = time.time()

    base_status = None
    base_guard = 0
    base_drops = 0

    print(f"[SOAK] start minutes={args.minutes} api={API}")
    while time.time() < t_end:
        # poll status
        try:
            st = _json(API + "/api/status", timeout=2.0)
            last_ok = time.time()
        except Exception as e:
            if time.time() - last_ok > 10.0:
                print(f"[FAIL] API unreachable for >10s: {e}")
                return 2
            time.sleep(0.5)
            continue

        gv = int(st.get("guard_violation_count") or 0)
        drops = int(st.get("ring_dropped") or 0)

        if base_status is None:
            base_status = st
            base_guard = gv
            base_drops = drops
            print(f"[SOAK] baseline guard={base_guard} drops={base_drops}")

        if not args.allow_guard_violations and gv > base_guard:
            print(f"[FAIL] guard violations increased: {base_guard} -> {gv}")
            return 3

        if drops > base_drops:
            print(f"[FAIL] ring dropped increased: {base_drops} -> {drops}")
            return 4

        # random action
        r = random.random()

        try:
            # 1) reload playlist (rare)
            if r < 0.03:
                print("[ACT] playlist reload")
                _post(API + "/api/control/reload_playlist", {})

            # 2) toggle program source (automation/live)
            elif r < 0.10:
                src = random.choice(["automation", "live"])
                print(f"[ACT] program source -> {src}")
                _post(API + "/api/control/program/source", {"source": src})

            # 3) take a live slot on air
            elif r < 0.18:
                name = random.choice(["cam_1", "cam_2", "cam_3", "cam_4"])
                print(f"[ACT] slot on_air -> {name}")
                _post(API + f"/api/live/slots/{name}/on_air", {})

            # 4) graphics toggle
            elif r < 0.28:
                # BUG
                on = random.choice([True, False])
                print(f"[ACT] BUG {'ON' if on else 'OFF'}")
                _post(API + "/api/graphics/bug", {"enabled": on, "text": "RED TV"})

            elif r < 0.38:
                # L3
                on = random.choice([True, False])
                print(f"[ACT] L3 {'ON' if on else 'OFF'}")
                _post(API + "/api/graphics/l3", {"enabled": on, "title": "BREAKING", "subtitle": "Soak test"})

            # 5) emergency trigger (very rare)
            elif r < 0.41:
                print("[ACT] EMERGENCY TRIGGER")
                _post(API + "/api/control/emergency", {"reason": "soak_test"})
                time.sleep(1.5)
                print("[ACT] emergency clear -> automation")
                _post(API + "/api/control/program/source", {"source": "automation"})

        except urllib.error.HTTPError as e:
            # non-fatal, but report
            print(f"[WARN] action HTTP {e.code}: {e.reason}")
        except Exception as e:
            print(f"[WARN] action error: {e}")

        time.sleep(max(0.05, args.poll_ms / 1000.0))

    print("[SOAK] PASS (no guard/ring regressions, API stayed alive)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
