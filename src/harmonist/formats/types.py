"""Format-agnostic tag types shared across all audio modules.

`TagSet` is what the orchestrating tagger hands to a per-format
`write_tags(path, tagset, cover)` call. Each format module knows how to
serialise it to its native tag representation (MP4 atoms, ID3v2 frames,
Vorbis comments, etc.).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class TagSet:
    """Picard-compatible tag values for a single track.

    Album-level fields (mb_album_id, album, album_artist, label, etc.)
    are the same across every track of an album. Per-track fields
    (title, mb_track_id, artist, track_num) vary.
    """

    # Album-level identity
    mb_album_id: str
    album: str
    album_artist: str

    # Per-track
    title: str
    artist: str
    track_num: int
    track_total: int

    mb_album_artist_ids: list[str] = field(default_factory=list)
    mb_release_group_id: str | None = None
    mb_album_type: str | None = None
    mb_album_status: str | None = None
    mb_album_country: str | None = None

    mb_track_id: str | None = None
    mb_release_track_id: str | None = None
    mb_artist_ids: list[str] = field(default_factory=list)

    date: str | None = None
    disc_num: int = 1
    disc_total: int = 1

    label: str | None = None
    catalog_number: str | None = None
    barcode: str | None = None
    asin: str | None = None
    media: str | None = None


class UnsupportedFormatError(Exception):
    """Raised when no audio module handles a given file extension."""
