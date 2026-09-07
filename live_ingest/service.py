"""
live_ingest.service  (v4.6)
============================
Multi-slot SRT/File Live Ingest Service — standalone FastAPI service (default port 8020).

Goals:
- Keep playout_core RT-safe by isolating all network/subprocess decode here.
- Manage multiple named live slots (cam_1, cam_2, whatsapp_1, ...).
- Provide operational status per slot (connected / error / last_seen).
- Provide lightweight preview per slot (latest JPEG frame + optional MJPEG stream).

Design:
- /connect starts (optionally) a verify probe and a preview capture loop.
- Preview capture uses FFmpeg to write a single JPEG repeatedly (image2 -update 1),
  so gateway/UI can fetch GET /preview/{name}.jpg without parsing a stream.
- Atomic writes are used for preview frames where possible.

Notes:
- This is a preview + health layer. It does NOT yet provide decoded program frames to playout_core.
- For production, run this service on the same workstation as the SRT NIC and FFmpeg binaries.
"""
from __future__ import annotations

import os
import time
import json
import shutil
import signal
import subprocess
import threading
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, Optional, Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------
# Config / paths
# ---------------------------------------------------------------------
DEFAULT_PREVIEW_DIR = Path(os.getenv("REDTV_LIVE_PREVIEW_DIR", str(Path.cwd() / "runtime" / "live_previews")))
DEFAULT_PREVIEW_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_FFMPEG = os.getenv("REDTV_FFMPEG", "ffmpeg")

# ---------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------
class ConnectRequest(BaseModel):
    name: str = Field(default="cam_1", description="Slot name (cam_1, cam_2, whatsapp_1, ...)")
    url: str = Field(..., description="Input URL (srt://..., file path, or other ffmpeg-supported input)")
    verify: bool = Field(default=True, description="Run a short ffmpeg probe to verify connectivity")
    preview: bool = Field(default=True, description="Enable preview JPEG capture")
    preview_fps: float = Field(default=1.0, ge=0.2, le=10.0)
    preview_width: int = Field(default=640, ge=160, le=1920)
    ffmpeg_path: str = Field(default=DEFAULT_FFMPEG)

class DisconnectRequest(BaseModel):
    name: str = Field(default="cam_1")
    reason: str = Field(default="operator_stop")

# ---------------------------------------------------------------------
# Slot runtime state
# ---------------------------------------------------------------------
@dataclass
class SlotState:
    name: str
    url: str = ""
    connected: bool = False
    preview_enabled: bool = False
    last_error: str = ""
    last_seen: float = 0.0
    started_at: float = 0.0
    ffmpeg_path: str = DEFAULT_FFMPEG
    preview_fps: float = 1.0
    preview_width: int = 640
    preview_path: str = ""

    # process handles
    probe_running: bool = False

_slots: Dict[str, SlotState] = {}
_slot_procs: Dict[str, subprocess.Popen] = {}
_lock = threading.Lock()

# ---------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------
def _which(exe: str) -> Optional[str]:
    try:
        return shutil.which(exe)
    except Exception:
        return None

def _preview_file_for(name: str) -> Path:
    return DEFAULT_PREVIEW_DIR / f"{name}.jpg"

def _stop_preview(name: str) -> None:
    with _lock:
        p = _slot_procs.pop(name, None)
    if not p:
        return
    try:
        p.terminate()
        p.wait(timeout=2.0)
    except Exception:
        try:
            p.kill()
        except Exception:
            pass

def _ffmpeg_probe(ffmpeg_path: str, url: str, timeout_s: float = 3.0) -> Optional[str]:
    """
    Best-effort connectivity probe:
    ffmpeg -hide_banner -loglevel error -i <url> -t 1 -f null -
    Returns error string if failed, else None.
    """
    ff = ffmpeg_path if os.path.exists(ffmpeg_path) else (_which(ffmpeg_path) or ffmpeg_path)
    cmd = [ff, "-hide_banner", "-loglevel", "error", "-i", url, "-t", "1", "-f", "null", "-"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
        if r.returncode != 0:
            msg = (r.stderr or r.stdout or "").strip()
            return msg or f"probe failed rc={r.returncode}"
        return None
    except subprocess.TimeoutExpired:
        return "probe timeout"
    except Exception as e:
        return f"probe exception: {e}"

def _start_preview_capture(name: str, ffmpeg_path: str, url: str, fps: float, width: int) -> None:
    """
    Start FFmpeg to overwrite a single JPEG repeatedly:
    ffmpeg -y -re -i <url> -vf fps=<fps>,scale=<width>:-2 -q:v 4 -update 1 <name>.jpg
    """
    _stop_preview(name)

    ff = ffmpeg_path if os.path.exists(ffmpeg_path) else (_which(ffmpeg_path) or ffmpeg_path)
    out_path = _preview_file_for(name)

    # ensure folder
    out_path.parent.mkdir(parents=True, exist_ok=True)

    vf = f"fps={fps},scale={width}:-2"
    cmd = [
        ff, "-y",
        "-hide_banner", "-loglevel", "warning",
        "-i", url,
        "-vf", vf,
        "-q:v", "4",
        "-update", "1",
        str(out_path),
    ]

    creation_flags = 0
    if os.name == "nt":
        creation_flags = subprocess.CREATE_NO_WINDOW  # type: ignore

    p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=creation_flags)
    with _lock:
        _slot_procs[name] = p

# ---------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------
app = FastAPI(title="REDTV live_ingest", version="4.16.2")

@app.get("/health")
def health() -> Dict[str, Any]:
    return {"ok": True, "slots": list(_slots.keys())}

@app.get("/status")
def status() -> Dict[str, Any]:
    """Return status for all slots.

    Includes computed preview frame freshness:
      - preview_mtime: epoch seconds of JPEG mtime (if exists)
      - frame_age_s:   seconds since last JPEG update
      - frame_ok:      frame_age_s <= 3 seconds (tunable client-side)
    """
    now = time.time()
    with _lock:
        slots_raw = {k: asdict(v) for k, v in _slots.items()}

    # Augment with preview freshness.
    slots: Dict[str, Any] = {}
    for name, d in slots_raw.items():
        try:
            p = _preview_file_for(name)
            if p.exists():
                m = p.stat().st_mtime
                age = max(0.0, now - m)
                d["preview_mtime"] = float(m)
                d["frame_age_s"] = float(age)
                d["frame_ok"] = bool(age <= 3.0)
            else:
                d["preview_mtime"] = 0.0
                d["frame_age_s"] = None
                d["frame_ok"] = False
        except Exception:
            d["preview_mtime"] = 0.0
            d["frame_age_s"] = None
            d["frame_ok"] = False
        slots[name] = d

    legacy = slots.get("cam_1", {})
    return {
        "ok": True,
        "slots": slots,
        # Backward-compatible single-slot fields (cam_1)
        "connected": bool(legacy.get("connected")),
        "url": legacy.get("url", ""),
        "preview_enabled": bool(legacy.get("preview_enabled")),
        "last_error": legacy.get("last_error", ""),
        "frame_age_s": legacy.get("frame_age_s"),
        "frame_ok": legacy.get("frame_ok"),
    }

@app.post("/connect")
def connect(req: ConnectRequest) -> Dict[str, Any]:
    # Normalize slot name
    name = req.name.strip() or "cam_1"

    # Resolve ffmpeg path (best-effort)
    ffmpeg_path = req.ffmpeg_path or DEFAULT_FFMPEG

    err = None
    if req.verify:
        err = _ffmpeg_probe(ffmpeg_path, req.url, timeout_s=3.0)

    st = SlotState(
        name=name,
        url=req.url,
        connected=(err is None),
        preview_enabled=False,
        last_error=(err or ""),
        last_seen=time.time(),
        started_at=time.time(),
        ffmpeg_path=ffmpeg_path,
        preview_fps=req.preview_fps,
        preview_width=req.preview_width,
        preview_path=str(_preview_file_for(name)),
    )

    with _lock:
        _slots[name] = st

    if st.connected and req.preview:
        try:
            _start_preview_capture(name, ffmpeg_path, req.url, req.preview_fps, req.preview_width)
            with _lock:
                _slots[name].preview_enabled = True
        except Exception as e:
            with _lock:
                _slots[name].last_error = f"preview start failed: {e}"
                _slots[name].preview_enabled = False

    return {
        "connected": st.connected,
        "last_error": st.last_error,
        "preview_enabled": st.preview_enabled,
        "name": name,
    }

@app.post("/disconnect")
def disconnect(req: DisconnectRequest) -> Dict[str, Any]:
    name = req.name.strip() or "cam_1"
    _stop_preview(name)
    with _lock:
        st = _slots.get(name)
        if st:
            st.connected = False
            st.preview_enabled = False
            st.last_error = f"disconnected: {req.reason}"
    return {"ok": True, "name": name}

# Backward compatible single-slot endpoints (cam_1)
@app.get("/preview.jpg")
def preview_default() -> Response:
    return preview_jpg("cam_1")

@app.get("/preview/{name}.jpg")
def preview_jpg(name: str) -> Response:
    p = _preview_file_for(name)
    if not p.exists():
        raise HTTPException(404, f"preview not found for slot {name}")
    return FileResponse(str(p), media_type="image/jpeg")

@app.get("/preview/{name}.mjpeg")
def preview_mjpeg(name: str):
    """
    Best-effort MJPEG stream built from repeatedly serving the JPEG file.
    This is intentionally simple and robust.
    """
    boundary = "frame"

    def gen():
        while True:
            p = _preview_file_for(name)
            if p.exists():
                data = p.read_bytes()
                yield (f"--{boundary}\r\n"
                       "Content-Type: image/jpeg\r\n"
                       f"Content-Length: {len(data)}\r\n\r\n").encode("utf-8") + data + b"\r\n"
            time.sleep(1.0)

    return StreamingResponse(gen(), media_type=f"multipart/x-mixed-replace; boundary={boundary}")
