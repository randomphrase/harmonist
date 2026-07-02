"""Picard-compatible tagger — orchestration layer.

Builds a format-agnostic `TagSet` per track from an MB release dict and
delegates the actual atom/frame/comment serialisation to the matching
`harmonist.formats.<format>` submodule.

For backward compatibility with existing tests, the MP4 atom-name
constants are re-exported here.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from . import formats
from .formats import TagSet
from .formats.m4a import (  # noqa: F401 — back-compat re-exports
    ATOM_ALBUM,
    ATOM_ALBUM_ARTIST,
    ATOM_ALBUM_ARTIST_SORT,
    ATOM_ARTIST,
    ATOM_ARTIST_SORT,
    ATOM_ARTISTS,
    ATOM_ASIN,
    ATOM_BARCODE,
    ATOM_CATALOG,
    ATOM_COMMENT,
    ATOM_COVER,
    ATOM_DATE,
    ATOM_DISC_NUM,
    ATOM_GENRE,
    ATOM_ISRC,
    ATOM_LABEL,
    ATOM_MB_ALBUM_ARTIST_ID,
    ATOM_MB_ALBUM_COUNTRY,
    ATOM_MB_ALBUM_ID,
    ATOM_MB_ALBUM_STATUS,
    ATOM_MB_ALBUM_TYPE,
    ATOM_MB_ARTIST_ID,
    ATOM_MB_RELEASE_GROUP_ID,
    ATOM_MB_RELEASE_TRACK_ID,
    ATOM_MB_TRACK_ID,
    ATOM_MEDIA,
    ATOM_ORIGINAL_DATE,
    ATOM_ORIGINAL_YEAR,
    ATOM_PREFIX,
    ATOM_SCRIPT,
    ATOM_TITLE,
    ATOM_TRACK_NUM,
    LEGACY_RELEASE_ID,
)
from .models import Release, Track

log = logging.getLogger(__name__)

# One flattened MB track: (medium, track_pos_in_medium, track).
_FlatTrack = tuple[dict[str, Any], int, Track]


class TagMismatchError(Exception):
    """Raised when the file count doesn't match the MB release's track count."""


@runtime_checkable
class Tagger(Protocol):
    """Contract for a Harmonist tagger.

    Implementations write tags to every audio file in `album_dir` based on
    the supplied MB release dict, optionally embedding cover art from
    `cover_path`. Returns the number of files tagged. Raises
    `TagMismatchError` when the file count and MB track count diverge
    (unless `incomplete=True`).
    """

    def tag_album(
        self,
        album_dir: Path,
        release: Release,
        cover_path: Path | None = None,
        *,
        incomplete: bool = False,
        overwrite_art: bool = False,
    ) -> int: ...


class PicardCompatibleTagger:
    """Default tagger — builds Picard-compatible tags and writes them to
    every supported audio file in the album dir."""

    def tag_album(
        self,
        album_dir: Path,
        release: Release,
        cover_path: Path | None = None,
        *,
        incomplete: bool = False,
        overwrite_art: bool = False,
    ) -> int:
        return tag_album(
            album_dir, release, cover_path, incomplete=incomplete, overwrite_art=overwrite_art
        )


def tag_album(
    album_dir: Path,
    release: Release,
    cover_path: Path | None = None,
    *,
    incomplete: bool = False,
    overwrite_art: bool = False,
) -> int:
    """Tag every supported audio file in `album_dir`.

    `release` is the unwrapped MusicBrainz release dict, i.e. what
    `musicbrainzngs.get_release_by_id()` returns under the "release" key.
    Returns the number of files tagged.

    `incomplete=True` allows file_count < track_count and assigns files
    to a subset of MB tracks via length-similarity (positional fallback).
    file_count > track_count is still an error in both modes (per design
    §15.3 — "extra files on disk" is out of scope).

    `overwrite_art=True` embeds the album cover even when the tracks carry
    differing per-track artwork (which is otherwise preserved) — the user's
    explicit "replace the artwork" override.
    """
    files = sorted(p for p in album_dir.iterdir() if formats.is_supported(p))
    flat_tracks = list(_flatten_tracks(release))

    if not incomplete and len(files) != len(flat_tracks):
        raise TagMismatchError(
            f"album {album_dir.name!r}: {len(files)} audio files but MB release "
            f"has {len(flat_tracks)} tracks"
        )
    if len(files) > len(flat_tracks):
        raise TagMismatchError(
            f"album {album_dir.name!r}: {len(files)} files exceeds MB release "
            f"track count {len(flat_tracks)} — extra files on disk are out of "
            f"scope (see design §15.3)"
        )

    if incomplete and len(files) < len(flat_tracks):
        pairs = _assign_files_to_tracks(files, flat_tracks)
    else:
        # Counts are guaranteed equal here by the checks above.
        pairs = list(zip(files, flat_tracks, strict=True))

    cover = cover_path.read_bytes() if cover_path else None
    # DATA SAFETY: if the tracks carry DIFFERENT embedded art (a per-track-art
    # album, e.g. a compilation), embedding one album cover would destroy those
    # images. Preserve them — pass cover=None (write_tags leaves the existing
    # embedded cover untouched); the folder cover.* is still written separately.
    if cover is not None and not overwrite_art and _has_per_track_art(files):
        log.warning(
            "%s: tracks have per-track embedded artwork — keeping it, NOT embedding "
            "the album cover (folder cover.* is still written). Re-tag with "
            "'replace artwork' to override.",
            album_dir.name,
        )
        cover = None
    media_total = len(release.get("medium-list", [])) or 1

    for file_path, (medium, track_pos_in_medium, track) in pairs:
        tagset = _build_tagset(release, medium, track_pos_in_medium, track, media_total)
        formats.write_tags(file_path, tagset, cover)

    return len(files)


def _has_per_track_art(files: list[Path]) -> bool:
    """True when the album's tracks carry DIFFERENT embedded cover images — i.e.
    per-track artwork worth preserving. Reads each file's existing cover once (at
    tag time, when we're opening the files anyway) and compares hashes."""
    seen: set[str] = set()
    for f in files:
        art = formats.read_cover(f)
        if art is not None:
            seen.add(hashlib.sha1(art[0]).hexdigest())
        if len(seen) > 1:
            return True
    return False


def _build_tagset(
    release: Release,
    medium: dict[str, Any],
    track_pos: int,
    track: Track,
    media_total: int,
) -> TagSet:
    """Translate one MB track within a release to a TagSet."""
    track_artist_credit = track.get("artist-credit") or release.get("artist-credit")
    label_info = release.get("label-info-list") or []
    first_label = label_info[0] if label_info else {}
    rg = release.get("release-group") or {}

    track_total = len(medium.get("track-list", []))

    disc_num = 1
    if "position" in medium:
        try:
            disc_num = int(medium["position"])
        except (TypeError, ValueError):
            disc_num = 1

    return TagSet(
        mb_album_id=release["id"],
        album=release.get("title", ""),
        album_artist=_artist_phrase(release.get("artist-credit")),
        title=_track_title(track),
        artist=_artist_phrase(track_artist_credit),
        track_num=track_pos + 1,
        track_total=track_total,
        album_artist_sort=_artist_sort_phrase(release.get("artist-credit")) or None,
        artist_sort=_artist_sort_phrase(track_artist_credit) or None,
        artists=_artist_names(track_artist_credit),
        original_date=rg.get("first-release-date") or None,
        script=(release.get("text-representation") or {}).get("script") or None,
        mb_album_artist_ids=_artist_ids(release.get("artist-credit")),
        mb_release_group_id=rg.get("id"),
        mb_album_type=rg.get("primary-type"),
        # Picard writes the status lower-cased (e.g. "official", not "Official").
        mb_album_status=(release.get("status") or "").lower() or None,
        mb_album_country=release.get("country"),
        mb_track_id=(track.get("recording") or {}).get("id"),
        mb_release_track_id=track.get("id"),
        mb_artist_ids=_artist_ids(track_artist_credit),
        isrcs=_isrcs(track),
        date=release.get("date") or None,
        disc_num=disc_num,
        disc_total=media_total,
        label=first_label.get("label", {}).get("name") if first_label else None,
        catalog_number=first_label.get("catalog-number") if first_label else None,
        barcode=release.get("barcode") or None,
        asin=release.get("asin") or None,
        media=medium.get("format") or None,
    )


def _assign_files_to_tracks(
    files: list[Path],
    flat_tracks: list[_FlatTrack],
) -> list[tuple[Path, _FlatTrack]]:
    """Best-fit assignment of files to a subset of MB tracks via length
    similarity, preserving input file order.

    Falls back to positional matching when any file or track length is
    unknown — the simpler choice is more predictable without enough data.
    """
    file_durations: list[int | None] = [formats.read_duration_ms(f) for f in files]

    track_lengths: list[int | None] = []
    for _medium, _pos, track in flat_tracks:
        # Per-release track length is authoritative; recording length can
        # differ by seconds across releases (see match._mb_track_length_ms).
        raw = track.get("length") or (track.get("recording") or {}).get("length")
        try:
            track_lengths.append(None if raw is None else int(raw))
        except (TypeError, ValueError):
            track_lengths.append(None)

    if any(t is None for t in track_lengths) or any(d is None for d in file_durations):
        # Positional fallback — first N tracks; the rest are "missing"
        # and get no file assigned.
        return [(files[i], flat_tracks[i]) for i in range(len(files))]

    used: set[int] = set()
    pairs: list[tuple[Path, _FlatTrack]] = []
    for f, dur in zip(files, file_durations, strict=True):
        # The guard above guarantees every duration/length is set here.
        assert dur is not None
        best_idx = None
        best_delta: int | None = None
        for i, tlen in enumerate(track_lengths):
            if i in used or tlen is None:
                continue
            delta = abs(dur - tlen)
            if best_delta is None or delta < best_delta:
                best_idx = i
                best_delta = delta
        assert best_idx is not None
        used.add(best_idx)
        pairs.append((f, flat_tracks[best_idx]))
    return pairs


def _flatten_tracks(release: Release) -> Iterator[_FlatTrack]:
    """Yield (medium, track_pos_in_medium, track) for every track in every medium."""
    for medium in release.get("medium-list", []):
        for i, track in enumerate(medium.get("track-list", [])):
            yield medium, i, track


def _track_title(track: Track) -> str:
    if (recording := track.get("recording")) and (title := recording.get("title")):
        return str(title)
    return str(track.get("title", ""))


def _isrcs(track: Track) -> list[str]:
    """The ISRC code(s) of the track's recording (MB returns `isrc-list` when
    the release is fetched with the `isrcs` include)."""
    recording = track.get("recording") or {}
    return [str(code) for code in (recording.get("isrc-list") or [])]


def _artist_ids(artist_credit: list[Any] | None) -> list[str]:
    """Pull MBIDs out of an MB artist-credit list."""
    if not artist_credit:
        return []
    ids: list[str] = []
    for ac in artist_credit:
        if isinstance(ac, dict):
            artist = ac.get("artist") or {}
            if artist_id := artist.get("id"):
                ids.append(artist_id)
    return ids


def _artist_phrase(artist_credit: list[Any] | None) -> str:
    """Build a display string from an MB artist-credit list."""
    if not artist_credit:
        return ""
    parts: list[str] = []
    for ac in artist_credit:
        if isinstance(ac, str):
            parts.append(ac)
        elif isinstance(ac, dict):
            name = ac.get("name") or ac.get("artist", {}).get("name", "")
            parts.append(name)
            if jp := ac.get("joinphrase"):
                parts.append(jp)
    return "".join(parts).strip()


def _artist_sort_phrase(artist_credit: list[Any] | None) -> str:
    """Like `_artist_phrase` but using each artist's MB **sort-name** (e.g.
    'Beatles, The'), keeping join phrases. Empty when no sort-names are present."""
    if not artist_credit:
        return ""
    parts: list[str] = []
    any_sort = False
    for ac in artist_credit:
        if isinstance(ac, dict):
            sort = (ac.get("artist") or {}).get("sort-name")
            if sort:
                any_sort = True
            parts.append(sort or ac.get("name") or (ac.get("artist") or {}).get("name", ""))
            if jp := ac.get("joinphrase"):
                parts.append(jp)
    return "".join(parts).strip() if any_sort else ""


def _artist_names(artist_credit: list[Any] | None) -> list[str]:
    """The individual artist display names (no join phrases) — Picard's
    multi-value `artists` / ARTISTS tag."""
    if not artist_credit:
        return []
    names: list[str] = []
    for ac in artist_credit:
        if isinstance(ac, dict):
            name = ac.get("name") or (ac.get("artist") or {}).get("name", "")
            if name:
                names.append(name)
    return names
