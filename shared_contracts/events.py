"""
shared_contracts.events
=======================
Structured broadcast event emitted by every service on every state change.
Events flow over the internal event bus and are streamed to clients via
WebSocket /events.
"""

from __future__ import annotations

import time
import uuid
from enum import Enum
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field


class Severity(str, Enum):
    DEBUG   = "debug"
    INFO    = "info"
    WARNING = "warning"
    ERROR   = "error"
    CRITICAL = "critical"


class BroadcastEvent(BaseModel):
    event_id:        str           = Field(default_factory=lambda: str(uuid.uuid4()))
    timestamp:       float         = Field(default_factory=time.time)
    timestamp_iso:   Optional[str] = None         # filled by emitter
    service:         str           = "unknown"    # playout_core | media_factory | ...
    state:           Optional[str] = None         # current FSM state
    current_item:    Optional[str] = None         # slot_id or title
    next_item:       Optional[str] = None
    timeline_offset: Optional[float] = None       # seconds since playlist start
    warning:         Optional[str] = None
    severity:        Severity      = Severity.INFO
    extra:           Dict[str, Any] = Field(default_factory=dict)

    def to_json_log(self) -> str:
        """Return a compact JSON string for structured log emission."""
        import json
        return json.dumps(self.model_dump(), default=str)
