"""MP4 / M4A tag reader + writer.

Picard-compatible atom naming throughout. Custom atoms use the
`----:com.apple.iTunes:<Name>` form; standard text uses ©-prefixed
4-byte atoms (©nam, ©alb, etc., where © is U+00A9 == 0xa9).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from mutagen.mp4 import MP4, MP4Cover

from .owned import Owned
from .types import ScanFields, TagSet, TrackTags

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

# Per-track ISRC(s) and multi-value artists, original date, script (freeform).
ATOM_ISRC = f"{ATOM_PREFIX}ISRC"
ATOM_ARTISTS = f"{ATOM_PREFIX}ARTISTS"
ATOM_ORIGINAL_DATE = f"{ATOM_PREFIX}ORIGINALDATE"
ATOM_ORIGINAL_YEAR = f"{ATOM_PREFIX}ORIGINALYEAR"
ATOM_SCRIPT = f"{ATOM_PREFIX}SCRIPT"

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

# Native sort-name atoms (Picard maps artistsort/albumartistsort here).
ATOM_ARTIST_SORT = "soar"
ATOM_ALBUM_ARTIST_SORT = "soaa"

# Numeric / binary
ATOM_TRACK_NUM = "trkn"
ATOM_DISC_NUM = "disk"
ATOM_COVER = "covr"


# The MP4 atoms behind each owned field (#149). ATOM_COMMENT is absent so a
# recovered Bandcamp URL survives a retag, ATOM_GENRE because Harmonist doesn't
# write one (#12), and ATOM_COVER because per-track artwork is preserved
# deliberately — see `owned.py`.
OWNED_ATOMS: dict[Owned, tuple[str, ...]] = {
    # The legacy atom rides along with the id it was an older spelling of, so
    # the cleanup happens by construction rather than as a separate step.
    Owned.MB_ALBUM_ID: (ATOM_MB_ALBUM_ID, LEGACY_RELEASE_ID),
    Owned.ALBUM: (ATOM_ALBUM,),
    Owned.ALBUM_ARTIST: (ATOM_ALBUM_ARTIST,),
    Owned.ALBUM_ARTIST_SORT: (ATOM_ALBUM_ARTIST_SORT,),
    Owned.MB_ALBUM_ARTIST_IDS: (ATOM_MB_ALBUM_ARTIST_ID,),
    Owned.MB_RELEASE_GROUP_ID: (ATOM_MB_RELEASE_GROUP_ID,),
    Owned.MB_ALBUM_TYPE: (ATOM_MB_ALBUM_TYPE,),
    Owned.MB_ALBUM_STATUS: (ATOM_MB_ALBUM_STATUS,),
    Owned.MB_ALBUM_COUNTRY: (ATOM_MB_ALBUM_COUNTRY,),
    Owned.DATE: (ATOM_DATE,),
    Owned.ORIGINAL_DATE: (ATOM_ORIGINAL_DATE, ATOM_ORIGINAL_YEAR),
    Owned.SCRIPT: (ATOM_SCRIPT,),
    Owned.LABEL: (ATOM_LABEL,),
    Owned.CATALOG_NUMBER: (ATOM_CATALOG,),
    Owned.BARCODE: (ATOM_BARCODE,),
    Owned.ASIN: (ATOM_ASIN,),
    # `trkn` and `disk` each carry the (number, total) pair in one atom, so two
    # owned fields map to the same atom. Clearing is idempotent, and both are
    # always written together below.
    Owned.DISC_TOTAL: (ATOM_DISC_NUM,),
    Owned.TITLE: (ATOM_TITLE,),
    Owned.ARTIST: (ATOM_ARTIST,),
    Owned.ARTIST_SORT: (ATOM_ARTIST_SORT,),
    Owned.ARTISTS: (ATOM_ARTISTS,),
    Owned.TRACK_NUM: (ATOM_TRACK_NUM,),
    Owned.TRACK_TOTAL: (ATOM_TRACK_NUM,),
    Owned.DISC_NUM: (ATOM_DISC_NUM,),
    Owned.MEDIA: (ATOM_MEDIA,),
    Owned.MB_TRACK_ID: (ATOM_MB_TRACK_ID,),
    Owned.MB_RELEASE_TRACK_ID: (ATOM_MB_RELEASE_TRACK_ID,),
    Owned.MB_ARTIST_IDS: (ATOM_MB_ARTIST_ID,),
    Owned.ISRCS: (ATOM_ISRC,),
}


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
        decoded: str = value[0].decode("utf-8")
        return decoded
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
    ms: int = round(audio.info.length * 1000)
    return ms


def _codec_label(audio: MP4) -> str:
    codec = getattr(audio.info, "codec", "")
    if codec == "alac":
        return "ALAC"
    if codec == "mp4a":
        return "AAC"
    return "MP4"


def describe(path: Path) -> str:
    """Short codec label. MP4 is a container — distinguish lossless ALAC
    from lossy AAC so it confirms the user's download-format choice."""
    audio = _open(path)
    return _codec_label(audio) if audio else "MP4"


def read_scan_fields(path: Path) -> ScanFields:
    """All scanner-needed fields in one open (album, MB album id, artist, codec)."""
    audio = _open(path)
    if audio is None:
        # Flagged, not silently blank: an unopenable file must not read as an
        # untagged one (#112).
        return ScanFields(None, None, None, None, unreadable=True)
    return ScanFields(
        album_title=_text_atom(audio, ATOM_ALBUM),
        album_id=_binary_atom_str(audio, ATOM_MB_ALBUM_ID),
        artist=_text_atom(audio, ATOM_ARTIST),
        codec=_codec_label(audio),
        has_cover=bool(audio.get(ATOM_COVER)),
        album_artist=_text_atom(audio, ATOM_ALBUM_ARTIST),
    )


def read_tags(path: Path) -> TrackTags:
    """Everything the album comparison needs from one file, in a single open."""
    audio = _open(path)
    if audio is None:
        return TrackTags(unreadable=True)
    trkn = audio.get(ATOM_TRACK_NUM) or []
    disk = audio.get(ATOM_DISC_NUM) or []
    return TrackTags(
        album=_text_atom(audio, ATOM_ALBUM),
        album_artist=_text_atom(audio, ATOM_ALBUM_ARTIST),
        date=_text_atom(audio, ATOM_DATE),
        # Freeform (----) atoms, so they come back as bytes.
        label=_binary_atom_str(audio, ATOM_LABEL),
        catalog_number=_binary_atom_str(audio, ATOM_CATALOG),
        barcode=_binary_atom_str(audio, ATOM_BARCODE),
        media=_binary_atom_str(audio, ATOM_MEDIA),
        genre=_text_atom(audio, ATOM_GENRE),
        title=_text_atom(audio, ATOM_TITLE),
        artist=_text_atom(audio, ATOM_ARTIST),
        track_num=trkn[0][0] if trkn and trkn[0] else None,
        disc_num=disk[0][0] if disk and disk[0] else None,
        duration_ms=int(audio.info.length * 1000) if audio.info else None,
        comment=_text_atom(audio, ATOM_COMMENT),
    )


def read_cover(path: Path) -> tuple[bytes, str] | None:
    """Extract the embedded cover art as (image_bytes, mime), or None."""
    audio = _open(path)
    if audio is None:
        return None
    covers = audio.get(ATOM_COVER)
    if not covers:
        return None
    cover = covers[0]
    is_png = getattr(cover, "imageformat", None) == MP4Cover.FORMAT_PNG
    return bytes(cover), ("image/png" if is_png else "image/jpeg")


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------


def _binary_atom_list(audio: MP4, atom: str) -> list[str]:
    """Every value of a freeform (`----`) atom, decoded. Freeform atoms come
    back as bytes and may legitimately repeat — ARTISTS, ISRC and the artist-id
    atoms are all multi-valued."""
    out: list[str] = []
    for value in audio.get(atom) or []:
        try:
            out.append(bytes(value).decode("utf-8"))
        except (AttributeError, UnicodeDecodeError):
            continue
    return out


def _read_owned(audio: MP4) -> dict[str, Any]:
    """The current value of every owned field, shaped exactly like the matching
    `TagSet` attribute (#86).

    Shape matters more than it looks: these values are diffed straight against
    the TagSet about to be written, so a track number read as "5" when the
    TagSet holds `5` would report a change on every re-tag forever.
    """
    trkn = (audio.get(ATOM_TRACK_NUM) or [(None, None)])[0]
    disk = (audio.get(ATOM_DISC_NUM) or [(None, None)])[0]
    return {
        Owned.MB_ALBUM_ID: _binary_atom_str(audio, ATOM_MB_ALBUM_ID),
        Owned.ALBUM: _text_atom(audio, ATOM_ALBUM),
        Owned.ALBUM_ARTIST: _text_atom(audio, ATOM_ALBUM_ARTIST),
        Owned.ALBUM_ARTIST_SORT: _text_atom(audio, ATOM_ALBUM_ARTIST_SORT),
        Owned.MB_ALBUM_ARTIST_IDS: _binary_atom_list(audio, ATOM_MB_ALBUM_ARTIST_ID),
        Owned.MB_RELEASE_GROUP_ID: _binary_atom_str(audio, ATOM_MB_RELEASE_GROUP_ID),
        Owned.MB_ALBUM_TYPE: _binary_atom_str(audio, ATOM_MB_ALBUM_TYPE),
        Owned.MB_ALBUM_STATUS: _binary_atom_str(audio, ATOM_MB_ALBUM_STATUS),
        Owned.MB_ALBUM_COUNTRY: _binary_atom_str(audio, ATOM_MB_ALBUM_COUNTRY),
        Owned.DATE: _text_atom(audio, ATOM_DATE),
        Owned.ORIGINAL_DATE: _binary_atom_str(audio, ATOM_ORIGINAL_DATE),
        Owned.SCRIPT: _binary_atom_str(audio, ATOM_SCRIPT),
        Owned.LABEL: _binary_atom_str(audio, ATOM_LABEL),
        Owned.CATALOG_NUMBER: _binary_atom_str(audio, ATOM_CATALOG),
        Owned.BARCODE: _binary_atom_str(audio, ATOM_BARCODE),
        Owned.ASIN: _binary_atom_str(audio, ATOM_ASIN),
        Owned.DISC_TOTAL: disk[1] if disk else None,
        Owned.TITLE: _text_atom(audio, ATOM_TITLE),
        Owned.ARTIST: _text_atom(audio, ATOM_ARTIST),
        Owned.ARTIST_SORT: _text_atom(audio, ATOM_ARTIST_SORT),
        Owned.ARTISTS: _binary_atom_list(audio, ATOM_ARTISTS),
        Owned.TRACK_NUM: trkn[0] if trkn else None,
        Owned.TRACK_TOTAL: trkn[1] if trkn else None,
        Owned.DISC_NUM: disk[0] if disk else None,
        Owned.MEDIA: _binary_atom_str(audio, ATOM_MEDIA),
        Owned.MB_TRACK_ID: _binary_atom_str(audio, ATOM_MB_TRACK_ID),
        Owned.MB_RELEASE_TRACK_ID: _binary_atom_str(audio, ATOM_MB_RELEASE_TRACK_ID),
        Owned.MB_ARTIST_IDS: _binary_atom_list(audio, ATOM_MB_ARTIST_ID),
        Owned.ISRCS: _binary_atom_list(audio, ATOM_ISRC),
    }


def write_tags(path: Path, tagset: TagSet, cover: bytes | None) -> dict[str, Any]:
    """Serialise the TagSet to MP4 atoms on `path`, plus optional cover.

    Returns the owned fields as they were BEFORE the write, read from the handle
    this function already holds — so the tagging audit (#86) gets its before
    state without a second open of every file.

    The comment atom (`©cmt`) is intentionally NOT touched here so the
    Bandcamp-URL fallback the user may have placed there survives a retag.
    """
    audio = MP4(path)
    before = _read_owned(audio)

    # Clear every owned atom before writing, so a field absent from this TagSet
    # is REMOVED rather than left stale from a previous tagging (#149). Anything
    # not owned — the comment, genre, arbitrary atoms, the cover — is untouched.
    for atoms in OWNED_ATOMS.values():
        for atom in atoms:
            if atom in audio:
                del audio[atom]

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
    if tagset.isrcs:
        audio[ATOM_ISRC] = [code.encode("utf-8") for code in tagset.isrcs]

    # ---- Standard text tags ----
    audio[ATOM_TITLE] = [tagset.title]
    audio[ATOM_ALBUM] = [tagset.album]
    audio[ATOM_ARTIST] = [tagset.artist]
    audio[ATOM_ALBUM_ARTIST] = [tagset.album_artist]
    if tagset.date:
        audio[ATOM_DATE] = [tagset.date]
    if tagset.artist_sort:
        audio[ATOM_ARTIST_SORT] = [tagset.artist_sort]
    if tagset.album_artist_sort:
        audio[ATOM_ALBUM_ARTIST_SORT] = [tagset.album_artist_sort]
    if tagset.artists:
        audio[ATOM_ARTISTS] = [a.encode("utf-8") for a in tagset.artists]

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
    if tagset.original_date:
        audio[ATOM_ORIGINAL_DATE] = [tagset.original_date.encode("utf-8")]
        audio[ATOM_ORIGINAL_YEAR] = [tagset.original_date[:4].encode("utf-8")]
    if tagset.script:
        audio[ATOM_SCRIPT] = [tagset.script.encode("utf-8")]

    # ---- Cover art ----
    if cover is not None:
        fmt = MP4Cover.FORMAT_PNG if cover[:4] == b"\x89PNG" else MP4Cover.FORMAT_JPEG
        audio[ATOM_COVER] = [MP4Cover(cover, imageformat=fmt)]

    # The legacy atom is cleared with the owned set above, not here.
    # ATOM_COMMENT is intentionally NOT touched.

    audio.save()
    return before
