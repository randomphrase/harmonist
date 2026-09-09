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

Each row also says what a re-tag would do to it — the decision `tagger._prepare`
already makes and used to keep to itself, reached here through the same
predicate so the page and the button cannot disagree. Rows where nothing is
written say nothing at all: agreement is silence everywhere else on this page,
and a column of "left as is" on a 37-image box set is 37 boxes of noise.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
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


class Outcome(StrEnum):
    """What a re-tag would do to one row's image.

    Only the first two reach the page. The other two are the two ways of writing
    nothing, kept apart because they are different facts — one is a protection,
    the other a no-op — and a caller asking "why is nothing happening here"
    deserves the real answer.
    """

    #: This image is replaced by the folder cover.
    REPLACED = "replaced"
    #: These tracks have no image; the folder cover fills the gap.
    FILLED = "filled"
    #: Per-track artwork Harmonist preserves.
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

    @property
    def is_gap(self) -> bool:
        return self.image is None

    @property
    def writes(self) -> bool:
        """Whether a re-tag would put something here. The rows that answer False
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
    #: A folder cover exists but could not be read. NOT the same as having none,
    #: and the difference is the whole right-hand column: with the cover unread
    #: there is no saying what a re-tag would write, so the section states that
    #: rather than showing outcomes derived from a file it never saw.
    cover_unreadable: bool = False

    @property
    def images(self) -> tuple[ArtRow, ...]:
        """The rows that actually have an image — the gap row excluded."""
        return tuple(r for r in self.rows if r.image is not None)

    @property
    def writes(self) -> bool:
        """Whether a re-tag would write anything at all.

        The column headings hang off this: with nothing to write there is no
        second column to name, and "After a re-tag" over an empty half promises a
        change that is not coming.
        """
        return any(r.writes for r in self.rows)

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
        return tuple(out.values())

    @property
    def summary(self) -> str:
        """One line for the top of the page (#417) — what updating the artwork
        would do, in the terms the rows use.

        Written here rather than in the template for the reason the verdict was:
        it has several shapes, and a Jinja `{% if %}` chain is where wording goes
        to stop being reviewed.
        """
        filled = sum(len(r.tracks) for r in self.rows if r.is_gap and r.writes)
        replaced = sum(1 for r in self.images if r.writes)
        parts = []
        if filled:
            parts.append(f"{filled} track{'' if filled == 1 else 's'} missing artwork")
        if replaced:
            parts.append(f"{replaced} image{'' if replaced == 1 else 's'} that could be better")
        if not parts:
            return "This album's artwork can be updated."
        return f"This album has {' and '.join(parts)}."

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
            return ArchiveRow(placeholder="not loaded", meta="size could not be read")
        return ArchiveRow(
            placeholder="not loaded",
            meta=describe_parts(size, answer.mime, answer.length),
            # Whose cover it is, when that is not this edition's (#434). A
            # release group's artwork stands for every edition of the album, and
            # someone comparing editions may care that this one is the general
            # one rather than their pressing's.
            from_release_group=answer.from_release_group,
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


def summarise(
    tracks: Sequence[tuple[str, TrackTags]],
    cover: FolderCover | None,
    caa: CoverArtAnswer | None = None,
    archive: EmbeddedArt | None = None,
) -> ArtworkView:
    """Everything the section shows, from tags already read and a folder cover.

    `tracks` is `(file_name, tags)` in track order — the same shape the album
    comparison takes, and taken in order for the same reason: the rows come out
    in the order the album plays.

    The outcomes mirror `tagger._prepare` exactly:

    - no folder cover, or per-track artwork present → nothing is written, every
      row is KEPT;
    - otherwise the folder cover goes onto every track, so a row already carrying
      it is SAME, a row carrying something else is REPLACED, and the row carrying
      nothing is FILLED.

    That third case is #397: an album that is uniform except for a hole in it has
    one distinct image, so the preservation guard does not fire and the correct
    images are rewritten to fill the gaps. The section states it plainly rather
    than hiding it — see the issue for what the fix costs.
    """
    readable = [(name, t) for name, t in tracks if not t.unreadable]
    by_digest: dict[str, list[TrackRef]] = {}
    art_of: dict[str, EmbeddedArt] = {}
    gap: list[TrackRef] = []

    for name, tags in readable:
        ref = TrackRef(
            name=name, track_num=tags.track_num, disc_num=tags.disc_num, title=tags.title
        )
        if tags.art is None:
            gap.append(ref)
            continue
        by_digest.setdefault(tags.art.digest, []).append(ref)
        art_of.setdefault(tags.art.digest, tags.art)

    # Per-track artwork is user data, and nothing overwrites it — not the folder
    # cover, not the archive, however large. Its own name because it is its own
    # rule: `preserved` below adds "and there is nothing to write anyway", which
    # is a different fact and used to be conflated with this one (#442).
    per_track = has_per_track_art(by_digest)
    # More than one disc is a property of the ALBUM, not of a row: a box set
    # whose images each sit on one disc still needs every label to name its disc,
    # or two discs' track 1 read as one repeated row (#400).
    multi_disc = len({t.disc_num for _, t in readable} - {None}) > 1

    def row(
        image: EmbeddedArt | None,
        refs: tuple[TrackRef, ...],
        outcome: Outcome,
        on_cover: str | None = None,
        written_from: str | None = None,
        written_image: EmbeddedArt | None = None,
    ) -> ArtRow:
        return ArtRow(
            image=image,
            tracks=refs,
            outcome=outcome,
            total_tracks=len(tracks),
            multi_disc=multi_disc,
            on_cover=on_cover,
            written_from=written_from,
            written_image=written_image,
            from_archive=written_image is not None and archive_wins,
        )

    def carried_by_cover(digest: str) -> str | None:
        return cover.name if cover is not None and cover.image.digest == digest else None

    def _unchanged(digest: str) -> bool:
        """Whether this row's tracks keep what they have — preserved per-track
        art, an album image that already won, or art that already IS the
        winner."""
        if preserved or keep_ours:
            return True
        return bool(carried_by_cover(digest)) and not archive_wins

    # The album's own image may be the better one, in which case the folder file
    # is what changes and the tracks are left alone (#410). Asked through the
    # same `beats` the tagger asks, so the page cannot promise a different
    # outcome from the one the button produces.
    album_image = next(iter(art_of.values())) if len(art_of) == 1 else None
    ours = album_image.size if album_image else None
    theirs = cover.image.size if cover else None

    # The best image the album ALREADY has, from either carrier — what a
    # candidate from outside has to beat. An image that cannot be measured
    # loses, exactly as `beats` reads a None.
    #
    # A folder cover that cannot be measured is the exception, and makes the
    # answer unknown rather than "the tracks' one": something is there, its size
    # is not, and leaving it alone is the safe reading of that.
    album_best = (
        None
        if cover is not None and theirs is None
        else ours
        if theirs is None or (ours is not None and beats(ours, theirs))
        else theirs
    )

    # The archive is the third candidate, and it displaces both when it beats
    # them (#276). It needs NO FOLDER COVER to be one: it is an image from
    # outside the album, and the folder file is not what carries it — requiring
    # one ruled the archive out on exactly the albums #276 said the wins were in,
    # art embedded in the files and no cover.jpg beside them (#442).
    #
    # Asked in the same order and by the same `beats` as `tagger.decide_artwork`,
    # because this is the page saying what that code will do.
    archive_wins = not per_track and archive is not None and beats(archive.size, album_best)

    # Nothing is written when the album's own artwork is protected, or when
    # there is nothing to write from: the folder cover normally, and the archive
    # when it has beaten everything.
    preserved = per_track or (cover is None and not archive_wins)

    # What goes on the tracks: the folder cover has to actually be better to be
    # written over what the album already carries, and both sizes have to be
    # readable for that to be established at all (#397, #410).
    keep_ours = (
        not preserved
        and album_image is not None
        and ours is not None
        and theirs is not None
        and not beats(theirs, ours)
    )
    # …and the folder file catches up only when ours is strictly better (#410).
    promote = keep_ours and beats(ours, theirs)
    #: What fills a track carrying nothing — the winner, whichever that is.
    fills_gaps = "the album's own artwork" if keep_ours else (cover.name if cover else None)
    #: …and the image those words name.
    gap_image = album_image if keep_ours else (cover.image if cover else None)

    if archive_wins:
        assert archive is not None  # narrowed by `archive_wins`
        keep_ours = False
        promote = archive.digest != cover.image.digest if cover else False
        fills_gaps = "the Cover Art Archive"
        gap_image = archive

    rows = [
        row(
            art_of[digest],
            tuple(refs),
            Outcome.KEPT
            if preserved or keep_ours
            else Outcome.SAME
            if carried_by_cover(digest) and not archive_wins
            else Outcome.REPLACED,
            on_cover=carried_by_cover(digest),
            # What this row would become, named and shown. `fills_gaps` is the
            # winner whatever it turned out to be — the folder cover, or the
            # archive when it beat everything — so the row cannot name one and
            # be written the other.
            written_from=None if _unchanged(digest) else fills_gaps,
            written_image=None if _unchanged(digest) else gap_image,
        )
        for digest, refs in by_digest.items()
    ]
    # The folder cover is a carrier too, and gets a row of its own when no track
    # already accounts for it (#400) — the album HAS this image, whatever the
    # tracks carry, and a reader deciding what a re-tag would do needs to see it
    # beside the rest rather than only as something arriving from outside.
    if cover is not None and cover.image.digest not in by_digest:
        rows.append(
            row(
                cover.image,
                (),
                Outcome.REPLACED if promote else Outcome.SAME,
                on_cover=cover.name,
                # Named as the thing it is rather than by a filename: what
                # replaces the folder cover is the album's own image or the
                # archive's, neither of which is a file in this album.
                written_from=fills_gaps if promote else None,
                written_image=gap_image if promote else None,
            )
        )
    # A gap is filled whenever there is anything to fill it with — including
    # when the tracks' own image is the one being kept (#397). "Left alone" is
    # the right answer for a track that has art; for one that has none it just
    # means left empty.
    if gap:
        fillable = not preserved and fills_gaps is not None
        rows.append(
            row(
                None,
                tuple(gap),
                Outcome.FILLED if fillable else Outcome.KEPT,
                written_from=fills_gaps if fillable else None,
                written_image=gap_image if fillable else None,
            )
        )

    return ArtworkView(
        rows=tuple(rows),
        cover=cover,
        total_tracks=len(tracks),
        unreadable=len(tracks) - len(readable),
        caa=caa,
        archive_wins=archive_wins,
    )
