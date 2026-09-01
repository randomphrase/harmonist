"""MP4 / M4A tag reader + writer.

Picard-compatible atom naming throughout. Custom atoms use the
`----:com.apple.iTunes:<Name>` form; standard text uses ©-prefixed
4-byte atoms (©nam, ©alb, etc., where © is U+00A9 == 0xa9).
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from mutagen.mp4 import MP4, MP4Cover

from . import quality
from .owned import Owned, as_flag
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
#: Picard's `discsubtitle` — the medium's own name (#218).
ATOM_DISC_SUBTITLE = f"{ATOM_PREFIX}DISCSUBTITLE"
ATOM_ASIN = f"{ATOM_PREFIX}ASIN"

# Per-track ISRC(s) and multi-value artists, original date, script (freeform).
ATOM_ISRC = f"{ATOM_PREFIX}ISRC"
ATOM_ARTISTS = f"{ATOM_PREFIX}ARTISTS"
ATOM_ALBUM_ARTISTS = f"{ATOM_PREFIX}ALBUMARTISTS"
# LOWER case, and that is Picard's spelling rather than a style choice (#333).
# Picard has no MP4 mapping for either tag — they are absent from its
# `__freeform_tags`, `__r_freeform_tags_ci` and `__text_tags` — so they fall
# through to the generic branch of its save, which writes the tag name
# unchanged: `tags['----:com.apple.iTunes:' + name] = values`.
#
# MP4 freeform keys are case-sensitive in mutagen, so the upper-case spelling
# Harmonist used could not see Picard's atom at all. `read_owned` reported the
# original date ABSENT on every Picard-tagged M4A, which made `owned.diff` find
# a change on every pass, so #266's write-skip never fired and the gardener
# rewrote every dated album in the library forever — #283's failure mode on a
# second field, and one a whole dogfood library sat in.
ATOM_ORIGINAL_DATE = f"{ATOM_PREFIX}originaldate"
ATOM_ORIGINAL_YEAR = f"{ATOM_PREFIX}originalyear"

#: The upper-case spellings Harmonist itself wrote before #333. Retired, not
#: renamed: they are already in users' files, and switching spelling without
#: clearing them would leave both on disk, free to diverge the moment
#: MusicBrainz corrects the date.
LEGACY_ORIGINAL_DATE = f"{ATOM_PREFIX}ORIGINALDATE"
LEGACY_ORIGINAL_YEAR = f"{ATOM_PREFIX}ORIGINALYEAR"
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

#: Picard's `compilation` — the Various Artists flag (#323). A NATIVE BOOLEAN
#: atom, unlike every other owned atom here: mutagen stores `cpil` as a bare
#: `True`/`False` rather than a list, so it goes through neither the text nor
#: the freeform tables below.
ATOM_COMPILATION = "cpil"

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
    Owned.ALBUM_ARTISTS: (ATOM_ALBUM_ARTISTS,),
    Owned.MB_ALBUM_ARTIST_IDS: (ATOM_MB_ALBUM_ARTIST_ID,),
    Owned.MB_RELEASE_GROUP_ID: (ATOM_MB_RELEASE_GROUP_ID,),
    Owned.MB_ALBUM_TYPE: (ATOM_MB_ALBUM_TYPE,),
    Owned.MB_ALBUM_STATUS: (ATOM_MB_ALBUM_STATUS,),
    Owned.MB_ALBUM_COUNTRY: (ATOM_MB_ALBUM_COUNTRY,),
    Owned.COMPILATION: (ATOM_COMPILATION,),
    Owned.DATE: (ATOM_DATE,),
    # The retired upper-case spellings ride along with the pair they were an
    # older spelling of (#333), the same way the legacy release id rides with
    # its own — so a write clears them by construction rather than as a
    # separate step, and no album ends up carrying both.
    Owned.ORIGINAL_DATE: (
        ATOM_ORIGINAL_DATE,
        ATOM_ORIGINAL_YEAR,
        LEGACY_ORIGINAL_DATE,
        LEGACY_ORIGINAL_YEAR,
    ),
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
    Owned.DISC_SUBTITLE: (ATOM_DISC_SUBTITLE,),
    Owned.MB_TRACK_ID: (ATOM_MB_TRACK_ID,),
    Owned.MB_RELEASE_TRACK_ID: (ATOM_MB_RELEASE_TRACK_ID,),
    Owned.MB_ARTIST_IDS: (ATOM_MB_ARTIST_ID,),
    Owned.ISRCS: (ATOM_ISRC,),
}

#: Atoms a write CLEARS and never writes back — an older spelling of an owned
#: field, kept here only so it can be removed. Distinct from the second atom of
#: an `OWNED_ATOMS` pair such as `ATOM_ORIGINAL_YEAR`, which a write clears and
#: then re-derives, so its presence is normal rather than something to clean up.
#:
#: The original-date pair is now on BOTH sides of that line, which is worth
#: stating plainly because it looks like a contradiction: the lower-case
#: `originalyear` is a re-derived pair member and must never be listed here,
#: while the upper-case `ORIGINALYEAR` Harmonist wrote before #333 is genuine
#: residue and must be. Same tag, two spellings, opposite dispositions.
#:
#: `_read_owned` cannot report these — that is the point of them — so a file can
#: match a release on all thirty owned fields and still carry one. Naming them
#: lets `has_superseded_tags` answer for the write-skip in `tagger.tag_album`
#: (#266), which would otherwise leave a stale legacy MBID in place forever on
#: exactly the albums #32 cares about: the ones adopted from an older Picard.
SUPERSEDED_ATOMS: tuple[str, ...] = (
    LEGACY_RELEASE_ID,
    LEGACY_ORIGINAL_DATE,
    LEGACY_ORIGINAL_YEAR,
)


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
    """The short label for what's inside the MP4 container.

    `MP4Info.codec` is an RFC 6381 string — mutagen documents it as
    `'mp4a[.*][.*]'` or `'alac'` — so AAC arrives as "mp4a.40.2" and an
    equality test against the bare "mp4a" matched nothing (#254). `alac` really
    is bare, which is why only half of this was ever wrong.

    Matched on `.40` rather than on "mp4a" alone: the suffix is
    `"%X" % objectTypeIndication`, and 0x40 is MPEG-4 Audio — the AAC family.
    Other object types in an `mp4a` box genuinely aren't AAC (`mp4a.69` and
    `mp4a.6B` are MP3 in an MP4 container), and a bare "mp4a" means no ESDS
    descriptor could be parsed at all. Both keep falling through to "MP4",
    which is the honest answer for a container whose codec we can't name.
    """
    codec = getattr(audio.info, "codec", "")
    if codec == "alac":
        return "ALAC"
    if codec.startswith("mp4a.40"):
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
    disk = audio.get(ATOM_DISC_NUM) or []
    trkn = audio.get(ATOM_TRACK_NUM) or []
    codec = _codec_label(audio)
    return ScanFields(
        album_title=_text_atom(audio, ATOM_ALBUM),
        album_id=_binary_atom_str(audio, ATOM_MB_ALBUM_ID),
        artist=_text_atom(audio, ATOM_ARTIST),
        codec=codec,
        has_cover=bool(audio.get(ATOM_COVER)),
        album_artist=_text_atom(audio, ATOM_ALBUM_ARTIST),
        disc_num=disk[0][0] if disk and disk[0] else None,
        track_total=trkn[0][1] if trkn and trkn[0] and len(trkn[0]) > 1 else None,
        disc_total=disk[0][1] if disk and disk[0] and len(disk[0]) > 1 else None,
        release_track_id=_binary_atom_str(audio, ATOM_MB_RELEASE_TRACK_ID),
        # MP4 is the one container here that holds both a lossless and a lossy
        # codec, so what its `info` means depends on which. `MP4Info` reports
        # `bits_per_sample` for AAC too, and it describes the decoder's output
        # rather than the file — see `quality`.
        quality=quality.read(audio.info, lossless=codec == "ALAC"),
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
        release_track_id=_binary_atom_str(audio, ATOM_MB_RELEASE_TRACK_ID),
        # Every owned field too, off the handle already open — so the album
        # comparison can cover all thirty tags Harmonist writes without a second
        # pass over the file (#295). Free here; a separate `read_owned` call on
        # the same request would double the page's file opens.
        owned=_read_owned(audio),
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
        Owned.ALBUM_ARTISTS: _binary_atom_list(audio, ATOM_ALBUM_ARTISTS),
        Owned.MB_ALBUM_ARTIST_IDS: _binary_atom_list(audio, ATOM_MB_ALBUM_ARTIST_ID),
        Owned.MB_RELEASE_GROUP_ID: _binary_atom_str(audio, ATOM_MB_RELEASE_GROUP_ID),
        # Multi-value: primary type then the secondaries (#331). Through the
        # LIST reader, or a Picard-tagged live album reads back as plain
        # "album" — equal to what Harmonist would write, so the difference
        # never surfaced and the next re-tag wrote the truncation back.
        Owned.MB_ALBUM_TYPE: _binary_atom_list(audio, ATOM_MB_ALBUM_TYPE),
        Owned.MB_ALBUM_STATUS: _binary_atom_str(audio, ATOM_MB_ALBUM_STATUS),
        Owned.MB_ALBUM_COUNTRY: _binary_atom_str(audio, ATOM_MB_ALBUM_COUNTRY),
        # `audio.get` returns the bare bool for `cpil`, not a list — see the
        # atom's definition above.
        Owned.COMPILATION: as_flag(audio.get(ATOM_COMPILATION)),
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
        Owned.DISC_SUBTITLE: _binary_atom_str(audio, ATOM_DISC_SUBTITLE),
        Owned.MB_TRACK_ID: _binary_atom_str(audio, ATOM_MB_TRACK_ID),
        Owned.MB_RELEASE_TRACK_ID: _binary_atom_str(audio, ATOM_MB_RELEASE_TRACK_ID),
        Owned.MB_ARTIST_IDS: _binary_atom_list(audio, ATOM_MB_ARTIST_ID),
        Owned.ISRCS: _binary_atom_list(audio, ATOM_ISRC),
    }


def read_owned(path: Path) -> dict[str, Any]:
    """Every owned field as it currently stands on disk (#157's undo).

    Raises rather than returning blanks when the file can't be opened — see
    `mp3.read_owned`.
    """
    return _read_owned(MP4(path))


def has_superseded_tags(path: Path) -> bool:
    """Whether `path` carries an atom a write would remove and not write back.

    An unopenable file answers False: the caller is about to write it and will
    raise there if it really can't be read.
    """
    audio = _open(path)
    return audio is not None and any(atom in audio for atom in SUPERSEDED_ATOMS)


def write_owned(path: Path, values: Mapping[str, Any]) -> dict[str, Any]:
    """Set every owned field to `values`, removing those absent (#157's undo).

    `values` is a COMPLETE owned snapshot shaped like `_read_owned`'s result,
    not a patch — `trkn` and `disk` each pack a (number, total) pair into one
    atom, so writing half of one without the other in hand would drop the other
    half. See `mp3.write_owned` for why this is separate from `write_tags`.
    """
    audio = MP4(path)
    before = _read_owned(audio)

    for atoms in OWNED_ATOMS.values():
        for atom in atoms:
            if atom in audio:
                del audio[atom]
    _apply_owned(audio, values)

    # ATOM_COMMENT and ATOM_COVER untouched, as in `write_tags`.
    audio.save()
    return before


def _apply_owned(audio: MP4, values: Mapping[str, Any]) -> None:
    """Write an owned snapshot into already-cleared MP4 atoms.

    Each field lands in the atom `OWNED_ATOMS` and `_read_owned` name for it; a
    test writes the same values through here and through `write_tags` and
    asserts both read back identically, so the two paths cannot drift apart.
    """
    for fld, atom in _TEXT_ATOMS.items():
        if (value := values.get(fld)) not in (None, ""):
            audio[atom] = [str(value)]
    for fld, atom in _BINARY_ATOMS.items():
        if (value := values.get(fld)) not in (None, ""):
            audio[atom] = [str(value).encode("utf-8")]
    for fld, atom in _BINARY_LIST_ATOMS.items():
        if value := values.get(fld):
            audio[atom] = [str(v).encode("utf-8") for v in value]

    # A native boolean atom, so it belongs to none of the three tables above —
    # writing it through them would put the string "True" in the file.
    if values.get(Owned.COMPILATION):
        audio[ATOM_COMPILATION] = True

    # ORIGINALYEAR is derived rather than stored: `_read_owned` reads only
    # ORIGINALDATE, so writing the year from anywhere else could disagree with
    # the date beside it.
    if original := values.get(Owned.ORIGINAL_DATE):
        audio[ATOM_ORIGINAL_YEAR] = [str(original)[:4].encode("utf-8")]

    _set_pair(audio, ATOM_TRACK_NUM, values.get(Owned.TRACK_NUM), values.get(Owned.TRACK_TOTAL))
    _set_pair(audio, ATOM_DISC_NUM, values.get(Owned.DISC_NUM), values.get(Owned.DISC_TOTAL))


def _set_pair(audio: MP4, atom: str, num: Any, total: Any) -> None:
    """Write MP4's packed (number, total) atoms.

    A total with no number writes nothing — `_read_owned` takes the number from
    the same tuple, so a pair with no number can't round-trip. mutagen requires
    both halves to be ints, so a missing total is written as 0, which is how it
    reads an absent total back.
    """
    if num is None:
        return
    audio[atom] = [(int(num), int(total) if total is not None else 0)]


#: Owned fields stored as plain MP4 text atoms.
_TEXT_ATOMS: dict[Owned, str] = {
    Owned.ALBUM: ATOM_ALBUM,
    Owned.ALBUM_ARTIST: ATOM_ALBUM_ARTIST,
    Owned.ALBUM_ARTIST_SORT: ATOM_ALBUM_ARTIST_SORT,
    Owned.DATE: ATOM_DATE,
    Owned.TITLE: ATOM_TITLE,
    Owned.ARTIST: ATOM_ARTIST,
    Owned.ARTIST_SORT: ATOM_ARTIST_SORT,
}

#: Owned fields stored as single-valued freeform (`----:`) atoms, UTF-8 bytes.
_BINARY_ATOMS: dict[Owned, str] = {
    Owned.MB_ALBUM_ID: ATOM_MB_ALBUM_ID,
    Owned.MB_RELEASE_GROUP_ID: ATOM_MB_RELEASE_GROUP_ID,
    Owned.MB_ALBUM_STATUS: ATOM_MB_ALBUM_STATUS,
    Owned.MB_ALBUM_COUNTRY: ATOM_MB_ALBUM_COUNTRY,
    Owned.ORIGINAL_DATE: ATOM_ORIGINAL_DATE,
    Owned.SCRIPT: ATOM_SCRIPT,
    Owned.LABEL: ATOM_LABEL,
    Owned.CATALOG_NUMBER: ATOM_CATALOG,
    Owned.BARCODE: ATOM_BARCODE,
    Owned.ASIN: ATOM_ASIN,
    Owned.MEDIA: ATOM_MEDIA,
    Owned.DISC_SUBTITLE: ATOM_DISC_SUBTITLE,
    Owned.MB_TRACK_ID: ATOM_MB_TRACK_ID,
    Owned.MB_RELEASE_TRACK_ID: ATOM_MB_RELEASE_TRACK_ID,
}

#: Owned fields stored as multi-valued freeform atoms.
_BINARY_LIST_ATOMS: dict[Owned, str] = {
    # Multi-value since #331: the primary type plus the secondaries.
    Owned.MB_ALBUM_TYPE: ATOM_MB_ALBUM_TYPE,
    Owned.ALBUM_ARTISTS: ATOM_ALBUM_ARTISTS,
    Owned.MB_ALBUM_ARTIST_IDS: ATOM_MB_ALBUM_ARTIST_ID,
    Owned.ARTISTS: ATOM_ARTISTS,
    Owned.MB_ARTIST_IDS: ATOM_MB_ARTIST_ID,
    Owned.ISRCS: ATOM_ISRC,
}


def _cover_atom(cover: bytes) -> MP4Cover:
    fmt = MP4Cover.FORMAT_PNG if cover[:4] == b"\x89PNG" else MP4Cover.FORMAT_JPEG
    return MP4Cover(cover, imageformat=fmt)


def write_cover(path: Path, cover: bytes) -> None:
    """Replace the embedded image, touching nothing else (#131's restore)."""
    audio = MP4(path)
    audio[ATOM_COVER] = [_cover_atom(cover)]
    audio.save()


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
        audio[ATOM_MB_ALBUM_TYPE] = [t.encode("utf-8") for t in tagset.mb_album_type]
    if tagset.mb_album_status:
        audio[ATOM_MB_ALBUM_STATUS] = [tagset.mb_album_status.encode("utf-8")]
    if tagset.mb_album_country:
        audio[ATOM_MB_ALBUM_COUNTRY] = [tagset.mb_album_country.encode("utf-8")]
    # Written only when true — absence IS "not a compilation", so an album that
    # stops being one has the atom removed by the clear above (#149).
    if tagset.compilation:
        audio[ATOM_COMPILATION] = True

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
    if tagset.album_artists:
        audio[ATOM_ALBUM_ARTISTS] = [a.encode("utf-8") for a in tagset.album_artists]
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
    if tagset.disc_subtitle:
        audio[ATOM_DISC_SUBTITLE] = [tagset.disc_subtitle.encode("utf-8")]
    if tagset.original_date:
        audio[ATOM_ORIGINAL_DATE] = [tagset.original_date.encode("utf-8")]
        audio[ATOM_ORIGINAL_YEAR] = [tagset.original_date[:4].encode("utf-8")]
    if tagset.script:
        audio[ATOM_SCRIPT] = [tagset.script.encode("utf-8")]

    # ---- Cover art ----
    if cover is not None:
        audio[ATOM_COVER] = [_cover_atom(cover)]

    # The legacy atom is cleared with the owned set above, not here.
    # ATOM_COMMENT is intentionally NOT touched.

    audio.save()
    return before
