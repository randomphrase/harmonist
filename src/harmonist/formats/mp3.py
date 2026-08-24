"""MP3 / ID3v2 tag reader + writer.

Follows the MusicBrainz Picard ID3v2 mapping
(https://picard.musicbrainz.org/docs/mappings/):

  - MB IDs go in `TXXX:MusicBrainz <Name>` user-text frames, except the
    recording (track) MBID which uses the dedicated `UFID` frame with
    owner `http://musicbrainz.org`.
  - Standard metadata uses the canonical frames (TIT2, TALB, TPE1, …).
  - Cover art is an `APIC` frame (type = front cover).

The comment (`COMM`) frame is left untouched on write so a Bandcamp URL
recovered into it survives a retag — mirrors the M4A behaviour.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from mutagen.id3 import (
    APIC,
    ID3,
    TALB,
    TDOR,
    TDRC,
    TIT2,
    TMED,
    TPE1,
    TPE2,
    TPOS,
    TPUB,
    TRCK,
    TSO2,
    TSOP,
    TSRC,
    TSST,
    TXXX,
    UFID,
    Encoding,
    PictureType,
)
from mutagen.mp3 import MP3

from . import quality
from .owned import Owned
from .types import ScanFields, TagSet, TrackTags

EXTENSIONS = (".mp3",)

UFID_OWNER = "http://musicbrainz.org"

# TXXX description suffixes for the MB-ID user-text frames.
TXXX_ALBUM_ID = "MusicBrainz Album Id"
TXXX_ALBUM_ARTIST_ID = "MusicBrainz Album Artist Id"
TXXX_RELEASE_GROUP_ID = "MusicBrainz Release Group Id"
TXXX_ALBUM_TYPE = "MusicBrainz Album Type"
TXXX_ALBUM_STATUS = "MusicBrainz Album Status"
TXXX_ALBUM_COUNTRY = "MusicBrainz Album Release Country"
TXXX_RELEASE_TRACK_ID = "MusicBrainz Release Track Id"
TXXX_ARTIST_ID = "MusicBrainz Artist Id"
TXXX_CATALOG = "CATALOGNUMBER"
TXXX_BARCODE = "BARCODE"
TXXX_ASIN = "ASIN"
TXXX_ARTISTS = "ARTISTS"
TXXX_SCRIPT = "SCRIPT"


# The ID3 frames behind each owned field (#149), by mutagen HashKey — which for
# a user-text frame is "TXXX:<description>" and for the recording MBID is
# "UFID:<owner>". COMM is absent so a recovered Bandcamp URL survives a retag,
# TCON because Harmonist doesn't write a genre (#12), and APIC because per-track
# artwork is preserved deliberately — see `owned.py`.
#
# This table is the single definition the read and write paths share. They
# disagreed before it existed: `media` was written to TMED and read back from
# TXXX:MEDIA, so it never round-tripped.
OWNED_FRAMES: dict[Owned, tuple[str, ...]] = {
    Owned.MB_ALBUM_ID: (f"TXXX:{TXXX_ALBUM_ID}",),
    Owned.ALBUM: ("TALB",),
    Owned.ALBUM_ARTIST: ("TPE2",),
    Owned.ALBUM_ARTIST_SORT: ("TSO2",),
    Owned.MB_ALBUM_ARTIST_IDS: (f"TXXX:{TXXX_ALBUM_ARTIST_ID}",),
    Owned.MB_RELEASE_GROUP_ID: (f"TXXX:{TXXX_RELEASE_GROUP_ID}",),
    Owned.MB_ALBUM_TYPE: (f"TXXX:{TXXX_ALBUM_TYPE}",),
    Owned.MB_ALBUM_STATUS: (f"TXXX:{TXXX_ALBUM_STATUS}",),
    Owned.MB_ALBUM_COUNTRY: (f"TXXX:{TXXX_ALBUM_COUNTRY}",),
    Owned.DATE: ("TDRC",),
    Owned.ORIGINAL_DATE: ("TDOR",),
    Owned.SCRIPT: (f"TXXX:{TXXX_SCRIPT}",),
    Owned.LABEL: ("TPUB",),
    Owned.CATALOG_NUMBER: (f"TXXX:{TXXX_CATALOG}",),
    Owned.BARCODE: (f"TXXX:{TXXX_BARCODE}",),
    Owned.ASIN: (f"TXXX:{TXXX_ASIN}",),
    # TRCK and TPOS each carry "n/total" in one frame, so two owned fields map
    # to the same frame. Clearing is idempotent, and both are always written
    # together below.
    Owned.DISC_TOTAL: ("TPOS",),
    Owned.TITLE: ("TIT2",),
    Owned.ARTIST: ("TPE1",),
    Owned.ARTIST_SORT: ("TSOP",),
    Owned.ARTISTS: (f"TXXX:{TXXX_ARTISTS}",),
    Owned.TRACK_NUM: ("TRCK",),
    Owned.TRACK_TOTAL: ("TRCK",),
    Owned.DISC_NUM: ("TPOS",),
    Owned.MEDIA: ("TMED",),
    Owned.DISC_SUBTITLE: ("TSST",),
    Owned.MB_TRACK_ID: (f"UFID:{UFID_OWNER}",),
    Owned.MB_RELEASE_TRACK_ID: (f"TXXX:{TXXX_RELEASE_TRACK_ID}",),
    Owned.MB_ARTIST_IDS: (f"TXXX:{TXXX_ARTIST_ID}",),
    Owned.ISRCS: ("TSRC",),
}


# ---------------------------------------------------------------------------
# Read helpers
# ---------------------------------------------------------------------------


def _open(path: Path) -> MP3 | None:
    try:
        return MP3(path)
    except Exception:
        return None


def _txxx(tags: Any, desc: str) -> str | None:
    if tags is None:
        return None
    frame = tags.get(f"TXXX:{desc}")
    if frame is None or not frame.text:
        return None
    return str(frame.text[0]) or None


def _text(tags: Any, frame_id: str) -> str | None:
    if tags is None:
        return None
    frame = tags.get(frame_id)
    if frame is None or not frame.text:
        return None
    return str(frame.text[0]) or None


def read_album_id(path: Path) -> str | None:
    audio = _open(path)
    return _txxx(audio.tags, TXXX_ALBUM_ID) if audio else None


def read_album_title(path: Path) -> str | None:
    audio = _open(path)
    return _text(audio.tags, "TALB") if audio else None


def read_artist(path: Path) -> str | None:
    audio = _open(path)
    return _text(audio.tags, "TPE1") if audio else None


def read_track_title(path: Path) -> str | None:
    audio = _open(path)
    return _text(audio.tags, "TIT2") if audio else None


def read_comment(path: Path) -> str | None:
    audio = _open(path)
    if audio is None or audio.tags is None:
        return None
    comms = audio.tags.getall("COMM")
    for c in comms:
        if c.text and c.text[0]:
            return str(c.text[0])
    return None


def read_duration_ms(path: Path) -> int | None:
    audio = _open(path)
    if audio is None or not audio.info.length:
        return None
    ms: int = round(audio.info.length * 1000)
    return ms


def describe(path: Path) -> str:
    return "MP3"


def read_scan_fields(path: Path) -> ScanFields:
    """All scanner-needed fields in one open (album, MB album id, artist, codec)."""
    audio = _open(path)
    if audio is None:
        # Flagged, not silently blank: an unopenable file must not read as an
        # untagged one (#112).
        return ScanFields(None, None, None, "MP3", unreadable=True)
    tags = audio.tags
    return ScanFields(
        album_title=_text(tags, "TALB"),
        album_id=_txxx(tags, TXXX_ALBUM_ID),
        artist=_text(tags, "TPE1"),
        codec="MP3",
        has_cover=bool(tags and tags.getall("APIC")),
        album_artist=_text(tags, "TPE2"),
        # TRCK / TPOS are "n" or "n/total" — both halves are wanted here.
        disc_num=_first_int(_text(tags, "TPOS")),
        track_total=_total_int(_text(tags, "TRCK")),
        disc_total=_total_int(_text(tags, "TPOS")),
        release_track_id=_txxx(tags, TXXX_RELEASE_TRACK_ID),
        quality=quality.read(audio.info, lossless=False),
    )


def read_tags(path: Path) -> TrackTags:
    """Everything the album comparison needs from one file, in a single open."""
    audio = _open(path)
    if audio is None:
        return TrackTags(unreadable=True)
    tags = audio.tags
    track_num = _text(tags, "TRCK")
    return TrackTags(
        album=_text(tags, "TALB"),
        album_artist=_text(tags, "TPE2"),
        date=_text(tags, "TDRC"),
        label=_text(tags, "TPUB"),  # a real frame, unlike the two below
        catalog_number=_txxx(tags, TXXX_CATALOG),
        barcode=_txxx(tags, TXXX_BARCODE),
        # TMED — the frame `write_tags` actually writes. Read as TXXX:MEDIA
        # until #149, so `media` never round-tripped and every MP3 Harmonist
        # had tagged reported it missing on the album page.
        media=_text(tags, "TMED"),
        genre=_text(tags, "TCON"),
        title=_text(tags, "TIT2"),
        artist=_text(tags, "TPE1"),
        # TRCK is "5" or "5/12" — the total belongs to the album, not the track.
        track_num=_first_int(track_num),
        disc_num=_first_int(_text(tags, "TPOS")),
        duration_ms=round(audio.info.length * 1000) if audio.info.length else None,
        comment=_comment_text(tags),
        release_track_id=_txxx(tags, TXXX_RELEASE_TRACK_ID),
    )


def _total_int(value: str | None) -> int | None:
    """The total half of an ID3 "n/total" pair, or None when absent or unparseable.
    A bare "5" carries no total, which is the honest answer rather than 5."""
    if not value or "/" not in value:
        return None
    try:
        return int(value.split("/", 1)[1])
    except ValueError:
        return None


def _first_int(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(value.split("/")[0])
    except ValueError:
        return None


def _comment_text(tags: Any) -> str | None:
    """The COMM frame Harmonist leaves alone on write, so a recovered Bandcamp
    URL survives a retag. Read back for display only — never compared to MB."""
    if tags is None:
        return None
    for frame in tags.getall("COMM"):
        if frame.text and frame.text[0]:
            return str(frame.text[0])
    return None


def read_cover(path: Path) -> tuple[bytes, str] | None:
    """Extract the embedded APIC cover art as (image_bytes, mime), or None."""
    audio = _open(path)
    if audio is None or audio.tags is None:
        return None
    apics = audio.tags.getall("APIC")
    if not apics:
        return None
    pic = apics[0]
    return bytes(pic.data), (pic.mime or "image/jpeg")


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------


def _set_txxx(tags: ID3, desc: str, values: list[str]) -> None:
    tags.delall(f"TXXX:{desc}")
    tags.add(TXXX(encoding=Encoding.UTF8, desc=desc, text=values))


def _txxx_list(tags: Any, desc: str) -> list[str]:
    """Every value of a TXXX user-text frame. ARTISTS and the artist-id frames
    are multi-valued, and ID3 carries them as several strings in one frame."""
    if tags is None:
        return []
    frame = tags.get(f"TXXX:{desc}")
    return [str(v) for v in frame.text] if frame is not None and frame.text else []


def _split_pair(value: str | None) -> tuple[int | None, int | None]:
    """ "5/12" -> (5, 12). ID3 packs number and total into one frame, so both
    owned fields are read from it. Anything unparseable reads as absent rather
    than raising — a vinyl rip numbered "A1" must not break a re-tag."""
    if not value:
        return None, None
    head, _, tail = value.partition("/")
    try:
        num = int(head)
    except ValueError:
        return None, None
    try:
        total = int(tail) if tail else None
    except ValueError:
        total = None
    return num, total


def _read_owned(tags: Any) -> dict[str, Any]:
    """The current value of every owned field, shaped exactly like the matching
    `TagSet` attribute (#86). See `m4a._read_owned` on why shape matters."""
    track_num, track_total = _split_pair(_text(tags, "TRCK"))
    disc_num, disc_total = _split_pair(_text(tags, "TPOS"))
    ufid = tags.get(f"UFID:{UFID_OWNER}") if tags is not None else None
    isrc = tags.get("TSRC") if tags is not None else None
    return {
        Owned.MB_ALBUM_ID: _txxx(tags, TXXX_ALBUM_ID),
        Owned.ALBUM: _text(tags, "TALB"),
        Owned.ALBUM_ARTIST: _text(tags, "TPE2"),
        Owned.ALBUM_ARTIST_SORT: _text(tags, "TSO2"),
        Owned.MB_ALBUM_ARTIST_IDS: _txxx_list(tags, TXXX_ALBUM_ARTIST_ID),
        Owned.MB_RELEASE_GROUP_ID: _txxx(tags, TXXX_RELEASE_GROUP_ID),
        Owned.MB_ALBUM_TYPE: _txxx(tags, TXXX_ALBUM_TYPE),
        Owned.MB_ALBUM_STATUS: _txxx(tags, TXXX_ALBUM_STATUS),
        Owned.MB_ALBUM_COUNTRY: _txxx(tags, TXXX_ALBUM_COUNTRY),
        Owned.DATE: _text(tags, "TDRC"),
        Owned.ORIGINAL_DATE: _text(tags, "TDOR"),
        Owned.SCRIPT: _txxx(tags, TXXX_SCRIPT),
        Owned.LABEL: _text(tags, "TPUB"),
        Owned.CATALOG_NUMBER: _txxx(tags, TXXX_CATALOG),
        Owned.BARCODE: _txxx(tags, TXXX_BARCODE),
        Owned.ASIN: _txxx(tags, TXXX_ASIN),
        Owned.DISC_TOTAL: disc_total,
        Owned.TITLE: _text(tags, "TIT2"),
        Owned.ARTIST: _text(tags, "TPE1"),
        Owned.ARTIST_SORT: _text(tags, "TSOP"),
        Owned.ARTISTS: _txxx_list(tags, TXXX_ARTISTS),
        Owned.TRACK_NUM: track_num,
        Owned.TRACK_TOTAL: track_total,
        Owned.DISC_NUM: disc_num,
        Owned.MEDIA: _text(tags, "TMED"),
        Owned.DISC_SUBTITLE: _text(tags, "TSST"),
        Owned.MB_TRACK_ID: ufid.data.decode("ascii", "replace") if ufid is not None else None,
        Owned.MB_RELEASE_TRACK_ID: _txxx(tags, TXXX_RELEASE_TRACK_ID),
        Owned.MB_ARTIST_IDS: _txxx_list(tags, TXXX_ARTIST_ID),
        Owned.ISRCS: [str(v) for v in isrc.text] if isrc is not None and isrc.text else [],
    }


def read_owned(path: Path) -> dict[str, Any]:
    """Every owned field as it currently stands on disk (#157's undo).

    Raises rather than returning blanks when the file can't be opened: a revert
    that read an unreadable file as an untagged one would decide every field had
    already been changed and quietly do nothing (#112's lesson, one layer down).
    """
    return _read_owned(MP3(path).tags)


def write_owned(path: Path, values: Mapping[str, Any]) -> dict[str, Any]:
    """Set every owned field to `values`, removing those absent (#157's undo).

    `values` is a COMPLETE owned snapshot, shaped exactly like `_read_owned`'s
    result — not a patch. It has to be: TRCK and TPOS each pack two owned fields
    into one frame, so writing `track_num` without `track_total` in hand would
    drop the total. The caller reads the file's current state and overlays what
    it wants changed.

    Separate from `write_tags` rather than folded into it because a `TagSet`
    cannot express absence: `title`, `album` and `artist` are required there and
    are written unconditionally, so reverting a first tagging through it would
    write empty frames instead of removing them. Absence is a value here (see
    `owned._absent`), and the undo has to be able to restore it.

    Returns the owned fields as they were BEFORE the write, like `write_tags`,
    so an undo can record its own before/after without a second read.
    """
    audio = MP3(path)
    if audio.tags is None:
        audio.add_tags()
    tags = audio.tags
    before = _read_owned(tags)

    for frames in OWNED_FRAMES.values():
        for frame_id in frames:
            tags.delall(frame_id)
    _apply_owned(tags, values)

    # COMM and APIC are untouched, as everywhere else: a revert of tags must not
    # disturb a recovered Bandcamp URL or the embedded artwork, which has its
    # own undo (#131).
    audio.save()
    return before


def _apply_owned(tags: ID3, values: Mapping[str, Any]) -> None:
    """Write an owned snapshot into already-cleared ID3 tags.

    Every field is written only when present, so absence stays absence. The
    frame each field lands in must match `OWNED_FRAMES` and `_read_owned` — a
    test writes the same values through here and through `write_tags` and
    asserts both read back identically, which is what stops the two paths
    drifting the way TMED once did.
    """
    for fld, frame in _TEXT_FRAMES.items():
        if (value := values.get(fld)) not in (None, ""):
            tags.setall(frame.__name__, [frame(encoding=Encoding.UTF8, text=[str(value)])])
    for fld, desc in _TXXX_FIELDS.items():
        if (value := values.get(fld)) not in (None, ""):
            _set_txxx(tags, desc, [str(value)])
    for fld, desc in _TXXX_LIST_FIELDS.items():
        if value := values.get(fld):
            _set_txxx(tags, desc, [str(v) for v in value])

    if track_id := values.get(Owned.MB_TRACK_ID):
        tags.add(UFID(owner=UFID_OWNER, data=str(track_id).encode("ascii")))
    if isrcs := values.get(Owned.ISRCS):
        tags.setall("TSRC", [TSRC(encoding=Encoding.UTF8, text=[str(v) for v in isrcs])])

    _set_pair(tags, "TRCK", TRCK, values.get(Owned.TRACK_NUM), values.get(Owned.TRACK_TOTAL))
    _set_pair(tags, "TPOS", TPOS, values.get(Owned.DISC_NUM), values.get(Owned.DISC_TOTAL))


def _set_pair(tags: ID3, frame_id: str, frame: Any, num: Any, total: Any) -> None:
    """Write ID3's packed "n/total" frames.

    A total with no number writes nothing: `_split_pair` reads the number first
    and returns `(None, None)` for anything it can't parse, so "/12" would not
    round-trip and writing it would lose the total silently.
    """
    if num is None:
        return
    text = f"{num}/{total}" if total is not None else str(num)
    tags.setall(frame_id, [frame(encoding=Encoding.UTF8, text=[text])])


#: Owned fields that are a single standard text frame, by frame class. The class
#: doubles as the frame id (`TALB.__name__ == "TALB"`), which is what mutagen's
#: `setall` keys on.
_TEXT_FRAMES: dict[Owned, Any] = {
    Owned.ALBUM: TALB,
    Owned.ALBUM_ARTIST: TPE2,
    Owned.ALBUM_ARTIST_SORT: TSO2,
    Owned.DATE: TDRC,
    Owned.ORIGINAL_DATE: TDOR,
    Owned.LABEL: TPUB,
    Owned.TITLE: TIT2,
    Owned.ARTIST: TPE1,
    Owned.ARTIST_SORT: TSOP,
    Owned.MEDIA: TMED,
    Owned.DISC_SUBTITLE: TSST,
}

#: Owned fields carried in a single-valued TXXX user-text frame.
_TXXX_FIELDS: dict[Owned, str] = {
    Owned.MB_ALBUM_ID: TXXX_ALBUM_ID,
    Owned.MB_RELEASE_GROUP_ID: TXXX_RELEASE_GROUP_ID,
    Owned.MB_ALBUM_TYPE: TXXX_ALBUM_TYPE,
    Owned.MB_ALBUM_STATUS: TXXX_ALBUM_STATUS,
    Owned.MB_ALBUM_COUNTRY: TXXX_ALBUM_COUNTRY,
    Owned.SCRIPT: TXXX_SCRIPT,
    Owned.CATALOG_NUMBER: TXXX_CATALOG,
    Owned.BARCODE: TXXX_BARCODE,
    Owned.ASIN: TXXX_ASIN,
    Owned.MB_RELEASE_TRACK_ID: TXXX_RELEASE_TRACK_ID,
}

#: Owned fields carried as several strings in one TXXX frame.
_TXXX_LIST_FIELDS: dict[Owned, str] = {
    Owned.MB_ALBUM_ARTIST_IDS: TXXX_ALBUM_ARTIST_ID,
    Owned.ARTISTS: TXXX_ARTISTS,
    Owned.MB_ARTIST_IDS: TXXX_ARTIST_ID,
}


def _apic(cover: bytes) -> APIC:
    mime = "image/png" if cover[:4] == b"\x89PNG" else "image/jpeg"
    return APIC(
        encoding=Encoding.UTF8, mime=mime, type=PictureType.COVER_FRONT, desc="", data=cover
    )


def write_cover(path: Path, cover: bytes) -> None:
    """Replace the embedded image, touching nothing else (#131's restore)."""
    audio = MP3(path)
    if audio.tags is None:
        audio.add_tags()
    audio.tags.delall("APIC")
    audio.tags.add(_apic(cover))
    audio.save()


def write_tags(path: Path, tagset: TagSet, cover: bytes | None) -> dict[str, Any]:
    """Write `tagset` to `path`, returning the owned fields as they were BEFORE
    the write — read from the handle already open here, so the tagging audit
    (#86) costs no second pass over the file."""
    audio = MP3(path)
    if audio.tags is None:
        audio.add_tags()
    tags = audio.tags
    before = _read_owned(tags)

    # Clear every owned frame before writing, so a field absent from this
    # TagSet is REMOVED rather than left stale from a previous tagging (#149).
    # Anything not owned — COMM, TCON, arbitrary frames, APIC — is untouched.
    for frames in OWNED_FRAMES.values():
        for frame_id in frames:
            tags.delall(frame_id)

    # ---- Album-level MB IDs ----
    _set_txxx(tags, TXXX_ALBUM_ID, [tagset.mb_album_id])
    if tagset.mb_album_artist_ids:
        _set_txxx(tags, TXXX_ALBUM_ARTIST_ID, tagset.mb_album_artist_ids)
    if tagset.mb_release_group_id:
        _set_txxx(tags, TXXX_RELEASE_GROUP_ID, [tagset.mb_release_group_id])
    if tagset.mb_album_type:
        _set_txxx(tags, TXXX_ALBUM_TYPE, [tagset.mb_album_type])
    if tagset.mb_album_status:
        _set_txxx(tags, TXXX_ALBUM_STATUS, [tagset.mb_album_status])
    if tagset.mb_album_country:
        _set_txxx(tags, TXXX_ALBUM_COUNTRY, [tagset.mb_album_country])

    # ---- Per-track MB IDs ----
    if tagset.mb_track_id:
        tags.delall(f"UFID:{UFID_OWNER}")
        tags.add(UFID(owner=UFID_OWNER, data=tagset.mb_track_id.encode("ascii")))
    if tagset.mb_release_track_id:
        _set_txxx(tags, TXXX_RELEASE_TRACK_ID, [tagset.mb_release_track_id])
    if tagset.mb_artist_ids:
        _set_txxx(tags, TXXX_ARTIST_ID, tagset.mb_artist_ids)
    if tagset.isrcs:
        tags.setall("TSRC", [TSRC(encoding=Encoding.UTF8, text=tagset.isrcs)])

    # ---- Standard text frames ----
    tags.setall("TIT2", [TIT2(encoding=Encoding.UTF8, text=[tagset.title])])
    tags.setall("TALB", [TALB(encoding=Encoding.UTF8, text=[tagset.album])])
    tags.setall("TPE1", [TPE1(encoding=Encoding.UTF8, text=[tagset.artist])])
    tags.setall("TPE2", [TPE2(encoding=Encoding.UTF8, text=[tagset.album_artist])])
    if tagset.date:
        tags.setall("TDRC", [TDRC(encoding=Encoding.UTF8, text=[tagset.date])])
    if tagset.artist_sort:
        tags.setall("TSOP", [TSOP(encoding=Encoding.UTF8, text=[tagset.artist_sort])])
    if tagset.album_artist_sort:
        tags.setall("TSO2", [TSO2(encoding=Encoding.UTF8, text=[tagset.album_artist_sort])])
    if tagset.artists:
        _set_txxx(tags, TXXX_ARTISTS, tagset.artists)
    if tagset.original_date:
        tags.setall("TDOR", [TDOR(encoding=Encoding.UTF8, text=[tagset.original_date])])
    if tagset.script:
        _set_txxx(tags, TXXX_SCRIPT, [tagset.script])

    # ---- Numeric position ("n/total") ----
    tags.setall(
        "TRCK", [TRCK(encoding=Encoding.UTF8, text=[f"{tagset.track_num}/{tagset.track_total}"])]
    )
    tags.setall(
        "TPOS", [TPOS(encoding=Encoding.UTF8, text=[f"{tagset.disc_num}/{tagset.disc_total}"])]
    )

    # ---- Optional album-level metadata ----
    if tagset.label:
        tags.setall("TPUB", [TPUB(encoding=Encoding.UTF8, text=[tagset.label])])
    if tagset.catalog_number:
        _set_txxx(tags, TXXX_CATALOG, [tagset.catalog_number])
    if tagset.barcode:
        _set_txxx(tags, TXXX_BARCODE, [tagset.barcode])
    if tagset.asin:
        _set_txxx(tags, TXXX_ASIN, [tagset.asin])
    if tagset.media:
        tags.setall("TMED", [TMED(encoding=Encoding.UTF8, text=[tagset.media])])
    if tagset.disc_subtitle:
        tags.setall("TSST", [TSST(encoding=Encoding.UTF8, text=[tagset.disc_subtitle])])

    # ---- Cover art ----
    if cover is not None:
        tags.delall("APIC")
        tags.add(_apic(cover))

    # COMM intentionally NOT touched — preserves a recovered Bandcamp URL.

    audio.save()
    return before
