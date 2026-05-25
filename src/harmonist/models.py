"""Core data types: Album, AlbumState, Sidecar."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

# MusicBrainz JSON shapes. `musicbrainzngs` returns plain untyped dicts whose
# schema varies with the `includes=` we request and is riddled with optional,
# nested keys we read defensively. The value type is genuinely Any, so a
# TypedDict would only assert a shape we never validate at the boundary. These
# aliases document *which* MB shape a dict represents at each call site.
type Release = dict[str, Any]
type Track = dict[str, Any]


class AlbumState(StrEnum):
    NEW = "new"
    # NEEDS_MBID covers "no confirmed MB release yet" — whether or not a
    # suggestion (mb_match_candidate) is attached. The card adapts: with a
    # candidate it shows the side-by-side + Confirm; without, the assign/find
    # tools. (Formerly split into NEEDS_MBID + NEEDS_REVIEW.)
    NEEDS_MBID = "needs_mbid"
    TAGGING = "tagging"
    NEEDS_SYNC = "needs_sync"
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"
    INCONSISTENT = "inconsistent"


MatchConfidence = Literal["exact", "approximate", "no_match"]


@dataclass
class InconsistentTrack:
    """One row in the INCONSISTENT card's per-file summary table.

    Surfaced when files in a single album dir disagree on album title
    (`©alb`) or MB Album Id (`----:com.apple.iTunes:MusicBrainz Album Id`).
    Compilations (varying artist, consistent album+MBID) don't appear here
    — they're legitimate, not inconsistent.
    """

    file_name: str
    album_title: str | None
    mb_album_id: str | None


@dataclass
class BandcampInfo:
    """Bandcamp-specific identifiers. Optional — only set when the album
    came from Bandcamp AND we know the item_id (typically captured during
    bandcampsync at download time).
    """

    item_id: int | None = None
    band_id: int | None = None


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
    """Per-album persisted state — the on-disk `.harmonist.json`.

    `store_url` carries the canonical purchase URL from any store Harmony
    supports (Bandcamp, Beatport, Discogs, etc.). Store identity is derived
    from the URL host; absence of store_url means "no store source recorded".

    Identity: exactly one of `(mb_release_id, temp_uid)` is non-null on
    any persisted sidecar. `temp_uid` holds the album's stable URL id
    until an MBID lands, at which point sidecar.write() drops it. The
    scanner reads whichever is set and assigns it to `Album.id`.
    """

    schema_version: int
    store_url: str | None = None
    bandcamp: BandcampInfo | None = None
    downloaded_at: datetime | None = None
    added_at: datetime | None = None
    mb_release_id: str | None = None
    temp_uid: str | None = None
    mb_match_candidate: MatchCandidate | None = None
    tagged_at: datetime | None = None
    # MB release's track count at the time the album was tagged. Set on
    # any tag (including incomplete-mode); the scanner uses it to distinguish
    # COMPLETE (file_count == this) from INCOMPLETE (file_count < this).
    # See design §3, §15.3.
    track_count_expected: int | None = None
    notes: str | None = None


@dataclass
class Album:
    """An album as observed on disk, derived from sidecar + filesystem.

    `id` is the album's stable URL id. The scanner assigns it from the
    sidecar's `mb_release_id` (preferred) or `temp_uid` (fallback). For
    NEW albums with no sidecar, the scanner mints a per-process UUID via
    `id_registry`. No path-derived ids exist anywhere — that's the point.
    """

    id: str
    path: Path
    title: str
    artist: str
    track_count: int
    state: AlbumState
    sidecar: Sidecar | None = None
    cover_path: Path | None = None
    # Populated only when state == INCONSISTENT — per-file summary of the
    # conflicting fields for the UI table. Empty list otherwise.
    inconsistent_tracks: list[InconsistentTrack] = field(default_factory=list)
    # `(tagged_count, total_count)` when only some files in the dir have
    # the matching MB Album Id atom (0 < tagged < total). None otherwise.
    # Not persisted — purely scanner-derived. Independent of INCOMPLETE:
    # partial tagging is about MBID atoms on present files, INCOMPLETE
    # is about how many files are present vs MB's tracklist.
    partial_tag_count: tuple[int, int] | None = None
    # Human label for the album's audio format(s), e.g. "ALAC", "FLAC", or
    # "Mixed" when files disagree. Scanner-derived; not persisted. Confirms
    # the download format the user chose actually landed.
    audio_format: str | None = None


# ---------------------------------------------------------------------------
# Store URL helpers
# ---------------------------------------------------------------------------


def is_bandcamp_url(url: str | None) -> bool:
    """True if the URL is on a bandcamp.com domain (any subdomain or
    custom domain mapped to bandcamp.com).
    """
    if not url:
        return False
    host = (urlparse(url).hostname or "").lower()
    return host == "bandcamp.com" or host.endswith(".bandcamp.com")


def store_name(url: str | None) -> str | None:
    """Identify the store from a URL's hostname. Returns None when the
    URL is absent or the store is unrecognised.
    """
    if not url:
        return None
    host = (urlparse(url).hostname or "").lower()
    if host == "bandcamp.com" or host.endswith(".bandcamp.com"):
        return "bandcamp"
    if host.endswith("beatport.com"):
        return "beatport"
    if host.endswith("discogs.com"):
        return "discogs"
    return None
