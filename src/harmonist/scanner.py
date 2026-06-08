"""Walk the music dir, build Album objects from sidecar + filesystem state.

Audio-file I/O goes through `harmonist.formats` so this module is
format-agnostic — adding MP3/FLAC/Ogg/Opus only requires registering a
new submodule in `formats/__init__.py`.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from pathlib import Path
from stat import S_ISREG

from . import formats, id_registry
from .models import Album, AlbumState, InconsistentTrack, Sidecar, is_bandcamp_url
from .sidecar import InvalidSidecarError, UnsupportedSchemaVersionError
from .sidecar import read as read_sidecar

log = logging.getLogger(__name__)

# A cheap per-album fingerprint: (audio file name+mtime_ns+size tuples, sidecar
# mtime_ns, cover mtime_ns). Changes whenever anything that affects an album's
# derived state could change, so it drives the re-scan cache below.
AlbumSignature = tuple[tuple[tuple[str, int, int], ...], int | None, int | None]
# Persistent {album_dir: (signature, Album)} the caller threads across scans so
# unchanged albums skip all tag reads on a re-scan.
AlbumCache = dict[Path, tuple[AlbumSignature, Album]]


def scan(music_dir: Path, *, album_cache: AlbumCache | None = None) -> list[Album]:
    """Return one Album for every album directory under music_dir.

    An "album directory" is any directory that contains at least one
    audio file in a supported format. State is derived from the sidecar
    (if present) plus a file-tag check for confirming "tagged" status.

    Pass a persistent ``album_cache`` dict to skip re-reading tags for
    albums whose on-disk fingerprint (file mtimes/sizes + sidecar + cover)
    is unchanged since the last scan — the big win on slow filesystems.
    Omit it (the default) for a full, uncached scan.
    """
    albums: list[Album] = []
    for album_dir, audio_files, signature in iter_album_dirs(music_dir):
        album = resolve_album(album_dir, audio_files, signature, album_cache)
        if album is not None:
            albums.append(album)
    if album_cache is not None:
        prune_cache(album_cache, {a.path for a in albums})
    return albums


def iter_album_dirs(root: Path) -> Iterator[tuple[Path, list[Path], AlbumSignature]]:
    """Yield (album_dir, sorted_audio_files, signature) for every directory
    containing supported audio, ONE DIRECTORY AT A TIME.

    Uses ``os.walk`` (not ``rglob`` + groupby) so a caller can interleave work
    between directories — the async scan runner yields to the event loop here.
    The signature is built from the same stat calls, so re-scans can skip
    unchanged albums (see ``resolve_album``).
    """
    if not root.exists():
        return
    for dirpath, _dirnames, filenames in os.walk(root):
        d = Path(dirpath)
        audio: list[tuple[Path, int, int]] = []
        sidecar_mtime: int | None = None
        cover_mtime: int | None = None
        for name in filenames:
            f = d / name
            try:
                st = f.stat()
            except OSError:
                continue
            if not S_ISREG(st.st_mode):
                continue
            if formats.is_supported(f):
                audio.append((f, st.st_mtime_ns, st.st_size))
            elif name == ".harmonist.json":
                sidecar_mtime = st.st_mtime_ns
            elif name in ("cover.jpg", "cover.png"):
                cover_mtime = st.st_mtime_ns
        if not audio:
            continue
        audio.sort(key=lambda e: e[0].name)
        files = [e[0] for e in audio]
        signature: AlbumSignature = (
            tuple((e[0].name, e[1], e[2]) for e in audio),
            sidecar_mtime,
            cover_mtime,
        )
        yield d, files, signature


def resolve_album(
    album_dir: Path,
    audio_files: list[Path],
    signature: AlbumSignature,
    album_cache: AlbumCache | None,
) -> Album | None:
    """Return the Album for one directory: from the cache when its signature
    is unchanged, else freshly built (reading tags). Returns None — logging a
    warning — when the album can't be built (bad sidecar, I/O error).
    """
    if album_cache is not None:
        cached = album_cache.get(album_dir)
        if cached is not None and cached[0] == signature:
            return cached[1]
    try:
        album = _build_album(album_dir, audio_files)
    except (InvalidSidecarError, UnsupportedSchemaVersionError) as e:
        log.warning("skipping %s: %s", album_dir, e)
        return None
    except Exception as e:
        log.warning("error scanning %s: %s", album_dir, e)
        return None
    if album_cache is not None:
        album_cache[album_dir] = (signature, album)
    return album


def prune_cache(album_cache: AlbumCache, seen: set[Path]) -> None:
    """Drop cache entries for album dirs not present in `seen` (removed dirs)."""
    for stale in [p for p in album_cache if p not in seen]:
        del album_cache[stale]


def _build_album(album_dir: Path, audio_files: list[Path]) -> Album:
    sidecar = read_sidecar(album_dir)
    title, artist = _read_album_artist(audio_files[0]) if audio_files else ("", "")
    if not title:
        title = album_dir.name

    # Inconsistency trumps sidecar-driven state — see design §15.2.
    # The sidecar is kept on disk; once the user fixes the on-disk tags
    # via Picard, the next scan re-derives state from the sidecar.
    inconsistent_tracks = _check_consistency(audio_files)
    state = AlbumState.INCONSISTENT if inconsistent_tracks else _derive_state(sidecar, audio_files)

    cover_path = _find_cover(album_dir)

    return Album(
        id=_album_id(album_dir, sidecar),
        path=album_dir,
        title=title,
        artist=artist,
        track_count=len(audio_files),
        state=state,
        sidecar=sidecar,
        cover_path=cover_path,
        inconsistent_tracks=inconsistent_tracks,
        partial_tag_count=_partial_tag_count(sidecar, audio_files),
        audio_format=_audio_format(audio_files),
    )


def _audio_format(audio_files: list[Path]) -> str | None:
    """Distinct codec label across the album's files. A single value when
    consistent (the norm), "Mixed" when files differ."""
    labels = {formats.describe(f) for f in audio_files}
    labels.discard(None)
    if not labels:
        return None
    if len(labels) == 1:
        return next(iter(labels))
    return "Mixed"


def _partial_tag_count(
    sidecar: Sidecar | None,
    audio_files: list[Path],
) -> tuple[int, int] | None:
    """Return `(tagged, total)` when only some files carry the matching
    MB Album Id atom (0 < tagged < total). None when fully tagged, none
    tagged, or when there's no MBID to compare against. Quality indicator
    only — does not affect state (§15.1).
    """
    if not sidecar or not sidecar.mb_release_id or not audio_files:
        return None
    tagged = _count_files_tagged_with(audio_files, sidecar.mb_release_id)
    total = len(audio_files)
    if 0 < tagged < total:
        return (tagged, total)
    return None


def _count_files_tagged_with(audio_files: list[Path], mbid: str) -> int:
    """Return how many of the given files carry an MB Album Id atom
    matching `mbid`."""
    return sum(1 for f in audio_files if formats.read_album_id(f) == mbid)


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


def _derive_state(sidecar: Sidecar | None, audio_files: list[Path]) -> AlbumState:
    if sidecar is None:
        return AlbumState.NEW
    if sidecar.mb_release_id is None:
        # Single "no confirmed MB release yet" state. The card adapts on
        # whether a suggestion (mb_match_candidate) and/or store_url is
        # present — but it's all one state, so the user never round-trips
        # between "review" and "assign".
        return AlbumState.NEEDS_MBID
    if _files_tagged_with(audio_files, sidecar.mb_release_id):
        # INCOMPLETE wins over NEEDS_SYNC when set: the user has explicitly
        # confirmed-as-incomplete (track_count_expected only gets set at
        # tag time), and that intent should be visible even on bandcamp
        # albums missing an item_id.
        if (
            sidecar.track_count_expected is not None
            and len(audio_files) < sidecar.track_count_expected
        ):
            return AlbumState.INCOMPLETE
        # NEEDS_SYNC: Bandcamp-sourced album, MB release known, files tagged,
        # but Bandcamp item_id not yet linked (a Sync run resolves this).
        if is_bandcamp_url(sidecar.store_url) and (
            sidecar.bandcamp is None or sidecar.bandcamp.item_id is None
        ):
            return AlbumState.NEEDS_SYNC
        return AlbumState.COMPLETE
    return AlbumState.TAGGING


def _check_consistency(audio_files: list[Path]) -> list[InconsistentTrack]:
    """Detect mixed-album dirs: files disagreeing on album title or MB
    Album Id. Compilations (varying artist, consistent album + MBID) are
    NOT inconsistent and produce an empty list.

    Files missing either field don't vote — partial tagging is handled
    separately (§15.1). Returns one row per file when inconsistent,
    empty list when consistent.
    """
    if len(audio_files) < 2:
        return []  # single-file album can't be inconsistent

    rows = [
        InconsistentTrack(
            file_name=f.name,
            album_title=formats.read_album_title(f),
            mb_album_id=formats.read_album_id(f),
        )
        for f in audio_files
    ]

    titles = {r.album_title for r in rows if r.album_title is not None}
    mbids = {r.mb_album_id for r in rows if r.mb_album_id is not None}
    if len(titles) > 1 or len(mbids) > 1:
        return rows
    return []


def _files_tagged_with(audio_files: list[Path], mbid: str) -> bool:
    """True iff at least one file's MB Album Id atom matches mbid."""
    return _count_files_tagged_with(audio_files, mbid) > 0


def _read_album_artist(file_path: Path) -> tuple[str, str]:
    return (formats.read_album_title(file_path) or "", formats.read_artist(file_path) or "")


def _find_cover(album_dir: Path) -> Path | None:
    for ext in (".jpg", ".png"):
        p = album_dir / f"cover{ext}"
        if p.exists():
            return p
    return None
