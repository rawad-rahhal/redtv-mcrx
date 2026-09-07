"""
playout_core.sources.live_source  (v3.3)
==========================================
LiveSourceAdapter — two-mode live source for PROGRAM bus.

MODE 1: file_live  (WhatsApp drop / operator file post)
  - Operator POSTs a UNC path to /live/file
  - Control thread validates path exists (I/O outside RT)
  - PrepWorker fills a DecoderSlot (same machinery as automation)
  - When pre-roll threshold reached → state=READY
  - RT thread reads from live slot

MODE 2: srt_live   (4G camera)
  - v3.3 stub: simulated frames + heartbeat status
  - Real SRT decode would be in live_ingest_service (separate process)
  - LiveSourceAdapter calls live_ingest_service REST API to get status
  - Frames generated synthetically at 25fps until real SRT decoder arrives

RT CONTRACT (inviolable):
  get_frame()      → DecoderSlot.pop_frame()    — GIL-atomic deque.popleft
  get_hold_frame() → DecoderSlot.get_hold_frame() — reference read
  is_ready()       → self._state == READY         — int comparison

All state mutations happen in background threads (control thread or workers).
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from enum import Enum
from pathlib import Path
from typing import Callable, Deque, Optional

from shared_contracts.guard import rt_forbidden, RealTimeGuard
from shared_contracts.rt_ring import RTRingBuffer, RTEventKind
from playout_core.decoder import (
    DecoderSlot, PrepWorker, VideoFrame, FRAME_BYTES, SlotState
)
from playout_core.sources.base import SourceAdapter, SourceState

log = logging.getLogger("redtv.live_source")


class LiveMode(str, Enum):
    IDLE      = "idle"
    FILE_LIVE = "file_live"
    SRT_LIVE  = "srt_live"


class LiveSourceAdapter(SourceAdapter):
    """
    Dual-mode live source.

    States:
        IDLE     → no input configured
        STAGING  → file validation / SRT connecting in progress
        READY    → pre-buffered, safe to switch to PROGRAM
        ACTIVE   → on air
        LOST     → EOF or SRT disconnect
        ERROR    → validation failed
    """

    def __init__(
        self,
        guard:            RealTimeGuard,
        pre_roll_frames:  int = 25,
        ring:             Optional[RTRingBuffer] = None,
        on_ready:         Optional[Callable[[], None]] = None,
        on_lost:          Optional[Callable[[str], None]] = None,
    ) -> None:
        self._guard           = guard
        self._pre_roll_frames = pre_roll_frames
        self._ring            = ring
        self._on_ready        = on_ready
        self._on_lost         = on_lost

        # State — written by control/worker threads, read by RT (GIL-atomic)
        self._state:  SourceState = SourceState.IDLE
        self._mode:   LiveMode    = LiveMode.IDLE
        self._input:  str         = ""       # file path or SRT URL
        self._last_error: str     = ""

        # Frame buffer (SPSC: worker pushes, RT pops)
        self._slot: DecoderSlot = DecoderSlot("live")
        self._worker: Optional[PrepWorker] = None
        self._srt_worker_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    # ── RT interface (ZERO I/O) ────────────────────────────────────────

    def get_frame(self) -> Optional[VideoFrame]:
        """RT thread only — GIL-atomic deque.popleft."""
        return self._slot.pop_frame()

    def get_hold_frame(self) -> Optional[VideoFrame]:
        """RT thread only — GIL-atomic reference read."""
        return self._slot.get_hold_frame()

    def is_ready(self) -> bool:
        """RT thread only — enum comparison, GIL-atomic."""
        return self._state in (SourceState.READY, SourceState.ACTIVE)

    # ── Properties (control thread) ───────────────────────────────────

    @property
    def state(self) -> SourceState:
        return self._state

    @property
    def mode(self) -> LiveMode:
        return self._mode

    @property
    def input_str(self) -> str:
        return self._input

    @property
    def last_error(self) -> str:
        return self._last_error

    # ── Control interface (@rt_forbidden) ─────────────────────────────

    @rt_forbidden("LiveSourceAdapter.start")
    def start(self) -> None:
        self._stop_event.clear()
        log.info("LiveSourceAdapter started", extra={"service": "live_source"})

    @rt_forbidden("LiveSourceAdapter.stop")
    def stop(self) -> None:
        self._stop_event.set()
        if self._worker:
            self._worker.stop()
        self._state = SourceState.IDLE
        self._mode  = LiveMode.IDLE
        log.info("LiveSourceAdapter stopped", extra={"service": "live_source"})

    @rt_forbidden("LiveSourceAdapter.stage_file")
    def stage_file(self, path: str) -> None:
        """
        Validate and stage a file for live playback.
        Called from control thread (POST /live/file handler).

        Steps:
          1. Validate path exists (I/O — safe here, control thread)
          2. Stop any existing worker
          3. Reset slot and start PrepWorker
          4. State → STAGING; worker calls _on_file_ready when buffered
        """
        norm = path.strip()
        if not norm:
            self._last_error = "Empty path"
            self._state = SourceState.ERROR
            return

        if not Path(norm).exists():
            self._last_error = f"File not found: {norm}"
            self._state = SourceState.ERROR
            log.error("Live file not found: %s", norm, extra={"service": "live_source"})
            return

        # Stop existing worker if any
        if self._worker:
            self._worker.stop()

        from shared_contracts.models import PlaylistItem, SlotType
        item = PlaylistItem(
            slot_type  = SlotType.PROGRAM,
            media_path = norm,
            title      = Path(norm).stem,
        )

        self._input = norm
        self._mode  = LiveMode.FILE_LIVE
        self._state = SourceState.STAGING
        self._last_error = ""
        self._slot.reset()

        self._worker = PrepWorker(
            slot            = self._slot,
            guard           = self._guard,
            pre_roll_frames = self._pre_roll_frames,
            on_ready        = self._on_file_ready,
            on_error        = self._on_file_error,
        )
        self._worker.load(item)
        log.info("Live file staging: %s", norm, extra={"service": "live_source"})

    @rt_forbidden("LiveSourceAdapter.connect_srt")
    def connect_srt(self, name: str, url: str) -> None:
        """
        Connect to an SRT source (v3.3: stub with synthetic frames).
        A real implementation would connect to live_ingest_service.
        """
        # Stop any existing live source
        self.stop()
        self._stop_event.clear()

        self._input = url
        self._mode  = LiveMode.SRT_LIVE
        self._state = SourceState.STAGING
        self._last_error = ""

        log.info("SRT connect: name=%s url=%s", name, url,
                 extra={"service": "live_source"})

        # Start synthetic SRT worker (v3.3 stub)
        self._srt_worker_thread = threading.Thread(
            target = self._srt_synthetic_worker,
            args   = (name, url),
            daemon = True,
            name   = f"live-srt-{name[:8]}",
        )
        self._srt_worker_thread.start()

    @rt_forbidden("LiveSourceAdapter.stop_live")
    def stop_live(self) -> None:
        self.stop()
        self._input = ""
        self._last_error = ""
        log.info("Live source stopped by operator", extra={"service": "live_source"})

    # ── Internal callbacks (worker threads) ───────────────────────────

    def _on_file_ready(self) -> None:
        """Called by PrepWorker when pre-roll threshold reached."""
        self._state = SourceState.READY
        log.info("Live file READY: %s", self._input,
                 extra={"service": "live_source"})
        if self._ring:
            self._ring.push(kind=RTEventKind.LIVE_SOURCE_READY, severity=1)
        if self._on_ready:
            try:
                self._on_ready()
            except Exception:
                pass

    def _on_file_error(self, msg: str) -> None:
        self._last_error = msg
        self._state = SourceState.ERROR
        log.error("Live file error: %s", msg, extra={"service": "live_source"})
        if self._on_lost:
            try:
                self._on_lost(msg)
            except Exception:
                pass

    def _srt_synthetic_worker(self, name: str, url: str) -> None:
        """
        v3.3 stub: generates synthetic black frames at 25fps to simulate
        a connected SRT camera.  A real implementation would call
        live_ingest_service and decode frames via ffmpeg/libSRT.
        """
        log.info("SRT synthetic worker started: %s", url,
                 extra={"service": "live_source"})
        fps         = 25
        frame_bytes = bytes(FRAME_BYTES)
        frame_num   = 0

        # Simulate 500ms connection delay
        if self._stop_event.wait(0.5):
            return

        # Mark ready
        self._state = SourceState.READY
        if self._ring:
            self._ring.push(kind=RTEventKind.LIVE_SOURCE_READY, severity=1)
        if self._on_ready:
            try:
                self._on_ready()
            except Exception:
                pass

        # Generate frames continuously until stopped
        interval = 1.0 / fps
        while not self._stop_event.is_set():
            t0 = time.monotonic()
            if self._slot.buffer_depth() < 200:
                frame = VideoFrame(
                    data=frame_bytes, pts=frame_num / fps,
                    frame_number=frame_num,
                )
                self._slot.push_frame(frame)
                frame_num += 1
            elapsed = time.monotonic() - t0
            sleep_t = max(0.0, interval - elapsed)
            if self._stop_event.wait(sleep_t):
                break

        # Signal lost
        self._state = SourceState.LOST
        if self._ring:
            self._ring.push(kind=RTEventKind.LIVE_SOURCE_LOST, severity=2)
        if self._on_lost:
            try:
                self._on_lost("SRT disconnected")
            except Exception:
                pass
        log.info("SRT synthetic worker exited", extra={"service": "live_source"})

    # ── Status ────────────────────────────────────────────────────────

    def get_status(self) -> dict:
        return {
            "source":       "live",
            "mode":         self._mode.value,
            "state":        self._state.value,
            "ready":        self.is_ready(),
            "input":        self._input,
            "last_error":   self._last_error,
            "buffer_depth": self._slot.buffer_depth(),
        }
