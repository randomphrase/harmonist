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

from pathlib import Path
from typing import Any

from mutagen.id3 import (
    APIC,
    ID3,
    TALB,
    TDRC,
    TIT2,
    TMED,
    TPE1,
    TPE2,
    TPOS,
    TPUB,
    TRCK,
    TXXX,
    UFID,
    Encoding,
    PictureType,
)
from mutagen.mp3 import MP3

from .types import ScanFields, TagSet

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
        return ScanFields(None, None, None, "MP3")
    tags = audio.tags
    return ScanFields(
        album_title=_text(tags, "TALB"),
        album_id=_txxx(tags, TXXX_ALBUM_ID),
        artist=_text(tags, "TPE1"),
        codec="MP3",
        has_cover=bool(tags and tags.getall("APIC")),
    )


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


def write_tags(path: Path, tagset: TagSet, cover: bytes | None) -> None:
    audio = MP3(path)
    if audio.tags is None:
        audio.add_tags()
    tags = audio.tags

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

    # ---- Standard text frames ----
    tags.setall("TIT2", [TIT2(encoding=Encoding.UTF8, text=[tagset.title])])
    tags.setall("TALB", [TALB(encoding=Encoding.UTF8, text=[tagset.album])])
    tags.setall("TPE1", [TPE1(encoding=Encoding.UTF8, text=[tagset.artist])])
    tags.setall("TPE2", [TPE2(encoding=Encoding.UTF8, text=[tagset.album_artist])])
    if tagset.date:
        tags.setall("TDRC", [TDRC(encoding=Encoding.UTF8, text=[tagset.date])])

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

    # ---- Cover art ----
    if cover is not None:
        mime = "image/png" if cover[:4] == b"\x89PNG" else "image/jpeg"
        tags.delall("APIC")
        tags.add(
            APIC(
                encoding=Encoding.UTF8, mime=mime, type=PictureType.COVER_FRONT, desc="", data=cover
            )
        )

    # COMM intentionally NOT touched — preserves a recovered Bandcamp URL.

    audio.save()
