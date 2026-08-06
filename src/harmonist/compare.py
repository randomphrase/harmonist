"""Comparing an album's on-disk tags against its MusicBrainz release (#106).

Pure functions over values — no I/O, no mutagen, no MusicBrainz client. The web
layer gathers the values and renders the result; the audit log (#86) wants the
same comparison rendered differently, so the model lives here rather than inside
either of them.

Three ideas, in the order they matter:

**Agreement is the answer, not a diff.** Harmonist's job is to say what differs,
and most fields don't. A field that matches carries no runs, no emphasis, and
renders as one plain line. The register is deliberately not a code diff: most
real differences here are formatting (a Bandcamp `Artist A | Artist B` credit vs
MusicBrainz's join phrases, a featured credit MB keeps out of the title), and
calling those errors would be wrong.

**A per-album field is really N per-track fields.** They usually agree; when they
don't there is no single on-disk value, so `consensus()` reports what most tracks
say, how many, and which disagree — and refuses to pick on a tie rather than
inventing an answer.

**"Couldn't read it" is a third state** (#112), distinct from both "matches" and
"differs". A file Harmonist failed to open must not be reported as untagged.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # keeps this module free of runtime imports — it stays pure
    from .formats.types import TagSet, TrackTags

# Emphasis is dropped only when a change is BOTH large and scattered. Size alone
# is the wrong test: "2019" -> "2019-03-15" changes 60% of the characters and is
# precisely the case worth marking, while "Kaskade" -> "Rainbow Connection"
# changes a similar proportion across half a dozen fragments and is unreadable.
# What makes it confetti is fragmentation, so both conditions must hold.
MAX_EMPHASIS_RATIO = 0.34
MAX_CHANGED_RUNS = 2

# Changed runs closer together than this are merged. Character-level diffing of
# "Galán | Spieth" against "Galán, Spieth" otherwise yields a scatter of
# one-character fragments that reads worse than the difference it marks.
_MERGE_GAP = 3


class Agreement(StrEnum):
    """What the comparison found for one field."""

    MATCHES = "matches"
    DIFFERS = "differs"
    #: Absent from the files, present on MusicBrainz (label, catalogue number).
    #: Rendered exactly like a change — a lone MusicBrainz line. It is not a
    #: conflict and must not be dressed as one.
    ONLY_MB = "only_mb"
    #: On disk with no MusicBrainz counterpart — the comment carrying the
    #: Bandcamp URL, and eventually arbitrary tags. MusicBrainz has no opinion,
    #: so offering a comparison would invent a difference.
    ONLY_DISK = "only_disk"
    #: Harmonist could not read the file(s) (#112). NOT the same as "no value":
    #: an unreadable file reported as untagged is how a failing disk gets an
    #: album re-tagged.
    UNREADABLE = "unreadable"


class Kind(StrEnum):
    """How the field wants to be shown. Long text stacks (both values flush
    left, one above the other); short scalars sit inline with an arrow."""

    TEXT = "text"
    SCALAR = "scalar"


@dataclass(frozen=True)
class Run:
    """A slice of a value, flagged as differing or not — for in-value emphasis."""

    text: str
    changed: bool


@dataclass(frozen=True)
class Consensus:
    """What the album's tracks say about one field.

    `value` is None when the tracks disagree with no majority; `outliers` names
    the files that don't carry `value`, so the UI can show them on demand
    without re-reading anything.
    """

    value: str | None
    agreeing: int
    total: int
    outliers: tuple[tuple[str, str | None], ...] = ()
    #: How many distinct non-empty values the tracks carry. Load-bearing: it is
    #: the only thing separating "no track has this field" (0 — the field is
    #: simply absent) from "the tracks disagree with no majority" (2+ on a tie).
    #: Both leave `value` None, and treating them the same would report an
    #: untagged album as inconsistent.
    distinct: int = 0

    @property
    def is_unanimous(self) -> bool:
        return self.total > 0 and self.agreeing == self.total


@dataclass(frozen=True)
class FieldComparison:
    """One row of the album's tag comparison."""

    label: str
    kind: Kind
    agreement: Agreement
    disk: str | None = None
    mb: str | None = None
    #: Empty unless the difference is small enough to mark in place — see
    #: MAX_EMPHASIS_RATIO. Callers render plain text when these are empty.
    disk_runs: tuple[Run, ...] = ()
    mb_runs: tuple[Run, ...] = ()
    consensus: Consensus | None = None

    @property
    def differs(self) -> bool:
        """Whether this row is something the user should look at. ONLY_DISK is
        excluded on purpose: a Bandcamp URL in the comment is not a finding."""
        return self.agreement in (
            Agreement.DIFFERS,
            Agreement.ONLY_MB,
            Agreement.UNREADABLE,
        )


def consensus(values: Sequence[tuple[str, str | None]]) -> Consensus:
    """What most tracks say for one field, from `(file_name, value)` pairs, in
    track order.

    On a tie — a 4/4 split, with no most-common value — **the first track with a
    value wins**. Deliberately a rule that fits in one sentence: "when your
    tracks disagree evenly, Harmonist shows what track 1 says." The count beside
    it (`4 of 8`) already tells the user this isn't the album's settled answer
    and the outliers are one hover away, so an arbitrary-looking pick is less
    confusing than a field that refuses to show anything at all.

    Tracks with no value are counted in `total` but can't win: a field present
    on six of eight tracks is "what the album says", with two outliers, not a
    field with no value. `value` is None only when NO track has one.
    """
    total = len(values)
    if total == 0:
        return Consensus(value=None, agreeing=0, total=0)
    counts = Counter(v for _, v in values if v is not None)
    if not counts:
        return Consensus(value=None, agreeing=0, total=total, distinct=0)
    ranked = counts.most_common()
    top_count = ranked[0][1]
    tied = [v for v, c in ranked if c == top_count]
    if len(tied) > 1:
        # Tie broken by track order: the first track carrying one of the joint-
        # winning values. `Counter.most_common` orders ties by first insertion,
        # which is close but not the same thing — it ranks by first *occurrence
        # of that value*, which coincides here only because we scan in order.
        # Being explicit costs a line and means the rule is the documented one.
        winner = next(v for _, v in values if v in tied)
    else:
        winner = ranked[0][0]
    top_count = counts[winner]
    outliers = tuple((name, v) for name, v in values if v != winner)
    return Consensus(
        value=winner, agreeing=top_count, total=total, outliers=outliers, distinct=len(counts)
    )


def diff_runs(a: str, b: str) -> tuple[tuple[Run, ...], tuple[Run, ...]]:
    """Split two values into runs, marking what differs between them.

    Returns empty tuples when the change is too large to mark usefully — the
    caller then renders both values plain. Character-level, because the
    differences worth marking here are punctuation and separators that a
    word-level diff would swallow whole ("2019" vs "2019-03-15" marks exactly
    "-03-15").
    """
    if a == b:
        return (), ()
    matcher = SequenceMatcher(None, a, b, autojunk=False)
    opcodes = matcher.get_opcodes()
    edits = [op for op in opcodes if op[0] != "equal"]
    changed = sum(max(i2 - i1, j2 - j1) for _, i1, i2, j1, j2 in edits)
    if len(edits) > MAX_CHANGED_RUNS and changed > max(len(a), len(b)) * MAX_EMPHASIS_RATIO:
        return (), ()
    a_runs: list[Run] = []
    b_runs: list[Run] = []
    for tag, i1, i2, j1, j2 in opcodes:
        equal = tag == "equal"
        if i2 > i1:
            a_runs.append(Run(a[i1:i2], not equal))
        if j2 > j1:
            b_runs.append(Run(b[j1:j2], not equal))
    return _merge(a_runs), _merge(b_runs)


def _merge(runs: list[Run]) -> tuple[Run, ...]:
    """Fold short unchanged gaps into the surrounding change, then join
    neighbours that ended up with the same flag."""
    for i in range(1, len(runs) - 1):
        bridges_a_change = runs[i - 1].changed and runs[i + 1].changed
        if not runs[i].changed and len(runs[i].text) < _MERGE_GAP and bridges_a_change:
            runs[i] = Run(runs[i].text, True)
    out: list[Run] = []
    for run in runs:
        if out and out[-1].changed == run.changed:
            out[-1] = Run(out[-1].text + run.text, run.changed)
        else:
            out.append(run)
    return tuple(out)


def compare_field(
    label: str,
    *,
    kind: Kind = Kind.TEXT,
    disk: Consensus | None = None,
    mb: str | None = None,
    unreadable: bool = False,
) -> FieldComparison:
    """Build one comparison row.

    `disk` is a `Consensus` rather than a bare value so the row can report both
    what the album says and how united it is about it.
    """
    if unreadable:
        return FieldComparison(label, kind, Agreement.UNREADABLE, mb=mb, consensus=disk)

    # `value` is None only when no track carries the field at all — a tie is
    # resolved in `consensus`, so uneven tagging never suppresses the row. How
    # united the tracks are travels alongside on `consensus`, for the UI to
    # annotate; it doesn't change which comparison is made.
    disk_value = disk.value if disk else None
    if disk_value is None and mb is None:
        return FieldComparison(label, kind, Agreement.MATCHES, consensus=disk)
    if disk_value is None:
        return FieldComparison(label, kind, Agreement.ONLY_MB, mb=mb, consensus=disk)
    if mb is None:
        return FieldComparison(label, kind, Agreement.ONLY_DISK, disk=disk_value, consensus=disk)
    if disk_value == mb:
        return FieldComparison(
            label, kind, Agreement.MATCHES, disk=disk_value, mb=mb, consensus=disk
        )
    disk_runs, mb_runs = diff_runs(disk_value, mb)
    return FieldComparison(
        label,
        kind,
        Agreement.DIFFERS,
        disk=disk_value,
        mb=mb,
        disk_runs=disk_runs,
        mb_runs=mb_runs,
        consensus=disk,
    )


#: The album-level fields the page shows, in display order, as
#: `(label, TrackTags attribute, TagSet attribute, kind)`.
#:
#: `comment` is here with no MusicBrainz counterpart on purpose: it carries the
#: recovered Bandcamp URL, MusicBrainz has no opinion on it, and comparing would
#: invent a difference. Absent from this table entirely would be worse — the
#: user can't see a tag Harmonist is keeping for them.
_ALBUM_FIELDS: tuple[tuple[str, str, str | None, Kind], ...] = (
    ("Album", "album", "album", Kind.TEXT),
    ("Album artist", "album_artist", "album_artist", Kind.TEXT),
    ("Date", "date", "date", Kind.SCALAR),
    ("Label", "label", "label", Kind.TEXT),
    ("Cat. no.", "catalog_number", "catalog_number", Kind.SCALAR),
    ("Barcode", "barcode", "barcode", Kind.SCALAR),
    ("Media", "media", "media", Kind.SCALAR),
    ("Genre", "genre", None, Kind.TEXT),
    ("Comment", "comment", None, Kind.TEXT),
)


def album_fields(
    tracks: Sequence[tuple[str, TrackTags]],
    mb: TagSet | None,
) -> tuple[FieldComparison, ...]:
    """Compare an album's per-track tags against what tagging would write.

    `tracks` is `(file_name, tags)` in track order — per-track rather than a
    single album value because the tracks are what actually exist, and their
    agreement is itself information.

    `mb` is any one track's TagSet: every field here is album-level, so they're
    identical across tracks. None when there's no MusicBrainz release to compare
    against, which leaves every field ONLY_DISK rather than pretending MB
    disagrees.
    """
    unreadable = all(t.unreadable for _, t in tracks) if tracks else False
    out: list[FieldComparison] = []
    for label, disk_attr, mb_attr, kind in _ALBUM_FIELDS:
        # Unreadable files contribute no value — they'd otherwise vote "absent"
        # and drag a field to ONLY_MB, reporting a tag as missing when the truth
        # is that Harmonist couldn't look (#112).
        values = [(name, getattr(t, disk_attr)) for name, t in tracks if not t.unreadable]
        mb_value = getattr(mb, mb_attr) if (mb is not None and mb_attr) else None
        out.append(
            compare_field(
                label,
                kind=kind,
                disk=consensus(values),
                mb=mb_value or None,
                unreadable=unreadable,
            )
        )
    return tuple(out)


@dataclass(frozen=True)
class AlbumComparison:
    """Every field of one album, plus the headline the section needs."""

    fields: tuple[FieldComparison, ...] = field(default_factory=tuple)

    @property
    def differing(self) -> tuple[FieldComparison, ...]:
        return tuple(f for f in self.fields if f.differs)

    @property
    def summary(self) -> str:
        """The line beside the MusicBrainz hexagon."""
        n = len(self.differing)
        if not self.fields:
            return "nothing to compare"
        if n == 0:
            return f"all {len(self.fields)} fields match"
        return f"{n} of {len(self.fields)} fields differ"
