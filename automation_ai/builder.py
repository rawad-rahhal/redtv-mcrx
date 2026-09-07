"""
automation_ai.builder
=====================
NashraPlaylistBuilder
---------------------
Builds a validated Playlist from a rundown text (one title / ID per line).

Features:
  - Numeric ID matching (REDTV_00001)
  - Fuzzy title matching
  - Fallback slate injection when no match found
  - ai_confidence score per slot
  - Warning list per build
  - Log correlation ID
  - Atomic playlist.json write

Usage:
    builder = NashraPlaylistBuilder(config, library)
    result  = builder.build(rundown_text, output_path="playlists/today.json")
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from shared_contracts.models import Playlist, PlaylistItem, SlotType
from automation_ai.matcher import MediaLibraryEntry, MediaMatcher

log = logging.getLogger("redtv.automation_ai.builder")


@dataclass
class BuildResult:
    playlist:       Playlist
    warnings:       List[str] = field(default_factory=list)
    correlation_id: str       = ""
    total_slots:    int       = 0
    matched_slots:  int       = 0
    fallback_slots: int       = 0


class NashraPlaylistBuilder:

    def __init__(self, config: dict, library: List[MediaLibraryEntry]) -> None:
        ai_cfg              = config.get("automation_ai", {})
        self._threshold     = ai_cfg.get("fuzzy_threshold", 0.6)
        self._warn_below    = ai_cfg.get("confidence_warn_below", 0.75)
        self._fallback_id   = ai_cfg.get("fallback_slate_id", "SLATE_001")
        self._log_corr      = ai_cfg.get("log_correlation", True)
        self._paths         = config.get("paths", {})
        self._matcher       = MediaMatcher(library, fuzzy_threshold=self._threshold)

        # Fallback slate entry
        self._slate_entry: Optional[MediaLibraryEntry] = None
        for entry in library:
            if entry.media_id == self._fallback_id:
                self._slate_entry = entry
                break

    def build(
        self,
        rundown_text: str,
        output_path:  Optional[str] = None,
        build_by:     str = "nashra_ai",
    ) -> BuildResult:
        """
        Parse rundown_text (one entry per line) and build a Playlist.
        """
        correlation_id = str(uuid.uuid4())[:8] if self._log_corr else ""
        log.info("NashraBuilder: starting build (corr=%s)", correlation_id,
                 extra={"service": "automation_ai", "correlation_id": correlation_id})

        lines    = [l.strip() for l in rundown_text.splitlines() if l.strip()]
        items:  List[PlaylistItem] = []
        warnings: List[str]       = []
        fallback_count = 0
        matched_count  = 0

        for line in lines:
            if line.startswith("#"):   # comment
                continue

            result = self._matcher.match(line)

            if result.match_type == "none":
                warnings.append(result.warning or f"No match: {line}")
                item = self._make_fallback_slate(line)
                fallback_count += 1
                log.warning("No match for '%s' — injecting fallback slate", line,
                            extra={"service": "automation_ai"})
            else:
                if result.warning:
                    warnings.append(result.warning)
                if result.confidence < self._warn_below:
                    warnings.append(
                        f"Low confidence match: '{line}' -> '{result.title}' "
                        f"(conf={result.confidence:.2f})"
                    )
                item = PlaylistItem(
                    slot_type     = SlotType.PROGRAM,
                    media_id      = result.media_id,
                    media_path    = result.media_path or "",
                    title         = result.title,
                    ai_confidence = result.confidence,
                )
                matched_count += 1

            items.append(item)

        if not items:
            # Empty rundown — inject single slate
            items.append(self._make_fallback_slate("empty_rundown"))
            warnings.append("Empty rundown — single emergency slate injected")
            fallback_count += 1

        playlist = Playlist(
            channel        = "REDTV",
            build_date     = datetime.date.today().isoformat(),
            build_by       = build_by,
            items          = items,
            warnings       = warnings,
            correlation_id = correlation_id,
        )

        if output_path:
            self._save_atomic(playlist, output_path)

        result_obj = BuildResult(
            playlist       = playlist,
            warnings       = warnings,
            correlation_id = correlation_id,
            total_slots    = len(items),
            matched_slots  = matched_count,
            fallback_slots = fallback_count,
        )

        log.info(
            "NashraBuilder: done  total=%d matched=%d fallback=%d warnings=%d (corr=%s)",
            result_obj.total_slots, result_obj.matched_slots,
            result_obj.fallback_slots, len(warnings), correlation_id,
            extra={"service": "automation_ai", "correlation_id": correlation_id},
        )
        return result_obj

    def _make_fallback_slate(self, label: str) -> PlaylistItem:
        if self._slate_entry:
            return PlaylistItem(
                slot_type     = SlotType.SLATE,
                media_id      = self._slate_entry.media_id,
                media_path    = self._slate_entry.media_path,
                title         = f"SLATE [{label}]",
                ai_confidence = 0.0,
                override_note = f"Fallback for: {label}",
            )
        # Absolute fallback — no media path (playout will use emergency slate)
        return PlaylistItem(
            slot_type     = SlotType.SLATE,
            media_id      = None,
            media_path    = self._paths.get("emergency_slate", ""),
            title         = f"EMERGENCY SLATE [{label}]",
            ai_confidence = 0.0,
            override_note = f"No library match or slate found for: {label}",
        )

    def _save_atomic(self, playlist: Playlist, path: str) -> None:
        """Write playlist.json atomically."""
        dir_ = os.path.dirname(path) or "."
        os.makedirs(dir_, exist_ok=True)
        data = playlist.model_dump()
        fd, tmp = tempfile.mkstemp(dir=dir_, suffix=".tmp.json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, default=str)
            os.replace(tmp, path)
            log.info("Playlist saved atomically: %s", path,
                     extra={"service": "automation_ai"})
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
