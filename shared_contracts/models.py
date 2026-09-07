"""
shared_contracts.models  (v3 — PRODUCTION HARDENED)
=====================================================
Pydantic v2 contract definitions.

v3 fix
------
FIX: PlaylistItem.program_requires_path now checks media_path.strip()
     so that a whitespace-only string like "  " is rejected, not just
     empty string.  An empty string is falsy in Python so was already
     caught; whitespace was not.
"""

from __future__ import annotations

import uuid
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------

class ScanType(str, Enum):
    INTERLACED  = "interlaced"
    PROGRESSIVE = "progressive"


class FieldOrder(str, Enum):
    TFF         = "tff"
    BFF         = "bff"
    PROGRESSIVE = "progressive"


class SlotType(str, Enum):
    PROGRAM = "program"
    LIVE_IN = "live_in"
    FILLER  = "filler"
    SLATE   = "slate"
    BREAK   = "break"


class NormalizationState(str, Enum):
    PENDING    = "pending"
    RUNNING    = "running"
    DONE       = "done"
    FAILED     = "failed"
    QUARANTINE = "quarantine"


# ---------------------------------------------------------------------------
# VideoSpec / AudioSpec
# ---------------------------------------------------------------------------

class VideoSpec(BaseModel):
    width:        int
    height:       int
    frame_rate:   str         = "25"
    scan_type:    ScanType    = ScanType.INTERLACED
    field_order:  FieldOrder  = FieldOrder.TFF
    color_space:  str         = "bt709"
    pixel_format: str         = "yuv422p"
    bitrate_kbps: Optional[int] = None
    codec:        str         = "h264"

    @field_validator("frame_rate")
    @classmethod
    def validate_frame_rate(cls, v: str) -> str:
        if "/" in v:
            num, den = v.split("/")
            float(num) / float(den)
        else:
            float(v)
        return v


class AudioSpec(BaseModel):
    codec:        str = "aac"
    sample_rate:  int = 48000
    channels:     int = 2
    bitrate_kbps: int = 192
    layout:       str = "stereo"


# ---------------------------------------------------------------------------
# MediaMetadata
# ---------------------------------------------------------------------------

class MediaMetadata(BaseModel):
    media_id:          str   = Field(default_factory=lambda: str(uuid.uuid4()))
    title:             str
    original_filename: str
    normalized_path:   str
    duration_seconds:  float
    video:             VideoSpec
    audio:             AudioSpec
    norm_state:        NormalizationState = NormalizationState.PENDING
    ingest_job_id:     Optional[str] = None
    created_utc:       Optional[str] = None
    tags:              List[str]     = Field(default_factory=list)

    @field_validator("duration_seconds")
    @classmethod
    def positive_duration(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("duration_seconds must be > 0")
        return v


# ---------------------------------------------------------------------------
# PlaylistItem
# ---------------------------------------------------------------------------

class PlaylistItem(BaseModel):
    slot_id:          str           = Field(default_factory=lambda: str(uuid.uuid4()))
    slot_type:        SlotType      = SlotType.PROGRAM
    media_id:         Optional[str] = None
    media_path:       Optional[str] = None
    title:            str           = ""
    scheduled_tc:     Optional[str] = None
    duration_seconds: Optional[float] = None
    trim_in:          float         = 0.0
    trim_out:         Optional[float] = None
    loop:             bool          = False
    auto_next:        bool          = True
    ai_confidence:    Optional[float] = Field(default=None, ge=0.0, le=1.0)
    override_note:    Optional[str] = None

    @model_validator(mode="after")
    def program_requires_path(self) -> "PlaylistItem":
        if self.slot_type == SlotType.PROGRAM:
            # FIX: check strip() to reject whitespace-only paths
            if not (self.media_path and self.media_path.strip()):
                raise ValueError(
                    "PROGRAM slot must have a non-empty media_path"
                )
        return self


# ---------------------------------------------------------------------------
# Playlist
# ---------------------------------------------------------------------------

class Playlist(BaseModel):
    playlist_id:    str           = Field(default_factory=lambda: str(uuid.uuid4()))
    channel:        str           = "REDTV"
    build_date:     Optional[str] = None
    build_by:       str           = "manual"
    items:          List[PlaylistItem]
    warnings:       List[str]     = Field(default_factory=list)
    correlation_id: Optional[str] = None

    @field_validator("items")
    @classmethod
    def at_least_one_item(cls, v: list) -> list:
        if not v:
            raise ValueError("Playlist must contain at least one item")
        return v


# ---------------------------------------------------------------------------
# IngestJob
# ---------------------------------------------------------------------------

class IngestJob(BaseModel):
    job_id:        str   = Field(default_factory=lambda: str(uuid.uuid4()))
    source_path:   str
    target_dir:    str
    preset:        str   = "broadcast_1080i25"
    submitted_utc: Optional[str] = None
    state:         NormalizationState = NormalizationState.PENDING
    error_msg:     Optional[str] = None
    output_path:   Optional[str] = None
