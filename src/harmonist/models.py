"""Core data types: Album, AlbumState, Sidecar."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Literal


class AlbumState(str, Enum):
    ORPHAN = "orphan"
    HELD_BANDCAMP = "held_bandcamp"
    HELD_MANUAL = "held_manual"
    NEEDS_CONFIRMATION = "needs_confirmation"
    TAGGING = "tagging"
    DONE = "done"


SourceKind = Literal["bandcamp", "manual"]
LookupResult = Literal["match", "no_match", "error"]
MatchConfidence = Literal["exact", "approximate", "no_match"]


@dataclass
class BandcampInfo:
    url: str
    item_id: int
    band_id: int | None = None


@dataclass
class MBLookupAttempt:
    at: datetime
    result: LookupResult
    mbid: str | None = None
    error: str | None = None


@dataclass
class TrackComparison:
    """One row in a side-by-side files-vs-MB-release comparison.

    All fields are nullable to allow padding rows when the counts don't match:
    - file_* None → an MB track with no corresponding file on disk
    - mb_* None  → a file on disk with no corresponding MB track
    """
    file_name: str | None
    file_duration_ms: int | None
    file_title: str | None  # from the ©nam tag if present, else filename stem
    mb_track_title: str | None
    mb_track_length_ms: int | None
    delta_ms: int | None  # None when either side's length is unknown


@dataclass
class MatchCandidate:
    """A proposed-but-not-confirmed MBID match for an album.

    Stashed in the sidecar when the MB lookup found a release but the
    file/track shape doesn't perfectly fit. The user must Confirm or
    Reject before tagging proceeds.
    """
    mb_release_id: str
    confidence: MatchConfidence
    file_count: int
    track_count: int
    track_comparisons: list[TrackComparison] = field(default_factory=list)
    proposed_at: datetime | None = None
    notes: list[str] = field(default_factory=list)


@dataclass
class Sidecar:
    schema_version: int
    source: SourceKind
    bandcamp: BandcampInfo | None = None
    downloaded_at: datetime | None = None
    added_at: datetime | None = None
    mb_release_id: str | None = None
    mb_match_candidate: MatchCandidate | None = None
    mb_last_checked_at: datetime | None = None
    mb_lookup_history: list[MBLookupAttempt] = field(default_factory=list)
    tagged_at: datetime | None = None
    notes: str | None = None


@dataclass
class Album:
    """An album as observed on disk, derived from sidecar + filesystem."""

    id: str
    path: Path
    title: str
    artist: str
    track_count: int
    state: AlbumState
    sidecar: Sidecar | None = None
    cover_path: Path | None = None

    @staticmethod
    def make_id(path: Path) -> str:
        return hashlib.md5(str(path).encode("utf-8")).hexdigest()
