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
import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import ClassVar, Protocol

from . import album_files
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


@dataclass(frozen=True)
class Incoming:
    """An image a plan writes, and where it comes from — which is where the
    writer goes back to for its bytes."""

    image: EmbeddedArt
    source: Source


class Choice(StrEnum):
    """What the user picked for one carrier, over the size rule's suggestion
    (#659)."""

    #: Leave this carrier's image as it is, whatever the size rule suggested.
    KEEP = "keep"
    #: Put the Cover Art Archive's image here, however it measures.
    ARCHIVE = "archive"


#: The carrier a choice names for the folder cover (#659).
COVER = "cover"
#: …and for the tracks carrying no image at all. Every other carrier is the
#: tracks sharing one image, named by its digest — which is how the section
#: already tells its rows apart, and which no user can mistake for another row.
GAP = "none"

#: The user's choices, by carrier. A carrier with no entry takes the size
#: rule's suggestion, so choosing for one row never drops another's.
#: The user's choices, by carrier: a `Choice`, or a particular archive image
#: named by its id (#659) — any of the release's or its group's, not only the
#: one front the size rule weighs. A carrier with no entry takes the size
#: rule's suggestion, so choosing for one row never drops another's.
Choices = Mapping[str, str]

_CARRIER = re.compile(rf"{COVER}|{GAP}|[0-9a-f]{{64}}")
#: An archive image id. The archive's are numbers, and nothing else may stand
#: in that place.
_IMAGE_ID = re.compile(r"[0-9]{1,20}")


def parse_choices(text: str) -> dict[str, str]:
    """Choices as a request carries them: `carrier=choice`, comma-separated.

    Anything else is dropped rather than refused. It can only come from a
    request no page of ours drew, and the ordinary plan is the safe reading of
    one — the fingerprint still has to match whatever is left, so nothing is
    written that the page did not show.
    """
    found: dict[str, str] = {}
    for part in text.split(","):
        carrier, _, value = part.partition("=")
        if not _CARRIER.fullmatch(carrier):
            continue
        if value in {c.value for c in Choice}:
            found[carrier] = Choice(value)
        elif _IMAGE_ID.fullmatch(value):
            found[carrier] = value
    return found


def format_choices(choices: Choices) -> str:
    """The spelling `parse_choices` reads — sorted, so one set of choices is one
    string."""
    return ",".join(f"{carrier}={choice}" for carrier, choice in sorted(choices.items()))


class FolderCoverPolicy(StrEnum):
    """Whether a plan may create a `cover.*` the album has not got (#516).

    A NAMED policy rather than a boolean, for the reason `GardenerConfig.level`
    is one: a third value is already foreseen — creating the folder cover
    ALWAYS, keeping it in step with the winning image rather than only filling
    its absence — and a `create_folder_cover = true` that later has to become
    `folder_cover = "always"` breaks a config file on somebody's NAS during an
    upgrade for no reason but our convenience. Two values are also two
    different questions once written down, where a bare `True` says only that
    something is on.

    Ordered by how much the plan writes: `NEVER` proposes nothing, `IF_MISSING`
    creates the file when the album lacks one.

    It governs CREATION alone. A folder cover that already exists is weighed by
    the size rule under either value, and neither value touches one — turning
    the policy down removes a proposal, never a file.
    """

    #: A missing folder cover is not a gap to close. Albums that have no
    #: `cover.*` are left without one, and nothing proposes creating it.
    NEVER = "never"
    #: The album has no `cover.*`, so the plan creates one from the winner
    #: (#457). What Harmonist did unconditionally before this was a setting.
    IF_MISSING = "if_missing"


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
    #: Every image a change writes, by digest, and where it comes from.
    #:
    #: A TABLE rather than one image for the tracks and one for the folder
    #: cover, which is what this was. The size rule never needs more than those
    #: two — the folder cover can take a better image than the tracks keep
    #: (#479) — but a user choosing row by row can put a different image on
    #: every row (#659), and a compilation's rows are a dozen of them.
    images: Mapping[str, Incoming] = field(default_factory=dict)
    changes: tuple[Change, ...] = ()
    #: The tracks carry differing images and are being left alone — a decision
    #: worth reporting rather than a silent no-op (#260).
    preserves_per_track_art: bool = False
    #: The user's choices this plan honoured (#659): the ones naming a carrier
    #: the album has, with an image in hand for them.
    choices: Mapping[str, str] = field(default_factory=dict)

    def image_for(self, change: Change) -> Incoming:
        """The image one change writes, and where it comes from.

        Asked by the writer and by the page alike, so a row cannot show one
        image while the write puts another there. Every change's `after` is in
        the table — that is how the plan is built — so a miss is a bug, and
        raises rather than writing nothing quietly.
        """
        return self.images[change.after]

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

        Names are relative to the album's COMMON ROOT, so the page — which reads
        through the album — and the tagger — which reads through its file list —
        spell the same target the same way, and two targets are never spelled
        alike (#423, #495).

        The root is taken once here and handed down, both because it is one
        answer for the whole plan and because deriving it per target would be
        quadratic on a box set.
        """
        root = album_files.root_of(self.album_dir, tuple(self.before))
        lines = sorted(
            f"{self._name(c.target, root)}\t{c.before or ''}\t{c.after}" for c in self.scoped(scope)
        )
        return hashlib.sha256("\n".join(lines).encode()).hexdigest()

    def _name(self, path: Path, root: Path) -> str:
        """How the fingerprint spells one target.

        Relative to the album's root — the deepest directory holding every file
        it has — which for the ordinary album IS its own directory and so leaves
        every name exactly as it was. It differs only for an album spread across
        sibling directories (`…/Album/CD1`, `…/Album/CD2`), and that is the case
        this exists for: named relative to the primary directory alone, the
        second disc fell outside it and dropped to its bare filename, so both
        discs' `01.m4a` became one name. A plan filling CD1's gap then digested
        identically to one filling CD2's, and the guard that exists to refuse an
        unreviewed write accepted it (#495).
        """
        try:
            return path.relative_to(root).as_posix()
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
    chosen: Source | None = None,
    choices: Choices | None = None,
    picks: Mapping[str, EmbeddedArt] | None = None,
    folder_cover: FolderCoverPolicy = FolderCoverPolicy.IF_MISSING,
) -> ArtworkPlan:
    """The size rule's suggestion (`_suggest`), with the user's choices made
    over it carrier by carrier (#659).

    `picks` are the archive images a choice may name by id, each with its
    original in hand.

    A carrier the choices do not name keeps its suggestion, so choosing for one
    row never quietly drops what the section proposed for another.

    Choosing is consent, and it is scoped to what was chosen. Per-track
    artwork is protected from the size rule because nobody has looked at it;
    a user who picked an image for one of a compilation's rows has looked at
    exactly that row, and only that row's tracks move.

    `overwrite_art` and an unreadable folder cover take no choices. The first
    is its own override; with the second nothing can be decided at all.
    """
    suggested = _suggest(
        album_dir,
        tracks,
        cover,
        archive,
        overwrite_art=overwrite_art,
        cover_unreadable=cover_unreadable,
        chosen=chosen,
        folder_cover=folder_cover,
    )
    if not choices or overwrite_art or cover_unreadable:
        return suggested
    return _with_choices(suggested, cover, archive, choices, picks or {})


def _with_choices(
    suggested: ArtworkPlan,
    cover: FolderCover | None,
    archive: EmbeddedArt | None,
    choices: Choices,
    picks: Mapping[str, EmbeddedArt],
) -> ArtworkPlan:
    """`suggested`, with every carrier `choices` names taken over by the choice.

    Only the choices that can be honoured are: a carrier the album has, and an
    image in hand for it — the release's own front for `Choice.ARCHIVE`, or the
    original of the archive image a pick names (`picks`, by archive id). The
    plan records which, so the page never claims a choice it could not act on
    (#472's rule, per carrier now).
    """
    album_dir = suggested.album_dir
    before = suggested.before
    # The folder cover is always a carrier, present or not. `folder` decides
    # what the size rule SUGGESTS creating (#516); a user who chose an image
    # for the missing file has asked for it, which is a different thing from
    # three hundred albums being told they have an update (#659).
    carriers = {digest or GAP for digest in before.values()} | {COVER}

    def image_of(choice: str) -> EmbeddedArt | None:
        if choice == Choice.ARCHIVE:
            return archive
        return picks.get(choice)

    honoured = {
        carrier: choice
        for carrier, choice in choices.items()
        if carrier in carriers and (choice == Choice.KEEP or image_of(choice) is not None)
    }
    if not honoured:
        return suggested

    def carrier_of(change: Change) -> str:
        return COVER if change.folder_cover else (change.before or GAP)

    # The suggestion's own changes, for the carriers nobody chose for — kept in
    # the suggestion's order, which is the album's.
    kept = {c.target: c for c in suggested.changes if carrier_of(c) not in honoured}
    images = dict(suggested.images)
    chosen: dict[Path, Change] = {}

    def take(carrier: str) -> EmbeddedArt | None:
        """The image `carrier` was chosen to take, or None (none, or keep)."""
        choice = honoured.get(carrier)
        image = image_of(choice) if choice is not None and choice != Choice.KEEP else None
        if image is not None:
            images[image.digest] = Incoming(image, Source.ARCHIVE)
        return image

    # Written only where the bytes differ: choosing the image a carrier
    # already holds is a no-op, not a rewrite (*Far & Off*, #659).
    for path, digest in before.items():
        image = take(digest or GAP)
        if image is not None and digest != image.digest:
            chosen[path] = Change(target=path, before=digest, after=image.digest)
    if (image := take(COVER)) is not None:
        if cover is None:
            target = album_dir / cover_name_for(image.mime)
            chosen[target] = Change(
                target=target, before=None, after=image.digest, folder_cover=True
            )
        elif cover.image.digest != image.digest:
            target = cover.path or album_dir / cover.name
            chosen[target] = Change(
                target=target,
                before=cover.image.digest,
                after=image.digest,
                folder_cover=True,
            )
    # Tracks in album order, then the folder cover, as `_plan_for` lays them out.
    changes = [
        c
        for path in before
        if (c := chosen.get(path) or kept.get(path)) is not None and not c.folder_cover
    ]
    changes += [c for c in (*kept.values(), *chosen.values()) if c.folder_cover]
    return ArtworkPlan(
        album_dir=album_dir,
        before=before,
        images={c.after: images[c.after] for c in changes},
        changes=tuple(changes),
        # Still true only while no track is being written: a row somebody chose
        # an image for is not being preserved, and the tagging's notice would
        # say it was (#260).
        preserves_per_track_art=suggested.preserves_per_track_art
        and all(c.folder_cover for c in changes),
        choices=honoured,
    )


def _suggest(
    album_dir: Path,
    tracks: Sequence[tuple[Path, EmbeddedArt | None]],
    cover: FolderCover | None,
    archive: EmbeddedArt | None = None,
    *,
    overwrite_art: bool = False,
    cover_unreadable: bool = False,
    chosen: Source | None = None,
    folder_cover: FolderCoverPolicy = FolderCoverPolicy.IF_MISSING,
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

    `chosen` opts out of it the other way (#472): the user picked a candidate,
    having looked at it, and it wins however it measures. Bigger is the only
    thing Harmonist can measure, and a 900px scan can be softer, worse cropped
    or a different pressing's sleeve than a 500px one — so the size rule is a
    default, not a verdict. A chosen image replaces the folder cover even when
    it is smaller, which is the one case the rule below would refuse. It still
    writes nothing where the bytes already match, and per-track artwork is still
    protected unless `overwrite_art` says otherwise.

    A missing folder cover is a target like any other, created from the winner
    (#457). What it is created FROM is the size rule's answer rather than a
    ladder of its own, so an album with a 3000px image in its tracks is no longer
    given a 1200px `cover.jpg` it then offers to replace.

    `folder_cover` decides whether that target exists at all (#516). Under
    `NEVER` no plan built here ever names a `cover.*` the album has not got, so
    the album page proposes nothing, the additions fingerprint covers nothing,
    and a tagging writes nothing — one answer rather than four places agreeing
    to hide the same row. A library whose albums carry embedded art and no
    `cover.jpg` is otherwise three hundred outstanding updates that are all the
    same update.

    It reaches only the CREATION. A folder cover that exists is weighed by the
    size rule either way, so turning the policy down never leaves an album's
    other artwork unimproved, and never touches a file.

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

    picked = archive if chosen is Source.ARCHIVE else None

    if has_per_track_art(before.values()):
        # The sleeves are user data either way. A chosen image still reaches the
        # folder cover — that carrier is not one of them — and replacing the
        # sleeves themselves needs `overwrite_art`, which is its own decision.
        candidate = picked if picked is not None else (archive if cover is None else None)
        # With no folder cover to write to, that carrier IS the whole plan —
        # the sleeves are protected and nothing else can take an image. So a
        # policy that will not create one leaves nothing to do, and saying so
        # here rather than dropping the change below keeps the view from
        # naming a winner that reaches no target (#516).
        if cover is None and folder_cover is FolderCoverPolicy.NEVER:
            candidate = None
        if candidate is not None:
            return _plan_for(
                album_dir,
                {},
                cover if picked is not None else None,
                candidate,
                Source.ARCHIVE,
                before=before,
                preserves=True,
                folder=folder_cover,
            )
        return ArtworkPlan(album_dir=album_dir, before=before, preserves_per_track_art=True)

    if picked is not None:
        return _plan_for(album_dir, before, cover, picked, Source.ARCHIVE, folder=folder_cover)

    own = next((art for _, art in tracks if art is not None), None)

    # WHAT THE TRACKS SHOULD CARRY: their own image, whenever they have one. A
    # larger folder cover does not displace it (#479) — `cover.*` is one file
    # and embedded art is one copy per track, so a high-resolution cover beside
    # modest embedded images is a layout somebody chose, not a gap to close.
    # With nothing embedded anywhere, a gap can only be filled from outside.
    track_image, track_source = (own, Source.ALBUM) if own is not None else (None, None)
    if track_image is None and cover is not None:
        track_image, track_source = cover.image, Source.FOLDER
    if archive is not None and track_image is None:
        track_image, track_source = archive, Source.ARCHIVE

    # WHAT THE FOLDER COVER SHOULD HOLD: the best image going (#276, #410).
    # One file, so improving it is cheap — this is the half of the album where
    # the size rule still decides, and ties keep what is already there.
    cover_image, cover_source = (cover.image, Source.FOLDER) if cover is not None else (None, None)
    for candidate, candidate_source in ((own, Source.ALBUM), (archive, Source.ARCHIVE)):
        if candidate is not None and (
            cover_image is None or beats(candidate.size, cover_image.size)
        ):
            cover_image, cover_source = candidate, candidate_source

    if track_image is None or track_source is None:
        return ArtworkPlan(album_dir=album_dir, before=before)
    return _plan_for(
        album_dir,
        before,
        cover,
        track_image,
        track_source,
        cover_image=cover_image,
        cover_source=cover_source,
        folder=folder_cover,
    )


def _plan_for(
    album_dir: Path,
    targets: Mapping[Path, str | None],
    cover: FolderCover | None,
    winner: EmbeddedArt,
    source: Source,
    *,
    before: Mapping[Path, str | None] | None = None,
    preserves: bool = False,
    cover_image: EmbeddedArt | None = None,
    cover_source: Source | None = None,
    folder: FolderCoverPolicy = FolderCoverPolicy.IF_MISSING,
) -> ArtworkPlan:
    """Every write that puts `winner` on `targets`, and `cover_image` on the
    folder cover.

    TWO IMAGES, because they are two kinds of carrier (#479). `cover_image`
    defaults to the tracks' image, which is the ordinary album; where they
    differ, the folder cover takes the better one and the tracks are left with
    theirs. Each is written only where the target does not already hold it —
    the selection is made by the caller, so a difference here IS the change.
    """
    for_cover = cover_image if cover_image is not None else winner
    for_cover_source = cover_source if cover_image is not None else source
    changes = [
        Change(target=path, before=digest, after=winner.digest)
        for path, digest in targets.items()
        if digest != winner.digest
    ]
    if cover is None:
        # The one place a folder cover is created, so the one place the policy
        # has to hold (#516). Dropped from the plan rather than filtered out of
        # a scope afterwards: the fingerprint, the row, the summary and the
        # write are all read off `changes`, and a change that is going to be
        # ignored by every one of them is a change that should never have been
        # named.
        if folder is not FolderCoverPolicy.NEVER:
            changes.append(
                Change(
                    target=album_dir / cover_name_for(for_cover.mime),
                    before=None,
                    after=for_cover.digest,
                    folder_cover=True,
                )
            )
    elif cover.image.digest != for_cover.digest:
        changes.append(
            Change(
                target=cover.path or album_dir / cover.name,
                before=cover.image.digest,
                after=for_cover.digest,
                folder_cover=True,
            )
        )
    images = {winner.digest: Incoming(winner, source)}
    if for_cover_source is not None:
        images.setdefault(for_cover.digest, Incoming(for_cover, for_cover_source))
    return ArtworkPlan(
        album_dir=album_dir,
        before=before if before is not None else targets,
        # Only what a change writes. An image in the table is a claim that
        # something is coming, and the view reads it as one — so a folder cover
        # the policy will not create has no incoming image either (#516).
        images={c.after: images[c.after] for c in changes},
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
class Candidate:
    """One image the picker offers (#659): listed by the Cover Art Archive for
    the release, its release group, or another release in that group.

    `image` is its original once it is in hand — fetched when it was chosen,
    or the release's own front the archive check downloaded — and None while
    it is only listed. Only an image in hand can be compared with what a row
    has, so only then can the picker know a Use would change nothing.
    """

    image_id: str
    #: Which listing offered it: `RELEASE`, `RELEASE_GROUP` or `OTHER_RELEASE`.
    origin: str
    front: bool
    types: tuple[str, ...]
    image: EmbeddedArt | None = None
    #: Which other release, for one that is: what tells two pressings apart —
    #: disambiguation, date, country, format.
    release: str | None = None
    #: The facts line from a measurement of the original, for one not in hand:
    #: measured the first time the picker showed it.
    measured: str | None = None

    #: The listings a candidate can come from.
    RELEASE: ClassVar[str] = "release"
    RELEASE_GROUP: ClassVar[str] = "release-group"
    OTHER_RELEASE: ClassVar[str] = "other-release"

    @property
    def meta(self) -> str | None:
        """The facts line, once there are facts: the original's size and
        type, off the original or a measurement of it. A thumbnail says
        nothing about the original."""
        return describe(self.image) if self.image is not None else self.measured


# No `ArchiveRow`: the archive's one front cover was drawn as a row of its own,
# with a "load" frame for a loser nobody had downloaded (#433, #448). Since
# #659 the picker offers every image the archive lists, each shown from its
# thumbnail — so there is no loser to load, and no single archive row.


#: The id of the Artwork row for tracks carrying no image at all.
GAP_ANCHOR = "art-row-none"


def row_anchor(digest: str | None) -> str:
    """The id of the Artwork row showing this image, or `GAP_ANCHOR` for none.

    ONE definition of the spelling, because two things agree on it across two
    files: the section renders the id and the tracklist's mark links to it
    (#404). A second spelling would be a link to nowhere — and a test of either
    side on its own would pass, since both halves would be individually correct.
    """
    return f"art-row-{digest[:12]}" if digest else GAP_ANCHOR


class ArtMark(StrEnum):
    """Why one track's artwork is worth pointing at from the tracklist (#404)."""

    #: Carries an image other than the one the rest of the album carries.
    OWN = "own"
    #: Carries no embedded image at all, on an album where other tracks do.
    GAP = "gap"


@dataclass(frozen=True)
class TrackMark:
    """A tracklist row's artwork mark, and the Artwork row it points at.

    The anchor travels WITH the mark rather than being rebuilt beside it, so a
    row can only ever link to a target `row_anchor` also named.
    """

    mark: ArtMark
    anchor: str


def marks(tracks: Sequence[tuple[str, TrackTags]]) -> dict[str, TrackMark]:
    """Which tracks' artwork the tracklist should point at, by file name (#404).

    `tracks` is the album's AUDIO as `compare` speaks it — `(file_name, tags)`,
    the names the tracklist's rows carry, so the caller does not have to join two
    different spellings of one path. Video takes no part: Harmonist never writes
    a video's tags and the artwork plan never targets one, so a `.m4v` has no
    artwork answer to point at. Neither does an unreadable file, which is #112's
    third state and says so in its own row.

    Two marks, and both mean "this track is not like the rest of the album":

    * **GAP** — no embedded image, on an album where something else has one. The
      case with an obvious remedy, and the one the Library's `has_cover` chip
      cannot see, since that is the folder cover or track 1.
    * **OWN** — an image other than the album's PREVAILING one: the digest a
      strict majority of the arted tracks share. Without a majority there is no
      "the album's artwork" to differ from — a compilation of twelve distinct
      covers is correct, and a mark on all twelve of them says nothing. Same rule
      the tracklist's columns are held to (#309): a mark earns its place by
      answering *which track*, or it is noise on every row.

    An album where NO track has an image gets no marks at all. That every track
    lacks one is a fact about the album, which the Artwork section states in a
    single row; repeating it against all twelve names no track in particular.

    Not read off `ArtworkView.rows`, though it is the same grouping by digest,
    and deliberately: the rows are built from the artwork PLAN, which a
    MusicBrainz re-read does not rebuild (#485) while it does re-render the
    tracklist. Marks derived from the view would vanish on a re-read, which reads
    as a bug and is invisible to a test that renders the page once. What must not
    drift between the two — the anchor — is `row_anchor`, shared.
    """
    readable = [(name, t) for name, t in tracks if not t.unreadable]
    counted = Counter(t.art.digest for _, t in readable if t.art is not None)
    if not counted:
        return {}
    top, carried = counted.most_common(1)[0]
    prevailing = top if carried * 2 > sum(counted.values()) else None
    found: dict[str, TrackMark] = {}
    for name, tags in readable:
        if tags.art is None:
            found[name] = TrackMark(ArtMark.GAP, GAP_ANCHOR)
        elif prevailing is not None and tags.art.digest != prevailing:
            found[name] = TrackMark(ArtMark.OWN, row_anchor(tags.art.digest))
    return found


@dataclass(frozen=True)
class ArtRow:
    """One distinct image, and everything carrying it.

    `image` is None on the row for tracks with no embedded art at all. That row is
    not a gap in the data — it is the finding, and the section draws it as an
    empty frame rather than omitting it, because an album where two tracks have
    lost their artwork looks exactly like a healthy one if you only draw what is
    there.

    `folder` is the folder cover's filename on the folder cover's own row, which
    every album has whether or not the file exists (#659). It used to share a
    row with the tracks carrying the same picture — "All 12 tracks and
    cover.jpg" (#400) — and that saved drawing one picture twice at the price of
    rows that merged and split as their fates diverged (#479). A row is what a
    choice is made on, and a row that split under the user the moment they
    chose for it was not something they could choose on.

    `total_tracks` and `multi_disc` are the album context the label needs. Carried
    on the row rather than passed to `label` so the template can't render two rows
    of one album against different totals.
    """

    image: EmbeddedArt | None
    tracks: tuple[TrackRef, ...]
    outcome: Outcome
    total_tracks: int = 0
    multi_disc: bool = False
    folder: str | None = None
    #: What a re-tag would put here, named — the folder cover for a track row,
    #: and the album's own artwork for the folder cover's row when that is the
    #: better image (#410). None where nothing is written.
    written_from: str | None = None
    #: Whether what would be written came from the Cover Art Archive (#276) —
    #: which is the one case where the MusicBrainz hexagon is a claim Harmonist
    #: can support. A folder cover may have come from a Bandcamp download.
    from_archive: bool = False
    #: …and whether that cover is the RELEASE GROUP's rather than this edition's
    #: (#434, #496). Carried on the incoming side, not only on the candidate row,
    #: because the candidate row stops existing the moment the image wins or is
    #: chosen — which is the moment a reader is approving it. A cover standing
    #: for every pressing of the album is a useful thing to take and a bad thing
    #: to mistake for this pressing's.
    from_release_group: bool = False
    #: …or another release's in the group (#659): a different pressing's sleeve,
    #: which may be exactly the one wanted and is still not this one's.
    from_other_release: bool = False
    #: …and the image itself, so the row can SHOW what it would become rather
    #: than only assert it (#413). "Replaced by the album's own artwork" carries
    #: no tense — a reader cannot tell whether it already happened — and the
    #: picture on the right under a dated heading is what settles that.
    written_image: EmbeddedArt | None = None
    #: The carriers a choice made on this row names (#659): its tracks' image
    #: digest or `GAP`, or `COVER` on the folder cover's row. A choice is made
    #: on the row, so it reaches everything the row stands for.
    carriers: tuple[str, ...] = ()
    #: This row's image IS the archive's, byte for byte — so choosing the
    #: archive's image here would write nothing, and the picker says so
    #: rather than offering it (*Far & Off*, #659).
    has_archive: bool = False
    #: …and the same of what this row is GETTING: the incoming image is the
    #: archive's bytes, whichever carrier the plan reads them from. An album's
    #: own image and the archive's are often the same file, and choosing the
    #: one over the other would change nothing but the label.
    takes_archive: bool = False

    @property
    def is_gap(self) -> bool:
        """The row for tracks with no embedded image."""
        return self.image is None and self.folder is None

    @property
    def absent(self) -> bool:
        """The folder cover's row, for a folder cover the album has not got.
        A gap like an artless track, and drawn as one: an absent carrier is the
        finding (#457)."""
        return self.folder is not None and self.image is None

    @property
    def anchor(self) -> str:
        """The id a tracklist mark links to (#404).

        The folder cover's row takes an id of its own rather than its image's: it
        usually carries the same picture as a row of tracks, and one page cannot
        hold two elements with one id. No mark points here — the cover is not a
        track — so the id is the section's alone, and it stays distinct.
        """
        if self.folder is not None:
            return "art-row-cover"
        return row_anchor(self.image.digest if self.image else None)

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
        if self.folder is not None:
            return self.folder
        n = len(self.tracks)
        return self._track_label(n) if n else "Not on any track"

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
    #: The candidate the USER chose, when this section is previewing an override
    #: rather than the size rule's answer (#472). None is the ordinary section.
    #: Set only where the choice could really be honoured, so the page never
    #: says it is showing an image it has not got.
    chosen: Source | None = None
    #: The choices made row by row that the plan honoured (#659). What the
    #: section's controls carry back, and what each of them changes by one row.
    choices: Mapping[str, str] = field(default_factory=dict)
    #: What the picker offers, in order: the release's images, then its release
    #: group's, then those of any other release in the group stepped to (#659).
    candidates: tuple[Candidate, ...] = ()
    #: Whether a step past the last candidate could find another release's
    #: images: decided by the caller, which knows what the page has browsed.
    other_releases: bool = False

    @property
    def front_only(self) -> bool:
        """Whether the picker opens showing fronts only — Picard's default —
        which it does unless there are none, when it would open on nothing."""
        return any(c.front for c in self.candidates)

    def unchanged(self, row: ArtRow, candidate: Candidate) -> bool:
        """Whether using `candidate` on `row` would change nothing: the row
        already has it, or is already getting it, byte for byte. Unknowable for
        an image not yet in hand, so False there — Use stays on offer, and the
        plan writes nothing if the bytes turn out to match."""
        if candidate.image is None:
            return False
        digest = candidate.image.digest
        return (row.image is not None and row.image.digest == digest) or (
            row.written_image is not None and row.written_image.digest == digest
        )

    @property
    def use(self) -> str:
        """The choices shown, spelled as a request carries them. A control
        that changes them sends what it changes alongside, and the route
        composes the next set (`album_artwork`) — one place deciding what a
        press means, rather than every button spelling out its outcome."""
        return format_choices(self.choices)

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
    def write_targets(self) -> str:
        """Name the reviewed plan's actual destinations, independent of image size."""
        changes = self.plan.changes if self.plan else ()
        tracks = sum(not change.folder_cover for change in changes)
        targets = []
        if tracks:
            targets.append(f"embedded artwork in {tracks} track{'' if tracks == 1 else 's'}")
        targets.extend(
            f"folder cover ({change.target.name})" for change in changes if change.folder_cover
        )
        return " and ".join(targets)

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

        Carried by the controls that only ADD — the partial-tag badge's "Write
        it to those files", and any tagging without a page in front of it — so a
        folder cover created or a gap filled is one this page showed (#469).

        NOT what **Apply updates** carries (#482). That control applies the plan
        in full, replacements included, so it sends `fingerprint` above. Sending
        this one would compare an additions digest against an all-scope plan and
        mismatch on every press, withholding the artwork and blaming a page that
        had not changed."""
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
        # …and every incoming image, wherever it came from. The incoming side of
        # every written row points at one, and when it is the archive's it is on
        # no row and no carrier, so nothing else above would emit its view.
        if self.plan is not None:
            for digest, incoming in self.plan.images.items():
                out.setdefault(
                    digest,
                    (
                        incoming.image,
                        _source_label(incoming.source, self.cover) or "the incoming image",
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
        improved = sum(len(r.tracks) for r in self.images if r.writes)
        creates = next((r.folder for r in self.rows if r.absent and r.writes), None)

        carriers = []
        if improved:
            carriers.append(f"{improved} track{'' if improved == 1 else 's'}")
        # The folder cover is its own row, and often the only thing changing.
        if self.cover is not None and any(r.writes and r.folder for r in self.images):
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
    def count(self) -> str | None:
        """The count beside the section heading — "3 images · 2 gaps", or None
        when there is nothing to count. Mirrors History's `· N`.

        The gaps are counted alongside because otherwise a heading reading
        "1 image" sits above an album where a third of the tracks have none —
        true, and quietly missing the point.
        """
        # Distinct pictures, not rows: the folder cover has a row of its own
        # and usually carries the tracks' picture (#659).
        n = len({r.image.digest for r in self.images if r.image is not None})
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
    chosen: Source | None = None,
    choices: Choices | None = None,
    picks: Mapping[str, EmbeddedArt] | None = None,
    candidates: Sequence[Candidate] = (),
    folder_cover: FolderCoverPolicy = FolderCoverPolicy.IF_MISSING,
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

    `chosen` is the user having picked a candidate over the size rule's answer
    (#472). Honoured only where the image is actually in hand, and recorded on
    the view as such: a section that said it was showing a chosen image it had
    not got would be describing a write nobody could make.

    `choices` is the same thing row by row (#659), under the same rule — the
    view carries only the choices the plan could honour.

    `folder_cover` is the user's policy on creating one the album lacks (#516).
    It is handed to the PLAN rather than applied to the rows, so the section
    never draws a row for a file the action would not write — the coupling #467
    went to some trouble to establish in the other direction.
    """
    readable = [(path, t) for path, t in tracks if not t.unreadable]
    taken = chosen if chosen is Source.ARCHIVE and archive is not None else None
    the_plan = plan(
        album_dir,
        [(path, t.art) for path, t in readable],
        cover,
        archive,
        cover_unreadable=cover_unreadable,
        chosen=taken,
        choices=choices,
        picks=picks,
        folder_cover=folder_cover,
    )
    by_target = {c.target: c for c in the_plan.changes}
    cover_change = the_plan.cover_change(Scope.ALL)
    cover_incoming = the_plan.image_for(cover_change) if cover_change is not None else None

    # The FIRST listing to name an image is whose it is: they come this
    # release's first, and the group very often lists the release's own front
    # again — which is no reason to call it anybody else's.
    listed_by: dict[str, str] = {}
    for c in candidates:
        if c.image is not None:
            listed_by.setdefault(c.image.digest, c.origin)

    def _from_group(digest: str) -> bool:
        """Whether an archive image is the release GROUP's (#434, #496): by
        where the picker found it, and for the one image no listing names —
        the release's front, measured before any listing was kept — by which
        listing answered the archive check. Read off the listings rather than
        the bytes, which carry no trace of it."""
        if digest in listed_by:
            return listed_by[digest] == Candidate.RELEASE_GROUP
        return caa is not None and caa.from_release_group

    def incoming_for(paths: Iterable[Path]) -> Incoming | None:
        """What a group of tracks becomes. One image for all of them: they
        are one carrier, and a choice or the size rule treats a carrier whole."""
        change = next((by_target[p] for p in paths if p in by_target), None)
        return the_plan.image_for(change) if change is not None else None

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
        incoming: Incoming | None,
        keys: tuple[str, ...],
        *,
        folder: str | None = None,
    ) -> ArtRow:
        # Each row shows ITS OWN incoming image (#479, #659). The folder cover
        # can be taking a better one than the tracks are, and a user choosing
        # row by row can give every row a different one, so nothing here is
        # read off a single album-wide winner.
        if incoming is not None:
            outcome = Outcome.FILLED if image is None else Outcome.REPLACED
        else:
            outcome = Outcome.SAME if folder is not None else Outcome.KEPT
        from_archive = incoming is not None and incoming.source is Source.ARCHIVE
        return ArtRow(
            image=image,
            tracks=tuple(ref for _, ref in carriers),
            outcome=outcome,
            total_tracks=len(tracks),
            multi_disc=multi_disc,
            folder=folder,
            written_from=_source_label(incoming.source, cover) if incoming else None,
            written_image=incoming.image if incoming else None,
            from_archive=from_archive,
            # Read off the stored answer rather than the image: which listing
            # replied is a fact about where the picture came from, and the bytes
            # carry no trace of it (#496).
            from_release_group=from_archive
            and incoming is not None
            and _from_group(incoming.image.digest),
            from_other_release=from_archive
            and incoming is not None
            and listed_by.get(incoming.image.digest) == Candidate.OTHER_RELEASE,
            carriers=keys,
            has_archive=archive is not None
            and image is not None
            and image.digest == archive.digest,
            takes_archive=archive is not None
            and incoming is not None
            and incoming.image.digest == archive.digest,
        )

    # One row per group of tracks sharing an image, then the tracks with none,
    # then the folder cover — each a single carrier, so a choice made on a row
    # reaches exactly that row and rows never merge or split under it (#659).
    rows = [
        row(art_of[digest], carriers, incoming_for(path for path, _ in carriers), (digest,))
        for digest, carriers in by_digest.items()
    ]
    if gap:
        rows.append(row(None, gap, incoming_for(path for path, _ in gap), (GAP,)))
    # The folder cover always has a row, whether or not the file exists (#659).
    # Present, it is an image the album HAS, and the reader deciding what
    # applying would do needs to see it beside the rest (#400). Absent, it is a
    # carrier the user can choose an image for — an empty frame like an
    # artless track's, with whatever the plan would create beside it (#457).
    #
    # Not when it exists and could not be read. That arrives here as no cover
    # at all, and a row saying "not in the folder" would be the opposite of
    # the truth (#112); the section says what really happened instead.
    if cover is not None:
        rows.append(row(cover.image, (), cover_incoming, (COVER,), folder=cover.name))
    elif not cover_unreadable:
        name = cover_change.target.name if cover_change is not None else "cover.jpg"
        rows.append(row(None, (), cover_incoming, (COVER,), folder=name))

    return ArtworkView(
        rows=tuple(rows),
        cover=cover,
        total_tracks=len(tracks),
        unreadable=len(tracks) - len(readable),
        caa=caa,
        archive=archive,
        cover_unreadable=cover_unreadable,
        plan=the_plan,
        chosen=taken,
        choices=the_plan.choices,
        candidates=tuple(candidates),
    )
