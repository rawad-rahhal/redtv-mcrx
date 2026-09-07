"""
playout_core.sources.automation_source  (v3.3)
================================================
AutomationSourceAdapter — thin wrapper around DoubleBufferDecoder.

The automation source IS the existing playlist/engine decoder machinery.
This adapter simply exposes the SourceAdapter interface over it, so
ProgramRouter can treat it identically to LiveSourceAdapter.

RT contract: get_frame() / get_hold_frame() / is_ready() are direct
delegates to the decoder's RT-safe methods — zero-I/O preserved.
"""

from __future__ import annotations

from typing import Optional

from playout_core.decoder import DoubleBufferDecoder, VideoFrame
from playout_core.sources.base import SourceAdapter, SourceState


class AutomationSourceAdapter(SourceAdapter):
    """
    Wraps the existing DoubleBufferDecoder as a SourceAdapter.
    No new logic — just the interface bridge.
    """

    def __init__(self, decoder: DoubleBufferDecoder) -> None:
        self._decoder = decoder

    # ── RT interface ───────────────────────────────────────────────────

    def get_frame(self) -> Optional[VideoFrame]:
        return self._decoder.get_current_frame()

    def get_hold_frame(self) -> Optional[VideoFrame]:
        return self._decoder.get_hold_frame()

    def is_ready(self) -> bool:
        return self._decoder.is_swap_ready()

    # ── Control interface ─────────────────────────────────────────────

    def start(self) -> None:
        pass   # decoder is managed by PlayoutEngine lifecycle

    def stop(self) -> None:
        pass   # same

    def get_status(self) -> dict:
        return {
            "source":       "automation",
            "state":        SourceState.ACTIVE.value,
            "buffer_depth": self._decoder.current_buffer_depth(),
            "swap_ready":   self._decoder.is_swap_ready(),
            "current_item": getattr(self._decoder.current_item(), "title", None),
        }
