"""
shared_contracts.preflight
==========================
Broadcast guard preflight for playlists BEFORE playout_core loads them.

This module is intentionally NOT used inside the RT loop.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, asdict
from typing import List, Optional, Dict, Any

from shared_contracts.models import Playlist, SlotType
from shared_contracts.path_utils import normalize_unc

@dataclass
class GuardReport:
    ok: bool
    checked_at: str
    playlist_id: str
    correlation_id: Optional[str]
    errors: List[str]
    warnings: List[str]
    missing_media: List[str]
    item_count: int
    # extra fields for ops
    root_hint: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

def _ts() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")

def preflight_playlist(playlist: Playlist, cfg: dict) -> GuardReport:
    errors: List[str] = []
    warnings: List[str] = []
    missing: List[str] = []

    # Root hint helps operators understand mapping between forward-slash and backslash UNC forms
    root_hint = str((cfg.get("paths") or {}).get("root", ""))

    if not playlist.items:
        errors.append("Playlist has 0 items.")

    for idx, it in enumerate(playlist.items):
        p = normalize_unc((it.media_path or "").strip())
        if it.slot_type == SlotType.PROGRAM and not p:
            errors.append(f"Item[{idx}] '{it.title}': media_path required but empty.")
            continue
        if p and not os.path.exists(p):
            missing.append(p)
            errors.append(f"Item[{idx}] '{it.title}': media not found: {p}")

        if it.duration_seconds is not None and it.duration_seconds <= 0:
            warnings.append(f"Item[{idx}] '{it.title}': non-positive duration_seconds={it.duration_seconds}")

    ok = len(errors) == 0

    return GuardReport(
        ok=ok,
        checked_at=_ts(),
        playlist_id=playlist.playlist_id,
        correlation_id=playlist.correlation_id,
        errors=errors,
        warnings=warnings + (playlist.warnings or []),
        missing_media=missing,
        item_count=len(playlist.items),
        root_hint=root_hint,
    )
