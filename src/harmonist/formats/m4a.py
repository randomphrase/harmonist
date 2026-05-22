"""MP4 / M4A tag reader + writer.

Picard-compatible atom naming throughout. Custom atoms use the
`----:com.apple.iTunes:<Name>` form; standard text uses ©-prefixed
4-byte atoms (©nam, ©alb, etc., where © is U+00A9 == 0xa9).
"""

from __future__ import annotations

from pathlib import Path

from mutagen.mp4 import MP4, MP4Cover

from .types import TagSet

EXTENSIONS = (".m4a", ".mp4")


ATOM_PREFIX = "----:com.apple.iTunes:"

# Album-level MB IDs
ATOM_MB_ALBUM_ID = f"{ATOM_PREFIX}MusicBrainz Album Id"
ATOM_MB_ALBUM_ARTIST_ID = f"{ATOM_PREFIX}MusicBrainz Album Artist Id"
ATOM_MB_RELEASE_GROUP_ID = f"{ATOM_PREFIX}MusicBrainz Release Group Id"
ATOM_MB_ALBUM_TYPE = f"{ATOM_PREFIX}MusicBrainz Album Type"
ATOM_MB_ALBUM_STATUS = f"{ATOM_PREFIX}MusicBrainz Album Status"
ATOM_MB_ALBUM_COUNTRY = f"{ATOM_PREFIX}MusicBrainz Album Release Country"

# Per-track MB IDs
ATOM_MB_TRACK_ID = f"{ATOM_PREFIX}MusicBrainz Track Id"
ATOM_MB_RELEASE_TRACK_ID = f"{ATOM_PREFIX}MusicBrainz Release Track Id"
ATOM_MB_ARTIST_ID = f"{ATOM_PREFIX}MusicBrainz Artist Id"

# Optional album-level metadata
ATOM_LABEL = f"{ATOM_PREFIX}LABEL"
ATOM_CATALOG = f"{ATOM_PREFIX}CATALOGNUMBER"
ATOM_BARCODE = f"{ATOM_PREFIX}BARCODE"
ATOM_MEDIA = f"{ATOM_PREFIX}MEDIA"
ATOM_ASIN = f"{ATOM_PREFIX}ASIN"

# Legacy (non-Picard) atom written by older versions; removed on retag.
LEGACY_RELEASE_ID = f"{ATOM_PREFIX}MUSICBRAINZ_RELEASEID"

# Standard text atoms
ATOM_TITLE = "\xa9nam"
ATOM_ALBUM = "\xa9alb"
ATOM_ARTIST = "\xa9ART"
ATOM_ALBUM_ARTIST = "aART"
ATOM_DATE = "\xa9day"
ATOM_GENRE = "\xa9gen"
ATOM_COMMENT = "\xa9cmt"

# Numeric / binary
ATOM_TRACK_NUM = "trkn"
ATOM_DISC_NUM = "disk"
ATOM_COVER = "covr"


# ---------------------------------------------------------------------------
# Read helpers
# ---------------------------------------------------------------------------


def _open(path: Path) -> MP4 | None:
    try:
        return MP4(path)
    except Exception:
        return None


def _text_atom(audio: MP4, atom: str) -> str | None:
    value = audio.get(atom) or []
    if not value:
        return None
    v = value[0]
    return v if isinstance(v, str) else None


def _binary_atom_str(audio: MP4, atom: str) -> str | None:
    value = audio.get(atom)
    if not value:
        return None
    try:
        return value[0].decode("utf-8")
    except (AttributeError, UnicodeDecodeError):
        return None


def read_album_id(path: Path) -> str | None:
    audio = _open(path)
    return _binary_atom_str(audio, ATOM_MB_ALBUM_ID) if audio else None


def read_album_title(path: Path) -> str | None:
    audio = _open(path)
    return _text_atom(audio, ATOM_ALBUM) if audio else None


def read_artist(path: Path) -> str | None:
    audio = _open(path)
    return _text_atom(audio, ATOM_ARTIST) if audio else None


def read_track_title(path: Path) -> str | None:
    audio = _open(path)
    return _text_atom(audio, ATOM_TITLE) if audio else None


def read_comment(path: Path) -> str | None:
    audio = _open(path)
    return _text_atom(audio, ATOM_COMMENT) if audio else None


def read_duration_ms(path: Path) -> int | None:
    audio = _open(path)
    if audio is None or not audio.info.length:
        return None
    return int(round(audio.info.length * 1000))


def describe(path: Path) -> str:
    """Short codec label. MP4 is a container — distinguish lossless ALAC
    from lossy AAC so it confirms the user's download-format choice."""
    audio = _open(path)
    codec = getattr(audio.info, "codec", "") if audio else ""
    if codec == "alac":
        return "ALAC"
    if codec == "mp4a":
        return "AAC"
    return "MP4"


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------


def write_tags(path: Path, tagset: TagSet, cover: bytes | None) -> None:
    """Serialise the TagSet to MP4 atoms on `path`, plus optional cover.

    The comment atom (`©cmt`) is intentionally NOT touched here so the
    Bandcamp-URL fallback the user may have placed there survives a retag.
    """
    audio = MP4(path)

    # ---- Album-level MBID atoms ----
    audio[ATOM_MB_ALBUM_ID] = [tagset.mb_album_id.encode("utf-8")]
    if tagset.mb_album_artist_ids:
        audio[ATOM_MB_ALBUM_ARTIST_ID] = [a.encode("utf-8") for a in tagset.mb_album_artist_ids]
    if tagset.mb_release_group_id:
        audio[ATOM_MB_RELEASE_GROUP_ID] = [tagset.mb_release_group_id.encode("utf-8")]
    if tagset.mb_album_type:
        audio[ATOM_MB_ALBUM_TYPE] = [tagset.mb_album_type.encode("utf-8")]
    if tagset.mb_album_status:
        audio[ATOM_MB_ALBUM_STATUS] = [tagset.mb_album_status.encode("utf-8")]
    if tagset.mb_album_country:
        audio[ATOM_MB_ALBUM_COUNTRY] = [tagset.mb_album_country.encode("utf-8")]

    # ---- Per-track MBID atoms ----
    if tagset.mb_track_id:
        audio[ATOM_MB_TRACK_ID] = [tagset.mb_track_id.encode("utf-8")]
    if tagset.mb_release_track_id:
        audio[ATOM_MB_RELEASE_TRACK_ID] = [tagset.mb_release_track_id.encode("utf-8")]
    if tagset.mb_artist_ids:
        audio[ATOM_MB_ARTIST_ID] = [a.encode("utf-8") for a in tagset.mb_artist_ids]

    # ---- Standard text tags ----
    audio[ATOM_TITLE] = [tagset.title]
    audio[ATOM_ALBUM] = [tagset.album]
    audio[ATOM_ARTIST] = [tagset.artist]
    audio[ATOM_ALBUM_ARTIST] = [tagset.album_artist]
    if tagset.date:
        audio[ATOM_DATE] = [tagset.date]

    # ---- Numeric position ----
    audio[ATOM_TRACK_NUM] = [(tagset.track_num, tagset.track_total)]
    audio[ATOM_DISC_NUM] = [(tagset.disc_num, tagset.disc_total)]

    # ---- Optional album-level metadata ----
    if tagset.label:
        audio[ATOM_LABEL] = [tagset.label.encode("utf-8")]
    if tagset.catalog_number:
        audio[ATOM_CATALOG] = [tagset.catalog_number.encode("utf-8")]
    if tagset.barcode:
        audio[ATOM_BARCODE] = [tagset.barcode.encode("utf-8")]
    if tagset.asin:
        audio[ATOM_ASIN] = [tagset.asin.encode("utf-8")]
    if tagset.media:
        audio[ATOM_MEDIA] = [tagset.media.encode("utf-8")]

    # ---- Cover art ----
    if cover is not None:
        fmt = MP4Cover.FORMAT_PNG if cover[:4] == b"\x89PNG" else MP4Cover.FORMAT_JPEG
        audio[ATOM_COVER] = [MP4Cover(cover, imageformat=fmt)]

    # ---- Cleanup legacy atom ----
    if LEGACY_RELEASE_ID in audio:
        del audio[LEGACY_RELEASE_ID]

    # ATOM_COMMENT is intentionally NOT touched.

    audio.save()
