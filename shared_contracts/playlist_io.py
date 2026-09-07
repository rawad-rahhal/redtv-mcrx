"""
shared_contracts.playlist_io
============================
Loading and validating playlist.json with UTF-8 and Pydantic contracts.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Tuple

from shared_contracts.models import Playlist
from shared_contracts.path_utils import normalize_unc

def load_playlist_file(path: str) -> Playlist:
    p = Path(path)
    data = json.loads(p.read_text(encoding="utf-8"))
    # normalize media paths to UNC style early (non-lossy for Arabic metadata)
    for item in data.get("items", []) or []:
        if isinstance(item, dict) and "media_path" in item and isinstance(item["media_path"], str):
            item["media_path"] = normalize_unc(item["media_path"])
    return Playlist(**data)

def validate_playlist_dict(payload: dict) -> Tuple[bool, str]:
    try:
        Playlist(**payload)
        return True, ""
    except Exception as e:
        return False, str(e)
