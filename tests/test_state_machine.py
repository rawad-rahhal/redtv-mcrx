"""tests/test_state_machine.py  (v2.1) — PlayoutFSM with ring buffer tests."""

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from playout_core.state_machine import PlayoutFSM, PlayoutState
from shared_contracts.events import BroadcastEvent, Severity
from shared_contracts.rt_ring import RTRingBuffer, RTEventKind


def make_fsm():
    ring   = RTRingBuffer(capacity=64)
    events = []
    fsm    = PlayoutFSM(ring=ring, event_cb=lambda e: events.append(e))
    return fsm, events, ring


def test_initial_state():
    fsm, _, _ = make_fsm()
    assert fsm.state == PlayoutState.IDLE


def test_idle_to_loading():
    fsm, events, _ = make_fsm()
    ok = fsm.transition(PlayoutState.LOADING, current_item="Clip A")
    assert ok
    assert fsm.state == PlayoutState.LOADING
    assert len(events) == 1
    assert events[0].current_item == "Clip A"


def test_full_happy_path():
    fsm, _, _ = make_fsm()
    fsm.transition(PlayoutState.LOADING)
    fsm.transition(PlayoutState.PRE_ROLL)
    ok = fsm.transition(PlayoutState.PLAYING)
    assert ok
    assert fsm.state == PlayoutState.PLAYING


def test_invalid_transition_returns_false():
    fsm, _, _ = make_fsm()
    ok = fsm.transition(PlayoutState.PLAYING)   # IDLE → PLAYING invalid
    assert not ok
    assert fsm.state == PlayoutState.IDLE


def test_state_change_pushed_to_ring():
    fsm, _, ring = make_fsm()
    fsm.transition(PlayoutState.LOADING)
    events = ring.drain()
    assert any(e.kind == RTEventKind.STATE_CHANGE for e in events)


def test_hold_count_increments():
    fsm, _, _ = make_fsm()
    fsm.transition(PlayoutState.LOADING)
    fsm.transition(PlayoutState.PRE_ROLL)
    fsm.transition(PlayoutState.PLAYING)
    fsm.transition(PlayoutState.HOLD_FRAME)
    assert fsm.hold_count == 1
    fsm.transition(PlayoutState.PLAYING)
    fsm.transition(PlayoutState.HOLD_FRAME)
    assert fsm.hold_count == 2


def test_emergency_count_increments():
    fsm, _, _ = make_fsm()
    fsm.transition(PlayoutState.LOADING)
    fsm.transition(PlayoutState.PRE_ROLL)
    fsm.transition(PlayoutState.PLAYING)
    fsm.transition(PlayoutState.EMERGENCY)
    assert fsm.emergency_count == 1


def test_shutdown_terminal():
    fsm, _, _ = make_fsm()
    fsm.transition(PlayoutState.LOADING)
    fsm.transition(PlayoutState.PRE_ROLL)
    fsm.transition(PlayoutState.PLAYING)
    fsm.transition(PlayoutState.SHUTDOWN)
    ok = fsm.transition(PlayoutState.IDLE)
    assert not ok
    assert fsm.state == PlayoutState.SHUTDOWN


def test_state_history_recorded():
    fsm, _, _ = make_fsm()
    fsm.transition(PlayoutState.LOADING)
    fsm.transition(PlayoutState.PRE_ROLL)
    fsm.transition(PlayoutState.PLAYING)
    history = fsm.state_history
    assert "LOADING"  in history
    assert "PRE_ROLL" in history
    assert "PLAYING"  in history


def test_state_age_increases():
    fsm, _, _ = make_fsm()
    fsm.transition(PlayoutState.LOADING)
    time.sleep(0.05)
    assert fsm.state_age_seconds >= 0.04


def test_event_carries_warning():
    fsm, events, _ = make_fsm()
    fsm.transition(PlayoutState.LOADING)
    fsm.transition(PlayoutState.PRE_ROLL)
    fsm.transition(PlayoutState.PLAYING)
    fsm.transition(PlayoutState.HOLD_FRAME,
                   warning="Buffer underrun", severity=Severity.WARNING)
    warn_events = [e for e in events if e.warning]
    assert any("underrun" in (e.warning or "") for e in warn_events)
