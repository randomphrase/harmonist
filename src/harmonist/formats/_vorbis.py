"""Shared Vorbis-comment tag logic for FLAC, Ogg Vorbis, and Opus.

All three use the same KEY=VALUE Vorbis-comment scheme (mutagen exposes
a case-insensitive dict-like `.tags` for each). They differ only in:
  - the mutagen class used to open the file (per-format `_open`), and
  - how cover art is embedded (FLAC has a native picture API; Ogg
    containers stash a base64 FLAC picture block in a comment).

`VorbisTagger` captures those two differences via injected callables and
provides the format-agnostic read/write surface the dispatcher expects.
Mapping follows the MusicBrainz Picard Vorbis spec.
"""

from __future__ import annotations

import base64
from collections.abc import Callable
from pathlib import Path
from typing import Any

from mutagen.flac import Picture

from .types import ScanFields, TagSet

# Vorbis comment keys (uppercase by convention; lookups are case-insensitive).
KEY_ALBUM_ID = "MUSICBRAINZ_ALBUMID"
KEY_ALBUM_ARTIST_ID = "MUSICBRAINZ_ALBUMARTISTID"
KEY_RELEASE_GROUP_ID = "MUSICBRAINZ_RELEASEGROUPID"
KEY_TRACK_ID = "MUSICBRAINZ_TRACKID"  # the recording MBID
KEY_RELEASE_TRACK_ID = "MUSICBRAINZ_RELEASETRACKID"
KEY_ARTIST_ID = "MUSICBRAINZ_ARTISTID"
KEY_ISRC = "ISRC"
KEY_RELEASE_TYPE = "RELEASETYPE"
KEY_RELEASE_STATUS = "RELEASESTATUS"
KEY_RELEASE_COUNTRY = "RELEASECOUNTRY"
KEY_TITLE = "TITLE"
KEY_ALBUM = "ALBUM"
KEY_ARTIST = "ARTIST"
KEY_ALBUM_ARTIST = "ALBUMARTIST"
KEY_ARTIST_SORT = "ARTISTSORT"
KEY_ALBUM_ARTIST_SORT = "ALBUMARTISTSORT"
KEY_ARTISTS = "ARTISTS"
KEY_DATE = "DATE"
KEY_ORIGINAL_DATE = "ORIGINALDATE"
KEY_ORIGINAL_YEAR = "ORIGINALYEAR"
KEY_SCRIPT = "SCRIPT"
KEY_TRACK_NUMBER = "TRACKNUMBER"
KEY_TRACK_TOTAL = "TOTALTRACKS"
KEY_DISC_NUMBER = "DISCNUMBER"
KEY_DISC_TOTAL = "TOTALDISCS"
KEY_LABEL = "LABEL"
KEY_CATALOG = "CATALOGNUMBER"
KEY_BARCODE = "BARCODE"
KEY_ASIN = "ASIN"
KEY_MEDIA = "MEDIA"
KEY_COMMENT = "COMMENT"
KEY_DESCRIPTION = "DESCRIPTION"

# Keys this tagger manages on write (cleared then rewritten). COMMENT /
# DESCRIPTION are deliberately excluded so a recovered Bandcamp URL survives.
_MANAGED_KEYS = (
    KEY_ALBUM_ID,
    KEY_ALBUM_ARTIST_ID,
    KEY_RELEASE_GROUP_ID,
    KEY_TRACK_ID,
    KEY_RELEASE_TRACK_ID,
    KEY_ARTIST_ID,
    KEY_ISRC,
    KEY_RELEASE_TYPE,
    KEY_RELEASE_STATUS,
    KEY_RELEASE_COUNTRY,
    KEY_TITLE,
    KEY_ALBUM,
    KEY_ARTIST,
    KEY_ALBUM_ARTIST,
    KEY_ARTIST_SORT,
    KEY_ALBUM_ARTIST_SORT,
    KEY_ARTISTS,
    KEY_DATE,
    KEY_ORIGINAL_DATE,
    KEY_ORIGINAL_YEAR,
    KEY_SCRIPT,
    KEY_TRACK_NUMBER,
    KEY_TRACK_TOTAL,
    KEY_DISC_NUMBER,
    KEY_DISC_TOTAL,
    KEY_LABEL,
    KEY_CATALOG,
    KEY_BARCODE,
    KEY_ASIN,
    KEY_MEDIA,
)


def _has_embedded_cover(audio: Any) -> bool:
    """True if the file carries cover art — FLAC native pictures or the
    Ogg/Opus base64 METADATA_BLOCK_PICTURE comment."""
    if getattr(audio, "pictures", None):  # FLAC
        return True
    tags = audio.tags
    return bool(tags and tags.get("metadata_block_picture"))


def make_picture(cover: bytes) -> Picture:
    """Build a FLAC front-cover Picture from raw image bytes."""
    pic = Picture()
    pic.type = 3  # front cover
    pic.mime = "image/png" if cover[:4] == b"\x89PNG" else "image/jpeg"
    pic.data = cover
    return pic


def ogg_set_cover(audio: Any, cover: bytes) -> None:
    """Cover-setter for Ogg containers (Vorbis/Opus): a base64 FLAC
    picture block in the METADATA_BLOCK_PICTURE comment."""
    pic = make_picture(cover)
    audio["metadata_block_picture"] = [base64.b64encode(pic.write()).decode("ascii")]


class VorbisTagger:
    def __init__(
        self,
        open_fn: Callable[[Path], Any | None],
        set_cover: Callable[[Any, bytes], None],
    ):
        self._open = open_fn
        self._set_cover = set_cover

    # ---- reads ----

    def _first(self, path: Path, key: str) -> str | None:
        audio = self._open(path)
        if audio is None or audio.tags is None:
            return None
        values = audio.tags.get(key)
        if not values:
            return None
        return str(values[0]) or None

    def read_album_id(self, path: Path) -> str | None:
        return self._first(path, KEY_ALBUM_ID)

    def read_album_title(self, path: Path) -> str | None:
        return self._first(path, KEY_ALBUM)

    def read_artist(self, path: Path) -> str | None:
        return self._first(path, KEY_ARTIST)

    def read_track_title(self, path: Path) -> str | None:
        return self._first(path, KEY_TITLE)

    def read_comment(self, path: Path) -> str | None:
        return self._first(path, KEY_COMMENT) or self._first(path, KEY_DESCRIPTION)

    def read_duration_ms(self, path: Path) -> int | None:
        audio = self._open(path)
        if audio is None or not audio.info.length:
            return None
        ms: int = round(audio.info.length * 1000)
        return ms

    def read_scan_fields(self, path: Path, codec: str) -> ScanFields:
        """All scanner-needed fields in one open. `codec` is the format label
        (a constant per Vorbis container — FLAC/Vorbis/Opus)."""
        audio = self._open(path)
        if audio is None:
            return ScanFields(None, None, None, codec)
        has_cover = _has_embedded_cover(audio)
        tags = audio.tags
        if tags is None:
            return ScanFields(None, None, None, codec, has_cover)

        def first(key: str) -> str | None:
            values = tags.get(key)
            return (str(values[0]) or None) if values else None

        return ScanFields(
            album_title=first(KEY_ALBUM),
            album_id=first(KEY_ALBUM_ID),
            artist=first(KEY_ARTIST),
            codec=codec,
            has_cover=has_cover,
            album_artist=first(KEY_ALBUM_ARTIST),
        )

    def read_cover(self, path: Path) -> tuple[bytes, str] | None:
        """Extract embedded cover art as (image_bytes, mime). Handles both the
        FLAC native picture block and the Ogg/Opus base64 METADATA_BLOCK_PICTURE."""
        audio = self._open(path)
        if audio is None:
            return None
        pictures = getattr(audio, "pictures", None)  # FLAC
        if pictures:
            pic = pictures[0]
            return bytes(pic.data), (pic.mime or "image/jpeg")
        tags = audio.tags  # Ogg/Opus
        encoded = tags.get("metadata_block_picture") if tags else None
        if encoded:
            try:
                pic = Picture(base64.b64decode(encoded[0]))
            except Exception:
                return None
            return bytes(pic.data), (pic.mime or "image/jpeg")
        return None

    # ---- write ----

    def write_tags(self, path: Path, tagset: TagSet, cover: bytes | None) -> None:
        audio = self._open(path)
        if audio is None:
            raise OSError(f"could not open {path} for tagging")
        if audio.tags is None:
            audio.add_tags()
        tags = audio.tags

        # Clear the keys we manage; leave COMMENT/DESCRIPTION (and anything
        # else) alone.
        for key in _MANAGED_KEYS:
            if key in tags:
                del tags[key]

        tags[KEY_ALBUM_ID] = [tagset.mb_album_id]
        if tagset.mb_album_artist_ids:
            tags[KEY_ALBUM_ARTIST_ID] = list(tagset.mb_album_artist_ids)
        if tagset.mb_release_group_id:
            tags[KEY_RELEASE_GROUP_ID] = [tagset.mb_release_group_id]
        if tagset.mb_album_type:
            tags[KEY_RELEASE_TYPE] = [tagset.mb_album_type]
        if tagset.mb_album_status:
            tags[KEY_RELEASE_STATUS] = [tagset.mb_album_status]
        if tagset.mb_album_country:
            tags[KEY_RELEASE_COUNTRY] = [tagset.mb_album_country]

        if tagset.mb_track_id:
            tags[KEY_TRACK_ID] = [tagset.mb_track_id]
        if tagset.mb_release_track_id:
            tags[KEY_RELEASE_TRACK_ID] = [tagset.mb_release_track_id]
        if tagset.mb_artist_ids:
            tags[KEY_ARTIST_ID] = list(tagset.mb_artist_ids)
        if tagset.isrcs:
            tags[KEY_ISRC] = list(tagset.isrcs)

        tags[KEY_TITLE] = [tagset.title]
        tags[KEY_ALBUM] = [tagset.album]
        tags[KEY_ARTIST] = [tagset.artist]
        tags[KEY_ALBUM_ARTIST] = [tagset.album_artist]
        if tagset.artist_sort:
            tags[KEY_ARTIST_SORT] = [tagset.artist_sort]
        if tagset.album_artist_sort:
            tags[KEY_ALBUM_ARTIST_SORT] = [tagset.album_artist_sort]
        if tagset.artists:
            tags[KEY_ARTISTS] = list(tagset.artists)
        if tagset.date:
            tags[KEY_DATE] = [tagset.date]
        if tagset.original_date:
            tags[KEY_ORIGINAL_DATE] = [tagset.original_date]
            tags[KEY_ORIGINAL_YEAR] = [tagset.original_date[:4]]
        if tagset.script:
            tags[KEY_SCRIPT] = [tagset.script]

        tags[KEY_TRACK_NUMBER] = [str(tagset.track_num)]
        tags[KEY_TRACK_TOTAL] = [str(tagset.track_total)]
        tags[KEY_DISC_NUMBER] = [str(tagset.disc_num)]
        tags[KEY_DISC_TOTAL] = [str(tagset.disc_total)]

        if tagset.label:
            tags[KEY_LABEL] = [tagset.label]
        if tagset.catalog_number:
            tags[KEY_CATALOG] = [tagset.catalog_number]
        if tagset.barcode:
            tags[KEY_BARCODE] = [tagset.barcode]
        if tagset.asin:
            tags[KEY_ASIN] = [tagset.asin]
        if tagset.media:
            tags[KEY_MEDIA] = [tagset.media]

        if cover is not None:
            self._set_cover(audio, cover)

        audio.save()
