"""Picard-compatible tagger — orchestration layer.

Builds a format-agnostic `TagSet` per track from an MB release dict and
delegates the actual atom/frame/comment serialisation to the matching
`harmonist.formats.<format>` submodule.

For backward compatibility with existing tests, the MP4 atom-name
constants are re-exported here.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from . import activity_store, artwork_store, audit, formats, tag_history
from . import sidecar as sidecar_mod
from .formats import TagSet, owned
from .formats.m4a import (  # noqa: F401 — back-compat re-exports
    ATOM_ALBUM,
    ATOM_ALBUM_ARTIST,
    ATOM_ALBUM_ARTIST_SORT,
    ATOM_ARTIST,
    ATOM_ARTIST_SORT,
    ATOM_ARTISTS,
    ATOM_ASIN,
    ATOM_BARCODE,
    ATOM_CATALOG,
    ATOM_COMMENT,
    ATOM_COVER,
    ATOM_DATE,
    ATOM_DISC_NUM,
    ATOM_GENRE,
    ATOM_ISRC,
    ATOM_LABEL,
    ATOM_MB_ALBUM_ARTIST_ID,
    ATOM_MB_ALBUM_COUNTRY,
    ATOM_MB_ALBUM_ID,
    ATOM_MB_ALBUM_STATUS,
    ATOM_MB_ALBUM_TYPE,
    ATOM_MB_ARTIST_ID,
    ATOM_MB_RELEASE_GROUP_ID,
    ATOM_MB_RELEASE_TRACK_ID,
    ATOM_MB_TRACK_ID,
    ATOM_MEDIA,
    ATOM_ORIGINAL_DATE,
    ATOM_ORIGINAL_YEAR,
    ATOM_PREFIX,
    ATOM_SCRIPT,
    ATOM_TITLE,
    ATOM_TRACK_NUM,
    LEGACY_RELEASE_ID,
)
from .models import Release, Track

log = logging.getLogger(__name__)

# One flattened MB track: (medium, track_pos_in_medium, track).
_FlatTrack = tuple[dict[str, Any], int, Track]


class TagMismatchError(Exception):
    """Raised when the file count doesn't match the MB release's track count."""


@runtime_checkable
class Tagger(Protocol):
    """Contract for a Harmonist tagger.

    Implementations write tags to every audio file in `album_dir` based on
    the supplied MB release dict, optionally embedding cover art from
    `cover_path`. Returns the number of files tagged. Raises
    `TagMismatchError` when the file count and MB track count diverge
    (unless `incomplete=True`).
    """

    def tag_album(
        self,
        album_dir: Path,
        release: Release,
        cover_path: Path | None = None,
        *,
        incomplete: bool = False,
        overwrite_art: bool = False,
    ) -> int: ...


class PicardCompatibleTagger:
    """Default tagger — builds Picard-compatible tags and writes them to
    every supported audio file in the album dir."""

    def tag_album(
        self,
        album_dir: Path,
        release: Release,
        cover_path: Path | None = None,
        *,
        incomplete: bool = False,
        overwrite_art: bool = False,
    ) -> int:
        return tag_album(
            album_dir, release, cover_path, incomplete=incomplete, overwrite_art=overwrite_art
        )


def tag_album(
    album_dir: Path,
    release: Release,
    cover_path: Path | None = None,
    *,
    incomplete: bool = False,
    overwrite_art: bool = False,
) -> int:
    """Tag every supported audio file in `album_dir`.

    `release` is the unwrapped MusicBrainz release dict, i.e. what
    `musicbrainzngs.get_release_by_id()` returns under the "release" key.
    Returns the number of files tagged.

    `incomplete=True` allows file_count < track_count and assigns files
    to a subset of MB tracks via length-similarity (positional fallback).
    file_count > track_count is still an error in both modes (per design
    §15.3 — "extra files on disk" is out of scope).

    `overwrite_art=True` embeds the album cover even when the tracks carry
    differing per-track artwork (which is otherwise preserved) — the user's
    explicit "replace the artwork" override.
    """
    files = sorted(p for p in album_dir.iterdir() if formats.is_supported(p))
    flat_tracks = list(_flatten_tracks(release))

    if not incomplete and len(files) != len(flat_tracks):
        raise TagMismatchError(
            f"album {album_dir.name!r}: {len(files)} audio files but MB release "
            f"has {len(flat_tracks)} tracks"
        )
    if len(files) > len(flat_tracks):
        raise TagMismatchError(
            f"album {album_dir.name!r}: {len(files)} files exceeds MB release "
            f"track count {len(flat_tracks)} — extra files on disk are out of "
            f"scope (see design §15.3)"
        )

    if incomplete and len(files) < len(flat_tracks):
        pairs = _assign_files_to_tracks(files, flat_tracks)
    else:
        # Counts are guaranteed equal here by the checks above.
        pairs = list(zip(files, flat_tracks, strict=True))

    cover = cover_path.read_bytes() if cover_path else None
    # Only when there IS a cover to embed. With `cover=None` write_tags leaves
    # the existing art alone, so nothing changes and there is nothing to record
    # — reading every file's cover would be a wasted pass over the album, on the
    # path where scanning is already the slow part (#44, #74). Guarding here
    # rather than relying on the `and` below, which used to short-circuit this
    # read and stopped doing so when the digests were hoisted out.
    art_before = _art_digests(files) if cover is not None else {}
    # DATA SAFETY: if the tracks carry DIFFERENT embedded art (a per-track-art
    # album, e.g. a compilation), embedding one album cover would destroy those
    # images. Preserve them — pass cover=None (write_tags leaves the existing
    # embedded cover untouched); the folder cover.* is still written separately.
    if cover is not None and not overwrite_art and _has_per_track_art(art_before):
        log.warning(
            "%s: tracks have per-track embedded artwork — keeping it, NOT embedding "
            "the album cover (folder cover.* is still written). Re-tag with "
            "'replace artwork' to override.",
            album_dir.name,
        )
        cover = None
    media_total = len(release.get("medium-list", [])) or 1

    # Tag writing replaces information in every audio file, so it belongs in the
    # audit log — it was the one core mutation with no record at all. The album
    # line is written BEFORE the loop so a crash part-way leaves evidence of what
    # was attempted, not silence.
    album_id = sidecar_mod.album_id_for(album_dir)
    audit.record(
        "tag.album",
        album_id=album_id,
        album=album_dir,
        release=release.get("id"),
        tracks=len(pairs),
        art="embedded" if cover is not None else "preserved",
        mode="incomplete" if incomplete else "full",
    )
    art_after = _digest(cover) if cover is not None else None
    if art_after is not None:
        # Only now is it settled that the embed is really happening — the
        # per-track-art guard above may have cancelled it.
        _keep_doomed_art(art_before, art_after)
    for file_path, (medium, track_pos_in_medium, track) in pairs:
        tagset = _build_tagset(release, medium, track_pos_in_medium, track, media_total)
        # The write hands back what was there before, read from the handle it
        # already had open — so the per-field record (#86) costs no second pass.
        before = formats.write_tags(file_path, tagset, cover)
        # The `tag.track` line comes AFTER the write, and the detail hangs off
        # it: a record claiming a change that never landed would make a future
        # revert restore a value that was never overwritten.
        event_id = audit.record(
            "tag.track",
            album_id=album_id,
            file=file_path.name,
            track=track_pos_in_medium,
            title=_track_title(track),
        )
        if event_id is not None:
            _record_changes(event_id, file_path, tagset, before, art_before, art_after)

    return len(files)


def _record_changes(
    event_id: int,
    file_path: Path,
    tagset: TagSet,
    before: dict[str, Any],
    art_before: dict[Path, str | None],
    art_after: str | None,
) -> None:
    """Attach this file's per-field before/after to its `tag.track` audit line.

    Writes nothing when nothing changed. A re-tag that finds MusicBrainz
    unchanged is a no-op the user should not have to scroll past, and the
    gardener (#32) will run one nightly per album — so silence is the feature,
    not an omission. The `tag.album` line above still records that it ran.
    """
    changes = owned.diff(before, {f.value: getattr(tagset, f.value) for f in owned.Owned})

    # Artwork rides alongside the owned fields but is not one of them: the
    # tagger, not `write_tags`, decides whether art is replaced or preserved,
    # and `cover=None` means "leave it alone" rather than "remove it".
    was = art_before.get(file_path)
    if art_after is not None and art_after != was:
        changes[owned.ARTWORK] = [was, art_after]

    if not changes:
        return
    activity_store.record_tag_changes(
        event_id,
        file=file_path.name,
        changes=changes,
        track_ref=tagset.mb_release_track_id,
        rec_ref=tagset.mb_track_id,
        position=(
            f"{tagset.disc_num}-{tagset.track_num}"
            if tagset.disc_total > 1
            else str(tagset.track_num)
        ),
    )


def _art_digests(files: list[Path]) -> dict[Path, str | None]:
    """Each file's embedded cover as a sha256, or None where it has none.

    One read per file, at tag time, when the files are being opened anyway. The
    same pass answers two questions that used to need separate machinery: whether
    the album carries per-track artwork worth preserving, and what each track's
    art WAS, so a tagging can record that it replaced it (#86).

    sha256 rather than the sha1 this used before #86: the digest is recorded, and
    #131 stores the images content-addressed under it, so the two must agree.
    """
    return {f: _digest(art[0]) if (art := formats.read_cover(f)) else None for f in files}


def _keep_doomed_art(digests: dict[Path, str | None], incoming: str) -> None:
    """Copy every image this tagging is about to overwrite into the artwork
    store, so replacing it can be undone (#131).

    Runs AFTER the per-track-art decision, not during the digest pass, and that
    ordering is the point: `_has_per_track_art` can still cancel the embed, and
    an image that survives is not being destroyed and has no business being
    backed up. So this re-reads the doomed files — but only the doomed ones, and
    only when something really is about to be lost. An album whose art already
    matches the incoming cover, or has none, reads nothing at all.

    Deduplicated by digest inside the store, so tracks sharing one cover cost
    one file rather than one each.
    """
    seen: set[str] = set()
    for path, key in digests.items():
        if key is None or key == incoming or key in seen:
            continue
        seen.add(key)
        art = formats.read_cover(path)
        if art is None:  # vanished between the two passes; nothing to keep
            continue
        # Best-effort: a copy that can't be written is a reason to warn, not to
        # abandon the re-tag the user asked for. `keep` logs and returns None.
        artwork_store.keep(art[0], mime=art[1])


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _has_per_track_art(digests: dict[Path, str | None]) -> bool:
    """True when the album's tracks carry DIFFERENT embedded cover images — i.e.
    per-track artwork worth preserving. A compilation's per-track images are user
    data a re-tag must not destroy."""
    return len({d for d in digests.values() if d is not None}) > 1


class RevertUnavailableError(Exception):
    """An undo can't be carried out as recorded — a file the tagging wrote is
    gone, or can't be read. Raised before anything is written, so the album is
    never left half-reverted: a state that was never real, with neither half
    undoable, is worse than a refusal that explains itself."""


@dataclass(frozen=True)
class RevertOutcome:
    """What an undo actually did, in the terms the user asked the question in.

    Not just a count. A revert that skipped half its fields because a later
    re-tag moved them is a *different* outcome from one that put everything
    back, and reporting both as "12 files" would hide the part the user most
    needs to know.
    """

    files: int
    #: Field names put back, and field names left alone because the file no
    #: longer carried what this tagging wrote. Unioned across files and sorted,
    #: since the message names fields rather than file-field pairs.
    restored: tuple[str, ...] = ()
    stale: tuple[str, ...] = ()
    #: Set when the revert moved `mb_album_id`, with what the files carry now —
    #: None meaning the id was removed altogether. The caller needs both facts
    #: to keep the sidecar in step: a sidecar still naming a release the files
    #: no longer carry derives as TAGGING, which is a spinner with no way out
    #: (#158). `None` for `release_id_now` is a real answer, so the boolean has
    #: to be separate from it.
    release_id_reverted: bool = False
    release_id_now: str | None = None

    @property
    def changed(self) -> bool:
        return bool(self.files)


@dataclass(frozen=True)
class _IdentityRevert:
    """The album's `mb_album_id` revert, decided once for the whole album."""

    value: str | None


def _identity_revert(
    album_dir: Path, plan: Sequence[tag_history.FileRevert]
) -> _IdentityRevert | None:
    """What `mb_album_id` should become, or None to leave it alone entirely.

    Answered for the album rather than per file, because it is the album's
    identity: the sidecar records exactly one release, so a revert that moved it
    on some files and not others would leave nothing coherent to write there —
    and would derive as INCONSISTENT, a state the user then has to repair by
    hand in Picard.

    So it is all-or-nothing, and every one of these has to hold:

    * every file in the plan agrees on what the id was before the tagging;
    * every one of them still carries what the tagging wrote, i.e. nothing has
      re-tagged or re-matched the album since;
    * every file is readable, and there is no file in the album that the plan
      doesn't cover — a tagging that touched half the album can't speak for the
      identity of the other half.
    """
    field = owned.Owned.MB_ALBUM_ID
    # Keyed by file name, never by position: the plan's order and the
    # directory's are both file order today, but pairing two lists that merely
    # happen to agree is how the wrong track's id gets written.
    changes = {item.file: item.fields[field] for item in plan if field in item.fields}
    if not changes or len(changes) != len(plan):
        return None
    befores = {before for before, _after in changes.values()}
    if len(befores) != 1:
        return None

    on_disk = sorted(p for p in album_dir.iterdir() if formats.is_supported(p))
    if {p.name for p in on_disk} != set(changes):
        # Files have appeared or gone since. Any the plan doesn't name would
        # keep whatever id they carry, so moving the rest would split the
        # album's identity between two releases.
        return None
    for path in on_disk:
        try:
            current = formats.read_owned(path)
        except Exception as e:
            raise RevertUnavailableError(f"could not read the tags on {path.name}: {e}") from e
        if owned.values_differ(current.get(field), changes[path.name][1]):
            return None

    before = next(iter(befores))
    return _IdentityRevert(value=before if isinstance(before, str) and before else None)


def revert_tags(album_dir: Path, plan: Sequence[tag_history.FileRevert]) -> RevertOutcome:
    """Put back the tags one tagging changed, and report what actually moved.

    `plan` comes from `tag_history.revert_plan` — the same records the History
    page described, so the button undoes what the user just read.

    **Per-field, not per-file.** A field is put back only when the file still
    carries the value this tagging wrote. Anything changed since — a later
    re-tag, an edit in Picard — is left alone and reported, because an undo that
    reached past the change it names into someone else's would be the confident
    lie the artwork restore was careful to avoid.

    **`mb_album_id` is all-or-nothing**, unlike every other field. It is the
    album's identity, and the sidecar has to follow it (#158) — so it must come
    out of here single-valued. Reverting it on the files whose value still
    matches while leaving the rest would leave the album's tracks disagreeing
    about which release they belong to, which derives as INCONSISTENT and hands
    the caller no id to write down. So it moves on every file or on none, and
    the outcome reports what the album now carries.

    **Everything resolves before anything is written**, like `restore_artwork`:
    every file is opened and read first, and a missing or unreadable one raises
    rather than leaving the album half-reverted.

    Writes its own per-file records, so the undo appears in History with its own
    field list and is itself undoable.
    """
    targets: dict[Path, tuple[dict[str, Any], dict[str, Any]]] = {}
    restored: set[str] = set()
    stale: set[str] = set()
    identity = _identity_revert(album_dir, plan)

    for item in plan:
        # `item.file` reaches here from a stored record and is joined to a path.
        # Records are permanent and unversioned, so a malformed one will turn up
        # eventually; a bare filename is the only thing ever written here, and
        # anything else must not become a write outside the album.
        if item.file != Path(item.file).name or item.file in ("", ".", ".."):
            raise RevertUnavailableError(f"{item.file!r} is not a file name in this album")
        path = album_dir / item.file
        if not path.exists():
            raise RevertUnavailableError(f"{item.file} is no longer in this album")
        try:
            current = formats.read_owned(path)
        except Exception as e:
            raise RevertUnavailableError(f"could not read the tags on {item.file}: {e}") from e

        target = dict(current)
        for field, (before, after) in item.fields.items():
            if field == owned.Owned.MB_ALBUM_ID:
                # Decided once for the album, above — not per file.
                if identity is not None:
                    target[field] = identity.value
                    restored.add(field)
                else:
                    stale.add(field)
                continue
            if field not in current:
                # A field this build no longer owns. The records are permanent
                # and unversioned, so one written by a future build may name it;
                # writing it back would put a tag under a key nothing reads.
                continue
            if owned.values_differ(current[field], after):
                stale.add(field)
                continue
            if not owned.values_differ(current[field], before):
                continue  # already back where it started
            target[field] = before
            restored.add(field)
        if target != current:
            targets[path] = (target, current)

    files = 0
    album_id = sidecar_mod.album_id_for(album_dir)
    if targets:
        # Written BEFORE the loop, like `tag.album`, so a crash part-way leaves
        # evidence of what was attempted rather than only of what completed.
        audit.record(
            "tag.revert",
            album_id=album_id,
            album=album_dir,
            files=len(targets),
            fields=len(restored),
            stale=len(stale),
            release_id=(identity.value or "removed") if identity is not None else "-",
        )
    for path, (target, _current) in targets.items():
        before = formats.write_owned(path, target)
        # The per-file line comes AFTER its write and the detail hangs off it,
        # as in `tag_album`: a record claiming a change that never landed would
        # make a future revert restore a value that was never overwritten.
        event_id = audit.record(
            "tag.revert.track",
            album_id=album_id,
            album=album_dir,
            file=path.name,
        )
        if event_id is not None:
            changes = owned.diff(before, target)
            if changes:
                activity_store.record_tag_changes(event_id, file=path.name, changes=changes)
        files += 1

    return RevertOutcome(
        files=files,
        restored=tuple(sorted(restored)),
        stale=tuple(sorted(stale)),
        # Reported only when files were actually written: an identity decided
        # but not carried out (every field already back) must not send the
        # caller off to rewrite a sidecar that is already correct.
        release_id_reverted=identity is not None and bool(files),
        release_id_now=identity.value if identity is not None else None,
    )


class ArtworkUnavailableError(Exception):
    """The image a restore needs is no longer in the store — evicted by the size
    cap, or never kept because the store was full or disabled. Raised rather
    than silently doing nothing, because "undo" that quietly succeeds without
    restoring anything is the confident lie the design forbids."""


def restore_artwork(album_dir: Path, digests: dict[str, str]) -> int:
    """Put back the artwork `digests` names, and return how many files changed.

    `digests` maps a file name to the sha256 of the image that file should carry
    — straight out of a tagging's `artwork` before-values (#86). Restoring by
    digest rather than "the album's old cover" is what makes a compilation's
    per-track art come back to the right tracks.

    Every image is checked to be present BEFORE anything is written: a partial
    restore would leave the album in a state that was never real, and neither
    half of it revertable. Files whose art already matches are skipped, so the
    operation is idempotent.
    """
    resolved: dict[Path, bytes] = {}
    for name, key in digests.items():
        # `name` comes out of a stored record and is joined to a path. Records
        # are permanent and unversioned, so a malformed one will turn up
        # eventually; a bare filename is the only thing that was ever written
        # here, and anything else must not become a write outside the album.
        if name != Path(name).name or name in ("", ".", ".."):
            raise ArtworkUnavailableError(f"{name!r} is not a file name in this album")
        path = album_dir / name
        if not path.exists():
            raise ArtworkUnavailableError(f"{name} is no longer in this album")
        stored = artwork_store.path_for(key)
        if stored is None:
            raise ArtworkUnavailableError(
                f"the image {name} used to carry is no longer kept "
                "(the artwork store evicted it, or never held it)"
            )
        try:
            resolved[path] = stored.read_bytes()
        except OSError as e:
            raise ArtworkUnavailableError(f"could not read the kept image for {name}: {e}") from e

    restored = 0
    for path, data in resolved.items():
        current = formats.read_cover(path)
        if current is not None and _digest(current[0]) == _digest(data):
            continue  # already correct — restoring twice is a no-op
        # Keep what we are about to overwrite, exactly as a tagging would: an
        # undo is itself a destructive write, and must be as undoable as the
        # thing it undoes.
        if current is not None:
            artwork_store.keep(current[0], mime=current[1])
        formats.write_cover(path, data)
        audit.record("artwork.restore", album=album_dir, file=path.name, digest=_digest(data))
        restored += 1
    return restored


def tagsets_for(release: Release) -> list[TagSet]:
    """Every track's TagSet for `release`, in track order — what tagging WOULD
    write, without writing it.

    Exists for the album comparison (#106), and deliberately routes through the
    same `_build_tagset` the tagger uses rather than re-deriving the fields.
    A second mapping would drift: read "label" from a different corner of the
    release than the writer does and the page reports a difference against tags
    Harmonist itself wrote, which is worse than showing nothing.

    That also gives the comparison its exact meaning — not "do my files match
    MusicBrainz" in the abstract, but "do my files match what Harmonist would
    write from this release", which is the question the user can act on.
    """
    media_total = len(release.get("medium-list", [])) or 1
    return [
        _build_tagset(release, medium, pos, track, media_total)
        for medium, pos, track in _flatten_tracks(release)
    ]


def _build_tagset(
    release: Release,
    medium: dict[str, Any],
    track_pos: int,
    track: Track,
    media_total: int,
) -> TagSet:
    """Translate one MB track within a release to a TagSet."""
    track_artist_credit = track.get("artist-credit") or release.get("artist-credit")
    label_info = release.get("label-info-list") or []
    first_label = label_info[0] if label_info else {}
    rg = release.get("release-group") or {}

    track_total = len(medium.get("track-list", []))

    disc_num = 1
    if "position" in medium:
        try:
            disc_num = int(medium["position"])
        except (TypeError, ValueError):
            disc_num = 1

    return TagSet(
        mb_album_id=release["id"],
        album=release.get("title", ""),
        album_artist=_artist_phrase(release.get("artist-credit")),
        title=_track_title(track),
        artist=_artist_phrase(track_artist_credit),
        track_num=track_pos + 1,
        track_total=track_total,
        album_artist_sort=_artist_sort_phrase(release.get("artist-credit")) or None,
        artist_sort=_artist_sort_phrase(track_artist_credit) or None,
        artists=_artist_names(track_artist_credit),
        original_date=rg.get("first-release-date") or None,
        script=(release.get("text-representation") or {}).get("script") or None,
        mb_album_artist_ids=_artist_ids(release.get("artist-credit")),
        mb_release_group_id=rg.get("id"),
        mb_album_type=rg.get("primary-type"),
        # Picard writes the status lower-cased (e.g. "official", not "Official").
        mb_album_status=(release.get("status") or "").lower() or None,
        mb_album_country=release.get("country"),
        mb_track_id=(track.get("recording") or {}).get("id"),
        mb_release_track_id=track.get("id"),
        mb_artist_ids=_artist_ids(track_artist_credit),
        isrcs=_isrcs(track),
        date=release.get("date") or None,
        disc_num=disc_num,
        disc_total=media_total,
        label=first_label.get("label", {}).get("name") if first_label else None,
        catalog_number=first_label.get("catalog-number") if first_label else None,
        barcode=release.get("barcode") or None,
        asin=release.get("asin") or None,
        media=medium.get("format") or None,
    )


def _assign_files_to_tracks(
    files: list[Path],
    flat_tracks: list[_FlatTrack],
) -> list[tuple[Path, _FlatTrack]]:
    """Best-fit assignment of files to a subset of MB tracks via length
    similarity, preserving input file order.

    Falls back to positional matching when any file or track length is
    unknown — the simpler choice is more predictable without enough data.
    """
    file_durations: list[int | None] = [formats.read_duration_ms(f) for f in files]

    track_lengths: list[int | None] = []
    for _medium, _pos, track in flat_tracks:
        # Per-release track length is authoritative; recording length can
        # differ by seconds across releases (see match._mb_track_length_ms).
        raw = track.get("length") or (track.get("recording") or {}).get("length")
        try:
            track_lengths.append(None if raw is None else int(raw))
        except (TypeError, ValueError):
            track_lengths.append(None)

    if any(t is None for t in track_lengths) or any(d is None for d in file_durations):
        # Positional fallback — first N tracks; the rest are "missing"
        # and get no file assigned.
        return [(files[i], flat_tracks[i]) for i in range(len(files))]

    used: set[int] = set()
    pairs: list[tuple[Path, _FlatTrack]] = []
    for f, dur in zip(files, file_durations, strict=True):
        # The guard above guarantees every duration/length is set here.
        assert dur is not None
        best_idx = None
        best_delta: int | None = None
        for i, tlen in enumerate(track_lengths):
            if i in used or tlen is None:
                continue
            delta = abs(dur - tlen)
            if best_delta is None or delta < best_delta:
                best_idx = i
                best_delta = delta
        assert best_idx is not None
        used.add(best_idx)
        pairs.append((f, flat_tracks[best_idx]))
    return pairs


def _flatten_tracks(release: Release) -> Iterator[_FlatTrack]:
    """Yield (medium, track_pos_in_medium, track) for every track in every medium."""
    for medium in release.get("medium-list", []):
        for i, track in enumerate(medium.get("track-list", [])):
            yield medium, i, track


def _track_title(track: Track) -> str:
    """The track's title, preferring the per-release **track** title over the
    underlying recording title.

    This matches Picard: `track_to_metadata` seeds the title from the recording
    and then overrides it with the track title when present. The track title is
    what appears on *this* release — e.g. after applying MusicBrainz's featured-
    artist style, the editor moves the guest out of the track title into the
    artist credit, while the recording title often keeps its original form.
    Reading the recording title instead would silently re-tag with the stale
    name (see issue #27)."""
    if title := track.get("title"):
        return str(title)
    return str((track.get("recording") or {}).get("title", ""))


def _isrcs(track: Track) -> list[str]:
    """The ISRC code(s) of the track's recording (MB returns `isrc-list` when
    the release is fetched with the `isrcs` include)."""
    recording = track.get("recording") or {}
    return [str(code) for code in (recording.get("isrc-list") or [])]


def _artist_ids(artist_credit: list[Any] | None) -> list[str]:
    """Pull MBIDs out of an MB artist-credit list."""
    if not artist_credit:
        return []
    ids: list[str] = []
    for ac in artist_credit:
        if isinstance(ac, dict):
            artist = ac.get("artist") or {}
            if artist_id := artist.get("id"):
                ids.append(artist_id)
    return ids


def _artist_phrase(artist_credit: list[Any] | None) -> str:
    """Build a display string from an MB artist-credit list."""
    if not artist_credit:
        return ""
    parts: list[str] = []
    for ac in artist_credit:
        if isinstance(ac, str):
            parts.append(ac)
        elif isinstance(ac, dict):
            name = ac.get("name") or ac.get("artist", {}).get("name", "")
            parts.append(name)
            if jp := ac.get("joinphrase"):
                parts.append(jp)
    return "".join(parts).strip()


def _artist_sort_phrase(artist_credit: list[Any] | None) -> str:
    """Like `_artist_phrase` but using each artist's MB **sort-name** (e.g.
    'Beatles, The'), keeping join phrases. Empty when no sort-names are present."""
    if not artist_credit:
        return ""
    parts: list[str] = []
    any_sort = False
    for ac in artist_credit:
        if isinstance(ac, dict):
            sort = (ac.get("artist") or {}).get("sort-name")
            if sort:
                any_sort = True
            parts.append(sort or ac.get("name") or (ac.get("artist") or {}).get("name", ""))
            if jp := ac.get("joinphrase"):
                parts.append(jp)
    return "".join(parts).strip() if any_sort else ""


def _artist_names(artist_credit: list[Any] | None) -> list[str]:
    """The individual artist display names (no join phrases) — Picard's
    multi-value `artists` / ARTISTS tag."""
    if not artist_credit:
        return []
    names: list[str] = []
    for ac in artist_credit:
        if isinstance(ac, dict):
            name = ac.get("name") or (ac.get("artist") or {}).get("name", "")
            if name:
                names.append(name)
    return names
