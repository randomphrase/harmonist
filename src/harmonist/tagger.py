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
from pathlib import Path, PurePosixPath
from typing import Any, Protocol, runtime_checkable

from . import (
    activity_store,
    album_files,
    artwork_store,
    audit,
    compare,
    formats,
    mb_lookup,
    tag_history,
)
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
from .models import Release, Track, norm_title

log = logging.getLogger(__name__)

# One flattened MB track: (medium, track_pos_in_medium, track).
_FlatTrack = tuple[dict[str, Any], int, Track]


class TagMismatchError(Exception):
    """Raised when the file count doesn't match the MB release's track count.

    Carries the two counts, and `short` says which way round they are: True when
    the release lists MORE tracks than the album has files. That is the one
    direction a caller can resolve — by re-running in incomplete mode, which is
    the user's decision to make (#252) — while the other direction (extra files
    on disk) is out of scope for the tagger in both modes (design §15.3).

    The counts are attributes rather than only prose in the message because the
    web layer has to tell the two apart and name the numbers back to the user;
    re-parsing the sentence for them would be a second, silently divergent copy
    of what happened.
    """

    def __init__(self, message: str, *, files: int, tracks: int) -> None:
        super().__init__(message)
        self.files = files
        self.tracks = tracks

    @property
    def short(self) -> bool:
        """The album has fewer files than the release has tracks."""
        return self.files < self.tracks


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
        files: list[Path] | None = None,
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
        files: list[Path] | None = None,
    ) -> int:
        return tag_album(
            album_dir,
            release,
            cover_path,
            incomplete=incomplete,
            overwrite_art=overwrite_art,
            files=files,
        )


def tag_album(
    album_dir: Path,
    release: Release,
    cover_path: Path | None = None,
    *,
    incomplete: bool = False,
    overwrite_art: bool = False,
    files: list[Path] | None = None,
) -> int:
    """Tag every supported audio file in `album_dir`.

    `files` overrides which files those are, and a caller holding an `Album`
    should pass them: since #197 an album can span several directories, and
    `album_dir` is only its primary one — tagging what is under that alone would
    silently leave the rest of the album on its old tags.

    `release` is the unwrapped MusicBrainz release dict, i.e. what
    `musicbrainzngs.get_release_by_id()` returns under the "release" key.
    Returns the number of files tagged.

    `incomplete=True` allows file_count < track_count. file_count >
    track_count is still an error in both modes (per design §15.3 — "extra
    files on disk" is out of scope).

    Which file is which track is decided by `compare.assign` in **both** modes
    (#235). It used to be positional in the complete mode and by the ladder in
    the incomplete one, which meant a release could fit the files perfectly and
    still be unpairable: an album whose only absent media are video is COMPLETE
    (#206), so it took the positional path and `zip(..., strict=True)` raised on
    a count that included tracks Harmonist will never write. The ladder's last
    rung IS positional pairing, so a single-medium album is assigned exactly as
    before.

    `overwrite_art=True` embeds the album cover even when the tracks carry
    differing per-track artwork (which is otherwise preserved) — the user's
    explicit "replace the artwork" override.
    """
    prep = _prepare(
        album_dir,
        release,
        cover_path,
        incomplete=incomplete,
        overwrite_art=overwrite_art,
        files=files,
    )
    # Read before the artwork guard as well as before the loop: the guard's
    # warning names the album it is about, and this is where that name comes
    # from. Tagging can still move the id afterwards (temp_uid -> MBID), which is
    # why `album_history` unions an album's alias chain — the same reason the
    # `tag.album` line below gets away with the pre-write id.
    album_id = sidecar_mod.album_id_for(album_dir)
    if prep.preserves_per_track_art:
        # Attributed to the album (#260). This is a decision Harmonist made on
        # the user's behalf about their files, so it has to reach that album's
        # own History — and the feed's log mirror drops any record that doesn't
        # say which album it means. The `art=preserved` token on the `tag.album`
        # line below is not a substitute: it shows only under "Show details".
        #
        # The album's name is NOT repeated into the message; it rides in its own
        # column, which is where the feed and the History both render it.
        log.warning(
            "tracks have per-track embedded artwork — keeping it, NOT embedding "
            "the album cover (folder cover.* is still written). Re-tag with "
            "'replace artwork' to override.",
            extra={"album_id": album_id, "album_label": _album_label(release, album_dir)},
        )

    # Tag writing replaces information in every audio file, so it belongs in the
    # audit log — it was the one core mutation with no record at all. The album
    # line is written BEFORE the loop so a crash part-way leaves evidence of what
    # was attempted, not silence. It is recorded even when the loop turns out to
    # write nothing: "the pass ran and found the files already correct" is a
    # different fact from "the pass never ran", and only this line carries it.
    audit.record(
        "tag.album",
        album_id=album_id,
        album=album_dir,
        release=release.get("id"),
        tracks=len(prep.pairs),
        art="embedded" if prep.cover is not None else "preserved",
        mode="incomplete" if incomplete else "full",
    )
    if prep.art_after is not None:
        _keep_doomed_art(prep.art_before, prep.art_after)

    for file_path, (medium, track_pos_in_medium, track) in prep.pairs:
        tagset = _build_tagset(release, medium, track_pos_in_medium, track, prep.media_total)
        before = formats.read_owned(file_path)
        changes = _changes_for(tagset, before, file_path, prep.art_before, prep.art_after)
        if not changes and not formats.has_superseded_tags(file_path):
            # Nothing to write, so nothing is written. The file keeps its mtime
            # — which `reconcile.looks_externally_retagged` compares against
            # `tagged_at` — and no `tag.track` line claims a change that didn't
            # happen. `_record_changes` has always taken this position for the
            # per-field detail; the write and its record now take it too, which
            # is what makes the gardener's nightly pass (#32) a real no-op
            # rather than one that merely records nothing.
            continue
        formats.write_tags(file_path, tagset, prep.cover)
        # The `tag.track` line comes AFTER the write, and the detail hangs off
        # it: a record claiming a change that never landed would make a future
        # revert restore a value that was never overwritten.
        event_id = audit.record(
            "tag.track",
            album_id=album_id,
            file=album_files.rel_name(album_dir, file_path),
            # +1 because `_flatten_tracks` enumerates from zero and MusicBrainz,
            # the files and the album page all count from one (#240). A record
            # off by one is worse than none: it is exactly what someone auditing
            # a re-tag would read as the tagger having assigned the wrong track.
            track=track_pos_in_medium + 1,
            title=_track_title(track),
        )
        if event_id is not None:
            _record_changes(event_id, album_dir, file_path, tagset, changes)

    return len(prep.files)


def plan_album(
    album_dir: Path,
    release: Release,
    cover_path: Path | None = None,
    *,
    incomplete: bool = False,
    overwrite_art: bool = False,
    files: list[Path] | None = None,
) -> AlbumPlan:
    """What `tag_album` would change here, computed without writing anything.

    Same arguments, same guards, same assignment of files to tracks — and the
    same `owned.diff` over the same values, so the plan and the audit record a
    real tagging writes cannot disagree about what "changed" means. That is the
    whole reason this exists rather than the album page's `compare.*` engine,
    which is display-shaped: its per-track vocabulary is Title and Artist, it
    shows fields it never compares (#164), and a gardener classifying off it
    would judge on things a re-tag cannot write while missing most of what one
    would (#32).

    Raises `TagMismatchError` exactly where tagging would, so a caller learns
    the release no longer fits the files without having to attempt the write.
    For #32 that IS the finding: a changed track count is a structural change,
    which is a question for a human rather than something to auto-apply.

    Costs one read per file — two on an album with a cover to embed, which is
    what `tag_album` costs on that path as well.
    """
    prep = _prepare(
        album_dir,
        release,
        cover_path,
        incomplete=incomplete,
        overwrite_art=overwrite_art,
        files=files,
    )
    changes: dict[Path, dict[str, list[Any]]] = {}
    for file_path, (medium, track_pos_in_medium, track) in prep.pairs:
        tagset = _build_tagset(release, medium, track_pos_in_medium, track, prep.media_total)
        if file_changes := _changes_for(
            tagset, formats.read_owned(file_path), file_path, prep.art_before, prep.art_after
        ):
            changes[file_path] = file_changes
    return AlbumPlan(changes=changes, preserves_per_track_art=prep.preserves_per_track_art)


@dataclass(frozen=True)
class AlbumPlan:
    """What a re-tag of one album would change, per file and per field.

    `changes` holds only the files that would change, and within each only the
    fields that would — `{path: {field: [before, after]}}`, the shape
    `activity_store.record_tag_changes` persists and `tag_history.label_for`
    renders. Detector, classifier, activity entry and undo therefore all speak
    one vocabulary, which is `owned.Owned` plus `owned.ARTWORK`.

    An empty `changes` is the interesting case: it means a tagging would write
    nothing at all. It does NOT mean a tagging would touch nothing on disk —
    see `formats.has_superseded_tags` for the tags a write cleans up that no
    owned-field diff can see.
    """

    changes: dict[Path, dict[str, list[Any]]]
    #: True when the album's tracks carry differing embedded art, so a tagging
    #: would keep it and NOT embed the album cover. Not a change — it is the
    #: absence of one — but the caller needs it to explain why the artwork the
    #: user expected didn't move (#260), and #272 needs it to stop announcing
    #: that decision on a pass that wrote nothing.
    preserves_per_track_art: bool

    @property
    def empty(self) -> bool:
        """True when a re-tag would write no owned field on any file."""
        return not self.changes


def significance_of(field: str, before: Any, after: Any) -> owned.Significance:
    """What kind of change this one entry of a tagging diff is (#267).

    `field` is a key from an `AlbumPlan`'s changes — an `owned.Owned` value or
    `owned.ARTWORK` — and `before`/`after` are that entry's two values. The
    field-level classification comes from `owned.SIGNIFICANCE`; this adds the one
    rule that cannot be given at field level.

    It says what the change IS, not what to do about it. Whether it needs a
    person is `owned.needs_review`, which is policy over this answer rather than
    part of it — today every level goes to review regardless, and #273 makes
    that a setting.

    **Identity is settled upstream of significance.** The diff handed to this
    must have been computed against the release the album is *now known to be*,
    not the one it was last recorded as. MusicBrainz redirects a merged MBID, so
    a merge arrives as a changed `mb_album_id` — and #268 settled that a merge
    always applies and is never held for review, there being nothing to
    authorise once MusicBrainz has already done it. That correction belongs at
    the fetch, where the redirect names both ids and the merge is provable. By
    the time a diff reaches here, `mb_album_id` moving means the album is being
    re-pointed at a genuinely different release, which is exactly the case its
    REVIEW verdict is for. See `docs/design.md` §5.

    Raises `KeyError` for a key that is neither owned nor artwork, rather than
    guessing. A plan cannot produce one, and inventing a default here is how a
    field would quietly acquire a significance nobody gave it — which, once
    #273 lets a level be trusted, is how it would acquire permission to write
    itself.
    """
    declared = owned.SIGNIFICANCE[field]
    if field in owned.BY_VALUE and _only_cosmetically_different(before, after):
        return owned.Significance.COSMETIC
    return declared


def _only_cosmetically_different(before: Any, after: Any) -> bool:
    """Whether two values of a BY_VALUE field differ only in spacing or casing.

    `models.norm_title` is the definition, borrowed rather than restated: it is
    what `TrackComparison.title_differs` uses to decide whether the album page
    shows a title as differing at all, and the two must agree. A title the page
    reports as unchanged is not one the gardener may treat as a retitle.

    Anything that isn't a pair of strings is not cosmetic — a field arriving or
    disappearing is a real change, and the safe direction here is to fall
    through to the higher significance the map already gave.
    """
    if not isinstance(before, str) or not isinstance(after, str):
        return False
    return norm_title(before) == norm_title(after)


@dataclass(frozen=True)
class _Prepared:
    """Everything a tagging has settled before it touches a file.

    Shared by `tag_album` and `plan_album` so the two cannot diverge on which
    files pair with which tracks, which count guard fires, or whether the album
    cover is going to be embedded. Splitting here rather than at the write —
    "plan, then apply the plan" — is deliberate: `write_tags` hands back the
    before-state from the handle it already has open, so a literal plan-then-
    apply would read every file twice on the path that is already the slow one.
    """

    files: list[Path]
    pairs: list[tuple[Path, _FlatTrack]]
    #: The cover to embed, or None to leave each file's own art alone.
    cover: bytes | None
    art_before: dict[Path, str | None]
    art_after: str | None
    preserves_per_track_art: bool
    media_total: int


def _prepare(
    album_dir: Path,
    release: Release,
    cover_path: Path | None,
    *,
    incomplete: bool,
    overwrite_art: bool,
    files: list[Path] | None,
) -> _Prepared:
    """Decide what a tagging of this album would consist of, reading no tags.

    (It does read embedded artwork, when there is a cover that might replace
    it — that is the only way to know whether replacing it would destroy
    per-track images.)
    """
    files = files if files is not None else album_files.audio_files(album_dir)
    flat_tracks = list(_flatten_tracks(release))

    # What the count guard is entitled to expect on disk: the release's tracks
    # minus the ones on media Harmonist cannot tag (#235). Counting a bonus
    # DVD's 53 videos against 16 audio files made a complete, correctly tagged
    # album permanently un-re-taggable — and it is the albums MusicBrainz has
    # since corrected that most need the button.
    taggable = _taggable_tracks(release, flat_tracks)

    if not incomplete and len(files) != len(taggable):
        raise TagMismatchError(
            f"album {album_dir.name!r}: {len(files)} audio files but MB release "
            f"has {len(taggable)} tracks",
            files=len(files),
            tracks=len(taggable),
        )
    if len(files) > len(flat_tracks):
        raise TagMismatchError(
            f"album {album_dir.name!r}: {len(files)} files exceeds MB release "
            f"track count {len(flat_tracks)} — extra files on disk are out of "
            f"scope (see design §15.3)",
            files=len(files),
            tracks=len(flat_tracks),
        )

    # Assigned against EVERY track, not just the taggable ones: a file that
    # names a video track's id is a file in the wrong place, and quietly
    # re-pointing it at an audio track would be the invention this ladder
    # exists to avoid.
    pairs = _assign_files_to_tracks(files, flat_tracks)

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
    #
    # Decided here and REPORTED rather than announced: `plan_album` must reach
    # the same verdict without logging anything, so the warning belongs to
    # `tag_album`, which is the one that acts on it.
    preserves_per_track_art = (
        cover is not None and not overwrite_art and _has_per_track_art(art_before)
    )
    if preserves_per_track_art:
        cover = None

    return _Prepared(
        files=files,
        pairs=pairs,
        cover=cover,
        art_before=art_before,
        # Only now is it settled that the embed is really happening — the
        # per-track-art guard above may have cancelled it.
        art_after=_digest(cover) if cover is not None else None,
        preserves_per_track_art=preserves_per_track_art,
        media_total=len(release.get("medium-list", [])) or 1,
    )


def _changes_for(
    tagset: TagSet,
    before: dict[str, Any],
    file_path: Path,
    art_before: dict[Path, str | None],
    art_after: str | None,
) -> dict[str, list[Any]]:
    """What tagging this one file to `tagset` would change, as `{field: [was, now]}`.

    The single answer to that question, so a dry run (`plan_album`) and the
    audit record a real tagging writes are computed by the same code over the
    same values. When they were two expressions they were free to disagree, and
    a detector that disagreed with the record would classify changes the
    history then said never happened.

    Only fields that actually changed appear — see `owned.diff`.
    """
    changes = owned.diff(before, {f.value: getattr(tagset, f.value) for f in owned.Owned})

    # Artwork rides alongside the owned fields but is not one of them: the
    # tagger, not `write_tags`, decides whether art is replaced or preserved,
    # and `cover=None` means "leave it alone" rather than "remove it".
    was = art_before.get(file_path)
    if art_after is not None and art_after != was:
        changes[owned.ARTWORK] = [was, art_after]
    return changes


def _record_changes(
    event_id: int,
    album_dir: Path,
    file_path: Path,
    tagset: TagSet,
    changes: dict[str, list[Any]],
) -> None:
    """Attach this file's per-field before/after to its `tag.track` audit line.

    `changes` comes from `_changes_for`, computed before the write decided
    whether to happen at all. Writes nothing when nothing changed — which since
    #266 the caller has already acted on by skipping the file entirely, so this
    guard is now the belt to that braces: a `tag.track` line with an empty
    detail would say a file was tagged and decline to say to what.
    """
    if not changes:
        return
    activity_store.record_tag_changes(
        event_id,
        file=album_files.rel_name(album_dir, file_path),
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

    on_disk = {album_files.rel_name(album_dir, p): p for p in album_files.audio_files(album_dir)}
    if set(on_disk) != set(changes):
        # Files have appeared or gone since. Any the plan doesn't name would
        # keep whatever id they carry, so moving the rest would split the
        # album's identity between two releases.
        return None
    for name, path in on_disk.items():
        try:
            current = formats.read_owned(path)
        except Exception as e:
            raise RevertUnavailableError(f"could not read the tags on {name}: {e}") from e
        if owned.values_differ(current.get(field), changes[name][1]):
            return None

    before = next(iter(befores))
    return _IdentityRevert(value=before if isinstance(before, str) and before else None)


def _resolve_in_album(album_dir: Path, name: str, error: type[Exception]) -> Path:
    """Join a stored record's file name onto the album directory, refusing
    anything that could name a file outside it.

    Raises the caller's own "this undo can't be done" exception rather than a
    bare ValueError, so a malformed record surfaces to the user as a declined
    undo instead of a 500.

    Since #16 the name may carry a disc directory ("CD2/01 - Intro.m4a"), so a
    bare-filename test no longer works — but relaxing the SHAPE must not relax
    the guarantee. Every component is checked, and the joined path is confirmed
    to still be inside the album afterwards, which is what actually holds on a
    case-insensitive or symlinked filesystem.
    """
    rel = PurePosixPath(name)
    if not name or rel.is_absolute() or any(part in ("", ".", "..") for part in rel.parts):
        raise error(f"{name!r} is not a file name in this album")
    path = album_dir / rel
    # The component check above rejects the obvious escapes; this rejects the
    # ones a filesystem invents — a symlinked disc directory pointing out of the
    # album. `resolve()` follows links, so it is the real target being tested.
    if not path.resolve().is_relative_to(album_dir.resolve()):
        raise error(f"{name!r} is not a file name in this album")
    return path


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
        # eventually, and this join must not become a write outside the album.
        path = _resolve_in_album(album_dir, item.file, RevertUnavailableError)
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
            file=album_files.rel_name(album_dir, path),
        )
        if event_id is not None:
            changes = owned.diff(before, target)
            if changes:
                activity_store.record_tag_changes(
                    event_id, file=album_files.rel_name(album_dir, path), changes=changes
                )
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
        # eventually, and this join must not become a write outside the album.
        path = _resolve_in_album(album_dir, name, ArtworkUnavailableError)
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
        audit.record(
            "artwork.restore",
            album=album_dir,
            file=album_files.rel_name(album_dir, path),
            digest=_digest(data),
        )
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
    disc_num = _disc_num(medium)

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
        # The medium's own name, from the same dict `media` comes from. Picard
        # writes this as `discsubtitle`, so a Picard-tagged library already
        # carries it — and until #218 a Harmonist re-tag silently removed it.
        disc_subtitle=medium.get("title") or None,
    )


def _assign_files_to_tracks(
    files: list[Path],
    flat_tracks: list[_FlatTrack],
) -> list[tuple[Path, _FlatTrack]]:
    """Which MusicBrainz track each file is, for an album missing some of them.

    Through `compare.assign` — the same ladder the album page uses, so a file
    cannot be one track in the tracklist and a different one in the tagger
    (#232). Release track id, then disc-and-track number, then file order.

    This used to assign by **length similarity**, which is why it is worth
    saying what changed: on *TISM — The White Albun* that rule bound
    `Sorted for D 'n M.m4a` to a DVD track called *Diatribe*, one file in
    sixteen, and would have written that track's title and ids into it. The
    file's own tags named its real slot the whole time and were never read.
    Fifteen right out of sixteen is the worst available outcome — nobody
    re-checks an album that looks mostly correct.

    Costs one open per file, as reading their durations did.
    """
    identities = [compare.identity_of(formats.read_tags(f)) for f in files]
    slots = compare.assign(identities, [_identity_of(t) for t in flat_tracks])
    # A file with no slot is not tagged at all. It can only happen with more
    # files than tracks, which the caller has already refused (§15.3) — but
    # leaving one alone beats writing another track's metadata into it.
    return [(f, flat_tracks[s]) for f, s in zip(files, slots, strict=True) if s is not None]


def _identity_of(flat: _FlatTrack) -> compare.TrackIdentity:
    """One MusicBrainz track's identity, exactly as tagging would write it —
    `_build_tagset` derives the same disc from the medium and the same track
    number from the position."""
    medium, track_pos, track = flat
    return compare.TrackIdentity(track.get("id"), _disc_num(medium), track_pos + 1)


def _taggable_tracks(release: Release, flat_tracks: list[_FlatTrack]) -> list[_FlatTrack]:
    """The release's tracks minus the ones on media Harmonist will never write.

    Read from the RELEASE (#237), not from the sidecar's `video_media` (#206),
    even though that field records the same fact. The sidecar's copy exists for
    the scanner, which has no MusicBrainz; here the release is already in hand
    and carries the per-track `video` flag, so asking the sidecar buys nothing
    and can be wrong: it is only written for albums that look like they are
    missing a medium (`reconcile.needs_video_media`), and an album whose tags
    predate the release gaining discs does not look like that. TISM's *The White
    Albun*, restored from a backup, says "disc 1 of 1, all present" — nothing
    absent, so nothing asked, so the guard counted 53 videos as missing audio
    and refused the re-tag for good.

    Per TRACK rather than per medium format, exactly as
    `mb_lookup.fetch_video_media` is: `Wish You Were Here 50` is one Blu-ray of
    45 audio tracks and 4 videos, and judging by format would expect nothing of
    it.
    """
    # `mb_lookup` for a PURE function: this makes no request, and the release it
    # reads was fetched by the caller.
    video = set(mb_lookup.video_media_of(release))
    if not video:
        return flat_tracks
    return [f for f in flat_tracks if _disc_num(f[0]) not in video]


def _disc_num(medium: dict[str, Any]) -> int:
    """Which disc this medium is. 1 when MusicBrainz doesn't say, or says
    something that isn't a number — a single-medium release often has no
    position at all."""
    if "position" not in medium:
        return 1
    try:
        return int(medium["position"])
    except (TypeError, ValueError):
        return 1


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


def _album_label(release: dict[str, Any], album_dir: Path) -> str:
    """The album's display name for an activity entry — "Artist — Title".

    Taken from the release being tagged rather than the sidecar, because that is
    what the files are about to say, and it is the same name the album will be
    listed under once this tagging lands. Falls back to the folder name when the
    release names neither, so an entry is never labelled with an empty string —
    the feed hides the album column entirely when the label is blank, which would
    lose the attribution this exists to add.
    """
    label = f"{_artist_phrase(release.get('artist-credit'))} — {release.get('title') or ''}"
    return label.strip(" —") or album_dir.name


def _artist_phrase(artist_credit: list[Any] | None) -> str:
    """Build a display string from an MB artist-credit list.

    musicbrainzngs emits each join phrase as a **bare string element** between the
    artist dicts (`[{...}, ' & ', {...}]`), not as a `joinphrase` key on the dict —
    the key is the JSON web service's shape, which Picard consumes but we never see.
    Every walker over an artist-credit must handle the string elements or it will
    silently concatenate the artists with no separator (#183)."""
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
        if isinstance(ac, str):
            parts.append(ac)
        elif isinstance(ac, dict):
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
