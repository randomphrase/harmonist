"""Per-format audio tag dispatch.

Scanner, reconcile, url_recovery, match, and the orchestrating tagger
all go through this module; mutagen itself stays inside the per-format
submodules (`m4a`, eventually `mp3`, `flac`, `vorbis`).

Adding a new format: implement a submodule exposing `EXTENSIONS` and the
read/write functions used here, then register it in `_MODULES`.
"""

from __future__ import annotations

from pathlib import Path
from types import ModuleType

from . import flac, m4a, mp3, ogg, opus
from .types import TagSet, UnsupportedFormatError

_MODULES: tuple[ModuleType, ...] = (m4a, mp3, flac, ogg, opus)


def supported_extensions() -> tuple[str, ...]:
    """All extensions (lowercase, leading dot) any audio module handles."""
    out: list[str] = []
    for mod in _MODULES:
        out.extend(mod.EXTENSIONS)
    return tuple(out)


def _module_for(path: Path) -> ModuleType | None:
    suffix = path.suffix.lower()
    for mod in _MODULES:
        if suffix in mod.EXTENSIONS:
            return mod
    return None


def is_supported(path: Path) -> bool:
    return _module_for(path) is not None


def read_album_id(path: Path) -> str | None:
    mod = _module_for(path)
    return mod.read_album_id(path) if mod else None


def read_album_title(path: Path) -> str | None:
    mod = _module_for(path)
    return mod.read_album_title(path) if mod else None


def read_artist(path: Path) -> str | None:
    mod = _module_for(path)
    return mod.read_artist(path) if mod else None


def read_track_title(path: Path) -> str | None:
    mod = _module_for(path)
    return mod.read_track_title(path) if mod else None


def read_comment(path: Path) -> str | None:
    mod = _module_for(path)
    return mod.read_comment(path) if mod else None


def read_duration_ms(path: Path) -> int | None:
    mod = _module_for(path)
    return mod.read_duration_ms(path) if mod else None


def describe(path: Path) -> str | None:
    """Short human label for the file's codec/format (e.g. "ALAC", "MP3",
    "FLAC"). None if no module handles the extension."""
    mod = _module_for(path)
    return mod.describe(path) if mod else None


def write_tags(path: Path, tagset: TagSet, cover: bytes | None) -> None:
    """Write `tagset` to `path` in its native format. `cover` is raw image
    bytes (jpeg/png) or None to leave existing cover untouched.
    """
    mod = _module_for(path)
    if mod is None:
        raise UnsupportedFormatError(f"no audio module handles {path.suffix}")
    mod.write_tags(path, tagset, cover)


__all__ = [
    "TagSet",
    "UnsupportedFormatError",
    "is_supported",
    "read_album_id",
    "read_album_title",
    "read_artist",
    "read_comment",
    "read_duration_ms",
    "read_track_title",
    "supported_extensions",
    "write_tags",
]
