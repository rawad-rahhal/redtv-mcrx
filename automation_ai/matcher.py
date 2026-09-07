"""
automation_ai.matcher
=====================
Media library matcher.
Supports:
  - Exact numeric ID match:   "REDTV_00042" -> media_id REDTV_00042
  - Exact title match
  - Fuzzy title match using difflib SequenceMatcher
  - Returns ai_confidence score 0.0–1.0
"""

from __future__ import annotations

import difflib
import logging
import re
from dataclasses import dataclass
from typing import List, Optional

log = logging.getLogger("redtv.automation_ai.matcher")


@dataclass
class MatchResult:
    media_id:       Optional[str]
    media_path:     Optional[str]
    title:          str
    confidence:     float
    match_type:     str    # "exact_id", "exact_title", "fuzzy", "none"
    warning:        Optional[str] = None


@dataclass
class MediaLibraryEntry:
    media_id:   str
    title:      str
    media_path: str
    tags:       List[str]


# Matches patterns like REDTV_00042, 42, #42
_NUMERIC_RE = re.compile(r"(?:REDTV_)?(\d+)", re.IGNORECASE)


class MediaMatcher:
    """
    Matches rundown text entries to the media library.
    """

    def __init__(self, library: List[MediaLibraryEntry], fuzzy_threshold: float = 0.6) -> None:
        self._library   = library
        self._threshold = fuzzy_threshold
        self._id_index  = {e.media_id.upper(): e for e in library}
        self._title_index = {e.title.lower().strip(): e for e in library}

    def match(self, query: str) -> MatchResult:
        """Attempt to match query to a library entry."""
        query = query.strip()

        # 1. Exact numeric / ID match
        m = _NUMERIC_RE.fullmatch(query.replace(" ", "_"))
        if not m:
            m = _NUMERIC_RE.search(query)
        if m:
            candidate_id = f"REDTV_{int(m.group(1)):05d}"
            entry = self._id_index.get(candidate_id.upper())
            if entry:
                return MatchResult(
                    media_id   = entry.media_id,
                    media_path = entry.media_path,
                    title      = entry.title,
                    confidence = 1.0,
                    match_type = "exact_id",
                )

        # 2. Exact title match (case-insensitive)
        entry = self._title_index.get(query.lower())
        if entry:
            return MatchResult(
                media_id   = entry.media_id,
                media_path = entry.media_path,
                title      = entry.title,
                confidence = 1.0,
                match_type = "exact_title",
            )

        # 3. Fuzzy match against titles
        titles = list(self._title_index.keys())
        matches = difflib.get_close_matches(query.lower(), titles, n=1, cutoff=self._threshold)
        if matches:
            entry = self._title_index[matches[0]]
            ratio = difflib.SequenceMatcher(None, query.lower(), matches[0]).ratio()
            return MatchResult(
                media_id   = entry.media_id,
                media_path = entry.media_path,
                title      = entry.title,
                confidence = round(ratio, 3),
                match_type = "fuzzy",
                warning    = f"Fuzzy match '{query}' -> '{entry.title}' (conf={ratio:.2f})",
            )

        return MatchResult(
            media_id   = None,
            media_path = None,
            title      = query,
            confidence = 0.0,
            match_type = "none",
            warning    = f"No match found for: '{query}'",
        )
