"""
tests/test_graphics.py  (v4.0)
================================
Tests for the graphics/CG control plane:
  - BugGraphic data model
  - LowerThird data model + expiry
  - GraphicsState snapshot
  - Config validator
"""

from __future__ import annotations

import sys
import time
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# ── Minimal stubs ──────────────────────────────────────────────────────────
import pytest  # may be stubbed

# ── Imports under test ─────────────────────────────────────────────────────
from shared_contracts.graphics import (
    BugGraphic, BugPosition,
    LowerThird, LowerThirdStyle, AnimateDir,
    GraphicsState,
)
from shared_contracts.config_validator import validate_config


# ═══════════════════════════════════════════════════════════════════════════
# BugGraphic tests
# ═══════════════════════════════════════════════════════════════════════════

def test_bug_default_state():
    bug = BugGraphic()
    assert bug.enabled      is False
    assert bug.text         is None
    assert bug.position     == BugPosition.BOTTOM_RIGHT
    assert 0.0 <= bug.opacity <= 1.0


def test_bug_opacity_clamped():
    bug = BugGraphic(opacity=1.5)
    assert bug.opacity == 1.0
    bug2 = BugGraphic(opacity=-0.5)
    assert bug2.opacity == 0.0


def test_bug_to_dict():
    bug = BugGraphic(enabled=True, text="LIVE", position=BugPosition.TOP_LEFT, opacity=0.9)
    d   = bug.to_dict()
    assert d["enabled"]  is True
    assert d["text"]     == "LIVE"
    assert d["position"] == "top_left"
    assert d["opacity"]  == 0.9


def test_bug_from_dict():
    d   = {"enabled": True, "text": "BREAKING", "position": "top_right", "opacity": 0.75}
    bug = BugGraphic.from_dict(d)
    assert bug.enabled      is True
    assert bug.text         == "BREAKING"
    assert bug.position     == BugPosition.TOP_RIGHT
    assert bug.opacity      == 0.75


def test_bug_roundtrip():
    original = BugGraphic(enabled=True, text="LIVE", position=BugPosition.BOTTOM_LEFT, opacity=0.6)
    restored = BugGraphic.from_dict(original.to_dict())
    assert restored.enabled  == original.enabled
    assert restored.text     == original.text
    assert restored.position == original.position
    assert restored.opacity  == original.opacity


# ═══════════════════════════════════════════════════════════════════════════
# LowerThird tests
# ═══════════════════════════════════════════════════════════════════════════

def test_l3_default_state():
    l3 = LowerThird()
    assert l3.enabled    is False
    assert l3.headline   == ""
    assert l3.expired    is False


def test_l3_duration_minimum():
    l3 = LowerThird(duration_s=0.0)
    assert l3.duration_s >= 1.0, "duration must be at least 1s"


def test_l3_not_expired_when_disabled():
    l3 = LowerThird(enabled=False, taken_at=time.time() - 999)
    assert l3.expired is False


def test_l3_expires_after_duration():
    l3 = LowerThird(
        enabled    = True,
        headline   = "Test",
        duration_s = 1.0,
        taken_at   = time.time() - 2.0,   # 2s ago, duration=1s → expired
    )
    assert l3.expired is True


def test_l3_not_expired_within_duration():
    l3 = LowerThird(
        enabled    = True,
        headline   = "Test",
        duration_s = 60.0,
        taken_at   = time.time() - 1.0,   # 1s ago, duration=60s
    )
    assert l3.expired is False


def test_l3_to_dict():
    l3 = LowerThird(
        enabled    = True,
        headline   = "Breaking News",
        subline    = "From our correspondent",
        style      = LowerThirdStyle.BREAKING,
        duration_s = 10.0,
    )
    d = l3.to_dict()
    assert d["enabled"]   is True
    assert d["headline"]  == "Breaking News"
    assert d["subline"]   == "From our correspondent"
    assert d["style"]     == "breaking"
    assert d["duration_s"] == 10.0
    assert "remaining_s" in d
    assert "expired"     in d


def test_l3_from_dict():
    d = {
        "enabled": True, "headline": "Sport Update", "subline": None,
        "style": "sport", "duration_s": 5.0, "animate_in": "up",
        "animate_out": "fade", "taken_at": 0.0,
    }
    l3 = LowerThird.from_dict(d)
    assert l3.headline   == "Sport Update"
    assert l3.style      == LowerThirdStyle.SPORT
    assert l3.animate_in == AnimateDir.UP


def test_l3_roundtrip():
    original = LowerThird(
        enabled=True, headline="Test Headline", subline="Test Sub",
        style=LowerThirdStyle.MINIMAL, duration_s=15.0,
        animate_in=AnimateDir.RIGHT, animate_out=AnimateDir.FADE,
        taken_at=time.time(),
    )
    restored = LowerThird.from_dict(original.to_dict())
    assert restored.headline   == original.headline
    assert restored.subline    == original.subline
    assert restored.style      == original.style
    assert restored.duration_s == original.duration_s


# ═══════════════════════════════════════════════════════════════════════════
# GraphicsState tests
# ═══════════════════════════════════════════════════════════════════════════

def test_graphics_state_default():
    gs = GraphicsState()
    assert gs.bug.enabled         is False
    assert gs.lower_third.enabled is False
    assert gs.update_count        == 0


def test_graphics_state_touch():
    gs = GraphicsState()
    gs.touch()
    assert gs.update_count  == 1
    assert gs.last_update   > 0


def test_graphics_state_to_dict():
    gs = GraphicsState()
    gs.bug.enabled  = True
    gs.bug.text     = "LIVE"
    gs.touch()
    d = gs.to_dict()
    assert "bug"          in d
    assert "lower_third"  in d
    assert "last_update"  in d
    assert "update_count" in d
    assert d["bug"]["enabled"] is True


def test_graphics_state_multiple_updates():
    gs = GraphicsState()
    for _ in range(5):
        gs.touch()
    assert gs.update_count == 5


# ═══════════════════════════════════════════════════════════════════════════
# Config validator tests
# ═══════════════════════════════════════════════════════════════════════════

def test_config_validator_good():
    """Valid minimal config produces no issues."""
    cfg = {
        "channel":     {"name": "REDTV", "standard": "1080i25"},
        "paths":       {"nas_root": "//NAS-SERVER/CHANNEL"},
        "playout":     {"frame_interval_ms": 40, "drift_warn_ms": 20, "drift_error_ms": 100},
        "api_gateway": {"port": 8000},
        "live":        {"refuse_switch_if_not_ready": True},
        "logging":     {"level": "INFO"},
    }
    issues = validate_config(cfg)
    assert issues == [], f"Unexpected issues: {issues}"


def test_config_validator_missing_section():
    """Missing required section produces a warning."""
    issues = validate_config({"channel": {}, "paths": {}, "playout": {}, "api_gateway": {}})
    assert any("live" in i for i in issues), f"Expected missing live warning, got: {issues}"


def test_config_validator_drift_illogical():
    """drift_error < drift_warn is flagged."""
    cfg = {
        "channel": {}, "paths": {}, "api_gateway": {"port": 8000},
        "live": {"refuse_switch_if_not_ready": True},
        "playout": {
            "frame_interval_ms": 40,
            "drift_warn_ms":  100,
            "drift_error_ms":  10,   # less than warn — wrong
        },
    }
    issues = validate_config(cfg)
    assert any("drift" in i.lower() for i in issues)


def test_config_validator_unsafe_switch():
    """refuse_switch_if_not_ready=false is flagged as unsafe."""
    cfg = {
        "channel": {}, "paths": {}, "api_gateway": {"port": 8000},
        "playout": {"frame_interval_ms": 40, "drift_warn_ms": 20, "drift_error_ms": 100},
        "live": {"refuse_switch_if_not_ready": False},
    }
    issues = validate_config(cfg)
    assert any("unsafe" in i.lower() or "refuse" in i.lower() for i in issues)


def test_config_validator_privileged_port():
    cfg = {
        "channel": {}, "paths": {},
        "playout": {"frame_interval_ms": 40, "drift_warn_ms": 20, "drift_error_ms": 100},
        "live": {"refuse_switch_if_not_ready": True},
        "api_gateway": {"port": 80},
    }
    issues = validate_config(cfg)
    assert any("1024" in i for i in issues)
