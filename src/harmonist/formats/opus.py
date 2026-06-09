"""Opus tag reader + writer.

Opus in an Ogg container uses Vorbis comments (shared logic in
`_vorbis`); cover art is a base64 FLAC picture block in the
METADATA_BLOCK_PICTURE comment, same as Ogg Vorbis.
"""

from __future__ import annotations

from pathlib import Path

from mutagen.oggopus import OggOpus

from . import _vorbis
from .types import ScanFields

EXTENSIONS = (".opus",)


def _open(path: Path) -> OggOpus | None:
    try:
        return OggOpus(path)
    except Exception:
        return None


def describe(path: Path) -> str:
    return "Opus"


_impl = _vorbis.VorbisTagger(_open, _vorbis.ogg_set_cover)

read_album_id = _impl.read_album_id
read_album_title = _impl.read_album_title
read_artist = _impl.read_artist
read_track_title = _impl.read_track_title
read_comment = _impl.read_comment
read_duration_ms = _impl.read_duration_ms
write_tags = _impl.write_tags


def read_scan_fields(path: Path) -> ScanFields:
    return _impl.read_scan_fields(path, "Opus")
