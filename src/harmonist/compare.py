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
from collections.abc import Callable, Hashable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from difflib import SequenceMatcher
from enum import StrEnum
from typing import TYPE_CHECKING

# The field set, its labels and its scopes, at runtime: the album panel's rows
# are DERIVED from `Owned` rather than listed beside it (#295), so this module
# needs the real values, not just their types. `owned` is pure stdlib itself,
# and `tag_history` — the sibling that describes itself as "pure functions over
# values, like `compare`" — already imports it the same way.
from .formats.owned import ALBUM_FIELDS, LABELS, TRACK_FIELDS, Owned

if TYPE_CHECKING:  # `types` stays type-only: importing it at runtime pulls mutagen in
    from .formats.types import TagSet, TrackTags

    #: One present, readable track as the column rules see it (#309): the file's
    #: tags beside the MusicBrainz track it was paired with. The MusicBrainz half
    #: is None for a video track (#226) and throughout the disk-only view (#228)
    #: — both cases where there is nothing to compare against, rather than a
    #: comparison that found nothing.
    _Present = tuple[TrackTags, "MBTrack | None"]

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

# Per-track length tolerance. Anything within this is "close enough" — small
# encoder differences, gapless playback edits, a fade trimmed differently.
#
# Lives HERE, in the pure module, and is imported by `match`: the matcher uses
# it to decide whether a release fits the files at all, and the tracklist
# comparison uses it to decide whether a length is worth showing as a
# difference. Two constants would let the album page report a track as differing
# from the very release Harmonist accepted as an exact match — the page
# contradicting a decision Harmonist has already made and acted on.
LENGTH_TOLERANCE_MS = 4000


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

    @property
    def missing_count(self) -> int:
        """Outliers that carry no value at all, as opposed to a different one.

        The distinction the old `title=` tooltip couldn't express: it rendered
        both as a filename followed by a dash, so "this track says Ambient too,
        just spelled differently" and "this track has no genre" looked alike.
        """
        return sum(1 for _, value in self.outliers if value is None)

    @property
    def odd_summary(self) -> str:
        """What the pill says: the finding, not a ratio (#164).

        "6 of 7" states arithmetic and leaves the reader to work out both what
        was counted and what is wrong with it — and, sitting under a note about
        matching MusicBrainz, invites them to read it as a second opinion on
        that. Naming the anomaly can't be misread the same way.

        Absence and disagreement get different words because they need
        different fixes: a missing tag is filled in, a differing one is
        reconciled.
        """
        odd = len(self.outliers)
        one = odd == 1
        tracks = "track" if one else "tracks"
        if self.missing_count == odd:
            return f"missing on {odd} {tracks}"
        # The verb agrees with the count too, not just the noun — "1 track
        # differ" is the kind of thing a plural helper produces when only the
        # noun is asked about.
        if self.missing_count:
            return f"{odd} {tracks} {'differs or is' if one else 'differ or are'} missing"
        return f"{odd} {tracks} {'differs' if one else 'differ'}"


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
    #: Whether MusicBrainz has a counterpart for this field at all. False for
    #: `genre` and `comment`, which are in the table deliberately but have no MB
    #: attribute behind them (#12, and the recovered Bandcamp URL).
    #:
    #: NOT derivable from `agreement`: a field with no MB counterpart lands in
    #: ONLY_DISK, but so does a comparable field MusicBrainz happens to have no
    #: value for — an album whose barcode MB doesn't know. Treating those the
    #: same is what let "All 9 fields match MusicBrainz" count two fields
    #: MusicBrainz was never asked about (#164).
    comparable: bool = True
    #: The MusicBrainz entity this row's value(s) identify — "artist",
    #: "release-group" — or None for an ordinary row (#298). Both the mark that
    #: the value is a set of MBIDs rather than something to read, and the path
    #: segment their links need.
    #:
    #: The NAME each id stands for is deliberately not here. It comes from the
    #: release payload, which this module never sees, and it exists only for the
    #: ids MusicBrainz just told us about — an id on disk that MB has moved away
    #: from has no name to give. That asymmetry is load-bearing: it is what stops
    #: a differing row rendering two identical names and reading as a match.
    entity: str | None = None
    #: Whether this row's values are artist CREDIT phrases, so each may be drawn
    #: as the named artists it is built from (#309) — see `_CREDITED`. A property
    #: of the FIELD, like `entity`: the two strings being compared cannot say
    #: whether they are a credit or an album title that reads like one.
    credit: bool = False

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


def compare_value(
    label: str,
    *,
    kind: Kind = Kind.TEXT,
    disk: str | None = None,
    mb: str | None = None,
    unreadable: bool = False,
    tracks: Consensus | None = None,
    also_matches: str | None = None,
) -> FieldComparison:
    """Compare one on-disk value against one MusicBrainz value.

    The core of the model. A per-track field has exactly one on-disk value, so
    this is what the tracklist uses directly; `compare_field` wraps it for the
    album panel, where the on-disk value is a consensus across the tracks.

    `also_matches` is a second on-disk spelling that counts as agreement — one
    exact string, never a pattern. It exists because a field can have more than
    one correct form on disk: Picard writes an album title with the release
    disambiguation appended when told to, and that is the same album (#283).

    This module stays deliberately free of runtime imports, so it is told the
    accepted spelling rather than deriving it; what makes a second spelling
    legitimate is MusicBrainz's business, and `models.title_with_disambiguation`
    is where that knowledge lives.
    """
    if unreadable:
        return FieldComparison(label, kind, Agreement.UNREADABLE, mb=mb, consensus=tracks)
    if disk is None and mb is None:
        return FieldComparison(label, kind, Agreement.MATCHES, consensus=tracks)
    if disk is None:
        return FieldComparison(label, kind, Agreement.ONLY_MB, mb=mb, consensus=tracks)
    if mb is None:
        return FieldComparison(label, kind, Agreement.ONLY_DISK, disk=disk, consensus=tracks)
    if disk == mb or disk == also_matches:
        return FieldComparison(label, kind, Agreement.MATCHES, disk=disk, mb=mb, consensus=tracks)
    disk_runs, mb_runs = diff_runs(disk, mb)
    return FieldComparison(
        label,
        kind,
        Agreement.DIFFERS,
        disk=disk,
        mb=mb,
        disk_runs=disk_runs,
        mb_runs=mb_runs,
        consensus=tracks,
    )


def compare_field(
    label: str,
    *,
    kind: Kind = Kind.TEXT,
    disk: Consensus | None = None,
    mb: str | None = None,
    unreadable: bool = False,
    also_matches: str | None = None,
) -> FieldComparison:
    """Build one row of the ALBUM panel.

    `disk` is a `Consensus` rather than a bare value so the row can report both
    what the album says and how united it is about it.

    A consensus `value` is None only when no track carries the field at all — a
    tie is resolved in `consensus`, so uneven tagging never suppresses the row.
    How united the tracks are travels alongside for the UI to annotate; it
    doesn't change which comparison is made.
    """
    return compare_value(
        label,
        kind=kind,
        disk=disk.value if disk else None,
        mb=mb,
        unreadable=unreadable,
        tracks=disk,
        also_matches=also_matches,
    )


#: The album-level fields the page shows, in display order, as
#: `(label, TrackTags attribute, TagSet attribute, kind)`.
#:
#: `comment` is here with no MusicBrainz counterpart on purpose: it carries the
#: recovered Bandcamp URL, MusicBrainz has no opinion on it, and comparing would
#: invent a difference. Absent from this table entirely would be worse — the
#: user can't see a tag Harmonist is keeping for them.
#: Which owned fields want the stacked (TEXT) treatment rather than an inline
#: arrow. Prose and identifiers stack because they are long; everything else is
#: short enough to sit on one line. Total over `Owned` — a field added later
#: without an entry fails the test beside `SCOPE`'s, rather than silently
#: rendering as a scalar.
_KINDS: dict[str, Kind] = {
    Owned.MB_ALBUM_ID: Kind.TEXT,
    Owned.ALBUM: Kind.TEXT,
    Owned.ALBUM_ARTIST: Kind.TEXT,
    Owned.ALBUM_ARTIST_SORT: Kind.TEXT,
    Owned.MB_ALBUM_ARTIST_IDS: Kind.TEXT,
    Owned.MB_RELEASE_GROUP_ID: Kind.TEXT,
    Owned.MB_ALBUM_TYPE: Kind.SCALAR,
    Owned.MB_ALBUM_STATUS: Kind.SCALAR,
    Owned.MB_ALBUM_COUNTRY: Kind.SCALAR,
    Owned.DATE: Kind.SCALAR,
    Owned.ORIGINAL_DATE: Kind.SCALAR,
    Owned.SCRIPT: Kind.SCALAR,
    Owned.LABEL: Kind.TEXT,
    Owned.CATALOG_NUMBER: Kind.SCALAR,
    Owned.BARCODE: Kind.SCALAR,
    Owned.ASIN: Kind.SCALAR,
    Owned.DISC_TOTAL: Kind.SCALAR,
    Owned.TITLE: Kind.TEXT,
    Owned.ARTIST: Kind.TEXT,
    Owned.ARTIST_SORT: Kind.TEXT,
    Owned.ARTISTS: Kind.TEXT,
    Owned.TRACK_NUM: Kind.SCALAR,
    Owned.TRACK_TOTAL: Kind.SCALAR,
    Owned.DISC_NUM: Kind.SCALAR,
    Owned.DISC_SUBTITLE: Kind.TEXT,
    Owned.MEDIA: Kind.SCALAR,
    Owned.MB_TRACK_ID: Kind.TEXT,
    Owned.MB_RELEASE_TRACK_ID: Kind.TEXT,
    Owned.MB_ARTIST_IDS: Kind.TEXT,
    Owned.ISRCS: Kind.TEXT,
}

#: Rows the panel shows but cannot compare, because Harmonist does not write the
#: tag and so has no MusicBrainz counterpart to put beside it (#164). `mb=None`
#: is what marks a row uncomparable, and it states a fact rather than a policy:
#:
#: * **Genre** — `RELEASE_INCLUDES` does not request `genres`, so no genre is
#:   ever fetched. There is nothing to compare against. Writing one is #12; if
#:   that lands, genre becomes owned and joins the compared set automatically,
#:   because the table below is derived rather than listed.
#: * **Comment** — user data Harmonist preserves and will never write. Its row
#:   answers a different question from the rest of the table: not "does this
#:   match MusicBrainz" but "here is the evidence this album was linked from",
#:   since `url_recovery` and `reconcile` read a Bandcamp URL out of it.
_DISPLAY_ONLY: tuple[tuple[str, str, None, Kind], ...] = (
    ("Genre", "genre", None, Kind.TEXT),
    ("Comment", "comment", None, Kind.TEXT),
)


def _disk_value(tags: TrackTags, key: str) -> str | None:
    """One field's value as this file carries it, as a display string.

    Owned fields come from the `owned` snapshot `read_tags` took off the handle
    it already had open (#295), which is why widening this panel to thirty
    fields costs no extra file reads. Genre and Comment are not owned, so they
    keep their named attribute.

    Lists join with "; " and numbers stringify, matching how the same values are
    rendered in a History entry — the panel and the record must not describe one
    tag two ways. Absent, empty and empty-list all collapse to None, the way
    `owned.values_differ` treats them, so a field the tagger considers unset
    never renders as a difference against MusicBrainz having nothing either.
    """
    return _as_display(tags.owned[key] if key in tags.owned else getattr(tags, key, None))


def _as_display(raw: object) -> str | None:
    """A raw owned value as the page shows it, or None when it counts as absent.

    Shared by both sides of every row so the disk and MusicBrainz halves are
    formatted by the same code. When they were two expressions they were free to
    disagree, and a list joined one way here and another way there would render
    as a difference in a field that matched.
    """
    if raw is None or raw == "" or raw == []:
        return None
    if isinstance(raw, (list, tuple)):
        return "; ".join(str(v) for v in raw)
    return str(raw)


def _rows(fields: Sequence[Owned]) -> tuple[tuple[str, str, str | None, Kind], ...]:
    """`(label, disk key, mb attr, kind)` for each owned field, in the given order."""
    return tuple((LABELS[f], f.value, f.value, _KINDS[f]) for f in fields)


#: The one album-scoped tag the panel does NOT show (#298).
#:
#: `mb_album_id` is the release the sidecar names, and this comparison is fetched
#: BY that id — so it matches on every album in the library except one case, and
#: the album header already shows and links the release above. What it bought for
#: that permanent row was nineteen hex characters in a panel whose job is to be
#: scannable.
#:
#: The case it can differ in is a MusicBrainz merge: the fetch redirects and
#: returns a different `id` than the one asked for. That is real, and it is not
#: dropped — `mb_album_id` leaves `SHOWN_FIELDS` with this row, so the re-tag box
#: below picks it up and states it as a change, which is what it is. #268 owns
#: the merge itself.
#:
#: An exception to "derived, not listed" below, and the only one. It is written
#: as a set to subtract rather than by hand-listing the survivors, so a field
#: added to `Owned` still arrives in the panel on the day it exists.
_NOT_COMPARED: frozenset[Owned] = frozenset({Owned.MB_ALBUM_ID})


#: The order the panel shows its rows in (#307).
#:
#: The grid fills row-major, two label/value pairs per row (#295), so this
#: sequence decides which COLUMN each field lands in — and `Owned` order, which
#: the panel used until now, was never chosen for that. It put the artist fields
#: in both columns and the release fields in both, so reading down either one
#: gave three subjects interleaved: Album, Album artist sort, Release group,
#: Release status.
#:
#: Paired up here instead: the release block down one column, the artist block
#: down the other, and then the pairs that belong together side by side — Date
#: with Original date, Label with Cat. no., Barcode with ASIN.
#:
#: **A sort key, not a replacement.** `_ALBUM_FIELDS` is derived rather than
#: listed precisely so a field added to `Owned` is compared from the day it
#: exists (#295), and a second hand-written list is exactly what let the panel
#: omit twenty-one fields. So a field missing from here sorts to the END and is
#: still shown; it never disappears. `test_display_order_cannot_drop_a_field`
#: holds that.
#:
#: Below 60rem the panel is one column and this sequence is what it reads top to
#: bottom, where the interleaving is the other way round: Album, Album artist,
#: Release group, Album artist sort. Accepted deliberately — one order cannot be
#: grouped for one column and for two, and the wide layout is the one being read
#: on the machine someone tags from.
_DISPLAY_ORDER: tuple[Owned, ...] = (
    # Row 1-3: what the release IS, beside who it is BY.
    Owned.ALBUM,
    Owned.ALBUM_ARTIST,
    Owned.MB_RELEASE_GROUP_ID,
    Owned.ALBUM_ARTIST_SORT,
    Owned.MB_ALBUM_TYPE,
    Owned.MB_ALBUM_ARTIST_IDS,
    # Row 4-5: which edition, and when.
    Owned.MB_ALBUM_STATUS,
    Owned.MB_ALBUM_COUNTRY,
    Owned.DATE,
    Owned.ORIGINAL_DATE,
    # Row 6-8: the release's paperwork.
    Owned.LABEL,
    Owned.CATALOG_NUMBER,
    Owned.BARCODE,
    Owned.ASIN,
    Owned.DISC_TOTAL,
    Owned.SCRIPT,
)


def _in_display_order(fields: Sequence[Owned]) -> list[Owned]:
    """`fields` sorted by `_DISPLAY_ORDER`, with anything unlisted at the end.

    Stable, so unlisted fields keep their `Owned` order among themselves — a
    field added to `Owned` and forgotten here appears in the panel, at the
    bottom, rather than vanishing from it.
    """
    last = len(_DISPLAY_ORDER)
    order = {f: i for i, f in enumerate(_DISPLAY_ORDER)}
    return sorted(fields, key=lambda f: order.get(f, last))


#: The album panel's rows: every album-scoped tag Harmonist writes bar the one
#: above, in the display order above, then the two it only displays.
#:
#: **Derived, not listed.** It used to be a hand-written tuple of nine, and the
#: gap between it and `Owned` grew to twenty-one fields without anyone noticing
#: — so an album whose release had gained an `original_date` differed on disk
#: while this panel reported every field matching, and its "N of M fields
#: differ" count measured a denominator that had nothing to do with what a
#: re-tag would write (#295). Deriving it means a field added to `Owned` is
#: compared from the day it exists.
_ALBUM_FIELDS: tuple[tuple[str, str, str | None, Kind], ...] = (
    _rows(_in_display_order([f for f in ALBUM_FIELDS if f not in _NOT_COMPARED])) + _DISPLAY_ONLY
)


#: The one album field with a second legitimate on-disk spelling — see
#: `album_fields`. Named rather than inlined so the special case is visible from
#: the table above rather than buried in the loop.
_ALIASED_FIELD = "album"


#: Which MusicBrainz entity each id-carrying row points at, which is both what
#: makes a row linkable and the path segment its URL needs (#298).
#:
#: The per-track ids joined this table in #309. They were deliberately left out
#: when it was written, on the grounds that nothing rendered them through a
#: `FieldComparison` and an entry would be a claim with no reader — which was
#: true right up until the tracklist started giving them columns. They are read
#: through `_track_list.html` now, and a column of raw hex is exactly the wall
#: #298 removed from the panel.
_ENTITY: dict[str, str] = {
    Owned.MB_ALBUM_ARTIST_IDS: "artist",
    Owned.MB_RELEASE_GROUP_ID: "release-group",
    Owned.MB_ARTIST_IDS: "artist",
    Owned.MB_TRACK_ID: "recording",
    # MusicBrainz has no page of its own for a release track; /track/<id>
    # resolves to the release at that track's position, which is the thing a
    # reader following this link is actually asking to see.
    Owned.MB_RELEASE_TRACK_ID: "track",
}


#: The fields whose value is an artist CREDIT — a phrase MusicBrainz assembles
#: from named artists and the words between them (#309).
#:
#: The mark that a value may be rendered as its parts: the page looks the value
#: up in the credits `tagger.artist_credits` built and, on an exact hit, draws
#: each artist as a link joined by MusicBrainz's own " feat. " / " & " / ", ".
#: A miss renders the flat string, which is what a tag that has drifted from
#: MusicBrainz gets — and what every disk value gets on a field where the two
#: disagree, since the file carries no structure to recover.
#:
#: Scoped to these two rather than applied to any value that happens to match a
#: credit phrase: an album named after its artist would otherwise turn its Album
#: row into an artist link.
_CREDITED: frozenset[str] = frozenset({Owned.ALBUM_ARTIST, Owned.ARTIST})


def album_fields(
    tracks: Sequence[tuple[str, TrackTags]],
    mb: TagSet | None,
    *,
    album_title_alias: str | None = None,
) -> tuple[FieldComparison, ...]:
    """Compare an album's per-track tags against what tagging would write.

    `tracks` is `(file_name, tags)` in track order — per-track rather than a
    single album value because the tracks are what actually exist, and their
    agreement is itself information.

    `mb` is any one track's TagSet: every field here is album-level, so they're
    identical across tracks. None when there's no MusicBrainz release to compare
    against, which leaves every field ONLY_DISK rather than pretending MB
    disagrees.

    `album_title_alias` is a second album title that counts as agreement —
    Picard's disambiguated spelling, built by `models.title_with_disambiguation`
    from the release the caller already holds (#283). Only the Album row can
    take one; nothing else here has a legitimate second form.
    """
    unreadable = all(t.unreadable for _, t in tracks) if tracks else False
    out: list[FieldComparison] = []
    for label, disk_attr, mb_attr, kind in _ALBUM_FIELDS:
        # Unreadable files contribute no value — they'd otherwise vote "absent"
        # and drag a field to ONLY_MB, reporting a tag as missing when the truth
        # is that Harmonist couldn't look (#112).
        values = [(name, _disk_value(t, disk_attr)) for name, t in tracks if not t.unreadable]
        mb_value = _as_display(getattr(mb, mb_attr)) if (mb is not None and mb_attr) else None
        row = compare_field(
            label,
            kind=kind,
            disk=consensus(values),
            mb=mb_value,
            unreadable=unreadable,
            also_matches=album_title_alias if disk_attr == _ALIASED_FIELD else None,
        )
        # Marked here rather than threaded through `compare_value`, because THIS
        # table is where the knowledge lives: a field is comparable iff it names
        # a TagSet attribute. The comparison functions only ever see values, and
        # an absent MB value is not the same fact as an absent MB counterpart.
        # Same for the entity — which MusicBrainz thing an id names is a property
        # of the field, not of the two strings being compared.
        out.append(
            replace(
                row,
                comparable=bool(mb_attr),
                entity=_ENTITY.get(disk_attr),
                credit=disk_attr in _CREDITED,
            ),
        )
    return tuple(out)


@dataclass(frozen=True)
class AlbumComparison:
    """Every field of one album, plus the headline the section needs."""

    fields: tuple[FieldComparison, ...] = field(default_factory=tuple)
    #: Whether there was a MusicBrainz release to compare against at all. False
    #: for the disk-only view (#228): the tags are still shown — they never
    #: depended on MusicBrainz — but nothing here was compared, and every field
    #: lands in ONLY_DISK for want of a counterpart rather than because
    #: MusicBrainz has nothing to say about it. Without this the summary would
    #: read "All 7 fields match MusicBrainz" for a release MusicBrainz has
    #: deleted, since ONLY_DISK is deliberately not a finding.
    mb_available: bool = True

    @property
    def differing(self) -> tuple[FieldComparison, ...]:
        return tuple(f for f in self.fields if f.differs)

    @property
    def comparable(self) -> tuple[FieldComparison, ...]:
        """The fields MusicBrainz actually has an opinion on.

        Genre and the comment are shown but never compared, so counting them in
        a sentence about matching MusicBrainz overstates what was checked — see
        `FieldComparison.comparable` (#164).
        """
        return tuple(f for f in self.fields if f.comparable)

    @property
    def summary(self) -> str:
        """The line beside the MusicBrainz hexagon.

        A whole sentence rather than a count to be glued to a label, so the two
        cases can read naturally — "differ in" and "match" want different
        prepositions, and assembling that in a template would scatter the
        wording across two files.

        Counted over `comparable` on BOTH sides. It used to say "All 9 fields
        match MusicBrainz" for an album whose genre and comment MusicBrainz had
        never been asked about — and, with unreadable files, could reach
        "9 of 7 differ", since every field goes UNREADABLE while only seven of
        them were ever comparable.
        """
        if not self.mb_available:
            return "No comparison — showing your files' tags"
        fields = self.comparable
        n = len([f for f in fields if f.differs])
        if not fields:
            return "Nothing to compare against MusicBrainz"
        if n == 0:
            return f"All {len(fields)} fields match MusicBrainz"
        return f"{n} of {len(fields)} fields differ in MusicBrainz"


# ---------------------------------------------------------------------------
# The tracklist (#135)
# ---------------------------------------------------------------------------


class TrackState(StrEnum):
    """What a row of the tracklist IS, before any field is compared.

    The album panel needs no equivalent: an album is always there. A track may
    not be, and the four cases have four different remedies — which is why they
    are four states rather than an "ok / not ok" flag.
    """

    PRESENT = "present"
    #: MusicBrainz lists the track; no file on disk carries it.
    MISSING = "missing"
    #: The file is there and would not open (#112, #126). NOT "untagged", and
    #: not "missing" either: the bytes exist, so the remedy is a re-download or
    #: a look at the disk, not a re-rip.
    UNREADABLE = "unreadable"
    #: A file with no counterpart in the MusicBrainz tracklist. Often legitimate
    #: (a bonus track MusicBrainz doesn't carry), so it is stated, not warned
    #: about.
    EXTRA = "extra"


@dataclass(frozen=True)
class MBTrack:
    """One MusicBrainz track, as the comparison needs it.

    `tags` is what tagging WOULD write for this track (a `tagger.TagSet`), so
    the comparison is against Harmonist's own output rather than a second
    reading of the release — the same guarantee `album_fields` gets, for the
    same reason.

    `length_ms` rides alongside instead of inside because a length is not a tag:
    nothing writes it to a file, and MusicBrainz doesn't always know it.
    """

    tags: TagSet
    length_ms: int | None = None


@dataclass(frozen=True)
class ComparedTrack:
    """One row of the tracklist: what state it's in, and its fields compared.

    `fields` is always the full set, in a fixed order, whatever the state — a
    missing track's fields are all ONLY_MB and an unreadable one's are all
    UNREADABLE. Uniform rows are what let the table keep its columns aligned
    instead of special-casing its own shape per row.
    """

    state: TrackState
    fields: tuple[FieldComparison, ...] = field(default_factory=tuple)
    #: Which medium of the release this track belongs to. Carried structurally
    #: rather than left encoded in the rendered "2-4" position string, so the
    #: tracklist can be GROUPED by disc (#216) instead of prefixing every row
    #: with a number the reader has to decode.
    disc: int = 1
    #: The file this row is about, for the rows that need to name it — the
    #: unreadable one especially, where "which file?" is the user's next
    #: question. None when there is no file.
    file_name: str | None = None
    #: A video file: on disk, readable, and never tagged by Harmonist (#66).
    #: The row says so, because otherwise the page shows a track that silently
    #: never takes part in anything — no comparison, no change when the album
    #: is re-tagged — and leaves the user to wonder which of those is a bug.
    video: bool = False
    #: Whether every difference on this row is in an identifier column, and so
    #: hidden until the reader asks for it (#319).
    #:
    #: The MusicBrainz line has to hide with them. `shows_mb` exists to stop a
    #: row being given "an empty purple row carrying only a hexagon — a
    #: difference marked against nothing", and hiding the CELLS while leaving the
    #: line recreated exactly that, one #319 later.
    #:
    #: A display state rather than a finding, which is why it is a second flag
    #: instead of a change to `shows_mb`: there is a line worth drawing here, and
    #: it is drawn the moment the identifiers are revealed.
    mb_only_identifiers: bool = False

    @property
    def differs(self) -> bool:
        """Whether this row is something the user should look at.

        Any state other than PRESENT counts on its own: an extra file's fields
        are all ONLY_DISK, which is deliberately not a field-level finding (it's
        how the Bandcamp comment stays quiet), but a whole track MusicBrainz has
        never heard of is one.
        """
        return self.state is not TrackState.PRESENT or any(f.differs for f in self.fields)

    @property
    def shows_mb(self) -> bool:
        """Whether there is a MusicBrainz line worth drawing beneath this row.

        Not the same as `differs`: an extra file, and an unreadable one that
        matched no MusicBrainz track, have nothing on the MusicBrainz side to
        put there. Drawing the line anyway would give them an empty purple row
        carrying only a hexagon — a difference marked against nothing.
        """
        return any(f.differs and f.mb for f in self.fields)


@dataclass(frozen=True)
class Medium:
    """One disc of the release, as MusicBrainz describes it.

    `title` is the medium's own name where it has one — MusicBrainz calls
    Hybrid's two discs *Wide Angle* and *Live Angle*, which is a good deal more
    use than "Disc 1" and "Disc 2" (#216). Most releases have none.
    """

    position: int
    title: str | None = None
    format: str | None = None

    @property
    def label(self) -> str:
        """ "Disc 2 — Live Angle", or plain "Disc 2" when it has no name."""
        return f"Disc {self.position} — {self.title}" if self.title else f"Disc {self.position}"


@dataclass(frozen=True)
class DiscGroup:
    """A disc and the tracklist rows belonging to it."""

    medium: Medium
    tracks: tuple[ComparedTrack, ...]

    @property
    def absent(self) -> bool:
        """True when NOT ONE of this disc's tracks is on disk.

        The whole disc is missing rather than the album being short — a bonus
        DVD never ripped, a box set half copied. Worth distinguishing because
        the reading is different, and because spelling out forty-four "Not on
        disk" rows for a disc the user knowingly does not have buries the
        album's actual tracks under it.
        """
        return bool(self.tracks) and all(t.state is TrackState.MISSING for t in self.tracks)

    @property
    def summary(self) -> str:
        n = len(self.tracks)
        return f"{self.medium.format or 'Disc'}, {n} track{'s' if n != 1 else ''}"


@dataclass(frozen=True)
class TracklistComparison:
    """Every track of one album, plus the headline the section needs."""

    tracks: tuple[ComparedTrack, ...] = field(default_factory=tuple)
    #: The release's media, in position order. Empty for a caller that has none
    #: to give, in which case `discs` falls back to what the tracks say.
    media: tuple[Medium, ...] = field(default_factory=tuple)
    #: Whether there was a MusicBrainz release to compare against at all — the
    #: tracklist half of `AlbumComparison.mb_available` (#228). False for the
    #: disk-only view built by `disk_tracklist`.
    mb_available: bool = True
    #: The table's columns, in order, matching each row's `fields` positionally
    #: (#309). A property of THIS comparison rather than a module constant: which
    #: per-track tags are worth a column is a fact about this album's tags, not
    #: about Harmonist.
    columns: tuple[TrackColumn, ...] = field(default_factory=tuple)
    #: The per-track tags with no column because every track agreed and matched
    #: MusicBrainz, each with the one value behind it. Named under the table so
    #: a collapsed column can't be mistaken for a field nobody looked at (#112).
    collapsed: tuple[CollapsedField, ...] = field(default_factory=tuple)

    @property
    def shown_fields(self) -> frozenset[str]:
        """The per-track owned tags this table has a column for.

        The tracklist's half of what scopes the re-tag box (#291, #297); the
        panel's half is `PANEL_FIELDS`. Read off the columns rather than
        recomputed, so "no field appears in both places" holds by construction
        rather than by two tables agreeing.

        The collapsed set is deliberately absent. A collapsed field matches
        MusicBrainz on every track, so a re-tag has nothing to say about it and
        it cannot reach the box anyway — and if one ever did, that is a real
        difference the page had better state somewhere rather than swallow.
        """
        return frozenset(f for c in self.columns for f in c.fields)

    @property
    def identifier_columns(self) -> tuple[TrackColumn, ...]:
        """The columns the "Show identifiers" control reveals (#319)."""
        return tuple(c for c in self.columns if c.identifier)

    @property
    def identifier_summary(self) -> str:
        """What sits beside that control, naming what is behind it.

        Named rather than counted, and for the same reason the collapsed line is
        (#112): a hidden column must not read as one nobody checked. Every column
        in the table earned its place, so these are tags with something to show —
        and the sentence says so, rather than leaving "3 columns hidden" to be
        read as housekeeping.
        """
        labels = [c.label for c in self.identifier_columns]
        if not labels:
            return ""
        named = labels[0] if len(labels) == 1 else f"{', '.join(labels[:-1])} and {labels[-1]}"
        return f"{named} {'differs' if len(labels) == 1 else 'differ'} here."

    @property
    def collapsed_summary(self) -> str:
        """What the disclosure under the table says.

        Names the fields rather than counting them. "5 fields hidden" is a fact
        about the table; "Artist sort, ISRC and 3 others are the same on every
        track" is a finding about the album — and it is the finding that keeps
        checked-and-agrees distinguishable from never-examined (#112).
        """
        labels = [c.label for c in self.collapsed]
        if not labels:
            return ""
        named, rest = labels[:2], labels[2:]
        if rest:
            phrase = f"{', '.join(named)} and {len(rest)} other{'s' if len(rest) != 1 else ''}"
        else:
            phrase = " and ".join(named)
        one = len(labels) == 1
        # No "and match MusicBrainz" without a release to have matched (#228).
        # The tracks still agree with each other, and that is all that was
        # checked, so it is all the sentence may claim.
        tail = f" and {'matches' if one else 'match'} MusicBrainz" if self.mb_available else ""
        return f"{phrase} {'is' if one else 'are'} the same on every track{tail}"

    @property
    def discs(self) -> tuple[DiscGroup, ...]:
        """The tracks grouped by disc, in position order.

        A single-disc album comes back as ONE group, and the template renders no
        heading for it — nearly every album is one disc, and a heading above the
        only disc is noise.
        """
        by_position: dict[int, list[ComparedTrack]] = {}
        for t in self.tracks:
            by_position.setdefault(t.disc, []).append(t)
        known = {m.position: m for m in self.media}
        return tuple(
            DiscGroup(known.get(pos, Medium(position=pos)), tuple(rows))
            for pos, rows in sorted(by_position.items())
        )

    @property
    def differing(self) -> tuple[ComparedTrack, ...]:
        return tuple(t for t in self.tracks if t.differs)

    @property
    def summary(self) -> str:
        """The line beside the Tracks section's hexagon.

        Missing, unreadable and extra tracks get their own clause rather than
        being folded into the count: "3 of 10 tracks differ" is true of an album
        with a dead file, but it isn't what the user needs to be told.
        """
        if not self.mb_available:
            n = len(self.tracks)
            return f"No comparison — showing your {n} track{'s' if n != 1 else ''}"
        if not self.tracks:
            return "Nothing to compare against MusicBrainz"

        # A disc with NOTHING on disk is reported ONCE, as an absent disc, and
        # its tracks are excluded from every count here. Counted individually
        # they made a bonus DVD the user knowingly never ripped into the album's
        # dominant problem — "44 of 60 tracks differ" — and buried the sixteen
        # they actually have (#216).
        absent_discs = [g for g in self.discs if g.absent]
        absent_positions = {g.medium.position for g in absent_discs}
        counted = [t for t in self.tracks if t.disc not in absent_positions]

        clauses: list[str] = []
        total = len(counted)
        n = sum(1 for t in counted if t.differs)
        if total and n == 0:
            clauses.append(
                f"All {total} tracks match MusicBrainz" if total > 1 else "Matches MusicBrainz"
            )
        elif total:
            verb = "differs" if n == 1 else "differ"
            clauses.append(f"{n} of {total} tracks {verb} from MusicBrainz")
        if absent_discs:
            clauses.append(f"{', '.join(g.medium.label for g in absent_discs)} not on disk")
        for state, phrase in (
            (TrackState.MISSING, "not on disk"),
            (TrackState.UNREADABLE, "unreadable"),
            (TrackState.EXTRA, "not in MusicBrainz"),
        ):
            count = sum(1 for t in counted if t.state is state)
            if count:
                clauses.append(f"{count} {phrase}")
        return " · ".join(clauses)


#: The owned fields the album PANEL shows — every compared row of it.
#:
#: Half of what the re-tag plan's box (#291) is scoped against (#297): the box
#: states what a re-tag would change in the fields nothing else on this page
#: shows, and a field the panel already compares would otherwise be restated
#: directly underneath itself. The other half is per-album and lives on the
#: tracklist — see `TracklistComparison.shown_fields`.
#:
#: **Derived from the rendering table, not listed beside it**, for the reason
#: `_ALBUM_FIELDS` is: a hand-kept copy drifts, and here the drift shows up as a
#: row printed twice — the exact complaint #297 was filed about. Taken from
#: `_ALBUM_FIELDS` rather than `ALBUM_FIELDS` so it tracks what the panel
#: RENDERS rather than what the album scope contains; a row the panel drops
#: falls back to the box by itself. The `mb_attr` guard is what keeps Genre and
#: Comment out — they are displayed, but they are not owned fields and a plan
#: can never carry them.
PANEL_FIELDS: frozenset[str] = frozenset(
    disk_attr for _, disk_attr, mb_attr, _ in _ALBUM_FIELDS if mb_attr
)


# ---------------------------------------------------------------------------
# Which per-track fields get a column (#309)
# ---------------------------------------------------------------------------
#
# A column is EARNED, not declared. There are eleven per-track tags with no
# other surface and the table cannot be eleven wide — but "which of them
# matter" was never the right question either. In the usual case a per-track
# value is the same on every track and matches MusicBrainz, and a column of N
# identical rows restates a fact the page already carries. That was already true
# of the `Artist` column before this: a single-artist release printed its album
# artist once per row and said nothing by doing so.
#
# So a field earns a column when ANY of these holds, judged over the tracks that
# are present and readable:
#
#   1. **It differs from MusicBrainz, and not identically on every track.** The
#      finding the page exists to show — and the one the re-tag box could only
#      ever state in the aggregate, as "1 of 7 tracks", leaving the reader to
#      work out which.
#
#      The second half is what keeps that from meaning "every field a re-tag
#      would touch". Where the change is the SAME on every track — every file
#      missing `media`, every one gaining "Digital Media" — the box's one line
#      already says the whole of it, and a column repeating "— → Digital Media"
#      eleven times adds a position nobody needed and spends one of three slots
#      doing it. A column earns its place by answering *which track*, so it is
#      drawn exactly when that question has an answer: some tracks differ and
#      others don't, or they differ in different ways.
#   2. **The tracks disagree with each other.** An album tagged unevenly over
#      decades; the compilation whose `Artist` column is the whole point.
#   3. **It differs from its album-level counterpart.** The featured credit,
#      where one track is credited to two artists and the album to one.
#
# None of them holding means every track says the same thing, that thing matches
# MusicBrainz, and it matches the album-level value. The column is then dropped
# and the field NAMED under the table with its one value a disclosure away — so
# checked-and-agrees still reads differently from never-examined (#112), which
# a silently missing column would not.


#: How many EARNED columns the table will take, over and above the three it
#: always has: the number, the title and the length.
#:
#: The second limit on rule 1, and the one that does not depend on judgment.
#: Rule 1's own "not identically on every track" clause is what keeps the
#: uninteresting fields out; this is what stops the interesting ones from
#: arriving all at once, on the album tagged unevenly over decades where a dozen
#: of them genuinely part company track by track.
#:
#: Above the cap the overflow stays in the box, which leaves each surface a job
#: it can do well: a column shows a difference against the track it belongs to,
#: and the box is where the ones that would not fit go.
MAX_EARNED_COLUMNS = 3


#: The row's identity: a column whatever the rules say.
#:
#: `Artist` is deliberately NOT here, and that was the judgment call of #309
#: rather than an oversight. It earns its column by the same rules as everything
#: else, because a single-artist album repeating one name down a column is
#: exactly the noise this change removes — and rules 2 and 3 bring it straight
#: back the moment a track's credit differs from the album's or from its
#: neighbours'.
_PINNED_COLUMNS: frozenset[Owned] = frozenset({Owned.TITLE})


#: The order columns appear in, and so which ones survive the cap.
#:
#: **A sort key, not a replacement** — the discipline `_DISPLAY_ORDER` states at
#: length: a field added to `Owned` and forgotten here sorts to the END and is
#: still eligible, rather than silently losing the ability to be shown at all.
#:
#: Readable fields first and the MusicBrainz ids last. An id is the widest thing
#: this table can carry and the least use at a glance even as a named link, and
#: the box renders one perfectly well — so when something must overflow, it is
#: an id rather than an ISRC or a sort name.
_COLUMN_ORDER: tuple[Owned, ...] = (
    Owned.TITLE,
    Owned.ARTIST,
    Owned.ARTIST_SORT,
    Owned.ARTISTS,
    Owned.ISRCS,
    Owned.DISC_SUBTITLE,
    Owned.MEDIA,
    Owned.TRACK_TOTAL,
    Owned.DISC_NUM,
    Owned.MB_ARTIST_IDS,
    Owned.MB_TRACK_ID,
    Owned.MB_RELEASE_TRACK_ID,
)


#: Tags derived from the MEDIUM rather than from the track.
#:
#: Exempt from rule 2 on a multi-disc release, and only there. `owned.Scope`
#: already states the fact this rests on: these "genuinely differ per track on a
#: multi-disc release — or a CD+DVD set", which is why they are track-scoped at
#: all. So their disagreeing across an album is the release's structure, not the
#: uneven tagging rule 2 exists to surface, and the disc heading above each group
#: already says "DVD, 29 tracks".
#:
#: Rule 1 still applies to them in full. A track total that has genuinely gone
#: stale is a difference from MusicBrainz and still earns its column.
_MEDIUM_DERIVED: frozenset[Owned] = frozenset({Owned.TRACK_TOTAL, Owned.MEDIA, Owned.DISC_SUBTITLE})


#: The per-track tags that are machine identifiers (#319).
#:
#: They earn columns by the same three rules as everything else, and then get
#: two exemptions the readable tags don't:
#:
#: * **Off the cap.** They start hidden, and a hidden column has no business
#:   spending one of the three slots.
#: * **Rendered short.** `_field_value.html` trims an unnamed id to its first
#:   characters, with the whole of it in the link and the tooltip.
#:
#: `isrcs` is here despite not being an MBID: an ISRC is a recording's
#: registration code, it is no more readable than a UUID, and it is not why
#: anyone opened this page.
_IDENTIFIERS: frozenset[Owned] = frozenset(
    {Owned.ISRCS, Owned.MB_TRACK_ID, Owned.MB_RELEASE_TRACK_ID, Owned.MB_ARTIST_IDS}
)


#: A per-track tag whose column another column already says, better (#319).
#:
#: `artists` is the credit unjoined — the same names as `artist`, separated by
#: "; " instead of by MusicBrainz's own " feat. " — and since #309 renders the
#: `Artist` column as the linked artists it names, `artists` beside it is
#: strictly less informative. On a compilation both earn by rule 2, and the
#: result is one fact in two columns.
#:
#: So `Artist` ACCOUNTS for it: no column of its own, and no collapsed entry and
#: no box row either, because it is not missing from the page — it is there,
#: spelled better. The relationship `disc_num` already has with the `#` column.
#:
#: Absorbed only where it has no difference of its own to report — see
#: `_choose_columns`. A column cannot stand in for a change it isn't showing.
_SUBSUMED_BY: dict[Owned, Owned] = {Owned.ARTISTS: Owned.ARTIST}


#: The album-level tag each per-track tag is measured against for rule 3.
#:
#: Only the three that HAVE a counterpart. An ISRC or a recording id has no
#: album-wide equivalent to differ from, and pairing one with something merely
#: adjacent would be a comparison with no meaning behind it.
_ALBUM_COUNTERPART: dict[Owned, Owned] = {
    Owned.ARTIST: Owned.ALBUM_ARTIST,
    Owned.ARTIST_SORT: Owned.ALBUM_ARTIST_SORT,
    Owned.MB_ARTIST_IDS: Owned.MB_ALBUM_ARTIST_IDS,
}


@dataclass(frozen=True)
class TrackColumn:
    """One column of the tracklist table.

    `fields` is which owned tags this column accounts for, and it is what
    `TracklistComparison.shown_fields` is built from — so a tag with a column can
    never also be listed by the re-tag box below it. Usually one; empty for
    Length, which is not a tag at all; two for the number column on a multi-disc
    release, where "2-4" states the disc as well as the track.
    """

    label: str
    fields: tuple[str, ...] = ()
    #: A machine identifier rather than something to read (#319) — an MBID, an
    #: ISRC. Correct to keep and correct to link, and not what anyone opens this
    #: page to look at, so these columns start hidden behind a control and are
    #: exempt from `MAX_EARNED_COLUMNS`: a column nobody sees by default must not
    #: spend one of the three slots the readable tags are competing for.
    identifier: bool = False


@dataclass(frozen=True)
class CollapsedField:
    """A per-track tag with no column, and the one value every track carries.

    Safe to state as a single value precisely BECAUSE the column was dropped: it
    collapsed only because every present track agreed and matched MusicBrainz.
    That is also why the affordance under the table is a short list and not a
    wider table — revealing N identical rows needs one row, not N.
    """

    label: str
    value: str | None = None
    entity: str | None = None
    credit: bool = False


@dataclass(frozen=True)
class _Candidate:
    """One per-track tag the tracklist could show, and how to render it."""

    owned: Owned
    label: str
    kind: Kind
    entity: str | None = None
    credit: bool = False


def _in_column_order(fields: Sequence[Owned]) -> list[Owned]:
    """`fields` sorted by `_COLUMN_ORDER`, with anything unlisted at the end."""
    last = len(_COLUMN_ORDER)
    order = {f: i for i, f in enumerate(_COLUMN_ORDER)}
    return sorted(fields, key=lambda f: order.get(f, last))


#: Every per-track tag that can become a column, in priority order.
#:
#: Derived from `TRACK_FIELDS` rather than listed, so a tag added to `Owned` is
#: eligible from the day it exists — the guarantee `_ALBUM_FIELDS` gives the
#: panel, and the one whose absence let that panel omit twenty-one fields.
#: `track_num` is the single exclusion and it is not an omission: the `#` column
#: IS that field.
_CANDIDATES: tuple[_Candidate, ...] = tuple(
    _Candidate(f, LABELS[f], _KINDS[f], _ENTITY.get(f), f in _CREDITED)
    for f in _in_column_order([f for f in TRACK_FIELDS if f is not Owned.TRACK_NUM])
)


def _number_column(multi_disc: bool) -> TrackColumn:
    """The `#` column, and which tags it accounts for.

    On a multi-disc release `_number` renders "2-4", so the column states the
    disc as well — and `disc_num` must not then earn a column of its own, which
    would print the same fact twice across one row. On a single-disc release
    `_number` renders `disc or 1`, saying nothing about the disc at all, so
    `disc_num` stays an ordinary candidate: a track that has gained an explicit
    disc number it lacked is a change this column genuinely cannot report.
    """
    fields = (
        (Owned.TRACK_NUM.value, Owned.DISC_NUM.value) if multi_disc else (Owned.TRACK_NUM.value,)
    )
    return TrackColumn("#", fields)


def _earns_column(candidate: _Candidate, present: Sequence[_Present], multi_disc: bool) -> bool:
    """Whether one candidate satisfies any of the three rules above."""
    key = candidate.owned.value
    disk = [_disk_value(t, key) for t, _ in present]
    mb = [_as_display(getattr(m.tags, key)) if m else None for _, m in present]

    # 1. Differs from MusicBrainz, and not identically on every track.
    #
    #    `pairs` is the set of (on disk, in MusicBrainz) readings, over the tracks
    #    MusicBrainz has an opinion about. A MusicBrainz value of None is not one
    #    — that is a video track (#226), or the disk-only view (#228), where a
    #    comparison would invent an opinion nobody offered; the same exclusion
    #    ONLY_DISK gets from `FieldComparison.differs`, for the same reason.
    #
    #    One pair means every comparable track reads the same way, so "which
    #    track" has no answer to give and the box's single line is the whole
    #    fact. Two or more means the tracks part company somewhere — some differ
    #    and others don't, or they differ differently — which is precisely what a
    #    column shows and a count cannot.
    pairs = {(d, v) for d, v in zip(disk, mb, strict=True) if v is not None}
    if len(pairs) > 1 and any(d != v for d, v in pairs):
        return True
    # 2. The tracks disagree with each other. None counts as a value here: a tag
    #    on six of eight tracks is uneven tagging, and the unevenness IS the
    #    finding — the same fact `consensus` reports for the album panel. Except
    #    for the medium-derived tags on a multi-disc release, where disagreeing
    #    is the release's shape rather than a defect — see `_MEDIUM_DERIVED`.
    if len(set(disk)) > 1 and not (multi_disc and candidate.owned in _MEDIUM_DERIVED):
        return True
    # 3. Differs from the album-level counterpart, on either side. Per track and
    #    per side, never across: a file's own `album_artist` is what its `artist`
    #    is measured against, so an album whose files disagree about the album
    #    artist doesn't make every track look like a featured credit.
    #
    #    Only where BOTH values are there. This rule says "this track is credited
    #    to somebody the album isn't", and an album credited to nobody supports no
    #    such claim — a file with an artist and no album artist is a missing album
    #    artist, which is the PANEL's row to report, and letting it earn a column
    #    here would spend one of three slots on it for every Picard-tagged album
    #    that skipped `aART`.
    counterpart = _ALBUM_COUNTERPART.get(candidate.owned)
    if counterpart is None:
        return False
    album_key = counterpart.value
    if any(
        d is not None and (album := _disk_value(t, album_key)) is not None and d != album
        for d, (t, _) in zip(disk, present, strict=True)
    ):
        return True
    return any(
        v is not None
        and (album := _as_display(getattr(m.tags, album_key))) is not None
        and v != album
        for v, (_, m) in zip(mb, present, strict=True)
        if m is not None
    )


def _only_identifiers(fields: Sequence[FieldComparison], kept: Sequence[_Candidate]) -> bool:
    """Whether every difference on one row sits in an identifier column (#319).

    By POSITION, not by label: `fields` is `[#, *kept, Length]`, and the index is
    what the table itself uses to line a cell up with its heading. Matching on the
    label would be a second, weaker way of asking the same question, and would
    answer it wrongly the day two columns share a name.
    """
    identifiers = {i + 1 for i, c in enumerate(kept) if c.owned in _IDENTIFIERS}
    marked = [i for i, f in enumerate(fields) if f.differs and f.mb]
    return bool(marked) and all(i in identifiers for i in marked)


def _matches_everywhere(candidate: _Candidate, present: Sequence[_Present]) -> bool:
    """Whether every track that HAS a MusicBrainz counterpart agrees with it.

    What separates a column worth collapsing from one the box has to state. A tag
    the files carry and MusicBrainz does not is deliberately not a *difference* —
    ONLY_DISK must never read as a finding, or the recovered Bandcamp URL becomes
    one — but it IS a change: a re-tag removes it. Naming it under the table as
    "the same on every track and matches MusicBrainz" would be false twice over,
    so it goes where removals are stated as removals.

    Vacuously true where MusicBrainz has no counterpart at all — a video track
    (#226), and every row of the disk-only view (#228). Nothing was compared
    there, so nothing can disagree, and the summary drops its MusicBrainz clause
    to say exactly that.
    """
    key = candidate.owned.value
    return all(
        _disk_value(t, key) == _as_display(getattr(m.tags, key))
        for t, m in present
        if m is not None
    )


def _choose_columns(
    present: Sequence[_Present], multi_disc: bool
) -> tuple[list[_Candidate], tuple[CollapsedField, ...], dict[str, tuple[str, ...]]]:
    """Split the candidates into the ones with a column and the ones named below.

    Judged over PRESENT, readable tracks only. A missing track's fields are all
    ONLY_MB and an unreadable one's are all UNREADABLE, so counting either would
    hand a column to every one of the eleven on the strength of a row whose
    finding is the row itself rather than anything in it.

    Returns the kept candidates, the collapsed set, and what each column ABSORBS
    — a tag another column already says (#319), which is a fourth disposition and
    not a fourth place: the tag is on the page, in the absorbing column.

    A candidate in none of the three is in the re-tag box, which shows whatever
    the surfaces above did not take.
    """
    kept: list[_Candidate] = []
    collapsed: list[CollapsedField] = []
    absorbed: dict[str, tuple[str, ...]] = {}
    earned = 0
    for candidate in _CANDIDATES:
        if multi_disc and candidate.owned is Owned.DISC_NUM:
            continue  # the number column already states it
        if candidate.owned in _PINNED_COLUMNS:
            kept.append(candidate)
            continue
        if not _earns_column(candidate, present, multi_disc):
            if _matches_everywhere(candidate, present):
                collapsed.append(_collapsed(candidate, present))
            continue
        # Absorbed by a column already keeping it — but only when it has no
        # MusicBrainz difference of its own, since a column cannot stand in for a
        # change it is not showing. Checked against `kept` rather than against
        # the rules again, so a subsumer that lost to the cap doesn't silently
        # swallow the tag that would have taken its place.
        subsumer = _SUBSUMED_BY.get(candidate.owned)
        if (
            subsumer is not None
            and any(c.owned is subsumer for c in kept)
            and _matches_everywhere(candidate, present)
        ):
            key = subsumer.value
            absorbed[key] = (*absorbed.get(key, ()), candidate.owned.value)
            continue
        if candidate.owned in _IDENTIFIERS:
            # Off the cap (#319): hidden by default, so it competes with nothing.
            kept.append(candidate)
            continue
        if earned < MAX_EARNED_COLUMNS:
            kept.append(candidate)
            earned += 1
        # Over the cap: it falls to the re-tag box, the one surface left that can
        # state it. Deliberately NOT collapsed — the collapsed set claims the
        # field matches MusicBrainz, which of one that overflowed BECAUSE it
        # differs would simply be false.
    return kept, tuple(collapsed), absorbed


def _collapsed(candidate: _Candidate, present: Sequence[_Present]) -> CollapsedField:
    """The one value behind a dropped column — the disk's, or MusicBrainz's when
    no file carries the tag and MusicBrainz has it (which is agreement too, in
    the direction where neither side has anything to say)."""
    key = candidate.owned.value
    values = [_disk_value(t, key) for t, _ in present]
    values += [_as_display(getattr(m.tags, key)) if m else None for _, m in present]
    return CollapsedField(
        candidate.label,
        next((v for v in values if v is not None), None),
        candidate.entity,
        candidate.credit,
    )


def tracklist(
    tracks: Sequence[tuple[str, TrackTags]],
    mb: Sequence[MBTrack],
    media: Sequence[Medium] = (),
) -> TracklistComparison:
    """Compare an album's files, track by track, against its MusicBrainz release.

    `tracks` is `(file_name, tags)` in file order; `mb` is the release's tracks
    in MusicBrainz order. Neither is authoritative about which file IS which
    track — see `_assign`.

    `media` describes the release's discs, so the result can be grouped by disc
    and each one named (#216). Optional: without it the discs are still grouped,
    from what the tracks themselves say, just unnamed.

    The columns are decided FIRST, from the pairings, and every row is then built
    to them (#309) — rather than each row deciding for itself, which is how a
    table gets cells that don't line up with its headings.
    """
    multi_disc = any((t.tags.disc_num or 1) > 1 for t in mb)
    assigned, extras = _assign(tracks, mb)
    present: list[_Present] = [
        (tags, None if tags.video else mb[i])
        for i, (_, tags) in sorted(assigned.items())
        if not tags.unreadable
    ]
    kept, collapsed, absorbed = _choose_columns(present, multi_disc)

    rows: list[ComparedTrack] = []
    for i, mb_track in enumerate(mb):
        # From MusicBrainz, never from the file: a row exists for every track the
        # RELEASE has, including ones with no file at all, and a missing track
        # has no tags to ask.
        disc = mb_track.tags.disc_num or 1
        entry = assigned.get(i)
        if entry is None:
            rows.append(
                ComparedTrack(
                    TrackState.MISSING, _track_fields(None, mb_track, multi_disc, kept), disc=disc
                )
            )
            continue
        name, tags = entry
        if tags.unreadable:
            rows.append(
                ComparedTrack(
                    TrackState.UNREADABLE,
                    _track_fields(None, mb_track, multi_disc, kept, unreadable=True),
                    file_name=name,
                    disc=disc,
                )
            )
            continue
        if tags.video:
            # Present, and that is the whole claim (#226). Compared against
            # `mb_track` every field would be a finding the user cannot act on:
            # Harmonist will not re-tag a video, so a title MusicBrainz spells
            # differently would sit on the page for good. Length is worse than
            # unactionable — a DVD track's runtime is the VIDEO's, intros and
            # all, and MusicBrainz frequently has no length for one at all.
            #
            # `mb=None` is how the module already says "MusicBrainz has no
            # opinion here": every field lands in ONLY_DISK, which is explicitly
            # not a difference, so the row draws one plain line of the file's own
            # values — the same shape `disk_tracklist` produces for #228.
            rows.append(
                ComparedTrack(
                    TrackState.PRESENT,
                    _track_fields(tags, None, multi_disc, kept),
                    file_name=name,
                    disc=disc,
                    video=True,
                )
            )
            continue
        rows.append(
            ComparedTrack(
                TrackState.PRESENT,
                _track_fields(tags, mb_track, multi_disc, kept),
                file_name=name,
                disc=disc,
            )
        )

    for name, tags in extras:
        rows.append(
            ComparedTrack(
                TrackState.UNREADABLE if tags.unreadable else TrackState.EXTRA,
                _track_fields(
                    None if tags.unreadable else tags,
                    None,
                    multi_disc,
                    kept,
                    unreadable=tags.unreadable,
                ),
                file_name=name,
                video=tags.video,
            )
        )
    return TracklistComparison(
        tracks=tuple(
            replace(r, mb_only_identifiers=_only_identifiers(r.fields, kept)) for r in rows
        ),
        media=tuple(media),
        columns=_columns(kept, multi_disc, absorbed),
        collapsed=collapsed,
    )


def disk_tracklist(tracks: Sequence[tuple[str, TrackTags]]) -> TracklistComparison:
    """The album's own tracks, with no MusicBrainz release to compare them to.

    For a release MusicBrainz has DELETED (#228). The tracks never depended on
    MusicBrainz — Harmonist read them off the files, and they are still there —
    so declining to show them drops the wrong half: this is exactly the evidence
    the user needs to go and find the replacement release.

    Deliberately not `tracklist(tracks, mb=[])`. That reaches a similar shape by
    a different route, and calls every row EXTRA — "not in MusicBrainz", which
    is a finding about the track. Nothing here is a finding: MusicBrainz was
    never asked. The rows are PRESENT with no counterpart, and `mb_available`
    tells the summary and the template to say so once, at the top.
    """
    multi_disc = any((t.disc_num or 1) > 1 for _, t in tracks)
    # Rule 1 can never fire here — there is no MusicBrainz side — so a column is
    # earned only by the tracks disagreeing with each other or with the album's
    # own values. That is the right reading of this view: it shows what the files
    # say, and what they say that is inconsistent is the only finding available.
    kept, collapsed, absorbed = _choose_columns(
        [(tags, None) for _, tags in tracks if not tags.unreadable], multi_disc
    )
    rows = [
        ComparedTrack(
            TrackState.UNREADABLE if tags.unreadable else TrackState.PRESENT,
            _track_fields(
                None if tags.unreadable else tags,
                None,
                multi_disc,
                kept,
                unreadable=tags.unreadable,
            ),
            file_name=name,
            disc=tags.disc_num or 1,
            video=tags.video,
        )
        for name, tags in tracks
    ]
    return TracklistComparison(
        tracks=tuple(rows),
        mb_available=False,
        columns=_columns(kept, multi_disc, absorbed),
        collapsed=collapsed,
    )


@dataclass(frozen=True)
class TrackIdentity:
    """What a file — or a MusicBrainz track — says about which track it is.

    The three things either side can offer, in descending order of how much they
    are worth. Deliberately a value with no tags, no paths and no MusicBrainz
    dicts in it: the ladder below is the same question for the album page and
    for the tagger, and they must not be able to answer it differently (#232).
    """

    release_track_id: str | None = None
    disc: int | None = None
    track: int | None = None

    @classmethod
    def of_tagset(cls, tags: TagSet) -> TrackIdentity:
        """The identity of a MusicBrainz track, as tagging would write it."""
        return cls(tags.mb_release_track_id, tags.disc_num, tags.track_num)


def identity_of(tags: TrackTags) -> TrackIdentity:
    """The identity one file claims for itself.

    An unreadable file claims nothing: it has no tags to read (#112), which is
    not the same as a readable file that carries no numbers, and inventing
    (disc 1, track None) for it would be a claim it never made.
    """
    if tags.unreadable:
        return TrackIdentity()
    return TrackIdentity(tags.release_track_id, tags.disc_num, tags.track_num)


def _by_release_track_id(identity: TrackIdentity) -> Hashable | None:
    return identity.release_track_id


def _by_number(identity: TrackIdentity) -> Hashable | None:
    # Paired with the disc, because track 4 exists on both halves of a 2-CD
    # release. A file that names no track names nothing.
    return None if identity.track is None else (identity.disc or 1, identity.track)


#: The ladder, best rung first. Each is tried for every file before the next is
#: tried for any — so one file's missing id can't cost another file its own.
_RUNGS: tuple[Callable[[TrackIdentity], Hashable | None], ...] = (
    _by_release_track_id,
    _by_number,
)


def assign(files: Sequence[TrackIdentity], tracks: Sequence[TrackIdentity]) -> list[int | None]:
    """Which MusicBrainz track each file is: the index of its track, or None.

    **By the release track id first — the one rung that is not a guess** (#232).
    That id names one position in one release, Harmonist writes it on every
    track it tags, and Picard writes the same. For anything either has touched,
    this is a lookup.

    Nothing else here can say that. TISM's *The White Albun* was tagged when
    MusicBrainz's release held only its CD; MusicBrainz later added two DVDs and
    moved the CD to position 2, and every one of those files — still correctly
    saying disc 1 — then keyed onto a video track, so a complete CD read as
    sixteen tracks that all differed and a disc that wasn't on disk. The ids in
    those same files named their true slots exactly, and Picard used them to
    re-file the album without being asked.

    Then, for files carrying no id — an adopted album Harmonist has never
    tagged — **the disc-and-track number**, trusted only where it is
    unambiguous: unique among the files, unique among MusicBrainz's tracks.

    Then **file order**, for whatever is left: files with no numbers, duplicate
    numbers, numbers MusicBrainz doesn't have, and unreadable files. An album
    with no numbers anywhere therefore behaves exactly as positional pairing
    always did.

    The last two rungs are guesses and can be wrong — a file mis-numbered as
    track 1 when it is really track 2 is compared against the wrong track, and
    so is the one it displaced. #136 is the escape hatch, letting the user say
    which track a file actually is. The first rung is what removes most albums
    from needing it at all.

    There is deliberately no length-similarity rung. A duration is not an
    identity: two recordings of the same length are common on one release, the
    odds get worse the longer the release, and being wrong writes another
    track's title and ids into the file with nothing on the page to show for it.
    """
    out: list[int | None] = [None] * len(files)
    taken: set[int] = set()

    for rung in _RUNGS:
        slot_of = _unique_slots(tracks, rung)
        claims = Counter(k for k in (rung(f) for f in files) if k is not None)
        for i, identity in enumerate(files):
            if out[i] is not None:
                continue
            key = rung(identity)
            # Two files claiming one key are two copies of a track, or a
            # mis-tag; either way the claim isn't unique and settles nothing.
            if key is None or claims[key] != 1:
                continue
            slot = slot_of.get(key)
            if slot is None or slot in taken:
                continue
            out[i] = slot
            taken.add(slot)

    free = iter([i for i in range(len(tracks)) if i not in taken])
    for i, slot in enumerate(out):
        if slot is None:
            out[i] = next(free, None)
    return out


def _unique_slots(
    tracks: Sequence[TrackIdentity], rung: Callable[[TrackIdentity], Hashable | None]
) -> dict[Hashable, int]:
    """MusicBrainz's side of one rung, keeping only the keys it answers once.

    A key MusicBrainz repeats identifies nothing, so it is dropped rather than
    resolved to its first holder — the ambiguity is the finding.
    """
    keys = [rung(t) for t in tracks]
    counts = Counter(k for k in keys if k is not None)
    return {k: i for i, k in enumerate(keys) if k is not None and counts[k] == 1}


def _assign(
    tracks: Sequence[tuple[str, TrackTags]],
    mb: Sequence[MBTrack],
) -> tuple[dict[int, tuple[str, TrackTags]], list[tuple[str, TrackTags]]]:
    """Decide which file is which MusicBrainz track — see `assign`.

    Returns `{mb_index: (file_name, tags)}` and the files that found no slot.
    """
    slots = assign(
        [identity_of(t) for _, t in tracks],
        [TrackIdentity.of_tagset(t.tags) for t in mb],
    )
    assigned: dict[int, tuple[str, TrackTags]] = {}
    leftover: list[tuple[str, TrackTags]] = []
    for entry, slot in zip(tracks, slots, strict=True):
        if slot is None:
            leftover.append(entry)
        else:
            assigned[slot] = entry
    return assigned, leftover


def _columns(
    kept: Sequence[_Candidate], multi_disc: bool, absorbed: Mapping[str, tuple[str, ...]] = {}
) -> tuple[TrackColumn, ...]:
    """The table's headings, matching what `_track_fields` emits, in that order.

    `absorbed` widens a column's `fields` to the tags it also accounts for, which
    is what keeps them out of the box and out of the collapsed set — they are on
    the page, inside the column that absorbed them (#319).
    """
    return (
        _number_column(multi_disc),
        *(
            TrackColumn(
                c.label,
                (c.owned.value, *absorbed.get(c.owned.value, ())),
                identifier=c.owned in _IDENTIFIERS,
            )
            for c in kept
        ),
        # Not a tag: nothing writes a length to a file, so it accounts for no
        # owned field and can never take one out of the re-tag box.
        TrackColumn("Length"),
    )


def _track_fields(
    tags: TrackTags | None,
    mb: MBTrack | None,
    multi_disc: bool,
    kept: Sequence[_Candidate],
    *,
    unreadable: bool = False,
) -> tuple[FieldComparison, ...]:
    """The compared fields of one row, always in `_columns` order.

    Always the FULL set of columns whatever the row's state, so the table keeps
    its alignment instead of special-casing its own shape per row — a missing
    track's fields are all ONLY_MB and an unreadable one's are all UNREADABLE.
    """
    fields = [
        compare_value(
            "#",
            kind=Kind.SCALAR,
            disk=_number(tags.disc_num, tags.track_num, multi_disc) if tags else None,
            mb=_number(mb.tags.disc_num, mb.tags.track_num, multi_disc) if mb else None,
            unreadable=unreadable,
        )
    ]
    for c in kept:
        key = c.owned.value
        row = compare_value(
            c.label,
            kind=c.kind,
            # Through `_disk_value`, the same reader the album panel uses, so a
            # list joins and a number stringifies identically in both — and so
            # the twenty-odd tags that live only in the `owned` snapshot can be
            # read at all. A named attribute read reached two of them.
            disk=_disk_value(tags, key) if tags else None,
            mb=_as_display(getattr(mb.tags, key)) if mb else None,
            unreadable=unreadable,
        )
        # Marked here rather than inside `compare_value` for the reason
        # `album_fields` states: which MusicBrainz thing an id names, and whether
        # a value is a credit, are properties of the FIELD. The two strings being
        # compared cannot say.
        fields.append(replace(row, entity=c.entity, credit=c.credit))
    fields.append(
        _length_field(
            tags.duration_ms if tags else None,
            mb.length_ms if mb else None,
            unreadable=unreadable,
        )
    )
    return tuple(fields)


def _length_field(disk_ms: int | None, mb_ms: int | None, *, unreadable: bool) -> FieldComparison:
    """Length, compared through LENGTH_TOLERANCE_MS *before* anything is shown.

    Within tolerance is not a difference at all — it's the same audio, encoded
    or trimmed a shade differently — so it renders as one plain line. Applying
    the tolerance first, rather than diffing "3:47" against "3:49" and hoping
    the user forgives it, is what keeps this page from contradicting the
    matcher's verdict on the very same release.
    """
    within_tolerance = (
        disk_ms is not None and mb_ms is not None and abs(disk_ms - mb_ms) <= LENGTH_TOLERANCE_MS
    )
    if within_tolerance and not unreadable:
        return FieldComparison(
            "Length", Kind.SCALAR, Agreement.MATCHES, disk=_mmss(disk_ms), mb=_mmss(mb_ms)
        )
    return compare_value(
        "Length", kind=Kind.SCALAR, disk=_mmss(disk_ms), mb=_mmss(mb_ms), unreadable=unreadable
    )


def _number(disc: int | None, track: int | None, multi_disc: bool) -> str | None:
    """A track's position as one displayable value — "4", or "2-4" on a release
    with more than one medium, where the number alone doesn't identify it."""
    if track is None:
        return None
    return f"{disc or 1}-{track}" if multi_disc else str(track)


def _mmss(ms: int | None) -> str | None:
    """A duration as "3:47". Rounded to the nearest second, so a length shown as
    equal to MusicBrainz's is equal to the second displayed."""
    if ms is None:
        return None
    seconds = (ms + 500) // 1000
    return f"{seconds // 60}:{seconds % 60:02d}"
