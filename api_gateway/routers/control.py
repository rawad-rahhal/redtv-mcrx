"""
api_gateway.routers.control  (v2.1 — HARDENED)
================================================
REST control surface for PlayoutEngine.

v2.1 additions
--------------
- All /api/status fields complete per spec:
    state, current, next, violation_count, drift_ms, hold_count, emergency_count
- playlist correlation_id included in load response and forwarded to WS
- /api/preview/frame still available
"""

from __future__ import annotations

import json
import logging
from pathlib import Path, PureWindowsPath
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel

from shared_contracts.models import Playlist, PlaylistItem, SlotType
from shared_contracts.preflight import preflight_playlist

log = logging.getLogger("redtv.api_gateway.control")
router = APIRouter(prefix="/api")


def _engine(request: Request):
    engine = request.app.state.engine
    if engine is None:
        raise HTTPException(status_code=503, detail="Engine not initialised")
    return engine


class LoadPlaylistRequest(BaseModel):
    playlist_json: dict


class DropInRequest(BaseModel):
    media_path:       str
    title:            str
    duration_seconds: Optional[float] = None
    slot_type:        str = "program"


@router.post("/playlist/load")
def load_playlist(req: LoadPlaylistRequest, request: Request):
    try:
        playlist = Playlist(**req.playlist_json)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    report = preflight_playlist(playlist, request.app.state.config)
    request.app.state.guard_report = report.to_dict()
    if not report.ok:
        raise HTTPException(status_code=422, detail={"guard_report": report.to_dict()})

    _engine(request).load_playlist(playlist)
    return {
        "status":         "accepted",
        "playlist_id":    playlist.playlist_id,
        "correlation_id": playlist.correlation_id,
        "item_count":     len(playlist.items),
        "warnings":       playlist.warnings,
        "guard_ok":       True,
    }
def _resolve_playlist_input_path(config: dict, identifier: str) -> Path:
    """Resolve a playlist *filename* inside the configured playlist directory.

    The API intentionally does not accept caller-supplied filesystem paths.
    Absolute paths, drive-qualified paths, nested paths, traversal and symlink
    escapes are rejected before any file is opened.
    """
    name = (identifier or "").strip()
    if not name or name in {".", ".."}:
        raise ValueError("playlist filename is required")

    # Reject both POSIX and Windows path syntax even when running on the other OS.
    win = PureWindowsPath(name)
    if (
        "/" in name
        or "\\" in name
        or Path(name).is_absolute()
        or win.is_absolute()
        or bool(win.drive)
        or name != Path(name).name
    ):
        raise ValueError("playlist must be a simple filename, not a filesystem path")

    playlist_dir = str(config.get("paths", {}).get("playlist_dir", "")).strip()
    if not playlist_dir:
        raise RuntimeError("paths.playlist_dir is not configured")

    base = Path(playlist_dir).expanduser().resolve(strict=False)
    candidate = (base / name).resolve(strict=False)
    if candidate.parent != base:
        raise ValueError("playlist path escapes configured playlist directory")
    return candidate


@router.post("/playlist/load_file")
def load_playlist_file(request: Request, path: str):
    try:
        playlist_path = _resolve_playlist_input_path(request.app.state.config, path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None

    if not playlist_path.is_file():
        raise HTTPException(status_code=404, detail=f"Playlist file not found: {path}")

    try:
        with playlist_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        playlist = Playlist(**data)
    except (OSError, UnicodeError, json.JSONDecodeError):
        log.exception("Failed to read playlist file %s", playlist_path)
        raise HTTPException(status_code=422, detail="Playlist file is unreadable or invalid JSON") from None
    except Exception:
        # Do not echo Pydantic/input values into the HTTP response.
        log.exception("Playlist schema validation failed for %s", playlist_path)
        raise HTTPException(status_code=422, detail="Playlist file failed schema validation") from None

    report = preflight_playlist(playlist, request.app.state.config)
    request.app.state.guard_report = report.to_dict()
    if not report.ok:
        raise HTTPException(status_code=422, detail={"guard_report": report.to_dict()})

    _engine(request).load_playlist(playlist)
    return {
        "status":         "accepted",
        "playlist_id":    playlist.playlist_id,
        "correlation_id": playlist.correlation_id,
        "item_count":     len(playlist.items),
        "warnings":       playlist.warnings,
        "guard_ok":       True,
    }
@router.post("/dropin")
def drop_in(req: DropInRequest, request: Request):
    try:
        slot_type = SlotType(req.slot_type.lower())
    except ValueError:
        slot_type = SlotType.PROGRAM
    item = PlaylistItem(
        slot_type        = slot_type,
        media_path       = req.media_path,
        title            = req.title,
        duration_seconds = req.duration_seconds,
    )
    _engine(request).drop_in_next(item)
    return {"status": "accepted", "slot_id": item.slot_id, "title": item.title}


@router.post("/emergency")
def emergency(request: Request):
    _engine(request).activate_emergency()
    return {"status": "emergency_activated"}


@router.get("/status")
def get_status(request: Request):
    """
    Full status snapshot (v4.0).
    Engine fields + program_source + live + graphics + live_slots.
    """
    status = _engine(request).get_status()

    # v4: attach graphics state
    try:
        from api_gateway.routers.graphics import get_graphics_state
        status["graphics"] = get_graphics_state()
    except Exception:
        status["graphics"] = None

    # v4: attach live slots summary
    try:
        from api_gateway.routers.live import get_slots_status
        status["live_slots"] = get_slots_status()
    except Exception:
        status["live_slots"] = []

    return status


@router.get("/preview/frame")
def preview_frame(request: Request):
    adapter = getattr(request.app.state, "preview_adapter", None)
    if adapter is None:
        raise HTTPException(status_code=503, detail="Preview not available")
    jpeg = adapter.get_jpeg(timeout=0.5)
    if jpeg is None:
        raise HTTPException(status_code=204, detail="No frame available")
    return Response(content=jpeg, media_type="image/jpeg")
