"""Zip an album up and take it off disk, so it can be downloaded again (#132).

Re-downloading an album — to upgrade MP3s to FLAC, or to pick up tracks the
artist added to the release after the purchase — means letting the sync fetch a
purchase it already has. Three separate mechanisms stop that, and all three key
off the album still being on disk: `library_index.item_ids()` is unioned into
bandcampsync's ignore set at sync start, `sync_item` short-circuits on a
`store_url` it finds in that index, and `LocalMedia.is_locally_downloaded` reads
the `bandcamp_item_id.txt` the download left in the directory. So the directory
has to go. There is no version of this that keeps the files where they are.

Deleting a user's music outright is not something Harmonist does, so the files
are zipped into the music root first, under a name that says what they are and
when they were put there. The user can unzip it back, or delete it once the
replacement looks right; nothing in Harmonist expects the archive to still be
there, and nothing prunes it either. It is theirs.

**The zip is verified before anything is deleted.** `archive_and_remove` writes
the archive, reopens it, CRC-checks every member and compares the manifest
against the files that went in — and only then removes a single byte. A failure
anywhere in that sequence leaves the album exactly where it was. This ordering is
the whole safety argument for the feature, which is why the archive and the
delete are one function rather than two a caller sequences.

**Stored, not deflated.** FLAC, ALAC and MP3 are already compressed; deflating a
500 MB album buys a fraction of a percent and costs minutes of CPU on the NAS
this runs on. The sidecar and the cover are small enough not to argue about.
"""

from __future__ import annotations

import logging
import shutil
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from . import audit

log = logging.getLogger(__name__)

#: Headroom demanded on top of the album's own size before archiving starts.
#: The zip and the album coexist until the delete, so the operation transiently
#: needs both — and a NAS that fills up mid-write leaves a truncated archive
#: beside an album we then must not delete. Cheaper to refuse.
_FREE_SPACE_MARGIN = 64 * 1024 * 1024

#: Filesystem limit on a single name is 255 bytes nearly everywhere; leave room
#: for the " (archived YYYY-MM-DD) (12).zip" tail this appends.
_MAX_STEM = 180

#: Characters that can't go in a filename on the platforms Harmonist runs on
#: (Linux under Docker, macOS, and Windows via a SMB share onto either).
_UNSAFE = '/\\:*?"<>|\0'


class ArchiveError(Exception):
    """Archiving failed, and the album is still on disk. Always raised before
    anything is deleted — a caller that sees this has lost nothing."""


class InsufficientSpaceError(ArchiveError):
    """Not enough free space to hold the archive alongside the album."""


@dataclass(frozen=True)
class ArchiveResult:
    """What `archive_and_remove` wrote and what it then took away."""

    path: Path
    file_count: int
    total_bytes: int
    removed: tuple[Path, ...]


def archive_and_remove(
    dirs: Sequence[Path],
    *,
    music_root: Path,
    label: str,
    album_id: str | None = None,
    now: datetime | None = None,
) -> ArchiveResult:
    """Zip every directory in `dirs`, verify the zip, then delete them.

    `dirs` is an album's directories — usually one, but a release split across
    per-disc folders (§13.5) has several and they belong in one archive, because
    they are one album and get restored together.

    Members are stored under their path relative to `music_root`, so unzipping at
    the music root puts everything back exactly where it was. Every directory
    must therefore lie under `music_root`; one that doesn't is a bug in the
    caller, not a user error, and raises.

    Raises `ArchiveError` (or its `InsufficientSpaceError` subclass) with the
    album untouched. Once this returns, the directories are gone and the archive
    has been checked.
    """
    if not dirs:
        raise ArchiveError("no directories to archive")

    members = _collect(dirs, music_root)
    if not members:
        raise ArchiveError(f"nothing to archive under {', '.join(str(d) for d in dirs)}")

    total = sum(size for _, _, size in members)
    _check_space(music_root, total)

    zip_path = _unique_path(music_root, label, now or datetime.now(UTC))
    # Recorded BEFORE the write, per event-recording rule 6: if the process dies
    # mid-archive there is a line saying what it was attempting, rather than
    # silence next to a half-written zip.
    audit.record(
        "redownload.archive",
        album_id=album_id,
        archive=zip_path,
        files=len(members),
        bytes=total,
        dirs=len(dirs),
    )
    try:
        _write(zip_path, members)
        _verify(zip_path, members)
    except (OSError, zipfile.BadZipFile, ArchiveError) as e:
        # The album is still on disk and stays that way. Clean up the partial
        # archive so a later attempt doesn't collide with a broken file, and so
        # the user isn't left a zip that would restore an incomplete album.
        zip_path.unlink(missing_ok=True)
        log.exception("archiving %s to %s failed — the album has NOT been removed", label, zip_path)
        raise ArchiveError(f"could not write {zip_path.name}: {e}") from e

    removed = _remove(dirs, music_root, album_id=album_id)
    return ArchiveResult(path=zip_path, file_count=len(members), total_bytes=total, removed=removed)


def _collect(dirs: Sequence[Path], music_root: Path) -> list[tuple[Path, str, int]]:
    """`(source, arcname, size)` for every file under `dirs`, deepest last.

    Everything is taken — audio, cover art, the `.harmonist.json` sidecar and
    bandcampsync's `bandcamp_item_id.txt`. An archive missing the bookkeeping
    would restore as a NEW album needing re-reconciliation, which is not what
    "you can recover this" should mean.
    """
    out: list[tuple[Path, str, int]] = []
    seen: set[str] = set()
    for d in dirs:
        try:
            rel_root = d.resolve().relative_to(music_root.resolve())
        except ValueError as e:
            raise ArchiveError(f"{d} is not inside the music folder {music_root}") from e
        if not d.is_dir():
            raise ArchiveError(f"{d} is not a directory")
        for path in sorted(d.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue  # a symlink's target may live outside the library
            arcname = str(rel_root / path.relative_to(d))
            if arcname in seen:
                continue  # nested album dirs listed twice by the caller
            seen.add(arcname)
            out.append((path, arcname, path.stat().st_size))
    return out


def _check_space(music_root: Path, total: int) -> None:
    try:
        free = shutil.disk_usage(music_root).free
    except OSError:
        # Can't tell. Don't refuse on that basis — the write below reports a real
        # ENOSPC perfectly well, and refusing here would block the feature on a
        # filesystem statvfs doesn't answer for. Loud, because a NAS that can't
        # answer this is worth knowing about.
        log.exception("could not read free space at %s — archiving anyway", music_root)
        return
    needed = total + _FREE_SPACE_MARGIN
    if free < needed:
        raise InsufficientSpaceError(
            f"{_mb(needed)} MB needed to archive this album, {_mb(free)} MB free"
        )


def _write(zip_path: Path, members: list[tuple[Path, str, int]]) -> None:
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as zf:
        for source, arcname, _ in members:
            zf.write(source, arcname)


def _verify(zip_path: Path, members: list[tuple[Path, str, int]]) -> None:
    """Re-open the archive and prove it holds what went into it.

    Three checks, because they fail differently: `testzip` catches corrupted
    bytes, the manifest comparison catches a member that never got written, and
    the size comparison catches one truncated to a valid-looking short entry.
    Only the first is what `zipfile` would tell you on its own.
    """
    expected = {arcname: size for _, arcname, size in members}
    with zipfile.ZipFile(zip_path) as zf:
        bad = zf.testzip()
        if bad is not None:
            raise ArchiveError(f"failed its CRC check on {bad}")
        got = {info.filename: info.file_size for info in zf.infolist()}
    if missing := sorted(set(expected) - set(got)):
        raise ArchiveError(f"{len(missing)} file(s) missing from the archive, e.g. {missing[0]}")
    if short := sorted(name for name, size in expected.items() if got[name] != size):
        raise ArchiveError(f"{len(short)} file(s) stored at the wrong size, e.g. {short[0]}")


def _remove(dirs: Sequence[Path], music_root: Path, *, album_id: str | None) -> tuple[Path, ...]:
    """Delete each archived directory, then any parent it just emptied.

    Pruning empty parents is not the directory reshuffling §1 forbids — nothing
    is renamed or moved, and the only directories touched are ones this call
    emptied itself. Left behind, an emptied `Artist/` shows up in Plex as an
    artist with no music; bandcampsync recreates the whole path on the way back
    in. A parent holding anything at all — another album, a stray `.DS_Store` —
    is left alone.
    """
    removed: list[Path] = []
    for d in dirs:
        try:
            shutil.rmtree(d)
        except OSError:
            # Partial removal is the one genuinely bad outcome here: the archive
            # is good, so nothing is lost, but the album is now half on disk and
            # will scan as a mangled remnant. Say so and stop — carrying on to
            # the next directory would widen the mess.
            log.exception("could not remove %s after archiving it", d)
            audit.record("redownload.delete_failed", album_id=album_id, dir=d)
            raise ArchiveError(
                f"the archive was written, but {d.name} could not be removed — "
                "the album is now partly deleted; check the folder by hand"
            ) from None
        audit.record("redownload.delete", album_id=album_id, dir=d)
        removed.append(d)
    for parent in _prunable_parents(dirs, music_root):
        try:
            parent.rmdir()
        except OSError:
            # Raced, or not actually empty. Nothing is lost by leaving it, so
            # this is genuinely moot — the album is already archived and gone.
            log.info("left %s in place — not empty", parent)
        else:
            audit.record("redownload.prune", album_id=album_id, dir=parent)
    return tuple(removed)


def _prunable_parents(dirs: Sequence[Path], music_root: Path) -> list[Path]:
    """Parents of `dirs` that are now empty, deepest first and never the root.

    Built by re-joining the relative parts onto `music_root` AS GIVEN rather than
    by walking the resolved path, so the paths handed to `audit.record` are the
    same shape as everything else in the log. `audit._rel` relativises against
    the configured music dir literally, and on macOS that is `/var/folders/…`
    while `resolve()` returns `/private/var/folders/…` — a symlink apart, enough
    for the relativisation to give up and print an absolute path nobody wants to
    read (#98).
    """
    root = music_root.resolve()
    candidates: list[Path] = []
    for d in dirs:
        try:
            rel = d.resolve().relative_to(root)
        except ValueError:
            continue  # not under the root; _collect already refused it
        for parent_rel in rel.parents:
            if parent_rel == Path("."):
                break  # the music root itself is never pruned
            candidates.append(music_root / parent_rel)
    # Deepest first, so emptying `Artist/Album/Disc 1` lets `Artist/Album` go
    # before `Artist` is considered.
    return sorted(set(candidates), key=lambda p: len(p.parts), reverse=True)


def _unique_path(music_root: Path, label: str, when: datetime) -> Path:
    """`<music_root>/<label> (archived YYYY-MM-DD).zip`, never an existing file.

    Re-downloading the same album twice — or two albums that clean down to the
    same name — must not overwrite the earlier archive, which may be the only
    copy of those files left. Suffix instead.
    """
    stem = f"{_clean(label)} (archived {when.strftime('%Y-%m-%d')})"
    candidate = music_root / f"{stem}.zip"
    n = 2
    while candidate.exists():
        candidate = music_root / f"{stem} ({n}).zip"
        n += 1
    return candidate


def _clean(label: str) -> str:
    """`label` reduced to something every filesystem here will accept.

    A leading dot would hide the archive — the opposite of the point — and a
    trailing dot or space is rejected outright over SMB from Windows.
    """
    out = "".join(" " if c in _UNSAFE else c for c in label)
    out = " ".join(out.split())[:_MAX_STEM].strip(". ")
    return out or "album"


def _mb(n: int) -> int:
    return n // (1024 * 1024)
