"""playout_core.sources — SourceAdapter interface and implementations."""
from playout_core.sources.base import SourceAdapter, SourceState
from playout_core.sources.automation_source import AutomationSourceAdapter
from playout_core.sources.live_source import LiveSourceAdapter, LiveMode

__all__ = [
    "SourceAdapter", "SourceState",
    "AutomationSourceAdapter",
    "LiveSourceAdapter", "LiveMode",
]
