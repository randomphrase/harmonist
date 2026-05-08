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
    TAGGING = "tagging"
    DONE = "done"


SourceKind = Literal["bandcamp", "manual"]
LookupResult = Literal["match", "no_match", "error"]


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
class Sidecar:
    schema_version: int
    source: SourceKind
    bandcamp: BandcampInfo | None = None
    downloaded_at: datetime | None = None
    added_at: datetime | None = None
    mb_release_id: str | None = None
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
