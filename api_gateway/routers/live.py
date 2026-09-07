"""
api_gateway.routers.live  (v4.0)
==================================
REST endpoints for dual-source program control + Source Slots.

Source Slots
------------
Named live inputs: "cam_1", "cam_2", "whatsapp_1", etc.
Each slot can be independently connected to an SRT URL or file.
In v4, one slot at a time can be "on air" (program_router active source).
Data model supports many; switching is still atomic via ProgramRouter.

Endpoints (v3.3 preserved)
POST /api/control/program/source    — switch PROGRAM to automation|live
POST /api/live/file                 — stage a file for live playback
POST /api/live/srt/connect          — connect SRT camera (proxy to live_ingest)
POST /api/live/stop                 — stop live source
GET  /api/live/status               — live source status

New in v4
POST /api/live/slots/connect        — connect a named slot via live_ingest service
POST /api/live/slots/{name}/disconnect  — disconnect a named slot
POST /api/live/slots/{name}/on_air  — switch named slot to program
GET  /api/live/slots                — all slots status
GET  /api/live/preview/{name}.jpg   — proxy preview JPEG from live_ingest
"""

from __future__ import annotations

import datetime
import logging
import time
import urllib.request
import urllib.error
import json
from typing import Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from fastapi.responses import Response
from pydantic import BaseModel

from shared_contracts.events import BroadcastEvent, Severity
from api_gateway.routers.events import push_event_sync


log = logging.getLogger("redtv.api_gateway.live")
router = APIRouter(prefix="/api")


class TakeRequest(BaseModel):
    force: bool = False



# ── In-memory slot registry (api_gateway owns this) ───────────────────────

class _Slot:
    def __init__(self, name: str) -> None:
        self.name       = name
        self.url        = ""
        self.connected  = False
        self.on_air     = False
        self.last_error = ""
        self.connected_at: Optional[float] = None
        self.preview_available = False
        self.preview_mtime: float = 0.0
        self.frame_age_s: Optional[float] = None
        self.frame_ok: bool = False

    def to_dict(self) -> dict:
        return {
            "name":              self.name,
            "url":               self.url,
            "connected":         self.connected,
            "on_air":            self.on_air,
            "last_error":        self.last_error,
            "connected_at":      self.connected_at,
            "uptime_s":          round(time.monotonic() - self.connected_at, 1)
                                  if self.connected_at else 0,
            "preview_available": self.preview_available,
            "preview_mtime":     self.preview_mtime,
            "frame_age_s":       self.frame_age_s,
            "frame_ok":          self.frame_ok,
        }


_slots: Dict[str, _Slot] = {
    "cam_1":       _Slot("cam_1"),
    "cam_2":       _Slot("cam_2"),
    "whatsapp_1":  _Slot("whatsapp_1"),
}


def get_slots_status() -> List[dict]:
    return [s.to_dict() for s in _slots.values()]


def _refresh_slots_from_ingest(request: Request) -> None:
    """Best-effort refresh: merge live_ingest /status slot info into api slots."""
    try:
        st = _call_ingest("GET", "/status", {}, request=request, timeout=2.0)
        slots = (st or {}).get("slots") or {}
        if not isinstance(slots, dict):
            return
        for name, data in slots.items():
            if name not in _slots:
                _slots[name] = _Slot(name)
            s = _slots[name]
            # mirror essentials
            try:
                s.connected = bool(data.get("connected"))
                s.url = str(data.get("url") or "")
                s.last_error = str(data.get("last_error") or "")
                s.preview_available = bool(data.get("preview_enabled")) and bool(data.get("preview_mtime"))
                s.preview_mtime = float(data.get("preview_mtime") or 0.0)
                fa = data.get("frame_age_s")
                s.frame_age_s = float(fa) if isinstance(fa, (int, float)) else None
                s.frame_ok = bool(data.get("frame_ok"))
            except Exception:
                pass
    except Exception:
        return


# ── Helpers ────────────────────────────────────────────────────────────────

def _router_obj(request: Request):
    r = getattr(request.app.state, "program_router", None)
    if r is None:
        raise HTTPException(503, "ProgramRouter not initialised")
    return r


def _emit(request: Request, event_type: str, data: dict = {}) -> None:
    cb = getattr(request.app.state, "event_callback", None)
    if not cb:
        return
    from shared_contracts.events import BroadcastEvent, Severity
    sev = Severity.WARNING if "ERROR" in event_type or "REFUSED" in event_type else Severity.INFO
    evt = BroadcastEvent(
        service       = "program_router",
        state         = event_type,
        warning       = data.get("reason"),
        severity      = sev,
        timestamp_iso = datetime.datetime.utcnow().isoformat() + "Z",
        extra         = data,
    )
    try:
        cb(evt)
    except Exception:
        pass


def _get_pvw_state(app) -> dict:
    st = getattr(app.state, "pvw_state", None)
    if not isinstance(st, dict):
        st = {"mode": "live", "slot": "cam_1", "updated_at": None}
        app.state.pvw_state = st
    st.setdefault("mode", "live")
    st.setdefault("slot", "cam_1")
    st.setdefault("updated_at", None)
    return st


@router.get("/pvw")
def get_pvw(request: Request):
    return {"ok": True, "pvw": _get_pvw_state(request.app)}


class PVWSetRequest(BaseModel):
    mode: str = "live"   # live | next_auto | program
    slot: str = "cam_1"


@router.post("/pvw/set")
def set_pvw(body: PVWSetRequest, request: Request):
    st = _get_pvw_state(request.app)
    mode = (body.mode or "live").strip().lower()
    if mode not in ("live", "next_auto", "program"):
        raise HTTPException(400, "mode must be one of: live, next_auto, program")
    st["mode"] = mode
    st["slot"] = (body.slot or "cam_1").strip()
    st["updated_at"] = __import__("time").time()
    _emit(request, "PVW_SET", {"mode": st["mode"], "slot": st["slot"]})
    return {"ok": True, "pvw": st}





def _call_ingest(method: str, path: str, body: dict = {},
                 request: Request = None, timeout: float = 5.0) -> dict:
    """
    Call live_ingest_service with retry + timeout.
    Returns parsed JSON response or raises HTTPException.
    """
    cfg = {}
    if request:
        cfg = getattr(request.app.state, "config", {})
    ingest_url = cfg.get("live", {}).get("srt_ingest_service_url", "http://127.0.0.1:8020")
    url = f"{ingest_url}{path}"

    for attempt in range(2):
        try:
            data = None
            headers = {"Content-Type": "application/json"}
            if method.upper() not in ("GET", "HEAD"):
                data = json.dumps(body).encode()
            req = urllib.request.Request(url, data=data, method=method, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            body_txt = e.read().decode(errors="replace")
            if attempt == 1 or e.code < 500:
                raise HTTPException(e.code, detail=f"live_ingest: {body_txt[:200]}")
        except (urllib.error.URLError, OSError) as e:
            if attempt == 1:
                raise HTTPException(503, f"live_ingest unreachable: {e}")
        time.sleep(0.3)
    raise HTTPException(503, "live_ingest call failed")


# ── Request models ─────────────────────────────────────────────────────────

class SourceSwitchRequest(BaseModel):
    source: str


class LiveFileRequest(BaseModel):
    path: str


class SRTConnectRequest(BaseModel):
    name:          str
    url:           str
    verify:        bool  = False   # probe disabled by default (no SRT available yet)
    preview:       bool  = True
    preview_fps:   float = 2.0
    preview_width: int   = 640
    ffmpeg_path:   str   = "ffmpeg"


class SlotConnectRequest(BaseModel):
    name:          str   = "cam_1"
    url:           str
    verify:        bool  = False
    preview:       bool  = True
    preview_fps:   float = 2.0
    preview_width: int   = 640
    ffmpeg_path:   str   = "ffmpeg"


# ── Original v3.3 endpoints (preserved) ───────────────────────────────────

@router.post("/control/program/source")
def set_program_source(req: SourceSwitchRequest, request: Request):
    pr = _router_obj(request)
    success, reason = pr.request_switch(req.source)
    if not success:
        _emit(request, "SWITCH_REFUSED", {"source": req.source, "reason": reason})
        raise HTTPException(409, reason)
    _emit(request, "PROGRAM_SOURCE_CHANGED", {"source": req.source, "reason": reason})
    return {"status": "accepted", "program_source": req.source, "reason": reason}


@router.post("/live/file")
def stage_live_file(req: LiveFileRequest, request: Request):
    pr = _router_obj(request)
    pr._live.stage_file(req.path)
    status = pr._live.get_status()
    if status["state"] == "error":
        _emit(request, "LIVE_FILE_ERROR", {"path": req.path, "error": status["last_error"]})
        raise HTTPException(422, status["last_error"])
    _emit(request, "LIVE_FILE_STAGED", {"path": req.path})
    return {"status": "staging", "path": req.path, "live_state": status["state"]}


@router.post("/live/srt/connect")
def connect_srt(req: SRTConnectRequest, request: Request):
    pr = _router_obj(request)
    pr._live.connect_srt(req.name, req.url)
    _emit(request, "LIVE_SRT_CONNECTING", {"name": req.name, "url": req.url})
    return {"status": "connecting", "name": req.name, "url": req.url}


@router.post("/live/stop")
def stop_live(request: Request):
    pr = _router_obj(request)
    if pr.program_source == "live":
        pr.request_switch("automation")
    pr._live.stop_live()
    _emit(request, "LIVE_STOPPED", {})
    # Also update slot on_air flags
    for s in _slots.values():
        s.on_air = False
    return {"status": "stopped"}


@router.get("/live/status")
def live_status(request: Request):
    pr = _router_obj(request)
    return {"program_source": pr.program_source, **pr._live.get_status()}


# ── v4: Source Slot endpoints ──────────────────────────────────────────────

@router.get("/live/slots")
def list_slots(request: Request):
    """All configured source slots and their current status."""
    _refresh_slots_from_ingest(request)
    return {"slots": get_slots_status()}


@router.post("/live/slots/connect")
def slot_connect(req: SlotConnectRequest, request: Request):
    """
    Connect a named slot through live_ingest service.
    Creates slot entry if it doesn't exist.
    """
    if req.name not in _slots:
        _slots[req.name] = _Slot(req.name)
    slot = _slots[req.name]

    # Proxy to live_ingest
    try:
        result = _call_ingest("POST", "/connect", {
            "name":          req.name,
            "url":           req.url,
            "verify":        req.verify,
            "preview":       req.preview,
            "preview_fps":   req.preview_fps,
            "preview_width": req.preview_width,
            "ffmpeg_path":   req.ffmpeg_path,
        }, request=request, timeout=10.0)
        slot.url              = req.url
        slot.connected        = result.get("connected", False)
        slot.last_error       = result.get("last_error", "")
        slot.connected_at     = time.monotonic()
        slot.preview_available = result.get("preview_enabled", False)
    except HTTPException as e:
        slot.last_error = e.detail
        slot.connected  = False
        _emit(request, "SLOT_CONNECT_ERROR",
              {"name": req.name, "error": e.detail})
        raise

    _emit(request, "SLOT_CONNECTED",
          {"name": req.name, "url": req.url, "connected": slot.connected})
    return {"status": "ok", "slot": slot.to_dict()}



@router.post("/live/slots/{name}/reconnect")
def reconnect_slot(name: str, request: Request):
    """Reconnect a slot using its last known URL or preset default URL."""
    slots = getattr(request.app.state, "slots", None)
    if not isinstance(slots, dict) or name not in slots:
        raise HTTPException(404, f"Unknown slot: {name}")
    slot = slots[name]
    cfg = getattr(request.app.state, "config", {}) or {}
    defaults = ((cfg.get("live_slots") or {}).get("default_urls") or {})
    url = (slot.url or "").strip() or str(defaults.get(name) or "").strip()
    if not url:
        raise HTTPException(400, f"No URL known for slot {name}. Set one with /api/live/slots/connect or preset live_slots.default_urls.{name}")

    # proxy to live_ingest connect
    ingest_url = _live_ingest_base(cfg) + "/slots/connect"
    payload = {"name": name, "url": url}
    try:
        _http_post_json(ingest_url, payload, timeout=2.0)
        slot.url = url
        slot.connected = True
        slot.connected_at = time.time()
        slot.last_error = ""
        push_event_sync(request.app, BroadcastEvent(type="LIVE_SLOT_CONNECTED", severity=Severity.INFO, message=f"{name} reconnected", data={"slot": name}))
        return {"ok": True, "slot": slot.to_dict()}
    except Exception as e:
        slot.last_error = str(e)
        push_event_sync(request.app, BroadcastEvent(type="LIVE_SLOT_DISCONNECTED", severity=Severity.WARN, message=f"{name} reconnect failed", data={"slot": name, "error": str(e)}))
        raise HTTPException(502, f"live_ingest error: {e}")

@router.post("/live/slots/{name}/disconnect")
def slot_disconnect(name: str, request: Request):
    """Disconnect a named slot."""
    if name not in _slots:
        raise HTTPException(404, f"Slot not found: {name}")
    slot = _slots[name]

    # If this slot is on air, switch back to automation first
    if slot.on_air:
        pr = _router_obj(request)
        pr.request_switch("automation")
        pr.tick_control()
        slot.on_air = False

    try:
        _call_ingest("POST", "/disconnect", {"reason": "operator_stop"},
                     request=request, timeout=5.0)
    except HTTPException:
        pass  # Best-effort disconnect

    slot.connected        = False
    slot.preview_available = False
    slot.last_error       = "disconnected by operator"
    _emit(request, "SLOT_DISCONNECTED", {"name": name})
    return {"status": "disconnected", "slot": slot.to_dict()}


@router.post("/live/slots/{name}/on_air")
def slot_on_air(name: str, request: Request, body: TakeRequest | None = None):
    """
    Switch a named slot to PROGRAM output.
    Requires slot to be connected and live adapter to be ready.
    """
    if name not in _slots:
        raise HTTPException(404, f"Slot not found: {name}")
    slot = _slots[name]

    if not slot.connected:
        raise HTTPException(409, f"Slot {name} not connected — connect it first")

    # Stage via live adapter (use SRT connect to make it ready)
    pr = _router_obj(request)
    pr._live.connect_srt(name, slot.url)

    # Wait up to 2s for live to become ready
    deadline = time.time() + 2.0
    while time.time() < deadline:
        if pr._live.is_ready():
            break
        time.sleep(0.05)

    if not pr._live.is_ready():
        raise HTTPException(503, f"Slot {name} live adapter not ready after 2s")

    
    # Guard: refuse TAKE if live preview is stale (unless forced).
    try:
        cfg = getattr(request.app.state, "config", {}) or {}
        thr = float(((cfg.get("guard_runtime") or {}) if isinstance(cfg, dict) else {}).get("live_stale_s", 1.2))
        ingest_base = (cfg.get("live_ingest") or {}).get("base_url", "http://127.0.0.1:8020").rstrip("/")
        st = _http_json(ingest_base + "/status", timeout=1.5)
        slots_map = (st.get("slots") or {}) if isinstance(st, dict) else {}
        sd = slots_map.get(name) or {}
        age = sd.get("frame_age_s")
        ok = bool(sd.get("frame_ok", False))
        if age is None:
            age = 999.0
        age = float(age)
        if (age > thr or not ok) and not (body and body.force):
            _emit(request, "LIVE_FRAME_STALE", {"name": name, "frame_age_s": age, "threshold_s": thr})
            _emit(request, "SWITCH_REFUSED", {"to": "live", "name": name, "reason": "live_frame_stale", "frame_age_s": age, "threshold_s": thr})
            raise HTTPException(409, f"Refused TAKE: slot {name} frame is stale ({age:.2f}s > {thr:.2f}s). Use force=true to override.")
    except HTTPException:
        raise
    except Exception as ex:
        # If status fetch fails, be conservative and refuse unless forced.
        if not (body and body.force):
            _emit(request, "SWITCH_REFUSED", {"to": "live", "name": name, "reason": f"live_status_unavailable:{ex}"})
            raise HTTPException(503, f"Live status unavailable; refused TAKE. ({ex})")

    success, reason = pr.request_switch("live")
    if not success:
        raise HTTPException(409, reason)
    pr.tick_control()

    # Update on_air flags
    for s in _slots.values():
        s.on_air = s.name == name
    slot.on_air = True

    _emit(request, "SLOT_ON_AIR", {"name": name, "url": slot.url})
    return {"status": "on_air", "slot": slot.to_dict()}


@router.get("/live/preview/{name}.jpg")
def slot_preview_jpg(name: str, request: Request):
    """
    Proxy preview JPEG from live_ingest service.
    Returns 404 if slot not connected or preview not available.
    """
    cfg         = getattr(request.app.state, "config", {})
    ingest_url  = cfg.get("live", {}).get("srt_ingest_service_url", "http://127.0.0.1:8020")
    preview_url = f"{ingest_url}/preview/{name}.jpg"

    try:
        with urllib.request.urlopen(preview_url, timeout=2.0) as r:
            data = r.read()
            return Response(content=data, media_type="image/jpeg")
    except urllib.error.HTTPError as e:
        raise HTTPException(e.code, f"preview: {e.reason}")
    except Exception as e:
        raise HTTPException(503, f"preview unavailable: {e}")
