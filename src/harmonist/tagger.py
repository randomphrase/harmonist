"""Picard-compatible tagger — orchestration layer.

Builds a format-agnostic `TagSet` per track from an MB release dict and
delegates the actual atom/frame/comment serialisation to the matching
`harmonist.formats.<format>` submodule.

For backward compatibility with existing tests, the MP4 atom-name
constants are re-exported here.
"""

from __future__ import annotations

import dataclasses
import logging
import os
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any, NamedTuple, Protocol, runtime_checkable

from . import (
    activity_store,
    album_files,
    artwork,
    artwork_store,
    artwork_watch,
    audit,
    compare,
    cover_art,
    derived,
    formats,
    id_registry,
    images,
    mb_lookup,
    mb_tracks,
    progress,
    squish,
    tag_history,
    track_structure,
)
from . import credits as credits_mod
from . import sidecar as sidecar_mod
from . import transforms as transforms_mod
from .formats import TagSet, owned
from .formats.m4a import (  # noqa: F401 — back-compat re-exports
    ATOM_ALBUM,
    ATOM_ALBUM_ARTIST,
    ATOM_ALBUM_ARTIST_SORT,
    ATOM_ALBUM_ARTISTS,
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
from .transforms import NO_CHOICES, TaggingChoices

log = logging.getLogger(__name__)

# One flattened MB track: (medium, MusicBrainz position, track), including zero.
_FlatTrack = tuple[dict[str, Any], int, Track]


class TagMismatchError(Exception):
    """Raised when the file count doesn't match the MB release's track count.

    Carries the two counts, and `short` says which way round they are: True when
    the release lists MORE tracks than the album has files. That is the one
    direction a caller can resolve — by re-running in incomplete mode, which is
    the user's decision to make (#252) — while the other direction (extra files
    on disk) requires an explicit assignment review (#517).

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


class TrackAssignmentRequired(TagMismatchError):
    """A confirmed tracklist changed or contains unresolved track identities."""

    def __init__(self, *, files: int, tracks: int, unassigned: int) -> None:
        super().__init__(
            "Review assignments before applying MusicBrainz track metadata.",
            files=files,
            tracks=tracks,
        )
        self.unassigned = unassigned


def assignment_review(
    release: Release, tags: Sequence[formats.TrackTags]
) -> track_structure.Review:
    """The shared structural guard, using only the release and tags already read."""
    targets = tagsets_for(release, TaggingChoices())
    video = set(mb_lookup.video_media_of(release))
    eligible = {i for i, t in enumerate(targets) if t.disc_num not in video}
    return track_structure.review(tags, targets, eligible, release["id"])


@runtime_checkable
class Tagger(Protocol):
    """Contract for a Harmonist tagger — the seam the web layer holds.

    One operation, in two halves (#481): implementations write tags to every
    audio file in `album_dir` from the supplied MB release dict, and then write
    the artwork that album's plan calls for — gaps filled, a missing folder
    cover created, or the folder cover embedded outright under `overwrite_art`.
    `TaggingOutcome` reports the halves separately, because they are separately
    scoped and separately undoable.

    Whether the folder cover is among the things it may create is the user's
    `[tagging] folder_cover` policy (#516), handed in by the web layer — the
    tagger reads no config of its own.

    Raises `TagMismatchError` when the file count and MB track count diverge
    (unless `incomplete=True`), before anything is written.

    Three settings decide the artwork half, and they are three different
    questions (#482). `artwork_included=False` removes it entirely. `scope`
    selects how much of the decided plan is written — `Scope.ALL` applies what a
    page drew, replacements included. `overwrite_art` changes the plan itself,
    embedding the folder cover over everything, and is the only one that
    flattens a compilation's per-track sleeves.
    """

    def tag_and_artwork(
        self,
        album_dir: Path,
        release: Release,
        cover_path: Path | None = None,
        *,
        incomplete: bool = False,
        overwrite_art: bool = False,
        files: list[Path] | None = None,
        archive: cover_art.Front | None = None,
        expected_artwork: str | None = None,
        scope: artwork.Scope | None = None,
        artwork_included: bool = True,
        chosen: artwork.Source | None = None,
        choices: artwork.Choices | None = None,
        picks: Mapping[str, cover_art.Front] | None = None,
        assignment: dict[Path, int] | None = None,
        folder_cover: artwork.FolderCoverPolicy = artwork.FolderCoverPolicy.IF_MISSING,
        tagging: TaggingChoices = NO_CHOICES,
    ) -> TaggingOutcome: ...


class PicardCompatibleTagger:
    """Default tagger — builds Picard-compatible tags and writes them to
    every supported audio file in the album dir, then applies the artwork
    its plan calls for."""

    def tag_and_artwork(
        self,
        album_dir: Path,
        release: Release,
        cover_path: Path | None = None,
        *,
        incomplete: bool = False,
        overwrite_art: bool = False,
        files: list[Path] | None = None,
        archive: cover_art.Front | None = None,
        expected_artwork: str | None = None,
        scope: artwork.Scope | None = None,
        artwork_included: bool = True,
        chosen: artwork.Source | None = None,
        choices: artwork.Choices | None = None,
        picks: Mapping[str, cover_art.Front] | None = None,
        assignment: dict[Path, int] | None = None,
        folder_cover: artwork.FolderCoverPolicy = artwork.FolderCoverPolicy.IF_MISSING,
        tagging: TaggingChoices = NO_CHOICES,
    ) -> TaggingOutcome:
        return tag_and_artwork(
            album_dir,
            release,
            cover_path,
            incomplete=incomplete,
            overwrite_art=overwrite_art,
            files=files,
            archive=archive,
            expected_artwork=expected_artwork,
            scope=scope,
            artwork_included=artwork_included,
            chosen=chosen,
            choices=choices,
            picks=picks,
            assignment=assignment,
            folder_cover=folder_cover,
            tagging=tagging,
        )


def tag_album(
    album_dir: Path,
    release: Release,
    *,
    incomplete: bool = False,
    files: list[Path] | None = None,
    tagging: TaggingChoices = NO_CHOICES,
) -> int:
    """Write tags to every supported audio file in `album_dir`. TAGS ONLY.

    Artwork is not this function's business (#481). It used to fill gaps as it
    wrote — the image rode along in the same `write_tags` call — and while that
    was true the two halves of a tagging could not be scoped, reported or undone
    apart from each other. `tag_and_artwork` composes them instead, and is what
    every entry point calls; this is the half that writes tags.

    `files` overrides which files those are, and a caller holding an `Album`
    should pass them: since #197 an album can span several directories, and
    `album_dir` is only its primary one — tagging what is under that alone would
    silently leave the rest of the album on its old tags.

    `release` is the unwrapped MusicBrainz release dict, i.e. what
    `musicbrainzngs.get_release_by_id()` returns under the "release" key.
    Returns the number of files tagged.

    `incomplete=True` allows file_count < track_count. file_count >
    track_count is still an error in both modes: extra files on disk need an
    explicit assignment review (docs/design/matching.md).

    Which file is which track is decided by `compare.assign` in **both** modes
    (#235). It used to be positional in the complete mode and by the ladder in
    the incomplete one, which meant a release could fit the files perfectly and
    still be unpairable: an album whose only absent media are video is COMPLETE
    (#206), so it took the positional path and `zip(..., strict=True)` raised on
    a count that included tracks Harmonist will never write. The ladder's last
    rung IS positional pairing, so a single-medium album is assigned exactly as
    before.

    Returns the number of files tagged.
    """
    return _tag_files(album_dir, release, incomplete=incomplete, files=files, tagging=tagging)[0]


def _tag_files(
    album_dir: Path,
    release: Release,
    *,
    incomplete: bool = False,
    files: list[Path] | None = None,
    assignment: dict[Path, int] | None = None,
    tagging: TaggingChoices = NO_CHOICES,
) -> tuple[int, bool]:
    """`tag_album`, reporting both halves a composed tagging needs: how many
    files the release covers, and whether any file was actually WRITTEN.

    The second is #272's gate, and it cannot be inferred from the first. The
    preserved-artwork notice reports a decision that recurs identically on every
    re-tag rather than a change, so it rides on the write — and a re-tag that
    finds the files already correct must not say it again. `tag_album` returns
    the count alone because that is what the page reports ("Tagged N files"),
    and it counts the files the release covers whether or not any of them
    needed changing.
    """
    prep = _prepare(
        album_dir,
        release,
        None,
        incomplete=incomplete,
        overwrite_art=False,
        files=files,
        # No artwork decision here, and so no pass over the files to read their
        # images: this function writes none (#481, and #448 for why that is
        # stated rather than implied by passing no cover path).
        artwork_in_scope=False,
        assignment=assignment,
    )
    with progress.job("Tagging", len(prep.files), foreground=True) as job:
        return _write_prepared_tags(album_dir, release, prep, tagging, job, incomplete=incomplete)


def _write_prepared_tags(
    album_dir: Path,
    release: Release,
    prep: _Prepared,
    tagging: TaggingChoices,
    job: progress.Job,
    *,
    incomplete: bool,
) -> tuple[int, bool]:
    # Read before the loop, and before anything can move it: tagging drops a
    # sidecar's `temp_uid` for the MBID afterwards, which is why `album_history`
    # unions an album's alias chain — the same reason the `tag.album` line below
    # gets away with the pre-write id.
    album_id = sidecar_mod.album_id_for(album_dir)

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
        mode="incomplete" if incomplete else "full",
    )
    # How this album's records name its files — built once from the files the
    # tagging is actually writing, so a disc in a sibling directory is named by
    # its disc rather than by a bare filename its sibling also answers to (#423).
    naming = album_files.Naming(album_dir, prep.files)
    wrote_something = False
    for file_path, (medium, track_pos_in_medium, track) in job.items(
        prep.pairs, lambda pair: naming.name_of(pair[0])
    ):
        before = formats.read_owned(file_path)
        tagset = formats.settled(
            _build_tagset(release, medium, track_pos_in_medium, track, prep.media_total, tagging),
            before,
        )
        # `cover=None` throughout: this writes tags, and leaves every file's
        # image exactly as it is (#481). `_changes_for` records no artwork
        # change either, because none happens here.
        changes = _changes_for(
            tagset,
            before,
            file_path,
            prep.art.before,
            None,
        )
        if not changes and not formats.has_superseded_tags(file_path):
            # Nothing to write, so nothing is written. The file keeps its mtime
            # — which `reconcile.looks_externally_retagged` compares against
            # `tagged_at` — and no `tag.track` line claims a change that didn't
            # happen. `_record_changes` has always taken this position for the
            # per-field detail; the write and its record now take it too, which
            # is what makes the gardener's nightly pass (#32) a real no-op
            # rather than one that merely records nothing.
            continue
        formats.write_tags(file_path, tagset, None)
        wrote_something = True
        # The `tag.track` line comes AFTER the write, and the detail hangs off
        # it: a record claiming a change that never landed would make a future
        # revert restore a value that was never overwritten.
        event_id = audit.record(
            "tag.track",
            album_id=album_id,
            file=naming.name_of(file_path),
            track=track_pos_in_medium,
            title=_track_title(track),
        )
        if event_id is not None:
            _record_changes(event_id, naming, file_path, tagset, changes)

    for path in job.items(prep.unassigned, naming.name_of):
        carried = formats.read_owned(path)
        target = _unassigned_target(release, prep, carried, tagging)
        if not owned.diff(carried, target):
            continue
        before = formats.write_owned(path, target)
        wrote_something = True
        event_id = audit.record(
            "tag.track", album_id=album_id, file=naming.name_of(path), assignment="unassigned"
        )
        if event_id is not None:
            activity_store.record_tag_changes(
                event_id,
                file=naming.name_of(path),
                changes=owned.diff(before, target),
                position=_history_position(target),
            )

    return len(prep.files), wrote_something


def plan_album(
    album_dir: Path,
    release: Release,
    cover_path: Path | None = None,
    *,
    incomplete: bool = False,
    overwrite_art: bool = False,
    files: list[Path] | None = None,
    artwork: bool = True,
    assignment: dict[Path, int] | None = None,
    tagging: TaggingChoices = NO_CHOICES,
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
        artwork_in_scope=artwork,
        assignment=assignment,
    )
    changes: dict[Path, dict[str, list[Any]]] = {}
    supported: dict[Path, dict[str, frozenset[Any]]] = {}
    positions: dict[Path, str | None] = {}
    for file_path, (medium, track_pos_in_medium, track) in prep.pairs:
        now = _owned_now(file_path, prep.read)
        tagset = formats.settled(
            _build_tagset(release, medium, track_pos_in_medium, track, prep.media_total, tagging),
            now,
        )
        positions[file_path] = _tagset_position(tagset)
        supported[file_path] = _supported_spellings(
            release, medium, track_pos_in_medium, track, prep.media_total
        )
        if file_changes := _changes_for(
            tagset,
            now,
            file_path,
            prep.art.before,
            # Only where the tagging would really write it — the same targets
            # `tag_album` hands `write_tags`. Every file differing from the
            # winner used to be reported here, including the ones a tagging
            # leaves alone since #418.
            prep.art_after.get(file_path),
        ):
            changes[file_path] = file_changes
    # An unassigned file's album fields answer to the same settings a paired
    # file's do, so they are classified against the same spellings.
    album_track = _album_track(release)
    album_wide = {f.value for f in owned.ALBUM_WIDE}
    album_spellings = (
        {
            field: spellings
            for field, spellings in _supported_spellings(
                release, *album_track, prep.media_total
            ).items()
            if field in album_wide
        }
        if prep.unassigned and album_track is not None
        else {}
    )
    for path in prep.unassigned:
        now = _owned_now(path, prep.read)
        target = _unassigned_target(release, prep, now, tagging)
        positions[path] = _history_position(target)
        supported[path] = album_spellings
        if file_changes := owned.diff(now, target):
            changes[path] = file_changes
    return AlbumPlan(
        changes=changes,
        supported_spellings=supported,
        accepted_countries=prep.accepted_countries,
        positions=positions,
        unassigned=len(prep.unassigned),
    )


def _album_track(release: Release) -> _FlatTrack | None:
    """A track to read the album's own values from. They are the same on every
    track's tagset, so the first stands for all; a release with no tracklist
    has none."""
    return next(iter(_flatten_tracks(release)), None)


def _unassigned_target(
    release: Release, prep: _Prepared, now: Mapping[str, Any], tagging: TaggingChoices
) -> dict[str, Any]:
    """The owned fields a file left unassigned is written to: the album's from
    the release, and the track's as the file has them (`now`).

    The file belongs to the release, so it is filed with its album; which
    track it is nobody has said, so no track's title, number or ID arrives.
    """
    track = _album_track(release)
    if track is None:
        return {**now, owned.Owned.MB_ALBUM_ID: release["id"]}
    tagset = _build_tagset(release, *track, prep.media_total, tagging)
    return {**now, **{f.value: getattr(tagset, f.value) for f in owned.ALBUM_WIDE}}


#: The owned fields a `TaggingChoices` can change — every field whose value
#: depends on the user's spelling settings rather than on MusicBrainz alone.
SETTING_FIELDS: tuple[owned.Owned, ...] = (
    owned.Owned.ALBUM,
    owned.Owned.ALBUM_ARTIST,
    owned.Owned.ARTIST,
    owned.Owned.ALBUM_ARTIST_SORT,
    owned.Owned.ARTIST_SORT,
    owned.Owned.ALBUM_ARTISTS,
    owned.Owned.ARTISTS,
    # A composer, lyricist or writer credited under a variation is spelled by
    # the same setting (#680).
    owned.Owned.COMPOSERS,
    owned.Owned.COMPOSER_SORT,
    owned.Owned.LYRICISTS,
    owned.Owned.WRITERS,
)


def _spelling_key(value: Any) -> Any:
    """`value` in a form a set can hold: a list tag compares as a tuple."""
    return tuple(value) if isinstance(value, list) else value


def _supported_spellings(
    release: Release, medium: dict[str, Any], track_pos: int, track: Track, media_total: int
) -> dict[str, frozenset[Any]]:
    """Every value each `SETTING_FIELDS` field of this track has under SOME
    combination of the spelling settings (#678) — what one setting or another
    writes, by Harmonist or by Picard, for this release.

    By running the write under each combination (`transforms.every_choice`)
    rather than by rule, so the set is exactly the release's own strings and
    cannot drift from what the tagger writes. Per file, because a track's
    artist credit can differ from its neighbours'.
    """
    tagsets = [
        _build_tagset(release, medium, track_pos, track, media_total, choices)
        for choices in transforms_mod.every_choice()
    ]
    return {
        field.value: frozenset(_spelling_key(getattr(t, field.value)) for t in tagsets)
        for field in SETTING_FIELDS
    }


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

    **Nor does a non-empty one mean there is an update to take.** Two kinds of
    entry are written and recorded without being a reason to tag: a credit list
    arriving with one name in it (#337) and a second correct spelling of the
    release country (#346). Both are filtered at the flag, in
    `gardener._countable`, never out of this dict — see `_changes_for`.

    A second correct spelling of the album title used to be the third (#283,
    #545). It is an update now, of the lowest significance, because which
    spelling a library carries is the user's setting to choose (#685) — as are
    the artist names (#678). See `significance_of`.
    """

    changes: dict[Path, dict[str, list[Any]]]
    #: Per file, every value each setting-dependent field has under some
    #: combination of the spelling settings (`_supported_spellings`). Carried on
    #: the plan because the classifier has to ask — `gardener` holds a plan, not
    #: a release, and what makes a spelling legitimate is a fact about the
    #: release. List values are held as tuples.
    supported_spellings: Mapping[Path, Mapping[str, frozenset[Any]]] = dataclasses.field(
        default_factory=dict
    )
    #: Every country the release names (#346), which no setting chooses between.
    accepted_countries: frozenset[str] = frozenset()
    #: Per file, the position a tagging would record for it in History (#820),
    #: so a waiting update numbers its tracks as the History entry it becomes
    #: will, not by the files' order in the folder.
    positions: Mapping[Path, str | None] = dataclasses.field(default_factory=dict)
    #: How many of the album's files are paired with no track (#1004).
    unassigned: int = 0

    def is_second_spelling(self, field: str, before: object) -> bool:
        """Whether this entry is a value that was already right, not an update.

        The disk held one of the countries this release legitimately names, and
        a tagging is about to replace it with the one Harmonist writes. That is
        a real write and a real record — it is simply not a reason to re-tag,
        and not something to put in front of a user as an update available. No
        setting chooses among them, so there is nothing to converge on.

        Only on the BEFORE value: an `after` that is not what the tagger would
        write cannot occur, and asking about it would widen this past the
        strings the release states.
        """
        return field == owned.Owned.MB_ALBUM_COUNTRY and before in self.accepted_countries

    def follows_setting(self, path: Path, field: str, before: object) -> bool:
        """Whether this entry only moves the value to the spelling a setting
        selects (#685, #678).

        The disk held a value some combination of the settings writes for this
        release, and the tagger writes the one the user's choices select — which
        every `after` in this plan is, because the plan was built under them. So
        the change is the user's own convention arriving, and nothing
        MusicBrainz did.

        Exact values the release produces, never a pattern (review-gate item 2):
        `models.titles_match` would say yes to `(deluxe edition)` here, and a
        retitle mistaken for this would reach #273's trust setting disguised as
        something the user asked for.
        """
        supported = self.supported_spellings.get(path, {}).get(field)
        return supported is not None and _spelling_key(before) in supported

    def significance_of(
        self, path: Path, field: str, before: Any, after: Any
    ) -> owned.Significance:
        """What kind of change this entry is, given what the release says.

        `SETTINGS` where `follows_setting` holds, otherwise the field's own
        classification (`significance_of` at module level). The release's
        evidence can only lower a change, never raise one: a value that is not
        a spelling the release has keeps whatever its field says, so a
        MusicBrainz retitle arriving at the same time as a settings change is a
        retitle.
        """
        if self.follows_setting(path, field, before):
            return owned.Significance.SETTINGS
        return significance_of(field, before, after)

    @property
    def empty(self) -> bool:
        """True when a re-tag would write no owned field on any file."""
        return not self.changes


def significance_of(field: str, before: Any, after: Any) -> owned.Significance:
    """What kind of change this one entry of a tagging diff is (#267).

    `field` is a key from an `AlbumPlan`'s changes — an `owned.Owned` value or
    `owned.ARTWORK` — and `before`/`after` are that entry's two values. The
    field-level classification comes from `owned.SIGNIFICANCE`; the rules that
    cannot be given at field level are `owned.BY_VALUE` — how far such a field
    may drop — paired with `LOWERED_WHEN` below, which decides whether this
    particular change is one of the small ones. Only ever downwards: see
    `owned.BY_VALUE` for why that direction is not symmetric.

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
    REVIEW verdict is for. See `docs/design/tagging.md`.

    Raises `KeyError` for a key that is neither owned nor artwork, rather than
    guessing. A plan cannot produce one, and inventing a default here is how a
    field would quietly acquire a significance nobody gave it — which, once
    #273 lets a level be trusted, is how it would acquire permission to write
    itself.
    """
    declared = owned.SIGNIFICANCE[field]
    lowered = owned.BY_VALUE.get(field)
    if lowered is not None and LOWERED_WHEN[field](before, after):
        return lowered
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


def _credits_arrive_without_moving(before: Any, after: Any) -> bool:
    """Whether a credit list's change keeps every name where it was (#389, #804).

    Either the list arrives where there was none, or a file that held it as one
    joined value gets it back split. Both supply the list's shape and neither
    credits anybody new; anything else a credit list does stays IDENTITY.
    """
    return owned.fills_in_an_absent_list(before, after) or owned.splits_a_joined_list(before, after)


#: When each `owned.BY_VALUE` field drops to the level declared there — the
#: comparison half of the pair, keyed alike (#389).
#:
#: Here rather than in `owned` because one of these needs `models.norm_title`,
#: and `formats.owned` is a leaf that does not reach up into the model layer for
#: it. Each rule answers only "is this change smaller than the field says": what
#: smaller MEANS is `owned.BY_VALUE`'s to say, so a rule cannot quietly invent a
#: level, and `significance_of` indexes this table rather than `.get`-ing it, so
#: a field declared there with no rule fails loudly instead of classifying
#: itself at its declared level.
LOWERED_WHEN: dict[str, Callable[[Any, Any], bool]] = {
    owned.Owned.TITLE: _only_cosmetically_different,
    owned.Owned.ALBUM_ARTISTS: _credits_arrive_without_moving,
    owned.Owned.ARTISTS: _credits_arrive_without_moving,
    # The ids only split, never fill: an id list arriving where there was none
    # says which entities the album points at, and that is what IDENTITY is for.
    owned.Owned.MB_ALBUM_ARTIST_IDS: owned.splits_a_joined_list,
    owned.Owned.MB_ARTIST_IDS: owned.splits_a_joined_list,
    # Work and composer ids fill, which the artist ids above do not: see
    # `owned.BY_VALUE`.
    owned.Owned.MB_WORK_IDS: owned.fills_in_an_absent_list,
    owned.Owned.MB_COMPOSER_IDS: owned.fills_in_an_absent_list,
}


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
    #: The files paired with no track: the ones an explicit `assignment` leaves
    #: out, or the ones an earlier review did (#1004). `_unassigned_target` is
    #: what each is written to.
    unassigned: list[Path]
    #: The album's artwork plan, whole. What this tagging writes of it is
    #: `art_targets` and `cover_change`.
    art: artwork.ArtworkPlan
    #: The image each file in `art_targets` would take, by digest, for the
    #: per-file records — a file each, because a user choosing row by row can
    #: give two rows two different images (#659). Empty when nothing is written.
    art_after: Mapping[Path, str]
    media_total: int
    #: Every release country that counts as already correct: the ones THIS
    #: release names. Picard writes whichever of them `preferred_release_
    #: countries` matches, so a library tagged that way carries a code that is
    #: not MusicBrainz's scalar `country` and is not stale either. Album-constant,
    #: since every file's TagSet carries the same country, so it is settled once
    #: here rather than rebuilt per file. (The album title's spellings used to
    #: live here too; they are per file now, beside the artists' — see
    #: `_supported_spellings`.)
    accepted_countries: frozenset[str]
    #: The files this TAGGING may write `cover` to — the ones carrying nothing,
    #: or every file under `overwrite_art` (#418). Everything else keeps the
    #: image it has; improving those is the artwork action's job.
    art_targets: frozenset[Path] = frozenset()
    #: The folder cover this tagging creates, when the album has none (#457),
    #: and the image it gets — its own, which may be better than the tracks'
    #: (#479).
    cover_change: artwork.Change | None = None
    #: The page's preview no longer matches the plan, so this tagging writes no
    #: artwork at all (#469).
    art_withheld: bool = False
    #: What the track review read from each file, when it read them — nothing
    #: under an explicit `assignment`. `plan_album` diffs against the owned
    #: fields already in here rather than parse every file a second time
    #: (#793).
    read: Mapping[Path, formats.TrackTags] = dataclasses.field(default_factory=dict)


def _owned_now(path: Path, read: Mapping[Path, formats.TrackTags]) -> dict[str, Any]:
    """Every owned field `path` carries, from the read already made when there
    is one (#793). Same answer as `formats.read_owned`: each format builds both
    from the same open with the same `_read_owned`.

    Read again when there is nothing to reuse: no read under an explicit
    assignment, a file that could not be opened (where `read_owned` raises,
    which is how a plan says "I could not tell"), or an untagged Vorbis file,
    whose `read_tags` returns before it reads the owned fields.
    """
    tags = read.get(path)
    if tags is None or tags.unreadable or not tags.owned:
        return formats.read_owned(path)
    return dict(tags.owned)


def decide_artwork(
    album_dir: Path,
    files: Sequence[Path],
    cover_path: Path | None,
    *,
    archive: cover_art.Front | None = None,
    overwrite_art: bool = False,
    consider: bool = True,
    chosen: artwork.Source | None = None,
    choices: artwork.Choices | None = None,
    picks: Mapping[str, cover_art.Front] | None = None,
    folder_cover: artwork.FolderCoverPolicy = artwork.FolderCoverPolicy.IF_MISSING,
) -> artwork.ArtworkPlan:
    """The album's artwork plan, read from its files (#418, #469).

    The reading half of `artwork.plan`, which decides. Every track's image and
    the folder cover are described through the same `EmbeddedArt.of` the album
    page's tag read uses, so the page and this reach the same plan from the
    same disk — the property a reviewed page's fingerprint relies on.

    THE LARGEST IMAGE WINS — see `artwork.plan`. A re-tag used to embed the
    folder cover regardless, which on a real library shrank one album in twelve:
    3000px replaced by 2000px, in one case 5700px by 2000px.

    Reads files and nothing else. `archive` is the Cover Art Archive's image,
    handed in by a caller that has it — from the cache, or fetched by the
    tagging entry point before any of this starts. No network here: `plan_album`
    reaches this on the gardener's path, where a request per album is exactly
    what must not happen.

    `consider=False` opts out entirely: no candidates, no winner, and no reads.
    The gardener's flag is about TAGS, and it used to say so by passing
    `cover_path=None` — which was a proxy, not a statement, and stopped being
    true the moment the archive became a second source that needs no folder
    cover (#442). Say the thing rather than implying it (#448).

    `chosen` is the candidate the user picked, when they picked one (#472,
    #488). It reaches here because the plan is rebuilt at write time: a re-tag
    that rebuilt it without the choice would produce the plan the SIZE RULE
    prefers, which still matches its own fingerprint and so writes a different
    image from the one the page showed — silently, and only when the user had
    overridden the rule.

    `choices` are the same, made row by row (#659), and ride here for the same
    reason — with `picks`, the originals of the archive images a choice may
    name by id.

    `folder_cover` is the user's policy on creating one the album has not got
    (#516), and it rides here for the reason `chosen` does: the plan is rebuilt
    at write time, so a caller that dropped it would rebuild the plan the
    default prefers and write a `cover.jpg` the page never offered.
    """
    if not consider:
        return artwork.ArtworkPlan(album_dir=album_dir)
    cover = None
    if cover_path is not None:
        cover = artwork.FolderCover(
            name=cover_path.name,
            image=formats.EmbeddedArt.of(cover_path.read_bytes(), _mime_for(cover_path)),
            path=cover_path,
            beside=cover_art.covers_beside(cover_path),
        )
    archive_image = (
        formats.EmbeddedArt.of(archive.data, archive.mime) if archive is not None else None
    )
    sc = sidecar_mod.read(album_dir)
    if sc and sc.artwork_rejected and sc.mb_release_id:
        artwork_watch.observe(sc.mb_release_id)
    return artwork.plan(
        album_dir,
        [(f, _embedded_art(f)) for f in files],
        cover,
        archive_image,
        excluded_archive=artwork_watch.excluded(sc, archive_image),
        overwrite_art=overwrite_art,
        # From the files, as the page has it from their tags: a side without
        # it would plan to keep tracks the other embiggens (#872).
        lossless=formats.all_lossless(formats.describe(f) for f in files),
        audio_bytes=album_files.total_bytes(files),
        # A copy squished to fit holds its original (#888), as on the page.
        originals=derived.originals(),
        chosen=chosen,
        choices=choices,
        picks={
            image_id: formats.EmbeddedArt.of(front.data, front.mime)
            for image_id, front in (picks or {}).items()
        },
        folder_cover=folder_cover,
    )


def _prepare(
    album_dir: Path,
    release: Release,
    cover_path: Path | None,
    *,
    incomplete: bool,
    overwrite_art: bool,
    files: list[Path] | None,
    artwork_in_scope: bool = True,
    archive: cover_art.Front | None = None,
    expected_artwork: str | None = None,
    assignment: dict[Path, int] | None = None,
) -> _Prepared:
    """Decide what a tagging of this album would consist of before writing.

    (It does read embedded artwork, when there is a candidate that might
    replace it — that is the only way to know whether replacing it would
    destroy per-track images.)

    `artwork_in_scope=False` leaves artwork out altogether, for a caller whose
    question is only about tags. See `decide_artwork` for why that has to be
    stated rather than implied by passing no cover path (#448).
    """
    files = files if files is not None else album_files.audio_files(album_dir)
    flat_tracks = list(_flatten_tracks(release))

    # What the count guard is entitled to expect on disk: the release's tracks
    # minus the ones on media Harmonist cannot tag (#235). Counting a bonus
    # DVD's 53 videos against 16 audio files made a complete, correctly tagged
    # album permanently un-re-taggable — and it is the albums MusicBrainz has
    # since corrected that most need the button.
    taggable = _taggable_tracks(release, flat_tracks)

    # The files a reviewed confirmation left unassigned, which their tags
    # record (#1004). That review already said which files are left over, so
    # they stay out of the count and out of the ladder: the count would refuse
    # an album the user confirmed, and the ladder would guess them a track.
    left: list[Path] = []
    pairs: list[tuple[Path, _FlatTrack]]
    if assignment is None:
        tags = [formats.read_tags(path) for path in files]
        review = assignment_review(release, tags)
        if review.required:
            raise TrackAssignmentRequired(
                files=len(files), tracks=len(taggable), unassigned=review.unassigned
            )
        left = [files[i] for i in sorted(review.left_unassigned)]
        counted = [
            pair
            for i, pair in enumerate(zip(files, tags, strict=True))
            if i not in review.left_unassigned
        ]
        # Assigned against EVERY track, not just the taggable ones: a file that
        # names a video track's id is a file in the wrong place, and quietly
        # re-pointing it at an audio track would be the invention this ladder
        # exists to avoid.
        pairs, filmed = _assign_files_to_tracks(
            [f for f, _ in counted],
            flat_tracks,
            [compare.identity_of(t) for _, t in counted],
            videos=_videos_beside(album_dir, files),
        )
        # A track a video file on disk already is counts as on disk (#1056),
        # as it does for the scan (#193) and the album page (#226). Only on a
        # disc that is partly audio does this change the count, since a disc
        # all of video is not expected at all — Fear Of Music's Blu-ray is hi-res
        # mixes and two concert films, and counting the films as missing audio
        # flagged a complete album as a Structural update.
        on_video = len(_taggable_tracks(release, [flat_tracks[i] for i in sorted(filmed)]))

    # The messages and the error count every file on disk, as the user would.
    if assignment is None and not incomplete and len(counted) + on_video != len(taggable):
        videos = f" and {on_video} video{'s' if on_video != 1 else ''}" if on_video else ""
        raise TagMismatchError(
            f"album {album_dir.name!r}: {len(files)} audio files{videos} but MB "
            f"release has {len(taggable)} tracks",
            files=len(files) + on_video,
            tracks=len(taggable),
        )
    if assignment is None and len(counted) > len(flat_tracks):
        raise TagMismatchError(
            f"album {album_dir.name!r}: {len(files)} files exceeds MB release "
            f"track count {len(flat_tracks)} — extra files on disk need an "
            f"explicit track assignment review",
            files=len(files),
            tracks=len(flat_tracks),
        )

    if assignment is not None:
        # Only explicitly paired files receive track metadata. Missing keys
        # are intentional gaps, never invitations to the automatic ladder.
        if (
            not set(assignment).issubset(files)
            or len(set(assignment.values())) != len(assignment)
            or any(type(s) is not int or not 0 <= s < len(flat_tracks) for s in assignment.values())
        ):
            raise ValueError("invalid track assignment: paired files need unique MB tracks")
        pairs = [(f, flat_tracks[assignment[f]]) for f in files if f in assignment]
        if any(t not in taggable for _, t in pairs):
            raise ValueError("invalid track assignment: video tracks cannot be tagged")

    if assignment is not None:
        left = [path for path in files if path not in assignment]
        for path in left:
            carried = formats.read_owned(path)
            if carried.get(owned.Owned.MB_RELEASE_TRACK_ID) or carried.get(owned.Owned.MB_TRACK_ID):
                raise ValueError(
                    f"{path.name}: unassigned file already has MusicBrainz track IDs. "
                    "Pair it with the correct track, or remove conflicting track IDs before confirming."
                )

    if archive is None and artwork_in_scope:
        archive = _archive_candidate(release)
    art = decide_artwork(
        album_dir,
        files,
        cover_path,
        archive=archive,
        overwrite_art=overwrite_art,
        consider=artwork_in_scope,
    )
    # A TAGGING FILLS GAPS AND REPLACES NOTHING (#418). Improving an image the
    # album already has is the artwork action's, because it is the change a user
    # is most likely to want to undo on its own — an Addition and a Replacement
    # are different operations (#468), and a tagging is permitted only the first.
    #
    # Per FILE, not per album, which is why this is a set of targets rather than
    # a flag: an album with ten good images and two gaps gets the two filled and
    # the ten left exactly as they are. That album is #397, and it is the shape
    # this rule exists for. A folder cover the album lacks is an addition too.
    #
    # `overwrite_art` remains what it always was — an explicit "embed the folder
    # cover over everything" — and is the one way a tagging still replaces.
    #
    # No `scope` parameter here, deliberately (#482). Widening what a write
    # takes from the plan is `tag_and_artwork`'s question, because it is the
    # only entry point that takes a fingerprint from a page and so the only one
    # with a preview to be faithful to. This function serves `tag_album`, which
    # writes no artwork at all, and `plan_album`, which writes nothing — a knob
    # neither of them turns would be API with no reader.
    scope = artwork.Scope.ALL if overwrite_art else artwork.Scope.ADDITIONS
    withheld = expected_artwork is not None and art.fingerprint(scope) != expected_artwork
    art_targets = frozenset() if withheld else art.track_targets(scope)
    cover_change = None if withheld else art.cover_change(scope)
    # The images are READ only when something would really be written, and
    # read to prove they are still where the plan found them: an album that
    # needs nothing costs no second read of its winner (#44, #74).
    written = () if withheld else art.scoped(scope)
    if written:
        try:
            _load_images(art, written, cover_path, archive)
        except ArtworkChangedError:
            # Between reading the album and loading the image, the image moved.
            # Tag without it rather than write something the plan never named.
            log.warning(
                "the winning artwork for %s changed while it was being tagged — "
                "tagging without writing any",
                album_dir.name,
                exc_info=True,
                extra={"album_id": sidecar_mod.album_id_for(album_dir)},
            )
            art_targets, cover_change = frozenset(), None

    return _Prepared(
        files=files,
        pairs=pairs,
        unassigned=left,
        art=art,
        # Only now is it settled that the embed is really happening — the
        # per-track-art guard may have cancelled it, and the preview check may
        # have withheld it.
        art_after={c.target: c.after for c in art.changes if c.target in art_targets},
        art_targets=art_targets,
        cover_change=cover_change,
        art_withheld=withheld,
        media_total=len(release.get("medium-list", [])) or 1,
        accepted_countries=release_countries(release),
        read=dict(zip(files, tags, strict=True)) if assignment is None else {},
    )


def _changes_for(
    tagset: TagSet,
    before: dict[str, Any],
    file_path: Path,
    art_before: Mapping[Path, str | None],
    art_after: str | None,
) -> dict[str, list[Any]]:
    """What tagging this one file to `tagset` would change, as `{field: [was, now]}`.

    The single answer to that question, so a dry run (`plan_album`) and the
    audit record a real tagging writes are computed by the same code over the
    same values. When they were two expressions they were free to disagree, and
    a detector that disagreed with the record would classify changes the
    history then said never happened.

    Only fields that actually changed appear — see `owned.diff`.

    **Every change, including the ones that are not an update to take** (#545).
    An album title or release country with a second legitimate spelling used to
    be deleted here, and that silenced three readers where only two wanted it:
    the write-skip and the update flag, yes — but also the audit record and the
    undo plan, while `write_tags` put the new spelling on the file regardless,
    because it writes the whole `TagSet` and not this diff. The record then said
    a field had not moved that had.

    The tolerance lives at the flag instead, in `gardener._countable`, exactly
    where `owned.is_opportunistic` puts #337's. Its docstring is the rule: a
    change that is not a reason to tag is still written, and still recorded.
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
    naming: album_files.Naming,
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
        file=naming.name_of(file_path),
        changes=changes,
        track_ref=tagset.mb_release_track_id,
        rec_ref=tagset.mb_track_id,
        position=_tagset_position(tagset),
    )


def _tagset_position(tagset: TagSet) -> str:
    """Where a tagset puts its track, in tagging history's notation: "2-1" on a
    multi-disc release, "7" otherwise. One function for the record a tagging
    writes and the plan that previews it, so the two number a track alike."""
    return (
        f"{tagset.disc_num}-{tagset.track_num}" if tagset.disc_total > 1 else str(tagset.track_num)
    )


def _embedded_art(path: Path) -> formats.EmbeddedArt | None:
    """One file's embedded image, described — or None where it carries none.

    One read per file, at tag time, when the files are being opened anyway. The
    same pass answers the questions that used to need separate machinery:
    whether the album carries per-track artwork worth preserving, how large each
    image is, and what each track's art WAS, so a tagging can record that it
    replaced it (#86).

    Described by `EmbeddedArt.of`, exactly as `formats.read_tags` describes it
    for the album page — so the digest this records is the one the artwork store
    keys on (#131), and the page and the tagger plan from identical facts.
    """
    art = formats.read_cover(path)
    return formats.EmbeddedArt.of(art[0], art[1]) if art is not None else None


def _mime_for(path: Path) -> str:
    """The image type of a folder cover, from its name — the store uses it only
    to give the kept file a real extension."""
    return "image/png" if path.suffix.lower() == ".png" else "image/jpeg"


def _archive_candidate(release: Release) -> cover_art.Front | None:
    """The archive's image for this release from the LOCAL CACHE, or None.

    Cache-only, deliberately. A tagging entry point that needs the archive asks
    it before tagging starts (`cover_art.front_image`) and hands the answer in;
    everything else — the gardener's `plan_album` above all — must not put a
    request behind every album it touches.
    """
    mbid = release.get("id")
    return cover_art.cached_front(mbid) if isinstance(mbid, str) else None


class ArtworkChangedError(Exception):
    """The image a plan names is not the image found when it came to be written.

    Raised before anything is written. The plan was reviewed, or at least
    decided, against one picture; writing a different one under it would be the
    preview saying one thing and the write doing another (#469).
    """


def _image_bytes(
    plan: artwork.ArtworkPlan,
    incoming: artwork.Incoming,
    cover_path: Path | None,
    archive: cover_art.Front | None,
    picks: Mapping[str, cover_art.Front] | None = None,
) -> bytes:
    """One of the plan's images, read from where the plan found it — and
    checked to BE that image, by digest.

    An archive image is the release's own front (`archive`) or one picked by
    id (`picks`, #659); which, is whichever of them the plan's digest is.

    The plan carries descriptions, not bytes: an album page holds every track's
    tags at once, and carrying the images too would be hundreds of megabytes on
    a box set (`EmbeddedArt`). So the bytes are fetched here, at the last
    moment — and anything that has moved since the plan was made is refused
    rather than written.

    Asked once per distinct image an action writes (`_load_images`).
    """
    image, source = incoming.image, incoming.source
    data: bytes | None = None
    try:
        if source is artwork.Source.FOLDER and cover_path is not None:
            data = cover_path.read_bytes()
        elif source is artwork.Source.ALBUM:
            carrier = next((p for p, d in plan.before.items() if d == image.digest), None)
            art = formats.read_cover(carrier) if carrier is not None else None
            data = art[0] if art is not None else None
        elif source is artwork.Source.ARCHIVE:
            offered = [*([archive] if archive is not None else []), *(picks or {}).values()]
            data = next((f.data for f in offered if images.digest(f.data) == image.digest), None)
    except OSError as e:
        raise ArtworkChangedError(f"could not read the winning image again: {e}") from e
    if data is None or images.digest(data) != image.digest:
        raise ArtworkChangedError("the winning image is no longer where the plan found it")
    return data


def _load_images(
    plan: artwork.ArtworkPlan,
    changes: Sequence[artwork.Change],
    cover_path: Path | None,
    archive: cover_art.Front | None,
    picks: Mapping[str, cover_art.Front] | None = None,
) -> dict[str, bytes]:
    """The bytes of every distinct image `changes` write, by digest — all of
    them before anything is written.

    Usually one image; an album whose folder cover is deliberately better than
    its embedded art has two (#479), and a user choosing row by row can give
    every row its own (#659). All loaded up front for two reasons: a failure
    then costs nothing, and an image read from the album's own tracks has to be
    read before the write that replaces those tracks' copies.
    """
    loaded: dict[str, bytes] = {}
    for change in changes:
        if change.after not in loaded:
            loaded[change.after] = _image_bytes(
                plan, plan.image_for(change), cover_path, archive, picks
            )
    return loaded


def _fit_images(album_dir: Path, loaded: Mapping[str, bytes]) -> dict[str, bytes]:
    """What to write for each image the plan names, by the plan's digest: the
    image itself, or a squished copy of one over the image size limit (#888).

    An image that can't be squished is written as it is, which is what was
    written before squishing existed: it loses nothing the album had, where
    refusing would cost the user the image they picked."""
    fitted: dict[str, bytes] = {}
    for digest, data in loaded.items():
        try:
            out = squish.fit(data, limit=artwork.PLAYER_MAX_BYTES)
        except squish.SquishError:
            log.warning(
                "could not squish the %d-byte image %s for %s; writing it as it is",
                len(data),
                digest[:12],
                album_dir.name,
                exc_info=True,
            )
            out = None
        fitted[digest] = out if out is not None else data
    return fitted


class _Wrote(StrEnum):
    """How one folder-cover write went."""

    WRITTEN = "written"
    #: The folder no longer holds what the plan saw — an edit made since.
    STALE = "stale"
    #: Its current image could not be kept, so it was not replaced (#470).
    UNKEPT = "unkept"
    #: It could not be read or written.
    FAILED = "failed"


def _write_folder_cover(
    album_dir: Path,
    change: artwork.Change,
    image: bytes,
    source: artwork.Source | None,
    album_id: str | None,
    kept: frozenset[str] = frozenset(),
) -> _Wrote:
    """Create or replace the folder cover as `change` says, and record it.

    Checked against the plan first. A creation finds no `cover.*` there now; a
    replacement finds the very image it was planned against. Either one failing
    means somebody changed the folder since, and their change stands.

    A replacement goes ahead only if the image it overwrites is among `kept` —
    the backups the caller took, all together, before any write (#408, #470).
    No retained backup, no replacement.

    A cover that cannot be written is a reason to warn, not to abandon the
    re-tag the user asked for.

    Recorded twice, as a promotion always was: `cover.write` for forensics, and
    a `tag.track` line carrying the `artwork` pair — the shape History renders
    and the artwork Undo reads. A creation's pair is `[None, digest]`: the image
    it added and the absence it added it to (#471 builds the Undo on that).

    `image` may be a squished copy of the plan's image (#888). Both records
    name the copy, since it's what the file holds and what Undo checks for;
    `cover.write` names the image it was squished from as well.
    """
    target = change.target
    if change.before is None:
        if cover_art.cached_cover(target.parent) is not None:
            log.info("%s gained a folder cover since it was planned; leaving it", album_dir.name)
            return _Wrote.STALE
    else:
        try:
            was = target.read_bytes()
        except OSError:
            log.exception("could not read %s before replacing it", target)
            return _Wrote.FAILED
        if images.digest(was) != change.before:
            log.info("%s changed since it was planned; leaving it", target.name)
            return _Wrote.STALE
        if change.before not in kept:
            # Reported by the caller, once, with the rest of the operation's
            # outcome — not here as well.
            log.info("not replacing %s: its current image could not be kept", target.name)
            return _Wrote.UNKEPT
    # Renamed as it's replaced, to the name its new image's type takes (#905).
    # The plan never renames onto a cover it saw; one that has appeared since is
    # somebody's change, and stands.
    dest = change.rename_to or target
    if change.rename_to is not None and dest.exists():
        log.info("%s gained a %s since it was planned; leaving it", album_dir.name, dest.name)
        return _Wrote.STALE
    try:
        tmp = dest.with_suffix(dest.suffix + ".tmp")
        tmp.write_bytes(image)
        os.replace(tmp, dest)
    except OSError:
        log.exception("could not write the album's artwork to %s", dest)
        return _Wrote.FAILED
    if change.rename_to is not None:
        try:
            target.unlink()
        except OSError:
            # Both names would hold a cover, and `cover.jpg` would win whichever
            # it was. Taking the new one back leaves the album as it was.
            log.exception("could not remove %s after writing %s", target, dest)
            dest.unlink(missing_ok=True)
            return _Wrote.FAILED

    # The id the album answers to right now (#456). A cover is often created by
    # an album's FIRST tagging, before any sidecar names it — and a bare None
    # would leave exactly the albums most likely to gain a cover with no record
    # of having gained one. `id_registry.peek` is a pure hash of the path, what
    # `sidecar.write()` then persists as `temp_uid`, and what the scanner already
    # calls the album meanwhile; the alias chain carries it forward from there.
    album_id = album_id or id_registry.peek(album_dir)
    written = images.digest(image)
    details = {"was": change.before} if change.before is not None else {}
    if written != change.after:
        details["squished_from"] = change.after
    audit.record(
        "cover.write",
        album_id=album_id,
        album=album_dir,
        file=dest.name,
        source=source.value if source is not None else "-",
        bytes=len(image),
        overwrote=change.before is not None,
        digest=written,
        **details,
    )
    if dest != target:
        # The old name is gone from disk now, and this is the only place both
        # are known. Its image is kept, under `was` in `cover.write`.
        audit.record(
            "cover.rename", album_id=album_id, album=album_dir, file=dest.name, was=target.name
        )
    _record_cover_change(album_dir, album_id, target, dest, change.before, written)
    return _Wrote.WRITTEN


def _rename_folder_cover(album_dir: Path, change: artwork.Change, album_id: str | None) -> _Wrote:
    """Rename the folder cover as `change` says, its image as it is (#907).

    Checked against the plan as a replacement is: the cover still holds the
    image the plan saw, and nothing has taken the new name since. A move, not
    a rewrite, so the file keeps everything about it but its name."""
    target, dest = change.target, change.rename_to
    assert dest is not None  # a rename-only change always names where it goes
    try:
        was = target.read_bytes()
    except OSError:
        log.exception("could not read %s before renaming it", target)
        return _Wrote.FAILED
    if images.digest(was) != change.before or dest.exists():
        log.info("%s changed since it was planned; leaving it", target.name)
        return _Wrote.STALE
    try:
        os.replace(target, dest)
    except OSError:
        log.exception("could not rename %s to %s", target, dest.name)
        return _Wrote.FAILED
    album_id = album_id or id_registry.peek(album_dir)
    audit.record(
        "cover.rename", album_id=album_id, album=album_dir, file=dest.name, was=target.name
    )
    _record_cover_change(album_dir, album_id, target, dest, change.before, change.after)
    return _Wrote.WRITTEN


def _record_cover_change(
    album_dir: Path,
    album_id: str | None,
    target: Path,
    dest: Path,
    before: str | None,
    written: str,
) -> None:
    """The `tag.track` line a folder-cover write leaves: the shape History
    renders and the artwork Undo reads.

    Under the name the file has now, which is where Undo looks for it, and
    with the name it had where that differs, which Undo puts back (#907)."""
    changes: dict[str, list[str | None]] = {owned.ARTWORK: [before, written]}
    if dest != target:
        changes[owned.FILE_NAME] = [target.name, dest.name]
    event_id = audit.record("tag.track", album_id=album_id, album=album_dir, file=dest.name)
    if event_id is not None:
        activity_store.record_tag_changes(event_id, file=dest.name, changes=changes)


def _keep_doomed_art(
    digests: Mapping[Path, str | None], incoming: str | None = None
) -> frozenset[str]:
    """Copy every image an operation is about to overwrite into the artwork
    store, and return the digests actually retained (#131, #470).

    `digests` is each target and the image it is expected to hold — a track or
    the folder cover alike. What comes back is the licence to replace: a target
    whose image is not in it must be left as it is. NO RETAINED BACKUP MEANS NO
    REPLACEMENT, for embedded and folder artwork alike; a backup is not a
    nicety that a full store may quietly skip.

    Kept ALL TOGETHER (`artwork_store.keep_all`), so the size cap cannot evict
    one of this operation's backups to make room for another.

    Reads only the doomed files, and only the ones still holding the image the
    caller expects: an image that has changed since is not the one being
    overwritten, and the per-target check that follows leaves it alone anyway.
    Deduplicated by digest, so tracks sharing one cover cost one file.
    """
    wanted: dict[str, tuple[bytes, str | None]] = {}
    for path, key in digests.items():
        if key is None or key == incoming or key in wanted:
            continue
        art = _image_at(path)
        if art is None or images.digest(art[0]) != key:
            continue  # gone or changed since; the write will find it stale
        wanted[key] = (art[0], art[1])
    return artwork_store.keep_all(wanted.values()) if wanted else frozenset()


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
    current: dict[Path, dict[str, Any]], resolved: dict[Path, tag_history.FileRevert]
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

    "Every file in the album" means every file the album HAS, across all of its
    directories (#423). Enumerating only the primary one made a split album's
    second disc invisible here: the discs it could not see were counted as
    absent, so the album's identity was moved on the files it could see and
    left on the rest — the split the all-or-nothing rule exists to prevent.
    """
    field = owned.Owned.MB_ALBUM_ID
    # Use the same uniquely resolved files as the per-field undo, including
    # renamed files. Never pair the history and directory by list order.
    changes = {path: item.fields[field] for path, item in resolved.items() if field in item.fields}
    if not changes or len(changes) != len(resolved):
        return None
    befores = {before for before, _after in changes.values()}
    if len(befores) != 1:
        return None

    if set(current) != set(changes):
        # Files have appeared or gone since. Any the plan doesn't name would
        # keep whatever id they carry, so moving the rest would split the
        # album's identity between two releases.
        return None
    for path, snapshot in current.items():
        if owned.values_differ(snapshot.get(field), changes[path][1]):
            return None

    before = next(iter(befores))
    return _IdentityRevert(value=before if isinstance(before, str) and before else None)


def _history_position(snapshot: dict[str, Any]) -> str | None:
    """The tagged position, in the same notation used by tagging history."""
    track = snapshot.get(owned.Owned.TRACK_NUM)
    disc = snapshot.get(owned.Owned.DISC_NUM) or 1
    total = snapshot.get(owned.Owned.DISC_TOTAL) or 1
    if not isinstance(track, int) or track < 0:
        return None
    return f"{disc}-{track}" if total > 1 or disc > 1 else str(track)


def _revert_file(
    naming: album_files.Naming,
    item: tag_history.FileRevert,
    snapshots: dict[Path, dict[str, Any]],
) -> Path:
    """Find a history track within this album, refusing ambiguous evidence.

    A missing identifier permits the next lookup; an ambiguous identifier does
    not. Never break a tie using a weaker identifier or files already claimed
    by other records. All lookups use the same pre-write snapshot.
    """
    if naming.matches(item.file):
        return _file_named(naming, item.file, RevertUnavailableError)

    def unique(matches: list[Path]) -> Path | None:
        if len(matches) > 1:
            raise RevertUnavailableError(f"{item.file}: ambiguous track identity in this album")
        return matches[0] if matches else None

    if item.track_ref:
        found = unique(
            [
                p
                for p, tags in snapshots.items()
                if tags.get(owned.Owned.MB_RELEASE_TRACK_ID) == item.track_ref
            ]
        )
        if found is not None:
            return found
    if item.position:
        # A single-disc record's '1' and a later '1-1' name the same position.
        position = item.position if "-" in item.position else f"1-{item.position}"
        positions = {}
        for path, tags in snapshots.items():
            value = _history_position(tags)
            if value is not None:
                positions[path] = value if "-" in value else f"1-{value}"
        matches = [p for p, value in positions.items() if value == position]
        if item.rec_ref:
            found = unique(
                [p for p in matches if snapshots[p].get(owned.Owned.MB_TRACK_ID) == item.rec_ref]
            )
            if found is not None:
                return found
        found = unique(matches)
        if found is not None:
            return found
    raise RevertUnavailableError(
        f"{item.file} is no longer in this album; no unique track identity found"
    )


def _file_named(naming: album_files.Naming, name: str, error: type[Exception]) -> Path:
    """The one file in this album a stored record names, or a refusal.

    `name` reaches here from a permanent, unversioned record, so it is looked up
    among the album's own files rather than joined to a directory — a name can
    then never address anything outside the album, however malformed it is.

    Two files can answer to one name only for a record written before the name
    carried its disc (#423). There is nothing to choose between them, and
    writing to the wrong one is worse than declining: an undo that quietly puts
    disc 2's tags on disc 1 is a lie about the user's files that reports success.
    """
    matches = naming.matches(name)
    if len(matches) > 1:
        raise error(
            f"{name!r} could be any of {len(matches)} files in this album — this "
            "record was written before Harmonist recorded which disc a file was "
            "on, and putting the tags on the wrong one is worse than declining"
        )
    if not matches:
        raise error(f"{name} is no longer in this album")
    return matches[0]


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


def revert_tags(
    album_dir: Path,
    plan: Sequence[tag_history.FileRevert],
    *,
    paths: Sequence[Path] | None = None,
) -> RevertOutcome:
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

    `paths` is every directory the album occupies, and a caller holding an
    `Album` should pass them (#423): since #197 the album's files can sit in
    sibling directories, and a record naming one of those was resolved against
    the primary directory alone — which for two discs whose files share a
    filename put one disc's tags on the other's file.

    Writes its own per-file records, so the undo appears in History with its own
    field list and is itself undoable.
    """
    album_all = album_files.for_paths(paths if paths is not None else [album_dir])
    naming = album_files.Naming(album_dir, album_all)
    targets: dict[Path, tuple[dict[str, Any], dict[str, Any]]] = {}
    restored: set[str] = set()
    stale: set[str] = set()
    snapshots = {}
    for path in album_all:
        try:
            snapshots[path] = formats.read_owned(path)
        except Exception as e:
            # File-format boundary: surface the failed read to the user before
            # any writes; parser errors must never look like missing tags.
            raise RevertUnavailableError(
                f"could not read the tags on {naming.name_of(path)}: {e}"
            ) from e
    resolved: dict[Path, tag_history.FileRevert] = {}
    for item in plan:
        path = _revert_file(naming, item, snapshots)
        if path in resolved:
            raise RevertUnavailableError(f"{item.file}: two history records name the same file")
        resolved[path] = item
    identity = _identity_revert(snapshots, resolved)

    for path, item in resolved.items():
        current = snapshots[path]

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
            file=naming.name_of(path),
        )
        if event_id is not None:
            changes = owned.diff(before, target)
            if changes:
                activity_store.record_tag_changes(
                    event_id,
                    file=naming.name_of(path),
                    changes=changes,
                    track_ref=target.get(owned.Owned.MB_RELEASE_TRACK_ID),
                    rec_ref=target.get(owned.Owned.MB_TRACK_ID),
                    position=_history_position(target),
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


@dataclass(frozen=True)
class ArtworkOutcome:
    """What an artwork action actually wrote — not what it was asked to."""

    changed: int = 0
    #: Targets left alone because they no longer held what the plan saw: an
    #: edit made since, which stands. Named, so the user knows where to look.
    stale: tuple[str, ...] = ()
    #: Targets not replaced because the image they hold could not be kept, and
    #: replacing it would have left no way back (#470).
    unkept: tuple[str, ...] = ()
    #: Targets that could not be read or written, already logged.
    failed: tuple[str, ...] = ()
    #: The images actually written, by digest, once each — what a stand-in
    #: record is made from (#663), since only a written image can be one. A
    #: squished copy's own digest (#888), since a stand-in record names the
    #: image as the album carries it.
    written: tuple[str, ...] = ()


def apply_artwork(
    album_dir: Path,
    plan: artwork.ArtworkPlan,
    *,
    files: Sequence[Path],
    cover_path: Path | None,
    archive: cover_art.Front | None = None,
    picks: Mapping[str, cover_art.Front] | None = None,
    scope: artwork.Scope = artwork.Scope.ALL,
) -> ArtworkOutcome:
    """Carry out the part of `plan` that `scope` permits, and nothing else.

    `archive` and `picks` are the archive images the plan may write: the
    release's own front, and any picked by id (#659).

    The executor half of #469. It decides nothing: which image, and where, came
    from the plan — the one the album page drew its rows from, when the page is
    who asked. A plan whose winner can no longer be found raises
    `ArtworkChangedError` before anything is written.

    Each target is re-read before it is written and left alone if it no longer
    holds what the plan saw, so an image changed by anyone in between survives
    and is reported rather than overwritten.

    Writes ARTWORK ONLY, through `formats.write_cover`. Tags are not touched.
    """
    changes = plan.scoped(scope)
    if not changes:
        return ArtworkOutcome()
    # The id the album answers to right now, sidecar or not (#456). The artwork
    # action works on an album Harmonist has not identified, and a record with
    # no album id is one History — and so the artwork Undo — can never find.
    album_id = sidecar_mod.album_id_for(album_dir) or id_registry.peek(album_dir)
    # Named the same way a tagging names them, or the Undo this records would
    # address the wrong disc of a split album (#423).
    naming = album_files.Naming(album_dir, files)
    tracks = [c for c in changes if not c.folder_cover]
    cover_change = plan.cover_change(scope)

    # Every image this action writes — the tracks' and the folder cover's own,
    # which may be the better one (#479), and one per row a user chose for
    # (#659). Loaded before anything is written, and only the ones this action
    # really writes — a plan that touches no track reads no track.
    # Keyed by the plan's digest whether or not it was squished (#888): the
    # plan and the outcome speak of the image that was picked, the records of
    # each file of the copy it holds.
    # A rename on its own writes no image, so none is loaded for it (#907).
    writes = [c for c in changes if not c.rename_only]
    loaded = _fit_images(album_dir, _load_images(plan, writes, cover_path, archive, picks))
    holds = {d: images.digest(data) for d, data in loaded.items()}
    track_digests = list(dict.fromkeys(c.after for c in tracks))

    # Before anything is written, and for the same reason `tag_album` records
    # its `tag.album` line first: a crash part-way through leaves evidence of
    # what was attempted rather than silence. `digest` names each image the
    # tracks take, in album order; usually that is one.
    audit.record(
        "artwork.update",
        album_id=album_id,
        album=album_dir,
        files=len(tracks),
        digest=",".join(track_digests) or "-",
        cover=cover_change.after if cover_change is not None else "-",
        scope=scope.value,
    )
    for original, copy in holds.items():
        if copy != original:
            audit.record(
                "artwork.squish",
                album_id=album_id,
                album=album_dir,
                original=original,
                digest=copy,
                bytes=len(loaded[original]),
            )
    # Keep whatever is about to be destroyed — every track AND the folder cover,
    # in one go — before a single write, so this is undoable (#131) and so a
    # target whose image could not be kept is known before anything moves (#470).
    # No `incoming` to skip: a change exists only where the target does NOT
    # already hold what is coming, so every before-image here is really doomed.
    # That includes a change whose `before` and `after` are the same image over
    # the limit (#893): what's coming is its squished copy, and Undo restores
    # the original from what's kept here. A rename on its own destroys nothing.
    kept = _keep_doomed_art({c.target: c.before for c in writes})

    changed = 0
    stale: list[str] = []
    unkept: list[str] = []
    failed: list[str] = []
    written: list[str] = []
    with progress.job("Applying artwork", len(changes), foreground=True) as job:
        for change in job.items(tracks, lambda change: naming.name_of(change.target)):
            name = naming.name_of(change.target)
            current = formats.read_cover(change.target)
            if (images.digest(current[0]) if current is not None else None) != change.before:
                stale.append(name)
                continue
            if change.before is not None and change.before not in kept:
                unkept.append(name)
                continue
            try:
                formats.write_cover(change.target, loaded[change.after])
            except formats.WRITE_ERRORS:
                # One file's failure, not the tagging's (#494). Artwork is written
                # AFTER the tags, so letting this propagate threw away the report of
                # work that had really happened: the route caught it as an unknown
                # error and said "Re-tag failed" over an album whose files had just
                # been re-tagged, and the sidecar update that follows never ran.
                #
                # Tolerable here only because the outcome can still tell the truth —
                # the file is named in `failed`, which both the album page and the
                # feed already report, so this cannot read as success. The folder
                # cover has been handled this way since #457.
                log.exception("could not write the album's artwork to %s", change.target)
                failed.append(name)
                continue
            changed += 1
            written.append(holds[change.after])
            # Recorded in the shape a tagged file's artwork change takes, which is
            # what puts an Undo on it: `tag_history.artwork_revert_plan` reads that
            # pair and `restore_artwork` writes it back.
            event_id = audit.record("tag.track", album_id=album_id, album=album_dir, file=name)
            if event_id is not None:
                activity_store.record_tag_changes(
                    event_id,
                    file=name,
                    changes={owned.ARTWORK: [change.before, holds[change.after]]},
                )

        for cover in job.items([cover_change] if cover_change else [], lambda c: c.target.name):
            if cover.rename_only:
                wrote = _rename_folder_cover(album_dir, cover, album_id)
            else:
                wrote = _write_folder_cover(
                    album_dir,
                    cover,
                    loaded[cover.after],
                    plan.image_for(cover).source,
                    album_id,
                    kept,
                )
            if wrote is _Wrote.WRITTEN:
                changed += 1
                # Not an image written: a rename ends no watch, and stands in
                # for nothing it didn't already (#907).
                if not cover.rename_only:
                    written.append(holds[cover.after])
            elif wrote is _Wrote.STALE:
                stale.append(cover.target.name)
            elif wrote is _Wrote.UNKEPT:
                unkept.append(cover.target.name)
            else:
                failed.append(cover.target.name)
    # Once a copy is on disk, so it answers as its original from now on.
    for original, copy in holds.items():
        if copy != original and copy in written:
            derived.record(copy, original=original)
    return ArtworkOutcome(
        changed=changed,
        stale=tuple(stale),
        unkept=tuple(unkept),
        failed=tuple(failed),
        written=tuple(dict.fromkeys(written)),
    )


# No `update_artwork(album_dir, release, ...)` that decides and applies in one
# call. It was the artwork action's entry point until #469, and would now have
# no production caller: the action applies the plan its page was DRAWN from,
# checked by fingerprint, and a convenience that re-decides behind the page's
# back is exactly the parallel decision that change removed.


@dataclass(frozen=True)
class TaggingOutcome:
    """What one tag-and-artwork action did, in its two halves (#473, #481).

    Separate on purpose: they are separately scoped, separately reported and
    separately undoable, and an action that tagged an album perfectly while its
    artwork was refused has to be able to say so.
    """

    files: int = 0
    #: Whether any file's tags were written. A tagging against the release the
    #: files already carry writes none, and its History entry says so (#639)
    #: rather than a bare "Tagged" that reads as a record gone missing.
    tags_changed: bool = False
    artwork: ArtworkOutcome = ArtworkOutcome()
    #: The artwork was left alone because the plan no longer matched the
    #: fingerprint a page carried back (#469).
    artwork_withheld: bool = False
    #: No artwork was asked for — the caller excluded it (#482).
    #:
    #: A DIFFERENT fact from `artwork_withheld`, and kept apart because only one
    #: of them is a warning. "You asked for this and the preview had gone stale"
    #: is something the user needs told; "you unticked the box" is what they
    #: just did, and reporting it back as a reason would be noise at best and,
    #: worded like the other, a lie.
    artwork_excluded: bool = False
    #: Artwork preparation failed; tags stand and every image was left alone (#511).
    artwork_unavailable: bool = False


def tag_and_artwork(
    album_dir: Path,
    release: Release,
    cover_path: Path | None = None,
    *,
    incomplete: bool = False,
    overwrite_art: bool = False,
    files: list[Path] | None = None,
    archive: cover_art.Front | None = None,
    expected_artwork: str | None = None,
    scope: artwork.Scope | None = None,
    artwork_included: bool = True,
    chosen: artwork.Source | None = None,
    choices: artwork.Choices | None = None,
    picks: Mapping[str, cover_art.Front] | None = None,
    assignment: dict[Path, int] | None = None,
    folder_cover: artwork.FolderCoverPolicy = artwork.FolderCoverPolicy.IF_MISSING,
    tagging: TaggingChoices = NO_CHOICES,
) -> TaggingOutcome:
    """Tag the album, then write the artwork its plan calls for (#481).

    The composition every entry point uses — exact-match auto-tagging, a
    re-download, Re-tag from MB, confirmation — so the order lives in one place
    rather than in each caller.

    **Tags first, and validated first.** `tag_album` raises `TagMismatchError`
    before touching a file, so a release that does not fit these files costs
    nothing. Artwork follows, and CANNOT undo the tagging: an image that fails
    is reported in the outcome while the tags stay written (#468).

    Artwork is the ADDITIONS of the plan by default (#418): gaps filled, and a
    missing folder cover created from whichever image wins (#457, #469, #479).

    `scope` widens that, and is a DIFFERENT question from `overwrite_art`
    (#482). Scope selects how much of the plan this writes — `scoped` is a pure
    filter over the changes already decided — so `Scope.ALL` applies the plan
    the page drew, replacements included, and writes nothing the preview did not
    show. `overwrite_art` changes the PLAN instead: it discards the size rule,
    plans the folder cover onto every target, yields an empty plan on an album
    that has no folder cover at all, and is the only thing that flattens a
    compilation's per-track sleeves. Conflating them would let a checkbox about
    the preview authorise an operation the preview never described.

    Defaults to the old derivation, so every caller that does not ask keeps
    exactly the behaviour it had.

    `artwork_included=False` removes the artwork half entirely — a third
    setting, not an empty scope, because "none of the plan's changes" is not a
    subset of a plan but the absence of the step. It is what the album page's
    artwork checkbox posts when unticked (#482), and it is deliberately NOT
    spelled as a withheld fingerprint: withholding means "you asked for this and
    the preview had gone stale", which is a warning the user is owed, while this
    means "you did not ask", which is not a finding about anything. The outcome
    keeps them apart for the same reason.

    `expected_artwork` is the fingerprint of the artwork a page showed the user,
    taken at this same scope. When the plan built here no longer matches it — an
    image edited since, a candidate that arrived after the page was drawn — the
    album is tagged and NO artwork is written, and the album's History says so.
    Writing an image the page never showed would be the confident surprise #457
    was.
    """
    paths = files if files is not None else album_files.audio_files(album_dir)
    tagged, wrote_something = _tag_files(
        album_dir,
        release,
        incomplete=incomplete,
        files=paths,
        assignment=assignment,
        tagging=tagging,
    )

    # EXCLUDED means there is no artwork half at all (#482) — not an empty
    # scope, because "none of them" is not a subset of a plan, it is the absence
    # of the step. Nothing is decided and nothing is read: `decide_artwork`
    # opens every file to describe its embedded image, and a caller who has said
    # they do not want artwork should not pay for that (#44, #74) — the same
    # saving `artwork_in_scope=False` buys `tag_album`.
    #
    # Reported as its own fact rather than as a withheld plan, which is a
    # warning about a stale preview and would be untrue here.
    if not artwork_included:
        return TaggingOutcome(files=tagged, tags_changed=wrote_something, artwork_excluded=True)

    # Tags are already validated and written. Attribute any artwork failure to
    # the album after that write, and let the caller finish its bookkeeping.
    album_id = sidecar_mod.album_id_for(album_dir)
    label = album_label(release, album_dir)
    # `chosen` — and `choices`, the same made row by row (#659) — rides along
    # with the fingerprint, never instead of it (#488): the
    # plan is rebuilt here, so a choice the page made must be made again or the
    # rebuilt plan is a different one — matching its own fingerprint, and writing
    # the image the size rule prefers over the image the user picked.
    try:
        archive = archive if archive is not None else _archive_candidate(release)
        art = decide_artwork(
            album_dir,
            paths,
            cover_path,
            archive=archive,
            overwrite_art=overwrite_art,
            chosen=chosen,
            choices=choices,
            picks=picks,
            folder_cover=folder_cover,
        )
    except OSError:
        # An unreadable image is not an absent one. Refuse the entire artwork
        # half rather than building a plan from incomplete inputs (#511).
        log.exception(
            "could not prepare artwork — tagged without writing any artwork. "
            "Check file permissions and retry.",
            extra={"album_id": album_id, "album_label": label},
        )
        return TaggingOutcome(files=tagged, tags_changed=wrote_something, artwork_unavailable=True)
    if scope is None:
        scope = artwork.Scope.ALL if overwrite_art else artwork.Scope.ADDITIONS
    withheld = expected_artwork is not None and art.fingerprint(scope) != expected_artwork

    if withheld:
        # Attributed, so it reaches the album's History and the feed: the user
        # pressed a button having looked at a preview, and is owed the reason
        # the artwork half of it did not happen.
        log.warning(
            "the album's artwork changed after the page showed it — tagged without "
            "writing any artwork. Review the Artwork section and apply it from there.",
            extra={"album_id": album_id, "album_label": label},
        )
        return TaggingOutcome(files=tagged, tags_changed=wrote_something, artwork_withheld=True)

    # Kept per-track artwork is not announced (#670). Nothing overwrites it, so
    # keeping it is the promise kept rather than a finding; the album's Artwork
    # section shows the sleeves, and a warning on every tagging that wrote
    # (#260, #272) named a "replace artwork" remedy no page offers.
    try:
        outcome = apply_artwork(
            album_dir,
            art,
            files=paths,
            cover_path=cover_path,
            archive=archive,
            picks=picks,
            scope=scope,
        )
    except ArtworkChangedError:
        # The image moved between deciding and writing. The TAGS stand: losing
        # a correct tagging to an artwork race would be the inversion #458
        # settled for an unreachable archive.
        log.warning(
            "the winning artwork for %s changed while it was being tagged — "
            "tagged without writing any",
            album_dir.name,
            exc_info=True,
            extra={"album_id": album_id, "album_label": label},
        )
        return TaggingOutcome(files=tagged, tags_changed=wrote_something, artwork_withheld=True)

    if outcome.unkept:
        # #470: the replacement did not happen, and the reason is the user's to
        # know. Attributed, so it reaches the album's History — this runs
        # unattended, and the outcome the web layer renders is only seen by
        # someone who happened to be watching when it ran.
        log.warning(
            "left the artwork on %d file%s in place: %s current image could not "
            "be kept, and replacing it would have left no way back",
            len(outcome.unkept),
            "" if len(outcome.unkept) == 1 else "s",
            "its" if len(outcome.unkept) == 1 else "their",
            extra={"album_id": album_id, "album_label": label},
        )
    return TaggingOutcome(files=tagged, tags_changed=wrote_something, artwork=outcome)


@dataclass(frozen=True)
class ArtworkRestoreOutcome:
    """What an artwork Undo actually did (#471) — not what it was asked to."""

    #: Images put back where the change had replaced one.
    restored: int = 0
    #: Images taken back off where the change had added one.
    removed: int = 0
    #: Files left alone because they no longer carry what the change left —
    #: changed since, by a later action or by hand. Named, so the user can look.
    stale: tuple[str, ...] = ()

    @property
    def changed(self) -> int:
        return self.restored + self.removed


def restore_artwork(
    album_dir: Path,
    plan: Sequence[tag_history.ArtworkRevert],
    *,
    paths: Sequence[Path] | None = None,
) -> ArtworkRestoreOutcome:
    """Undo one artwork change: put back what it replaced, and take back off
    what it added (#131, #471).

    `plan` comes from `tag_history.artwork_revert_plan` — the same records the
    History row described, so the button undoes what the user just read.
    Restoring by digest rather than "the album's old cover" is what makes a
    compilation's per-track art come back to the right tracks.

    **Per file, against what the change left.** A file still carrying the
    change's `after` is undone. One carrying anything else has been changed
    since — a later action, an image put there by hand — and is left alone and
    reported, the way `revert_tags` leaves a field: an undo that reached past
    this change into someone else's would be the confident lie the design
    forbids. A file already back where it started is skipped, so running this
    twice changes nothing.

    **An addition goes back to absence.** Its `before` is None: the embedded
    image is taken off (`formats.remove_cover`), and a folder cover the change
    created is deleted. Nothing is needed from the store for that.

    **A rename goes back to the old name** (#907): a folder cover renamed to
    match its image is renamed back, with the image it replaced put back too
    if it replaced one. Not onto a file that has taken the old name since.

    **Everything resolves before anything is written**: every file is found,
    every image to put back is read from the store, and every image about to be
    destroyed is kept — or `ArtworkUnavailableError` is raised and nothing is
    written. An undo is itself a destructive write, and must be as undoable as
    the thing it undoes (#470). The existing safeguards against a record that
    two files could answer to still apply.

    `paths` is every directory the album occupies, for the same reason
    `revert_tags` takes them (#423): a track on a second disc is addressed by
    the album's own file list rather than by a join onto the primary directory.
    """
    files = album_files.for_paths(paths if paths is not None else [album_dir])
    naming = album_files.Naming(album_dir, files)
    #: (target, the image to put back or None to take it off, what it holds
    #: now, and the name a renamed folder cover goes back to)
    actions: list[tuple[Path, bytes | None, str, Path | None]] = []
    stale: list[str] = []
    for item in plan:
        # A record can name the folder cover as well as a track (#410), and that
        # is not one of the album's audio files — so a name the album's own list
        # doesn't answer falls back to a guarded join onto the primary directory,
        # where the cover lives. `_file_named` refuses an ambiguous one first, so
        # the fallback can never be reached by a name two tracks share.
        path = (
            _file_named(naming, item.file, ArtworkUnavailableError)
            if naming.matches(item.file)
            else _resolve_in_album(album_dir, item.file, ArtworkUnavailableError)
        )
        if formats.is_supported(path) and not path.exists():
            raise ArtworkUnavailableError(f"{item.file} is no longer in this album")
        # A folder cover the change renamed goes back to its old name (#907).
        # A rename that kept its image puts back nothing else, so nothing is
        # read from the store for it.
        old = (
            _resolve_in_album(album_dir, item.renamed_from, ArtworkUnavailableError)
            if item.renamed_from is not None
            else None
        )
        data: bytes | None = None
        if item.before is not None and not (old is not None and item.before == item.after):
            stored = artwork_store.path_for(item.before)
            if stored is None:
                raise ArtworkUnavailableError(
                    f"the image {item.file} used to carry is no longer kept "
                    "(the artwork store evicted it, or never held it)"
                )
            try:
                data = stored.read_bytes()
            except OSError as e:
                raise ArtworkUnavailableError(
                    f"could not read the kept image for {item.file}: {e}"
                ) from e
        # A folder cover that is not there reads as None — which is exactly
        # what a created cover is undone TO.
        current = _image_at(path)
        now = images.digest(current[0]) if current is not None else None
        if old is not None:
            was = _image_at(old)
            if now is None and was is not None and images.digest(was[0]) == item.before:
                continue  # already back where it started
            # Never onto a cover that has taken the old name since: it's
            # somebody's change, and putting this one back would destroy it.
            if now != item.after or old.exists():
                stale.append(naming.name_of(path))
                continue
            actions.append((path, data, item.after, old))
            continue
        if now == item.before:
            continue  # already back where it started
        if now != item.after:
            stale.append(naming.name_of(path))
            continue
        actions.append((path, data, item.after, None))

    # Keep what is about to be destroyed, all of it BEFORE anything is written —
    # and if any of it cannot be kept, nothing is written at all (#470).
    # Refusing the whole undo rather than doing the rest is this function's own
    # rule: a partial restore is a state the album never had. Renaming a cover
    # back destroys nothing unless it puts an image back as well.
    doomed = [
        (path, after) for path, data, after, old in actions if old is None or data is not None
    ]
    kept = _keep_doomed_art(dict(doomed))
    if unkept := [path for path, after in doomed if after not in kept]:
        raise ArtworkUnavailableError(
            f"the image {naming.name_of(unkept[0])} carries now could not be kept, "
            "and undoing would destroy it with no way back"
        )

    # The id this album answers to, for the same reason `apply_artwork` takes
    # one (#260, #493): a record with no album id is one the feed's mirror drops
    # and the album's own History can never find — so the single record that an
    # Undo happened was invisible exactly where someone would look for it.
    # Read here rather than after the loop because a restore writes images and
    # touches neither tags nor the sidecar, so the album's identity cannot move
    # underneath it.
    album_id = sidecar_mod.album_id_for(album_dir) or id_registry.peek(album_dir)

    restored = removed = 0
    for path, data, after, old in actions:
        if old is not None:
            # A bare rename moves the file. One that puts an image back too
            # writes the old name first and then removes the new, as the
            # replacement it undoes did the other way round.
            if data is None:
                os.replace(path, old)
            else:
                _write_image_at(old, data)
                path.unlink()
            restored += 1
            audit.record(
                "artwork.restore",
                album_id=album_id,
                album=album_dir,
                file=old.name,
                digest=images.digest(data) if data is not None else after,
                removed=False,
            )
            audit.record(
                "cover.rename", album_id=album_id, album=album_dir, file=old.name, was=path.name
            )
            continue
        if data is not None:
            _write_image_at(path, data)
            restored += 1
        else:
            _remove_image_at(path)
            removed += 1
        audit.record(
            "artwork.restore",
            album_id=album_id,
            album=album_dir,
            file=naming.name_of(path),
            digest=images.digest(data) if data is not None else "-",
            removed=data is None,
        )
    return ArtworkRestoreOutcome(restored=restored, removed=removed, stale=tuple(stale))


def _remove_image_at(path: Path) -> None:
    """Take an added image back off: out of a track's tags, or the folder cover
    file itself, which the change created (#471)."""
    if formats.is_supported(path):
        formats.remove_cover(path)
        return
    path.unlink()


def _image_at(path: Path) -> tuple[bytes, str] | None:
    """The image `path` currently holds, whether it is a track or the folder
    cover (#410).

    A restore target is no longer always an audio file: since the album's own
    image can be promoted to `cover.jpg`, that write is undoable too, and the
    stored record names `cover.jpg` exactly as it names a track. The file IS the
    image there, rather than carrying one.
    """
    if formats.is_supported(path):
        return formats.read_cover(path)
    try:
        return path.read_bytes(), _mime_for(path)
    except OSError:
        return None


def _write_image_at(path: Path, data: bytes) -> None:
    """Put `data` back, into a track's tags or over the folder cover itself.

    The folder file is replaced through a temp file and a rename, like every
    other write Harmonist makes to the user's directory: a crash mid-restore
    must not leave a half-image where a cover was.
    """
    if formats.is_supported(path):
        formats.write_cover(path, data)
        return
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def tagsets_for(release: Release, tagging: TaggingChoices) -> list[TagSet]:
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
        _build_tagset(release, medium, pos, track, media_total, tagging)
        for medium, pos, track in _flatten_tracks(release)
    ]


def _build_tagset(
    release: Release,
    medium: dict[str, Any],
    track_pos: int,
    track: Track,
    media_total: int,
    tagging: TaggingChoices,
) -> TagSet:
    """Translate one MB track within a release to a TagSet.

    `tagging` is the user's spelling settings (#544) and has NO DEFAULT on
    purpose, for the reason `web/main.py`'s folder-cover seam has none: a call
    site that forgot it would write MusicBrainz's spelling while the album page
    showed the user's, and nothing in the suite would look wrong. Callers with
    no business knowing the settings — `track_assignment`, which reads these
    tagsets for track titles alone — pass `TaggingChoices()` and say so.
    """
    track_artist_credit = track.get("artist-credit") or release.get("artist-credit")
    labels, catalog_numbers = _label_info(release.get("label-info-list") or [])
    rg = release.get("release-group") or {}

    track_total = mb_tracks.track_total(medium)
    disc_num = _disc_num(medium)
    # None, not empty, when the release could not answer: a TagSet then keeps
    # the file's credits (`formats.settled`) instead of removing them (#680).
    works: list[str] | None = None
    work_ids: list[str] | None = None
    composers: list[str] | None = None
    composer_sort: list[str] | None = None
    composer_ids: list[str] | None = None
    lyricists: list[str] | None = None
    writers: list[str] | None = None
    if credits_mod.relationships_answered(release):
        found = credits_mod.track_credits(release, track, tagging)
        works = [w.title for w in found.works]
        work_ids = [w.mbid for w in found.works]
        composers = [p.name for p in found.composers]
        composer_sort = [p.sort_name for p in found.composers]
        composer_ids = [p.mbid for p in found.composers]
        lyricists = [p.name for p in found.lyricists]
        writers = [p.name for p in found.writers]

    return TagSet(
        mb_album_id=release["id"],
        album=transforms_mod.album_title(release, tagging.transforms, tagging.library_releases),
        album_artist=_artist_phrase(release.get("artist-credit"), tagging),
        title=_track_title(track),
        artist=_artist_phrase(track_artist_credit, tagging),
        track_num=track_pos,
        track_total=track_total,
        album_artist_sort=_artist_sort_phrase(release.get("artist-credit"), tagging) or None,
        artist_sort=_artist_sort_phrase(track_artist_credit, tagging) or None,
        # The release credit unjoined, so a two-artist collaboration files under
        # both names instead of under one composite pseudo-artist (#322). Bare
        # names by construction — `_artist_names` drops the join phrases, which
        # is the guess this tag exists to remove.
        album_artists=_artist_names(release.get("artist-credit"), tagging),
        artists=_artist_names(track_artist_credit, tagging),
        original_date=rg.get("first-release-date") or None,
        script=(release.get("text-representation") or {}).get("script") or None,
        mb_album_artist_ids=_artist_ids(release.get("artist-credit")),
        mb_release_group_id=rg.get("id"),
        # Picard lower-cases BOTH of these — "album", not "Album"; "official",
        # not "Official" — so Harmonist does too, or an adopted library differs
        # from us on a field forever (#290). It differed on the type for exactly
        # that reason: the status was normalised here and the type was not, and
        # the gap is invisible because both fields look right in isolation.
        #
        # Cost of getting it wrong is not one bad row: `mb_album_type` is
        # Identity under the significance map (docs/design/tagging.md), so the
        # gardener would route ~90% of an adopted library to the Inbox on its
        # first night over a capital letter — #283's failure mode on a second
        # field. Any future field taken from a MusicBrainz vocabulary belongs in
        # this pair, not beside it.
        mb_album_type=_release_types(rg),
        mb_album_status=(release.get("status") or "").lower() or None,
        mb_album_country=release.get("country"),
        compilation=_compilation_flag(release.get("artist-credit")),
        mb_track_id=(track.get("recording") or {}).get("id"),
        mb_release_track_id=track.get("id"),
        mb_artist_ids=_artist_ids(track_artist_credit),
        isrcs=_isrcs(track),
        date=release.get("date") or None,
        disc_num=disc_num,
        disc_total=media_total,
        label=labels,
        catalog_number=catalog_numbers,
        barcode=release.get("barcode") or None,
        asin=release.get("asin") or None,
        media=medium.get("format") or None,
        # The medium's own name, from the same dict `media` comes from. Picard
        # writes this as `discsubtitle`, so a Picard-tagged library already
        # carries it — and until #218 a Harmonist re-tag silently removed it.
        disc_subtitle=medium.get("title") or None,
        works=works,
        mb_work_ids=work_ids,
        composers=composers,
        composer_sort=composer_sort,
        mb_composer_ids=composer_ids,
        lyricists=lyricists,
        writers=writers,
    )


def _videos_beside(album_dir: Path, files: Sequence[Path]) -> list[Path]:
    """The video files in the directories the album's audio is in.

    The set the scan counts towards completeness (#193): an album directory is
    one holding audio, so these are every video the album has. Read to place
    them, never written (#66) — they are kept out of the file list the tagger
    writes, and this is the only way it learns of them.
    """
    return album_files.videos_for_paths(sorted({f.parent for f in files}) or [album_dir])


def _assign_files_to_tracks(
    files: list[Path],
    flat_tracks: list[_FlatTrack],
    identities: Sequence[compare.TrackIdentity],
    *,
    videos: Sequence[Path] = (),
) -> tuple[list[tuple[Path, _FlatTrack]], frozenset[int]]:
    """Which MusicBrainz track each file is, for an album missing some of them,
    and which tracks (by index) the `videos` are.

    Through `compare.assign` — the same ladder the album page uses, so a file
    cannot be one track in the tracklist and a different one in the tagger
    (#232). Release track id, then disc-and-track number, then file order.

    The videos are placed in the same pass, as the page's tracklist places them,
    and for the same reason: a track a video is can't also be an audio file's
    (#1056). Placing the audio alone dealt the next free track in file order to
    whichever audio file was left, and on a disc that opens with its concert
    film that track is the film. Then the audio and video are dealt in track
    order together, as the page has them (`_in_track_order`); without a video
    the order is the one given, as it always was.

    This used to assign by **length similarity**, which is why it is worth
    saying what changed: on *TISM — The White Albun* that rule bound
    `Sorted for D 'n M.m4a` to a DVD track called *Diatribe*, one file in
    sixteen, and would have written that track's title and ids into it. The
    file's own tags named its real slot the whole time and were never read.
    Fifteen right out of sixteen is the worst available outcome — nobody
    re-checks an album that looks mostly correct.

    Reuses the identities read for the confirmed-release guard.
    """
    entries = [
        *zip(files, identities, strict=True),
        *((v, compare.identity_of(formats.read_video_tags(v))) for v in videos),
    ]
    order = (
        sorted(range(len(entries)), key=lambda i: album_files.sort_key(entries[i][0]))
        if videos
        else range(len(entries))
    )
    dealt = compare.assign([entries[i][1] for i in order], [_identity_of(t) for t in flat_tracks])
    slots: list[int | None] = [None] * len(entries)
    for i, slot in zip(order, dealt, strict=True):
        slots[i] = slot
    # A file with no slot is not tagged at all. It can only happen with more
    # files than tracks, which the caller refuses — but leaving one alone beats
    # writing another track's metadata into it.
    pairs = [
        (f, flat_tracks[s])
        for f, s in zip(files, slots[: len(files)], strict=True)
        if s is not None
    ]
    return pairs, frozenset(s for s in slots[len(files) :] if s is not None)


def _identity_of(flat: _FlatTrack) -> compare.TrackIdentity:
    """One MusicBrainz track's identity, exactly as tagging would write it —
    `_build_tagset` derives the same disc from the medium and the same track
    number from the position."""
    medium, track_pos, track = flat
    return compare.TrackIdentity(track.get("id"), _disc_num(medium), track_pos)


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
    except TypeError, ValueError:
        return 1


def _flatten_tracks(release: Release) -> Iterator[_FlatTrack]:
    """Yield (medium, track_pos_in_medium, track) for every track in every medium."""
    for medium in release.get("medium-list", []):
        # Minimal payloads may omit positions; retain their conventional order.
        # Supplied positions are authoritative, including the pregap's zero.
        start = 0 if medium.get("pregap") else 1
        for number, track in enumerate(mb_tracks.tracks_of(medium), start):
            yield medium, int(track.get("position", number)), track


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


def _credits_of(release: Release) -> Iterator[list[Any]]:
    """Every artist credit in a release: the release's own, then each track's.

    A track's credit is where a featured artist actually lives — MusicBrainz's
    style moves the guest out of the track title and into the credit — so a
    walker that stops at the release sees only the album artist. `mbid_names`
    did exactly that, which is why on *A Fragile Geography* the second credited
    artist's id had no name available anywhere on the page (#309).
    """
    if release_credit := release.get("artist-credit"):
        yield release_credit
    for _, _, track in _flatten_tracks(release):
        if track_credit := track.get("artist-credit"):
            yield track_credit


def mbid_names(release: Release) -> dict[str, str]:
    """The human name behind each MusicBrainz id `tagsets_for` writes, by id.

    For the album page (#298), which otherwise renders `mb_album_artist_ids`,
    `mb_release_group_id` and — since #309 gave them a column — the per-track
    id fields as raw hex: a wall of characters carrying nothing a reader can act
    on, in a panel whose whole job is to be scannable.

    Lives here, beside `_artist_ids` and `_build_tagset`, for the same reason
    `tagsets_for` does: the id and the name have to come out of the same corner
    of the same payload. Read the name from somewhere else and a row can show
    one artist's name over another artist's id, which is worse than the hex.

    **Only the ids MusicBrainz has just told us about.** An id the files carry
    that this release has moved away from is not in here and cannot be — we know
    the hex and nothing else about it. The caller falls back to showing it raw,
    which is what keeps a differing row from rendering two identical names.

    **Recordings and release tracks are deliberately absent.** The name of a
    recording is the track's title, which the tracklist's Title column already
    carries one cell to the left — so captioning a moved recording id with it
    would put the same words on both sides of a row whose whole content is that
    something changed. The raw hex, linked, states that honestly.

    The ARTIST name here is the artist's own, not the credited-as name: this
    table answers "which entity is this id", where `artist_credits` below
    answers "how is this release spelling it". They differ on a release that
    credits Prince as ✧, and each row wants the one it asks for.
    """
    names: dict[str, str] = {}
    for credit in _credits_of(release):
        for entry in _credit_entries(credit):
            artist = entry.get("artist") or {}
            if (artist_id := artist.get("id")) and (name := artist.get("name")):
                names[artist_id] = name
    rg = release.get("release-group") or {}
    if (rg_id := rg.get("id")) and (rg_title := rg.get("title")):
        names[rg_id] = rg_title
    # Works are named, unlike recordings: the Work column is not pinned beside
    # the ids the way Title is, so its title is not already on the row (#680).
    # A composer id is named by the artist's own name, as an artist id is, which
    # is the name "standardize always" spells every credit with.
    own_names = TaggingChoices(standardize_artist_names=transforms_mod.ArtistNames.ALL)
    for _, _, track in _flatten_tracks(release):
        found = credits_mod.track_credits(release, track, own_names)
        for work in found.works:
            if work.title:
                names[work.mbid] = work.title
        for person in found.composers:
            names.setdefault(person.mbid, person.name)
    return names


def change_names(release: Release) -> dict[str, str]:
    """What to call each id in a list of tag changes (#801), by id.

    `mbid_names`, plus the release itself and every recording and release
    track. A change list has no Title column beside it, which is the reason
    `mbid_names` leaves recordings out, so here a moved recording id would
    otherwise be bare hex; the track's title is what tells a reader which one.
    Built on `mbid_names` so an artist is named exactly as the album page names
    it, from the same credits.

    Only what this release carries, as `mbid_names` says of itself: an id it has
    moved away from is not here, and the change list shows it shortened.
    """
    names = mbid_names(release)
    if (release_id := release.get("id")) and (title := release.get("title")):
        disambiguation = release.get("disambiguation")
        names[release_id] = f"{title} ({disambiguation})" if disambiguation else title
    for _, _, track in _flatten_tracks(release):
        track_title = _track_title(track)
        recording = track.get("recording") or {}
        if track_id := track.get("id"):
            names[track_id] = track_title
        if recording_id := recording.get("id"):
            names[recording_id] = recording.get("title") or track_title
    return names


class ReleaseEvent(NamedTuple):
    """One (where, when) pair MusicBrainz records for a release (#329).

    A release is issued in one country on one date only in the simple case. MB
    models the general one as a LIST of release events, and collapses it to the
    scalar `country` / `date` that Picard writes and Harmonist follows — so an
    album issued in Germany, the UK and the US carries "DE" in its tags and the
    other two are reachable only from `release-event-list`.

    That is correct as a tag and wrong as an answer to "where did this come
    out", which is what the album page's Country row was read as. This is the
    rest of the answer, for the page alone: nothing here is ever written.

    Lives beside `mbid_names` and `_build_tagset` for the reason those do — the
    value the tag carries and the fuller picture that explains it have to be
    read out of one payload, or the page annotates DE with somebody else's
    release events.
    """

    #: ISO 3166-1 alpha-2, or None for an event with no area — how MusicBrainz
    #: spells "worldwide". Kept rather than dropped, because the event's DATE is
    #: half of what this list explains.
    country: str | None
    #: The area's name — "Germany" — or None alongside a None `country`.
    area: str | None
    date: str | None
    #: Whether this is the event the tags come from. Read off the release's own
    #: `country` rather than assumed to be the first, because MusicBrainz picks
    #: it and `_build_tagset` follows; guessing here would let the page mark one
    #: row as written while the tagger wrote another. False on every event when
    #: the two cannot be reconciled — a list with no mark beats a wrong mark.
    written: bool


def release_countries(release: Release) -> frozenset[str]:
    """Every country this release names, as a set (#346).

    `release_events` with the order and the dates thrown away, which is what
    both callers of *this* want: the album page and `plan_album` ask only
    whether the code on a file is one the release was issued in. One derivation
    rather than the same comprehension in three places — the page and the plan
    disagreeing about which countries are acceptable is precisely the bug this
    is here to prevent.
    """
    return frozenset(e.country for e in release_events(release) if e.country)


def release_events(release: Release) -> tuple[ReleaseEvent, ...]:
    """Every release event MusicBrainz records, in its own order (#329).

    MusicBrainz's order is chronological and is the order `country` was picked
    from, so it is preserved rather than sorted — Picard sorts its
    `~releasecountries` because it only ever asks that list for membership.

    No include needed: `release-event-list` comes back with any release lookup,
    so this reads a corner of the payload the page already holds.
    """
    events: list[ReleaseEvent] = []
    tagged = (release.get("country") or "").strip() or None
    claimed = False
    for event in release.get("release-event-list") or []:
        area = event.get("area") or {}
        codes = area.get("iso-3166-1-code-list") or []
        country = (codes[0] if codes else None) or None
        written = not claimed and country is not None and country == tagged
        claimed = claimed or written
        events.append(
            ReleaseEvent(
                country=country,
                area=(area.get("name") or None) if country else None,
                date=event.get("date") or None,
                written=written,
            )
        )
    return tuple(events)


class CreditPart(NamedTuple):
    """One artist within an artist credit, as the page renders it.

    `name` is the artist as the phrase spells them — credited, or standardized
    under the user's setting (#678) — so the parts spell the phrase exactly;
    `join` is the text that runs from this artist to the next — " feat. ", " & ",
    ", " — and is empty on the last. `mbid` is None for an artist MusicBrainz
    names without identifying, which renders as plain text rather than a link.

    A relationship credit is a part too (#863): a composer, lyricist or writer,
    or the work itself, whose `entity` is then "work". `sort` is the person's
    sort name, shown on the link: for a composer it is what the `composersort`
    tag carries, which the Composer column states. An artist credit has none.
    """

    name: str
    mbid: str | None
    join: str
    entity: str = "artist"
    sort: str | None = None


def _credit_entries(artist_credit: list[Any] | None) -> Iterator[dict[str, Any]]:
    """The artist dicts of one credit, in order.

    musicbrainzngs emits each join phrase as a **bare string element** between
    the artist dicts (`[{...}, ' & ', {...}]`), not as a `joinphrase` key on the
    dict — the key is the JSON web service's shape, which Picard consumes but we
    never see. Every walker over an artist-credit must handle the string
    elements or it will silently concatenate the artists with no separator
    (#183), so they are skipped in exactly one place: here.
    """
    for entry in artist_credit or []:
        if isinstance(entry, dict):
            yield entry


def _credit_parts(
    artist_credit: list[Any] | None, tagging: TaggingChoices = NO_CHOICES
) -> tuple[CreditPart, ...]:
    """One artist credit as its parts — the structured form of `_artist_phrase`.

    The two cannot drift, because `_artist_phrase` is built from this. That
    matters more than it looks: the page only ever applies a credit to a value
    that IS the phrase this produces, so the moment the parts stopped spelling
    the phrase, the page would replace a user's tag with different words and
    call it the same value.

    Both spellings of the join phrase are handled — the bare string element
    musicbrainzngs actually emits, and the `joinphrase` key the JSON service
    uses — because the walker they replaced handled both and dropping either
    silently loses a separator.
    """
    parts: list[CreditPart] = []
    for entry in artist_credit or []:
        if isinstance(entry, str):
            # A join phrase, and it belongs to the artist BEFORE it. A credit
            # that OPENS with one has no artist to attach it to, so it becomes a
            # nameless part carrying only the text: nothing to link, and the
            # phrase still reproduces character for character. That case does not
            # arise in musicbrainzngs' output, and it is handled anyway because
            # this feeds `_artist_phrase`, which writes tags — a payload shape
            # that made the two disagree would change what lands in a file.
            if parts:
                parts[-1] = parts[-1]._replace(join=parts[-1].join + entry)
            else:
                parts.append(CreditPart("", None, entry))
        elif isinstance(entry, dict):
            artist = entry.get("artist") or {}
            parts.append(
                CreditPart(
                    transforms_mod.artist_spelling(entry, tagging).name,
                    artist.get("id"),
                    entry.get("joinphrase") or "",
                )
            )
    return tuple(parts)


def artist_credits(release: Release) -> dict[str, tuple[CreditPart, ...]]:
    """Every artist credit in `release`, keyed by the phrase it renders as.

    For the album page (#309), where a credit — "Rafael Anton Irisarri feat.
    Julia Kent" — should read as the two artists it names, each linked, joined
    by the words MusicBrainz itself uses. Picard and Harmonist both write that
    phrase into `artist` / `albumartist` as one flat string, so the file on disk
    has no structure left to recover; this is the corner of the payload where it
    still exists.

    **Keyed by the phrase, not by the field**, which is what makes applying it
    safe. A credit is only ever put on a value that is character-for-character
    the phrase these parts build, so the links are guaranteed to spell the value
    they replace. A tag that has drifted from MusicBrainz — the whole reason the
    page exists — simply misses the lookup and renders as the flat string it is,
    the same fallback `mbid_names` leans on.

    **A phrase two different credits produce is dropped, not resolved.** That is
    the design's exact-scoped-unique rule: two artists sharing a spelling is
    ambiguity, and picking the first would link one artist's name to the other's
    page. Rendering it flat loses a link; guessing states something false.

    **Every setting's phrase is keyed** (#678). A file carries whichever spelling
    the setting it was tagged under wrote — as credited, or standardized — and
    each names the same artists, so each gets the same links.

    **The relationship credits are keyed too** (#863): each track's composers,
    lyricists, writers and works, as the "; "-joined list the page reads their
    tags as. Under the same rule, so a composer list spelled exactly like an
    artist credit naming other people is drawn flat rather than linked to either.
    """
    found: dict[str, tuple[CreditPart, ...] | None] = {}

    def unsorted(parts: tuple[CreditPart, ...]) -> tuple[CreditPart, ...]:
        return tuple(p._replace(sort=None) for p in parts)

    def key(phrase: str, parts: tuple[CreditPart, ...]) -> None:
        # The same people whether or not a sort name came with them: an artist
        # credit carries none, and a composer credited as a track's artist too
        # is still one person. The parts that know the sort name are kept.
        if phrase not in found:
            found[phrase] = parts
        elif (known := found[phrase]) is not None and unsorted(known) != unsorted(parts):
            found[phrase] = None
        elif known is not None and any(p.sort for p in parts):
            found[phrase] = parts

    answered = credits_mod.relationships_answered(release)
    for names in transforms_mod.ArtistNames:
        spelled = TaggingChoices(standardize_artist_names=names)
        for credit in _credits_of(release):
            if parts := _credit_parts(credit, spelled):
                key(_artist_phrase(credit, spelled), parts)
        if not answered:
            continue
        for _, _, track in _flatten_tracks(release):
            for parts in _relationship_parts(credits_mod.track_credits(release, track, spelled)):
                key("; ".join(p.name for p in parts), parts)
    return {phrase: parts for phrase, parts in found.items() if parts}


def _relationship_parts(credits: credits_mod.TrackCredits) -> Iterator[tuple[CreditPart, ...]]:
    """One track's relationship credits as parts, a tuple per tag (#863)."""

    def joined(parts: list[CreditPart]) -> tuple[CreditPart, ...]:
        return tuple(
            p._replace(join="; " if i < len(parts) - 1 else "") for i, p in enumerate(parts)
        )

    for people in (credits.composers, credits.lyricists, credits.writers):
        if people:
            yield joined([CreditPart(p.name, p.mbid, "", sort=p.sort_name) for p in people])
    if credits.works:
        yield joined([CreditPart(w.title, w.mbid, "", entity="work") for w in credits.works])


#: MusicBrainz's special **Various Artists** artist. A real MBID like any other,
#: and the whole of the compilation rule (#323).
#:
#: The test is an id comparison because the id is what MusicBrainz gives us:
#: Harmonist already writes this exact string into `mb_album_artist_ids`, so
#: matching on the *string* "Various Artists" would be guessing at an identity
#: that is available exactly — the thing review-gate item 2 forbids. It would
#: also be wrong in both directions: a real band could be called that, and a
#: release credited to VA in another language would be missed.
VARIOUS_ARTISTS_ID = "89ad4ac3-39f7-470e-963a-56509c546377"


def _compilation_flag(artist_credit: list[Any] | None) -> bool | None:
    """True when this release is credited to Various Artists, else None (#323).

    None rather than False because the tag is written only when set — see
    `owned.as_flag`, and `formats.types.TagSet.compilation`.

    Deliberately NOT the release group's `compilation` secondary type. That
    describes the release group's nature, so a greatest-hits album by one artist
    carries it — and flagging one of those is precisely what makes a player
    shatter it into one album per track artist, the failure this tag exists to
    prevent (docs/design/tagging.md).
    """
    return True if VARIOUS_ARTISTS_ID in _artist_ids(artist_credit) else None


def _release_types(rg: Release) -> list[str]:
    """The release group's type, as Picard writes it: primary then secondaries.

    ONE multi-value tag (`picard/mbjson.py`, `release_group_to_metadata`):

        m['releasetype'] = m.getall('~primaryreleasetype') + m.getall('~secondaryreleasetype')

    Order is Picard's — primary first, then MusicBrainz's own order for the rest
    — because the two libraries have to agree value for value or an adopted
    album differs on this field forever (#290). Lower-cased for the same reason,
    which the primary already was; the secondaries join it rather than sitting
    beside it in a second convention.

    The secondaries are what say an album is live, a remix or a soundtrack, and
    Navidrome reads this tag and has no other source for that (#331). They cost
    no extra request: `secondary-type-list` rides along with the `release-groups`
    include `RELEASE_INCLUDES` already asks for.

    Empty when the release group has no primary type, which is how the backends
    know to leave the tag off entirely — a secondary type with no primary would
    be a shape neither Picard nor a player expects.
    """
    primary = (rg.get("primary-type") or "").lower()
    if not primary:
        return []
    return [primary, *((t or "").lower() for t in rg.get("secondary-type-list") or [])]


def _label_info(entries: list[Any]) -> tuple[list[str], list[str]]:
    """Every label and catalogue number the release names, deduped (#334).

    The two collected INDEPENDENTLY, which is the whole point and is what
    Picard's `label_info_from_node` does. Reading both off `label-info[0]` lost
    two different things: every label after the first — a co-release or a
    licensed reissue names two routinely — and, less obviously, the catalogue
    number of any release whose first entry has a label and no number while a
    later one does. That second case fires on Harmonist's own output, not only
    on files adopted from Picard.

    Order is MusicBrainz's, and duplicates are dropped rather than repeated: the
    same label named on two entries is one label.
    """
    labels: list[str] = []
    catalog_numbers: list[str] = []
    for entry in entries:
        name = ((entry.get("label") or {}).get("name") or "").strip()
        if name and name not in labels:
            labels.append(name)
        number = (entry.get("catalog-number") or "").strip()
        if number and number not in catalog_numbers:
            catalog_numbers.append(number)
    return labels, catalog_numbers


def _artist_ids(artist_credit: list[Any] | None) -> list[str]:
    """Pull MBIDs out of an MB artist-credit list."""
    return [
        artist_id
        for entry in _credit_entries(artist_credit)
        if (artist_id := (entry.get("artist") or {}).get("id"))
    ]


def album_label(release: dict[str, Any], album_dir: Path) -> str:
    """The album's display name for an activity entry — "Artist — Title".

    Taken from the release being tagged rather than the sidecar, because that is
    what the files are about to say, and it is the same name the album will be
    listed under once this tagging lands. Falls back to the folder name when the
    release names neither, so an entry is never labelled with an empty string —
    the feed hides the album column entirely when the label is blank, which would
    lose the attribution this exists to add.

    PUBLIC because a tagging's outcome is not the only entry it writes (#571).
    A merge is named by its own entry, from `retag._record_merge`, and that
    one had built its label out of the folder name — so one press put two rows in
    the feed naming the same album two different ways, each row's name a link.
    The label has to come from one place if they are to agree, and this is the
    place that already had the release in hand.
    """
    label = f"{_artist_phrase(release.get('artist-credit'))} — {release.get('title') or ''}"
    return label.strip(" —") or album_dir.name


def _artist_phrase(artist_credit: list[Any] | None, tagging: TaggingChoices = NO_CHOICES) -> str:
    """Build a display string from an MB artist-credit list, each artist spelled
    the way `tagging` chooses (#678).

    Derived from `_credit_parts` rather than walking the credit a second time,
    so the flat phrase written to `artist` / `albumartist` and the linked parts
    the album page renders cannot disagree about what the credit says (#309).
    """
    parts = _credit_parts(artist_credit, tagging)
    return "".join(part.name + part.join for part in parts).strip()


def _artist_sort_phrase(
    artist_credit: list[Any] | None, tagging: TaggingChoices = NO_CHOICES
) -> str:
    """Like `_artist_phrase` but using each artist's MB **sort-name** (e.g.
    'Beatles, The'), keeping join phrases. Empty when no sort-names are present.

    A credited name kept as credited sorts by its own alias's sort name where it
    is one (#678), as Picard writes it."""
    if not artist_credit:
        return ""
    parts: list[str] = []
    any_sort = False
    for ac in artist_credit:
        if isinstance(ac, str):
            parts.append(ac)
        elif isinstance(ac, dict):
            spelling = transforms_mod.artist_spelling(ac, tagging)
            if spelling.sort_name:
                any_sort = True
            parts.append(spelling.sort_name or spelling.name)
            if jp := ac.get("joinphrase"):
                parts.append(jp)
    return "".join(parts).strip() if any_sort else ""


def _artist_names(
    artist_credit: list[Any] | None, tagging: TaggingChoices = NO_CHOICES
) -> list[str]:
    """The individual artist names (no join phrases) — Picard's multi-value
    `artists` / ARTISTS tag, standardized on its own when the user asks (#678)."""
    if not artist_credit:
        return []
    names: list[str] = []
    for ac in artist_credit:
        if isinstance(ac, dict) and (name := transforms_mod.artist_spelling(ac, tagging).list_name):
            names.append(name)
    return names
