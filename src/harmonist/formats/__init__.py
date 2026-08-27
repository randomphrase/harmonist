"""Per-format audio tag dispatch.

Scanner, reconcile, url_recovery, match, and the orchestrating tagger
all go through this module; mutagen itself stays inside the per-format
submodules (`m4a`, eventually `mp3`, `flac`, `vorbis`).

Adding a new format: implement a submodule exposing `EXTENSIONS` and the
read/write functions used here, then register it in `_MODULES`.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import ModuleType
from typing import Any

from . import flac, m4a, mp3, ogg, opus
from .types import ScanFields, TagSet, TrackTags, UnsupportedFormatError

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


# Video containers that carry the same MP4 tag atoms as `.m4a`, so a Picard-
# tagged video track states its disc, its position and its release exactly as an
# audio one does. Harmonist cannot TAG these (that is #66), which is why they are
# not `is_supported` — but it can read them, and refusing to look means an album
# whose second disc is a DVD reads as missing every one of its tracks (#193).
#
# Narrow on purpose: `.mp4` is left out because it is routinely audio, and
# guessing wrong there would feed a music file into the wrong half of the scan.
VIDEO_EXTENSIONS = frozenset({".m4v"})


def is_video(path: Path) -> bool:
    """True for a video file whose tags Harmonist can read but not write."""
    return path.suffix.lower() in VIDEO_EXTENSIONS


def read_video_scan_fields(path: Path) -> ScanFields:
    """`read_scan_fields` for a video container.

    Routed through the MP4 reader explicitly rather than through
    `_module_for`, which is keyed on the extensions Harmonist will WRITE. Adding
    `.m4v` there would let the tagger try to tag it.
    """
    from . import m4a

    return m4a.read_scan_fields(path)


def read_video_tags(path: Path) -> TrackTags:
    """`read_tags` for a video container (#226).

    Routed through the MP4 reader explicitly, for the same reason
    `read_video_scan_fields` is: `_module_for` is keyed on what Harmonist
    WRITES, and `.m4v` must stay out of it.

    A Picard-tagged video carries the whole album-level set — title, artist,
    label, catalogue number, `media`, its position and its length — so an album
    whose second disc is a DVD has 26 perfectly readable tracks that
    `read_tags` was answering None to. The `video` flag rides along so the
    comparison can report them as present without comparing them.
    """
    from . import m4a

    return replace(m4a.read_tags(path), video=True)


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


def read_scan_fields(path: Path) -> ScanFields:
    """Read the scanner's per-file fields (album title, MB album id, artist,
    codec) in a SINGLE file open. None-filled when no module handles the
    extension. Replaces N separate read_*() opens per file during a scan."""
    mod = _module_for(path)
    if mod is None:
        return ScanFields(None, None, None, None)
    fields: ScanFields = mod.read_scan_fields(path)
    return fields


def read_tags(path: Path) -> TrackTags:
    """Everything the album comparison needs from one file, in a single open
    (#106). Unlike `read_scan_fields` this reads the album-level metadata a user
    would recognise — label, catalogue number, date — not just what the scanner
    needs to derive state.

    An unsupported extension is not a read failure, so it comes back empty
    rather than flagged: nothing was wrong, there is simply nothing to read.
    """
    mod = _module_for(path)
    if mod is None:
        return TrackTags()
    tags: TrackTags = mod.read_tags(path)
    return tags


def read_cover(path: Path) -> tuple[bytes, str] | None:
    """Extract the file's embedded cover art as (image_bytes, mime_type), or
    None when there's no cover / no module for the extension."""
    mod = _module_for(path)
    if mod is None:
        return None
    result: tuple[bytes, str] | None = mod.read_cover(path)
    return result


def write_cover(path: Path, cover: bytes) -> None:
    """Replace `path`'s embedded image, leaving every tag alone.

    Separate from `write_tags` because restoring artwork (#131) must not rewrite
    tags as a side effect: the user is undoing an artwork change, and silently
    re-applying a TagSet at the same time would make the undo do more than it
    says.
    """
    mod = _module_for(path)
    if mod is None:
        raise UnsupportedFormatError(f"no audio module handles {path.suffix}")
    mod.write_cover(path, cover)


def read_owned(path: Path) -> dict[str, Any]:
    """Every field Harmonist owns, as `path` currently carries it.

    Shaped like the matching `TagSet` attribute, so the result can be diffed
    against a stored record or handed straight back to `write_owned`.

    The read-side counterpart to `write_owned`, and distinct from `read_tags`:
    that returns `TrackTags`, which is deliberately narrower — no MusicBrainz
    ids, no sort names — because it exists to be shown to a person. A revert
    needs every field it might have to put back.
    """
    mod = _module_for(path)
    if mod is None:
        raise UnsupportedFormatError(f"no audio module handles {path.suffix}")
    values: dict[str, Any] = mod.read_owned(path)
    return values


def has_superseded_tags(path: Path) -> bool:
    """Whether `path` carries a tag a write would remove and not write back.

    A backend may retire an older spelling of an owned field — MP4's legacy
    `MUSICBRAINZ_RELEASEID` — by clearing it on every write without ever
    reading it back. `read_owned` therefore cannot report it, so a file can
    match a `TagSet` on all thirty owned fields and still have something for a
    write to clean up. That is the one thing `tagger.plan_album` cannot see, and
    it has to be asked separately before deciding a file needs no write (#266).

    Not the same as "carries a tag `read_owned` skips": `ORIGINALYEAR` is also
    unread, but a write re-derives it from `ORIGINALDATE`, so its presence is
    the normal state of a correctly tagged file rather than residue. Answering
    True for it would mean re-writing every album with an original date, on
    every pass, forever — which is the failure this whole change exists to end.

    An unsupported extension answers False: nothing here writes it either.
    """
    mod = _module_for(path)
    if mod is None:
        return False
    superseded: bool = mod.has_superseded_tags(path)
    return superseded


def write_owned(path: Path, values: dict[str, Any]) -> dict[str, Any]:
    """Set every owned field on `path` to `values`, removing those absent.

    `values` is a COMPLETE owned snapshot — read one with `read_owned`, change
    what you mean to change, and pass the whole thing back. Not a patch: ID3
    packs track number and total into one TRCK frame and MP4 packs them into one
    `trkn` atom, so a writer handed half a pair would drop the other half.

    Separate from `write_tags` because a `TagSet` cannot express absence — its
    `title`, `album` and `artist` are required and written unconditionally — and
    restoring absence is exactly what undoing a first tagging has to do (#157).
    Nothing outside the owned set is touched, so the comment field and embedded
    artwork survive, as they do in `write_tags`.

    Returns the owned fields as they were BEFORE the write, so the caller can
    record what it changed without a second read.
    """
    mod = _module_for(path)
    if mod is None:
        raise UnsupportedFormatError(f"no audio module handles {path.suffix}")
    before: dict[str, Any] = mod.write_owned(path, values)
    return before


def write_tags(path: Path, tagset: TagSet, cover: bytes | None) -> dict[str, Any]:
    """Write `tagset` to `path` in its native format. `cover` is raw image
    bytes (jpeg/png) or None to leave existing cover untouched.

    Returns the owned fields (`formats.owned.Owned`) as they were BEFORE the
    write, each shaped like the matching `TagSet` attribute. The backend reads
    them from the handle it already has open, so the tagging audit (#86) gets
    its before state without a second pass over every file — which was the
    original objection to recording per-field diffs at all.
    """
    mod = _module_for(path)
    if mod is None:
        raise UnsupportedFormatError(f"no audio module handles {path.suffix}")
    before: dict[str, Any] = mod.write_tags(path, tagset, cover)
    return before


__all__ = [
    "ScanFields",
    "TagSet",
    "TrackTags",
    "UnsupportedFormatError",
    "describe",
    "has_superseded_tags",
    "is_supported",
    "read_album_id",
    "read_album_title",
    "read_artist",
    "read_comment",
    "read_cover",
    "read_duration_ms",
    "read_owned",
    "read_scan_fields",
    "read_tags",
    "read_track_title",
    "supported_extensions",
    "write_cover",
    "write_owned",
    "write_tags",
]
