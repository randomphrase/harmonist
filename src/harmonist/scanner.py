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
from typing import NamedTuple

from . import formats, id_registry
from .models import Album, AlbumState, InconsistentTrack, Sidecar, is_bandcamp_url
from .sidecar import InvalidSidecarError, UnsupportedSchemaVersionError
from .sidecar import read as read_sidecar

log = logging.getLogger(__name__)

# A cheap per-album fingerprint: (audio file name+mtime_ns+size tuples, sidecar
# mtime_ns, cover mtime_ns). Changes whenever anything that affects an album's
# derived state could change, so it drives the re-scan cache below. The FIRST
# element (the audio tuples) is the "audio signature": it changes only when the
# tracks themselves change — used to skip the expensive tag reads when just the
# sidecar/cover moved (see resolve_album / the scan runner).
AlbumSignature = tuple[tuple[tuple[str, int, int], ...], int | None, int | None]
# Persistent {album_dir: (full_signature, Album, fields)} threaded across scans.
# Two-level: a FULL-signature hit returns the cached Album with zero I/O; when
# only the sidecar/cover changed (audio signature == cached audio signature) the
# cached `fields` (the mutagen tag reads) are REUSED and only the cheap sidecar +
# cover are re-read. So a sidecar-only change (a sync link, a reconcile) no longer
# forces a full tag re-read of the album.
AlbumCache = dict[Path, tuple[AlbumSignature, Album, list["formats.ScanFields"]]]


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
    cached = album_cache.get(album_dir) if album_cache is not None else None
    if cached is not None and cached[0] == signature:
        return cached[1]  # nothing changed → cached Album, zero I/O
    # Audio unchanged (only the sidecar/cover moved)? Reuse the cached tag fields
    # so we skip the per-track mutagen reads — only the cheap sidecar + cover are
    # re-read below.
    reuse = cached[2] if (cached is not None and cached[0][0] == signature[0]) else None
    try:
        io = read_album_io(album_dir, audio_files, reuse)
        album = build_album(album_dir, audio_files, io)
    except (InvalidSidecarError, UnsupportedSchemaVersionError) as e:
        log.warning("skipping %s: %s", album_dir, e)
        return None
    except Exception as e:
        log.warning("error scanning %s: %s", album_dir, e)
        return None
    if album_cache is not None:
        album_cache[album_dir] = (signature, album, io.fields)
    return album


def prune_cache(album_cache: AlbumCache, seen: set[Path]) -> None:
    """Drop cache entries for album dirs not present in `seen` (removed dirs)."""
    for stale in [p for p in album_cache if p not in seen]:
        del album_cache[stale]


class AlbumIO(NamedTuple):
    """Everything for one album that requires blocking filesystem I/O —
    sidecar JSON, each track's tags, and the cover lookup. Produced by
    `read_album_io` (safe to run in a worker thread: pure I/O, no shared
    state) and consumed by `build_album` (CPU only, runs on the event loop)."""

    sidecar: Sidecar | None
    fields: list[formats.ScanFields]
    cover_path: Path | None


def read_album_io(
    album_dir: Path,
    audio_files: list[Path],
    reuse_fields: list[formats.ScanFields] | None = None,
) -> AlbumIO:
    """Do an album's blocking reads in one place: the sidecar, each track's tags
    (one open per file), and the cover lookup. Touches no shared state, so the
    async scan runner can hand this to a worker thread.

    `reuse_fields`: when the audio files are unchanged since the last scan, the
    caller passes the previously-read tag fields so we SKIP the per-track mutagen
    reads (the expensive part) and only re-read the cheap sidecar + cover.
    """
    return AlbumIO(
        sidecar=read_sidecar(album_dir),
        fields=reuse_fields
        if reuse_fields is not None
        else [formats.read_scan_fields(f) for f in audio_files],
        cover_path=_find_cover(album_dir),
    )


def _display_artist(fields: list[formats.ScanFields]) -> str:
    """The album-level artist to show. Prefer the album-artist tag (aART / TPE2 /
    ALBUMARTIST — authoritative, and "Various Artists" on a Picard-tagged
    compilation). When it's absent, fall back to "Various Artists" if the tracks
    disagree on artist (an untagged compilation), else the single track artist."""
    if not fields:
        return ""
    album_artist = (fields[0].album_artist or "").strip()
    if album_artist:
        return album_artist
    distinct = {(f.artist or "").strip() for f in fields if (f.artist or "").strip()}
    if len(distinct) > 1:
        return "Various Artists"
    return (fields[0].artist or "").strip()


def build_album(album_dir: Path, audio_files: list[Path], io: AlbumIO) -> Album:
    """Assemble the Album from pre-read I/O. CPU + id-registry only (no file
    I/O), so it runs on the event-loop thread where the shared registry lives."""
    sidecar = io.sidecar
    fields = io.fields
    title = (fields[0].album_title if fields else None) or album_dir.name
    artist = _display_artist(fields)

    # Inconsistency trumps sidecar-driven state — see design §15.2.
    # The sidecar is kept on disk; once the user fixes the on-disk tags
    # via Picard, the next scan re-derives state from the sidecar.
    inconsistent_tracks = _check_consistency(audio_files, fields)
    state = AlbumState.INCONSISTENT if inconsistent_tracks else _derive_state(sidecar, fields)

    return Album(
        id=_album_id(album_dir, sidecar),
        path=album_dir,
        title=title,
        artist=artist,
        track_count=len(audio_files),
        state=state,
        sidecar=sidecar,
        cover_path=io.cover_path,
        inconsistent_tracks=inconsistent_tracks,
        partial_tag_count=_partial_tag_count(sidecar, fields),
        audio_format=_audio_format(fields),
        # A cover exists if there's a folder cover.* OR the first track has
        # embedded art (album art is on every track; first is representative).
        has_cover=io.cover_path is not None or (bool(fields) and fields[0].has_cover),
        # Reconcilable iff some track carries an MB Album Id atom (matches what
        # reconcile.reconcile_album reads). Lets the inbox skip kicking
        # reconcile for untagged orphans it could never resolve.
        has_tag_mbid=any(sf.album_id for sf in fields),
    )


def _audio_format(fields: list[formats.ScanFields]) -> str | None:
    """Distinct codec label across the album's files. A single value when
    consistent (the norm), "Mixed" when files differ."""
    labels = {sf.codec for sf in fields}
    labels.discard(None)
    if not labels:
        return None
    if len(labels) == 1:
        return next(iter(labels))
    return "Mixed"


def _partial_tag_count(
    sidecar: Sidecar | None,
    fields: list[formats.ScanFields],
) -> tuple[int, int] | None:
    """Return `(tagged, total)` when only some files carry the matching
    MB Album Id atom (0 < tagged < total). None when fully tagged, none
    tagged, or when there's no MBID to compare against. Quality indicator
    only — does not affect state (§15.1).
    """
    if not sidecar or not sidecar.mb_release_id or not fields:
        return None
    tagged = _count_files_tagged_with(fields, sidecar.mb_release_id)
    total = len(fields)
    if 0 < tagged < total:
        return (tagged, total)
    return None


def _count_files_tagged_with(fields: list[formats.ScanFields], mbid: str) -> int:
    """Return how many files carry an MB Album Id matching `mbid`."""
    return sum(1 for sf in fields if sf.album_id == mbid)


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


def _derive_state(sidecar: Sidecar | None, fields: list[formats.ScanFields]) -> AlbumState:
    if sidecar is None:
        return AlbumState.NEW
    if sidecar.mb_release_id is None:
        # Single "no confirmed MB release yet" state. The card adapts on
        # whether a suggestion (mb_match_candidate) and/or store_url is
        # present — but it's all one state, so the user never round-trips
        # between "review" and "assign".
        return AlbumState.NEEDS_MBID
    if _files_tagged_with(fields, sidecar.mb_release_id):
        # INCOMPLETE wins over NEEDS_SYNC when set: the user has explicitly
        # confirmed-as-incomplete (track_count_expected only gets set at
        # tag time), and that intent should be visible even on bandcamp
        # albums missing an item_id.
        if sidecar.track_count_expected is not None and len(fields) < sidecar.track_count_expected:
            return AlbumState.INCOMPLETE
        # NEEDS_SYNC: Bandcamp-sourced album, MB release known, files tagged,
        # but Bandcamp item_id not yet linked (a Sync run resolves this).
        # An *ambiguous* link (candidate_item_ids set — several editions share a
        # store URL and a title tiebreak couldn't separate them) is as resolved
        # as we can get, so it's NOT NEEDS_SYNC: fall through to COMPLETE.
        bc = sidecar.bandcamp
        if (
            is_bandcamp_url(sidecar.store_url)
            and (bc is None or bc.item_id is None)
            and not (bc is not None and bc.candidate_item_ids)
            # The user accepted "no purchase available" (withdrawn/ripped/elsewhere):
            # it's a terminal Library album, not something a sync can resolve.
            and not sidecar.purchase_unavailable
        ):
            return AlbumState.NEEDS_SYNC
        return AlbumState.COMPLETE
    return AlbumState.TAGGING


def _check_consistency(
    audio_files: list[Path], fields: list[formats.ScanFields]
) -> list[InconsistentTrack]:
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
        InconsistentTrack(file_name=f.name, album_title=sf.album_title, mb_album_id=sf.album_id)
        for f, sf in zip(audio_files, fields, strict=True)
    ]

    titles = {r.album_title for r in rows if r.album_title is not None}
    mbids = {r.mb_album_id for r in rows if r.mb_album_id is not None}
    if len(titles) > 1 or len(mbids) > 1:
        return rows
    return []


def _files_tagged_with(fields: list[formats.ScanFields], mbid: str) -> bool:
    """True iff at least one file carries an MB Album Id matching mbid."""
    return _count_files_tagged_with(fields, mbid) > 0


def _find_cover(album_dir: Path) -> Path | None:
    for ext in (".jpg", ".png"):
        p = album_dir / f"cover{ext}"
        if p.exists():
            return p
    return None
