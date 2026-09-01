"""Format-agnostic tag types shared across all audio modules.

`TagSet` is what the orchestrating tagger hands to a per-format
`write_tags(path, tagset, cover)` call. Each format module knows how to
serialise it to its native tag representation (MP4 atoms, ID3v2 frames,
Vorbis comments, etc.).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, NamedTuple

from mutagen import MutagenError

from .quality import AudioQuality


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
    # Disc number (Picard: disk / TPOS / DISCNUMBER). None when the tag is
    # absent, which is the norm for a single-disc release. Read here — in the
    # scan's one open per file — rather than via `read_tags`, because it is what
    # separates the two folders of a split release (disc 1, disc 2) from two
    # duplicate copies of the same one (both disc 1), and that question is asked
    # of every album in the library, not of one the user opened (#16).
    disc_num: int | None = None
    # The release's own counts, as the tagging wrote them: how many tracks this
    # file's medium has, and how many media the release has (Picard: trkn/disk
    # totals, TRCK/TPOS "n/total", TOTALTRACKS/TOTALDISCS).
    #
    # These are what COMPLETE vs INCOMPLETE is derived from (#195). MusicBrainz
    # told us both at tagging time and they were written into every file, so
    # re-fetching them would be asking a question already answered on disk — and
    # asking it once per album is a rate-limited request per album.
    track_total: int | None = None
    disc_total: int | None = None
    # This track's `MusicBrainz Release Track Id` — unique to its position in the
    # release, unlike the recording id, which a release can repeat.
    #
    # What tells two DIFFERENT parts of one album from two COPIES of it (#197):
    # different discs have disjoint sets of these, duplicates have identical
    # ones. Read here, in the scan's single open per file, because the question
    # is asked of any directory that shares a release with another — which the
    # scan cannot know in advance.
    release_track_id: str | None = None
    # What the audio stream itself is — sample rate, bit depth, bitrate (#130).
    # Read from the same handle as everything above, so it costs no extra open.
    #
    # Here rather than in `read_tags` because the album panel states the format
    # beside the path, which is scanner-derived and rendered before the tag
    # comparison is fetched. Empty for a file that wouldn't open, like every
    # other field on an unreadable one.
    quality: AudioQuality = AudioQuality()


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
    # albumartists, artists). Sort names drive correct alphabetisation in
    # Plex/Navidrome ("The Beatles" under B); the two list fields are the
    # unjoined per-artist names.
    #
    # `album_artists` is `artists` one level up, and it exists for the same
    # reason: `album_artist` is a single joined phrase — "zakè & rhubiqs" — so
    # a player wanting to file a collaboration under BOTH artists has to guess
    # where one name ends and the next begins. Navidrome and Plex read the
    # album-level list to avoid that guess (#322).
    album_artist_sort: str | None = None
    artist_sort: str | None = None
    album_artists: list[str] = field(default_factory=list)
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

    # Picard's `compilation` — the Various Artists flag (#323). Plex and every
    # iTunes-lineage player read it to decide whether the album is a VA
    # compilation; without it a 20-track compilation is shattered into twenty
    # one-track albums, one per track artist.
    #
    # `True` or absent, never `False`: Harmonist writes it only when set, so
    # "not a compilation" is the tag's absence — see `owned.as_flag`. NOT the
    # release group's `compilation` secondary type, which a greatest-hits album
    # by one artist also carries, and flagging one of those is precisely what
    # makes a player split it apart.
    compilation: bool | None = None

    mb_track_id: str | None = None
    mb_release_track_id: str | None = None
    mb_artist_ids: list[str] = field(default_factory=list)

    # ISRC(s) of the track's recording (a recording can carry several).
    isrcs: list[str] = field(default_factory=list)

    date: str | None = None
    disc_num: int = 1
    disc_total: int = 1
    # The medium's own name, where MusicBrainz has one — Hybrid's two discs are
    # "Wide Angle" and "Live Angle". Picard writes this as `discsubtitle`, so a
    # Picard-tagged library already carries it and Harmonist was STRIPPING it on
    # re-tag: a field the user's tagger wrote, silently removed by an operation
    # sold as bringing their tags up to date (#218). Most releases have none.
    disc_subtitle: str | None = None

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

    The named fields below are the ones a user recognises on an album page.
    They are NOT the whole story, and used to be: the comparison compared only
    what was named here, which was 9 of the 30 tags Harmonist writes, so a
    release that had grown an ISRC or an original date differed on disk while
    the panel reported every field matching — and its "N of M fields differ"
    count measured the wrong M (#295). `owned` closes that without turning this
    into a second copy of `TagSet`.
    """

    #: The file could not be opened at all. Every other field is then None, and
    #: that is NOT the same as an untagged file — see ScanFields.unreadable.
    unreadable: bool = False

    #: Every owned field as this file currently carries it — the `read_owned`
    #: snapshot, keyed by `Owned` value, taken from the handle `read_tags`
    #: already has open. Empty when the file could not be read.
    #:
    #: A dict rather than thirty more typed attributes, because the comparison
    #: table is then *derivable* from `Owned` instead of hand-listed beside it.
    #: Two hand-maintained lists are what let twenty-one fields go uncompared
    #: without anyone noticing, and a thirty-first added later would have joined
    #: them silently.
    #:
    #: Costs one dict per track on the album page and during the tagger's
    #: identity check. Deliberately not on the scan path — the scanner reads
    #: `read_scan_fields`, which stays as narrow as it was.
    owned: Mapping[str, Any] = field(default_factory=dict)

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

    #: This track's `MusicBrainz Release Track Id` — the id of one position in
    #: one release, unlike the recording id, which a release can repeat.
    #:
    #: What says WHICH track a file is (#232). Harmonist writes it on every
    #: track it tags and Picard writes the same, so for anything either has
    #: touched, "which MusicBrainz track is this file?" is a lookup and not a
    #: guess — where disc-and-track numbers are a guess the moment MusicBrainz
    #: renumbers a release's media, and a duration is a guess always.
    #:
    #: Already read into `ScanFields` for a different question (#197: two parts
    #: of a release, or two copies of one). This is the same atom, read on the
    #: comparison's pass instead of the scanner's.
    release_track_id: str | None = None

    #: Read from a video file, which Harmonist can read but never write (#66).
    #: Not a tag — a property of the read, like `unreadable`, and here for the
    #: same reason: the tracklist has to say something different about this row
    #: and cannot tell from the values alone. A video track is reported as
    #: PRESENT and nothing more (#226); comparing it field by field would raise
    #: findings against a file no amount of re-tagging can change.
    video: bool = False


class UnsupportedFormatError(Exception):
    """Raised when no audio module handles a given file extension."""


#: Everything a per-file tag read can fail with, as one name a caller can catch.
#:
#: The point of this package is that mutagen stays inside it, and a caller that
#: has to tolerate an unreadable file was otherwise forced to break that: an
#: `except OSError` looks complete and misses the common case, because
#: `MutagenError` is NOT an `OSError` — a truncated or non-audio file raises
#: `MP4StreamInfoError`, which inherits straight from `Exception`. That gap is
#: silent, which is the worst kind: the catch reads as thorough and the failure
#: goes straight past it.
#:
#: Only for callers that genuinely have a truthful answer for "I could not read
#: this" and can carry on. Anything writing on the user's behalf should let it
#: propagate — see the error-handling skill.
READ_ERRORS: tuple[type[Exception], ...] = (OSError, MutagenError, UnsupportedFormatError)
