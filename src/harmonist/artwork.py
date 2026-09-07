"""What an album's artwork actually is, and what a re-tag would do to it (#155).

Pure functions over values, like `compare` and for the same reason: the album
page's Artwork section is a rendering of facts, and nothing here opens a file or
knows what a MusicBrainz release is. The caller reads; this decides what the
reading means.

The unit is the **distinct image**, not the track. Twelve tracks sharing one
cover is one thing to look at with twelve names on it, and a compilation with
four covers is four — which is also how the section is laid out: one row per
image, the tracks carrying it beside it, and what a re-tag would put there on
the right.

That last part is the reason this is worth building. Harmonist already decides,
per album, whether to embed the folder cover on every track or to leave per-track
artwork alone, and until now the only trace of that decision was a tooltip. The
`Outcome` on each row is that same decision, reached through the same predicate
the tagger uses, so the page and the button cannot disagree.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum

from .formats import TrackTags
from .formats.types import EmbeddedArt


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


class Outcome(StrEnum):
    """What a re-tag would do to one row's image.

    Four, because "what happens to your artwork" has four honest answers and
    collapsing any two of them misleads: an album whose art is preserved must not
    read like one whose art is about to be overwritten.
    """

    #: This image is replaced by the folder cover.
    REPLACED = "replaced"
    #: These tracks have no image; the folder cover fills the gap.
    FILLED = "filled"
    #: Per-track artwork Harmonist preserves. Nothing is written.
    KEPT = "kept"
    #: Already the folder cover. The write happens and changes nothing.
    SAME = "same"


@dataclass(frozen=True)
class TrackRef:
    """One track, as the section needs to name it."""

    name: str
    track_num: int | None = None
    disc_num: int | None = None


@dataclass(frozen=True)
class FolderCover:
    """The `cover.*` beside the audio — what a re-tag would embed.

    Named separately from the tracks' own art because it is answering a different
    question: not "what do your files carry" but "what is about to be written to
    them". It is also what most players read off disk, so an album whose folder
    cover disagrees with its tracks looks like two different albums depending on
    which player opened it.
    """

    name: str
    image: EmbeddedArt


@dataclass(frozen=True)
class ArtRow:
    """One distinct image, and everyone carrying it.

    `image` is None on the row for tracks with no embedded art at all. That row is
    not a gap in the data — it is the finding, and the section draws it as an
    empty frame rather than omitting it, because an album where two tracks have
    lost their artwork looks exactly like a healthy one if you only draw what is
    there.
    """

    image: EmbeddedArt | None
    tracks: tuple[TrackRef, ...]
    outcome: Outcome

    @property
    def is_gap(self) -> bool:
        return self.image is None

    def label(self, total: int) -> str:
        """Who carries this image, in the fewest words that stay true.

        "All 12 tracks" when it is all of them — the common case, and a list of
        twelve numbers would say the same thing worse. Otherwise the numbers
        themselves, collapsed into ranges, because "tracks 3, 7" is what tells the
        user which files to look at. A track with no number falls back to a count:
        an unnumbered file cannot be pointed at by position.
        """
        n = len(self.tracks)
        if n == total:
            return f"All {total} tracks" if total != 1 else "The only track"
        numbers = [t.track_num for t in self.tracks]
        if any(num is None for num in numbers):
            return f"{n} track" if n == 1 else f"{n} tracks"
        spans = _ranges(sorted(num for num in numbers if num is not None))
        return f"Track {spans}" if n == 1 else f"Tracks {spans}"


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
class Disc:
    """A disc's rows, when every image belongs to exactly one disc."""

    number: int
    rows: tuple[ArtRow, ...]
    tracks: int


@dataclass(frozen=True)
class ArtworkView:
    """Everything the Artwork section shows about one album."""

    rows: tuple[ArtRow, ...]
    cover: FolderCover | None
    total_tracks: int
    #: Tracks that could not be read at all. They vote for nothing — a file
    #: Harmonist cannot open carries no evidence about artwork, and counting it
    #: as artless would report a mount that blinked as an album that lost its
    #: covers (#112).
    unreadable: int = 0
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

        The section's headings hang off this: with nothing to write there is no
        second column to name, and "After a re-tag" over an empty half promises a
        change that is not coming.
        """
        return any(r.outcome in (Outcome.REPLACED, Outcome.FILLED) for r in self.rows)

    @property
    def discs(self) -> tuple[Disc, ...]:
        """The rows grouped by disc, or empty when that grouping isn't true.

        A box set with per-disc artwork is a dozen images that read as a wall
        unless the discs carry the structure. But only when every image really
        does belong to one disc: an image spanning two discs, split under two
        headings, would report one cover as two.
        """
        if len({t.disc_num for r in self.rows for t in r.tracks} - {None}) < 2:
            return ()
        groups: dict[int, list[ArtRow]] = {}
        for row in self.rows:
            discs = {t.disc_num for t in row.tracks}
            if len(discs) != 1 or None in discs:
                return ()
            groups.setdefault(discs.pop(), []).append(row)  # type: ignore[arg-type]
        return tuple(
            Disc(number=n, rows=tuple(rows), tracks=sum(len(r.tracks) for r in rows))
            for n, rows in sorted(groups.items())
        )

    @property
    def verdict(self) -> str:
        """The section's one line, stating what is there.

        Written here rather than in the template for the reason
        `Consensus.odd_summary` is: it is the sentence a user reads to decide
        whether anything is wrong, and it has six shapes. Scattering those across
        a Jinja `{% if %}` chain puts the wording where nobody reviews it.

        No alarm anywhere in it. An album assembled over decades having uneven
        artwork is normal, and a compilation having four covers is correct.
        """
        images = self.images
        gaps = sum(len(r.tracks) for r in self.rows if r.is_gap)
        carried = self.total_tracks - gaps - self.unreadable

        if self.cover_unreadable and not images:
            return "Harmonist couldn't read this album's folder cover."
        if not images and self.cover is None:
            return "No artwork. There is no cover file, and no track carries an embedded image."
        if not images:
            name = self.cover.name if self.cover else "cover"
            return f"{name} only. No track carries an embedded image."
        if len(images) > 1:
            return f"{self.total_tracks} tracks, {len(images)} different images."
        if gaps:
            return (
                f"{carried} of {self.total_tracks} tracks carry artwork. "
                f"{gaps} {'has' if gaps == 1 else 'have'} none."
            )
        if self.cover and self.cover.image.digest != images[0].image.digest:  # type: ignore[union-attr]
            return "Your tracks carry a different image from the folder cover."
        where = f" and in {self.cover.name}" if self.cover else ""
        return f"One image, on every track{where}."

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
            out.setdefault(row.image.digest, (row.image, row.label(self.total_tracks)))
        if self.cover is not None:
            out.setdefault(self.cover.image.digest, (self.cover.image, self.cover.name))
        return tuple(out.values())

    @property
    def count(self) -> str | None:
        """The count beside the section heading — "3 images · 2 gaps", or None
        when there is nothing to count. Mirrors History's `· N`.

        Counts what the TRACKS carry, never the folder cover: the section is
        about the album's own artwork, and the cover is what would be written
        over it. The gaps are counted alongside because otherwise a heading
        reading "1 image" sits above an album where a third of the tracks have
        none — true, and quietly missing the point.
        """
        n = len(self.images)
        gaps = sum(1 for r in self.rows if r.is_gap for _ in r.tracks)
        if not n:
            return None
        images = "1 image" if n == 1 else f"{n} images"
        return f"{images} · {gaps} gap{'' if gaps == 1 else 's'}" if gaps else images


def describe(image: EmbeddedArt) -> str:
    """One image as a line of facts: "1400×1400 · JPEG · 718 KB".

    The dimensions come first because they are what the question is usually
    about — whether a re-tag is an improvement or a downgrade is decided there —
    and they are simply absent when the header could not be read, rather than
    guessed at (see `images.dimensions`).

    KB and MB in the units a user recognises off a file listing, not bytes.
    """
    size = f"{image.size.width}×{image.size.height} · " if image.size else ""
    kind = image.mime.removeprefix("image/").upper()
    kb = image.length / 1024
    weight = f"{kb / 1024:.1f} MB" if kb >= 1024 else f"{kb:.0f} KB"
    return f"{size}{kind} · {weight}"


def summarise(
    tracks: Sequence[tuple[str, TrackTags]],
    cover: FolderCover | None,
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
    one distinct image, so the preservation guard does not fire and the ten
    correct images are rewritten to fill the two gaps. The section states it
    plainly rather than hiding it — see the issue for what the fix costs.
    """
    readable = [(name, t) for name, t in tracks if not t.unreadable]
    by_digest: dict[str, list[TrackRef]] = {}
    art_of: dict[str, EmbeddedArt] = {}
    gap: list[TrackRef] = []

    for name, tags in readable:
        ref = TrackRef(name=name, track_num=tags.track_num, disc_num=tags.disc_num)
        if tags.art is None:
            gap.append(ref)
            continue
        by_digest.setdefault(tags.art.digest, []).append(ref)
        art_of.setdefault(tags.art.digest, tags.art)

    preserved = cover is None or has_per_track_art(by_digest)

    rows = [
        ArtRow(
            image=art_of[d],
            tracks=tuple(refs),
            outcome=(
                Outcome.KEPT
                if preserved
                else Outcome.SAME
                if cover is not None and cover.image.digest == d
                else Outcome.REPLACED
            ),
        )
        for d, refs in by_digest.items()
    ]
    if gap:
        rows.append(
            ArtRow(
                image=None,
                tracks=tuple(gap),
                outcome=Outcome.KEPT if preserved else Outcome.FILLED,
            )
        )

    return ArtworkView(
        rows=tuple(rows),
        cover=cover,
        total_tracks=len(tracks),
        unreadable=len(tracks) - len(readable),
    )
