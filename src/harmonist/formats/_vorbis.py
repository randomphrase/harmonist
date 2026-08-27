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
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from mutagen.flac import Picture

from . import quality
from .owned import Owned
from .types import ScanFields, TagSet, TrackTags

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
#: Picard's `discsubtitle` — the medium's own name (#218).
KEY_DISC_SUBTITLE = "DISCSUBTITLE"
KEY_COMMENT = "COMMENT"
# Read-only: Harmonist doesn't write a genre (that's #12), but files tagged
# elsewhere carry one and the album comparison should show it.
KEY_GENRE = "GENRE"
KEY_DESCRIPTION = "DESCRIPTION"

# The Vorbis keys behind each owned field (#149). COMMENT / DESCRIPTION are
# absent so a recovered Bandcamp URL survives a retag, GENRE because Harmonist
# doesn't write one (#12), and the picture block because per-track artwork is
# preserved deliberately — see `owned.py`.
OWNED_KEYS: dict[Owned, tuple[str, ...]] = {
    Owned.MB_ALBUM_ID: (KEY_ALBUM_ID,),
    Owned.ALBUM: (KEY_ALBUM,),
    Owned.ALBUM_ARTIST: (KEY_ALBUM_ARTIST,),
    Owned.ALBUM_ARTIST_SORT: (KEY_ALBUM_ARTIST_SORT,),
    Owned.MB_ALBUM_ARTIST_IDS: (KEY_ALBUM_ARTIST_ID,),
    Owned.MB_RELEASE_GROUP_ID: (KEY_RELEASE_GROUP_ID,),
    Owned.MB_ALBUM_TYPE: (KEY_RELEASE_TYPE,),
    Owned.MB_ALBUM_STATUS: (KEY_RELEASE_STATUS,),
    Owned.MB_ALBUM_COUNTRY: (KEY_RELEASE_COUNTRY,),
    Owned.DATE: (KEY_DATE,),
    # One field, two keys: Picard writes the year alongside the full date.
    Owned.ORIGINAL_DATE: (KEY_ORIGINAL_DATE, KEY_ORIGINAL_YEAR),
    Owned.SCRIPT: (KEY_SCRIPT,),
    Owned.LABEL: (KEY_LABEL,),
    Owned.CATALOG_NUMBER: (KEY_CATALOG,),
    Owned.BARCODE: (KEY_BARCODE,),
    Owned.ASIN: (KEY_ASIN,),
    Owned.DISC_TOTAL: (KEY_DISC_TOTAL,),
    Owned.TITLE: (KEY_TITLE,),
    Owned.ARTIST: (KEY_ARTIST,),
    Owned.ARTIST_SORT: (KEY_ARTIST_SORT,),
    Owned.ARTISTS: (KEY_ARTISTS,),
    Owned.TRACK_NUM: (KEY_TRACK_NUMBER,),
    Owned.TRACK_TOTAL: (KEY_TRACK_TOTAL,),
    Owned.DISC_NUM: (KEY_DISC_NUMBER,),
    Owned.MEDIA: (KEY_MEDIA,),
    Owned.DISC_SUBTITLE: (KEY_DISC_SUBTITLE,),
    Owned.MB_TRACK_ID: (KEY_TRACK_ID,),
    Owned.MB_RELEASE_TRACK_ID: (KEY_RELEASE_TRACK_ID,),
    Owned.MB_ARTIST_IDS: (KEY_ARTIST_ID,),
    Owned.ISRCS: (KEY_ISRC,),
}

#: Keys a write CLEARS and never writes back — see `m4a.SUPERSEDED_ATOMS`.
#: Empty here: `KEY_ORIGINAL_YEAR` is the second key of an `OWNED_KEYS` pair,
#: but a write re-derives it from `ORIGINALDATE`, so its presence is normal.
SUPERSEDED_KEYS: tuple[str, ...] = ()


#: Owned fields carried by a single Vorbis comment each. Numbers are written as
#: their decimal string, which is how `_read_owned` reads them back.
_SINGLE_KEYS: dict[Owned, str] = {
    Owned.MB_ALBUM_ID: KEY_ALBUM_ID,
    Owned.ALBUM: KEY_ALBUM,
    Owned.ALBUM_ARTIST: KEY_ALBUM_ARTIST,
    Owned.ALBUM_ARTIST_SORT: KEY_ALBUM_ARTIST_SORT,
    Owned.MB_RELEASE_GROUP_ID: KEY_RELEASE_GROUP_ID,
    Owned.MB_ALBUM_TYPE: KEY_RELEASE_TYPE,
    Owned.MB_ALBUM_STATUS: KEY_RELEASE_STATUS,
    Owned.MB_ALBUM_COUNTRY: KEY_RELEASE_COUNTRY,
    Owned.DATE: KEY_DATE,
    Owned.ORIGINAL_DATE: KEY_ORIGINAL_DATE,
    Owned.SCRIPT: KEY_SCRIPT,
    Owned.LABEL: KEY_LABEL,
    Owned.CATALOG_NUMBER: KEY_CATALOG,
    Owned.BARCODE: KEY_BARCODE,
    Owned.ASIN: KEY_ASIN,
    Owned.DISC_TOTAL: KEY_DISC_TOTAL,
    Owned.TITLE: KEY_TITLE,
    Owned.ARTIST: KEY_ARTIST,
    Owned.ARTIST_SORT: KEY_ARTIST_SORT,
    Owned.TRACK_NUM: KEY_TRACK_NUMBER,
    Owned.TRACK_TOTAL: KEY_TRACK_TOTAL,
    Owned.DISC_NUM: KEY_DISC_NUMBER,
    Owned.MEDIA: KEY_MEDIA,
    Owned.DISC_SUBTITLE: KEY_DISC_SUBTITLE,
    Owned.MB_TRACK_ID: KEY_TRACK_ID,
    Owned.MB_RELEASE_TRACK_ID: KEY_RELEASE_TRACK_ID,
}

#: Owned fields carried as several values under one key.
_LIST_KEYS: dict[Owned, str] = {
    Owned.MB_ALBUM_ARTIST_IDS: KEY_ALBUM_ARTIST_ID,
    Owned.ARTISTS: KEY_ARTISTS,
    Owned.MB_ARTIST_IDS: KEY_ARTIST_ID,
    Owned.ISRCS: KEY_ISRC,
}


def _total_int(value: str | None) -> int | None:
    """The total half of a "5/12" Vorbis comment, or None. Mirrors `mp3._total_int`."""
    if not value or "/" not in value:
        return None
    try:
        return int(value.split("/", 1)[1])
    except ValueError:
        return None


def _first_int(value: str | None) -> int | None:
    """The leading integer of a "5" / "5/12" Vorbis comment, or None.

    None for anything that isn't a number, rather than an exception: vinyl rips
    routinely carry TRACKNUMBER="A1", and a whole album page is not allowed to
    500 because one file is numbered by side. "No usable number" is the honest
    reading of "A1" here — the tracklist comparison then falls back to file
    order for that track (#135). Mirrors `mp3._first_int`.
    """
    if not value:
        return None
    try:
        return int(value.split("/")[0])
    except ValueError:
        return None


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

    def read_scan_fields(self, path: Path, codec: str, *, lossless: bool) -> ScanFields:
        """All scanner-needed fields in one open. `codec` is the format label
        and `lossless` whether the stream is — both constants per Vorbis
        container (FLAC is lossless, Vorbis and Opus are not), and both passed
        in because this class handles all three and can't tell them apart."""
        audio = self._open(path)
        if audio is None:
            # Flagged, not silently blank: an unopenable file must not read as
            # an untagged one (#112).
            return ScanFields(None, None, None, codec, unreadable=True)
        has_cover = _has_embedded_cover(audio)
        # Read before the untagged early-return below: a file with no comment
        # block still has a perfectly readable stream, and what it IS doesn't
        # depend on whether anyone has tagged it.
        stream = quality.read(audio.info, lossless=lossless)
        tags = audio.tags
        if tags is None:
            return ScanFields(None, None, None, codec, has_cover, quality=stream)

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
            disc_num=_first_int(first(KEY_DISC_NUMBER)),
            # TOTALTRACKS/TOTALDISCS are the canonical keys Harmonist writes;
            # fall back to the "n/total" form some taggers put in TRACKNUMBER.
            track_total=_first_int(first(KEY_TRACK_TOTAL)) or _total_int(first(KEY_TRACK_NUMBER)),
            disc_total=_first_int(first(KEY_DISC_TOTAL)) or _total_int(first(KEY_DISC_NUMBER)),
            release_track_id=first(KEY_RELEASE_TRACK_ID),
            quality=stream,
        )

    def read_tags(self, path: Path) -> TrackTags:
        """Everything the album comparison needs from one file, in one open."""
        audio = self._open(path)
        if audio is None:
            return TrackTags(unreadable=True)
        duration = round(audio.info.length * 1000) if audio.info.length else None
        tags = audio.tags
        if tags is None:
            # Opened fine, carries no tag block: genuinely untagged, not
            # unreadable. The duration is still real.
            return TrackTags(duration_ms=duration)

        def first(key: str) -> str | None:
            values = tags.get(key)
            return (str(values[0]) or None) if values else None

        track_num = first(KEY_TRACK_NUMBER)
        disc_num = first(KEY_DISC_NUMBER)
        return TrackTags(
            album=first(KEY_ALBUM),
            album_artist=first(KEY_ALBUM_ARTIST),
            date=first(KEY_DATE),
            label=first(KEY_LABEL),
            catalog_number=first(KEY_CATALOG),
            barcode=first(KEY_BARCODE),
            media=first(KEY_MEDIA),
            genre=first(KEY_GENRE),
            title=first(KEY_TITLE),
            artist=first(KEY_ARTIST),
            track_num=_first_int(track_num),
            disc_num=_first_int(disc_num),
            duration_ms=duration,
            comment=first(KEY_COMMENT),
            release_track_id=first(KEY_RELEASE_TRACK_ID),
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

    def _read_owned(self, tags: Any) -> dict[str, Any]:
        """The current value of every owned field, shaped exactly like the
        matching `TagSet` attribute (#86). See `m4a._read_owned` on why the
        shape rather than just the value has to line up."""

        def one(key: str) -> str | None:
            values = tags.get(key) or []
            return str(values[0]) if values else None

        def many(key: str) -> list[str]:
            return [str(v) for v in (tags.get(key) or [])]

        def num(key: str) -> int | None:
            return _first_int(one(key))

        return {
            Owned.MB_ALBUM_ID: one(KEY_ALBUM_ID),
            Owned.ALBUM: one(KEY_ALBUM),
            Owned.ALBUM_ARTIST: one(KEY_ALBUM_ARTIST),
            Owned.ALBUM_ARTIST_SORT: one(KEY_ALBUM_ARTIST_SORT),
            Owned.MB_ALBUM_ARTIST_IDS: many(KEY_ALBUM_ARTIST_ID),
            Owned.MB_RELEASE_GROUP_ID: one(KEY_RELEASE_GROUP_ID),
            Owned.MB_ALBUM_TYPE: one(KEY_RELEASE_TYPE),
            Owned.MB_ALBUM_STATUS: one(KEY_RELEASE_STATUS),
            Owned.MB_ALBUM_COUNTRY: one(KEY_RELEASE_COUNTRY),
            Owned.DATE: one(KEY_DATE),
            Owned.ORIGINAL_DATE: one(KEY_ORIGINAL_DATE),
            Owned.SCRIPT: one(KEY_SCRIPT),
            Owned.LABEL: one(KEY_LABEL),
            Owned.CATALOG_NUMBER: one(KEY_CATALOG),
            Owned.BARCODE: one(KEY_BARCODE),
            Owned.ASIN: one(KEY_ASIN),
            Owned.DISC_TOTAL: num(KEY_DISC_TOTAL),
            Owned.TITLE: one(KEY_TITLE),
            Owned.ARTIST: one(KEY_ARTIST),
            Owned.ARTIST_SORT: one(KEY_ARTIST_SORT),
            Owned.ARTISTS: many(KEY_ARTISTS),
            Owned.TRACK_NUM: num(KEY_TRACK_NUMBER),
            Owned.TRACK_TOTAL: num(KEY_TRACK_TOTAL),
            Owned.DISC_NUM: num(KEY_DISC_NUMBER),
            Owned.MEDIA: one(KEY_MEDIA),
            Owned.DISC_SUBTITLE: one(KEY_DISC_SUBTITLE),
            Owned.MB_TRACK_ID: one(KEY_TRACK_ID),
            Owned.MB_RELEASE_TRACK_ID: one(KEY_RELEASE_TRACK_ID),
            Owned.MB_ARTIST_IDS: many(KEY_ARTIST_ID),
            Owned.ISRCS: many(KEY_ISRC),
        }

    def write_cover(self, path: Path, cover: bytes) -> None:
        """Replace the embedded image, touching nothing else (#131's restore)."""
        audio = self._open(path)
        if audio is None:
            raise OSError(f"could not open {path} to write its cover")
        if audio.tags is None:
            audio.add_tags()
        self._set_cover(audio, cover)
        audio.save()

    def write_tags(self, path: Path, tagset: TagSet, cover: bytes | None) -> dict[str, Any]:
        """Write `tagset` to `path`, returning the owned fields as they were
        BEFORE the write — read from the handle already open here, so the
        tagging audit (#86) costs no second pass over the file."""
        audio = self._open(path)
        if audio is None:
            raise OSError(f"could not open {path} for tagging")
        if audio.tags is None:
            audio.add_tags()
        tags = audio.tags
        before = self._read_owned(tags)

        # Clear every owned key before writing, so a field absent from this
        # TagSet is REMOVED rather than left stale from a previous tagging
        # (#149). Anything not owned — COMMENT/DESCRIPTION, GENRE, arbitrary
        # user tags, the picture block — is untouched.
        for keys in OWNED_KEYS.values():
            for key in keys:
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
        if tagset.disc_subtitle:
            tags[KEY_DISC_SUBTITLE] = [tagset.disc_subtitle]

        if cover is not None:
            self._set_cover(audio, cover)

        audio.save()
        return before

    def read_owned(self, path: Path) -> dict[str, Any]:
        """Every owned field as it currently stands on disk (#157's undo).

        Raises rather than returning blanks when the file can't be opened — see
        `mp3.read_owned`.
        """
        audio = self._open(path)
        if audio is None:
            raise OSError(f"could not open {path} to read its tags")
        return self._read_owned(audio.tags)

    def has_superseded_tags(self, path: Path) -> bool:
        """Whether `path` carries a key a write would remove and not write back.

        Always False while `SUPERSEDED_KEYS` is empty, and written to read the
        file rather than to return the constant so it keeps working if it isn't.
        """
        if not SUPERSEDED_KEYS:
            return False
        audio = self._open(path)
        tags = audio.tags if audio is not None else None
        return tags is not None and any(key in tags for key in SUPERSEDED_KEYS)

    def write_owned(self, path: Path, values: Mapping[str, Any]) -> dict[str, Any]:
        """Set every owned field to `values`, removing those absent (#157's undo).

        `values` is a complete owned snapshot shaped like `_read_owned`'s
        result. Vorbis is the straightforward case of the three: every owned
        field has a key to itself, so nothing has to be written in pairs the way
        ID3's TRCK and MP4's `trkn` do. See `mp3.write_owned` for why this is
        separate from `write_tags`.
        """
        audio = self._open(path)
        if audio is None:
            raise OSError(f"could not open {path} for tagging")
        if audio.tags is None:
            audio.add_tags()
        tags = audio.tags
        before = self._read_owned(tags)

        for keys in OWNED_KEYS.values():
            for key in keys:
                if key in tags:
                    del tags[key]
        self._apply_owned(tags, values)

        # COMMENT/DESCRIPTION and the picture block untouched, as in `write_tags`.
        audio.save()
        return before

    def _apply_owned(self, tags: Any, values: Mapping[str, Any]) -> None:
        """Write an owned snapshot into already-cleared Vorbis comments.

        Each field lands under the key `OWNED_KEYS` and `_read_owned` name for
        it; a test writes the same values through here and through `write_tags`
        and asserts both read back identically, so the two cannot drift.
        """
        for fld, key in _SINGLE_KEYS.items():
            if (value := values.get(fld)) not in (None, ""):
                tags[key] = [str(value)]
        for fld, key in _LIST_KEYS.items():
            if value := values.get(fld):
                tags[key] = [str(v) for v in value]

        # ORIGINALYEAR is derived from the date rather than stored separately —
        # `_read_owned` reads only ORIGINALDATE, so taking the year from
        # anywhere else could disagree with the date beside it.
        if original := values.get(Owned.ORIGINAL_DATE):
            tags[KEY_ORIGINAL_YEAR] = [str(original)[:4]]
