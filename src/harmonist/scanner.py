"""Walk the music dir, build Album objects from sidecar + filesystem state.

Audio-file I/O goes through `harmonist.formats` so this module is
format-agnostic — adding MP3/FLAC/Ogg/Opus only requires registering a
new submodule in `formats/__init__.py`.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable, Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from stat import S_ISREG
from typing import NamedTuple

from . import album_files, compare, formats, id_registry
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
# The third element is the whole `AlbumIO`, not just the tag fields: merging by
# identity (#197) rebuilds an album from its parts' combined reads, and that needs
# the cover and the video fields too.
AlbumCache = dict[Path, tuple[AlbumSignature, Album, "AlbumIO"]]


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
    scanned: list[ScannedDir] = []
    for album_dir, audio, videos, signature in iter_album_dirs(music_dir):
        entry = resolve_dir(album_dir, audio, videos, signature, album_cache)
        if entry is not None:
            scanned.append(entry)
    if album_cache is not None:
        prune_cache(album_cache, {e.album.path for e in scanned})
    return merge_by_identity(scanned)


def iter_album_dirs(root: Path) -> Iterator[tuple[Path, list[Path], list[Path], AlbumSignature]]:
    """Yield (album_dir, audio_files, video_files, signature) for every album
    directory under `root`, ONE DIRECTORY AT A TIME.

    Video files are yielded separately because Harmonist reads them but never
    writes them (#193, #66): they count toward whether an album is complete, and
    must stay out of everything that tags.

    An album directory is simply one containing supported audio. Which
    directories belong to the SAME album is not decided here — that is
    `merge_by_identity`, afterwards, from the release ids in the tags (#197).
    Keeping the walk per-directory is what lets the cache stay per-directory too.

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
        video: list[tuple[Path, int, int]] = []
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
            elif formats.is_video(f):
                # Not audio, but tracks all the same — see `album_files.video_files`.
                video.append((f, st.st_mtime_ns, st.st_size))
            elif name == ".harmonist.json":
                sidecar_mtime = st.st_mtime_ns
            elif name in ("cover.jpg", "cover.png"):
                cover_mtime = st.st_mtime_ns
        if not audio:
            continue  # not an album directory
        audio.sort(key=lambda e: album_files.sort_key(Path(e[0].name)))
        video.sort(key=lambda e: album_files.sort_key(Path(e[0].name)))
        files = [e[0] for e in audio]
        videos = [e[0] for e in video]
        signature: AlbumSignature = (
            # Video files ride in the SAME tuple as the audio. They are read for
            # completeness (#193), so a video appearing or vanishing changes what
            # the album derives — and a signature blind to them would serve a
            # stale Album from the cache after a DVD rip landed.
            #
            # Bare names, because everything here is in `d`: this walk is
            # per-directory again since #197, so nothing can collide. It briefly
            # keyed on the relative path, when one entry could span disc
            # subdirectories.
            tuple((e[0].name, e[1], e[2]) for e in [*audio, *video]),
            sidecar_mtime,
            cover_mtime,
        )
        yield d, files, videos, signature


def _written_at(signature: AlbumSignature) -> datetime | None:
    """When this album's files were last written, from the mtimes the walk has
    already stat'ed. None for an album with no files.

    Compared against `sidecar.tagged_at` to notice a re-tag done outside
    Harmonist (#220) — so it costs nothing beyond what the signature already
    holds.
    """
    entries = signature[0]
    if not entries:
        return None
    return datetime.fromtimestamp(max(e[1] for e in entries) / 1e9, tz=UTC)


class ScannedDir(NamedTuple):
    """One directory's worth of scan output, before identity grouping.

    The files and fields ride along with the Album because `merge_by_identity`
    has to REBUILD a merged album from the combined tags — state, the expected
    track count and the partial-tag count are all derived from the whole album's
    files, and re-reading them to merge would undo the caching this scan exists
    to preserve.
    """

    album: Album
    files: list[Path]
    io: AlbumIO


def resolve_dir(
    album_dir: Path,
    audio_files: list[Path],
    video_files: list[Path],
    signature: AlbumSignature,
    album_cache: AlbumCache | None,
) -> ScannedDir | None:
    """Return one directory's scan output: from the cache when its signature is
    unchanged, else freshly built (reading tags). Returns None — logging a
    warning — when it can't be built (bad sidecar, I/O error).

    One DIRECTORY, not one album: since #197 an album can span several, and
    which ones go together is decided afterwards by `merge_by_identity`. The
    walk and the cache stay per-directory precisely so that decision costs no
    extra I/O.
    """
    cached = album_cache.get(album_dir) if album_cache is not None else None
    if cached is not None and cached[0] == signature:
        # Nothing changed → cached Album, zero I/O.
        return ScannedDir(cached[1], audio_files, cached[2])
    # Audio unchanged (only the sidecar/cover moved)? Reuse the cached tag fields
    # so we skip the per-track mutagen reads — only the cheap sidecar + cover are
    # re-read below.
    reuse = cached[2].fields if (cached is not None and cached[0][0] == signature[0]) else None
    try:
        io = read_album_io(album_dir, audio_files, video_files, reuse)
        album = build_album(album_dir, audio_files, io, _written_at(signature))
    except (InvalidSidecarError, UnsupportedSchemaVersionError) as e:
        log.warning("skipping %s: %s", album_dir, e)
        return None
    except Exception as e:
        log.warning("error scanning %s: %s", album_dir, e)
        return None
    if album_cache is not None:
        album_cache[album_dir] = (signature, album, io)
    return ScannedDir(album, audio_files, io)


def merge_by_identity(scanned: list[ScannedDir]) -> list[Album]:
    """Fold directories that hold parts of ONE MusicBrainz release into one album.

    An album is the files that name its release, wherever they sit (#197). The
    directory is where they happen to live; the `MusicBrainz Album Id` in the
    tags is what the album IS, and the scan has already read it for every file.

    Two real layouts this exists for, both from a dogfooded library and both
    refused by the directory-based rule it replaces:

    * `Hybrid/Wide Angle` (disc 1) + `Hybrid/Live Angle_ Sydney` (disc 2), with
      two OTHER Hybrid albums alongside them;
    * eleven folders of Autechre EPs that are one compilation, under an
      `Autechre/` directory holding unrelated albums too.

    In both cases the old rule refused because the parent had "leftovers" — which
    were simply other albums, which is what an artist directory contains.

    **No containment.** Files claiming release X are that album wherever they
    are, because a boundary would rule out `Hybrid/Wide Angle` +
    `Live Albums/Hybrid/Live Angle`, which is a perfectly reasonable way to
    organise a library, to prevent merges that are correct anyway.

    **Duplicates do not merge**, and that is what makes dropping the boundary
    safe: two copies of one release share release-track ids, so they are not
    disjoint — see `_holds_different_tracks`. A backup copy, a second rip, the
    same album in a FLAC tree and an ALAC tree all stay separate.

    Untagged directories have no identity and are returned untouched.
    """
    by_release: dict[str, list[ScannedDir]] = {}
    singles: list[Album] = []
    for entry in scanned:
        sc = entry.album.sidecar
        mbid = sc.mb_release_id if sc else None
        if mbid is None:
            singles.append(entry.album)  # no identity to group on
        else:
            by_release.setdefault(mbid, []).append(entry)

    merged: list[Album] = []
    for mbid, parts in by_release.items():
        if len(parts) == 1:
            merged.append(parts[0].album)
            continue
        groups = _disjoint_groups(parts)
        if len(groups) == len(parts):
            # Nothing could be merged — every part overlaps every other, i.e.
            # they are duplicates of each other, not pieces of one album.
            merged.extend(p.album for p in parts)
            continue
        for group in groups:
            merged.append(group[0].album if len(group) == 1 else _combine(mbid, group))
    return singles + merged


def _disjoint_groups(parts: list[ScannedDir]) -> list[list[ScannedDir]]:
    """Partition parts naming one release into groups that can merge.

    Greedy and order-independent in the case that matters: a part joins the
    first group none of whose members it overlaps. Two copies of a release
    therefore end up in different groups, and three folders that are its three
    discs end up in one.
    """
    groups: list[list[ScannedDir]] = []
    for part in sorted(parts, key=lambda p: p.album.path):
        for group in groups:
            if all(_holds_different_tracks(part, other) for other in group):
                group.append(part)
                break
        else:
            groups.append([part])
    return groups


def _holds_different_tracks(a: ScannedDir, b: ScannedDir) -> bool:
    """True when two directories hold different tracks of the same release.

    Release-track ids when the files carry them: every track of a release has
    its own, so different discs have DISJOINT sets and two copies of one disc
    have identical ones. This reads the thing in question rather than a proxy
    for it — it establishes not that the parts *claim* to be different but that
    they *are*.

    Falls back to distinct disc numbers when neither side carries track ids (a
    pre-2011 rip). A MIXTURE is refused: with ids on one side and not the other
    there is nothing to compare, and answering with the weaker evidence a
    question the better evidence could settle is how a duplicate gets merged.
    """
    ids_a, ids_b = _release_track_ids(a), _release_track_ids(b)
    if ids_a and ids_b:
        return not (ids_a & ids_b)
    if ids_a or ids_b:
        return False
    discs_a, discs_b = _disc_numbers(a), _disc_numbers(b)
    if not discs_a or not discs_b:
        return False  # no evidence either way, so no merge
    return not (discs_a & discs_b)


def _release_track_ids(entry: ScannedDir) -> frozenset[str]:
    """Every file's release-track id, or empty if ANY file lacks one.

    All-or-nothing: a partial set makes disjointness meaningless, since two
    copies of a disc with half their files untagged would compare as disjoint
    on the tagged half.
    """
    ids = {f.release_track_id for f in entry.io.fields}
    if not ids or None in ids:
        return frozenset()
    return frozenset(i for i in ids if i is not None)


def _disc_numbers(entry: ScannedDir) -> frozenset[int]:
    """The disc numbers this directory's files carry, or empty if any lacks one."""
    discs = {f.disc_num for f in entry.io.fields}
    if not discs or None in discs:
        return frozenset()
    return frozenset(d for d in discs if d is not None)


def _combine(mbid: str, group: list[ScannedDir]) -> Album:
    """Rebuild one album from the directories that hold its parts.

    Rebuilt rather than patched: state, the expected track count and the
    partial-tag count are all derived from the WHOLE album's tags, and a merged
    album whose state was copied from one of its parts would report that part's
    answer — a two-disc release assembled from two "incomplete" halves has to
    come out complete.

    The primary directory (the one that becomes `Album.path`) is whichever holds
    the most tracks, ties broken by path so the choice is stable across scans.
    Something has to answer "where is this album" in one line; `paths` carries
    the truth and the album page lists all of them (#198).
    """
    ordered = sorted(group, key=lambda e: (-len(e.files), e.album.path))
    primary = ordered[0]
    files = sorted(
        (f for e in group for f in e.files),
        key=lambda f: (str(f.parent), album_files.sort_key(Path(f.name))),
    )
    io = AlbumIO(
        sidecar=_merge_sidecars([e.album.sidecar for e in group], mbid),
        fields=[f for e in ordered for f in e.io.fields],
        cover_path=next((e.io.cover_path for e in ordered if e.io.cover_path), None),
        video_fields=tuple(f for e in ordered for f in e.io.video_fields),
    )
    written = [e.album.files_written_at for e in group if e.album.files_written_at]
    return build_album(primary.album.path, files, io, max(written) if written else None)


def _merge_sidecars(sidecars: list[Sidecar | None], mbid: str) -> Sidecar:
    """One sidecar describing the whole album, from its parts'.

    Every part keeps its own file on disk — none is a shard, none is primary, and
    a folder moved out of the group is still a complete album on its own (#197).
    This is only the album's VIEW of them.

    The rules follow from each field being a fact about the album rather than
    about a folder: it has existed since the first of its parts did, it was last
    tagged when the last of them was, and a decision recorded on any part (a
    surrender, an accepted incompleteness) was made about the album.

    **Every field is named, and that is enforced** (#263). A merge needs a rule
    per field — first / earliest / latest / any — so unlike the rest of the
    codebase this cannot be a `replace()` off some arbitrary part. The price is
    that a field added to the model and forgotten here would be reset to its
    default on every multi-folder album, silently, which is exactly what happened
    to `video_media` and the two surrender flags. `test_sidecar_carries` holds
    this call total so the next field cannot repeat it.
    """
    present = [s for s in sidecars if s is not None]
    return Sidecar(
        store_url=next((s.store_url for s in present if s.store_url), None),
        bandcamp=next((s.bandcamp for s in present if s.bandcamp), None),
        downloaded_at=min((s.downloaded_at for s in present if s.downloaded_at), default=None),
        added_at=min((s.added_at for s in present if s.added_at), default=None),
        mb_release_id=mbid,
        # Dropped deliberately, and named so rather than omitted: exactly one of
        # `(mb_release_id, temp_uid)` is non-null on a persisted sidecar (§4),
        # and the line above just set the MBID — a merged album is grouped BY its
        # release, so it always has one and can never still be wearing a temp id.
        temp_uid=None,
        # A suggestion recorded against any part is a suggestion about the album,
        # by the same reasoning as the surrenders below. It cannot change the
        # merged album's STATE — `mb_release_id` is set from the tags just above,
        # so this view never derives NEEDS_MBID — it only lets the card render
        # the suggestion instead of pretending no one made it.
        mb_match_candidate=next(
            (s.mb_match_candidate for s in present if s.mb_match_candidate), None
        ),
        tagged_at=max((s.tagged_at for s in present if s.tagged_at), default=None),
        notes=next((s.notes for s in present if s.notes), None),
        purchase_unavailable=any(s.purchase_unavailable for s in present),
        tracks_unavailable=any(s.tracks_unavailable for s in present),
        # "Not asked" is None and "asked, none are video" is `()`, so the first
        # part that ASKED wins — `any`/`or` would collapse those two into each
        # other and re-ask MusicBrainz forever on a release with no video (#206).
        video_media=next((s.video_media for s in present if s.video_media is not None), None),
    )


def prune_cache(album_cache: AlbumCache, seen: set[Path]) -> None:
    """Drop cache entries for album dirs not present in `seen` (removed dirs)."""
    for stale in [p for p in album_cache if p not in seen]:
        del album_cache[stale]


class AlbumIO(NamedTuple):
    """Everything for one album that requires blocking filesystem I/O —
    sidecar JSON, each track's tags, and the cover lookup. Produced by
    `read_album_io` and consumed by `build_album` — both safe in a worker
    thread, neither touching shared mutable state."""

    sidecar: Sidecar | None
    fields: list[formats.ScanFields]
    cover_path: Path | None
    # Read from the album's video files, which Harmonist never tags. Kept apart
    # from `fields` so nothing that writes can reach them, and consulted only by
    # the completeness derivation (#193).
    # A tuple, not a list: `AlbumIO` is a NamedTuple, so a mutable default
    # would be shared by every instance that omits it.
    video_fields: tuple[formats.ScanFields, ...] = ()


def read_album_io(
    album_dir: Path,
    audio_files: list[Path],
    video_files: list[Path] | None = None,
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
        video_fields=tuple(formats.read_video_scan_fields(f) for f in (video_files or [])),
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


def build_album(
    album_dir: Path,
    audio_files: list[Path],
    io: AlbumIO,
    files_written_at: datetime | None = None,
) -> Album:
    """Assemble the Album from pre-read I/O — CPU only, no file I/O.

    Thread-agnostic: it touches no shared mutable state (an album's id is a hash
    of its path since #114), so the background scan can build on its worker
    thread alongside the reads, rather than hopping back to the event loop."""
    sidecar = io.sidecar
    fields = io.fields
    title = (fields[0].album_title if fields else None) or album_dir.name
    artist = _display_artist(fields)

    # Inconsistency trumps sidecar-driven state — see design §15.2.
    # The sidecar is kept on disk; once the user fixes the on-disk tags
    # via Picard, the next scan re-derives state from the sidecar.
    inconsistent_tracks = _check_consistency(audio_files, fields)
    expected = expected_tracks(fields, io.video_fields, sidecar.video_media if sidecar else None)
    state = (
        AlbumState.INCONSISTENT
        if inconsistent_tracks
        else _derive_state(sidecar, fields, album_dir, io.video_fields)
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
        audio_quality=_audio_quality(audio_files, fields),
        # A cover exists if there's a folder cover.* OR the first track has
        # embedded art (album art is on every track; first is representative).
        has_cover=io.cover_path is not None or (bool(fields) and fields[0].has_cover),
        # Reconcilable iff some track carries an MB Album Id atom (matches what
        # reconcile.reconcile_album reads). Lets the inbox skip kicking
        # reconcile for untagged orphans it could never resolve.
        has_tag_mbid=any(sf.album_id for sf in fields),
        expected_track_count=expected.total,
        absent_media=expected.absent_media,
        disc_total=_consistent(f.disc_total for f in fields if not f.unreadable),
        paths=tuple(sorted({f.parent for f in audio_files})) or (album_dir,),
        files_written_at=files_written_at,
    )


def _consistent(values: Iterable[int | None]) -> int | None:
    """The single value every file agrees on, or None when they disagree or any
    lacks it. Files that disagree about the album's own shape are not evidence
    of anything, so they get no answer rather than a majority one."""
    seen = set(values)
    return next(iter(seen)) if len(seen) == 1 and None not in seen else None


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
    # Medium positions with no files of any kind on disk. Knowable from the tags
    # alone (`disc_total` says how many there are, the discs present say which),
    # and the input to #206: whether an absent medium MATTERS depends on whether
    # it was video, which only MusicBrainz can say.
    absent_media: frozenset[int] = frozenset()


# What an album says when its files carry no counts at all. Complete, because
# "we have no idea" must not read as "tracks are missing" — that is the
# pre-#195 behaviour for an untagged album and it stays.
UNKNOWN_EXPECTED = ExpectedTracks(total=None, complete=True)


def expected_tracks(
    fields: list[formats.ScanFields],
    video_fields: Sequence[formats.ScanFields] = (),
    video_media: tuple[int, ...] | None = None,
) -> ExpectedTracks:
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

    `video_fields` are counted as present tracks (#193). Harmonist cannot tag a
    video file, but the user HAS it, and a release whose second medium is a DVD
    otherwise reads as missing every track on it — *Barking* is nine CD tracks
    and nine DVD ones, all on disk, reported as "missing 9 of 18". They are
    tracks for the purpose of "do you have this album", and for no other purpose.
    """
    present = [f for f in [*fields, *video_fields] if not f.unreadable]
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
    absent = frozenset(range(1, expected_media + 1)) - set(totals)
    if absent:
        # A whole medium has no files of any kind. Whether that matters depends
        # on what was ON it, and nothing on disk can say — the files that would
        # have carried its length are precisely the missing ones (#206).
        if video_media is not None and absent <= set(video_media):
            # Every absent medium was video-only. Harmonist cannot tag video
            # (#66), so a CD the user ripped in full is not "missing 44 tracks"
            # because they declined the bonus DVD. The album is complete on the
            # media it has, and its page lists the ones it hasn't.
            pass
        else:
            # An absent medium that is not known to be video — or not asked
            # about yet. The total is unknowable either way: the absent disc's
            # length was only ever recorded in the files that are missing.
            return ExpectedTracks(total=None, complete=False, absent_media=absent)
    if any(t is None for t in totals.values()):
        return UNKNOWN_EXPECTED  # a disc we can't size — don't guess the album's total

    total = sum(t for t in totals.values() if t is not None)
    return ExpectedTracks(total=total, complete=len(present) >= total, absent_media=absent)


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


def _audio_quality(audio_files: list[Path], fields: list[formats.ScanFields]) -> str | None:
    """What the album's audio actually IS, beside the codec label (#130).

    "ALAC" doesn't say whether a download is the quality that was paid for, or
    whether two copies of an album are the same files; "44.1 kHz · 16 bit"
    does.

    Rolled up the way the tag comparison rolls up a field, and for the same
    reason: an album that is half 16-bit and half 24-bit is worth knowing
    about. `consensus` gives what most tracks say plus a count of the ones that
    don't, so the row reads "44.1 kHz · 16 bit · 2 tracks differ" rather than
    collapsing to "Mixed" and throwing away the answer.

    The codec is deliberately NOT part of the value compared here. A folder
    holding one ALAC and one FLAC track, both 44.1/16, is a mixed *codec* — the
    label beside this already says so — but its audio is uniform, and reporting
    two disagreements for one fact would overstate it.
    """
    labels = [(p.name, sf.quality.label) for p, sf in zip(audio_files, fields, strict=True)]
    agreed = compare.consensus(labels)
    if agreed.value is None:
        # No file reports anything — every one unreadable, or a format whose
        # container records none of this.
        return None
    if agreed.is_unanimous:
        return agreed.value
    return f"{agreed.value} · {agreed.odd_summary}"


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
    sidecar: Sidecar | None,
    fields: list[formats.ScanFields],
    album_dir: Path,
    video_fields: Sequence[formats.ScanFields] = (),
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
        if not expected_tracks(fields, video_fields, sidecar.video_media).complete:
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
