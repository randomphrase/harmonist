"""Where an album's files came from: the owner's choice, otherwise evidence.

Only positive evidence counts. A store's own tags establish a download and
AccurateRip's establish a CD rip; a UPC on its own establishes neither (CD
rippers write one too), nor does hi-res audio (so is a vinyl or Blu-ray rip) or
a CD table of contents (XLD synthesizes one on any transcode).
docs/design/tagging.md keeps the table of what each tag proves.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from . import compare
from .formats.types import ProvenanceTags

if TYPE_CHECKING:
    from .models import Album


class Origin(StrEnum):
    """Where an album's files came from, as its page names it.

    The files, not the purchase: a CD bought from Bandcamp and ripped is a CD,
    since nothing of the purchase travels with the rip."""

    BANDCAMP = "Bandcamp"
    QOBUZ = "Qobuz"
    AMAZON = "Amazon"
    BEATPORT = "Beatport"
    CD = "CD"
    UNKNOWN = "Unknown"


#: The origins that are a store's download.
STORES = frozenset({Origin.BANDCAMP, Origin.QOBUZ, Origin.AMAZON, Origin.BEATPORT})


def stores(tags: ProvenanceTags) -> frozenset[Origin]:
    """The stores this one file's tags name, each by the mark it leaves.

    Not Bandcamp: its evidence is a URL in the comment (`url_recovery`), which
    names the release as well as the store, and the scan keeps it as such."""
    found = {Origin.QOBUZ} if tags.qobuz_track_id else set()
    for comment in tags.comments:
        text = comment.strip()
        if text.startswith("Amazon.com Song ID:"):
            found.add(Origin.AMAZON)
        elif text.casefold() == "purchased at beatport.com":
            found.add(Origin.BEATPORT)
    return frozenset(found)


def _evidence(album: Album) -> frozenset[Origin]:
    """Every origin some file's tags point to."""
    found = set(album.download_stores)
    if album.bandcamp_comment_urls:
        found.add(Origin.BANDCAMP)
    if album.ripped:
        found.add(Origin.CD)
    return frozenset(found)


def _harmonist_download(album: Album) -> bool:
    return album.sidecar is not None and album.sidecar.bandcamp_downloaded


def origin(album: Album) -> Origin:
    """The owner's choice, otherwise Harmonist's download record or tag evidence.

    Without a choice or download record, conflicting or absent tag evidence
    means Unknown."""
    if album.sidecar is not None and album.sidecar.origin_override is not None:
        return album.sidecar.origin_override
    if _harmonist_download(album):
        return Origin.BANDCAMP
    found = _evidence(album)
    return next(iter(found)) if len(found) == 1 else Origin.UNKNOWN


def _why(album: Album, found: Origin) -> str:
    """What `found` rests on, for its tooltip."""
    if album.origin_override_conflict:
        return "Your origin choices differ between this album's folders."
    if album.sidecar is not None and album.sidecar.origin_override is not None:
        return "You selected this origin."
    if found is Origin.BANDCAMP and _harmonist_download(album):
        return "Harmonist downloaded it from Bandcamp."
    if found is Origin.UNKNOWN:
        if _evidence(album):
            return "Its tags point to more than one origin."
        return "None of its tags says where the files came from."
    return {
        Origin.BANDCAMP: "Its comment carries a Bandcamp URL.",
        Origin.QOBUZ: "Its files carry Qobuz track IDs (QBZ:TID).",
        Origin.AMAZON: "Its comment carries an Amazon.com song ID.",
        Origin.BEATPORT: "Its comment says it was purchased at Beatport.",
        Origin.CD: "Its AccurateRip tags verify a rip of a physical CD.",
    }[found]


@dataclass(frozen=True)
class TagRow:
    """One tag as the album's tracks carry it: the same consensus the Album
    section shows for the tags Harmonist writes, so a disagreement reads the
    same in both — except when every track carries its own value."""

    tag: str
    consensus: compare.Consensus
    #: Every track's value, in track order.
    tracks: tuple[tuple[str, str | None], ...]

    @property
    def every_differs(self) -> bool:
        """Every track carries a value, and no two agree.

        Whether a tag is meant to vary per track (`QBZ:TID`, AccurateRip's
        CRCs) isn't something Harmonist knows or needs to. What it can say is
        that there's no majority to show, where the consensus model would pick
        track 1's value and call the rest outliers."""
        c = self.consensus
        return c.total > 1 and c.distinct == c.total


@dataclass(frozen=True)
class Info:
    """The album page's Additional info: the origin, then the tags."""

    origin: Origin
    why: str
    rows: tuple[TagRow, ...]


def _row(tag: str, files: Sequence[tuple[str, str | None]]) -> TagRow | None:
    found = compare.consensus(files)
    return TagRow(tag, found, tuple(files)) if found.value is not None else None


def info(album: Album) -> Info | None:
    """The Additional info section for `album`, or None when its files carry
    none of the tags it shows and nothing else settles its origin."""
    merged = len(album.paths) > 1

    def name(path: Path) -> str:
        return f"{path.parent.name}/{path.name}" if merged else path.name

    files = [(name(path), tags) for path, tags in album.file_provenance]
    rows: list[TagRow | None] = [
        _row("QBZ:TID", [(f, t.qobuz_track_id) for f, t in files]),
        _row("AccurateRipResult", [(f, t.accuraterip_result) for f, t in files])
        or _row("AccurateRipDiscID", [(f, t.accuraterip_disc_id) for f, t in files]),
        _row("UPC", [(f, ", ".join(t.upcs) or None) for f, t in files]),
    ]
    # One row per comment: an MP3 can carry several frames, Beatport's genre
    # before its purchase note.
    for i in range(max((len(t.comments) for _, t in files), default=0)):
        rows.append(
            _row("Comment", [(f, t.comments[i] if i < len(t.comments) else None) for f, t in files])
        )
    shown = tuple(r for r in rows if r is not None)
    found = origin(album)
    if (
        not shown
        and found is Origin.UNKNOWN
        and (album.sidecar is None or album.sidecar.origin_override is None)
    ):
        return None
    return Info(found, _why(album, found), shown)
