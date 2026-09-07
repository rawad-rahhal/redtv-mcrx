"""
playout_core.program_router  (v3.3)
=====================================
ProgramRouter — dual-source program bus selector.

DESIGN
------
The router sits between the RT loop and two SourceAdapters:
  [AutomationSourceAdapter]  ──┐
                               ├──► ProgramRouter ──► RT output
  [LiveSourceAdapter]        ──┘

RT PATH (ZERO I/O, ZERO locks):
    get_program_frame()     reads _active (GIL-atomic int), delegates to adapter
    get_program_hold()      same
    is_program_ready()      same

The RT thread reads `_active` as a plain integer — CPython GIL-atomic.
The control thread writes `_active` only inside `_apply_switch()`.

SWITCHING LOGIC:
    1. Operator calls request_switch("live") via API
    2. Control thread validates live.is_ready()
    3. If NOT ready → push SWITCH_REFUSED into ring → return False
    4. If ready     → set _active = 1 (GIL-atomic) → push PROGRAM_SOURCE_CHANGED
    5. RT thread reads new _active on next frame tick — seamless boundary

SAFETY INVARIANT:
    If live source is lost while on PROGRAM, control loop detects
    live.state == LOST and auto-switches back to automation.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from shared_contracts.guard import rt_forbidden, RealTimeGuard
from shared_contracts.rt_ring import RTRingBuffer, RTEventKind
from playout_core.decoder import VideoFrame
from playout_core.sources.base import SourceAdapter, SourceState
from playout_core.sources.automation_source import AutomationSourceAdapter
from playout_core.sources.live_source import LiveSourceAdapter

log = logging.getLogger("redtv.program_router")

_SRC_AUTOMATION = 0
_SRC_LIVE       = 1


class ProgramRouter:
    """
    Dual-source program selector.

    Usage:
        router = ProgramRouter(automation_adapter, live_adapter, ring=ring)
        # In RT loop:
        frame = router.get_program_frame()
        hold  = router.get_program_hold()
        # In control loop:
        router.tick_control()          # watchdog + deferred switch apply
        router.request_switch("live")  # operator command
    """

    def __init__(
        self,
        automation: AutomationSourceAdapter,
        live:       LiveSourceAdapter,
        ring:       Optional[RTRingBuffer] = None,
    ) -> None:
        self._automation  = automation
        self._live        = live
        self._ring        = ring

        # Written ONLY by control thread; read by RT thread (GIL-atomic)
        self._active: int = _SRC_AUTOMATION

        # Pending switch request (control thread sets, apply_switch clears)
        self._pending_source: Optional[str] = None
        self._switch_ts: float = 0.0

        # Switch refused events counter
        self._refused_count: int = 0

    # ── RT interface (ZERO I/O) ────────────────────────────────────────

    def get_program_frame(self) -> Optional[VideoFrame]:
        """RT thread only — GIL-atomic int read, then deque.popleft."""
        if self._active == _SRC_LIVE:
            return self._live.get_frame()
        return self._automation.get_frame()

    def get_program_hold(self) -> Optional[VideoFrame]:
        """RT thread only — GIL-atomic int read, then reference read."""
        if self._active == _SRC_LIVE:
            return self._live.get_hold_frame()
        return self._automation.get_hold_frame()

    # ── Properties (read from any thread, GIL-atomic) ─────────────────

    @property
    def program_source(self) -> str:
        return "live" if self._active == _SRC_LIVE else "automation"

    @property
    def refused_count(self) -> int:
        return self._refused_count

    # ── Control interface ─────────────────────────────────────────────

    @rt_forbidden("ProgramRouter.request_switch")
    def request_switch(self, source: str) -> tuple[bool, str]:
        """
        Request a program source switch.  Called from control thread / API.

        Returns (success, reason).
        If switching to live and live is not ready:
            → pushes SWITCH_REFUSED event
            → returns (False, reason)
        """
        if source not in ("automation", "live"):
            return False, f"Unknown source: {source}"

        if source == "automation" and self._active == _SRC_AUTOMATION:
            return True, "already on automation"
        if source == "live" and self._active == _SRC_LIVE:
            return True, "already on live"

        if source == "live" and not self._live.is_ready():
            self._refused_count += 1
            live_status = self._live.get_status()
            reason = (
                f"Live not ready "
                f"(state={live_status.get('state','?')}, "
                f"mode={live_status.get('mode','?')})"
            )
            if self._ring:
                self._ring.push(
                    kind     = RTEventKind.SWITCH_REFUSED,
                    severity = 2,
                    detail   = self._refused_count,
                )
            log.warning("Switch refused: %s", reason,
                        extra={"service": "program_router"})
            return False, reason

        self._pending_source = source
        return True, "switch queued"

    @rt_forbidden("ProgramRouter.tick_control")
    def tick_control(self) -> Optional[str]:
        """
        Called from control loop every 5ms.
        Applies any pending source switch and runs live watchdog.
        Returns event name if something notable happened, else None.
        """
        # Apply pending switch
        if self._pending_source is not None:
            event = self._apply_switch(self._pending_source)
            self._pending_source = None
            return event

        # Live watchdog: if live is on PROGRAM but lost signal → auto-switch back
        if (self._active == _SRC_LIVE
                and self._live.get_status().get("state") == SourceState.LOST.value):
            log.warning(
                "Live source LOST while on PROGRAM — auto-switching to automation",
                extra={"service": "program_router"},
            )
            self._apply_switch("automation")
            return "LIVE_SOURCE_LOST_AUTO_RECOVER"

        return None

    def _apply_switch(self, source: str) -> str:
        """
        Apply the switch.  Called ONLY from control thread.
        Writing to self._active is GIL-atomic in CPython.
        """
        old_src  = self.program_source
        new_idx  = _SRC_LIVE if source == "live" else _SRC_AUTOMATION
        self._active   = new_idx   # GIL-atomic write
        self._switch_ts = time.monotonic()

        if self._ring:
            self._ring.push(
                kind     = RTEventKind.PROGRAM_SOURCE_CHANGED,
                severity = 1,
                detail   = new_idx,
            )

        log.info(
            "Program source: %s → %s",
            old_src, source,
            extra={"service": "program_router"},
        )
        return "PROGRAM_SOURCE_CHANGED"

    # ── Status ────────────────────────────────────────────────────────

    @rt_forbidden("ProgramRouter.get_status")
    def get_status(self) -> dict:
        return {
            "program_source":   self.program_source,
            "switch_refused":   self._refused_count,
            "last_switch_ts":   self._switch_ts,
            "automation":       self._automation.get_status(),
            "live":             self._live.get_status(),
        }
