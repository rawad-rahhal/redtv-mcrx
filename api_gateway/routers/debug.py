
from __future__ import annotations

import json
import time
import urllib.request
from typing import Any, Dict

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from api_gateway.routers.graphics import get_graphics_state


def _get_runtime_ack(app) -> dict:
    st = getattr(app.state, "guard_ack", None)
    if not isinstance(st, dict):
        st = {"last_ack_at": None, "acked_violation_count": 0}
        app.state.guard_ack = st
    st.setdefault("runtime_last_ack_at", None)
    st.setdefault("runtime_acked_count", 0)
    return st

def _now() -> float:
    return __import__("time").time()

def _thresholds(cfg: dict) -> dict:
    rt = (cfg.get("guard_runtime") or {}) if isinstance(cfg, dict) else {}
    return {
        "tick_drift_ms": float(rt.get("tick_drift_ms", 80.0)),
        "renderer_freeze_s": float(rt.get("renderer_freeze_s", 1.5)),
        "live_stale_s": float(rt.get("live_stale_s", 1.2)),
    }

router = APIRouter(prefix="/api")


def _http_json(url: str, timeout: float = 1.5) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", errors="replace"))


@router.get("/debug/runtime")
def runtime_snapshot(request: Request):
    app = request.app
    cfg = getattr(app.state, "config", {}) or {}

    out: Dict[str, Any] = {
        "ts": time.time(),
        "version": getattr(app.state, "version", "unknown"),
        "uptime_s": time.time() - float(getattr(app.state, "started_ts", time.time())),
    }

    engine = getattr(app.state, "engine", None)
    if engine:
        try:
            out["engine"] = engine.get_status()
            # guard lives inside engine
            g = getattr(engine, "_guard", None)
            if g and hasattr(g, "snapshot"):
                out["guard"] = g.snapshot()
            else:
                out["guard"] = {"error": "guard missing"}
        except Exception as e:
            out["engine"] = {"error": str(e)}
            out["guard"] = {"error": str(e)}
    else:
        out["engine"] = {"error": "engine not wired"}
        out["guard"] = {"error": "engine not wired"}

    # graphics singleton snapshot
    try:
        out["graphics"] = get_graphics_state()
    except Exception as e:
        out["graphics"] = {"error": str(e)}

    # live ingest status
    live_base = ((cfg.get("live_ingest") or {}).get("base_url") or "http://127.0.0.1:8020").rstrip("/")
    try:
        out["live_ingest"] = _http_json(f"{live_base}/status", timeout=1.0)
    except Exception as e:
        out["live_ingest"] = {"error": str(e), "base_url": live_base}

    # renderer status
    r_base = ((cfg.get("program_renderer") or {}).get("base_url") or "http://127.0.0.1:8030").rstrip("/")
    try:
        out["renderer"] = _http_json(f"{r_base}/status", timeout=1.0)
    except Exception as e:
        out["renderer"] = {"error": str(e), "base_url": r_base}

    return JSONResponse(out)
