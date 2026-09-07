"""tests/test_models.py — Pydantic contract model tests."""

import pytest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared_contracts.models import (
    Playlist, PlaylistItem, MediaMetadata, VideoSpec, AudioSpec,
    SlotType, ScanType, FieldOrder, NormalizationState,
)


def make_item(**overrides):
    defaults = dict(
        slot_type  = SlotType.PROGRAM,
        media_path = "//NAS/media/test.mp4",
        title      = "Test Clip",
    )
    defaults.update(overrides)
    return PlaylistItem(**defaults)


def test_valid_playlist():
    pl = Playlist(items=[make_item()])
    assert len(pl.items) == 1
    assert pl.channel == "REDTV"


def test_playlist_requires_items():
    with pytest.raises(Exception):
        Playlist(items=[])


def test_program_requires_media_path():
    with pytest.raises(Exception):
        PlaylistItem(slot_type=SlotType.PROGRAM, title="No path")


def test_live_in_no_path_required():
    # LIVE_IN slots don't need a media_path
    item = PlaylistItem(slot_type=SlotType.LIVE_IN, title="Live Feed")
    assert item.slot_type == SlotType.LIVE_IN


def test_valid_media_metadata():
    meta = MediaMetadata(
        title             = "Test",
        original_filename = "test.mp4",
        normalized_path   = "//NAS/media/test_norm.mp4",
        duration_seconds  = 120.5,
        video = VideoSpec(
            width=1920, height=1080, frame_rate="25",
            scan_type=ScanType.INTERLACED, field_order=FieldOrder.TFF
        ),
        audio = AudioSpec(),
    )
    assert meta.video.scan_type == ScanType.INTERLACED
    assert meta.audio.sample_rate == 48000


def test_metadata_invalid_duration():
    with pytest.raises(Exception):
        MediaMetadata(
            title="x", original_filename="x.mp4", normalized_path="/x.mp4",
            duration_seconds=-1.0,
            video=VideoSpec(width=1920, height=1080),
            audio=AudioSpec(),
        )


def test_video_spec_frame_rate_validation():
    v = VideoSpec(width=1920, height=1080, frame_rate="25000/1001")
    assert v.frame_rate == "25000/1001"
    with pytest.raises(Exception):
        VideoSpec(width=1920, height=1080, frame_rate="not/valid/rate")


def test_playlist_item_ai_confidence_range():
    item = make_item(ai_confidence=0.95)
    assert item.ai_confidence == 0.95
    with pytest.raises(Exception):
        make_item(ai_confidence=1.5)   # > 1.0 should fail
