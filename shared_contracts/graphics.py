"""
shared_contracts.graphics  (v4.0)
===================================
Data contracts for the graphics/CG control plane.

Design principles
-----------------
- All state is in-memory in api_gateway (not in playout_core).
- playout_core NEVER imports from this module in RT paths.
- These contracts are consumed by:
    api_gateway (store + serve state)
    operator_ui (render controls)
    future renderers (DeckLink overlay, NDI title, HTML CG engine)

BugGraphic    — persistent channel bug / watermark
LowerThird    — animated lower-third title card
GraphicsState — combined snapshot served at GET /api/graphics/state
"""

from __future__ import annotations

import time
from enum import Enum
from typing import Optional


class BugPosition(str, Enum):
    TOP_LEFT     = "top_left"
    TOP_RIGHT    = "top_right"
    BOTTOM_LEFT  = "bottom_left"
    BOTTOM_RIGHT = "bottom_right"


class LowerThirdStyle(str, Enum):
    STANDARD = "standard"   # RED TV default
    BREAKING = "breaking"   # Red band breaking news
    SPORT    = "sport"      # Sports score style
    MINIMAL  = "minimal"    # Text only


class AnimateDir(str, Enum):
    LEFT   = "left"
    RIGHT  = "right"
    UP     = "up"
    DOWN   = "down"
    FADE   = "fade"
    NONE   = "none"


class BugGraphic:
    """
    Channel bug / watermark overlay.

    Rendering contract:
      Any renderer consuming GraphicsState should display this in the
      specified corner at the given opacity when enabled=True.
      text is optional (e.g. "LIVE" badge next to logo).
    """
    def __init__(
        self,
        enabled:      bool        = False,
        text:         Optional[str] = None,
        position:     BugPosition = BugPosition.BOTTOM_RIGHT,
        opacity:      float        = 0.85,
        safe_margins: bool         = True,   # respect 4% safe area
    ) -> None:
        self.enabled      = enabled
        self.text         = text
        self.position     = position
        self.opacity      = max(0.0, min(1.0, opacity))
        self.safe_margins = safe_margins

    def to_dict(self) -> dict:
        return {
            "enabled":      self.enabled,
            "text":         self.text,
            "position":     self.position.value,
            "opacity":      self.opacity,
            "safe_margins": self.safe_margins,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "BugGraphic":
        return cls(
            enabled      = bool(d.get("enabled", False)),
            text         = d.get("text"),
            position     = BugPosition(d.get("position", "bottom_right")),
            opacity      = float(d.get("opacity", 0.85)),
            safe_margins = bool(d.get("safe_margins", True)),
        )


class LowerThird:
    """
    Lower-third title card.

    Rendering contract:
      Show when enabled=True. After duration_s seconds, auto-clear
      (renderer responsibility). animate_in/out hint the transition.
      headline is required; subline is optional.
    """
    def __init__(
        self,
        enabled:     bool            = False,
        headline:    str             = "",
        subline:     Optional[str]   = None,
        style:       LowerThirdStyle = LowerThirdStyle.STANDARD,
        duration_s:  float           = 8.0,
        animate_in:  AnimateDir      = AnimateDir.LEFT,
        animate_out: AnimateDir      = AnimateDir.LEFT,
        taken_at:    float           = 0.0,   # time.time() when TAKE was pressed
    ) -> None:
        self.enabled    = enabled
        self.headline   = headline
        self.subline    = subline
        self.style      = style
        self.duration_s = max(1.0, duration_s)
        self.animate_in  = animate_in
        self.animate_out = animate_out
        self.taken_at   = taken_at

    @property
    def expired(self) -> bool:
        if not self.enabled or self.taken_at == 0.0:
            return False
        return (time.time() - self.taken_at) >= self.duration_s

    def to_dict(self) -> dict:
        return {
            "enabled":     self.enabled,
            "headline":    self.headline,
            "subline":     self.subline,
            "style":       self.style.value,
            "duration_s":  self.duration_s,
            "animate_in":  self.animate_in.value,
            "animate_out": self.animate_out.value,
            "taken_at":    self.taken_at,
            "expired":     self.expired,
            "remaining_s": max(0.0, round(self.duration_s - (time.time() - self.taken_at), 1))
                           if self.enabled and self.taken_at else None,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "LowerThird":
        return cls(
            enabled     = bool(d.get("enabled", False)),
            headline    = d.get("headline", ""),
            subline     = d.get("subline"),
            style       = LowerThirdStyle(d.get("style", "standard")),
            duration_s  = float(d.get("duration_s", 8.0)),
            animate_in  = AnimateDir(d.get("animate_in", "left")),
            animate_out = AnimateDir(d.get("animate_out", "left")),
            taken_at    = float(d.get("taken_at", 0.0)),
        )


class GraphicsState:
    """
    Combined graphics state snapshot.
    Served at GET /api/graphics/state and included in /status.
    Thread-safe via GraphicsController lock.
    """
    def __init__(self) -> None:
        self.bug:          BugGraphic = BugGraphic()
        self.lower_third:  LowerThird = LowerThird()
        self.last_update:  float      = 0.0
        self.update_count: int        = 0

    def touch(self) -> None:
        self.last_update  = time.time()
        self.update_count += 1

    def to_dict(self) -> dict:
        return {
            "bug":          self.bug.to_dict(),
            "lower_third":  self.lower_third.to_dict(),
            "last_update":  self.last_update,
            "update_count": self.update_count,
        }
