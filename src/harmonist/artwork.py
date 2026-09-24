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
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Protocol

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
    #: The image the TRACKS should carry, and where it comes from. None when
    #: nothing is written to them.
    winner: EmbeddedArt | None = None
    source: Source | None = None
    #: …and the folder cover's own, which may be a different and larger image
    #: (#479). `cover.*` is one file; embedded art is one copy per track, so a
    #: library can sensibly keep a high-resolution cover beside modest embedded
    #: images. Defaults to the tracks' image, which is the ordinary case.
    cover_image: EmbeddedArt | None = None
    cover_source: Source | None = None
    changes: tuple[Change, ...] = ()
    #: The tracks carry differing images and are being left alone — a decision
    #: worth reporting rather than a silent no-op (#260).
    preserves_per_track_art: bool = False

    def image_for(self, change: Change) -> tuple[EmbeddedArt | None, Source | None]:
        """The image one change writes, and where it comes from.

        The folder cover has its own (#479); every other target takes the
        tracks' image. Asked by the writer and by the page alike, so a row
        cannot show one image while the write puts another there.
        """
        if change.folder_cover:
            return self.cover_image, self.cover_source
        return self.winner, self.source

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
    # No folder cover, and none coming: the album has no such carrier, so it has
    # no incoming image either (#516). Left as the winner it would say the
    # archive was supplying a `cover.jpg` that is not going to exist —
    # `archive_on_cover` reads exactly this, and the section draws its candidate
    # row off that answer.
    no_cover = cover is None and folder is FolderCoverPolicy.NEVER
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
    return ArtworkPlan(
        album_dir=album_dir,
        before=before if before is not None else targets,
        winner=winner,
        source=source,
        cover_image=None if no_cover else for_cover,
        cover_source=None if no_cover else for_cover_source,
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
    #: Its presence says nothing about whether it won. Winning is decided on
    #: size and is not consulted here, so a loaded loser stays exactly what it
    #: was: muted, unmarked, and not going to be written (#441).
    image: EmbeddedArt | None = None
    #: This image is ALREADY coming to the folder cover, and the row is here to
    #: offer it to the tracks as well (#490). "Also considered" is past tense and
    #: would be untrue of it, so the heading says so instead — the row is not an
    #: also-ran, it is a candidate for the half of the album it is not going to.
    also_coming: bool = False

    @property
    def loadable(self) -> bool:
        """Whether there is a picture to go and get: the archive has one, and it
        is not here yet. False for a release the archive holds nothing for —
        there is nothing to load, which is a different fact from not having
        loaded it (#433)."""
        return self.image is None and self.placeholder != "none"


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
    #: …and whether that cover is the RELEASE GROUP's rather than this edition's
    #: (#434, #496). Carried on the incoming side, not only on the candidate row,
    #: because the candidate row stops existing the moment the image wins or is
    #: chosen — which is the moment a reader is approving it. A cover standing
    #: for every pressing of the album is a useful thing to take and a bad thing
    #: to mistake for this pressing's.
    from_release_group: bool = False
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
    def anchor(self) -> str:
        """The id a tracklist mark links to (#404).

        The folder cover's OWN row takes an id of its own rather than its image's:
        when the cover is about to diverge from the tracks it gets a second row
        carrying the same picture as theirs (#479), and one page cannot hold two
        elements with one id. No mark points here — the cover is not a track — so
        the id is the section's alone, and it stays distinct.
        """
        if self.creates or (self.on_cover is not None and not self.tracks):
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
    #: Whether the archive's cover is what the TRACKS are getting — the case
    #: where it is wholly the incoming value rather than an also-ran (#433).
    #:
    #: Split from the folder cover's own source (#490). One flag meaning "the
    #: archive supplies either destination" read as a verdict on both, so an
    #: album whose tracks keep their image while `cover.jpg` takes the archive's
    #: (the ordinary #479 outcome) counted as a clean win — and the candidate
    #: row vanished, taking the only **Use this artwork** override with it.
    archive_on_tracks: bool = False
    #: …and whether it is what the FOLDER COVER is getting, which is a different
    #: question and the common one.
    archive_on_cover: bool = False
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
        creates = next((r.creates for r in self.rows if r.creates and r.writes), None)

        carriers = []
        if improved:
            carriers.append(f"{improved} track{'' if improved == 1 else 's'}")
        # The folder cover counts whether it shares a row with the tracks or has
        # one of its own — since #479 it usually has one of its own, because it
        # is often the only thing changing.
        if self.cover is not None and any(
            r.writes and (r.on_cover or (r.image is not None and not r.tracks)) for r in self.images
        ):
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

        None when the archive's cover is going ONTO THE TRACKS: it is then the
        incoming value everywhere it could be, and appears in that column with
        the hexagon, which is where purple belongs.

        Not when it merely wins the folder cover (#490). That is the ordinary
        #479 outcome — one high-resolution `cover.jpg` beside modest embedded
        art — and the tracks are keeping what they carry, so the archive's image
        is still a candidate for them. This row is the only place **Use this
        artwork** lives, and suppressing it there left the deliberate choice
        reachable only by applying the folder-only change and coming back.

        "Not what would be written" covers two cases and deliberately treats
        them alike: a cover that is smaller than the album's own, and one that
        is bigger but was never downloaded. The second is not a hole in the
        model — the tagger reads the same cache, so an image that is not there
        cannot be written whatever its measurement says, and a row stating its
        size is the honest account of it.
        """
        answer = self.caa
        if answer is None or self.archive_on_tracks:
            return None
        if not answer.has_art:
            # A different word, because it is a different fact: there is nothing
            # to load, rather than something not loaded (#433).
            return ArchiveRow(placeholder="none", meta="no front cover for this release")
        size = Size(answer.width, answer.height) if answer.width and answer.height else None
        if size is None:
            return ArchiveRow(
                placeholder="not loaded",
                meta="size could not be read",
                image=self.archive,
                also_coming=self.archive_on_cover,
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
            # line at all means the archive's cover is not going onto the tracks
            # — so an image here is either one a user fetched to look at, or the
            # folder cover's incoming image offered to the tracks as well (#490).
            image=self.archive,
            also_coming=self.archive_on_cover,
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
    chosen: Source | None = None,
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
        folder_cover=folder_cover,
    )
    written = {c.target for c in the_plan.changes}
    cover_change = the_plan.cover_change(Scope.ALL)
    # The two destinations, asked separately (#490). "Is the archive's image
    # coming anywhere" was one flag, and it decided whether the candidate row —
    # which carries the only explicit override — was drawn at all. An album whose
    # tracks keep their own image while `cover.jpg` takes the archive's is the
    # ORDINARY #479 outcome, and it hid the row on exactly the albums where the
    # user most needs it: the image is there, better than what the tracks carry,
    # and the size rule deliberately will not put it in them.
    archive_on_tracks = the_plan.source is Source.ARCHIVE
    archive_on_cover = the_plan.cover_source is Source.ARCHIVE

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
        folder: bool = False,
    ) -> ArtRow:
        if writes:
            outcome = Outcome.FILLED if image is None else Outcome.REPLACED
        else:
            outcome = Outcome.SAME if on_cover is not None else Outcome.KEPT
        # Each carrier shows ITS OWN incoming image (#479). The folder cover can
        # be taking a better one than the tracks are — that is the whole point
        # of the asymmetry — so a row drawn from one shared winner would show
        # the wrong picture on one side of it.
        incoming, incoming_source = (
            (the_plan.cover_image, the_plan.cover_source)
            if folder
            else (the_plan.winner, the_plan.source)
        )
        return ArtRow(
            image=image,
            tracks=tuple(ref for _, ref in carriers),
            outcome=outcome,
            total_tracks=len(tracks),
            multi_disc=multi_disc,
            on_cover=on_cover,
            written_from=_source_label(incoming_source, cover) if writes else None,
            written_image=incoming if writes else None,
            from_archive=writes and incoming_source is Source.ARCHIVE,
            # Read off the stored answer rather than the image: which listing
            # replied is a fact about where the picture came from, and the bytes
            # carry no trace of it (#496).
            from_release_group=(
                writes
                and incoming_source is Source.ARCHIVE
                and caa is not None
                and caa.from_release_group
            ),
            creates=creates,
        )

    def cover_written(digest: str) -> bool:
        return cover_change is not None and cover_change.before == digest

    rows = []
    for digest, carriers in by_digest.items():
        # The folder cover shares this row only while it shares this row's FATE
        # (#479). Since the tracks and the cover can be taking different images
        # — the tracks keeping theirs while the cover takes a better one — a row
        # reading "All 12 tracks and cover.jpg" would have to show two incoming
        # pictures at once. When they diverge the cover gets its own row below.
        on_cover = (
            cover.name
            if cover is not None and cover.image.digest == digest and cover_change is None
            else None
        )
        rows.append(
            row(
                art_of[digest],
                carriers,
                any(path in written for path, _ in carriers),
                on_cover=on_cover,
            )
        )
    # The folder cover is a carrier too, and gets a row of its own when no track
    # already accounts for it (#400) — the album HAS this image, whatever the
    # tracks carry, and a reader deciding what applying would do needs to see it
    # beside the rest rather than only as something arriving from outside.
    if cover is not None and (cover.image.digest not in by_digest or cover_change is not None):
        existing: FolderCover = cover
        rows.append(
            row(
                existing.image,
                (),
                cover_written(existing.image.digest),
                on_cover=existing.name,
                folder=True,
            )
        )
    # …and when there is no folder cover and the plan makes one, that is a row
    # too: an empty frame on the left, the image it will hold on the right. It
    # was a sentence while nothing but a tagging could create it, because a row
    # counts toward the section's action and that action could not (#457).
    if cover is None and cover_change is not None:
        rows.append(row(None, (), True, creates=cover_change.target.name, folder=True))
    if gap:
        rows.append(row(None, gap, any(path in written for path, _ in gap)))

    return ArtworkView(
        rows=tuple(rows),
        cover=cover,
        total_tracks=len(tracks),
        unreadable=len(tracks) - len(readable),
        caa=caa,
        archive_on_tracks=archive_on_tracks,
        archive_on_cover=archive_on_cover,
        archive=archive,
        cover_unreadable=cover_unreadable,
        plan=the_plan,
        chosen=taken,
    )
