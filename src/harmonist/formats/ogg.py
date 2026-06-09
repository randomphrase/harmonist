"""Ogg Vorbis tag reader + writer.

Vorbis comments (shared logic in `_vorbis`); cover art is a base64 FLAC
picture block in the METADATA_BLOCK_PICTURE comment (Ogg has no native
picture API).
"""

from __future__ import annotations

from pathlib import Path

from mutagen.oggvorbis import OggVorbis

from . import _vorbis
from .types import ScanFields

EXTENSIONS = (".ogg", ".oga")


def _open(path: Path) -> OggVorbis | None:
    try:
        return OggVorbis(path)
    except Exception:
        return None


def describe(path: Path) -> str:
    return "Vorbis"


_impl = _vorbis.VorbisTagger(_open, _vorbis.ogg_set_cover)

read_album_id = _impl.read_album_id
read_album_title = _impl.read_album_title
read_artist = _impl.read_artist
read_track_title = _impl.read_track_title
read_comment = _impl.read_comment
read_duration_ms = _impl.read_duration_ms
write_tags = _impl.write_tags


def read_scan_fields(path: Path) -> ScanFields:
    return _impl.read_scan_fields(path, "Vorbis")
