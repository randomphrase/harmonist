"""Picard-compatible MP4 tag writer.

Writes the full MusicBrainz atom set + standard text tags + cover art onto
every .m4a file in an album directory. Atom names follow Picard's convention
(spaces, not underscores).

The default implementation (`PicardCompatibleTagger`) conforms to the
MusicBrainz Picard MP4 mapping spec
(https://picard.musicbrainz.org/docs/mappings/). The `Tagger` Protocol exists
so this can be swapped for another implementation later (e.g. headless
Picard, a different format target) without touching call sites.
"""
from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from mutagen.mp4 import MP4, MP4Cover


ATOM_PREFIX = "----:com.apple.iTunes:"

# Album-level (same on every track in the album)
ATOM_MB_ALBUM_ID = f"{ATOM_PREFIX}MusicBrainz Album Id"
ATOM_MB_ALBUM_ARTIST_ID = f"{ATOM_PREFIX}MusicBrainz Album Artist Id"
ATOM_MB_RELEASE_GROUP_ID = f"{ATOM_PREFIX}MusicBrainz Release Group Id"
ATOM_MB_ALBUM_TYPE = f"{ATOM_PREFIX}MusicBrainz Album Type"
ATOM_MB_ALBUM_STATUS = f"{ATOM_PREFIX}MusicBrainz Album Status"
ATOM_MB_ALBUM_COUNTRY = f"{ATOM_PREFIX}MusicBrainz Album Release Country"

# Per-track
ATOM_MB_TRACK_ID = f"{ATOM_PREFIX}MusicBrainz Track Id"
ATOM_MB_RELEASE_TRACK_ID = f"{ATOM_PREFIX}MusicBrainz Release Track Id"
ATOM_MB_ARTIST_ID = f"{ATOM_PREFIX}MusicBrainz Artist Id"

# Optional album-level metadata
ATOM_LABEL = f"{ATOM_PREFIX}LABEL"
ATOM_CATALOG = f"{ATOM_PREFIX}CATALOGNUMBER"
ATOM_BARCODE = f"{ATOM_PREFIX}BARCODE"
ATOM_MEDIA = f"{ATOM_PREFIX}MEDIA"
ATOM_ASIN = f"{ATOM_PREFIX}ASIN"

# Legacy (non-Picard) atom written by the previous code; remove on retag.
LEGACY_RELEASE_ID = f"{ATOM_PREFIX}MUSICBRAINZ_RELEASEID"

# Comment atom — preserved (Bandcamp URL fallback lives here).
COMMENT_ATOM = "\xa9cmt"


class TagMismatchError(Exception):
    """Raised when the file count doesn't match the MB release's track count."""


@runtime_checkable
class Tagger(Protocol):
    """Contract for a Harmonist tagger.

    Implementations write tags to every audio file in `album_dir` based on
    the supplied MB release dict, optionally embedding cover art from
    `cover_path`. Returns the number of files tagged. Raises
    `TagMismatchError` when the file count and MB track count diverge.
    """

    def tag_album(
        self,
        album_dir: Path,
        release: dict,
        cover_path: Path | None = None,
    ) -> int:
        ...


class PicardCompatibleTagger:
    """Default tagger — writes the Picard-spec MP4 atom set via mutagen.

    Output intended to be byte-identical to what MusicBrainz Picard would
    write for the same release. Verified by chunk G's byte-diff fixture
    test (TBD).
    """

    def tag_album(
        self,
        album_dir: Path,
        release: dict,
        cover_path: Path | None = None,
    ) -> int:
        return tag_album(album_dir, release, cover_path)


def tag_album(
    album_dir: Path,
    release: dict,
    cover_path: Path | None = None,
) -> int:
    """Tag every .m4a file in `album_dir` with Picard-compatible atoms.

    `release` is the unwrapped MusicBrainz release dict, i.e. what
    musicbrainzngs.get_release_by_id() returns under the "release" key.
    Returns the number of files tagged.
    """
    files = sorted(p for p in album_dir.glob("*.m4a") if p.is_file())
    flat_tracks = list(_flatten_tracks(release))

    if len(files) != len(flat_tracks):
        raise TagMismatchError(
            f"album {album_dir.name!r}: {len(files)} .m4a files but MB release "
            f"has {len(flat_tracks)} tracks"
        )

    cover = _load_cover(cover_path) if cover_path else None
    media_total = len(release.get("medium-list", [])) or 1

    for file_path, (medium, track_pos_in_medium, track) in zip(files, flat_tracks):
        _tag_file(file_path, release, medium, track_pos_in_medium, track, cover, media_total)

    return len(files)


def _tag_file(
    file_path: Path,
    release: dict,
    medium: dict,
    track_pos: int,
    track: dict,
    cover: MP4Cover | None,
    media_total: int,
) -> None:
    audio = MP4(file_path)

    # ---- Album-level MBID atoms ----
    audio[ATOM_MB_ALBUM_ID] = [release["id"].encode("utf-8")]

    if album_artist_ids := _artist_ids(release.get("artist-credit")):
        audio[ATOM_MB_ALBUM_ARTIST_ID] = [a.encode("utf-8") for a in album_artist_ids]

    if rg := release.get("release-group"):
        if rg_id := rg.get("id"):
            audio[ATOM_MB_RELEASE_GROUP_ID] = [rg_id.encode("utf-8")]
        if pt := rg.get("primary-type"):
            audio[ATOM_MB_ALBUM_TYPE] = [pt.encode("utf-8")]

    if status := release.get("status"):
        audio[ATOM_MB_ALBUM_STATUS] = [status.encode("utf-8")]
    if country := release.get("country"):
        audio[ATOM_MB_ALBUM_COUNTRY] = [country.encode("utf-8")]

    # ---- Per-track MBID atoms ----
    if recording := track.get("recording"):
        if rec_id := recording.get("id"):
            audio[ATOM_MB_TRACK_ID] = [rec_id.encode("utf-8")]

    if track_id := track.get("id"):
        audio[ATOM_MB_RELEASE_TRACK_ID] = [track_id.encode("utf-8")]

    track_artist_credit = track.get("artist-credit") or release.get("artist-credit")
    if track_artist_ids := _artist_ids(track_artist_credit):
        audio[ATOM_MB_ARTIST_ID] = [a.encode("utf-8") for a in track_artist_ids]

    # ---- Standard text tags ----
    audio["\xa9nam"] = [_track_title(track)]
    audio["\xa9alb"] = [release.get("title", "")]
    audio["\xa9ART"] = [_artist_phrase(track_artist_credit)]
    audio["aART"] = [_artist_phrase(release.get("artist-credit"))]
    if date := release.get("date"):
        audio["\xa9day"] = [date]

    track_total = len(medium.get("track-list", []))
    audio["trkn"] = [(track_pos + 1, track_total)]
    if "position" in medium:
        try:
            disc_pos = int(medium["position"])
            audio["disk"] = [(disc_pos, media_total)]
        except (TypeError, ValueError):
            pass

    # ---- Optional album-level metadata ----
    if label_info := (release.get("label-info-list") or []):
        first = label_info[0]
        if label := first.get("label", {}).get("name"):
            audio[ATOM_LABEL] = [label.encode("utf-8")]
        if catnum := first.get("catalog-number"):
            audio[ATOM_CATALOG] = [catnum.encode("utf-8")]
    if barcode := release.get("barcode"):
        audio[ATOM_BARCODE] = [barcode.encode("utf-8")]
    if asin := release.get("asin"):
        audio[ATOM_ASIN] = [asin.encode("utf-8")]
    if fmt := medium.get("format"):
        audio[ATOM_MEDIA] = [fmt.encode("utf-8")]

    # ---- Cover art ----
    if cover is not None:
        audio["covr"] = [cover]

    # ---- Cleanup of the legacy non-Picard atom ----
    if LEGACY_RELEASE_ID in audio:
        del audio[LEGACY_RELEASE_ID]

    # ©cmt is intentionally NOT touched — preserves Bandcamp URL fallback.

    audio.save()


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
    """Pull MBIDs out of an MB artist-credit list.

    artist-credit is a heterogeneous list — dicts (with `artist`) or strings
    (joinphrases). We extract just the MBIDs.
    """
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


def _load_cover(cover_path: Path) -> MP4Cover:
    data = cover_path.read_bytes()
    fmt = MP4Cover.FORMAT_PNG if cover_path.suffix.lower() == ".png" else MP4Cover.FORMAT_JPEG
    return MP4Cover(data, imageformat=fmt)
