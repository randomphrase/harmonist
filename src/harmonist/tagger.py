"""Picard-compatible tagger — orchestration layer.

Builds a format-agnostic `TagSet` per track from an MB release dict and
delegates the actual atom/frame/comment serialisation to the matching
`harmonist.formats.<format>` submodule.

For backward compatibility with existing tests, the MP4 atom-name
constants are re-exported here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from . import formats
from .formats import TagSet
from .formats.m4a import (  # noqa: F401 — back-compat re-exports
    ATOM_ALBUM,
    ATOM_ALBUM_ARTIST,
    ATOM_ARTIST,
    ATOM_ASIN,
    ATOM_BARCODE,
    ATOM_CATALOG,
    ATOM_COMMENT,
    ATOM_COVER,
    ATOM_DATE,
    ATOM_DISC_NUM,
    ATOM_GENRE,
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
    ATOM_PREFIX,
    ATOM_TITLE,
    ATOM_TRACK_NUM,
    LEGACY_RELEASE_ID,
)


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
        release: dict,
        cover_path: Path | None = None,
        *,
        incomplete: bool = False,
    ) -> int: ...


class PicardCompatibleTagger:
    """Default tagger — builds Picard-compatible tags and writes them to
    every supported audio file in the album dir."""

    def tag_album(
        self,
        album_dir: Path,
        release: dict,
        cover_path: Path | None = None,
        *,
        incomplete: bool = False,
    ) -> int:
        return tag_album(album_dir, release, cover_path, incomplete=incomplete)


def tag_album(
    album_dir: Path,
    release: dict,
    cover_path: Path | None = None,
    *,
    incomplete: bool = False,
) -> int:
    """Tag every supported audio file in `album_dir`.

    `release` is the unwrapped MusicBrainz release dict, i.e. what
    `musicbrainzngs.get_release_by_id()` returns under the "release" key.
    Returns the number of files tagged.

    `incomplete=True` allows file_count < track_count and assigns files
    to a subset of MB tracks via length-similarity (positional fallback).
    file_count > track_count is still an error in both modes (per design
    §15.3 — "extra files on disk" is out of scope).
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
        pairs = list(zip(files, flat_tracks))

    cover = cover_path.read_bytes() if cover_path else None
    media_total = len(release.get("medium-list", [])) or 1

    for file_path, (medium, track_pos_in_medium, track) in pairs:
        tagset = _build_tagset(release, medium, track_pos_in_medium, track, media_total)
        formats.write_tags(file_path, tagset, cover)

    return len(files)


def _build_tagset(
    release: dict,
    medium: dict,
    track_pos: int,
    track: dict,
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
        mb_album_artist_ids=_artist_ids(release.get("artist-credit")),
        mb_release_group_id=rg.get("id"),
        mb_album_type=rg.get("primary-type"),
        mb_album_status=release.get("status"),
        mb_album_country=release.get("country"),
        mb_track_id=(track.get("recording") or {}).get("id"),
        mb_release_track_id=track.get("id"),
        mb_artist_ids=_artist_ids(track_artist_credit),
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
    flat_tracks: list[tuple[dict, int, dict]],
) -> list[tuple[Path, tuple[dict, int, dict]]]:
    """Best-fit assignment of files to a subset of MB tracks via length
    similarity, preserving input file order.

    Falls back to positional matching when any file or track length is
    unknown — the simpler choice is more predictable without enough data.
    """
    file_durations: list[int | None] = [formats.read_duration_ms(f) for f in files]

    track_lengths: list[int | None] = []
    for _medium, _pos, track in flat_tracks:
        raw = (track.get("recording") or {}).get("length") or track.get("length")
        try:
            track_lengths.append(int(raw))
        except (TypeError, ValueError):
            track_lengths.append(None)

    if any(t is None for t in track_lengths) or any(d is None for d in file_durations):
        # Positional fallback — first N tracks; the rest are "missing"
        # and get no file assigned.
        return [(files[i], flat_tracks[i]) for i in range(len(files))]

    used: set[int] = set()
    pairs: list[tuple[Path, tuple[dict, int, dict]]] = []
    for f, dur in zip(files, file_durations):
        best_idx = None
        best_delta: int | None = None
        for i, tlen in enumerate(track_lengths):
            if i in used:
                continue
            delta = abs(dur - tlen)
            if best_delta is None or delta < best_delta:
                best_idx = i
                best_delta = delta
        assert best_idx is not None
        used.add(best_idx)
        pairs.append((f, flat_tracks[best_idx]))
    return pairs


def _flatten_tracks(release: dict):
    """Yield (medium, track_pos_in_medium, track) for every track in every medium."""
    for medium in release.get("medium-list", []):
        for i, track in enumerate(medium.get("track-list", [])):
            yield medium, i, track


def _track_title(track: dict) -> str:
    if recording := track.get("recording"):
        if title := recording.get("title"):
            return title
    return track.get("title", "")


def _artist_ids(artist_credit) -> list[str]:
    """Pull MBIDs out of an MB artist-credit list."""
    if not artist_credit:
        return []
    ids = []
    for ac in artist_credit:
        if isinstance(ac, dict):
            artist = ac.get("artist") or {}
            if artist_id := artist.get("id"):
                ids.append(artist_id)
    return ids


def _artist_phrase(artist_credit) -> str:
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
