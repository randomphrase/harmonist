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
    #: Which field this row IS — the `Owned` value, or `"genre"` / `"comment"`
    #: for the two display-only rows. `label` is prose and free to be reworded;
    #: this is the identity, and it is what lets the page hang a per-field
    #: annotation off one row without matching on its wording (#329).
    #:
    #: The annotation itself is deliberately not here, for the reason the NAME
    #: behind an id isn't: it comes from the release payload, which this module
    #: never sees. This is the join key, not the data.
    key: str | None = None

    @property
    def differs(self) -> bool:
        """Whether this row is something the user should look at.

        ONLY_DISK is a finding when the field is COMPARABLE, and silent when it
        is not (#340). The two cases look identical in the agreement alone, and
        `comparable` is the only thing that tells them apart:

        * MusicBrainz has no counterpart for the field — `genre`, and the
          recovered Bandcamp URL in `comment`. Nothing is pending; a re-tag
          preserves them. Calling those findings would put every adopted album
          permanently in the Inbox over a URL Harmonist put there itself. This is
          the case the blanket exclusion was written for.
        * MusicBrainz has a counterpart and simply no value — a barcode it does
          not know. **A re-tag deletes that**, so it is exactly the kind of
          pending change this table exists to state. Excluding it left the page
          saying "1 of 18 tags differ" about an album the update flag had
          flagged for two others, because `owned.diff` counts `'X' -> None` and
          this did not.
        """
        if self.agreement is Agreement.ONLY_DISK:
            return self.comparable
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
    also_matches: _Accepted = (),
) -> FieldComparison:
    """Compare one on-disk value against one MusicBrainz value.

    The core of the model. A per-track field has exactly one on-disk value, so
    this is what the tracklist uses directly; `compare_field` wraps it for the
    album panel, where the on-disk value is a consensus across the tracks.

    `also_matches` holds the other on-disk spellings that count as agreement —
    exact strings, never patterns. It exists because a field can have more than
    one correct form on disk, and two fields do:

    * an album title with the release disambiguation appended, which Picard
      writes when told to and which is the same album (#283);
    * any release country THIS release names, since Picard writes whichever of
      them `preferred_release_countries` matches while MusicBrainz's scalar
      `country` is only the first (#346).

    A set rather than one string because of the second: a release is issued in
    as many countries as it is issued in. Spelled as a union of concrete
    containers rather than `Collection[str]`, deliberately: `str` satisfies
    `Collection[str]`, so a future `also_matches="DE"` would type-check and
    quietly become a SUBSTRING test — the shape of bug this repo keeps paying
    for, since it works on the value that prompted it.

    This module stays deliberately free of runtime imports, so it is told the
    accepted spellings rather than deriving them; what makes a second spelling
    legitimate is MusicBrainz's business, and `models.title_with_disambiguation`
    and `tagger.release_events` are where that knowledge lives.
    """
    if unreadable:
        return FieldComparison(label, kind, Agreement.UNREADABLE, mb=mb, consensus=tracks)
    if disk is None and mb is None:
        return FieldComparison(label, kind, Agreement.MATCHES, consensus=tracks)
    if disk is None:
        return FieldComparison(label, kind, Agreement.ONLY_MB, mb=mb, consensus=tracks)
    if mb is None:
        return FieldComparison(label, kind, Agreement.ONLY_DISK, disk=disk, consensus=tracks)
    if disk == mb or disk in also_matches:
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
    also_matches: _Accepted = (),
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
    Owned.ALBUM_ARTISTS: Kind.TEXT,
    Owned.MB_ALBUM_ARTIST_IDS: Kind.TEXT,
    Owned.MB_RELEASE_GROUP_ID: Kind.TEXT,
    Owned.MB_ALBUM_TYPE: Kind.SCALAR,
    Owned.MB_ALBUM_STATUS: Kind.SCALAR,
    Owned.MB_ALBUM_COUNTRY: Kind.SCALAR,
    Owned.COMPILATION: Kind.SCALAR,
    Owned.DATE: Kind.SCALAR,
    Owned.ORIGINAL_DATE: Kind.SCALAR,
    Owned.SCRIPT: Kind.SCALAR,
    Owned.LABEL: Kind.TEXT,
    # TEXT since #334: a release can name several catalogue numbers, and a
    # joined pair stacks better than it fits beside an arrow.
    Owned.CATALOG_NUMBER: Kind.TEXT,
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


def _and_list(labels: Sequence[str]) -> str:
    """`["a"]` → "a", `["a", "b"]` → "a and b", `["a", "b", "c"]` → "a, b and c".

    Both callers NAME the tags they are talking about rather than counting them,
    for the reason #112 gives: a reader told "2 tags differ" under a band of
    eight rows still has to go and find which two. Callers guard the empty case
    themselves — there is no sentence to build without a label in it.
    """
    return labels[0] if len(labels) == 1 else f"{', '.join(labels[:-1])} and {labels[-1]}"


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
#: dropped — it is said beside the badge, as an album-level note about the
#: identity rather than a row among the tags (#361, `_mb_merged.html`), which is
#: also why `PANEL_FIELDS` names this field even though no row derives it. #268
#: owns the merge itself.
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
    # Row 1-4: what the release IS, beside who it is BY. `album_artists` sits
    # directly under the singular phrase it unjoins (#322), and `compilation`
    # (#323) under the release facts it belongs with — it is set from the
    # release artist, so it faces that artist's ids across the row.
    Owned.ALBUM,
    Owned.ALBUM_ARTIST,
    Owned.MB_RELEASE_GROUP_ID,
    Owned.ALBUM_ARTISTS,
    Owned.MB_ALBUM_TYPE,
    Owned.ALBUM_ARTIST_SORT,
    Owned.COMPILATION,
    Owned.MB_ALBUM_ARTIST_IDS,
    # Row 5-6: which edition, and when.
    Owned.MB_ALBUM_STATUS,
    Owned.MB_ALBUM_COUNTRY,
    Owned.DATE,
    Owned.ORIGINAL_DATE,
    # Row 7-9: the release's paperwork.
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


#: The two album fields with a second legitimate on-disk spelling — see
#: `album_fields`. Named rather than inlined so the special cases are visible
#: from the table above rather than buried in the loop.
_ALIASED_FIELD = "album"
_MULTI_VALUED_FIELD = Owned.MB_ALBUM_COUNTRY.value

#: The accepted-spellings container — see `compare_value`. Concrete types, never
#: `Collection[str]`, so a bare string cannot be passed by mistake.
type _Accepted = frozenset[str] | tuple[str, ...]


def _accepted(
    disk_attr: str,
    album_title_alias: str | None,
    accepted_countries: _Accepted,
) -> _Accepted:
    """The other on-disk values that count as agreement on this row.

    Empty for every row but two, and that is the point: a tolerance wide enough
    to apply generally would stop the panel reporting real drift. See
    `album_fields` for what makes each of the two legitimate.
    """
    if disk_attr == _ALIASED_FIELD:
        return (album_title_alias,) if album_title_alias else ()
    if disk_attr == _MULTI_VALUED_FIELD:
        return accepted_countries
    return ()


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
    accepted_countries: _Accepted = (),
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
    from the release the caller already holds (#283).

    `accepted_countries` is the same idea on the Country row (#346): every
    country the release names, from `tagger.release_events`, of which
    MusicBrainz's scalar `country` is only the first. Picard writes whichever
    one `preferred_release_countries` matches, and the page must agree with
    `tagger.plan_album` about whether that is a difference — the page saying a
    tag differs while the Library says the album is up to date is worse than
    either answer alone.

    Those two rows are the only ones with a legitimate second form.
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
            also_matches=_accepted(disk_attr, album_title_alias, accepted_countries),
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
                key=disk_attr,
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
        """The TAGS clause of the MusicBrainz note (#328) — see `headline`.

        A whole clause rather than a count to be glued to a label, so the two
        cases read naturally; assembling that in a template would scatter the
        wording across two files.

        It no longer names MusicBrainz. The note it sits in is the MusicBrainz
        note — it carries the hexagon — and since #328 joined this to the
        tracklist's clause in one line, saying it in both said it twice.

        Counted over `comparable` on BOTH sides. It used to say "All 9 fields
        match MusicBrainz" for an album whose genre and comment MusicBrainz had
        never been asked about — and, with unreadable files, could reach
        "9 of 7 differ", since every field goes UNREADABLE while only seven of
        them were ever comparable.

        Assumes there was a release to compare against. `headline` answers for
        the disk-only view (#228) before reaching here, so that "nothing was
        compared" is said once for the whole note rather than twice in it.
        """
        fields = self.comparable
        n = len([f for f in fields if f.differs])
        if not fields:
            return "No tags to compare"
        if n == 0:
            return f"All {len(fields)} tags match"
        return f"{n} of {len(fields)} tags differ"


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
class HeadingSlot:
    """One cell of a disc heading (#320).

    The heading lays out three of them — the disc's name, its medium, its track
    count — and the MusicBrainz line beneath repeats ONLY the ones that changed,
    each held in its own column so it sits directly under its counterpart. The
    register the track rows already use: a difference is a vertical scan, and a
    cell that agrees stays blank rather than saying the same thing twice.

    `mb` is therefore None on a slot that matches, which is not the same as a
    slot MusicBrainz has no value for — that one has nothing to restate either,
    and both correctly render as empty.
    """

    disk: str | None
    mb: str | None = None
    #: Rendered in the quiet voice the medium and the track count share, rather
    #: than as the heading's name. A property of the SLOT, so the template
    #: doesn't have to know which of the three it is drawing.
    meta: bool = False


@dataclass(frozen=True)
class DiscHeading:
    """What a disc heading states, compared against the files that make it up.

    The three tags `_MEDIUM_DERIVED` names, rolled up out of the tracklist and
    into the one place on the page that already belongs to the disc (#320).

    They are track-scoped for a real reason — `owned.Scope` explains it: they
    describe the MEDIUM, and a multi-disc release genuinely carries a different
    one per track. But that makes them per-disc CONSTANTS wearing per-track
    clothes: a column of them says "Live Angle" twenty-nine times, or worse,
    "— → Live Angle" twenty-nine times. #309's uniform-difference rule doesn't
    catch it, because across the whole album the readings aren't uniform — they
    vary by disc, which is exactly what the heading above each disc is for.

    Only built where it can state the whole truth — see `_per_disc_constant`.
    """

    position: int
    subtitle: FieldComparison
    media: FieldComparison
    track_total: FieldComparison

    @property
    def fields(self) -> tuple[FieldComparison, ...]:
        return (self.subtitle, self.media, self.track_total)

    @property
    def differs(self) -> bool:
        """Whether a MusicBrainz line is worth drawing under this heading."""
        return any(f.differs for f in self.fields)

    def _name(self, subtitle: str | None) -> str:
        """ "Disc 2 — Live Angle", or plain "Disc 2" when the disc has no name.

        The same shape `Medium.label` builds, from whichever side is being
        rendered — the heading's whole point is that the disc's NAME is the disc
        subtitle, so the two cannot be allowed to disagree about how it reads.
        """
        return f"Disc {self.position} — {subtitle}" if subtitle else f"Disc {self.position}"

    def _count(self, total: str | None) -> str | None:
        """ "16 tracks", from a track total that is a bare number on disk."""
        if total is None:
            return None
        return f"{total} track{'' if total == '1' else 's'}"

    @property
    def slots(self) -> tuple[HeadingSlot, ...]:
        """The three cells, in the order they are laid out.

        A slot's `mb` is filled only where that field differs, so the second line
        carries the change and nothing else. The name slot is the exception worth
        noting: it restates the WHOLE name rather than the subtitle alone,
        because "Disc 2" over "Disc 2 — Live Angle" is how a disc that has gained
        a name reads, and a bare "Live Angle" under a bare "Disc 2" would not.
        """
        return (
            HeadingSlot(
                self._name(self.subtitle.disk),
                self._name(self.subtitle.mb) if self.subtitle.differs else None,
            ),
            HeadingSlot(
                self.media.disk,
                self.media.mb if self.media.differs else None,
                meta=True,
            ),
            HeadingSlot(
                self._count(self.track_total.disk),
                self._count(self.track_total.mb) if self.track_total.differs else None,
                meta=True,
            ),
        )

    @property
    def mark_index(self) -> int:
        """Which slot carries the hexagon: the LAST one that differs.

        One mark per line rather than one per cell, the same rule the track rows
        follow — the hexagon is what makes the line readable to someone who can't
        distinguish the purple, and three of them across one heading is noise.
        Last rather than first so it terminates the line it marks.
        """
        return max((i for i, s in enumerate(self.slots) if s.mb is not None), default=0)


@dataclass(frozen=True)
class DiscGroup:
    """A disc and the tracklist rows belonging to it."""

    medium: Medium
    tracks: tuple[ComparedTrack, ...]
    #: What the files say this disc is, against what MusicBrainz says (#320).
    #:
    #: None wherever there is nothing to compare, and the template then renders
    #: the heading from `medium` alone, exactly as it did before: a disc NOBODY
    #: ripped (a difference against files that do not exist has nothing to say),
    #: a disc whose files Harmonist could not read (#112 — it must not report
    #: tags it never managed to look at), the disk-only view where MusicBrainz
    #: has no opinion at all (#228), and every single-disc album, which draws no
    #: heading to roll anything up into.
    heading: DiscHeading | None = None

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
    # `mb_available` lived here, the tracklist half of the same flag the album
    # panel carries (#228). It went in #328, when its last two readers did: the
    # summary's "no comparison" branch (moved to `headline`, which asks the album
    # once for the whole note) and `collapsed_summary`'s conditional tail
    # (dropped — see above).
    #
    # Deleted rather than left for a future reader, and deliberately: "was there
    # a release?" is ONE fact, and two flags for one fact is a pair that can
    # disagree. They were only ever set together, by `_album_disk_view`, which is
    # exactly the shape that holds right up until someone sets one of them.
    #: The table's columns, in order, matching each row's `fields` positionally
    #: (#309). A property of THIS comparison rather than a module constant: which
    #: per-track tags are worth a column is a fact about this album's tags, not
    #: about Harmonist.
    columns: tuple[TrackColumn, ...] = field(default_factory=tuple)
    #: The per-track tags with no column because every track agreed and matched
    #: MusicBrainz, each with the one value behind it. Named under the table so
    #: a collapsed column can't be mistaken for a field nobody looked at (#112).
    collapsed: tuple[CollapsedField, ...] = field(default_factory=tuple)
    #: The disc headings that carry a comparison of their own (#320), by
    #: position. Empty when the medium-derived tags were not rolled up — see
    #: `_per_disc_constant` — in which case every heading renders from `media`
    #: alone, as it did before.
    headings: tuple[DiscHeading, ...] = field(default_factory=tuple)

    @property
    def heading_fields(self) -> frozenset[str]:
        """The per-track tags the DISC HEADINGS account for (#320).

        A fourth disposition and not a fourth place, exactly as #319's absorbed
        set is: the tag is on the page, in the heading above the disc it belongs
        to. So it gets no column, no collapsed entry and no re-tag-box row —
        which is only true because the headings state it for every disc, the
        condition `_per_disc_constant` enforces before any of this is built.

        All three or none. The heading is one line describing one disc's shape,
        and half of it comparing against the files while the other half quietly
        described MusicBrainz would be two registers in one sentence.
        """
        return frozenset(f.value for f in _MEDIUM_DERIVED) if self.headings else frozenset()

    @property
    def shown_fields(self) -> frozenset[str]:
        """The per-track owned tags this section states, in a column or under it.

        The tracklist's half of what scopes the re-tag box (#291, #297); the
        panel's half is `PANEL_FIELDS`. Read off what is rendered rather than
        recomputed, so "no field appears in both places" holds by construction
        rather than by two tables agreeing.

        The disc headings are included, because they show what they account for
        (#320) — a tag rolled up into them has been stated, per disc, and the box
        restating it underneath would be the same row printed twice.

        **The collapsed set is included too, since #360.** It used to be left out
        on the grounds that a collapsed field matched MusicBrainz everywhere, so
        a re-tag had nothing to say about it and it could not reach the box — and
        that omission doubled as a net: *"if one ever did, that is a real
        difference the page had better state somewhere rather than swallow."*
        The band now carries differing readings deliberately, so the net is no
        longer needed and has become the bug it was guarding against: leaving
        collapsed out would print every one of those rows a second time, in the
        box, in another section.

        What replaces the net is the band rendering the difference. A field in
        `collapsed` is stated either way — this is the assertion that it is.
        """
        return (
            frozenset(f for c in self.columns for f in c.fields)
            | self.heading_fields
            | frozenset(f.field for f in self.collapsed if f.field)
        )

    @property
    def identifier_columns(self) -> tuple[TrackColumn, ...]:
        """The columns the "Show identifiers" control reveals (#319)."""
        return tuple(c for c in self.columns if c.identifier)

    @property
    def reveal_identifiers(self) -> bool:
        """Whether the identifier columns should start SHOWN (#339).

        True exactly when hiding them would leave a stated difference with
        nothing visible behind it: at least one row whose every difference is in
        a hidden column. That row's own MusicBrainz line is suppressed too — by
        `mb_only_identifiers`, which exists so a row cannot get "an empty purple
        row carrying only a hexagon" — so the table shows the reader nothing at
        all while the summary above it counts the row as differing.

        Deliberately narrower than "any identifier differs". A track differing on
        its title AND its ISRC already has a visible difference accounting for
        the count, so the identifiers stay behind their control and the table
        stays narrow — which is what #319 was for. Revealing on any identifier
        difference would undo it on most albums that have one.
        """
        return any(t.mb_only_identifiers for t in self.tracks)

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
        return f"{_and_list(labels)} {'differs' if len(labels) == 1 else 'differ'} here."

    # `collapsed_summary` lived here: "Artist sort, ISRC and 3 others are the
    # same on every track and match MusicBrainz", the label on a <details> that
    # hid the values (#309).
    #
    # Both halves of it went in #328. The values are now shown IN THE OPEN, in
    # the same label/value grid the album panel uses, so a sentence naming the
    # fields directly above a grid that names them again was the same list
    # twice — and the grid is the better proof that they were checked, which is
    # all #112 ever wanted from it. What is left is a static caption in the
    # template, so there is nothing here to compute.
    #
    # "and match MusicBrainz" went with it, and that removed a conditional
    # rather than shortening a string: the clause was guarded on `mb_available`
    # because a disk-only view (#228) compared nothing and could claim nothing.
    # With no claim about MusicBrainz in the caption at all, both views tell the
    # truth with one sentence. Agreement is already carried visually — a value
    # that differs gets a hexagon and a purple line, and this band has neither.

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
        headings = {h.position: h for h in self.headings}
        return tuple(
            DiscGroup(known.get(pos, Medium(position=pos)), tuple(rows), headings.get(pos))
            for pos, rows in sorted(by_position.items())
        )

    @property
    def differing(self) -> tuple[ComparedTrack, ...]:
        return tuple(t for t in self.tracks if t.differs)

    @property
    def clean(self) -> bool:
        """Nothing in the tracklist for the user to act on (#352).

        The other half of what decides whether the note is drawn as an advisory
        or as a finding. Deliberately enumerated against `summary`'s clauses —
        a differing track, one missing / unreadable / not in MusicBrainz, a
        disc heading that differs, a disc absent from disk, a tag the band under
        the table states a change in — because the two are one statement in two
        registers, and a tint that disagrees with the sentence beside it is
        worse than no tint at all. Add a clause there, add it here; every clause
        it can emit has a test that would go red.

        That last one was a clause added to neither (#373), and it cost more
        than a contradiction: on an album whose ONLY difference is a collapsed
        per-track field, both halves read clean, the note went advisory, and
        `_album_update.html` drew no section at all — no chip, no **Re-tag from
        MB** — while the Library listed the album under Update available.

        `not counted` is NOT clean: "No tracks to compare" is the disk-only view
        saying nothing was checked, which is not the same as nothing being wrong.
        """
        absent = {g.medium.position for g in self.discs if g.absent}
        counted = [t for t in self.tracks if t.disc not in absent]
        if not counted or absent:
            return False
        # `differs` covers the three states as well as a mismatch: a track that
        # is MISSING, UNREADABLE or EXTRA differs by virtue of being one — which
        # is why the count alone is enough here, and why the tests pin it.
        if any(t.differs for t in counted):
            return False
        if any(f.differs for f in self.collapsed):
            return False
        return not any(g.heading and g.heading.differs for g in self.discs)

    @property
    def summary(self) -> str:
        """The TRACKS clause of the MusicBrainz note (#328) — see `headline`.

        Missing, unreadable and extra tracks get their own clause rather than
        being folded into the count: "3 of 10 tracks differ" is true of an album
        with a dead file, but it isn't what the user needs to be told. So does a
        tag the band under the table states a change in (#373) — the count only
        speaks for what the ROWS show, and that band is the rest of the table.

        MusicBrainz is not named here, for the reason `AlbumComparison.summary`
        gives: since #328 this and the tags clause share one line under one
        hexagon, and naming it in both said it twice.

        Assumes there was a release to compare against — `headline` answers for
        the disk-only view (#228) before reaching here.
        """
        if not self.tracks:
            return "No tracks to compare"

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
        # A tag with no column, stated as a pending change by the band under the
        # table (#360). Named, not counted, and NOT folded into the track count:
        # it reaches every track, so "24 of 24 tracks differ" would point the
        # reader at a table where every row is unmarked.
        stated = [f.label for f in self.collapsed if f.differs]
        if total and n:
            verb = "differs" if n == 1 else "differ"
            clauses.append(f"{n} of {total} tracks {verb}")
        elif total and not stated:
            # "All 24 tracks match" is not a second clause beside one of these,
            # it is its contradiction (#373): a re-tag would change a tag on
            # every one of them. The band's clause is the whole of what the
            # tracks have to say, so this one stands down.
            clauses.append(f"All {total} tracks match" if total > 1 else "The track matches")
        if stated:
            clauses.append(
                f"{_and_list(stated)} {'differs' if len(stated) == 1 else 'differ'} on every track"
            )

        # A disc whose DESCRIPTION differs (#320). Its own clause, because the
        # roll-up moved those three tags off the rows: without this the album
        # above reads "All 7 tracks match" over a heading drawing a difference in
        # purple, which is the headline contradicting the table. Not folded into
        # the track count either — nothing is wrong with the tracks, and saying
        # "7 of 7 differ" over a disc that is merely named differently would
        # point at the wrong thing.
        #
        # By NUMBER, not by `medium.label` (#328). #216 put the medium's name in
        # here — "Disc 1 — Bonus DVD not on disk" — because at the time the disc
        # heading rendered at the column headings' size and colour and could not
        # be read (the cascade bug #320 fixed). The heading carries the name
        # legibly now, so repeating it here is the same fact twice on a line that
        # already holds three clauses.
        def named(discs: list[DiscGroup]) -> str:
            return ", ".join(f"Disc {g.medium.position}" for g in discs)

        odd_discs = [g for g in self.discs if g.heading and g.heading.differs]
        if odd_discs:
            clauses.append(f"{named(odd_discs)} {'differs' if len(odd_discs) == 1 else 'differ'}")
        if absent_discs:
            clauses.append(f"{named(absent_discs)} not in your files")
        # "not in your files", not "not on disk" (#326). Three spellings of one
        # syllable — disk, Disc 2, DVD-Video — landed inside eleven words, which
        # is the pun #245 already removed from the Library tile one surface over.
        # The phrase that replaces it is chosen to MIRROR "not in MusicBrainz"
        # below: they are the two opposite findings this table can reach, and
        # reading as a pair is worth more than three characters of length.
        for state, phrase in (
            (TrackState.MISSING, "not in your files"),
            (TrackState.UNREADABLE, "unreadable"),
            (TrackState.EXTRA, "not in MusicBrainz"),
        ):
            count = sum(1 for t in counted if t.state is state)
            if count:
                clauses.append(f"{count} {phrase}")
        return " · ".join(clauses)


def headline(album: AlbumComparison, tracks: TracklistComparison) -> str:
    """The legend of the page's ONE MusicBrainz note (#328).

    The hexagon band used to be drawn twice — once over the Tags panel, once
    over the tracklist — saying the same thing about the same fetch in two
    places. It is now drawn once, in the album panel beside **Re-tag from MB**,
    which is the action a difference leads to.

    Composed HERE rather than in the template, for the reason each half is a
    whole clause rather than a count: the wording of a sentence belongs in one
    place, and a template joining fragments is how "MusicBrainz" ends up in a
    line twice.

    The disk-only view (#228) is answered once, for the whole note, instead of
    each half saying "no comparison" beside the other. Neither clause has to
    carry that case, which is why neither of them does.
    """
    if not album.mb_available:
        n = len(tracks.tracks)
        return f"No comparison — showing your own tags and {n} track{'s' if n != 1 else ''}"
    return f"{album.summary} · {tracks.summary}"


def advisory(album: AlbumComparison, tracks: TracklistComparison) -> bool:
    """Is the note `headline` composed purely advisory — nothing to act on (#352)?

    The note is one band whichever it is saying, and on a clean album a tinted
    band reads as a warning about a page where nothing is wrong. This is what
    lets the tint drop to neutral there while a note carrying findings keeps its
    colour, so the two stay tellable apart at a glance.

    Both halves must be clean, and there must have been something to compare:
    the disk-only view (#228) is not an advisory that all is well, it is a note
    saying MusicBrainz could not be reached for an answer.

    The tags half is counted over `comparable`, not over `differing`, for the
    reason `AlbumComparison.summary` is: Genre and the comment are shown but
    never compared, and a tint drawn from a wider set than the sentence beside
    it would contradict it on exactly the album that reads "All 22 tags match".
    """
    fields = album.comparable
    if not (album.mb_available and fields):
        return False
    return not any(f.differs for f in fields) and tracks.clean


#: The owned fields the album PANEL accounts for — every compared row of it, plus
#: the release id, which it states as a note instead of a row (#361).
#:
#: Half of what the re-tag plan's box (#291) is scoped against (#297): the box
#: states what a re-tag would change in the fields nothing else on this page
#: shows, and a field the panel already accounts for would otherwise be restated
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
#:
#: `MB_ALBUM_ID` is named by hand, and is the one member that has to be: it has
#: no row in `_ALBUM_FIELDS` to be derived from (`_NOT_COMPARED` took it out in
#: #298), and the surface that replaced the row is prose rather than a table —
#: `_mb_merged.html`, beside the badge whose meaning the merge changed. The
#: name is here rather than the whole of `_NOT_COMPARED` because membership of
#: that set is only a statement that the panel does not COMPARE a field; a
#: second field joining it would need a surface of its own before it belonged
#: here, and inheriting the exemption silently is how a finding goes missing.
PANEL_FIELDS: frozenset[str] = frozenset(
    disk_attr for _, disk_attr, mb_attr, _ in _ALBUM_FIELDS if mb_attr
) | {Owned.MB_ALBUM_ID.value}


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
#: Rule 1 applies to them only where the disc HEADINGS have not taken them — on
#: a multi-disc release they normally have, and the heading is then the one place
#: each of them appears (#320). Where the roll-up is off, rule 1 applies in full:
#: a track total that has genuinely gone stale is a difference from MusicBrainz
#: and still earns its column.
#:
#: The order is the order the heading lays them out in, and `DiscHeading` is
#: built from the same three — one list, so the set that skips the columns and
#: the set the heading states cannot drift apart.
_MEDIUM_DERIVED: tuple[Owned, ...] = (Owned.DISC_SUBTITLE, Owned.MEDIA, Owned.TRACK_TOTAL)


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
    """A per-track tag with no column, and the one reading every track carries.

    Safe to state as a single line precisely BECAUSE the column was dropped:
    rule 2 hands a column to any tag whose tracks disagree with each other, so a
    tag that got no column has one reading on disk. That is also why the band
    under the table is a short list and not a wider table — stating N identical
    rows needs one row, not N.

    It used to mean agreement as well, and only agreement. A tag reading the
    same on every track and DIFFERING from MusicBrainz fell past this into the
    re-tag box, up in the album's Tags section — a per-track fact stated away
    from the tracks, among leftovers, labelled `all tracks` forty pixels above a
    band captioned "The same on every track" (#360). Both readings are carried
    here now, and `differs` says which this is.

    `field` is the owned key, so `shown_fields` can name what the band accounts
    for. Before #360 it did not need one: nothing collapsed could reach the box,
    so there was nothing to keep out of it.
    """

    label: str
    value: str | None = None
    entity: str | None = None
    credit: bool = False
    #: The owned key this band row accounts for.
    field: str = ""
    #: What MusicBrainz reads, when there was a counterpart to read. None both
    #: when MusicBrainz has no value and when nothing was comparable at all —
    #: `differs` is what tells those apart, because only the first is a change.
    mb: str | None = None
    #: Whether a re-tag would change this tag. Decided in `_collapsed`, where the
    #: tracks are still in scope: a band row must never claim a difference in the
    #: disk-only view (#228), where MusicBrainz offered no opinion to differ from.
    differs: bool = False


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
    # 2. The tracks disagree with each other — ON EITHER SIDE. None counts as a
    #    value here: a tag on six of eight tracks is uneven tagging, and the
    #    unevenness IS the finding — the same fact `consensus` reports for the
    #    album panel. Except for the medium-derived tags on a multi-disc release,
    #    where disagreeing is the release's shape rather than a defect — see
    #    `_MEDIUM_DERIVED`.
    #
    #    The MusicBrainz half was missing until #374, and rule 1's None filter
    #    could not stand in for it: an ISRC MusicBrainz holds on one track of 24
    #    leaves a single pair once the 23 it says nothing about drop out, so
    #    "which track" was ruled unanswerable on an album where the answer is
    #    track 3. Both halves together are also the band's whole claim — one
    #    reading, on both sides — which is why `_collapsed` asks the same pair.
    shaped_by_the_discs = multi_disc and candidate.owned in _MEDIUM_DERIVED
    one_reading = _one_reading_on_disk(candidate, present)
    one_answer = _one_reading_in_mb(candidate, present)
    if not (one_reading and one_answer) and not shaped_by_the_discs:
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


def _one_reading_on_disk(candidate: _Candidate, present: Sequence[_Present]) -> bool:
    """Whether every present track carries the same value for this tag (#360).

    What the band under the table claims, stated as a predicate so the claim is
    checked rather than inferred. Rule 2 makes it true of almost everything that
    reaches it — a tag whose tracks disagree earns a column — with the one
    exemption that rule carves out, the medium-derived tags on a multi-disc
    release, where per-disc variation is the release's shape and not a finding.

    `None` counts as a value here, exactly as rule 2 counts it: a tag on six of
    eight tracks is two readings, and a band line claiming one of them would be
    hiding the other.
    """
    key = candidate.owned.value
    return len({_disk_value(t, key) for t, _ in present}) <= 1


def _one_reading_in_mb(candidate: _Candidate, present: Sequence[_Present]) -> bool:
    """Whether MusicBrainz says the same thing about this tag on every track (#374).

    The other half of the band's claim, and the half that went unchecked. A band
    line is one reading and one arrow, so it can only be drawn over a field both
    sides read one way; where MusicBrainz reads two ways the line has to pick one
    of them, which is how an ISRC being ADDED to track 3 came out as an ISRC
    being removed from all 24.

    Over the tracks MusicBrainz has a counterpart for, and only those. A track it
    has never heard of offers no reading to vary — a video track (#226), and every
    row of the disk-only view (#228), where this is vacuously true and nothing can
    earn a column on MusicBrainz's account. A counterpart that simply has NO value
    for the tag is a reading like any other, and the one this rule turns on: it is
    MusicBrainz saying "not here", which is what the tracks that do have one part
    company with.
    """
    key = candidate.owned.value
    return len({_as_display(getattr(m.tags, key)) for _, m in present if m is not None}) <= 1


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
    present: Sequence[_Present], multi_disc: bool, rolled_up: bool = False
) -> tuple[list[_Candidate], tuple[CollapsedField, ...], dict[str, tuple[str, ...]]]:
    """Split the candidates into the ones with a column and the ones named below.

    Judged over PRESENT, readable tracks only. A missing track's fields are all
    ONLY_MB and an unreadable one's are all UNREADABLE, so counting either would
    hand a column to every one of the eleven on the strength of a row whose
    finding is the row itself rather than anything in it.

    Returns the kept candidates, the collapsed set, and what each column ABSORBS
    — a tag another column already says (#319), which is a fourth disposition and
    not a fourth place: the tag is on the page, in the absorbing column.

    `rolled_up` says the disc headings have taken the medium-derived three (#320)
    — the same disposition again, one surface further out.

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
        if rolled_up and candidate.owned in _MEDIUM_DERIVED:
            continue  # the disc heading above each group already states it (#320)
        if candidate.owned in _PINNED_COLUMNS:
            kept.append(candidate)
            continue
        if not _earns_column(candidate, present, multi_disc):
            # Whether it agrees with MusicBrainz or not (#360) — but only if the
            # files really do read the same way, which is the band's whole claim.
            #
            # Agreement used to be the gate, and it sent a uniform DIFFERENCE up
            # into the album's Tags section to be listed among leftovers as "all
            # tracks", directly above a band captioned "The same on every track"
            # that did not carry it. Rule 2 hands a column to anything whose
            # tracks disagree, so for almost everything "no column" already means
            # one reading and the gate can simply go.
            #
            # Almost. Rule 2 exempts the medium-derived tags on a multi-disc
            # release, where disagreeing is the release's shape rather than a
            # defect — so `media` can reach here reading CD on one disc and
            # Digital Media on another. That case is `_collapsed`'s to handle,
            # and it handles it by declining to claim a change; it must still be
            # collapsed, because being NAMED under the table is what separates a
            # tag that agreed from one nobody looked at (#112), and for an album
            # of nothing but video this band is the only surface left.
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
    """The one reading behind a dropped column, and whether a re-tag would change it.

    `value` is the disk's, falling back to MusicBrainz's when no file carries the
    tag — which keeps the agreement case reading as it always has, including the
    direction where neither side has anything to say.

    `differs` is decided here rather than by the template comparing two strings,
    because it takes two pieces of context the template does not have.

    First, only tracks MusicBrainz has an opinion about count. Where none does —
    the disk-only view (#228), a video track (#226) — there is nothing to differ
    FROM, and a band row claiming a pending change would be inventing an opinion
    nobody offered. That is the same exclusion `FieldComparison.differs` makes,
    for the same reason.

    Second, it takes ONE reading on EACH SIDE. Rule 2 gives a column to any tag
    whose tracks disagree on either — so that holds for nearly everything here —
    except the medium-derived tags on a multi-disc release, which rule 2 exempts
    because varying by disc is the release's shape. `media` can therefore arrive
    reading CD on one disc and Digital Media on another, and a single "X → Y"
    line would be asserting one reading and one change where there are two of
    each. Such a field is still named — #112's distinction between a tag that
    agreed and one nobody looked at is what the band exists for — but it states
    its value only, exactly as it did before #360.

    The MusicBrainz half of that is #374's: it was assumed rather than asked, and
    `mb` was taken from the first track regardless. On a release whose one ISRC
    sits on track 3, track 1's silence became the whole album's MusicBrainz
    reading, and the band drew an addition as a removal.
    """
    key = candidate.owned.value
    disk = [_disk_value(t, key) for t, _ in present]
    comparable = [
        (d, _as_display(getattr(m.tags, key)))
        for d, (_, m) in zip(disk, present, strict=True)
        if m is not None
    ]
    uniform = _one_reading_on_disk(candidate, present) and _one_reading_in_mb(candidate, present)
    differs = uniform and any(d != v for d, v in comparable)
    # On a DIFFERENCE `value` is what the files carry, even when that is nothing:
    # it is the "before" of a change, and the row is about to state MusicBrainz's
    # reading beside it. Falling back to MusicBrainz's here — which is right for
    # agreement, where either side names the one value — printed "Digital Media →
    # Digital Media" for a tag no file carried at all, turning an addition into a
    # change from itself.
    values = [*disk, *(v for _, v in comparable)]
    return CollapsedField(
        candidate.label,
        disk[0] if differs and disk else next((v for v in values if v is not None), None),
        candidate.entity,
        candidate.credit,
        field=key,
        mb=comparable[0][1] if comparable and uniform else None,
        differs=differs,
    )


def _per_disc_constant(present: Sequence[tuple[int, TrackTags]]) -> bool:
    """Whether the medium-derived tags may be rolled up into the headings (#320).

    True when every disc's own present tracks agree about all three of them. Their
    readings are then per-disc constants — a column of them answers *which disc*,
    and the disc heading is where that question is already answered.

    When one disc's tracks disagree with each other, they are not constants: the
    column answers *which track*, which is a column earning its place under rule
    1, and a heading built from a majority would quietly bury the outlier. So the
    roll-up is off and all three go back to competing for columns exactly as they
    did before — which is why this is checked over the whole album before a single
    heading is built, rather than per disc.

    All three together, for the reason `heading_fields` gives: the heading is one
    line describing one disc's shape, and it states all of it or none of it.
    """
    by_disc: dict[int, list[TrackTags]] = {}
    for disc, tags in present:
        by_disc.setdefault(disc, []).append(tags)
    return all(
        len({_disk_value(t, f.value) for t in tagsets}) == 1
        for tagsets in by_disc.values()
        for f in _MEDIUM_DERIVED
    )


def _disc_headings(
    present: Sequence[tuple[int, TrackTags]],
    media: Sequence[Medium],
    mb: Sequence[MBTrack],
) -> tuple[DiscHeading, ...]:
    """One heading per disc that has files, comparing the three medium tags.

    Only for discs with present, readable, non-video tracks. A disc nobody ripped
    has no files to compare (the difference would be against nothing); one whose
    files Harmonist could not read has tags it never managed to look at (#112);
    and one holding nothing but videos has tags Harmonist will never rewrite
    (#226). All three keep the plain heading built from `media`, which describes
    the disc without claiming anything about the files.

    MusicBrainz's track total for a medium is how many tracks it HAS, which is
    what `tagger` writes into the tag and what `DiscGroup.summary` has always
    printed. Counted from `mb` rather than taken from a medium's own field, so
    the number under the heading and the rows beneath it cannot disagree.
    """
    known = {m.position: m for m in media}
    totals = Counter(t.tags.disc_num or 1 for t in mb)
    by_disc: dict[int, list[TrackTags]] = {}
    for disc, tags in present:
        by_disc.setdefault(disc, []).append(tags)

    out: list[DiscHeading] = []
    for disc, tagsets in sorted(by_disc.items()):
        medium = known.get(disc)
        # Unanimous by construction — `_per_disc_constant` is the gate — so the
        # first track's reading IS the disc's reading.
        one = tagsets[0]
        out.append(
            DiscHeading(
                position=disc,
                subtitle=compare_value(
                    LABELS[Owned.DISC_SUBTITLE],
                    disk=_disk_value(one, Owned.DISC_SUBTITLE.value),
                    mb=medium.title if medium else None,
                ),
                media=compare_value(
                    LABELS[Owned.MEDIA],
                    kind=Kind.SCALAR,
                    disk=_disk_value(one, Owned.MEDIA.value),
                    mb=medium.format if medium else None,
                ),
                track_total=compare_value(
                    LABELS[Owned.TRACK_TOTAL],
                    kind=Kind.SCALAR,
                    disk=_disk_value(one, Owned.TRACK_TOTAL.value),
                    mb=str(totals[disc]) if totals[disc] else None,
                ),
            )
        )
    return tuple(out)


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
    # The disc comes from MusicBrainz, like the rows' own does, and NOT from the
    # file: a file that says disc 1 while its release track id places it on disc
    # 2 is exactly the case #232 exists for, and grouping it by what it claims
    # would file its tags under the wrong heading.
    #
    # Videos take no part (#226). Harmonist reads their tags and never writes
    # them, so a bonus DVD whose files carry no `discsubtitle` would get a purple
    # heading stating a change no re-tag will ever make — the finding-you-cannot-
    # act-on that #226 removed from the rows, reappearing one level up. A disc
    # with nothing but videos on it therefore has no comparison at all, and its
    # heading stays MusicBrainz's description of the disc.
    on_disc: list[tuple[int, TrackTags]] = [
        (mb[i].tags.disc_num or 1, tags)
        for i, (_, tags) in sorted(assigned.items())
        if not tags.unreadable and not tags.video
    ]
    present: list[_Present] = [
        (tags, None if tags.video else mb[i])
        for i, (_, tags) in sorted(assigned.items())
        if not tags.unreadable
    ]
    # The medium-derived tags go to the disc headings where they can (#320),
    # which is what keeps them out of the columns, the collapsed set and the box.
    #
    # The columns are told to skip them BY the headings existing, not by the same
    # condition evaluated twice: an album of nothing but video discs satisfies
    # every part of the test and still builds no heading, and two readings of one
    # rule would then drop three tags off the page entirely — no column, no
    # heading, nothing. Asking `bool(headings)` makes the two agree by
    # construction rather than by staying in step.
    headings = (
        _disc_headings(on_disc, media, mb) if multi_disc and _per_disc_constant(on_disc) else ()
    )
    kept, collapsed, absorbed = _choose_columns(present, multi_disc, bool(headings))

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
        headings=headings,
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
    never asked. The rows are PRESENT with no counterpart, and the panel's
    `mb_available` is what makes `headline` say so once, at the top.
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
        #
        # `comparable` is per-ROW here, not per-field, and that is load-bearing
        # since #340 made a comparable ONLY_DISK field a finding. A video track
        # (#226) and every row of the disk-only view (#228) have `mb=None` for
        # ALL their fields, so without this each of them would light up as a row
        # full of pending removals — when the whole claim of a video row is
        # "present, and that is all". MusicBrainz has no counterpart for the row,
        # which is precisely what `comparable` means.
        fields.append(replace(row, entity=c.entity, credit=c.credit))
    fields.append(
        _length_field(
            tags.duration_ms if tags else None,
            mb.length_ms if mb else None,
            unreadable=unreadable,
        )
    )
    # `comparable` is per-ROW here, not per-field, and it is load-bearing since
    # #340 made a comparable ONLY_DISK field a finding. A video track (#226) and
    # every row of the disk-only view (#228) have no MusicBrainz counterpart at
    # all, so all their fields land in ONLY_DISK — and without this each row
    # would light up as a set of pending removals, when the whole claim of a
    # video row is "present, and that is all".
    #
    # Applied to the WHOLE row rather than inside the loop above: `#` and Length
    # are built outside it, and marking only the tag columns left those two
    # still reading as findings on exactly the rows this protects.
    if mb is None:
        return tuple(replace(f, comparable=False) for f in fields)
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
    # `comparable=False`, always, and it is the one field that needs saying
    # explicitly (#340). A length is NOT a tag — nothing writes one to a file —
    # so a length MusicBrainz does not carry can never be a pending removal, and
    # the rule that makes a comparable ONLY_DISK field a finding would otherwise
    # report every digital release whose lengths MusicBrainz lacks. What
    # `comparable` is really standing in for throughout is "a field Harmonist
    # writes from MusicBrainz", which everywhere else it names exactly.
    #
    # A length that genuinely DIFFERS is untouched: that lands in DIFFERS, which
    # does not consult this flag.
    return replace(
        compare_value(
            "Length", kind=Kind.SCALAR, disk=_mmss(disk_ms), mb=_mmss(mb_ms), unreadable=unreadable
        ),
        comparable=False,
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
