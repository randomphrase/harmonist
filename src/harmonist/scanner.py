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

from . import album_files, formats, id_registry
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

    An "album directory" is any directory that contains at least one audio
    file in a supported format — plus, per `album_files`, a sidecar'd parent
    whose audio lives in per-disc subdirectories (#16). State is derived from
    the sidecar (if present) plus a file-tag check for confirming "tagged"
    status.

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
    """Yield (album_dir, sorted_audio_files, signature) for every album
    directory under `root`, ONE DIRECTORY AT A TIME.

    An album directory is one containing supported audio — or, per
    `album_files`, one that declares itself an album with a sidecar while
    holding its audio in per-disc subdirectories (#16). A grouped album is
    yielded ONCE, for the parent, carrying every file beneath it; the
    subdirectories are then not albums in their own right and are skipped, so
    the two halves of a split release stop appearing as two Library tiles.

    Uses ``os.walk`` (not ``rglob`` + groupby) so a caller can interleave work
    between directories — the async scan runner yields to the event loop here.
    The signature is built from the same stat calls, so re-scans can skip
    unchanged albums (see ``resolve_album``).
    """
    if not root.exists():
        return
    # Parents already yielded as grouped albums. Everything below one of these
    # belongs to it, so it must not also be yielded on its own account. os.walk
    # is top-down, which is what makes a parent reliably known before its
    # children are reached.
    grouped_roots: list[Path] = []
    for dirpath, _dirnames, filenames in os.walk(root):
        d = Path(dirpath)
        if any(r in d.parents for r in grouped_roots):
            continue
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
            # No audio of its own. A sidecar here declares a release split
            # across per-disc subdirectories — collect their files under this
            # directory. Anything else is an ordinary artist/container dir.
            if sidecar_mtime is None:
                continue
            grouped = _grouped_entries(d)
            if not grouped:
                continue  # sidecar but nothing beneath it — not an album
            grouped_roots.append(d)
            audio = grouped
        else:
            audio.sort(key=lambda e: album_files.sort_key(Path(e[0].name)))
        files = [e[0] for e in audio]
        signature: AlbumSignature = (
            # Keyed on the path RELATIVE to the album dir, not the bare name:
            # a grouped album has a "01 - …" in every disc directory, and bare
            # names would collide into one indistinguishable signature entry.
            tuple((str(e[0].relative_to(d)), e[1], e[2]) for e in audio),
            sidecar_mtime,
            cover_mtime,
        )
        yield d, files, signature


def _grouped_entries(album_dir: Path) -> list[tuple[Path, int, int]]:
    """(path, mtime_ns, size) for every audio file below a grouped album dir,
    in track order. Unstattable files are dropped, exactly as in the walk above.
    """
    entries = []
    for f in album_files.descendant_audio_files(album_dir):
        try:
            st = f.stat()
        except OSError:
            continue
        if S_ISREG(st.st_mode):
            entries.append((f, st.st_mtime_ns, st.st_size))
    return entries


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
    expected = expected_tracks(fields)
    state = (
        AlbumState.INCONSISTENT
        if inconsistent_tracks
        else _derive_state(sidecar, fields, album_dir)
    )

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
        disc_num=_consistent_disc_num(fields),
        expected_track_count=expected.total,
    )


class ExpectedTracks(NamedTuple):
    """What the files themselves say the release holds, and whether it's all here.

    `total` is the release's track count, or None when it cannot be known from
    the files present — which happens when an entire medium is absent, so
    nothing on disk can say how long it was. (`total` rather than `count`
    because `count` is already a tuple method.)

    `complete` is the answer that actually drives state, and it is deliberately
    separate: an album missing a whole disc is knowably INCOMPLETE even though
    its exact total is unknowable. Reporting an unknown total as "no
    information" would derive COMPLETE for what is visibly half a box set.
    """

    total: int | None
    complete: bool


# What an album says when its files carry no counts at all. Complete, because
# "we have no idea" must not read as "tracks are missing" — that is the
# pre-#195 behaviour for an untagged album and it stays.
UNKNOWN_EXPECTED = ExpectedTracks(total=None, complete=True)


def expected_tracks(fields: list[formats.ScanFields]) -> ExpectedTracks:
    """How many tracks the release has, read from the files' own tags (#195).

    MusicBrainz told us this at tagging time and the tagger wrote it into every
    file — `trkn`'s total per medium, `disk`'s total for the release (Picard
    writes the same). So the number is already on disk, from the same source and
    the same moment as the sidecar field this replaces, and reading it costs
    nothing where re-fetching it costs one rate-limited request per album.

    Three shapes:

    * **Single medium** — every file agrees on `track_total`; that is the total.
    * **Every medium present** — sum each disc's own `track_total`. A 2-disc
      release of 11 + 10 gives 21 without asking anyone.
    * **A medium entirely absent** — `disc_total` says there are 2 discs and
      only disc 1 has files. The total is unknowable (nothing on disk describes
      disc 2), but the album is certainly incomplete.

    Files carrying no totals give `UNKNOWN_EXPECTED`, so an album Harmonist has
    never tagged is not accused of missing tracks.
    """
    present = [f for f in fields if not f.unreadable]
    if not present:
        return UNKNOWN_EXPECTED
    # Per disc, the track_total its files agree on. A disc whose files disagree
    # contributes None — a mid-retag album, and not something to average.
    by_disc: dict[int, set[int | None]] = {}
    for f in present:
        by_disc.setdefault(f.disc_num or 1, set()).add(f.track_total)
    totals = {disc: next(iter(v)) if len(v) == 1 else None for disc, v in by_disc.items()}
    if all(t is None for t in totals.values()):
        return UNKNOWN_EXPECTED

    disc_totals = {f.disc_total for f in present if f.disc_total is not None}
    expected_media = next(iter(disc_totals)) if len(disc_totals) == 1 else len(totals)
    if len(totals) < expected_media:
        # A whole medium has no files at all. Certainly incomplete; the total is
        # not knowable, because the absent disc's length was only ever recorded
        # in the files that are missing.
        return ExpectedTracks(total=None, complete=False)
    if any(t is None for t in totals.values()):
        return UNKNOWN_EXPECTED  # a disc we can't size — don't guess the album's total

    total = sum(t for t in totals.values() if t is not None)
    return ExpectedTracks(total=total, complete=len(present) >= total)


def _consistent_disc_num(fields: list[formats.ScanFields]) -> int | None:
    """The single disc number all this album's files carry, or None when they
    disagree or none is tagged. Untagged is the norm for a single-disc release,
    so None means "don't know", never "disc 0"."""
    discs = {sf.disc_num for sf in fields if sf.disc_num is not None}
    if len(discs) != 1 or any(sf.disc_num is None for sf in fields):
        return None
    return next(iter(discs))


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


def _derive_state(
    sidecar: Sidecar | None, fields: list[formats.ScanFields], album_dir: Path
) -> AlbumState:
    if sidecar is None:
        return AlbumState.NEW
    if sidecar.mb_release_id is None:
        # Single "no confirmed MB release yet" state. The card adapts on
        # whether a suggestion (mb_match_candidate) and/or store_url is
        # present — but it's all one state, so the user never round-trips
        # between "review" and "assign".
        return AlbumState.NEEDS_MBID
    unreadable = sum(1 for sf in fields if sf.unreadable)
    if unreadable:
        # A track Harmonist can't open is, for every purpose the user cares
        # about, a track they don't have — so it lands in the same state as one
        # that's absent (#112). Checked BEFORE the tagged/untagged branch,
        # because a correctly tagged album with one corrupt file would otherwise
        # come out COMPLETE and the corruption would never be shown.
        #
        # It also must not read as TAGGING, which is what it did: that invites a
        # re-tag, i.e. a WRITE to the drive that just failed a read.
        log.warning(
            "%d of %d file(s) in %s could not be read — treating the album as incomplete",
            unreadable,
            len(fields),
            album_dir,
        )
        return AlbumState.INCOMPLETE
    if _files_tagged_with(fields, sidecar.mb_release_id):
        # INCOMPLETE wins over NEEDS_SYNC: a defect the user can act on should
        # be visible even on a bandcamp album missing an item_id.
        #
        # Read from the FILES (#195), not from a sidecar field holding the same
        # number. `complete` rather than a count comparison, because an album
        # missing an entire medium is knowably incomplete with no total to
        # compare against — see `expected_tracks`.
        if not expected_tracks(fields).complete:
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
