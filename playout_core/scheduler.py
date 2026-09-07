"""
playout_core.scheduler  (v2.1 — HARDENED)
==========================================
PlayoutScheduler — 25fps frame tick generator.

v2.1 changes
------------
- ZERO logging calls inside wait_next_frame() — previously had a comment
  placeholder that could tempt a log.  Now fully clean.
- Drift events pushed directly into RTRingBuffer (no string, no I/O).
- Counters are plain integers — no lock needed (RT is sole writer).
- reset() is only ever called from control thread; safe.
"""

from __future__ import annotations

import time
from enum import Enum
from typing import Optional

from shared_contracts.rt_ring import RTRingBuffer, RTEventKind


class DriftStatus(int, Enum):
    OK    = 0
    WARN  = 1
    ERROR = 2


class FrameTick:
    """Returned by wait_next_frame().  Allocated once per frame — kept minimal."""
    __slots__ = ("frame_number", "expected_time", "actual_time", "drift_ms", "drift_status")

    def __init__(self, fn: int, et: float, at: float, dm: float, ds: DriftStatus) -> None:
        self.frame_number  = fn
        self.expected_time = et
        self.actual_time   = at
        self.drift_ms      = dm
        self.drift_status  = ds


class PlayoutScheduler:
    """
    Real-time 25fps tick generator.

    RT thread usage:
        scheduler.start()
        while True:
            tick = scheduler.wait_next_frame()
            # deliver frame
    """

    def __init__(
        self,
        frame_rate:     float = 25.0,
        drift_warn_ms:  float = 20.0,
        drift_error_ms: float = 100.0,
        ring:           Optional[RTRingBuffer] = None,
    ) -> None:
        self._frame_rate      = frame_rate
        self._frame_interval  = 1.0 / frame_rate
        self._drift_warn      = drift_warn_ms  / 1000.0
        self._drift_error     = drift_error_ms / 1000.0
        self._ring            = ring

        self._start_time:     Optional[float] = None
        self._frame_number    = 0
        self._total_frames    = 0
        self._drift_warn_count  = 0
        self._drift_error_count = 0
        # Accumulate drift sum for moving average (control thread reads)
        self._drift_sum_ms    = 0.0

    def set_ring(self, ring: RTRingBuffer) -> None:
        self._ring = ring

    def start(self) -> None:
        self._start_time   = time.monotonic()
        self._frame_number = 0

    def wait_next_frame(self) -> FrameTick:
        """
        Block until next frame deadline.
        NO logging, NO I/O, NO allocation except the returned FrameTick.
        Sleep is capped to one frame interval (RT guard compliant).
        """
        if self._start_time is None:
            self.start()

        expected = self._start_time + self._frame_number * self._frame_interval
        now      = time.monotonic()
        sleep_s  = expected - now

        if 0 < sleep_s <= self._frame_interval:
            time.sleep(sleep_s)
        elif sleep_s > self._frame_interval:
            time.sleep(self._frame_interval)
        # If sleep_s <= 0: we're late — process immediately

        actual   = time.monotonic()
        drift    = actual - expected
        drift_ms = drift * 1000.0
        abs_d    = abs(drift)

        if abs_d >= self._drift_error:
            ds = DriftStatus.ERROR
            self._drift_error_count += 1
            if self._ring is not None:
                self._ring.push(
                    kind      = RTEventKind.DRIFT_ERROR,
                    drift_ms  = drift_ms,
                    severity  = 3,
                    frame_number = self._frame_number,
                )
        elif abs_d >= self._drift_warn:
            ds = DriftStatus.WARN
            self._drift_warn_count += 1
            if self._ring is not None:
                self._ring.push(
                    kind      = RTEventKind.DRIFT_WARN,
                    drift_ms  = drift_ms,
                    severity  = 2,
                    frame_number = self._frame_number,
                )
        else:
            ds = DriftStatus.OK

        self._drift_sum_ms += drift_ms
        tick = FrameTick(self._frame_number, expected, actual, drift_ms, ds)
        self._frame_number += 1
        self._total_frames += 1
        return tick

    # ------------------------------------------------------------------
    # Readable from control thread (snapshot only, no lock — counters are
    # incremented by RT thread; individual int reads are GIL-atomic)
    # ------------------------------------------------------------------

    @property
    def timeline_seconds(self) -> float:
        if self._start_time is None:
            return 0.0
        return time.monotonic() - self._start_time

    @property
    def drift_warn_count(self) -> int:
        return self._drift_warn_count

    @property
    def drift_error_count(self) -> int:
        return self._drift_error_count

    @property
    def total_frames(self) -> int:
        return self._total_frames

    @property
    def last_drift_ms(self) -> float:
        """Instantaneous drift from most recent frame."""
        return self._drift_sum_ms / max(1, self._total_frames)

    def reset(self) -> None:
        """Control thread only."""
        self._start_time   = time.monotonic()
        self._frame_number = 0
        self._drift_sum_ms = 0.0
