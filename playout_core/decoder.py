"""
playout_core.decoder  (v3 — PRODUCTION HARDENED)
===================================================
DoubleBufferDecoder + PrepWorker + CleanupWorker

v3 bug fixes vs v2.1
---------------------
FIX 1: load_emergency() no longer calls wait_for_ready(timeout=10).
        That call blocked the entire control loop for up to 10 seconds,
        during which no ring drain, no FSM transitions, no /status responses.
        New behaviour: start the worker non-blocking; the control loop already
        polls buffer_depth every 5ms and will begin outputting frames once
        the slot is ready.

FIX 2: CleanupWorker tracks orphaned PrepWorker threads and joins them in a
        daemon background thread.  Without this, rapid load_next() calls
        (drop-in spam) accumulated growing stacks of zombie threads.
        active_worker_count is exposed so /status can sanity-check leaks.

FIX 3: load_next() now stops and records the old worker in the cleanup queue
        before replacing it, so no thread reference is silently dropped.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Deque, List, Optional

from shared_contracts.guard import rt_forbidden, RealTimeGuard
from shared_contracts.models import PlaylistItem
from shared_contracts.rt_ring import RTRingBuffer, RTEventKind

FRAME_BYTES = 1920 * 1080 * 2   # YUV422p planar


@dataclass
class VideoFrame:
    data:         bytes
    pts:          float
    is_last:      bool = False
    frame_number: int  = 0


class SlotState(str, Enum):
    IDLE    = "idle"
    LOADING = "loading"
    READY   = "ready"
    PLAYING = "playing"
    DONE    = "done"
    ERROR   = "error"


# ---------------------------------------------------------------------------
# CleanupWorker — joins orphaned PrepWorker threads in background
# ---------------------------------------------------------------------------

class _CleanupWorker:
    """
    Daemon background thread that joins stopped PrepWorker threads.
    Called when a PrepWorker is evicted from the NEXT slot.
    """

    def __init__(self) -> None:
        self._queue: Deque["PrepWorker"] = deque(maxlen=32)
        self._lock   = threading.Lock()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="decoder-cleanup"
        )
        self._thread.start()

    def enqueue(self, worker: "PrepWorker") -> None:
        with self._lock:
            self._queue.append(worker)

    @property
    def queue_size(self) -> int:
        return len(self._queue)

    def _run(self) -> None:
        while True:
            time.sleep(0.5)
            with self._lock:
                pending = list(self._queue)
                self._queue.clear()
            for w in pending:
                try:
                    w.join(timeout=2.0)
                except Exception:
                    pass


_CLEANUP = _CleanupWorker()   # singleton, started at import time


# ---------------------------------------------------------------------------
# DecoderSlot
# ---------------------------------------------------------------------------

class DecoderSlot:
    """
    Frame ring — GIL-atomic deque operations.
    push_frame: PrepWorker only.  pop_frame/get_hold_frame: RT thread only.
    """
    __slots__ = (
        "slot_id", "item", "state",
        "_buffer", "_ready_event",
        "_frames_decoded", "_frames_popped", "_hold_frame",
    )

    def __init__(self, slot_id: str = "") -> None:
        self.slot_id:          str                    = slot_id
        self.item:             Optional[PlaylistItem] = None
        self.state:            SlotState              = SlotState.IDLE
        self._buffer:          Deque[VideoFrame]      = deque(maxlen=300)
        self._ready_event:     threading.Event        = threading.Event()
        self._frames_decoded:  int                    = 0
        self._frames_popped:   int                    = 0
        self._hold_frame:      Optional[VideoFrame]   = None

    def push_frame(self, frame: VideoFrame) -> None:
        self._buffer.append(frame)
        self._frames_decoded += 1
        if frame.data:
            self._hold_frame = frame

    def pop_frame(self) -> Optional[VideoFrame]:
        """RT thread only — GIL-atomic deque.popleft()."""
        try:
            frame = self._buffer.popleft()
            self._frames_popped += 1
            return frame
        except IndexError:
            return None

    def get_hold_frame(self) -> Optional[VideoFrame]:
        """RT thread only — GIL-atomic reference read."""
        return self._hold_frame

    def buffer_depth(self) -> int:
        return len(self._buffer)

    def wait_for_ready(self, timeout: float = 10.0) -> bool:
        return self._ready_event.wait(timeout)

    def mark_ready(self) -> None:
        self._ready_event.set()
        self.state = SlotState.READY

    def reset(self) -> None:
        self._buffer.clear()
        self._ready_event.clear()
        self._frames_decoded = 0
        self._frames_popped  = 0
        self._hold_frame     = None
        self.state           = SlotState.IDLE
        self.item            = None


# ---------------------------------------------------------------------------
# PrepWorker
# ---------------------------------------------------------------------------

class PrepWorker:
    """Background thread — fills a DecoderSlot from a media file."""

    def __init__(
        self,
        slot:            DecoderSlot,
        guard:           RealTimeGuard,
        pre_roll_frames: int = 25,
        on_ready:        Optional[Callable[[], None]] = None,
        on_error:        Optional[Callable[[str], None]] = None,
    ) -> None:
        self._slot            = slot
        self._guard           = guard
        self._pre_roll_frames = pre_roll_frames
        self._on_ready        = on_ready
        self._on_error        = on_error
        self._thread:         Optional[threading.Thread] = None
        self._stop_event      = threading.Event()

    @rt_forbidden("PrepWorker.load")
    def load(self, item: PlaylistItem) -> None:
        self._stop_event.clear()
        self._slot.reset()
        self._slot.item  = item
        self._slot.state = SlotState.LOADING
        self._thread = threading.Thread(
            target=self._run, args=(item,),
            daemon=True, name=f"prep-{item.slot_id[:8]}",
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()

    def join(self, timeout: float = 2.0) -> None:
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)

    @property
    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _run(self, item: PlaylistItem) -> None:
        import logging
        log = logging.getLogger("redtv.prep_worker")
        log.info("PrepWorker: loading %s (%s)", item.slot_id[:8], item.title)
        try:
            self._decode(item)
        except Exception as exc:
            log.error("PrepWorker error on %s: %s", item.slot_id[:8], exc)
            self._slot.state = SlotState.ERROR
            if self._on_error:
                try:
                    self._on_error(str(exc))
                except Exception:
                    pass

    @rt_forbidden("PrepWorker._decode")
    def _decode(self, item: PlaylistItem) -> None:
        try:
            import av  # type: ignore
            self._decode_pyav(item.media_path or "", item.trim_in or 0.0,
                              item.trim_out, item)
        except ImportError:
            self._decode_synthetic(item)

    @rt_forbidden("PrepWorker._decode_pyav")
    def _decode_pyav(self, path: str, trim_in: float,
                     trim_out: Optional[float], item: PlaylistItem) -> None:
        import av  # type: ignore
        container = av.open(path)
        stream    = container.streams.video[0]
        stream.thread_type = "AUTO"
        if trim_in > 0:
            container.seek(int(trim_in / float(stream.time_base)))
        frame_num = 0
        for packet in container.demux(stream):
            if self._stop_event.is_set():
                break
            for frame in packet.decode():
                pts = float(frame.pts * stream.time_base) if frame.pts else 0.0
                if trim_out and pts > trim_out:
                    break
                raw = bytes(frame.to_ndarray(format="yuv422p").tobytes())
                vf  = VideoFrame(data=raw, pts=pts, frame_number=frame_num)
                self._slot.push_frame(vf)
                frame_num += 1
                if frame_num == self._pre_roll_frames:
                    self._slot.mark_ready()
                    if self._on_ready:
                        self._on_ready()
                while self._slot.buffer_depth() > 250 and not self._stop_event.is_set():
                    time.sleep(0.01)
        self._slot.push_frame(VideoFrame(data=b"", pts=0.0, is_last=True))
        self._slot.state = SlotState.DONE
        container.close()

    @rt_forbidden("PrepWorker._decode_synthetic")
    def _decode_synthetic(self, item: PlaylistItem) -> None:
        fps         = 25
        duration    = item.duration_seconds or 10.0
        total       = int(duration * fps)
        black_frame = bytes(FRAME_BYTES)
        for i in range(total):
            if self._stop_event.is_set():
                break
            self._slot.push_frame(
                VideoFrame(data=black_frame, pts=i / fps, frame_number=i)
            )
            if i == self._pre_roll_frames - 1:
                self._slot.mark_ready()
                if self._on_ready:
                    self._on_ready()
            while self._slot.buffer_depth() > 250 and not self._stop_event.is_set():
                time.sleep(0.01)
        self._slot.push_frame(VideoFrame(data=b"", pts=0.0, is_last=True))
        self._slot.state = SlotState.DONE


# ---------------------------------------------------------------------------
# DoubleBufferDecoder
# ---------------------------------------------------------------------------

class DoubleBufferDecoder:
    """
    Manages CURRENT + NEXT decoder slots.

    RT interface (zero-I/O):
        get_current_frame()     pop frame from CURRENT
        get_hold_frame()        last known good frame
        is_swap_ready()         lockless state check
        current_buffer_depth()  CURRENT queue depth

    Control interface (@rt_forbidden):
        load_next(item)         load item into NEXT slot
        swap()                  NEXT → CURRENT
        load_emergency(item)    load into CURRENT, non-blocking (FIX 1)
    """

    def __init__(
        self,
        guard:           RealTimeGuard,
        pre_roll_frames: int = 25,
        on_swap_ready:   Optional[Callable[[], None]] = None,
        on_error:        Optional[Callable[[str], None]] = None,
        ring:            Optional[RTRingBuffer] = None,
    ) -> None:
        self._guard           = guard
        self._pre_roll_frames = pre_roll_frames
        self._on_swap_ready   = on_swap_ready
        self._on_error        = on_error
        self._ring            = ring

        self._current: DecoderSlot = DecoderSlot("current")
        self._next:    DecoderSlot = DecoderSlot("next")

        self._worker_current: Optional[PrepWorker] = None
        self._worker_next:    Optional[PrepWorker] = None

        self._swap_lock        = threading.Lock()
        self._frame_drop_count = 0   # RT sole writer

    def set_ring(self, ring: RTRingBuffer) -> None:
        self._ring = ring

    # ── RT interface ─────────────────────────────────────────────────

    def get_current_frame(self) -> Optional[VideoFrame]:
        frame = self._current.pop_frame()
        if frame is None:
            self._frame_drop_count += 1
            if self._ring is not None:
                self._ring.push(kind=RTEventKind.FRAME_DROP, severity=2,
                                detail=self._frame_drop_count)
        return frame

    def get_hold_frame(self) -> Optional[VideoFrame]:
        return self._current.get_hold_frame()

    def is_swap_ready(self) -> bool:
        """Lockless — GIL-atomic enum reference read."""
        return self._next.state in (SlotState.READY, SlotState.PLAYING, SlotState.DONE)

    def current_buffer_depth(self) -> int:
        return self._current.buffer_depth()

    def current_item(self) -> Optional[PlaylistItem]:
        return self._current.item
    def next_item(self) -> Optional[PlaylistItem]:
        """Return the pre-rolled NEXT slot item (may be None)."""
        return self._next.item


    @property
    def frame_drop_count(self) -> int:
        return self._frame_drop_count

    @property
    def active_worker_count(self) -> int:
        """Number of PrepWorker threads currently alive (for /status)."""
        count = 0
        if self._worker_current and self._worker_current.is_alive:
            count += 1
        if self._worker_next and self._worker_next.is_alive:
            count += 1
        return count

    @property
    def cleanup_queue_size(self) -> int:
        return _CLEANUP.queue_size

    # ── Control interface (@rt_forbidden) ─────────────────────────────

    @rt_forbidden("DoubleBufferDecoder.load_next")
    def load_next(self, item: PlaylistItem) -> None:
        # Stop old next worker, send it to cleanup queue (FIX 2)
        if self._worker_next:
            self._worker_next.stop()
            _CLEANUP.enqueue(self._worker_next)
            self._worker_next = None

        self._next.reset()
        self._worker_next = PrepWorker(
            slot            = self._next,
            guard           = self._guard,
            pre_roll_frames = self._pre_roll_frames,
            on_ready        = self._on_swap_ready,
            on_error        = self._on_error,
        )
        self._worker_next.load(item)

    @rt_forbidden("DoubleBufferDecoder.swap")
    def swap(self) -> None:
        with self._swap_lock:
            # Retire current worker to cleanup
            if self._worker_current:
                self._worker_current.stop()
                _CLEANUP.enqueue(self._worker_current)

            old_current           = self._current
            self._current         = self._next
            self._next            = old_current
            self._worker_current  = self._worker_next
            self._worker_next     = None
            self._current.state   = SlotState.PLAYING
            self._next.reset()

    @rt_forbidden("DoubleBufferDecoder.load_emergency")
    def load_emergency(self, item: PlaylistItem) -> None:
        """
        Non-blocking emergency load (FIX 1).
        Starts PrepWorker and returns immediately.
        Control loop polls current_buffer_depth(); frames appear within
        pre_roll_frames / 25fps ≈ 1 second.
        """
        # Stop anything currently in CURRENT
        if self._worker_current:
            self._worker_current.stop()
            _CLEANUP.enqueue(self._worker_current)
            self._worker_current = None

        self._current.reset()
        worker = PrepWorker(
            slot            = self._current,
            guard           = self._guard,
            pre_roll_frames = self._pre_roll_frames,
            on_ready        = None,
            on_error        = self._on_error,
        )
        worker.load(item)
        self._worker_current = worker
        # Intentionally NOT calling wait_for_ready() — non-blocking