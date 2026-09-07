"""
playout_core.sources.base  (v3.3)
==================================
SourceAdapter — abstract interface for any program source.

RT CONTRACT
-----------
get_frame()       must be zero-I/O, GIL-atomic, no allocation on hot path.
get_hold_frame()  same.
is_ready()        same — reads a single int/enum reference.

All other methods are control-thread only and decorated @rt_forbidden.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import Enum
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from playout_core.decoder import VideoFrame


class SourceState(str, Enum):
    IDLE        = "idle"
    STAGING     = "staging"     # file validation / SRT connecting
    READY       = "ready"       # buffered, can switch to PROGRAM
    ACTIVE      = "active"      # currently on PROGRAM output
    LOST        = "lost"        # EOF or SRT disconnect
    ERROR       = "error"


class SourceAdapter(ABC):
    """
    Abstract base for AutomationSourceAdapter and LiveSourceAdapter.
    
    RT interface (ZERO I/O):
        get_frame()       → VideoFrame | None
        get_hold_frame()  → VideoFrame | None  (last good frame)
        is_ready()        → bool

    Control interface (@rt_forbidden on implementations):
        start()
        stop()
        get_status() → dict
    """

    @abstractmethod
    def get_frame(self) -> Optional["VideoFrame"]:
        """RT thread only — deque.popleft(), GIL-atomic, no I/O."""

    @abstractmethod
    def get_hold_frame(self) -> Optional["VideoFrame"]:
        """RT thread only — reference read, GIL-atomic."""

    @abstractmethod
    def is_ready(self) -> bool:
        """RT thread only — reads int/enum, GIL-atomic."""

    @abstractmethod
    def start(self) -> None:
        """Control thread — start background workers."""

    @abstractmethod
    def stop(self) -> None:
        """Control thread — stop workers cleanly."""

    @abstractmethod
    def get_status(self) -> dict:
        """Control thread — in-memory snapshot for /status."""
