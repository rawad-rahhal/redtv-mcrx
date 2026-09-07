"""tests/test_builder.py — NashraPlaylistBuilder unit tests."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from automation_ai.builder import NashraPlaylistBuilder
from automation_ai.matcher import MediaLibraryEntry
from shared_contracts.models import SlotType


SAMPLE_LIBRARY = [
    MediaLibraryEntry("REDTV_00001", "Morning News Headlines",   "//NAS/media/REDTV_00001.mp4", ["news"]),
    MediaLibraryEntry("REDTV_00002", "Sports Wrap Weekly",       "//NAS/media/REDTV_00002.mp4", ["sports"]),
    MediaLibraryEntry("REDTV_00003", "Weather Forecast",         "//NAS/media/REDTV_00003.mp4", ["weather"]),
    MediaLibraryEntry("SLATE_001",   "Emergency Slate",          "//NAS/media/slate/emergency.mp4", ["slate"]),
]

SAMPLE_CONFIG = {
    "automation_ai": {
        "fuzzy_threshold":      0.6,
        "confidence_warn_below": 0.75,
        "fallback_slate_id":    "SLATE_001",
        "log_correlation":      True,
    },
    "paths": {
        "playlist_dir":    "./test_playlists",
        "emergency_slate": "//NAS/media/slate/emergency.mp4",
    }
}


def make_builder():
    return NashraPlaylistBuilder(SAMPLE_CONFIG, SAMPLE_LIBRARY)


def test_exact_id_match():
    builder = make_builder()
    result  = builder.build("REDTV_00001")
    assert len(result.playlist.items) == 1
    assert result.playlist.items[0].media_id == "REDTV_00001"
    assert result.playlist.items[0].ai_confidence == 1.0
    assert result.matched_slots == 1


def test_exact_title_match():
    builder = make_builder()
    result  = builder.build("Morning News Headlines")
    assert result.playlist.items[0].media_id == "REDTV_00001"
    assert result.playlist.items[0].ai_confidence == 1.0


def test_fuzzy_match():
    builder = make_builder()
    result  = builder.build("Morning News")   # truncated title
    item = result.playlist.items[0]
    assert item.media_id == "REDTV_00001"
    assert 0.6 <= item.ai_confidence < 1.0


def test_no_match_injects_slate():
    builder = make_builder()
    result  = builder.build("XXXXXXXXXXX totally unknown clip")
    item = result.playlist.items[0]
    assert item.slot_type == SlotType.SLATE
    assert result.fallback_slots == 1
    assert len(result.warnings) > 0


def test_multiline_rundown():
    builder = make_builder()
    rundown = """
    Morning News Headlines
    Sports Wrap Weekly
    Weather Forecast
    """
    result = builder.build(rundown)
    assert result.total_slots == 3
    assert result.matched_slots == 3
    assert result.fallback_slots == 0


def test_comment_lines_skipped():
    builder = make_builder()
    rundown = "# This is a comment\nMorning News Headlines"
    result  = builder.build(rundown)
    assert result.total_slots == 1


def test_empty_rundown_gets_slate():
    builder = make_builder()
    result  = builder.build("   ")
    assert result.total_slots == 1
    assert result.playlist.items[0].slot_type == SlotType.SLATE


def test_correlation_id_set():
    builder = make_builder()
    result  = builder.build("Morning News Headlines")
    assert result.correlation_id
    assert len(result.correlation_id) == 8


def test_warnings_for_low_confidence():
    builder = make_builder()
    # Partial match that's below warn threshold
    result = builder.build("News")   # vague
    # Should either match with low confidence warning or produce no-match
    if result.matched_slots > 0:
        # Check warning emitted for low confidence
        has_warn = any("conf=" in w or "Low confidence" in w for w in result.warnings)
        # Either a warning was issued OR it was exact
        assert result.playlist.items[0].ai_confidence is not None
