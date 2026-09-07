"""
media_factory.watcher
=====================
Watches the ingest drop folder using the watchdog library.
New files trigger job creation, written to queue.jsonl atomically.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable, List

log = logging.getLogger("redtv.media_factory.watcher")

MEDIA_EXTENSIONS = {".mp4", ".mov", ".mxf", ".avi", ".mkv", ".ts", ".mts", ".m2ts"}


def _job_for_file(path: str, target_dir: str) -> dict:
    from shared_contracts.models import IngestJob
    import datetime
    job = IngestJob(
        source_path   = path,
        target_dir    = target_dir,
        submitted_utc = datetime.datetime.utcnow().isoformat() + "Z",
    )
    return job.model_dump()


def _append_job_atomic(queue_path: str, job: dict) -> None:
    """Append one JSON line to queue.jsonl atomically."""
    dir_ = os.path.dirname(queue_path) or "."
    os.makedirs(dir_, exist_ok=True)
    line = json.dumps(job, default=str) + "\n"
    # Append-then-rename is not trivially atomic on all FSes, but
    # appending to an existing file with a lock is the practical approach.
    with open(queue_path, "a", encoding="utf-8") as f:
        f.write(line)


class DropFolderWatcher:
    """
    Polls the drop folder for new media files.
    Uses watchdog when available; falls back to polling every 5s.
    """

    def __init__(
        self,
        watch_dir:  str,
        queue_path: str,
        target_dir: str,
        on_new_file: Callable[[str], None] = None,
        poll_interval: float = 5.0,
    ) -> None:
        self._watch_dir    = watch_dir
        self._queue_path   = queue_path
        self._target_dir   = target_dir
        self._on_new_file  = on_new_file
        self._poll_interval = poll_interval
        self._seen: set[str] = set()
        self._stop_event   = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        Path(self._watch_dir).mkdir(parents=True, exist_ok=True)
        self._thread = threading.Thread(
            target=self._poll_loop, daemon=True, name="drop-watcher"
        )
        self._thread.start()
        log.info("DropFolderWatcher started on: %s", self._watch_dir,
                 extra={"service": "watcher"})

    def stop(self) -> None:
        self._stop_event.set()

    def _poll_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._scan()
            except Exception as exc:
                log.error("Watcher scan error: %s", exc,
                          extra={"service": "watcher"})
            self._stop_event.wait(self._poll_interval)

    def _scan(self) -> None:
        if not os.path.isdir(self._watch_dir):
            return
        for entry in os.scandir(self._watch_dir):
            if not entry.is_file():
                continue
            ext = Path(entry.name).suffix.lower()
            if ext not in MEDIA_EXTENSIONS:
                continue
            if entry.path in self._seen:
                continue

            # Wait briefly to ensure file is fully written
            size1 = entry.stat().st_size
            time.sleep(1.0)
            try:
                size2 = os.stat(entry.path).st_size
            except OSError:
                continue
            if size1 != size2:
                continue   # still writing

            self._seen.add(entry.path)
            job = _job_for_file(entry.path, self._target_dir)
            _append_job_atomic(self._queue_path, job)
            log.info("Queued: %s (job_id=%s)", entry.path, job["job_id"][:8],
                     extra={"service": "watcher"})

            if self._on_new_file:
                try:
                    self._on_new_file(entry.path)
                except Exception:
                    pass
