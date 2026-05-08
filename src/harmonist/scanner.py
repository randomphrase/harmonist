"""Walk the music dir, build Album objects from sidecar + filesystem state."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterator

from mutagen.mp4 import MP4

from .models import Album, AlbumState, Sidecar
from .sidecar import InvalidSidecar, UnsupportedSchemaVersion, read as read_sidecar
from .tagger import ATOM_MB_ALBUM_ID


log = logging.getLogger(__name__)


def scan(music_dir: Path) -> list[Album]:
    """Return one Album for every album directory under music_dir.

    An "album directory" is any directory that contains at least one .m4a file.
    State is derived from the sidecar (if present) plus a file-tag check for
    confirming "tagged" status.
    """
    return list(_iter_albums(music_dir))


def _iter_albums(root: Path) -> Iterator[Album]:
    if not root.exists():
        return
    for album_dir, m4a_files in _find_album_dirs(root):
        try:
            yield _build_album(album_dir, m4a_files)
        except (InvalidSidecar, UnsupportedSchemaVersion) as e:
            log.warning("skipping %s: %s", album_dir, e)
            continue
        except Exception as e:
            log.warning("error scanning %s: %s", album_dir, e)
            continue


def _find_album_dirs(root: Path) -> Iterator[tuple[Path, list[Path]]]:
    """Yield (album_dir, sorted_m4a_files) for every dir containing .m4a files."""
    by_dir: dict[Path, list[Path]] = {}
    for f in root.rglob("*.m4a"):
        if f.is_file():
            by_dir.setdefault(f.parent, []).append(f)
    for d, files in by_dir.items():
        yield d, sorted(files)


def _build_album(album_dir: Path, m4a_files: list[Path]) -> Album:
    sidecar = read_sidecar(album_dir)
    title, artist = _read_album_artist(m4a_files[0]) if m4a_files else ("", "")
    if not title:
        title = album_dir.name

    state = _derive_state(sidecar, m4a_files)
    cover_path = _find_cover(album_dir)

    return Album(
        id=Album.make_id(album_dir),
        path=album_dir,
        title=title,
        artist=artist,
        track_count=len(m4a_files),
        state=state,
        sidecar=sidecar,
        cover_path=cover_path,
    )


def _derive_state(sidecar: Sidecar | None, m4a_files: list[Path]) -> AlbumState:
    if sidecar is None:
        return AlbumState.ORPHAN
    if sidecar.mb_release_id is None:
        if sidecar.mb_match_candidate is not None:
            return AlbumState.NEEDS_CONFIRMATION
        return (
            AlbumState.HELD_BANDCAMP
            if sidecar.source == "bandcamp"
            else AlbumState.HELD_MANUAL
        )
    if _files_tagged_with(m4a_files, sidecar.mb_release_id):
        return AlbumState.DONE
    return AlbumState.TAGGING


def _files_tagged_with(m4a_files: list[Path], mbid: str) -> bool:
    """True iff at least one file's MB Album Id atom matches mbid."""
    for f in m4a_files:
        try:
            audio = MP4(f)
        except Exception:
            continue
        atom_value = audio.get(ATOM_MB_ALBUM_ID)
        if not atom_value:
            continue
        try:
            value = atom_value[0].decode("utf-8")
        except (AttributeError, UnicodeDecodeError):
            continue
        if value == mbid:
            return True
    return False


def _read_album_artist(file_path: Path) -> tuple[str, str]:
    try:
        audio = MP4(file_path)
    except Exception:
        return "", ""
    title = (audio.get("\xa9alb") or [""])[0] or ""
    artist = (audio.get("\xa9ART") or [""])[0] or ""
    return title, artist


def _find_cover(album_dir: Path) -> Path | None:
    for ext in (".jpg", ".png"):
        p = album_dir / f"cover{ext}"
        if p.exists():
            return p
    return None
