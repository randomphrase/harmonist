"""FLAC tag reader + writer.

FLAC uses Vorbis comments (shared logic in `_vorbis`) plus a native
embedded-picture API for cover art.
"""

from __future__ import annotations

from pathlib import Path

from mutagen.flac import FLAC

from . import _vorbis
from .types import ScanFields

EXTENSIONS = (".flac",)


def _open(path: Path) -> FLAC | None:
    try:
        return FLAC(path)
    except Exception:
        return None


def _set_cover(audio: FLAC, cover: bytes) -> None:
    audio.clear_pictures()
    audio.add_picture(_vorbis.make_picture(cover))


def describe(path: Path) -> str:
    return "FLAC"


_impl = _vorbis.VorbisTagger(_open, _set_cover)

read_album_id = _impl.read_album_id
read_album_title = _impl.read_album_title
read_artist = _impl.read_artist
read_track_title = _impl.read_track_title
read_comment = _impl.read_comment
read_duration_ms = _impl.read_duration_ms
write_tags = _impl.write_tags


def read_scan_fields(path: Path) -> ScanFields:
    return _impl.read_scan_fields(path, "FLAC")
