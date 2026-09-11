"""What an album's artwork actually is, and what a re-tag would do to it (#155).

Pure functions over values, like `compare` and for the same reason: the album
page's Artwork section is a rendering of facts, and nothing here opens a file or
knows what a MusicBrainz release is. The caller reads; this decides what the
reading means.

The unit is the **distinct image**, not the track — and not the place it is kept.
Twelve tracks and a `cover.jpg` carrying one picture is ONE row reading "All 12
tracks and cover.jpg", because that is one thing to look at; a compilation with
four covers is four rows. The folder cover is a carrier like any track (#400):
showing it only as an incoming value made a file already on disk read as
something arriving from outside, and left the reader asking which of the two
images was about to be replaced.

Each row also says what applying the artwork would do to it — read off the
`ArtworkPlan` below, which is the same object the writer executes (#469). The
page used to reach that verdict with its own copy of the size rule, and the two
copies drifted: an unmeasurable folder cover made the page promise to replace
the tracks while the writer filled their gaps instead. Rows where nothing is
written say nothing at all: agreement is silence everywhere else on this page,
and a column of "left as is" on a 37-image box set is 37 boxes of noise.

Display and actionability are separate facts (#467). A row exists because the
album HAS something to show — an image, a gap, an absent folder cover that is
about to be created — and its right-hand side exists because the plan names a
write there. Neither is derived from the other.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from .formats import TrackTags
from .formats.types import EmbeddedArt
from .images import Size


def has_per_track_art(digests: Iterable[str | None]) -> bool:
    """True when the tracks carry DIFFERENT embedded images — per-track artwork
    worth preserving.

    A compilation's per-track covers are user data a re-tag must not destroy, and
    this is the whole of the rule that protects them. It lives here rather than
    in `tagger` because it is a fact about images; the tagger asks it, and so does
    the album page, and the two reaching it separately is how the page would come
    to promise something the re-tag then didn't do.

    Counts DISTINCT images, so tracks that merely differ in whether they have art
    at all are not per-track artwork — see #397, which is about that gap being the
    wrong answer, not about this being the wrong reading of it.
    """
    return len({d for d in digests if d is not None}) > 1


class CoverArtAnswer(Protocol):
    """What this module needs of a Cover Art Archive answer (#276).

    A protocol rather than the stored type, so `artwork` stays what it is: pure
    functions over values, with no reach into the event store. The concrete
    `activity_store.CachedCoverArt` satisfies it by having these members.

    Read-only properties throughout, deliberately: the concrete type is a
    FROZEN dataclass, and a protocol declaring plain attributes demands settable
    ones that a frozen instance cannot satisfy.
    """

    @property
    def has_art(self) -> bool: ...

    @property
    def from_release_group(self) -> bool: ...

    @property
    def width(self) -> int | None: ...

    @property
    def height(self) -> int | None: ...

    @property
    def length(self) -> int | None: ...

    @property
    def mime(self) -> str | None: ...


def beats(mine: Size | None, theirs: Size | None) -> bool:
    """Whether `mine` is the better image: strictly larger on both axes (#410).

    The one comparison, asked by the tagger when it decides what to write and by
    the album page when it says what a re-tag would do. Two spellings of it would
    let the page promise one thing and the button do another — the class of bug
    #283 and #346 closed for the album title and country rows.

    An unreadable header measures as None (see `images.dimensions`) and loses:
    "leave it alone" is the safe answer, and today's behaviour is not a bad one
    to fall back to. Equal sizes lose too — a re-encode of the same dimensions is
    not an improvement worth overwriting a user's file for.
    """
    if mine is None or theirs is None:
        return False
    return mine.width > theirs.width and mine.height > theirs.height


# ---------------------------------------------------------------------------
# The plan: what an album's images become, decided once (#418, #469)
# ---------------------------------------------------------------------------


class Operation(StrEnum):
    """What one artwork write does to its target (#468).

    Not a rank beside the tag levels. A tag change is described by how far it
    reaches; an image is described by whether the place it lands already held
    one — which is also what decides whether there is anything for an Undo to
    put back.
    """

    #: The target carried no image: an artless track, or a folder cover that
    #: does not exist yet.
    ADDITION = "addition"
    #: The target carried a different image, which this write overwrites.
    REPLACEMENT = "replacement"


class Source(StrEnum):
    """Where the image a plan writes comes from — the three candidates an album
    can offer (#276, #410)."""

    #: The `cover.*` beside the tracks.
    FOLDER = "folder"
    #: The one image the tracks themselves carry.
    ALBUM = "album"
    #: The Cover Art Archive's front cover for the release, from the local cache.
    ARCHIVE = "archive"


class Scope(StrEnum):
    """Which of a plan's changes an action may make.

    The plan says everything the winning image implies; an action takes the
    part of it that action is allowed to write. A tagging fills what is empty
    and replaces nothing (#418); the artwork action does both.
    """

    ADDITIONS = "additions"
    ALL = "all"


@dataclass(frozen=True)
class Change:
    """One write the plan makes: a target, what it holds now, what it will hold.

    `before` is None where the target holds no image at all — an artless track,
    or a folder cover that does not exist. Digests on both sides, because that
    is what the History records and the artwork store key on.
    """

    target: Path
    before: str | None
    after: str
    folder_cover: bool = False

    @property
    def operation(self) -> Operation:
        return Operation.ADDITION if self.before is None else Operation.REPLACEMENT


@dataclass(frozen=True)
class ArtworkPlan:
    """What should happen to an album's images, as the writes that do it.

    The single answer, consumed by the album page, the tagging, and the artwork
    action alike (#469). Each of those used to reach its own verdict — the page
    through a copy of the size rule, the tagging and the button through
    `tagger.decide_artwork`, and the folder cover's creation through
    `cover_art.ensure_cover` before any of them had looked — and a page
    describing one decision while the writer made another is the bug class
    this replaces.

    `changes` is everything the winner implies. An action narrows it with
    `scoped`; nothing narrows it by deciding again.
    """

    album_dir: Path
    #: Every readable track's current image as a digest, or None where it has
    #: none. Unreadable tracks are absent: a file nobody could open carries no
    #: evidence, and is never a target.
    before: Mapping[Path, str | None] = field(default_factory=dict)
    #: The image being written, and where it comes from. None when nothing is.
    winner: EmbeddedArt | None = None
    source: Source | None = None
    changes: tuple[Change, ...] = ()
    #: The tracks carry differing images and are being left alone — a decision
    #: worth reporting rather than a silent no-op (#260).
    preserves_per_track_art: bool = False

    def scoped(self, scope: Scope) -> tuple[Change, ...]:
        """The changes `scope` permits."""
        if scope is Scope.ALL:
            return self.changes
        return tuple(c for c in self.changes if c.operation is Operation.ADDITION)

    def track_targets(self, scope: Scope) -> frozenset[Path]:
        """The tracks `scope` writes to."""
        return frozenset(c.target for c in self.scoped(scope) if not c.folder_cover)

    def cover_change(self, scope: Scope) -> Change | None:
        """The folder cover's write under `scope`, if it has one."""
        return next((c for c in self.scoped(scope) if c.folder_cover), None)

    def operation(self, scope: Scope) -> Operation | None:
        """The summary of what `scope` would do: Replacement if anything is
        overwritten, Addition if everything written lands where nothing was,
        None if nothing is written. A mixed plan summarises as Replacement —
        that is the half a reader deciding whether they mind needs to hear."""
        changes = self.scoped(scope)
        if not changes:
            return None
        if any(c.operation is Operation.REPLACEMENT for c in changes):
            return Operation.REPLACEMENT
        return Operation.ADDITION

    def fingerprint(self, scope: Scope) -> str:
        """A digest of exactly what `scope` would write, target by target.

        What a reviewed page carries back to the server: the action rebuilds
        its plan from disk and proceeds only if this matches, so an image edited
        since the page was drawn, or a candidate that changed, cannot be written
        under a preview that described something else.

        Names are relative to the album, so the page — which reads through the
        album — and the tagger — which reads through its file list — spell the
        same target the same way.
        """
        lines = sorted(
            f"{self._name(c.target)}\t{c.before or ''}\t{c.after}" for c in self.scoped(scope)
        )
        return hashlib.sha256("\n".join(lines).encode()).hexdigest()

    def _name(self, path: Path) -> str:
        try:
            return path.relative_to(self.album_dir).as_posix()
        except ValueError:
            return path.name


def cover_name_for(mime: str) -> str:
    """The name a created folder cover takes: `cover.png` for a PNG, else
    `cover.jpg` — the two names `cover_art.cached_cover` looks for."""
    return "cover.png" if "png" in mime.lower() else "cover.jpg"


def plan(
    album_dir: Path,
    tracks: Sequence[tuple[Path, EmbeddedArt | None]],
    cover: FolderCover | None,
    archive: EmbeddedArt | None = None,
    *,
    overwrite_art: bool = False,
    cover_unreadable: bool = False,
) -> ArtworkPlan:
    """Which image wins, and every write it takes for it to be everywhere.

    Pure: descriptions in, a plan out. The caller reads — the album page from
    the tags it already has, the tagger from the files — and both reach the same
    plan because both come through here.

    THE LARGEST IMAGE WINS among the three an album can offer: what its tracks
    carry, what its folder holds, and what the Cover Art Archive has cached
    (#276, #410). Strictly larger on both axes (`beats`), so a tie goes to what
    is already there and an unmeasurable challenger never displaces anything.
    An image does beat NOTHING, though: an album with no artwork anywhere takes
    whatever candidate exists.

    `overwrite_art` opts out of the comparison: the user asked for the folder
    cover to be embedded, not judged.

    A missing folder cover is a target like any other, created from the winner
    (#457). What it is created FROM is the size rule's answer rather than a
    ladder of its own, so an album with a 3000px image in its tracks is no longer
    given a 1200px `cover.jpg` it then offers to replace.

    Per-track artwork is user data and nothing overwrites it. The one write
    such an album can still receive is a folder cover it lacks, and only from
    the archive — a compilation's first sleeve is not the album's cover.

    `cover_unreadable` is a folder cover that exists and could not be read. The
    plan is then empty: nothing can be decided about a file nobody saw, and a
    plan that treated it as absent would create a second cover beside it.
    """
    before = {path: (art.digest if art is not None else None) for path, art in tracks}
    if cover_unreadable:
        return ArtworkPlan(album_dir=album_dir, before=before)

    if overwrite_art:
        if cover is None:
            return ArtworkPlan(album_dir=album_dir, before=before)
        return _plan_for(album_dir, before, cover, cover.image, Source.FOLDER)

    if has_per_track_art(before.values()):
        if cover is None and archive is not None:
            return _plan_for(
                album_dir, {}, None, archive, Source.ARCHIVE, before=before, preserves=True
            )
        return ArtworkPlan(album_dir=album_dir, before=before, preserves_per_track_art=True)

    own = next((art for _, art in tracks if art is not None), None)
    winner: EmbeddedArt | None = None
    source: Source | None = None
    if cover is not None:
        winner, source = cover.image, Source.FOLDER
    # With no folder cover the tracks' image is the incumbent, measurable or not
    # — there is nothing for it to beat. Against a folder cover it wins unless
    # the cover is strictly larger, which is where ties to the tracks come from
    # (#397): `tagger` has always resolved it that way.
    if own is not None and (
        cover is None
        or (
            own.digest != cover.image.digest
            and own.size is not None
            and (cover.image.size is None or not beats(cover.image.size, own.size))
        )
    ):
        winner, source = own, Source.ALBUM
    if archive is not None and (winner is None or beats(archive.size, winner.size)):
        winner, source = archive, Source.ARCHIVE
    if winner is None or source is None:
        return ArtworkPlan(album_dir=album_dir, before=before)
    return _plan_for(album_dir, before, cover, winner, source)


def _plan_for(
    album_dir: Path,
    targets: Mapping[Path, str | None],
    cover: FolderCover | None,
    winner: EmbeddedArt,
    source: Source,
    *,
    before: Mapping[Path, str | None] | None = None,
    preserves: bool = False,
) -> ArtworkPlan:
    """Every write that puts `winner` on `targets` and on the folder cover.

    The folder file catches up only on a strict improvement: it is the one
    write here the tracks cannot be used to undo, so a same-sized different
    picture stays where it is.
    """
    changes = [
        Change(target=path, before=digest, after=winner.digest)
        for path, digest in targets.items()
        if digest != winner.digest
    ]
    if cover is None:
        changes.append(
            Change(
                target=album_dir / cover_name_for(winner.mime),
                before=None,
                after=winner.digest,
                folder_cover=True,
            )
        )
    elif cover.image.digest != winner.digest and beats(winner.size, cover.image.size):
        changes.append(
            Change(
                target=cover.path or album_dir / cover.name,
                before=cover.image.digest,
                after=winner.digest,
                folder_cover=True,
            )
        )
    return ArtworkPlan(
        album_dir=album_dir,
        before=before if before is not None else targets,
        winner=winner,
        source=source,
        changes=tuple(changes),
        preserves_per_track_art=preserves,
    )


class Outcome(StrEnum):
    """What applying the artwork would do to one row's image.

    Only the first two reach the page. The other two are the two ways of writing
    nothing, kept apart because they are different facts — one is a protection,
    the other a no-op — and a caller asking "why is nothing happening here"
    deserves the real answer.
    """

    #: This image is replaced by the winner.
    REPLACED = "replaced"
    #: These tracks have no image, or the folder cover does not exist; the
    #: winner fills the gap.
    FILLED = "filled"
    #: Left as it is though it is not the folder cover's image — per-track
    #: artwork Harmonist preserves, an album image that won or tied against the
    #: folder cover, or a gap with nothing to fill it.
    KEPT = "kept"
    #: Already the folder cover, or the folder cover itself. Nothing changes.
    SAME = "same"


@dataclass(frozen=True)
class TrackRef:
    """One track, as the section needs to name it."""

    name: str
    track_num: int | None = None
    disc_num: int | None = None
    title: str | None = None


@dataclass(frozen=True)
class FolderCover:
    """The `cover.*` beside the audio.

    Two things at once, which is why it needs its own type: a file the album
    already has — so it appears among the rows like any other carrier of an image
    — and the thing a re-tag would embed into every track.
    """

    name: str
    image: EmbeddedArt
    #: Where it is, when that is not simply `name` in the album's directory — a
    #: caller holding the real path hands it over rather than have it rebuilt.
    path: Path | None = None


@dataclass(frozen=True)
class ArchiveRow:
    """The Cover Art Archive's answer, drawn as a row rather than a sentence.

    Every answer it can give is one of these, because the archive's cover is one
    of the images this album could carry and everything in that category is a row
    (#433). `placeholder` is the word in the frame where a picture would be, and
    it distinguishes the two ways there isn't one: *not loaded* for an image that
    exists and was never downloaded because it lost, *none* for a release the
    archive holds nothing for.
    """

    placeholder: str
    meta: str
    #: Whether the archive keeps this cover against the RELEASE GROUP rather than
    #: this edition (#434) — true of a great many albums, and worth saying.
    from_release_group: bool = False
    #: The picture itself, once someone has asked for it (#448). A losing
    #: candidate is never downloaded on its own, because bigger is the only thing
    #: Harmonist can measure and a loser by that measure is not worth the
    #: megabytes — but bigger is not the same as better, and a reader deciding
    #: whether theirs really is the better scan needs to see the other one.
    #:
    #: Its presence says nothing about whether it won. `archive_wins` is decided
    #: on size and is not consulted here, so a loaded loser stays exactly what it
    #: was: muted, unmarked, and not going to be written (#441).
    image: EmbeddedArt | None = None

    @property
    def loadable(self) -> bool:
        """Whether there is a picture to go and get: the archive has one, and it
        is not here yet. False for a release the archive holds nothing for —
        there is nothing to load, which is a different fact from not having
        loaded it (#433)."""
        return self.image is None and self.placeholder != "none"


@dataclass(frozen=True)
class ArtRow:
    """One distinct image, and everything carrying it.

    `image` is None on the row for tracks with no embedded art at all. That row is
    not a gap in the data — it is the finding, and the section draws it as an
    empty frame rather than omitting it, because an album where two tracks have
    lost their artwork looks exactly like a healthy one if you only draw what is
    there.

    `on_cover` is the folder cover's filename when this same image is also the
    folder cover, so one row can say "All 12 tracks and cover.jpg" rather than
    drawing one picture twice.

    `total_tracks` and `multi_disc` are the album context the label needs. Carried
    on the row rather than passed to `label` so the template can't render two rows
    of one album against different totals.
    """

    image: EmbeddedArt | None
    tracks: tuple[TrackRef, ...]
    outcome: Outcome
    total_tracks: int = 0
    multi_disc: bool = False
    on_cover: str | None = None
    #: What a re-tag would put here, named — the folder cover for a track row,
    #: and the album's own artwork for the folder cover's row when that is the
    #: better image (#410). None where nothing is written.
    written_from: str | None = None
    #: Whether what would be written came from the Cover Art Archive (#276) —
    #: which is the one case where the MusicBrainz hexagon is a claim Harmonist
    #: can support. A folder cover may have come from a Bandcamp download.
    from_archive: bool = False
    #: …and the image itself, so the row can SHOW what it would become rather
    #: than only assert it (#413). "Replaced by the album's own artwork" carries
    #: no tense — a reader cannot tell whether it already happened — and the
    #: picture on the right under a dated heading is what settles that.
    written_image: EmbeddedArt | None = None
    #: The name of a folder cover that does not exist and that the plan will
    #: create (#457). A row rather than a sentence, because an absent carrier
    #: about to gain an image is a gap like an artless track, and a gap is the
    #: finding.
    creates: str | None = None

    @property
    def is_gap(self) -> bool:
        return self.image is None

    @property
    def writes(self) -> bool:
        """Whether the plan puts something here. The rows that answer False
        render nothing on the right at all (#400)."""
        return self.outcome in (Outcome.REPLACED, Outcome.FILLED)

    @property
    def title(self) -> str | None:
        """The track's own title, when this row is a single track.

        Shown because there is room for it and because a number is a poor thing
        to recognise an image by — on a box set of episodes, the title is how you
        know which one you are looking at. Deliberately not shown for a row
        covering several tracks: that is a list, and the row is about the image.
        """
        if len(self.tracks) != 1:
            return None
        return self.tracks[0].title

    @property
    def label(self) -> str:
        """What carries this image, in the fewest words that stay true.

        "All 12 tracks" when it is all of them — the common case, and a list of
        twelve numbers would say the same thing worse. Otherwise the positions
        themselves, because that is what tells the reader which files to look at.

        On a multi-disc release a bare track number is not a unique reference, and
        rendering one is not merely terse but WRONG: *DRIFT Series 1* is eight
        discs in one directory, and an image shared by disc 1's track 5 and disc
        7's track 7 rendered as "Tracks 5, 7", which names two tracks that don't
        exist as one pair that does (#400). Naming the disc is what the tracklist
        already does for the same reason (`compare._number_column`).

        A track with no number at all falls back to a count: an unnumbered file
        cannot be pointed at by position.
        """
        if self.creates:
            return self.creates
        n = len(self.tracks)
        carriers = []
        if n:
            carriers.append(self._track_label(n))
        if self.on_cover:
            carriers.append(self.on_cover)
        return " and ".join(carriers) if carriers else "Not on any track"

    def _track_label(self, n: int) -> str:
        if n == self.total_tracks:
            return f"All {n} tracks" if n != 1 else "The only track"
        if any(t.track_num is None for t in self.tracks):
            return f"{n} track" if n == 1 else f"{n} tracks"
        if not self.multi_disc:
            spans = _ranges(sorted(t.track_num for t in self.tracks if t.track_num))
            return f"Track {spans}" if n == 1 else f"Tracks {spans}"
        # Grouped by disc, so each number is read against the disc it belongs to.
        by_disc: dict[int | None, list[int]] = {}
        for t in self.tracks:
            if t.track_num is not None:
                by_disc.setdefault(t.disc_num, []).append(t.track_num)
        parts = []
        for disc, numbers in sorted(by_disc.items(), key=lambda kv: (kv[0] is None, kv[0])):
            spans = _ranges(sorted(numbers))
            word = "track" if len(numbers) == 1 else "tracks"
            parts.append(f"Disc {disc}, {word} {spans}" if disc else f"{word.title()} {spans}")
        return " · ".join(parts)


def _ranges(numbers: Sequence[int]) -> str:
    """`[1,2,3,7]` → `"1–3, 7"`. An en dash, as the rest of the UI uses."""
    spans: list[tuple[int, int]] = []
    for num in numbers:
        if spans and num == spans[-1][1] + 1:
            spans[-1] = (spans[-1][0], num)
        else:
            spans.append((num, num))
    return ", ".join(str(a) if a == b else f"{a}–{b}" for a, b in spans)


@dataclass(frozen=True)
class ArtworkView:
    """Everything the Artwork section shows about one album."""

    rows: tuple[ArtRow, ...] = field(default_factory=tuple)
    cover: FolderCover | None = None
    total_tracks: int = 0
    #: Tracks that could not be read at all. They vote for nothing — a file
    #: Harmonist cannot open carries no evidence about artwork, and counting it
    #: as artless would report a mount that blinked as an album that lost its
    #: covers (#112).
    unreadable: int = 0
    #: What the Cover Art Archive holds for this release, when it has been asked
    #: (#276). Reported rather than acted on for now: a re-tag still writes only
    #: what is already on disk, and the archive's answer is here to say whether
    #: it is worth taking.
    caa: CoverArtAnswer | None = None
    #: Whether the archive's cover beat everything the album has, and is
    #: therefore the incoming value rather than an also-ran (#433).
    archive_wins: bool = False
    #: The archive's image itself, when a copy is held locally. Present for a
    #: WINNER, which is downloaded as part of the check, and for a loser someone
    #: has asked to see (#448) — so it says only "there is a copy of this on
    #: disk", never anything about which won.
    archive: EmbeddedArt | None = None
    #: A folder cover exists but could not be read. NOT the same as having none,
    #: and the difference is the whole right-hand column: with the cover unread
    #: there is no saying what applying would write, so the section states that
    #: rather than showing outcomes derived from a file it never saw.
    cover_unreadable: bool = False
    #: The plan the rows were read off — and the one the section's action
    #: executes, checked by `fingerprint` (#469).
    plan: ArtworkPlan | None = None

    @property
    def images(self) -> tuple[ArtRow, ...]:
        """The rows that actually have an image — the gap rows excluded."""
        return tuple(r for r in self.rows if r.image is not None)

    @property
    def writes(self) -> bool:
        """Whether applying the artwork would write anything at all.

        Read off the plan, not off the rows: the rows are what the album HAS,
        and a row existing says nothing about whether anything happens to it
        (#467). The column headings and the action hang off this — with nothing
        to write there is no second column to name, and "After Apply" over an
        empty half promises a change that is not coming.
        """
        return bool(self.plan and self.plan.changes)

    @property
    def operation(self) -> Operation | None:
        """Addition or Replacement, for the action's own scope — what the
        finding at the top of the page is labelled with (#468)."""
        return self.plan.operation(Scope.ALL) if self.plan else None

    @property
    def fingerprint(self) -> str:
        """What the section's action carries back, so it writes what was shown."""
        return (self.plan or ArtworkPlan(album_dir=Path())).fingerprint(Scope.ALL)

    @property
    def tagging_fingerprint(self) -> str:
        """The same, for the part of the plan a re-tag writes — the additions.

        Carried by Re-tag from MB, so a folder cover created or a gap filled by
        a tagging is one this page showed (#469)."""
        return (self.plan or ArtworkPlan(album_dir=Path())).fingerprint(Scope.ADDITIONS)

    @property
    def distinct(self) -> tuple[tuple[EmbeddedArt, str], ...]:
        """Every image the section draws, deduplicated, each with its caption.

        The full-size view is one element per image, rendered ONCE and referenced
        by each place the image appears. Without this the folder cover — which
        the incoming side of every replaced and filled row shows — would emit an
        element per row under one id, and every button on the page would open
        whichever copy came last.
        """
        out: dict[str, tuple[EmbeddedArt, str]] = {}
        for row in self.images:
            assert row.image is not None  # `images` filters them out
            out.setdefault(row.image.digest, (row.image, row.label))
        if self.cover is not None:
            out.setdefault(self.cover.image.digest, (self.cover.image, self.cover.name))
        # …and the archive's, once it is here to be looked at (#448). It is not
        # one of `self.rows` — it is on no row, because it is not on the album —
        # so without this the one image a user pressed a button to SEE would be
        # the only one that could not be opened full size, which is most of what
        # they wanted it for.
        if (archive_row := self.archive_row) is not None and archive_row.image is not None:
            out.setdefault(archive_row.image.digest, (archive_row.image, "Cover Art Archive"))
        # …and the winner, wherever it came from. The incoming side of every
        # written row points at it, and when it is the archive's it is on no
        # row and no carrier, so nothing else above would emit its view.
        if self.plan is not None and self.plan.winner is not None:
            out.setdefault(
                self.plan.winner.digest,
                (
                    self.plan.winner,
                    _source_label(self.plan.source, self.cover) or "the incoming image",
                ),
            )
        return tuple(out.values())

    @property
    def summary(self) -> str:
        """One line for the top of the page (#417) — what updating the artwork
        would do, in the terms the rows use.

        Written here rather than in the template for the reason the verdict was:
        it has several shapes, and a Jinja `{% if %}` chain is where wording goes
        to stop being reviewed.

        Counted in FILES, not in images (#443). The distinct image is the right
        unit for the rows — twelve tracks sharing one picture is one thing to
        look at — and the wrong one for a sentence at the top of the page, where
        "1 image that could be better" understates a change to twelve files and
        reads as a complaint about the picture rather than an offer to improve
        it.

        The folder cover is counted by NAME rather than folded into the number,
        because it is not a track and because it can be the only thing that
        changes: when the album's own art beats `cover.jpg`, it is the folder
        file that catches up and no track moves at all (#410). A count of tracks
        would report zero for that album and say nothing.
        """
        filled = sum(len(r.tracks) for r in self.rows if r.is_gap and r.writes)
        # A row that writes and sits on the cover means the cover file changes,
        # whether or not any track shares that image.
        improved = sum(len(r.tracks) for r in self.images if r.writes)
        cover_improved = any(r.writes and r.on_cover for r in self.images)
        creates = next((r.creates for r in self.rows if r.creates and r.writes), None)

        carriers = []
        if improved:
            carriers.append(f"{improved} track{'' if improved == 1 else 's'}")
        if cover_improved and self.cover is not None:
            carriers.append(self.cover.name)

        parts = []
        if filled:
            parts.append(f"{filled} track{' is' if filled == 1 else 's are'} missing artwork")
        if carriers:
            parts.append(f"better artwork is available for {' and '.join(carriers)}")
        if creates:
            # Named, because it is a file that will appear in the user's folder
            # and the one thing on this line they could not see coming (#457).
            parts.append(f"there is no {creates}")
        if not parts:
            return "This album's artwork can be updated."
        # Capitalised from whichever clause leads, since either can.
        line = ", and ".join(parts)
        return line[0].upper() + line[1:] + "."

    @property
    def archive_row(self) -> ArchiveRow | None:
        """The archive's cover as a row of facts, when it is not what would be
        written.

        A row rather than a sentence, because it is one of the images this album
        could carry and everything else in that category is a row (#433). Drawn
        muted, with a placeholder where the picture would be and no hexagon: it
        exists, and nothing is going to come of it.

        The placeholder is honest rather than a stand-in. A losing candidate is
        deliberately never downloaded (#276) — measured with a 64 KB range and
        forgotten — so there genuinely is no picture, and fetching one to show
        would spend megabytes on an image nobody will use.

        None when the archive's cover WINS: it is then the incoming value, and
        appears in that column with the hexagon, which is where purple belongs.

        "Not what would be written" covers two cases and deliberately treats
        them alike: a cover that is smaller than the album's own, and one that
        is bigger but was never downloaded. The second is not a hole in the
        model — the tagger reads the same cache, so an image that is not there
        cannot be written whatever its measurement says, and a row stating its
        size is the honest account of it.
        """
        answer = self.caa
        if answer is None or self.archive_wins:
            return None
        if not answer.has_art:
            # A different word, because it is a different fact: there is nothing
            # to load, rather than something not loaded (#433).
            return ArchiveRow(placeholder="none", meta="no front cover for this release")
        size = Size(answer.width, answer.height) if answer.width and answer.height else None
        if size is None:
            return ArchiveRow(
                placeholder="not loaded", meta="size could not be read", image=self.archive
            )
        return ArchiveRow(
            placeholder="not loaded",
            meta=describe_parts(size, answer.mime, answer.length),
            # Whose cover it is, when that is not this edition's (#434). A
            # release group's artwork stands for every edition of the album, and
            # someone comparing editions may care that this one is the general
            # one rather than their pressing's.
            from_release_group=answer.from_release_group,
            # The picture, when someone has asked for it (#448). Reaching this
            # line at all means the archive's cover did NOT win — so an image
            # here is one a user went and fetched to look at, and showing it
            # changes nothing about what a re-tag would write.
            image=self.archive,
        )

    @property
    def count(self) -> str | None:
        """The count beside the section heading — "3 images · 2 gaps", or None
        when there is nothing to count. Mirrors History's `· N`.

        The gaps are counted alongside because otherwise a heading reading
        "1 image" sits above an album where a third of the tracks have none —
        true, and quietly missing the point.
        """
        n = len(self.images)
        gaps = sum(len(r.tracks) for r in self.rows if r.is_gap)
        if not n:
            return None
        images = "1 image" if n == 1 else f"{n} images"
        return f"{images} · {gaps} gap{'' if gaps == 1 else 's'}" if gaps else images


def describe_parts(size: Size | None, mime: str | None, length: int | None) -> str:
    """The facts line, from parts rather than from an image (#433).

    The archive's losing cover is never downloaded, so there is no `EmbeddedArt`
    to describe — only what the measurement recorded. Same formatter either way,
    so the two are comparable at a glance, which is the whole point of showing
    them together.
    """
    dims = f"{size.width}×{size.height} · " if size else ""
    kind = f"{mime.removeprefix('image/').upper()}" if mime else "?"
    if not length:
        return f"{dims}{kind}"
    kb = length / 1024
    weight = f"{kb / 1024:.1f} MB" if kb >= 1024 else f"{kb:.0f} KB"
    return f"{dims}{kind} · {weight}"


def describe(image: EmbeddedArt) -> str:
    """One image as a line of facts: "1400×1400 · JPEG · 718 KB".

    The dimensions come first because they are what the question is usually
    about — whether a re-tag is an improvement or a downgrade is decided there —
    and they are simply absent when the header could not be read, rather than
    guessed at (see `images.dimensions`).

    KB and MB in the units a user recognises off a file listing, not bytes.
    """
    return describe_parts(image.size, image.mime, image.length)


def _source_label(source: Source | None, cover: FolderCover | None) -> str | None:
    """The winner, named the way a row names what it becomes.

    By what it IS rather than by a filename, except where it is a file in this
    album: the album's own image and the archive's are neither.
    """
    if source is Source.FOLDER:
        return cover.name if cover is not None else "the folder cover"
    if source is Source.ALBUM:
        return "the album's own artwork"
    if source is Source.ARCHIVE:
        return "the Cover Art Archive"
    return None


def _name_in(path: Path, album_dir: Path) -> str:
    """A track's name as the section shows it: relative to the album, or its
    bare name when it sits outside the album's primary directory."""
    try:
        return path.relative_to(album_dir).as_posix()
    except ValueError:
        return path.name


def summarise(
    album_dir: Path,
    tracks: Sequence[tuple[Path, TrackTags]],
    cover: FolderCover | None,
    caa: CoverArtAnswer | None = None,
    archive: EmbeddedArt | None = None,
    *,
    cover_unreadable: bool = False,
) -> ArtworkView:
    """Everything the section shows, from tags already read and a folder cover.

    `tracks` is `(path, tags)` in track order — taken in order because the rows
    come out in the order the album plays.

    The rows are what the album HAS: one per distinct image, one for the tracks
    carrying none, one for a folder cover that differs from them all, and one
    for a folder cover that does not exist yet but is about to (#457). Which of
    them change, and into what, is read off the `plan` built here from the same
    descriptions — the plan the section's action then executes (#469). Nothing
    below decides what wins; it only looks up what the plan decided.
    """
    readable = [(path, t) for path, t in tracks if not t.unreadable]
    the_plan = plan(
        album_dir,
        [(path, t.art) for path, t in readable],
        cover,
        archive,
        cover_unreadable=cover_unreadable,
    )
    written = {c.target for c in the_plan.changes}
    cover_change = the_plan.cover_change(Scope.ALL)
    written_from = _source_label(the_plan.source, cover)
    from_archive = the_plan.source is Source.ARCHIVE

    by_digest: dict[str, list[tuple[Path, TrackRef]]] = {}
    art_of: dict[str, EmbeddedArt] = {}
    gap: list[tuple[Path, TrackRef]] = []
    for path, tags in readable:
        ref = TrackRef(
            name=_name_in(path, album_dir),
            track_num=tags.track_num,
            disc_num=tags.disc_num,
            title=tags.title,
        )
        if tags.art is None:
            gap.append((path, ref))
            continue
        by_digest.setdefault(tags.art.digest, []).append((path, ref))
        art_of.setdefault(tags.art.digest, tags.art)

    # More than one disc is a property of the ALBUM, not of a row: a box set
    # whose images each sit on one disc still needs every label to name its disc,
    # or two discs' track 1 read as one repeated row (#400).
    multi_disc = len({t.disc_num for _, t in readable} - {None}) > 1

    def row(
        image: EmbeddedArt | None,
        carriers: Sequence[tuple[Path, TrackRef]],
        writes: bool,
        *,
        on_cover: str | None = None,
        creates: str | None = None,
    ) -> ArtRow:
        if writes:
            outcome = Outcome.FILLED if image is None else Outcome.REPLACED
        else:
            outcome = Outcome.SAME if on_cover is not None else Outcome.KEPT
        return ArtRow(
            image=image,
            tracks=tuple(ref for _, ref in carriers),
            outcome=outcome,
            total_tracks=len(tracks),
            multi_disc=multi_disc,
            on_cover=on_cover,
            written_from=written_from if writes else None,
            written_image=the_plan.winner if writes else None,
            from_archive=writes and from_archive,
            creates=creates,
        )

    def cover_written(digest: str) -> bool:
        return cover_change is not None and cover_change.before == digest

    rows = []
    for digest, carriers in by_digest.items():
        on_cover = cover.name if cover is not None and cover.image.digest == digest else None
        rows.append(
            row(
                art_of[digest],
                carriers,
                any(path in written for path, _ in carriers)
                or (on_cover is not None and cover_written(digest)),
                on_cover=on_cover,
            )
        )
    # The folder cover is a carrier too, and gets a row of its own when no track
    # already accounts for it (#400) — the album HAS this image, whatever the
    # tracks carry, and a reader deciding what applying would do needs to see it
    # beside the rest rather than only as something arriving from outside.
    if cover is not None and cover.image.digest not in by_digest:
        rows.append(row(cover.image, (), cover_written(cover.image.digest), on_cover=cover.name))
    # …and when there is no folder cover and the plan makes one, that is a row
    # too: an empty frame on the left, the image it will hold on the right. It
    # was a sentence while nothing but a tagging could create it, because a row
    # counts toward the section's action and that action could not (#457).
    if cover is None and cover_change is not None:
        rows.append(row(None, (), True, creates=cover_change.target.name))
    if gap:
        rows.append(row(None, gap, any(path in written for path, _ in gap)))

    return ArtworkView(
        rows=tuple(rows),
        cover=cover,
        total_tracks=len(tracks),
        unreadable=len(tracks) - len(readable),
        caa=caa,
        archive_wins=from_archive,
        archive=archive,
        cover_unreadable=cover_unreadable,
        plan=the_plan,
    )
