"""
playout_core.output_adapters
============================
OutputAdapter ABC + concrete implementations.

Architecture
------------
The RT loop feeds frames through an OutputAdapter.  The adapter is the
only component allowed to "consume" frames — everything else is upstream.

Concrete adapters:
  NullAdapter          – discards frames (unit tests, benchmarks)
  PreviewWindowAdapter – pushes to an in-process queue for HTTP MJPEG preview
  DeckLinkAdapter      – STUB for future BMD DeckLink SDK integration

Adapters must NEVER block longer than one frame interval.
"""

from __future__ import annotations

import abc
import logging
import queue
import threading
from typing import Optional

from playout_core.decoder import VideoFrame

log = logging.getLogger("redtv.playout.output")


class OutputAdapter(abc.ABC):
    """Abstract base for all output sinks."""

    @abc.abstractmethod
    def write_frame(self, frame: VideoFrame) -> None:
        """
        Called from RT thread.
        MUST return within one frame interval (40ms at 25fps).
        MUST NOT block on I/O, file access, or acquire heavy locks.
        """

    @abc.abstractmethod
    def write_hold(self, frame: Optional[VideoFrame]) -> None:
        """Called when entering HOLD_FRAME — output frozen frame or black."""

    def start(self) -> None:
        """Lifecycle: called before playout begins."""

    def stop(self) -> None:
        """Lifecycle: called on shutdown."""


class NullAdapter(OutputAdapter):
    """Discards all frames.  Used in tests and benchmarks."""

    def __init__(self) -> None:
        self._frame_count = 0

    def write_frame(self, frame: VideoFrame) -> None:
        self._frame_count += 1

    def write_hold(self, frame: Optional[VideoFrame]) -> None:
        pass

    @property
    def frame_count(self) -> int:
        return self._frame_count


class PreviewWindowAdapter(OutputAdapter):
    """
    MJPEG preview adapter.
    Pushes JPEG-encoded frames into a bounded queue for the API gateway
    to serve as a confidence preview stream.

    Encoding is done asynchronously in a worker thread so the RT loop
    never waits on JPEG compression.
    """

    QUEUE_SIZE = 5   # max queued frames; older frames dropped on overflow

    def __init__(self, target_width: int = 640, target_height: int = 360) -> None:
        self._target_w = target_width
        self._target_h = target_height
        self._raw_queue: queue.Queue[Optional[VideoFrame]] = queue.Queue(maxsize=self.QUEUE_SIZE)
        self._jpeg_queue: queue.Queue[bytes] = queue.Queue(maxsize=self.QUEUE_SIZE)
        self._worker: Optional[threading.Thread] = None
        self._running = False

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._worker = threading.Thread(target=self._encode_loop, daemon=True, name="preview-enc")
        self._worker.start()
        log.info("PreviewWindowAdapter started", extra={"service": "output"})

    def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        try:
            self._raw_queue.put_nowait(None)   # sentinel
        except queue.Full:
            pass
        if self._worker and self._worker is not threading.current_thread():
            self._worker.join(timeout=1.0)
        self._worker = None

    def write_frame(self, frame: VideoFrame) -> None:
        """RT thread: non-blocking put."""
        try:
            self._raw_queue.put_nowait(frame)
        except queue.Full:
            pass   # drop frame — preview is best-effort

    def write_hold(self, frame: Optional[VideoFrame]) -> None:
        if frame:
            self.write_frame(frame)

    def get_jpeg(self, timeout: float = 0.1) -> Optional[bytes]:
        """API thread: get latest JPEG for MJPEG stream."""
        try:
            return self._jpeg_queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def _encode_loop(self) -> None:
        while self._running:
            try:
                frame = self._raw_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            if frame is None:
                break
            jpeg = self._encode_jpeg(frame)
            if jpeg:
                try:
                    self._jpeg_queue.put_nowait(jpeg)
                except queue.Full:
                    try:
                        self._jpeg_queue.get_nowait()   # discard old
                        self._jpeg_queue.put_nowait(jpeg)
                    except queue.Empty:
                        pass

    def _encode_jpeg(self, frame: VideoFrame) -> Optional[bytes]:
        """Convert raw YUV422 bytes to JPEG.  Uses Pillow when available."""
        if not frame.data:
            return None
        try:
            import io
            import numpy as np
            from PIL import Image

            # Reconstruct YUV422 plane
            arr = np.frombuffer(frame.data, dtype=np.uint8).reshape(1080, 1920 * 2)
            # Simple Y-only extraction for preview (luminance)
            y = arr[:, 0::2]   # every other byte is Y
            img = Image.fromarray(y, mode="L").convert("RGB")
            img = img.resize((self._target_w, self._target_h), Image.BILINEAR)
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=70)
            return buf.getvalue()
        except Exception:
            return None


class DeckLinkAdapter(OutputAdapter):
    """
    STUB — BMD DeckLink SDI output adapter.
    Requires Blackmagic Desktop Video SDK + Python bindings (not included in v2).

    Replace the stub methods below with real SDK calls in v2.1.
    """

    def __init__(self, device_index: int = 0, mode: str = "1080i25") -> None:
        self._device_index = device_index
        self._mode         = mode
        self._sdk_loaded   = False
        log.warning(
            "DeckLinkAdapter is a STUB — SDI output not active (v2 TODO)",
            extra={"service": "output"}
        )

    def start(self) -> None:
        # TODO: initialize BMD SDK, open device, set output mode
        log.info("DeckLink STUB start (device=%d, mode=%s)",
                 self._device_index, self._mode, extra={"service": "output"})

    def stop(self) -> None:
        # TODO: stop output, release device
        pass

    def write_frame(self, frame: VideoFrame) -> None:
        # TODO: copy frame.data into DeckLink frame buffer, schedule output
        pass

    def write_hold(self, frame: Optional[VideoFrame]) -> None:
        # TODO: hold last frame on SDI output
        pass
