"""Picard-compatible tagger — orchestration layer.

Builds a format-agnostic `TagSet` per track from an MB release dict and
delegates the actual atom/frame/comment serialisation to the matching
`harmonist.formats.<format>` submodule.

For backward compatibility with existing tests, the MP4 atom-name
constants are re-exported here.
"""

from __future__ import annotations

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
    audit,
    compare,
    cover_art,
    formats,
    id_registry,
    images,
    mb_lookup,
    tag_history,
)
from . import sidecar as sidecar_mod
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
from .models import Release, Track, norm_title, title_with_disambiguation

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
        archive: cover_art.Front | None = None,
        expected_artwork: str | None = None,
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
        archive: cover_art.Front | None = None,
        expected_artwork: str | None = None,
    ) -> int:
        return tag_album(
            album_dir,
            release,
            cover_path,
            incomplete=incomplete,
            overwrite_art=overwrite_art,
            files=files,
            archive=archive,
            expected_artwork=expected_artwork,
        )


def tag_album(
    album_dir: Path,
    release: Release,
    cover_path: Path | None = None,
    *,
    incomplete: bool = False,
    overwrite_art: bool = False,
    files: list[Path] | None = None,
    archive: cover_art.Front | None = None,
    expected_artwork: str | None = None,
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

    Artwork is the ADDITIONS of the album's artwork plan (#418, #469): gaps
    filled and a missing folder cover created, from whichever image the plan
    says wins. `archive` is the Cover Art Archive's image when the caller has
    just fetched it; otherwise the local cache is read.

    `expected_artwork` is the fingerprint of the additions a page showed the
    user. When the plan built here no longer matches it — an image edited
    since, a candidate that arrived after the page was drawn — the album is
    tagged and NO artwork is written, and the album's History says so. Writing
    an image the page never showed would be the confident surprise #457 was.
    """
    prep = _prepare(
        album_dir,
        release,
        cover_path,
        incomplete=incomplete,
        overwrite_art=overwrite_art,
        files=files,
        archive=archive,
        expected_artwork=expected_artwork,
    )
    # Read before the loop, and before anything can move it: tagging drops a
    # sidecar's `temp_uid` for the MBID afterwards, which is why `album_history`
    # unions an album's alias chain — the same reason the `tag.album` line below
    # gets away with the pre-write id. The artwork notice at the end of this
    # function reuses it rather than re-reading, so both records name the album
    # the same way even if the id moves in between.
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
        art="embedded" if prep.cover is not None else "preserved",
        mode="incomplete" if incomplete else "full",
    )
    if prep.art_withheld:
        # Attributed, so it reaches the album's History and the feed: the user
        # pressed a button having looked at a preview, and is owed the reason
        # the artwork half of it did not happen.
        log.warning(
            "the album's artwork changed after the page showed it — tagged without "
            "writing any artwork. Review the Artwork section and apply it from there.",
            extra={"album_id": album_id, "album_label": _album_label(release, album_dir)},
        )
    art_targets = prep.art_targets
    if prep.cover is not None and prep.art_after is not None:
        # Only this tagging's own targets: an image the plan leaves alone is not
        # being destroyed and has no business being backed up.
        doomed = {p: prep.art.before.get(p) for p in art_targets}
        kept = _keep_doomed_art(doomed, prep.art_after)
        # NO RETAINED BACKUP, NO REPLACEMENT (#470). Reachable only under
        # `overwrite_art` — the one way a tagging replaces — and it narrows the
        # artwork, never the tagging: those files still take their tags, and
        # keep the image they have.
        unkept = {p for p, key in doomed.items() if key is not None and key not in kept}
        if unkept:
            log.warning(
                "left the artwork on %d file%s in place: %s current image could not "
                "be kept, and replacing it would have left no way back",
                len(unkept),
                "" if len(unkept) == 1 else "s",
                "its" if len(unkept) == 1 else "their",
                extra={"album_id": album_id, "album_label": _album_label(release, album_dir)},
            )
            art_targets = art_targets - unkept

    # The folder cover this tagging creates, when the album has none (#457) —
    # written from the plan like every other image here, where it used to be
    # fetched into the folder before the tagging had decided anything.
    if prep.cover is not None and prep.cover_change is not None:
        _write_folder_cover(album_dir, prep.cover_change, prep.cover, prep.art.source, album_id)

    wrote_something = False
    # How this album's records name its files — built once from the files the
    # tagging is actually writing, so a disc in a sibling directory is named by
    # its disc rather than by a bare filename its sibling also answers to (#423).
    naming = album_files.Naming(album_dir, prep.files)
    for file_path, (medium, track_pos_in_medium, track) in prep.pairs:
        tagset = _build_tagset(release, medium, track_pos_in_medium, track, prep.media_total)
        before = formats.read_owned(file_path)
        # None for a file that already carries an image: `write_tags` then
        # leaves its art alone, and `_changes_for` records no artwork change for
        # it, because none happens (#418).
        incoming = prep.cover if file_path in art_targets else None
        changes = _changes_for(
            tagset,
            before,
            file_path,
            prep.art.before,
            prep.art_after if incoming is not None else None,
            prep.accepted_album_title,
            prep.accepted_countries,
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
        formats.write_tags(file_path, tagset, incoming)
        wrote_something = True
        # The `tag.track` line comes AFTER the write, and the detail hangs off
        # it: a record claiming a change that never landed would make a future
        # revert restore a value that was never overwritten.
        event_id = audit.record(
            "tag.track",
            album_id=album_id,
            file=naming.name_of(file_path),
            # +1 because `_flatten_tracks` enumerates from zero and MusicBrainz,
            # the files and the album page all count from one (#240). A record
            # off by one is worse than none: it is exactly what someone auditing
            # a re-tag would read as the tagger having assigned the wrong track.
            track=track_pos_in_medium + 1,
            title=_track_title(track),
        )
        if event_id is not None:
            _record_changes(event_id, naming, file_path, tagset, changes)

    if prep.art.preserves_per_track_art and wrote_something:
        # Attributed to the album (#260). This is a decision Harmonist made on
        # the user's behalf about their files, so it has to reach that album's
        # own History — and the feed's log mirror drops any record that doesn't
        # say which album it means. The `art=preserved` token on the `tag.album`
        # line above is not a substitute: it shows only under "Show details".
        #
        # AFTER the loop, and only if it wrote (#272). The decision recurs
        # identically on every re-tag — preserving the user's artwork is the
        # outcome every time — so announcing it unconditionally reports a
        # decision rather than a change, and under #32's nightly pass that is one
        # warning per night forever on every compilation. `_record_changes`
        # already takes this position for the per-field detail; this line joins
        # it rather than being demoted back to an audit-only token, which is the
        # state #260 was filed against.
        #
        # A crash part-way through the loop therefore loses it — acceptable,
        # because the `tag.album` line records `art=preserved` before the first
        # write, so the forensic record of the decision is already down.
        #
        # The album's name is NOT repeated into the message; it rides in its own
        # column, which is where the feed and the History both render it.
        log.warning(
            "tracks have per-track embedded artwork — keeping it, NOT embedding "
            "the album cover (folder cover.* is still written). Re-tag with "
            "'replace artwork' to override.",
            extra={"album_id": album_id, "album_label": _album_label(release, album_dir)},
        )

    return len(prep.files)


def plan_album(
    album_dir: Path,
    release: Release,
    cover_path: Path | None = None,
    *,
    incomplete: bool = False,
    overwrite_art: bool = False,
    files: list[Path] | None = None,
    artwork: bool = True,
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
    )
    changes: dict[Path, dict[str, list[Any]]] = {}
    for file_path, (medium, track_pos_in_medium, track) in prep.pairs:
        tagset = _build_tagset(release, medium, track_pos_in_medium, track, prep.media_total)
        if file_changes := _changes_for(
            tagset,
            formats.read_owned(file_path),
            file_path,
            prep.art.before,
            # Only where the tagging would really write it — the same targets
            # `tag_album` hands `write_tags`. Every file differing from the
            # winner used to be reported here, including the ones a tagging
            # leaves alone since #418.
            prep.art.winner.digest
            if prep.art.winner is not None and file_path in prep.art_targets
            else None,
            prep.accepted_album_title,
            prep.accepted_countries,
        ):
            changes[file_path] = file_changes
    return AlbumPlan(changes=changes, preserves_per_track_art=prep.art.preserves_per_track_art)


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
    REVIEW verdict is for. See `docs/design.md` §5.

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
    owned.Owned.ALBUM_ARTISTS: owned.fills_in_an_absent_list,
    owned.Owned.ARTISTS: owned.fills_in_an_absent_list,
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
    #: The album's artwork plan, whole. What this tagging writes of it is
    #: `art_targets` and `cover_change`.
    art: artwork.ArtworkPlan
    #: The winning image's bytes, loaded only when this tagging writes it —
    #: None to leave every file's own art alone.
    cover: bytes | None
    #: …and its digest, for the per-file records.
    art_after: str | None
    media_total: int
    #: The one other album title that counts as already correct — Picard's
    #: disambiguated spelling (#283). Album-constant, since every file's TagSet
    #: carries the same `album`, so it is settled once here rather than rebuilt
    #: per file.
    accepted_album_title: str | None
    #: Every release country that counts as already correct: the ones THIS
    #: release names. Picard writes whichever of them `preferred_release_
    #: countries` matches, so a library tagged that way carries a code that is
    #: not MusicBrainz's scalar `country` and is not stale either. Album-constant
    #: for the reason above.
    accepted_countries: frozenset[str]
    #: The files this TAGGING may write `cover` to — the ones carrying nothing,
    #: or every file under `overwrite_art` (#418). Everything else keeps the
    #: image it has; improving those is the artwork action's job.
    art_targets: frozenset[Path] = frozenset()
    #: The folder cover this tagging creates, when the album has none (#457).
    cover_change: artwork.Change | None = None
    #: The page's preview no longer matches the plan, so this tagging writes no
    #: artwork at all (#469).
    art_withheld: bool = False


def decide_artwork(
    album_dir: Path,
    files: Sequence[Path],
    cover_path: Path | None,
    *,
    archive: cover_art.Front | None = None,
    overwrite_art: bool = False,
    consider: bool = True,
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
    """
    if not consider:
        return artwork.ArtworkPlan(album_dir=album_dir)
    cover = None
    if cover_path is not None:
        cover = artwork.FolderCover(
            name=cover_path.name,
            image=formats.EmbeddedArt.of(cover_path.read_bytes(), _mime_for(cover_path)),
            path=cover_path,
        )
    return artwork.plan(
        album_dir,
        [(f, _embedded_art(f)) for f in files],
        cover,
        formats.EmbeddedArt.of(archive.data, archive.mime) if archive is not None else None,
        overwrite_art=overwrite_art,
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
) -> _Prepared:
    """Decide what a tagging of this album would consist of, reading no tags.

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
    scope = artwork.Scope.ALL if overwrite_art else artwork.Scope.ADDITIONS
    withheld = expected_artwork is not None and art.fingerprint(scope) != expected_artwork
    art_targets = frozenset() if withheld else art.track_targets(scope)
    cover_change = None if withheld else art.cover_change(scope)
    # The bytes only when something is really written. With `cover=None`
    # write_tags leaves the existing art alone, so an album that needs nothing
    # costs no second read of its winner (#44, #74).
    cover: bytes | None = None
    if art_targets or cover_change is not None:
        try:
            cover = _winner_bytes(art, cover_path, archive)
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
        art=art,
        cover=cover,
        # Only now is it settled that the embed is really happening — the
        # per-track-art guard may have cancelled it, and the preview check may
        # have withheld it.
        art_after=art.winner.digest if cover is not None and art.winner is not None else None,
        art_targets=art_targets,
        cover_change=cover_change,
        art_withheld=withheld,
        media_total=len(release.get("medium-list", [])) or 1,
        accepted_album_title=title_with_disambiguation(
            release.get("title"), release.get("disambiguation")
        ),
        accepted_countries=release_countries(release),
    )


def _changes_for(
    tagset: TagSet,
    before: dict[str, Any],
    file_path: Path,
    art_before: Mapping[Path, str | None],
    art_after: str | None,
    accepted_album_title: str | None = None,
    accepted_countries: frozenset[str] = frozenset(),
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

    # An album title carrying the release disambiguation is not a change (#283).
    # Dropped from the diff rather than smoothed over in `owned.diff`, which
    # compares values and rightly knows nothing about MusicBrainz: what makes
    # this second spelling legitimate is a fact about the release.
    #
    # Only the diff is tolerant. A write that happens for some OTHER reason
    # still puts MusicBrainz's plain title on the file — Harmonist writes what
    # MusicBrainz says, and preserving a spelling it did not derive is the
    # separate, configurable question this issue deliberately left alone.
    album_change = changes.get(owned.Owned.ALBUM)
    if (
        album_change is not None
        and accepted_album_title is not None
        and album_change[0] == accepted_album_title
    ):
        del changes[owned.Owned.ALBUM]

    # Nor is a release country the release actually names (#346). MusicBrainz
    # collapses a release issued in several countries to one scalar `country`;
    # Picard writes whichever of them `preferred_release_countries` matches
    # (`picard/mbjson.py`, `release_to_metadata`), so a library tagged that way
    # carries a different code that is every bit as true of the release.
    #
    # The same tolerance as the album title, for the same reason and with the
    # same limit: THIS release's own release events, never "any country" — a
    # code MusicBrainz does not list for it is genuinely stale, and accepting it
    # would be inventing a fact the release states for free.
    #
    # Only the diff is tolerant. A write that happens for some other reason
    # still puts MusicBrainz's `country` on the file.
    country_change = changes.get(owned.Owned.MB_ALBUM_COUNTRY)
    if country_change is not None and country_change[0] in accepted_countries:
        del changes[owned.Owned.MB_ALBUM_COUNTRY]

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
        position=(
            f"{tagset.disc_num}-{tagset.track_num}"
            if tagset.disc_total > 1
            else str(tagset.track_num)
        ),
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


def _winner_bytes(
    plan: artwork.ArtworkPlan, cover_path: Path | None, archive: cover_art.Front | None
) -> bytes:
    """The image the plan writes, read from where the plan found it — and
    checked to BE that image, by digest.

    The plan carries descriptions, not bytes: an album page holds every track's
    tags at once, and carrying the images too would be hundreds of megabytes on
    a box set (`EmbeddedArt`). So the bytes are fetched here, once, at the last
    moment — and anything that has moved since the plan was made is refused
    rather than written.
    """
    winner = plan.winner
    if winner is None:
        raise ArtworkChangedError("the plan writes no image")
    data: bytes | None = None
    try:
        if plan.source is artwork.Source.FOLDER and cover_path is not None:
            data = cover_path.read_bytes()
        elif plan.source is artwork.Source.ALBUM:
            carrier = next((p for p, d in plan.before.items() if d == winner.digest), None)
            art = formats.read_cover(carrier) if carrier is not None else None
            data = art[0] if art is not None else None
        elif plan.source is artwork.Source.ARCHIVE and archive is not None:
            data = archive.data
    except OSError as e:
        raise ArtworkChangedError(f"could not read the winning image again: {e}") from e
    if data is None or images.digest(data) != winner.digest:
        raise ArtworkChangedError("the winning image is no longer where the plan found it")
    return data


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
    try:
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_bytes(image)
        os.replace(tmp, target)
    except OSError:
        log.exception("could not write the album's artwork to %s", target)
        return _Wrote.FAILED

    # The id the album answers to right now (#456). A cover is often created by
    # an album's FIRST tagging, before any sidecar names it — and a bare None
    # would leave exactly the albums most likely to gain a cover with no record
    # of having gained one. `id_registry.peek` is a pure hash of the path, what
    # `sidecar.write()` then persists as `temp_uid`, and what the scanner already
    # calls the album meanwhile; the alias chain carries it forward from there.
    album_id = album_id or id_registry.peek(album_dir)
    details = {"was": change.before} if change.before is not None else {}
    audit.record(
        "cover.write",
        album_id=album_id,
        album=album_dir,
        file=target.name,
        source=source.value if source is not None else "-",
        bytes=len(image),
        overwrote=change.before is not None,
        digest=change.after,
        **details,
    )
    event_id = audit.record("tag.track", album_id=album_id, album=album_dir, file=target.name)
    if event_id is not None:
        activity_store.record_tag_changes(
            event_id, file=target.name, changes={owned.ARTWORK: [change.before, change.after]}
        )
    return _Wrote.WRITTEN


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
    naming: album_files.Naming, files: Sequence[Path], plan: Sequence[tag_history.FileRevert]
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
    # Keyed by file name, never by position: the plan's order and the
    # directory's are both file order today, but pairing two lists that merely
    # happen to agree is how the wrong track's id gets written.
    changes = {item.file: item.fields[field] for item in plan if field in item.fields}
    if not changes or len(changes) != len(plan):
        return None
    befores = {before for before, _after in changes.values()}
    if len(befores) != 1:
        return None

    on_disk = {naming.name_of(p): p for p in files}
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
    identity = _identity_revert(naming, album_all, plan)

    for item in plan:
        path = _file_named(naming, item.file, RevertUnavailableError)
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
            file=naming.name_of(path),
        )
        if event_id is not None:
            changes = owned.diff(before, target)
            if changes:
                activity_store.record_tag_changes(
                    event_id, file=naming.name_of(path), changes=changes
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


def apply_artwork(
    album_dir: Path,
    plan: artwork.ArtworkPlan,
    *,
    files: Sequence[Path],
    cover_path: Path | None,
    archive: cover_art.Front | None = None,
    scope: artwork.Scope = artwork.Scope.ALL,
) -> ArtworkOutcome:
    """Carry out the part of `plan` that `scope` permits, and nothing else.

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
    image = _winner_bytes(plan, cover_path, archive)
    digest = images.digest(image)
    album_id = sidecar_mod.album_id_for(album_dir)
    # Named the same way a tagging names them, or the Undo this records would
    # address the wrong disc of a split album (#423).
    naming = album_files.Naming(album_dir, files)
    tracks = [c for c in changes if not c.folder_cover]

    # Before anything is written, and for the same reason `tag_album` records
    # its `tag.album` line first: a crash part-way through leaves evidence of
    # what was attempted rather than silence.
    audit.record(
        "artwork.update",
        album_id=album_id,
        album=album_dir,
        files=len(tracks),
        digest=digest,
        scope=scope.value,
    )
    # Keep whatever is about to be destroyed — every track AND the folder cover,
    # in one go — before a single write, so this is undoable (#131) and so a
    # target whose image could not be kept is known before anything moves (#470).
    kept = _keep_doomed_art({c.target: c.before for c in changes}, digest)

    changed = 0
    stale: list[str] = []
    unkept: list[str] = []
    failed: list[str] = []
    for change in tracks:
        name = naming.name_of(change.target)
        current = formats.read_cover(change.target)
        if (images.digest(current[0]) if current is not None else None) != change.before:
            stale.append(name)
            continue
        if change.before is not None and change.before not in kept:
            unkept.append(name)
            continue
        formats.write_cover(change.target, image)
        changed += 1
        # Recorded in the shape a tagged file's artwork change takes, which is
        # what puts an Undo on it: `tag_history.artwork_replaced` reads that
        # pair and `restore_artwork` writes it back.
        event_id = audit.record("tag.track", album_id=album_id, album=album_dir, file=name)
        if event_id is not None:
            activity_store.record_tag_changes(
                event_id, file=name, changes={owned.ARTWORK: [change.before, change.after]}
            )

    if (cover := plan.cover_change(scope)) is not None:
        wrote = _write_folder_cover(album_dir, cover, image, plan.source, album_id, kept)
        if wrote is _Wrote.WRITTEN:
            changed += 1
        elif wrote is _Wrote.STALE:
            stale.append(cover.target.name)
        elif wrote is _Wrote.UNKEPT:
            unkept.append(cover.target.name)
        else:
            failed.append(cover.target.name)
    return ArtworkOutcome(
        changed=changed, stale=tuple(stale), unkept=tuple(unkept), failed=tuple(failed)
    )


# No `update_artwork(album_dir, release, ...)` that decides and applies in one
# call. It was the artwork action's entry point until #469, and would now have
# no production caller: the action applies the plan its page was DRAWN from,
# checked by fingerprint, and a convenience that re-decides behind the page's
# back is exactly the parallel decision that change removed.


def restore_artwork(
    album_dir: Path, digests: dict[str, str], *, paths: Sequence[Path] | None = None
) -> int:
    """Put back the artwork `digests` names, and return how many files changed.

    `digests` maps a file name to the sha256 of the image that file should carry
    — straight out of a tagging's `artwork` before-values (#86). Restoring by
    digest rather than "the album's old cover" is what makes a compilation's
    per-track art come back to the right tracks.

    Every image is checked to be present BEFORE anything is written: a partial
    restore would leave the album in a state that was never real, and neither
    half of it revertable. Files whose art already matches are skipped, so the
    operation is idempotent.

    `paths` is every directory the album occupies, for the same reason
    `revert_tags` takes them (#423): a track on a second disc is addressed by
    the album's own file list rather than by a join onto the primary directory.
    """
    files = album_files.for_paths(paths if paths is not None else [album_dir])
    naming = album_files.Naming(album_dir, files)
    resolved: dict[Path, bytes] = {}
    for name, key in digests.items():
        # A record can name the folder cover as well as a track (#410), and that
        # is not one of the album's audio files — so a name the album's own list
        # doesn't answer falls back to a guarded join onto the primary directory,
        # where the cover lives. `_file_named` refuses an ambiguous one first, so
        # the fallback can never be reached by a name two tracks share.
        matches = naming.matches(name)
        path = (
            _file_named(naming, name, ArtworkUnavailableError)
            if matches
            else _resolve_in_album(album_dir, name, ArtworkUnavailableError)
        )
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

    # Keep what is about to be overwritten, exactly as a tagging would: an undo
    # is itself a destructive write, and must be as undoable as the thing it
    # undoes. All of it, BEFORE anything is written — and if any of it cannot be
    # kept, nothing is written at all (#470). Refusing the whole undo rather
    # than restoring the rest is this function's own rule: a partial restore is
    # a state the album never had.
    current = {path: art for path in resolved if (art := _image_at(path)) is not None}
    overwritten = {
        path: images.digest(art[0])
        for path, art in current.items()
        if images.digest(art[0]) != images.digest(resolved[path])
    }
    kept = _keep_doomed_art(overwritten)
    if unkept := [path for path, key in overwritten.items() if key not in kept]:
        raise ArtworkUnavailableError(
            f"the image {naming.name_of(unkept[0])} carries now could not be kept, "
            "and undoing would destroy it with no way back"
        )

    restored = 0
    for path, data in resolved.items():
        if path not in overwritten and path in current:
            continue  # already correct — restoring twice is a no-op
        _write_image_at(path, data)
        audit.record(
            "artwork.restore",
            album=album_dir,
            file=naming.name_of(path),
            digest=images.digest(data),
        )
        restored += 1
    return restored


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
    labels, catalog_numbers = _label_info(release.get("label-info-list") or [])
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
        # The release credit unjoined, so a two-artist collaboration files under
        # both names instead of under one composite pseudo-artist (#322). Bare
        # names by construction — `_artist_names` drops the join phrases, which
        # is the guess this tag exists to remove.
        album_artists=_artist_names(release.get("artist-credit")),
        artists=_artist_names(track_artist_credit),
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
        # Identity under the significance map (design §"Identity"), so the
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

    `name` is the CREDITED-AS name, so the parts spell the credit phrase exactly;
    `join` is the text that runs from this artist to the next — " feat. ", " & ",
    ", " — and is empty on the last. `mbid` is None for an artist MusicBrainz
    names without identifying, which renders as plain text rather than a link.
    """

    name: str
    mbid: str | None
    join: str


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


def _credit_parts(artist_credit: list[Any] | None) -> tuple[CreditPart, ...]:
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
                    entry.get("name") or artist.get("name", ""),
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
    """
    found: dict[str, tuple[CreditPart, ...] | None] = {}
    for credit in _credits_of(release):
        parts = _credit_parts(credit)
        if not parts:
            continue
        phrase = _artist_phrase(credit)
        if phrase in found and found[phrase] != parts:
            found[phrase] = None
        else:
            found.setdefault(phrase, parts)
    return {phrase: parts for phrase, parts in found.items() if parts}


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
    prevent. Harmonist writes the primary type only (design §5).
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

    Derived from `_credit_parts` rather than walking the credit a second time,
    so the flat phrase written to `artist` / `albumartist` and the linked parts
    the album page renders cannot disagree about what the credit says (#309).
    """
    return "".join(part.name + part.join for part in _credit_parts(artist_credit)).strip()


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
