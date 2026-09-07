"""tests/test_rt_ring.py — RTRingBuffer unit tests."""

import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from shared_contracts.rt_ring import RTRingBuffer, RTEventKind


def test_basic_push_drain():
    ring = RTRingBuffer(capacity=16)
    ring.push(kind=RTEventKind.FRAME_OUT, frame_number=1, severity=1)
    ring.push(kind=RTEventKind.DRIFT_WARN, drift_ms=25.5, severity=2)
    events = ring.drain()
    assert len(events) == 2
    assert events[0].kind == RTEventKind.FRAME_OUT
    assert events[0].frame_number == 1
    assert events[1].kind == RTEventKind.DRIFT_WARN
    assert abs(events[1].drift_ms - 25.5) < 0.001


def test_drain_returns_copies():
    ring = RTRingBuffer(capacity=16)
    ring.push(kind=RTEventKind.FRAME_DROP, detail=42)
    events = ring.drain()
    assert events[0].detail == 42
    # After drain, another drain returns nothing
    events2 = ring.drain()
    assert len(events2) == 0


def test_ring_overflow_drops_newest():
    ring = RTRingBuffer(capacity=4)   # tiny ring
    for i in range(6):
        ring.push(kind=RTEventKind.FRAME_OUT, frame_number=i)
    # The producer must never overwrite unread slots or advance the consumer index.
    assert ring.dropped_count == 2
    events = ring.drain()
    assert [event.frame_number for event in events] == [0, 1, 2, 3]


def test_has_data():
    ring = RTRingBuffer(capacity=16)
    assert not ring.has_data
    ring.push(kind=RTEventKind.HEARTBEAT)
    assert ring.has_data
    ring.drain()
    assert not ring.has_data


def test_capacity_must_be_power_of_2():
    with pytest.raises(AssertionError):
        RTRingBuffer(capacity=7)
    # These should be fine
    RTRingBuffer(capacity=1)
    RTRingBuffer(capacity=256)


def test_concurrent_producer_consumer():
    """Simulate RT producer + control consumer."""
    ring    = RTRingBuffer(capacity=256)
    results = []
    stop    = threading.Event()

    def producer():
        for i in range(500):
            ring.push(kind=RTEventKind.FRAME_OUT, frame_number=i)
            time.sleep(0.0001)
        stop.set()

    def consumer():
        total = 0
        while not stop.is_set() or ring.has_data:
            evts = ring.drain()
            total += len(evts)
            time.sleep(0.001)
        results.append(total)

    t_prod = threading.Thread(target=producer)
    t_cons = threading.Thread(target=consumer)
    t_cons.start()
    t_prod.start()
    t_prod.join()
    t_cons.join()

    # Every push is either delivered exactly once or explicitly dropped.
    received = results[0]
    dropped  = ring.dropped_count
    assert received + dropped == 500


def test_pending_count():
    ring = RTRingBuffer(capacity=32)
    assert ring.pending_count == 0
    ring.push(kind=RTEventKind.GUARD_VIOLATION)
    ring.push(kind=RTEventKind.GUARD_VIOLATION)
    assert ring.pending_count == 2
    ring.drain()
    assert ring.pending_count == 0


def test_all_fields_preserved():
    ring = RTRingBuffer(capacity=16)
    ring.push(
        kind         = RTEventKind.STATE_CHANGE,
        frame_number = 999,
        drift_ms     = -3.14,
        severity     = 3,
        state_value  = 4,
        slot_index   = 7,
        detail       = 12345,
    )
    evt = ring.drain()[0]
    assert evt.kind         == RTEventKind.STATE_CHANGE
    assert evt.frame_number == 999
    assert abs(evt.drift_ms - (-3.14)) < 0.001
    assert evt.severity     == 3
    assert evt.state_value  == 4
    assert evt.slot_index   == 7
    assert evt.detail       == 12345


def test_overflow_concurrency_never_duplicates_or_tears_events():
    """Regression for producer-owned _read race under sustained overflow.

    frame_number, drift_ms and detail encode the same sequence id. Delivered
    events must be unique and field-coherent even while the producer outruns
    the consumer and the ring reports drops.
    """
    total_pushes = 100_000
    ring = RTRingBuffer(capacity=64)
    delivered = []
    producer_done = threading.Event()

    def producer():
        for i in range(total_pushes):
            ring.push(
                kind=RTEventKind.DRIFT_ERROR,
                frame_number=i,
                drift_ms=i + 0.25,
                detail=i ^ 0x5A5A,
            )
        producer_done.set()

    def consumer():
        while not producer_done.is_set() or ring.has_data:
            delivered.extend(ring.drain())
            # Intentionally let the producer outrun the drain so overflow occurs.
            time.sleep(0.0002)

    t_cons = threading.Thread(target=consumer)
    t_prod = threading.Thread(target=producer)
    t_cons.start()
    t_prod.start()
    t_prod.join()
    t_cons.join()

    assert ring.dropped_count > 0
    assert len(delivered) + ring.dropped_count == total_pushes

    frame_numbers = [event.frame_number for event in delivered]
    assert len(frame_numbers) == len(set(frame_numbers))
    for event in delivered:
        assert event.drift_ms == event.frame_number + 0.25
        assert event.detail == (event.frame_number ^ 0x5A5A)
