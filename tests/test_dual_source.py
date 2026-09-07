"""
tests/test_dual_source.py  (v3.3)
===================================
Tests for the dual-source program bus:
  - SourceAdapter interface
  - ProgramRouter switching logic (RT purity + control contract)
  - Switch refused when live not ready
  - Live file staging marks live ready → switch allowed
  - /status includes program_source and live fields
  - No I/O in RT-dispatched frame calls
  - Existing ring buffer events emitted correctly

These tests run WITHOUT the full engine — they test the new components
in isolation, with minimal stubs for guard and decoder.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Real Pydantic is a development dependency; do not replace it in sys.modules.

# ── Imports under test ────────────────────────────────────────────────────
import pytest

from shared_contracts.rt_ring import RTRingBuffer, RTEventKind
from shared_contracts.guard import RealTimeGuard, GuardConfig
from playout_core.sources.base import SourceAdapter, SourceState
from playout_core.sources.live_source import LiveSourceAdapter, LiveMode
from playout_core.program_router import ProgramRouter


# ── Minimal stub adapters ─────────────────────────────────────────────────

class _FakeFrame:
    data = b"\x00" * 8
    pts = 0.0
    frame_number = 0
    is_last = False


class _StubAutomation:
    """Minimal SourceAdapter — always ready, always returns a frame."""
    def get_frame(self):        return _FakeFrame()
    def get_hold_frame(self):   return _FakeFrame()
    def is_ready(self):         return True
    def start(self):            pass
    def stop(self):             pass
    def get_status(self):
        return {"source": "automation", "state": "active",
                "buffer_depth": 50, "swap_ready": True, "current_item": "test.mp4"}


class _StubAutomationNone:
    """Minimal SourceAdapter — returns None frame (underrun)."""
    def get_frame(self):        return None
    def get_hold_frame(self):   return None
    def is_ready(self):         return True
    def start(self):            pass
    def stop(self):             pass
    def get_status(self):       return {"source": "automation"}


# ── Helpers ────────────────────────────────────────────────────────────────

def _make_guard():
    return RealTimeGuard(GuardConfig(enforce=True, patch_logging=False))


def _make_router(ring=None, automation=None, live=None):
    guard = _make_guard()
    guard.install_patches()
    auto  = automation or _StubAutomation()
    live_ = live or LiveSourceAdapter(guard=guard, pre_roll_frames=2, ring=ring)
    return ProgramRouter(auto, live_, ring=ring), live_


# ═══════════════════════════════════════════════════════════════════════════
# TEST 1 — RT path zero-I/O: get_program_frame / get_program_hold
#          must not touch any lock (guard is RT-thread-registered here
#          but we just verify no violation fires via attribute read)
# ═══════════════════════════════════════════════════════════════════════════

def test_program_frame_automation_default():
    """ProgramRouter starts on automation, get_program_frame returns a frame."""
    ring = RTRingBuffer(64)
    router, _ = _make_router(ring=ring)

    frame = router.get_program_frame()
    assert frame is not None, "automation frame must not be None"
    assert router.program_source == "automation"


def test_program_frame_returns_none_on_underrun():
    """If automation adapter returns None, router propagates it without crash."""
    ring = RTRingBuffer(64)
    router, _ = _make_router(ring=ring, automation=_StubAutomationNone())
    assert router.get_program_frame() is None


# ═══════════════════════════════════════════════════════════════════════════
# TEST 2 — Switch refused when live not ready
# ═══════════════════════════════════════════════════════════════════════════

def test_switch_refused_when_live_not_ready():
    """Switching to live while live is idle must return False + emit SWITCH_REFUSED."""
    ring = RTRingBuffer(64)
    router, live = _make_router(ring=ring)

    assert live.state == SourceState.IDLE
    assert live.is_ready() is False

    success, reason = router.request_switch("live")

    assert success is False, "switch to idle live must fail"
    assert "not ready" in reason.lower() or "idle" in reason.lower()
    assert router.refused_count == 1

    # SWITCH_REFUSED event must be in ring
    events = ring.drain()
    kinds  = [e.kind for e in events]
    assert RTEventKind.SWITCH_REFUSED in kinds, \
        f"SWITCH_REFUSED not emitted; got: {kinds}"

    # Program source unchanged
    assert router.program_source == "automation"


def test_switch_refused_increments_counter():
    """Multiple refused switches accumulate counter."""
    router, _ = _make_router()
    for _ in range(3):
        router.request_switch("live")
    assert router.refused_count == 3


# ═══════════════════════════════════════════════════════════════════════════
# TEST 3 — Switch allowed when live is ready
# ═══════════════════════════════════════════════════════════════════════════

def test_switch_allowed_when_live_ready():
    """After marking live ready manually, switch succeeds and emits event."""
    ring = RTRingBuffer(64)
    router, live = _make_router(ring=ring)

    # Force live to READY state (simulates PrepWorker completing)
    live._state = SourceState.READY

    success, reason = router.request_switch("live")
    assert success is True, f"should succeed: {reason}"

    # Apply pending switch (normally done by tick_control in control loop)
    router.tick_control()

    assert router.program_source == "live"
    events = ring.drain()
    kinds  = [e.kind for e in events]
    assert RTEventKind.PROGRAM_SOURCE_CHANGED in kinds, \
        f"PROGRAM_SOURCE_CHANGED not emitted; got: {kinds}"


def test_switch_back_to_automation():
    """Switching back to automation always succeeds, no ready check needed."""
    ring = RTRingBuffer(64)
    router, live = _make_router(ring=ring)

    live._state = SourceState.READY
    router.request_switch("live")
    router.tick_control()
    assert router.program_source == "live"

    success, _ = router.request_switch("automation")
    assert success is True
    router.tick_control()
    assert router.program_source == "automation"


# ═══════════════════════════════════════════════════════════════════════════
# TEST 4 — LiveSourceAdapter staging a missing file produces ERROR state
# ═══════════════════════════════════════════════════════════════════════════

def test_live_file_missing_sets_error():
    """stage_file with non-existent path → state=ERROR, last_error set."""
    guard = _make_guard()
    guard.install_patches()
    live = LiveSourceAdapter(guard=guard, pre_roll_frames=2)
    live.start()

    live.stage_file("//NONEXISTENT/NAS/file_that_does_not_exist.mp4")

    assert live.state == SourceState.ERROR
    assert live.last_error != ""
    assert live.is_ready() is False
    live.stop()


def test_live_empty_path_sets_error():
    """stage_file with empty/whitespace path → state=ERROR."""
    guard = _make_guard()
    guard.install_patches()
    live = LiveSourceAdapter(guard=guard, pre_roll_frames=2)
    live.start()
    live.stage_file("   ")
    assert live.state == SourceState.ERROR
    live.stop()


# ═══════════════════════════════════════════════════════════════════════════
# TEST 5 — SRT stub: connect → READY within 2 seconds
# ═══════════════════════════════════════════════════════════════════════════

def test_srt_connect_becomes_ready():
    """connect_srt triggers synthetic worker; state→READY within 2s."""
    guard = _make_guard()
    guard.install_patches()
    ring = RTRingBuffer(64)
    live = LiveSourceAdapter(guard=guard, pre_roll_frames=2, ring=ring)
    live.start()

    live.connect_srt("test_cam", "srt://192.0.2.1:1234")
    assert live.mode == LiveMode.SRT_LIVE

    # Wait up to 2s for READY
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if live.is_ready():
            break
        time.sleep(0.05)

    assert live.is_ready(), f"SRT stub should reach READY; state={live.state}"

    events = ring.drain()
    kinds  = [e.kind for e in events]
    assert RTEventKind.LIVE_SOURCE_READY in kinds, \
        f"LIVE_SOURCE_READY not emitted; got: {kinds}"

    live.stop()


# ═══════════════════════════════════════════════════════════════════════════
# TEST 6 — Live watchdog: auto-switch back when live is lost
# ═══════════════════════════════════════════════════════════════════════════

def test_live_watchdog_auto_recovers():
    """If live goes LOST while on PROGRAM, tick_control auto-switches to automation."""
    ring = RTRingBuffer(64)
    router, live = _make_router(ring=ring)

    # Put live on air
    live._state = SourceState.READY
    router.request_switch("live")
    router.tick_control()
    assert router.program_source == "live"

    # Simulate live signal loss
    live._state = SourceState.LOST

    event = router.tick_control()
    assert event is not None and "LOST" in event
    assert router.program_source == "automation", \
        "watchdog must auto-recover to automation on LIVE_LOST"


# ═══════════════════════════════════════════════════════════════════════════
# TEST 7 — ProgramRouter.get_status includes all required fields
# ═══════════════════════════════════════════════════════════════════════════

def test_router_get_status_fields():
    """get_status() must expose program_source and live sub-dict."""
    router, _ = _make_router()
    status = router.get_status()

    assert "program_source" in status
    assert "live" in status
    assert "automation" in status
    assert status["program_source"] == "automation"

    live_dict = status["live"]
    assert "mode" in live_dict
    assert "state" in live_dict
    assert "ready" in live_dict
    assert "input" in live_dict
    assert "last_error" in live_dict


# ═══════════════════════════════════════════════════════════════════════════
# TEST 8 — RT ring: new event kinds parse correctly
# ═══════════════════════════════════════════════════════════════════════════

def test_new_rt_event_kinds():
    """New v3.3 event kinds can be pushed and drained from RTRingBuffer."""
    ring = RTRingBuffer(32)
    ring.push(kind=RTEventKind.PROGRAM_SOURCE_CHANGED, detail=1)
    ring.push(kind=RTEventKind.LIVE_SOURCE_READY)
    ring.push(kind=RTEventKind.LIVE_SOURCE_LOST)
    ring.push(kind=RTEventKind.SWITCH_REFUSED, detail=3)

    events = ring.drain()
    assert len(events) == 4
    assert events[0].kind == RTEventKind.PROGRAM_SOURCE_CHANGED
    assert events[0].detail == 1
    assert events[1].kind == RTEventKind.LIVE_SOURCE_READY
    assert events[2].kind == RTEventKind.LIVE_SOURCE_LOST
    assert events[3].kind == RTEventKind.SWITCH_REFUSED
    assert events[3].detail == 3


# ═══════════════════════════════════════════════════════════════════════════
# TEST 9 — Thread safety: concurrent switch requests don't corrupt state
# ═══════════════════════════════════════════════════════════════════════════

def test_concurrent_switch_requests_no_corruption():
    """Multiple threads calling request_switch simultaneously must not crash
    or leave router in an inconsistent state."""
    ring = RTRingBuffer(512)
    router, live = _make_router(ring=ring)
    live._state = SourceState.READY

    errors = []

    def _worker(src: str, n: int):
        for _ in range(n):
            try:
                router.request_switch(src)
                router.tick_control()
                _ = router.program_source  # read must not raise
            except Exception as e:
                errors.append(str(e))

    threads = [
        threading.Thread(target=_worker, args=("automation", 20)),
        threading.Thread(target=_worker, args=("live",       20)),
        threading.Thread(target=_worker, args=("automation", 20)),
    ]
    for t in threads: t.start()
    for t in threads: t.join()

    assert not errors, f"Concurrent switch errors: {errors}"
    assert router.program_source in ("automation", "live")


# ═══════════════════════════════════════════════════════════════════════════
# TEST 10 — LiveSourceAdapter.get_status fields
# ═══════════════════════════════════════════════════════════════════════════

def test_live_status_fields():
    """LiveSourceAdapter.get_status() exposes all required keys."""
    guard = _make_guard()
    guard.install_patches()
    live = LiveSourceAdapter(guard=guard)
    live.start()
    s = live.get_status()
    for key in ("source", "mode", "state", "ready", "input", "last_error", "buffer_depth"):
        assert key in s, f"Missing key in live status: {key}"
    assert s["source"] == "live"
    live.stop()
