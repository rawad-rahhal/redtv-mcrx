"""
playout_core.engine  (v3.3 — DUAL SOURCE)
==========================================
PlayoutEngine — deterministic 1080i25 playout with dual-source bus.

v3.3 additions (surgical — no RT loop rewrites)
-------------------------------------------------
- ProgramRouter wired: RT loop delegates get_frame / get_hold to router
  when dual-source is enabled.
- _get_program_frame() / _get_program_hold() helper methods isolate the
  dispatch so _rt_playing() / _rt_hold() / _rt_emergency() are unchanged
  in logic — they just call helpers instead of decoder directly.
- control loop: router.tick_control() called every iteration to apply
  pending source switches and run live watchdog.
- get_status() extended with program_source + live fields.
- install_patches() still called from start() (main thread).
"""

from __future__ import annotations

import datetime
import logging
import threading
import time
from typing import Callable, List, Optional

from shared_contracts.events import BroadcastEvent, Severity
from shared_contracts.guard import GuardConfig, RealTimeGuard, rt_forbidden, set_global_guard
from shared_contracts.models import Playlist, PlaylistItem, SlotType
from shared_contracts.rt_ring import RTEvent, RTEventKind, RTRingBuffer

from playout_core.decoder import DoubleBufferDecoder, VideoFrame, FRAME_BYTES
from playout_core.output_adapters import NullAdapter, OutputAdapter
from playout_core.scheduler import PlayoutScheduler
from playout_core.state_machine import PlayoutFSM, PlayoutState
# v3.3: dual-source router (optional — None means automation-only mode)
from playout_core.program_router import ProgramRouter

log = logging.getLogger("redtv.playout.engine")

_HEARTBEAT_INTERVAL = 25   # frames (once per second at 25fps)


class PlayoutEngine:
    """
    24/7 playout orchestrator.  Thread-safe public API.
    """

    def __init__(
        self,
        config:         dict,
        output_adapter: Optional[OutputAdapter] = None,
        event_callback: Optional[Callable[[BroadcastEvent], None]] = None,
    ) -> None:
        self._cfg      = config
        self._output   = output_adapter or NullAdapter()
        self._event_cb = event_callback

        playout_cfg = config.get("playout", {})
        guard_cfg_d = config.get("guard",   {})

        # ── RT Ring ──────────────────────────────────────────────────
        self._ring = RTRingBuffer(capacity=512)

        # ── Guard ────────────────────────────────────────────────────
        guard_cfg = GuardConfig(
            enforce            = guard_cfg_d.get("enforce",            True),
            force_hold         = guard_cfg_d.get("force_hold",         True),
            patch_open         = guard_cfg_d.get("patch_open",         False),
            patch_subprocess   = guard_cfg_d.get("patch_subprocess",   False),
            patch_logging      = guard_cfg_d.get("patch_logging",      True),
            rt_log_buffer_size = guard_cfg_d.get("rt_log_buffer_size", 256),
        )
        self._guard = RealTimeGuard(guard_cfg)
        self._guard.set_event_ring(self._ring)
        self._guard.add_violation_callback(self._on_guard_violation_from_rt)
        set_global_guard(self._guard)

        # ── FSM ──────────────────────────────────────────────────────
        self._fsm = PlayoutFSM(ring=self._ring, event_cb=self._event_cb)

        # ── Scheduler ────────────────────────────────────────────────
        self._scheduler = PlayoutScheduler(
            frame_rate     = 25.0,
            drift_warn_ms  = playout_cfg.get("drift_warn_ms",  20),
            drift_error_ms = playout_cfg.get("drift_error_ms", 100),
            ring           = self._ring,
        )

        # ── Decoder ──────────────────────────────────────────────────
        self._pre_roll_frames = playout_cfg.get("pre_roll_frames", 25)
        self._decoder = DoubleBufferDecoder(
            guard           = self._guard,
            pre_roll_frames = self._pre_roll_frames,
            on_swap_ready   = self._on_next_ready,
            on_error        = self._on_decoder_error,
            ring            = self._ring,
        )

        # ── Playlist state ───────────────────────────────────────────
        self._playlist:       Optional[Playlist] = None
        self._playlist_index: int                = 0
        self._playlist_lock   = threading.Lock()

        # ── Control signals ───────────────────────────────────────────
        self._swap_request    = threading.Event()   # RT sets; control clears
        self._stop_event      = threading.Event()

        # FIX 4: track whether current pending swap is a drop-in
        self._drop_in_pending = False
        self._drop_in_lock    = threading.Lock()

        # FIX 5: idempotency for _control_load_first()
        self._load_initiated  = False

        # ── Emergency ────────────────────────────────────────────────
        self._emergency_item: Optional[PlaylistItem] = None
        paths = config.get("paths", {})
        self._emergency_path = playout_cfg.get(
            "emergency_slate", paths.get("emergency_slate", "")
        )
        self._hold_timeout = playout_cfg.get("hold_frame_timeout", 5.0)

        # ── Counters (RT writes / control reads) ─────────────────────
        self._frame_count:            int = 0
        self._hold_count:             int = 0
        self._guard_violation_count:  int = 0
        self._emergency_count:        int = 0

        # ── Pre-allocated black frame ─────────────────────────────────
        self._black_frame = VideoFrame(data=bytes(FRAME_BYTES), pts=0.0)

        # ── Program router (set by attach_program_router after construction) ──
        # None = automation-only mode (legacy behaviour preserved)
        self._program_router: Optional[ProgramRouter] = None

        # ── Threads ──────────────────────────────────────────────────
        self._rt_thread:      Optional[threading.Thread] = None
        self._control_thread: Optional[threading.Thread] = None
        self._running:        bool = False

    # ══════════════════════════════════════════════════════════════════
    # Lifecycle
    # ══════════════════════════════════════════════════════════════════

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._stop_event.clear()

        # FIX 1: Install patches BEFORE threads start so the logging patch
        # is active before any RT log call occurs.
        self._guard.install_patches()

        self._output.start()
        self._preload_emergency_slate()

        self._control_thread = threading.Thread(
            target=self._control_loop, daemon=True, name="playout-control"
        )
        self._rt_thread = threading.Thread(
            target=self._rt_loop, daemon=False, name="playout-rt"
        )
        self._control_thread.start()
        self._rt_thread.start()
        log.info("PlayoutEngine started", extra={"service": "engine"})

    def stop(self) -> None:
        log.info("PlayoutEngine stop requested", extra={"service": "engine"})
        # FIX 7: Transition FSM first so RT sees SHUTDOWN on its next tick
        self._fsm.transition(PlayoutState.SHUTDOWN, severity=Severity.WARNING)
        self._stop_event.set()
        if self._rt_thread:
            self._rt_thread.join(timeout=5.0)
        self._output.stop()
        self._running = False
        log.info("PlayoutEngine stopped", extra={"service": "engine"})

    def attach_program_router(self, router: ProgramRouter) -> None:
        """
        Wire a ProgramRouter for dual-source operation.
        Must be called BEFORE start().  Thread-safe (called from main thread).
        """
        self._program_router = router
        log.info("ProgramRouter attached — dual-source enabled",
                 extra={"service": "engine"})

    def _preload_emergency_slate(self) -> None:
        """Resolve and cache the configured emergency slate before RT starts."""
        from pathlib import Path

        path = self._emergency_path
        if path and Path(path).exists():
            self._emergency_item = PlaylistItem(
                slot_id          = "EMERGENCY",
                slot_type        = SlotType.SLATE,
                media_path       = path,
                title            = "EMERGENCY SLATE",
                duration_seconds = 30.0,
            )
            log.info("Emergency slate preloaded: %s", path,
                     extra={"service": "engine"})
        else:
            self._emergency_item = None
            log.warning("Emergency slate not found: %s", path,
                        extra={"service": "engine"})

    # ══════════════════════════════════════════════════════════════════
    # Public control API (any thread — all @rt_forbidden)
    # ══════════════════════════════════════════════════════════════════

    @rt_forbidden("engine.load_playlist")
    def load_playlist(self, playlist: Playlist) -> None:
        with self._playlist_lock:
            self._playlist       = playlist
            self._playlist_index = 0
            self._load_initiated = False   # allow _control_load_first() to run

        log.info(
            "Playlist loaded: id=%s items=%d corr=%s",
            playlist.playlist_id[:8], len(playlist.items),
            playlist.correlation_id or "—",
            extra={"service": "engine"},
        )

        # Transition to LOADING from wherever we are.
        # FSM v3 allows LOADING from PLAYING/PRE_ROLL/HOLD_FRAME/EMERGENCY/ERROR.
        # From IDLE this also works.  From SHUTDOWN it will fail — correct.
        if not self._fsm.transition(
            PlayoutState.LOADING,
            current_item = playlist.items[0].title if playlist.items else "",
        ):
            log.error(
                "Cannot load playlist: FSM refused LOADING from %s",
                self._fsm.state.value,
                extra={"service": "engine"},
            )

    @rt_forbidden("engine.drop_in_next")
    def drop_in_next(self, item: PlaylistItem) -> None:
        """
        Slot-safe drop-in.  Does NOT advance the playlist index (FIX 4).
        If called rapidly (spam), each call stops the previous NEXT worker
        before starting a new one — safe via CleanupWorker.
        """
        log.info("Drop-in: %s", item.title, extra={"service": "engine"})
        with self._drop_in_lock:
            self._drop_in_pending = True
        self._decoder.load_next(item)
        self._swap_request.set()

    @rt_forbidden("engine.activate_emergency")
    def activate_emergency(self) -> None:
        """Operator-triggered emergency.  Idempotent."""
        if self._fsm.state == PlayoutState.EMERGENCY:
            log.warning("activate_emergency: already in EMERGENCY",
                        extra={"service": "engine"})
            return
        log.warning("Emergency activated by operator",
                    extra={"service": "engine"})
        # FIX 3: only increment if transition succeeds
        if self._fsm.transition(
            PlayoutState.EMERGENCY,
            warning  = "Operator emergency",
            severity = Severity.CRITICAL,
        ):
            self._emergency_count += 1


    def get_playlist(self) -> Optional[Playlist]:
        """
        Return the currently loaded playlist (control-plane safe).

        NOTE: This is NOT used in the RT loop. It is guarded by the same lock
        used by load_playlist() so the API can expose the active playlist.
        """
        with self._lock:
            return self._playlist

    def get_status(self) -> dict:
        """Thread-safe snapshot for /status endpoint."""
        base = {
            # Required by spec
            "state":                  self._fsm.state.value,
            "current_media_id":       self._fsm.current_item,
            "next_media_id":          self._fsm.next_item,
            "drift_ms":               round(self._scheduler.last_drift_ms, 3),
            "hold_count":             self._hold_count,
            "emergency_count":        self._emergency_count,
            "guard_violation_count":  self._guard_violation_count,
            "cleanup_queue_size":     self._decoder.cleanup_queue_size,
            "rt_buffer_depth":        self._decoder.current_buffer_depth(),
            # Extended
            "timeline_seconds":       round(self._scheduler.timeline_seconds, 2),
            "frame_count":            self._frame_count,
            "frame_drop_count":       self._decoder.frame_drop_count,
            "active_decoder_count":   self._decoder.active_worker_count,
            "guard_recent_violations": [
                {"op": op, "ts": round(ts, 3)}
                for op, ts in (self._guard.recent_violations[-5:])
            ],
            "drift_warn_count":       self._scheduler.drift_warn_count,
            "drift_error_count":      self._scheduler.drift_error_count,
            "ring_pending":           self._ring.pending_count,
            "ring_dropped":           self._ring.dropped_count,
            "state_history":          self._fsm.state_history[-10:],
        }
        # v3.3: dual-source fields
        if self._program_router is not None:
            rs = self._program_router.get_status()
            base["program_source"] = rs["program_source"]
            base["live"]           = rs["live"]
            base["switch_refused"] = rs["switch_refused"]
        else:
            base["program_source"] = "automation"
            base["live"]           = {"mode": "idle", "ready": False,
                                      "input": "", "last_error": "",
                                      "state": "idle"}
            base["switch_refused"] = 0


        # ── Enriched item snapshots for renderers/UI (paths, titles)
        def _dump_item(it):
            if it is None:
                return None
            try:
                return it.model_dump(mode="json")  # pydantic v2
            except Exception:
                try:
                    return it.dict()  # pydantic v1 fallback
                except Exception:
                    return {"title": getattr(it, "title", ""), "media_path": getattr(it, "media_path", None), "media_id": getattr(it, "media_id", None)}

        try:
            cur = self._decoder.current_item()
            nxt = self._decoder.next_item()
            base["current"] = _dump_item(cur)
            base["next"]    = _dump_item(nxt)

            # N+2: compute next-next from playlist cursor (best-effort)
            with self._playlist_lock:
                pl  = self._playlist
                idx = self._playlist_index
            nn = None
            if pl and (idx + 1) < len(pl.items):
                nn = pl.items[idx + 1]
            base["next2"] = _dump_item(nn)
        except Exception as e:
            base.setdefault("current", None)
            base.setdefault("next", None)
            base.setdefault("next2", None)
            base["enrich_error"] = str(e)

        return base

        # ── RT frame dispatch helpers ─────────────────────────────────────
        # These are the ONLY methods the RT loop calls for frame data.
        # When no router is attached: direct decoder access (legacy).
        # When router is attached:    router dispatches to active source.
        # Both paths are zero-I/O, GIL-atomic.

    def _get_program_frame(self) -> Optional[VideoFrame]:
        if self._program_router is not None:
            return self._program_router.get_program_frame()
        return self._decoder.get_current_frame()

    def _get_program_hold(self) -> Optional[VideoFrame]:
        if self._program_router is not None:
            return self._program_router.get_program_hold()
        return self._decoder.get_hold_frame()

        # ══════════════════════════════════════════════════════════════════
        # RT LOOP — ZERO I/O, ZERO LOGGING
        # ══════════════════════════════════════════════════════════════════

    def _rt_loop(self) -> None:
        self._guard.register_rt_thread(event_ring=self._ring)
        self._scheduler.start()

        while not self._stop_event.is_set():
            self._scheduler.wait_next_frame()
            self._frame_count += 1

            state = self._fsm.state   # GIL-atomic reference read

            if state == PlayoutState.PLAYING:
                self._rt_playing()
            elif state == PlayoutState.HOLD_FRAME:
                self._rt_hold()
            elif state == PlayoutState.EMERGENCY:
                self._rt_emergency()
            elif state == PlayoutState.SHUTDOWN:
                break
            else:
                # IDLE / LOADING / PRE_ROLL / ERROR → black output
                self._output.write_frame(self._black_frame)

            if self._frame_count % _HEARTBEAT_INTERVAL == 0:
                self._ring.push(
                    kind         = RTEventKind.HEARTBEAT,
                    frame_number = self._frame_count,
                    severity     = 0,
                )

        self._ring.push(kind=RTEventKind.STATE_CHANGE, state_value=7, severity=2)

    def _rt_playing(self) -> None:
        frame = self._get_program_frame()

        if frame is None:
            # Buffer underrun
            self._hold_count += 1
            self._ring.push(kind=RTEventKind.FRAME_HOLD, severity=2,
                            frame_number=self._frame_count)
            self._output.write_hold(self._get_program_hold() or self._black_frame)
            return

        if frame.is_last:
            self._ring.push(kind=RTEventKind.CLIP_END,
                            frame_number=self._frame_count, severity=1)
            self._swap_request.set()
            self._output.write_hold(self._get_program_hold() or self._black_frame)
            return

        self._output.write_frame(frame)

    def _rt_hold(self) -> None:
        self._hold_count += 1
        self._output.write_hold(self._get_program_hold() or self._black_frame)

    def _rt_emergency(self) -> None:
        frame = self._get_program_frame()
        if frame and not frame.is_last and frame.data:
            self._output.write_frame(frame)
        else:
            self._output.write_hold(self._get_program_hold() or self._black_frame)

        # ══════════════════════════════════════════════════════════════════
        # CONTROL LOOP — all I/O, logging, FSM transitions
        # ══════════════════════════════════════════════════════════════════

    def _control_loop(self) -> None:
        log.info("Control loop started", extra={"service": "control"})
        last_drift_log = time.monotonic()

        while not self._stop_event.is_set():
            self._drain_ring()
            self._flush_deferred_logs()

            # v3.3: tick program router (applies pending switches, watchdog)
            if self._program_router is not None:
                event = self._program_router.tick_control()
                if event:
                    log.info("ProgramRouter: %s", event,
                             extra={"service": "control"})

            state = self._fsm.state

            if state == PlayoutState.LOADING:
                self._control_load_first()

            elif state == PlayoutState.PRE_ROLL:
                if self._decoder.is_swap_ready():
                    self._do_swap_and_play()

            elif state == PlayoutState.PLAYING:
                if self._swap_request.is_set():
                    self._swap_request.clear()
                    self._control_swap()

            elif state == PlayoutState.HOLD_FRAME:
                if self._fsm.state_age_seconds > self._hold_timeout:
                    log.error("Hold timeout → emergency",
                              extra={"service": "control"})
                    self._activate_emergency_internal()
                elif self._decoder.is_swap_ready():
                    self._do_swap_and_play(
                        warning  = "Recovered from HOLD_FRAME",
                        severity = Severity.WARNING,
                    )

            elif state == PlayoutState.EMERGENCY:
                if (self._emergency_item
                        and self._decoder.current_buffer_depth() < 5):
                    self._decoder.load_emergency(self._emergency_item)

            elif state == PlayoutState.SHUTDOWN:
                break

            # Periodic drift log
            now = time.monotonic()
            if now - last_drift_log >= 60.0:
                last_drift_log = now
                if self._scheduler.drift_error_count > 0:
                    log.warning(
                        "Drift 1min: warns=%d errors=%d avg=%.2fms",
                        self._scheduler.drift_warn_count,
                        self._scheduler.drift_error_count,
                        self._scheduler.last_drift_ms,
                        extra={"service": "control"},
                    )

            time.sleep(0.005)

        log.info("Control loop exited", extra={"service": "control"})

        # ── Control helpers ──────────────────────────────────────────────

    def _control_load_first(self) -> None:
        """
        Load items[0] into NEXT slot and transition to PRE_ROLL.
        FIX 5: _load_initiated flag prevents repeated calls while
        the FSM is still transitioning.
        """
        if self._load_initiated:
            return
        with self._playlist_lock:
            pl  = self._playlist
            idx = self._playlist_index
        if not pl or idx >= len(pl.items):
            return
        self._load_initiated = True   # set BEFORE load_next to prevent re-entry
        item = pl.items[idx]
        self._decoder.load_next(item)
        self._fsm.transition(PlayoutState.PRE_ROLL, current_item=item.title,
                             slot_index=idx)

    def _control_swap(self) -> None:
        if self._decoder.is_swap_ready():
            self._do_swap_and_play()
        else:
            log.warning("Swap requested but NEXT not ready — holding",
                        extra={"service": "control"})
            self._fsm.transition(PlayoutState.HOLD_FRAME,
                                 warning="Next clip not ready",
                                 severity=Severity.WARNING)

    def _do_swap_and_play(
        self,
        warning:  Optional[str] = None,
        severity: Severity      = Severity.INFO,
        ) -> None:
        """
        Perform NEXT→CURRENT swap, update FSM, pre-roll next item.

        FIX 4: Only advance _playlist_index for a natural playlist transition.
        Drop-ins do not advance the index.
        """
        self._decoder.swap()
        new_item = self._decoder.current_item()

        with self._drop_in_lock:
            is_dropin = self._drop_in_pending
            self._drop_in_pending = False

        if not is_dropin:
            with self._playlist_lock:
                self._playlist_index += 1

        self._fsm.transition(
            PlayoutState.PLAYING,
            current_item    = new_item.title if new_item else "",
            next_item       = None,
            warning         = warning,
            severity        = severity,
            timeline_offset = self._scheduler.timeline_seconds,
        )
        log.info("Swap → PLAYING: %s%s",
                 new_item.title if new_item else "?",
                 " [drop-in]" if is_dropin else "",
                 extra={"service": "control"})

        # For drop-ins don't pre-roll the next playlist item yet —
        # let the clip finish naturally; next CLIP_END will trigger another swap
        if not is_dropin:
            self._schedule_next_item()

    def _schedule_next_item(self) -> None:
        """
        Pre-roll the next playlist item into the NEXT slot.
        FIX 2: Use _playlist_index directly (not +1).
        After _do_swap_and_play increments index, it now points at the
        item that should be pre-rolling.
        """
        with self._playlist_lock:
            pl  = self._playlist
            idx = self._playlist_index   # FIX: was + 1, which skipped item B
        if not pl or idx >= len(pl.items):
            log.info("Playlist end — nothing to pre-roll",
                     extra={"service": "control"})
            return
        next_item = pl.items[idx]
        self._decoder.load_next(next_item)
        self._fsm.transition(PlayoutState.PLAYING, next_item=next_item.title,
                             slot_index=idx)
        log.info("Pre-rolling next[%d]: %s", idx, next_item.title,
                 extra={"service": "control"})

    def _activate_emergency_internal(self) -> None:
        """FIX 3: only increment counter if transition succeeds."""
        if self._fsm.transition(
            PlayoutState.EMERGENCY,
            warning  = "Auto-emergency (hold timeout)",
            severity = Severity.CRITICAL,
        ):
            self._emergency_count += 1
        if self._emergency_item:
            self._decoder.load_emergency(self._emergency_item)

        # ── Ring drain ───────────────────────────────────────────────────

    def _drain_ring(self) -> None:
        for evt in self._ring.drain():
            k = evt.kind

            if k == RTEventKind.GUARD_VIOLATION:
                self._guard_violation_count = evt.detail
                msg = f"RT guard violation #{evt.detail}"
                log.critical(msg, extra={"service": "guard"})
                self._emit_event(BroadcastEvent(
                    service       = "playout_core",
                    state         = self._fsm.state.value,
                    warning       = msg,
                    severity      = Severity.CRITICAL,
                    timestamp_iso = datetime.datetime.utcnow().isoformat() + "Z",
                ))
                if self._cfg.get("guard", {}).get("force_hold", True):
                    self._fsm.transition(
                        PlayoutState.HOLD_FRAME,
                        warning  = "Guard violation → HOLD",
                        severity = Severity.CRITICAL,
                    )

            elif k == RTEventKind.DRIFT_ERROR:
                log.error("Drift ERROR: %.1fms frame %d",
                          evt.drift_ms, evt.frame_number,
                          extra={"service": "scheduler"})
                self._emit_event(BroadcastEvent(
                    service       = "playout_core",
                    state         = self._fsm.state.value,
                    warning       = f"Drift error {evt.drift_ms:.1f}ms",
                    severity      = Severity.ERROR,
                    timestamp_iso = datetime.datetime.utcnow().isoformat() + "Z",
                    extra         = {"drift_ms": evt.drift_ms},
                ))

            elif k == RTEventKind.DRIFT_WARN:
                log.warning("Drift WARN: %.1fms frame %d",
                            evt.drift_ms, evt.frame_number,
                            extra={"service": "scheduler"})

            elif k == RTEventKind.FRAME_DROP:
                log.debug("Frame drop #%d", evt.detail,
                          extra={"service": "decoder"})

            elif k == RTEventKind.CLIP_END:
                log.info("Clip end frame %d", evt.frame_number,
                         extra={"service": "decoder"})

            elif k == RTEventKind.HEARTBEAT:
                pass   # RT thread alive confirmation — no action needed

            elif k == RTEventKind.EMERGENCY_ENTER:
                log.critical("Emergency from RT thread",
                             extra={"service": "engine"})

    def _flush_deferred_logs(self) -> None:
        for msg in self._guard.drain_deferred_logs():
            log.info("[RT-DEFERRED] %s", msg, extra={"service": "rt_log"})

    def _emit_event(self, event: BroadcastEvent) -> None:
        if self._event_cb:
            try:
                self._event_cb(event)
            except Exception:
                pass

        # ── Internal callbacks ────────────────────────────────────────────

    def _on_next_ready(self) -> None:
        """Called by PrepWorker when NEXT reaches pre-roll threshold."""
        if self._fsm.state == PlayoutState.PRE_ROLL:
            self._swap_request.set()

    def _on_decoder_error(self, msg: str) -> None:
        log.error("Decoder error: %s", msg, extra={"service": "engine"})
        self._fsm.transition(PlayoutState.HOLD_FRAME,
                             warning=f"Decoder: {msg}",
                             severity=Severity.ERROR)

    def _on_guard_violation_from_rt(self, operation: str) -> None:
        # Intentionally empty — ring push already done by _record_violation()
        # FSM transition happens in control thread via _drain_ring()
        pass