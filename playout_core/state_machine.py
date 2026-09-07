"""
playout_core.state_machine  (v3 — PRODUCTION HARDENED)
=========================================================
PlayoutFSM — deterministic FSM with ring-buffer event emission.

v3 bug fix
----------
FIX: Added PlayoutState.LOADING to the allowed transitions from PRE_ROLL
     and HOLD_FRAME.  v2.1 omitted these, meaning a load_playlist() call
     while in PRE_ROLL or HOLD_FRAME would silently fail the FSM transition
     while the playlist dict was already replaced — causing the engine to
     play items from the new playlist out of index order.
"""

from __future__ import annotations

import datetime
import threading
import time
from collections import deque
from enum import Enum
from typing import Callable, Deque, List, Optional, Tuple

from shared_contracts.events import BroadcastEvent, Severity
from shared_contracts.rt_ring import RTRingBuffer, RTEventKind


class PlayoutState(str, Enum):
    IDLE        = "IDLE"
    LOADING     = "LOADING"
    PRE_ROLL    = "PRE_ROLL"
    PLAYING     = "PLAYING"
    HOLD_FRAME  = "HOLD_FRAME"
    EMERGENCY   = "EMERGENCY"
    ERROR       = "ERROR"
    SHUTDOWN    = "SHUTDOWN"


_STATE_ORDINAL = {s: i for i, s in enumerate(PlayoutState)}

_TRANSITIONS: dict[PlayoutState, set[PlayoutState]] = {
    PlayoutState.IDLE:       {
        PlayoutState.LOADING,
        PlayoutState.EMERGENCY,
        PlayoutState.SHUTDOWN,
    },
    PlayoutState.LOADING:    {
        PlayoutState.PRE_ROLL,
        PlayoutState.HOLD_FRAME,
        PlayoutState.EMERGENCY,
        PlayoutState.ERROR,
    },
    PlayoutState.PRE_ROLL:   {
        PlayoutState.PLAYING,
        PlayoutState.HOLD_FRAME,
        PlayoutState.LOADING,    # FIX: was missing; needed for playlist reload
        PlayoutState.EMERGENCY,
        PlayoutState.ERROR,
    },
    PlayoutState.PLAYING:    {
        PlayoutState.LOADING,
        PlayoutState.HOLD_FRAME,
        PlayoutState.EMERGENCY,
        PlayoutState.ERROR,
        PlayoutState.IDLE,
        PlayoutState.SHUTDOWN,
    },
    PlayoutState.HOLD_FRAME: {
        PlayoutState.PLAYING,
        PlayoutState.LOADING,    # FIX: was missing; needed for playlist reload
        PlayoutState.EMERGENCY,
        PlayoutState.ERROR,
        PlayoutState.SHUTDOWN,
    },
    PlayoutState.EMERGENCY:  {
        PlayoutState.LOADING,
        PlayoutState.IDLE,
        PlayoutState.ERROR,
        PlayoutState.SHUTDOWN,
    },
    PlayoutState.ERROR:      {
        PlayoutState.IDLE,
        PlayoutState.LOADING,
        PlayoutState.EMERGENCY,
        PlayoutState.SHUTDOWN,
    },
    PlayoutState.SHUTDOWN:   set(),
}


class PlayoutFSM:
    """
    Thread-safe FSM.  transition() is called from the control thread only.
    The RT thread reads .state (GIL-atomic reference read).
    """

    def __init__(
        self,
        initial:  PlayoutState = PlayoutState.IDLE,
        ring:     Optional[RTRingBuffer] = None,
        event_cb: Optional[Callable[[BroadcastEvent], None]] = None,
    ) -> None:
        self._state            = initial
        self._lock             = threading.Lock()
        self._ring             = ring
        self._event_cb         = event_cb
        self._state_ts         = time.monotonic()
        self._current_item:    Optional[str] = None
        self._next_item:       Optional[str] = None
        self._timeline_offset: float = 0.0
        self._hold_count       = 0
        self._emergency_count  = 0
        self._history: Deque[Tuple[str, float]] = deque(maxlen=32)

    # ── Properties (GIL-atomic reads) ────────────────────────────────

    @property
    def state(self) -> PlayoutState:
        return self._state

    @property
    def current_item(self) -> Optional[str]:
        return self._current_item

    @property
    def next_item(self) -> Optional[str]:
        return self._next_item

    @property
    def state_age_seconds(self) -> float:
        return time.monotonic() - self._state_ts

    @property
    def hold_count(self) -> int:
        return self._hold_count

    @property
    def emergency_count(self) -> int:
        return self._emergency_count

    @property
    def state_history(self) -> List[str]:
        return [s for s, _ in self._history]

    # ── Transition (control thread only) ─────────────────────────────

    def transition(
        self,
        to:              PlayoutState,
        *,
        current_item:    Optional[str] = None,
        next_item:       Optional[str] = None,
        warning:         Optional[str] = None,
        severity:        Severity = Severity.INFO,
        timeline_offset: Optional[float] = None,
        slot_index:      int = -1,
    ) -> bool:
        with self._lock:
            if to not in _TRANSITIONS.get(self._state, set()):
                return False

            from_state   = self._state
            self._state  = to
            self._state_ts = time.monotonic()

            if current_item is not None:
                self._current_item = current_item
            if next_item is not None:
                self._next_item = next_item
            if timeline_offset is not None:
                self._timeline_offset = timeline_offset

            if to == PlayoutState.HOLD_FRAME:
                self._hold_count += 1
            if to == PlayoutState.EMERGENCY:
                self._emergency_count += 1

            self._history.append((to.value, time.monotonic()))

        if self._ring is not None:
            sev_int = {"debug":0,"info":1,"warning":2,"error":3,"critical":4}.get(
                severity.value, 1
            )
            self._ring.push(
                kind        = RTEventKind.STATE_CHANGE,
                severity    = sev_int,
                state_value = _STATE_ORDINAL.get(to, 0),
                slot_index  = slot_index,
            )

        self._emit_ws(to, current_item, next_item, warning, severity, timeline_offset)
        return True

    def _emit_ws(self, state, current, nxt, warning, severity, offset) -> None:
        if self._event_cb is None:
            return
        evt = BroadcastEvent(
            service         = "playout_core",
            state           = state.value,
            current_item    = current or self._current_item,
            next_item       = nxt or self._next_item,
            timeline_offset = offset or self._timeline_offset,
            warning         = warning,
            severity        = severity,
            timestamp_iso   = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
        )
        try:
            self._event_cb(evt)
        except Exception:
            pass

    def set_ring(self, ring: RTRingBuffer) -> None:
        self._ring = ring
