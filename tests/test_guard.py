"""tests/test_guard.py  (v2.1) — RealTimeGuard + @rt_forbidden tests."""

import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared_contracts.guard import (
    GuardConfig, RealTimeGuard, RTViolationError, rt_forbidden,
)
from shared_contracts.rt_ring import RTRingBuffer, RTEventKind


def make_guard(enforce=True, force_hold=True, patch_logging=False):
    return RealTimeGuard(GuardConfig(
        enforce=enforce, force_hold=force_hold,
        patch_logging=patch_logging, patch_open=False, patch_subprocess=False,
    ))


def test_non_rt_assert_passes():
    guard = make_guard()
    guard.register_rt_thread()   # this thread IS RT

    result = []

    def worker():
        try:
            guard.assert_not_rt("av.open")
            result.append("ok")
        except RTViolationError:
            result.append("violation")

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    assert result == ["ok"]


def test_rt_thread_raises():
    guard = make_guard(enforce=True)
    guard.register_rt_thread()
    with pytest.raises(RTViolationError):
        guard.assert_not_rt("file.open")


def test_warn_only_mode():
    guard = make_guard(enforce=False)
    guard.register_rt_thread()
    guard.assert_not_rt("risky_op")   # should not raise
    assert guard.violation_count == 1


def test_violation_pushed_to_ring():
    ring  = RTRingBuffer(capacity=32)
    guard = make_guard(enforce=False)
    guard.register_rt_thread(event_ring=ring)
    guard.assert_not_rt("subprocess.Popen")
    events = ring.drain()
    assert any(e.kind == RTEventKind.GUARD_VIOLATION for e in events)


def test_violation_callback():
    guard = make_guard(enforce=False)
    guard.register_rt_thread()
    log   = []
    guard.add_violation_callback(lambda op: log.append(op))
    guard.assert_not_rt("json.loads")
    assert log == ["json.loads"]


def test_violation_details_recorded():
    guard = make_guard(enforce=False)
    guard.register_rt_thread()
    guard.assert_not_rt("op_alpha")
    guard.assert_not_rt("op_beta")
    details = guard.recent_violations
    ops = [d[0] for d in details]
    assert "op_alpha" in ops
    assert "op_beta" in ops


def test_guard_inactive_before_register():
    guard = make_guard()
    # No thread registered — should not raise
    guard.assert_not_rt("any_op")


def test_rt_forbidden_decorator_from_non_rt():
    guard = make_guard()
    guard.register_rt_thread()   # this thread IS RT

    class Worker:
        _guard = guard

        @rt_forbidden("Worker.do_io")
        def do_io(self):
            return "result"

    w = Worker()

    # Call from non-RT thread — should succeed
    result = [None]

    def non_rt():
        result[0] = w.do_io()

    t = threading.Thread(target=non_rt)
    t.start()
    t.join()
    assert result[0] == "result"


def test_rt_forbidden_decorator_from_rt():
    guard = make_guard(enforce=True)
    guard.register_rt_thread()   # this thread IS RT

    class Worker:
        _guard = guard

        @rt_forbidden("Worker.blocking_call")
        def blocking_call(self):
            return "never"

    w = Worker()
    with pytest.raises(RTViolationError):
        w.blocking_call()


def test_guard_snapshot():
    """snapshot() must return all required health keys."""
    guard = make_guard(enforce=False)
    guard.register_rt_thread()
    guard.assert_not_rt("test_op_alpha")
    guard.assert_not_rt("test_op_beta")

    snap = guard.snapshot()
    assert snap["rt_thread_registered"] is True
    assert snap["violation_count"] == 2
    ops = [v["operation"] for v in snap["recent_violations"]]
    assert "test_op_alpha" in ops
    assert "test_op_beta"  in ops
    assert "logging_patched"  in snap
    assert "open_patched"     in snap
    assert "subprocess_patched" in snap
