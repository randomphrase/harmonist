"""Walk the music dir, build Album objects from sidecar + filesystem state."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterator

from mutagen.mp4 import MP4

from . import id_registry
from .models import Album, AlbumState, InconsistentTrack, Sidecar, is_bandcamp_url
from .sidecar import InvalidSidecar, UnsupportedSchemaVersion, read as read_sidecar
from .tagger import ATOM_ALBUM, ATOM_ARTIST, ATOM_MB_ALBUM_ID


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

    # Inconsistency trumps sidecar-driven state — see design §15.2.
    # The sidecar is kept on disk; once the user fixes the on-disk tags
    # via Picard, the next scan re-derives state from the sidecar.
    inconsistent_tracks = _check_consistency(m4a_files)
    if inconsistent_tracks:
        state = AlbumState.INCONSISTENT
    else:
        state = _derive_state(sidecar, m4a_files)

    cover_path = _find_cover(album_dir)

    return Album(
        id=_album_id(album_dir, sidecar),
        path=album_dir,
        title=title,
        artist=artist,
        track_count=len(m4a_files),
        state=state,
        sidecar=sidecar,
        cover_path=cover_path,
        inconsistent_tracks=inconsistent_tracks,
    )


def _album_id(album_dir: Path, sidecar: Sidecar | None) -> str:
    """Canonical id: sidecar.mb_release_id (preferred), else sidecar.temp_uid,
    else a registry-minted UUID for NEW albums.
    """
    if sidecar:
        if sidecar.mb_release_id:
            return sidecar.mb_release_id
        if sidecar.temp_uid:
            return sidecar.temp_uid
    return id_registry.get_or_mint(album_dir)


def _derive_state(sidecar: Sidecar | None, m4a_files: list[Path]) -> AlbumState:
    if sidecar is None:
        return AlbumState.NEW
    if sidecar.mb_release_id is None:
        if sidecar.mb_match_candidate is not None:
            return AlbumState.NEEDS_REVIEW
        # Single state for "we don't have an MB release yet" — the card
        # template branches on whether store_url is present.
        return AlbumState.NEEDS_MBID
    if _files_tagged_with(m4a_files, sidecar.mb_release_id):
        # NEEDS_SYNC: Bandcamp-sourced album, MB release known, files tagged,
        # but Bandcamp item_id not yet linked (a Sync run resolves this).
        if (
            is_bandcamp_url(sidecar.store_url)
            and (sidecar.bandcamp is None or sidecar.bandcamp.item_id is None)
        ):
            return AlbumState.NEEDS_SYNC
        return AlbumState.DONE
    return AlbumState.TAGGING


def _check_consistency(m4a_files: list[Path]) -> list[InconsistentTrack]:
    """Detect mixed-album dirs: files disagreeing on album title (`©alb`)
    or MB Album Id. Compilations (varying artist, consistent album+MBID)
    are NOT inconsistent and produce an empty list.

    Files missing a `©alb` or MBID atom don't vote — partial tagging is
    handled separately (§15.1). Returns one row per file when inconsistent,
    empty list when consistent.
    """
    if len(m4a_files) < 2:
        return []  # single-file album can't be inconsistent

    rows: list[InconsistentTrack] = []
    for f in m4a_files:
        try:
            audio = MP4(f)
        except Exception:
            continue
        title_atom = audio.get(ATOM_ALBUM) or []
        album_title = title_atom[0] if title_atom else None
        atom = audio.get(ATOM_MB_ALBUM_ID)
        mb_album_id: str | None = None
        if atom:
            try:
                mb_album_id = atom[0].decode("utf-8")
            except (AttributeError, UnicodeDecodeError):
                pass
        rows.append(InconsistentTrack(
            file_name=f.name,
            album_title=album_title,
            mb_album_id=mb_album_id,
        ))

    titles = {r.album_title for r in rows if r.album_title is not None}
    mbids = {r.mb_album_id for r in rows if r.mb_album_id is not None}
    if len(titles) > 1 or len(mbids) > 1:
        return rows
    return []


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
    title = (audio.get(ATOM_ALBUM) or [""])[0] or ""
    artist = (audio.get(ATOM_ARTIST) or [""])[0] or ""
    return title, artist


def _find_cover(album_dir: Path) -> Path | None:
    for ext in (".jpg", ".png"):
        p = album_dir / f"cover{ext}"
        if p.exists():
            return p
    return None
