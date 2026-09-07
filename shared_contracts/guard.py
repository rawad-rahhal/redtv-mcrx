"""
shared_contracts.guard  (v3 — PRODUCTION HARDENED)
=====================================================
RealTimeGuard — enforces absolute RT thread purity.

v3 bug fixes vs v2.1
--------------------
FIX 1: Per-patch flags (_open_patched, _subprocess_patched, _logging_patched).
        v2.1 used a single shared _patched flag; if both patch_open and
        patch_subprocess were enabled only the first ever ran.

FIX 2: install_patches() must be called from the MAIN thread BEFORE the RT
        thread starts. v2.1 called _patch_logging() from inside
        register_rt_thread() which runs in the RT thread — so the first log
        call from RT (inside register_rt_thread itself) was NOT intercepted.

FIX 3: register_rt_thread() no longer calls logging.* at all.

FIX 4: _record_violation() is only called from the RT thread.  Removed the
        _violation_lock.  Plain int += 1 is GIL-atomic; deque.append is
        GIL-safe for single producer.

FIX 5: _RTLogHandler.emit() no longer takes _rt_log_lock from RT path.
        deque.append is GIL-atomic.  A separate _drain_lock protects only
        the drain path (control thread).
"""

from __future__ import annotations

import builtins
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Deque, List, Optional, Tuple

from shared_contracts.rt_ring import RTRingBuffer, RTEventKind


class RTViolationError(RuntimeError):
    """Raised when a forbidden operation is attempted inside the RT thread."""


@dataclass
class GuardConfig:
    enforce:            bool = True
    force_hold:         bool = True
    patch_open:         bool = False
    patch_subprocess:   bool = False
    patch_logging:      bool = True
    rt_log_buffer_size: int  = 256


class _RTLogHandler(logging.Handler):
    """
    Intercepts log records from the RT thread; defers them to an in-memory
    deque.  Non-RT records pass through to original handlers unchanged.

    RT path:    deque.append — NO lock (GIL-atomic, single producer).
    Drain path: _drain_lock (control thread only).
    """

    def __init__(self, guard: "RealTimeGuard", original_handlers: list) -> None:
        super().__init__()
        self._guard             = guard
        self._original_handlers = original_handlers
        self._rt_log_ring: Deque[str] = deque(maxlen=guard._cfg.rt_log_buffer_size)
        self._drain_lock        = threading.Lock()  # drain path only

    def emit(self, record: logging.LogRecord) -> None:
        if self._guard.is_rt_thread():
            # RT HOT PATH — zero locks, zero I/O
            try:
                msg = self.format(record)
            except Exception:
                try:
                    msg = "[RT-LOG] " + record.getMessage()
                except Exception:
                    msg = "[RT-LOG] <format error>"
            self._rt_log_ring.append(msg)   # GIL-atomic, single producer
        else:
            for h in self._original_handlers:
                try:
                    h.emit(record)
                except Exception:
                    pass

    def drain_rt_logs(self) -> List[str]:
        """Control thread only."""
        with self._drain_lock:
            items = list(self._rt_log_ring)
            self._rt_log_ring.clear()
        return items


class RealTimeGuard:
    """
    Thread-safety sentinel for the playout RT loop (v3).

    Correct call sequence:
        guard.install_patches()          # main thread, before threads start
        rt_thread = Thread(target=rt_loop)
        rt_thread.start()
        # inside rt_loop:
        guard.register_rt_thread(ring)   # sets thread id, nothing else
    """

    def __init__(self, config: Optional[GuardConfig] = None) -> None:
        self._cfg               = config or GuardConfig()
        self._rt_thread_id:     Optional[int] = None
        self._event_ring:       Optional[RTRingBuffer] = None

        # Written ONLY by RT thread — no lock needed (GIL guarantees)
        self._violation_count   = 0
        self._violation_details: Deque[Tuple[str, float]] = deque(maxlen=50)

        # Registered before RT starts; read-only after
        self._on_violation_cbs: List[Callable[[str], None]] = []

        # Per-patch flags (FIX 1: separate, not shared)
        self._logging_patched    = False
        self._open_patched       = False
        self._subprocess_patched = False

        self._rt_log_handler: Optional[_RTLogHandler] = None

    # ------------------------------------------------------------------ #
    # Patch installation — MAIN thread, BEFORE RT thread starts           #
    # ------------------------------------------------------------------ #

    def install_patches(self) -> None:
        """
        Install all configured patches.
        MUST be called from the main/control thread before the RT thread starts.
        Safe to call multiple times (idempotent per flag).
        """
        if self._cfg.patch_logging and not self._logging_patched:
            self._patch_logging()
        if self._cfg.patch_open and not self._open_patched:
            self._patch_open()
        if self._cfg.patch_subprocess and not self._subprocess_patched:
            self._patch_subprocess()

    # ------------------------------------------------------------------ #
    # RT thread registration — INSIDE RT thread                           #
    # ------------------------------------------------------------------ #

    def register_rt_thread(self, event_ring: Optional[RTRingBuffer] = None) -> None:
        """
        Record this thread as the RT thread.
        Called from INSIDE the RT thread at loop start.
        Does NOT call logging.* (patch may or may not be installed yet).
        """
        self._rt_thread_id = threading.get_ident()
        if event_ring is not None:
            self._event_ring = event_ring

    def set_event_ring(self, ring: RTRingBuffer) -> None:
        """Set ring from main thread (before RT starts)."""
        self._event_ring = ring

    def add_violation_callback(self, cb: Callable[[str], None]) -> None:
        self._on_violation_cbs.append(cb)

    # ------------------------------------------------------------------ #
    # Core enforcement                                                     #
    # ------------------------------------------------------------------ #

    def is_rt_thread(self) -> bool:
        return threading.get_ident() == self._rt_thread_id

    def assert_not_rt(self, operation: str) -> None:
        if self._rt_thread_id is None:
            return
        if not self.is_rt_thread():
            return
        self._record_violation(operation)
        if self._cfg.enforce:
            raise RTViolationError(
                f"RT violation: '{operation}' called from RT playout thread"
            )

    def _record_violation(self, operation: str) -> None:
        """
        Called ONLY from RT thread.
        NO locks — int += 1 is GIL-atomic, deque.append is GIL-safe
        for a single producer thread.
        """
        self._violation_count += 1        # GIL-atomic
        count = self._violation_count

        self._violation_details.append((operation, time.monotonic()))  # GIL-safe

        if self._event_ring is not None:
            self._event_ring.push(
                kind     = RTEventKind.GUARD_VIOLATION,
                severity = 4,
                detail   = count,
            )

        # Callbacks must be zero-I/O; never let one crash the RT thread
        for cb in self._on_violation_cbs:
            try:
                cb(operation)
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    # Observability (control thread)                                       #
    # ------------------------------------------------------------------ #

    @property
    def violation_count(self) -> int:
        return self._violation_count

    @property
    def recent_violations(self) -> List[Tuple[str, float]]:
        return list(self._violation_details)

    def drain_deferred_logs(self) -> List[str]:
        if self._rt_log_handler is None:
            return []
        return self._rt_log_handler.drain_rt_logs()

    def snapshot(self) -> dict:
        """
        Control thread only.
        Returns a dict snapshot of guard health for /api/debug/runtime.

        FIX: Added in v3.1 — debug.py called guard.snapshot() but the
        method did not exist, causing the guard key to always show
        {"error": "guard missing"} even when the guard was healthy.
        """
        return {
            "rt_thread_registered":  self._rt_thread_id is not None,
            "violation_count":       self._violation_count,
            "recent_violations":     [
                {"operation": op, "monotonic_ts": round(ts, 4)}
                for op, ts in list(self._violation_details)
            ],
            "logging_patched":       self._logging_patched,
            "open_patched":          self._open_patched,
            "subprocess_patched":    self._subprocess_patched,
        }

    # ------------------------------------------------------------------ #
    # Patch implementations (main thread)                                  #
    # ------------------------------------------------------------------ #

    def _patch_logging(self) -> None:
        root     = logging.getLogger()
        original = list(root.handlers)
        self._rt_log_handler = _RTLogHandler(self, original)
        self._rt_log_handler.setFormatter(
            logging.Formatter(
                '{"ts":"%(asctime)s","svc":"%(name)s","lvl":"%(levelname)s"'
                ',"msg":"%(message)s","source":"rt_deferred"}'
            )
        )
        root.handlers = [self._rt_log_handler]
        self._logging_patched = True
        logging.getLogger("redtv.guard").info(
            "RealTimeGuard: logging patch installed (main thread)"
        )

    def _patch_open(self) -> None:
        _orig = builtins.open
        guard = self

        def _guarded_open(*args, **kwargs):
            guard.assert_not_rt("builtins.open")
            return _orig(*args, **kwargs)

        builtins.open = _guarded_open  # type: ignore[assignment]
        self._open_patched = True
        logging.getLogger("redtv.guard").warning(
            "RealTimeGuard: builtins.open patched"
        )

    def _patch_subprocess(self) -> None:
        import subprocess
        _orig = subprocess.Popen
        guard = self

        class _GuardedPopen(_orig):  # type: ignore[misc]
            def __init__(self_, *args, **kwargs):
                guard.assert_not_rt("subprocess.Popen")
                super().__init__(*args, **kwargs)

        subprocess.Popen = _GuardedPopen  # type: ignore[misc]
        self._subprocess_patched = True
        logging.getLogger("redtv.guard").warning(
            "RealTimeGuard: subprocess.Popen patched"
        )


# ---------------------------------------------------------------------------
# @rt_forbidden decorator
# ---------------------------------------------------------------------------

def rt_forbidden(label: str = "", guard_attr: str = "_guard"):
    """
    Decorator that fires a guard violation if the decorated function is ever
    called from the RT playout thread.
    """
    def decorator(fn):
        import functools
        lbl = label or fn.__qualname__

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            guard: Optional[RealTimeGuard] = None
            if args:
                guard = getattr(args[0], guard_attr, None)
            if guard is None:
                guard = _GLOBAL_GUARD
            if guard is not None:
                guard.assert_not_rt(lbl)
            return fn(*args, **kwargs)

        return wrapper
    return decorator


_GLOBAL_GUARD: Optional[RealTimeGuard] = None


def set_global_guard(g: RealTimeGuard) -> None:
    global _GLOBAL_GUARD
    _GLOBAL_GUARD = g
