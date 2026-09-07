"""
api_gateway.routers.guard (v4.3)
================================
Exposes the last playlist preflight report (Broadcast Guard).
"""
from __future__ import annotations

from fastapi import APIRouter, Request


def _get_ack_state(app) -> dict:
    st = getattr(app.state, "guard_ack", None)
    if isinstance(st, dict):
        return st
    st = {"last_ack_at": None, "acked_violation_count": 0, "runtime_last_ack_at": None, "runtime_acked_count": 0}
    app.state.guard_ack = st
    return st

router = APIRouter(prefix="/api")

@router.get("/guard/report")
def guard_report(request: Request):
    rep = getattr(request.app.state, "guard_report", None)
    return rep or {"ok": None, "message": "No guard report yet. Load a playlist first."}


@router.get("/guard/snapshot")
def guard_snapshot(request: Request):
    """Runtime snapshot of the RealTimeGuard.

    This is separate from the playlist preflight report.
    """
    engine = getattr(request.app.state, "engine", None)
    guard = getattr(engine, "_guard", None) if engine else None
    ack = _get_ack_state(request.app)

    if guard is None:
        return {"ok": False, "error": "Guard not wired"}

    try:
        recent = guard.recent_violations
        # Monotonic timestamps are intentionally process-local diagnostics.
        recent_out = [{"operation": op, "ts_monotonic": float(ts)} for (op, ts) in recent]
        return {
            "ok": True,
            "violation_count": int(guard.violation_count),
            "recent": recent_out,
            "ack": ack,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.post("/guard/ack")
def guard_ack(request: Request):
    """Operator acknowledges current guard violations (does not clear history)."""
    engine = getattr(request.app.state, "engine", None)
    guard = getattr(engine, "_guard", None) if engine else None
    ack = _get_ack_state(request.app)
    if guard is None:
        return {"ok": False, "error": "Guard not wired"}
    try:
        now = __import__("time").time()
        ack["last_ack_at"] = now
        ack["runtime_last_ack_at"] = now
        ack["acked_violation_count"] = int(guard.violation_count)
        # runtime alert count is computed in /api/debug/runtime
        try:
            # if runtime snapshot was already computed, preserve last count; else keep
            pass
        except Exception:
            pass
        return {"ok": True, "ack": ack}
    except Exception as e:
        return {"ok": False, "error": str(e)}
