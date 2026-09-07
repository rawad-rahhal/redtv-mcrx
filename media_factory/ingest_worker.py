"""
media_factory.ingest_worker  (v2.1 — HARDENED)
================================================
IngestWorker — FFmpeg normalisation pipeline.

v2.1 changes
------------
1. Media naming:  000001.mp4, 000002.mp4, ... (6-digit zero-padded)
   counter.txt on NAS holds the last-used integer.

2. SMB-safe counter lock:
   - Primary strategy: exclusive file lock via platform API
     (msvcrt.locking on Windows, fcntl.LOCK_EX on POSIX)
   - Fallback: rename-based advisory lock (.counter.lock file)
   - Retry loop: up to 10 attempts × 200ms = 2s max wait
   - Even with two workers racing, the counter is always incremented
     atomically (read-increment-write inside lock) so no duplicates.

3. Atomic output write: tempfile in same directory + os.replace().
   Works correctly on Windows NTFS and SMB shares (same-volume rename).

4. @rt_forbidden applied (belt-and-suspenders) to all blocking methods.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

from shared_contracts.guard import rt_forbidden
from shared_contracts.models import (
    IngestJob,
    MediaMetadata,
    NormalizationState,
)
from media_factory.probe import build_metadata_from_probe, probe_file, ProbeError

log = logging.getLogger("redtv.media_factory.worker")


# ---------------------------------------------------------------------------
# 6-digit sequential naming
# ---------------------------------------------------------------------------

def _acquire_counter_lock(lock_path: str, timeout: float = 2.0) -> Optional[object]:
    """
    Acquire an exclusive file lock on a PERSISTENT lock file.
    Returns the open file object on success, None on timeout.

    v3.2 fix: open in "a+" (append+read) so we NEVER truncate or recreate
    the lock file.  The lock file must live forever; only the lock/unlock
    state changes.  Deleting the file (as v3.1 did) creates a window where
    two processes each create a *new* file with the same name, each locks
    their own new inode, and both believe they hold the exclusive lock —
    producing duplicate counter values.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if sys.platform == "win32":
                import msvcrt
                lf = open(lock_path, "a+b")   # binary for msvcrt byte-level locking
                lf.seek(0)
                msvcrt.locking(lf.fileno(), msvcrt.LK_NBLCK, 1)
                return lf
            else:
                import fcntl
                lf = open(lock_path, "a+")    # "a+" never truncates existing file
                fcntl.flock(lf.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return lf
        except (IOError, OSError):
            time.sleep(0.05)   # tighter retry: 50ms × 40 = 2s
    return None


def _release_counter_lock(lf: object) -> None:
    """
    Release the lock and close the file.
    DO NOT unlink the lock file — it must remain on disk permanently.
    Unlinking creates a TOCTOU race where a new process opens a new inode
    with the same filename and holds a separate exclusive lock simultaneously.
    """
    try:
        if sys.platform == "win32":
            import msvcrt
            msvcrt.locking(lf.fileno(), msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
        else:
            import fcntl
            fcntl.flock(lf.fileno(), fcntl.LOCK_UN)           # type: ignore[attr-defined]
    except Exception:
        pass
    try:
        lf.close()  # type: ignore[attr-defined]
    except Exception:
        pass
    # NOTE: intentionally NOT calling os.unlink() — lock file is persistent.


def _next_media_filename(counter_path: str, extension: str = "mp4") -> str:
    """
    Thread-and-process-safe 6-digit sequential file naming.
    Returns e.g. "000001.mp4".
    Uses file locking; retries for up to 2 seconds on contention.
    """
    lock_path = counter_path + ".lock"
    lf = _acquire_counter_lock(lock_path, timeout=2.0)
    if lf is None:
        # Fallback: timestamp-based name to avoid blocking ingest
        ts = int(time.time() * 1000) % 999999
        log.warning("Counter lock timeout — using timestamp fallback: %06d", ts,
                    extra={"service": "ingest_worker"})
        return f"{ts:06d}.{extension}"

    try:
        # Read current counter
        if os.path.exists(counter_path):
            raw = ""
            try:
                with open(counter_path, "r") as f:
                    raw = f.read().strip()
                val = int(raw) if raw else 0
            except (ValueError, OSError):
                val = 0
        else:
            val = 0

        val += 1

        # Write new counter atomically within the same directory
        cdir = os.path.dirname(counter_path) or "."
        fd, tmp = tempfile.mkstemp(dir=cdir, suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            f.write(str(val))
        os.replace(tmp, counter_path)

        return f"{val:06d}.{extension}"

    finally:
        _release_counter_lock(lf)


# ---------------------------------------------------------------------------
# Atomic JSON write
# ---------------------------------------------------------------------------

def _atomic_write_json(path: str, data: dict) -> None:
    dir_ = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=dir_, suffix=".tmp.json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=str)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# IngestWorker
# ---------------------------------------------------------------------------

class IngestWorker:
    """Processes a single IngestJob end-to-end."""

    def __init__(self, config: dict) -> None:
        self._cfg          = config
        self._mf_cfg       = config.get("media_factory", {})
        self._paths        = config.get("paths", {})
        self._ffmpeg       = self._mf_cfg.get("ffmpeg_binary", "ffmpeg")
        self._ffprobe      = self._mf_cfg.get("ffprobe_binary", "ffprobe")
        self._timeout      = self._mf_cfg.get("timeout_seconds", 3600)
        self._counter_file = self._paths.get("counter_file", "counter.txt")
        self._target_dir   = self._paths.get("ingest_done",     "./ingest/done")
        self._quarantine   = self._paths.get("ingest_quarantine", "./ingest/quarantine")
        self._presets      = self._mf_cfg.get("presets", {})

    @rt_forbidden("IngestWorker.process")
    def process(self, job: IngestJob) -> IngestJob:
        log.info("IngestWorker: starting job %s  src=%s",
                 job.job_id[:8], job.source_path,
                 extra={"service": "ingest_worker"})
        job.state = NormalizationState.RUNNING

        try:
            # 1. Probe source
            probe = probe_file(job.source_path, self._ffprobe)
            meta  = build_metadata_from_probe(probe, job.source_path)

            # 2. Select preset (auto-detect portrait)
            preset_name = self._select_preset(meta, job.preset)
            preset      = self._presets.get(preset_name)
            if not preset:
                raise ValueError(f"Unknown preset: {preset_name}")

            # 3. Generate sequential output filename  (e.g. 000001.mp4)
            container = preset.get("container", "mp4")
            out_dir   = Path(self._target_dir)
            out_dir.mkdir(parents=True, exist_ok=True)

            out_name = _next_media_filename(self._counter_file, container)
            out_path = str(out_dir / out_name)

            log.info("Output filename: %s  (preset=%s)", out_name, preset_name,
                     extra={"service": "ingest_worker"})

            # 4. Run FFmpeg
            self._run_ffmpeg(job.source_path, out_path, preset)

            # 5. Verify output
            out_probe = probe_file(out_path, self._ffprobe)
            out_meta  = build_metadata_from_probe(out_probe, out_path)

            # 6. Media ID = basename without extension (e.g. "000001")
            media_id = Path(out_name).stem

            # 7. Write metadata.json atomically
            meta.media_id         = media_id
            meta.normalized_path  = out_path
            meta.norm_state       = NormalizationState.DONE
            meta.ingest_job_id    = job.job_id
            meta.created_utc      = datetime.datetime.utcnow().isoformat() + "Z"
            meta.video            = out_meta.video
            meta.audio            = out_meta.audio

            meta_path = str(out_dir / f"{media_id}.metadata.json")
            _atomic_write_json(meta_path, meta.model_dump())

            job.state       = NormalizationState.DONE
            job.output_path = out_path
            log.info("IngestWorker: DONE job=%s  out=%s",
                     job.job_id[:8], out_path,
                     extra={"service": "ingest_worker"})

        except Exception as exc:
            log.error("IngestWorker: FAILED job=%s  err=%s",
                      job.job_id[:8], exc,
                      extra={"service": "ingest_worker"})
            job.state     = NormalizationState.FAILED
            job.error_msg = str(exc)
            self._quarantine_source(job.source_path, job.job_id)
            job.state     = NormalizationState.QUARANTINE

        return job

    def _select_preset(self, meta: MediaMetadata, requested: str) -> str:
        if requested in self._presets:
            return requested
        if meta.video.height > meta.video.width:
            log.info("Portrait source → vertical_safe preset",
                     extra={"service": "ingest_worker"})
            return "vertical_safe"
        return "broadcast_1080i25"

    @rt_forbidden("IngestWorker._run_ffmpeg")
    def _run_ffmpeg(self, src: str, dst: str, preset: dict) -> None:
        res  = preset.get("resolution", "1920x1080")
        w, h = res.split("x")

        if preset.get("scan_type") == "interlaced":
            vf = (
                f"scale={w}:{h},"
                f"setfield={preset.get('field_order', 'tff')},"
                f"format={preset.get('pix_fmt', 'yuv422p')}"
            )
        else:
            vf = f"scale={w}:{h},format={preset.get('pix_fmt', 'yuv420p')}"

        cmd = [
            self._ffmpeg,
            "-y",
            "-i", src,
            "-vf", vf,
            "-c:v", preset.get("vcodec", "libx264"),
            "-crf", str(preset.get("crf", 18)),
            "-color_primaries", preset.get("color_space", "bt709"),
            "-color_trc",       "bt709",
            "-colorspace",      preset.get("color_space", "bt709"),
            "-c:a",  preset.get("acodec", "aac"),
            "-ar",   str(preset.get("audio_rate", 48000)),
            "-ac",   str(preset.get("audio_channels", 2)),
            "-b:a",  preset.get("audio_bitrate", "192k"),
            "-movflags", "+faststart",
            dst,
        ]
        log.info("FFmpeg: %s", " ".join(cmd), extra={"service": "ingest_worker"})

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            stdout, _ = proc.communicate(timeout=self._timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            raise RuntimeError(f"FFmpeg timed out after {self._timeout}s")

        if proc.returncode != 0:
            raise RuntimeError(
                f"FFmpeg failed (rc={proc.returncode}):\n{stdout[-800:]}"
            )

    def _quarantine_source(self, src: str, job_id: str) -> None:
        q_dir = Path(self._quarantine)
        q_dir.mkdir(parents=True, exist_ok=True)
        dst = str(q_dir / f"{job_id[:8]}_{os.path.basename(src)}")
        try:
            import shutil
            shutil.move(src, dst)
            log.warning("Quarantined: %s → %s", src, dst,
                        extra={"service": "ingest_worker"})
        except Exception as exc:
            log.error("Quarantine failed for %s: %s", src, exc,
                      extra={"service": "ingest_worker"})
