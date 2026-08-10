"""Format-agnostic tag types shared across all audio modules.

`TagSet` is what the orchestrating tagger hands to a per-format
`write_tags(path, tagset, cover)` call. Each format module knows how to
serialise it to its native tag representation (MP4 atoms, ID3v2 frames,
Vorbis comments, etc.).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import NamedTuple


class ScanFields(NamedTuple):
    """The tag fields the scanner needs per file, read in a SINGLE file open
    (instead of one open per field). `codec` is the short format label;
    `has_cover` is True when the file carries embedded cover art."""

    album_title: str | None
    album_id: str | None
    artist: str | None
    codec: str | None
    has_cover: bool = False
    # Album-level artist (Picard: aART / TPE2 / ALBUMARTIST). Authoritative for
    # display; "Various Artists" on a compilation. None when the tag is absent.
    album_artist: str | None = None
    # True when the file could not be opened at all — a permission error, a
    # truncated file, a failing disk. WITHOUT this, every field reads None and
    # the result is byte-identical to a perfectly readable untagged file, so a
    # COMPLETE album on a dying drive reappears in the inbox as stuck
    # mid-tagging and invites the user to re-tag it (#112). "I couldn't read
    # this" and "there's nothing here" are different answers.
    unreadable: bool = False


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

    # Sort names + multi-value artists (Picard: albumartistsort, artistsort,
    # artists). Sort names drive correct alphabetisation in Plex/Navidrome
    # ("The Beatles" under B); `artists` is the unjoined per-artist list.
    album_artist_sort: str | None = None
    artist_sort: str | None = None
    artists: list[str] = field(default_factory=list)

    # Original release date of the *work* (release-group first-release-date),
    # distinct from this edition's `date`. The MB script of the title text
    # (e.g. "Latn").
    original_date: str | None = None
    script: str | None = None

    mb_album_artist_ids: list[str] = field(default_factory=list)
    mb_release_group_id: str | None = None
    mb_album_type: str | None = None
    mb_album_status: str | None = None
    mb_album_country: str | None = None

    mb_track_id: str | None = None
    mb_release_track_id: str | None = None
    mb_artist_ids: list[str] = field(default_factory=list)

    # ISRC(s) of the track's recording (a recording can carry several).
    isrcs: list[str] = field(default_factory=list)

    date: str | None = None
    disc_num: int = 1
    disc_total: int = 1

    label: str | None = None
    catalog_number: str | None = None
    barcode: str | None = None
    asin: str | None = None
    media: str | None = None


@dataclass(frozen=True)
class TrackTags:
    """What one file actually carries, for comparison against MusicBrainz (#106).

    The read-side counterpart to `TagSet`. Separate from it on purpose: `TagSet`
    is what Harmonist *writes* and every field is required or defaulted, whereas
    this is what it *finds*, where every field is legitimately absent and the
    difference between "absent" and "unreadable" is the whole point.

    Deliberately narrower than `TagSet` too. It carries the fields a user would
    recognise on an album page — not MusicBrainz ids, which are plumbing, and
    not sort names. Arbitrary/unknown tags are a later addition (see #106); the
    shape here doesn't preclude them.
    """

    #: The file could not be opened at all. Every other field is then None, and
    #: that is NOT the same as an untagged file — see ScanFields.unreadable.
    unreadable: bool = False

    # Album-level: the same on every track, which is what makes disagreement
    # between tracks meaningful rather than expected.
    album: str | None = None
    album_artist: str | None = None
    date: str | None = None
    label: str | None = None
    catalog_number: str | None = None
    barcode: str | None = None
    media: str | None = None
    genre: str | None = None

    # Per-track: expected to vary.
    title: str | None = None
    artist: str | None = None
    track_num: int | None = None
    #: Which medium the track belongs to. Load-bearing for the tracklist
    #: comparison (#135), not decoration: files are lined up against
    #: MusicBrainz by number, and on a 2-CD release track 4 exists twice.
    #: Without the disc, the two collide and the comparison pairs the wrong
    #: halves of the album against each other.
    disc_num: int | None = None
    duration_ms: int | None = None

    #: Harmonist-owned. Carries the recovered Bandcamp URL, so MusicBrainz has
    #: no counterpart and it must never be rendered as a difference.
    comment: str | None = None


class UnsupportedFormatError(Exception):
    """Raised when no audio module handles a given file extension."""
