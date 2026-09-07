"""shared_contracts v3 — contract models, events, guard, and RT ring buffer."""
from shared_contracts.models  import PlaylistItem, Playlist, MediaMetadata, VideoSpec, AudioSpec
from shared_contracts.events  import BroadcastEvent, Severity
from shared_contracts.guard   import RealTimeGuard, GuardConfig, rt_forbidden, RTViolationError
from shared_contracts.rt_ring import RTRingBuffer, RTEvent, RTEventKind

__all__ = [
    "PlaylistItem", "Playlist", "MediaMetadata", "VideoSpec", "AudioSpec",
    "BroadcastEvent", "Severity",
    "RealTimeGuard", "GuardConfig", "rt_forbidden", "RTViolationError",
    "RTRingBuffer", "RTEvent", "RTEventKind",
]

from .graphics import (  # noqa: F401
    BugGraphic, BugPosition,
    LowerThird, LowerThirdStyle, AnimateDir,
    GraphicsState,
)
from .config_validator import validate_config  # noqa: F401

from .preflight import preflight_playlist, GuardReport
