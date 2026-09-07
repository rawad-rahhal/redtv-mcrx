"""
automation_ai.main
==================
REST API for Nashra playlist builder.
Accepts rundown text, returns playlist JSON, optionally saves to disk.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from pathlib import Path, PureWindowsPath

import yaml
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from automation_ai.builder import NashraPlaylistBuilder
from automation_ai.matcher import MediaLibraryEntry
from shared_contracts.models import MediaMetadata, NormalizationState

log = logging.getLogger("redtv.automation_ai.main")

config_path = Path(__file__).resolve().parents[1] / "config" / "redtv.yaml"
with open(config_path) as f:
    CFG = yaml.safe_load(f)


def _load_library(cfg: dict) -> list[MediaLibraryEntry]:
    """Scan media library dir for *.metadata.json files and build index."""
    lib_dir = cfg.get("paths", {}).get("media_library", "./media")
    entries: list[MediaLibraryEntry] = []
    if not os.path.isdir(lib_dir):
        log.warning("Media library dir not found: %s", lib_dir,
                    extra={"service": "automation_ai"})
        return entries
    import json
    for name in os.listdir(lib_dir):
        if not name.endswith(".metadata.json"):
            continue
        try:
            with open(os.path.join(lib_dir, name)) as f:
                data = json.load(f)
            meta = MediaMetadata(**data)
            if meta.norm_state != NormalizationState.DONE:
                continue
            entries.append(MediaLibraryEntry(
                media_id   = meta.media_id,
                title      = meta.title,
                media_path = meta.normalized_path,
                tags       = meta.tags,
            ))
        except Exception as exc:
            log.warning("Could not load metadata %s: %s", name, exc,
                        extra={"service": "automation_ai"})
    log.info("Library loaded: %d items", len(entries),
             extra={"service": "automation_ai"})
    return entries


app = FastAPI(title="Nashra AI Playlist Builder", version="2.0")
_library = _load_library(CFG)
_builder = NashraPlaylistBuilder(CFG, _library)


class BuildRequest(BaseModel):
    rundown_text: str
    save_to_disk: bool = False
    output_filename: str = "playlist_ai.json"


def _resolve_playlist_output_path(playlist_dir: str, output_filename: str) -> str:
    """Resolve a caller-supplied filename strictly inside playlist_dir.

    The API accepts a *filename*, not an arbitrary path. Reject absolute paths,
    drive-qualified Windows paths, traversal components and nested directories.
    """
    name = (output_filename or "").strip()
    win = PureWindowsPath(name)
    if (
        not name
        or name in {".", ".."}
        or "/" in name
        or "\\" in name
        or Path(name).is_absolute()
        or win.is_absolute()
        or bool(win.drive)
    ):
        raise ValueError("output_filename must be a simple filename inside playlist_dir")

    base = Path(playlist_dir).expanduser().resolve()
    target = (base / name).resolve()
    if target.parent != base:
        raise ValueError("output_filename escapes playlist_dir")
    return str(target)


@app.post("/build")
def build_playlist(req: BuildRequest):
    output_path = None
    if req.save_to_disk:
        pl_dir = CFG.get("paths", {}).get("playlist_dir", "./playlists")
        try:
            output_path = _resolve_playlist_output_path(pl_dir, req.output_filename)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    result = _builder.build(req.rundown_text, output_path=output_path)
    return {
        "playlist":       result.playlist.model_dump(),
        "correlation_id": result.correlation_id,
        "total_slots":    result.total_slots,
        "matched_slots":  result.matched_slots,
        "fallback_slots": result.fallback_slots,
        "warnings":       result.warnings,
    }


@app.get("/library/reload")
def reload_library():
    global _library, _builder
    _library = _load_library(CFG)
    _builder = NashraPlaylistBuilder(CFG, _library)
    return {"library_count": len(_library)}


@app.get("/health")
def health():
    return {"status": "ok", "library_items": len(_library)}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)
