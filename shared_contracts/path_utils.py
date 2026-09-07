"""
shared_contracts.path_utils
===========================
Windows-first path helpers for UNC/NAS/UNC environments.

- normalize_unc: accepts //NAS-SERVER/CHANNEL and \\NAS-SERVER\\CHANNEL forms.
- atomic_write_bytes / atomic_write_text: write temp then os.replace (atomic on same volume/share).
- safe_mkdir: mkdir parents.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Union

PathLike = Union[str, os.PathLike]

def normalize_unc(p: str) -> str:
    if not p:
        return p
    # Convert forward UNC //SERVER/Share -> \\SERVER\\Share
    if p.startswith("//"):
        parts = p[2:].split("/")
        return "\\\\" + "\\\\".join([x for x in parts if x])
    return p

def safe_mkdir(p: PathLike) -> None:
    Path(p).mkdir(parents=True, exist_ok=True)

def atomic_write_bytes(path: PathLike, data: bytes) -> None:
    path = Path(path)
    safe_mkdir(path.parent)
    tmp = path.with_suffix(path.suffix + ".tmp")
    # Use NamedTemporaryFile in same directory to keep rename atomic on SMB.
    fd = None
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        try:
            if tmp.exists():
                tmp.unlink(missing_ok=True)
        except Exception:
            pass

def atomic_write_text(path: PathLike, text: str, encoding: str="utf-8") -> None:
    atomic_write_bytes(path, text.encode(encoding))
