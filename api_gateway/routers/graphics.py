"""
api_gateway.routers.graphics  (v4.0)
======================================
Graphics / CG control plane.

Endpoints
---------
POST /api/graphics/bug/set       — enable bug, set text/position/opacity
POST /api/graphics/bug/clear     — disable bug
POST /api/graphics/l3/set        — stage + TAKE a lower third
POST /api/graphics/l3/clear      — take lower third off air
GET  /api/graphics/state         — full graphics state snapshot

All mutations emit WS events.

DESIGN: Graphics state is owned by api_gateway (in-memory singleton).
playout_core does NOT know about graphics.
A future renderer (DeckLink, NDI, browser-based HTML CG) subscribes to
GET /api/graphics/state (poll) or WS /events (push) and renders accordingly.
"""

from __future__ import annotations

import datetime
import logging
import threading
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from shared_contracts.graphics import (
    BugGraphic, BugPosition,
    LowerThird, LowerThirdStyle, AnimateDir,
    GraphicsState,
)

log = logging.getLogger("redtv.api_gateway.graphics")
router = APIRouter(prefix="/api/graphics")

# ── Singleton graphics state (api_gateway owns it) ─────────────────────────
_gfx_state = GraphicsState()
_gfx_lock  = threading.Lock()


def get_graphics_state() -> dict:
    """Thread-safe snapshot — called by /status and /graphics/state."""
    with _gfx_lock:
        return _gfx_state.to_dict()


# ── Request models ─────────────────────────────────────────────────────────

class BugSetRequest(BaseModel):
    enabled:      bool              = True
    text:         Optional[str]     = None
    position:     str               = "bottom_right"
    opacity:      float             = 0.85
    safe_margins: bool              = True


class LowerThirdSetRequest(BaseModel):
    headline:    str                = ""
    subline:     Optional[str]      = None
    style:       str                = "standard"
    duration_s:  float              = 8.0
    animate_in:  str                = "left"
    animate_out: str                = "left"


# ── Emit helper ────────────────────────────────────────────────────────────

def _emit(request: Request, event_type: str, extra: dict = {}) -> None:
    cb = getattr(request.app.state, "event_callback", None)
    if not cb:
        return
    from shared_contracts.events import BroadcastEvent, Severity
    evt = BroadcastEvent(
        service       = "graphics",
        state         = event_type,
        severity      = Severity.INFO,
        timestamp_iso = datetime.datetime.utcnow().isoformat() + "Z",
        extra         = extra,
    )
    try:
        cb(evt)
    except Exception:
        pass


# ── Endpoints ──────────────────────────────────────────────────────────────

@router.post("/bug/set")
def bug_set(req: BugSetRequest, request: Request):
    """Enable the channel bug with given properties."""
    try:
        pos = BugPosition(req.position)
    except ValueError:
        raise HTTPException(422, f"Invalid position: {req.position}")

    with _gfx_lock:
        _gfx_state.bug = BugGraphic(
            enabled      = req.enabled,
            text         = req.text,
            position     = pos,
            opacity      = req.opacity,
            safe_margins = req.safe_margins,
        )
        _gfx_state.touch()
        snapshot = _gfx_state.bug.to_dict()

    _emit(request, "BUG_UPDATED", snapshot)
    log.info("Bug updated: enabled=%s text=%s pos=%s",
             req.enabled, req.text, req.position,
             extra={"service": "graphics"})
    return {"status": "ok", "bug": snapshot}


@router.post("/bug/clear")
def bug_clear(request: Request):
    """Disable the channel bug."""
    with _gfx_lock:
        _gfx_state.bug.enabled = False
        _gfx_state.touch()
    _emit(request, "BUG_CLEARED", {})
    log.info("Bug cleared", extra={"service": "graphics"})
    return {"status": "ok", "bug_enabled": False}


@router.post("/l3/set")
def l3_set(req: LowerThirdSetRequest, request: Request):
    """Stage and TAKE a lower third immediately."""
    import time
    if not req.headline.strip():
        raise HTTPException(422, "headline must not be empty")

    try:
        style       = LowerThirdStyle(req.style)
        animate_in  = AnimateDir(req.animate_in)
        animate_out = AnimateDir(req.animate_out)
    except ValueError as e:
        raise HTTPException(422, str(e))

    with _gfx_lock:
        _gfx_state.lower_third = LowerThird(
            enabled     = True,
            headline    = req.headline.strip(),
            subline     = req.subline.strip() if req.subline else None,
            style       = style,
            duration_s  = req.duration_s,
            animate_in  = animate_in,
            animate_out = animate_out,
            taken_at    = time.time(),
        )
        _gfx_state.touch()
        snapshot = _gfx_state.lower_third.to_dict()

    _emit(request, "L3_TAKEN", snapshot)
    log.info("L3 TAKE: headline=%r duration=%.1fs",
             req.headline, req.duration_s,
             extra={"service": "graphics"})
    return {"status": "ok", "lower_third": snapshot}


@router.post("/l3/clear")
def l3_clear(request: Request):
    """Take the lower third off air immediately."""
    with _gfx_lock:
        _gfx_state.lower_third.enabled = False
        _gfx_state.touch()
    _emit(request, "L3_CLEARED", {})
    log.info("L3 cleared", extra={"service": "graphics"})
    return {"status": "ok", "lower_third_enabled": False}


@router.get("/state")
def graphics_state(request: Request):
    """Full graphics state snapshot for renderers and operator UI."""
    return get_graphics_state()
