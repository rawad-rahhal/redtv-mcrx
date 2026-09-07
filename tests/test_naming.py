"""tests/test_naming.py — 6-digit sequential media naming tests."""

import os
import sys
import tempfile
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from media_factory.ingest_worker import _next_media_filename


def test_basic_sequence():
    with tempfile.TemporaryDirectory() as tmp:
        counter = os.path.join(tmp, "counter.txt")
        a = _next_media_filename(counter, "mp4")
        b = _next_media_filename(counter, "mp4")
        c = _next_media_filename(counter, "mp4")
        assert a == "000001.mp4"
        assert b == "000002.mp4"
        assert c == "000003.mp4"


def test_six_digits_zero_padded():
    with tempfile.TemporaryDirectory() as tmp:
        counter = os.path.join(tmp, "counter.txt")
        name = _next_media_filename(counter, "mp4")
        assert len(name.split(".")[0]) == 6
        assert name.startswith("0")


def test_extension_preserved():
    with tempfile.TemporaryDirectory() as tmp:
        counter = os.path.join(tmp, "counter.txt")
        assert _next_media_filename(counter, "mxf").endswith(".mxf")
        assert _next_media_filename(counter, "mov").endswith(".mov")


def test_counter_persists_across_calls():
    with tempfile.TemporaryDirectory() as tmp:
        counter = os.path.join(tmp, "counter.txt")
        _next_media_filename(counter, "mp4")
        _next_media_filename(counter, "mp4")
        # Read counter file directly
        val = int(open(counter).read().strip())
        assert val == 2


def test_no_duplicates_concurrent():
    """Two threads must never produce the same filename."""
    with tempfile.TemporaryDirectory() as tmp:
        counter = os.path.join(tmp, "counter.txt")
        results = []
        lock    = threading.Lock()

        def gen(n):
            for _ in range(n):
                name = _next_media_filename(counter, "mp4")
                with lock:
                    results.append(name)

        threads = [threading.Thread(target=gen, args=(10,)) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # No duplicates
        assert len(results) == len(set(results)), "Duplicate filenames detected!"
        # All 40 names should be sequential
        nums = sorted(int(r.split(".")[0]) for r in results)
        assert nums == list(range(1, 41))


def test_counter_starts_from_existing_value():
    with tempfile.TemporaryDirectory() as tmp:
        counter = os.path.join(tmp, "counter.txt")
        # Pre-seed counter at 99
        with open(counter, "w") as f:
            f.write("99")
        name = _next_media_filename(counter, "mp4")
        assert name == "000100.mp4"
