"""
shared_contracts.rt_ring  (v3.3 — DUAL SOURCE)
================================================
RTRingBuffer — Single-Producer/Single-Consumer lockless ring buffer.

v3.3 additions
--------------
New RTEventKind values for dual-source program switching:
  PROGRAM_SOURCE_CHANGED  — RT loop observed the source swap
  LIVE_SOURCE_READY       — live adapter buffered and ready
  LIVE_SOURCE_LOST        — live adapter lost signal / EOF
  SWITCH_REFUSED          — switch requested but live not ready

All other design is unchanged.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import IntEnum
from typing import List


class RTEventKind(IntEnum):
    FRAME_OUT               = 1
    FRAME_DROP              = 2
    FRAME_HOLD              = 3
    STATE_CHANGE            = 4
    SWAP_DONE               = 5
    DRIFT_WARN              = 6
    DRIFT_ERROR             = 7
    GUARD_VIOLATION         = 8
    CLIP_END                = 9
    EMERGENCY_ENTER         = 10
    HEARTBEAT               = 11
    # v3.3 dual-source events
    PROGRAM_SOURCE_CHANGED  = 12   # detail=0 automation, 1=live
    LIVE_SOURCE_READY       = 13
    LIVE_SOURCE_LOST        = 14
    SWITCH_REFUSED          = 15   # live not ready


@dataclass(slots=True)
class RTEvent:
    kind:        int   = 0
    frame_number: int  = 0
    timestamp:   float = 0.0
    drift_ms:    float = 0.0
    severity:    int   = 0
    state_value: int   = 0
    slot_index:  int   = -1
    detail:      int   = 0


_EMPTY = RTEvent()


class RTRingBuffer:
    """
    Fixed-size SPSC ring buffer.
    Producer: RT thread (push, owns ``_write``).
    Consumer: control thread (drain, owns ``_read``).

    On overflow the producer drops the new event rather than overwriting
    unread data. This preserves the SPSC index-ownership invariant and keeps
    delivered event fields coherent under load.
    """

    def __init__(self, capacity: int = 512) -> None:
        assert capacity > 0 and (capacity & (capacity - 1)) == 0, \
            "capacity must be a power of 2"
        self._cap   = capacity
        self._mask  = capacity - 1
        self._slots: List[RTEvent] = [RTEvent() for _ in range(capacity)]
        self._write = 0
        self._read  = 0
        self._dropped = 0

    def push(
        self,
        kind:         int,
        frame_number: int   = 0,
        drift_ms:     float = 0.0,
        severity:     int   = 1,
        state_value:  int   = 0,
        slot_index:   int   = -1,
        detail:       int   = 0,
    ) -> None:
        w = self._write
        r = self._read
        if (w - r) >= self._cap:
            # SPSC ownership invariant: only the consumer may advance _read.
            # When full, conservatively drop the *new* event. Overwriting an
            # unread slot (or moving _read here) can race with drain() and
            # produce duplicate or torn telemetry under overload.
            self._dropped += 1
            return

        slot = self._slots[w & self._mask]
        slot.kind         = kind
        slot.frame_number = frame_number
        slot.timestamp    = time.monotonic()
        slot.drift_ms     = drift_ms
        slot.severity     = severity
        slot.state_value  = state_value
        slot.slot_index   = slot_index
        slot.detail       = detail
        self._write       = w + 1

    def drain(self) -> List[RTEvent]:
        results: List[RTEvent] = []
        r = self._read
        w = self._write
        while r != w:
            src = self._slots[r & self._mask]
            results.append(RTEvent(
                kind=src.kind, frame_number=src.frame_number,
                timestamp=src.timestamp, drift_ms=src.drift_ms,
                severity=src.severity, state_value=src.state_value,
                slot_index=src.slot_index, detail=src.detail,
            ))
            r += 1
        self._read = r
        return results

    @property
    def has_data(self) -> bool:
        return self._write != self._read

    @property
    def dropped_count(self) -> int:
        return self._dropped

    @property
    def pending_count(self) -> int:
        return self._write - self._read
