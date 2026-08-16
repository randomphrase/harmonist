"""Turning stored tag-change records into something readable (#86).

One tagging writes one record per *file* — that granularity is what makes the
record exactly reversible, since an album tagged unevenly over the years has
different "before" values on different tracks. But it is not how anyone reads
history. Nobody asks "what happened to track 9?"; they ask "what did it
change?", and an eighteen-track album that answers the first question makes the
second one unanswerable by burying it in eighteen near-identical rows.

So this module inverts the records: **one row per field**, with how far it
reached stated beside it. The height of the result is bounded by the number of
fields that changed, not by the size of the album — which matters because the
gardener (#32) will eventually write one of these per album per night.

Pure functions over values, like `compare`: no I/O, no store, no templates. It
borrows `compare.diff_runs` for in-value emphasis rather than growing a second
implementation, because a change marked one way on the album panel and another
way in history would read as two different kinds of fact.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .compare import Run, diff_runs
from .formats.owned import ARTWORK, SCOPE, Owned, Scope

#: Human labels for every owned field, plus artwork. Unknown keys fall back to
#: the key itself — deliberately, and load-bearing: these records are permanent
#: and unversioned, so a payload written today may name a field this build has
#: since renamed or dropped. Rendering must degrade to "show the raw name"
#: rather than raise, or one old row takes a whole album page down with it.
_LABELS: dict[str, str] = {
    Owned.MB_ALBUM_ID: "MusicBrainz release",
    Owned.ALBUM: "Album",
    Owned.ALBUM_ARTIST: "Album artist",
    Owned.ALBUM_ARTIST_SORT: "Album artist sort",
    Owned.MB_ALBUM_ARTIST_IDS: "Album artist IDs",
    Owned.MB_RELEASE_GROUP_ID: "Release group",
    Owned.MB_ALBUM_TYPE: "Release type",
    Owned.MB_ALBUM_STATUS: "Release status",
    Owned.MB_ALBUM_COUNTRY: "Country",
    Owned.DATE: "Date",
    Owned.ORIGINAL_DATE: "Original date",
    Owned.SCRIPT: "Script",
    Owned.LABEL: "Label",
    Owned.CATALOG_NUMBER: "Cat. no.",
    Owned.BARCODE: "Barcode",
    Owned.ASIN: "ASIN",
    Owned.DISC_TOTAL: "Disc total",
    Owned.TITLE: "Title",
    Owned.ARTIST: "Artist",
    Owned.ARTIST_SORT: "Artist sort",
    Owned.ARTISTS: "Artists",
    Owned.TRACK_NUM: "Track no.",
    Owned.TRACK_TOTAL: "Track total",
    Owned.DISC_NUM: "Disc no.",
    Owned.MEDIA: "Media",
    Owned.MB_TRACK_ID: "Recording",
    Owned.MB_RELEASE_TRACK_ID: "Release track",
    Owned.MB_ARTIST_IDS: "Artist IDs",
    Owned.ISRCS: "ISRC",
    ARTWORK: "Artwork",
}

#: Display order: the owned fields as `Owned` declares them (album-level first,
#: then per-track), with artwork last. Artwork is not an owned field and has no
#: scope, so it can't be sorted among them — and it belongs at the end anyway,
#: being the one row whose value isn't a tag.
_ORDER: dict[str, int] = {f.value: i for i, f in enumerate(Owned)} | {ARTWORK: len(Owned)}


@dataclass(frozen=True)
class TrackChange:
    """What one file's copy of a field did, for the per-track expansion."""

    file: str
    position: str | None
    before: str | None
    after: str | None


@dataclass(frozen=True)
class FieldChange:
    """One row of the field-first view: what changed, and how far it reached."""

    field: str
    label: str
    before: str | None
    after: str | None
    #: How many of the tagging's files carried this change, and how many files
    #: it touched in total. `tracks == total` is the ordinary case.
    tracks: int
    total: int
    #: True when the values are digests rather than anything a person reads —
    #: artwork. The row states what happened ("replaced") instead of reciting
    #: 128 hex characters that would dominate every row around it. The digests
    #: are still in the record, where #131 needs them.
    opaque: bool = False
    #: None for artwork, which is deliberately not an owned field and so has no
    #: album/track scope of its own.
    scope: Scope | None = None
    before_runs: tuple[Run, ...] = ()
    after_runs: tuple[Run, ...] = ()
    #: Every file's version of this change, in the order recorded. Populated
    #: only when they DIFFER — when the whole album moved the same way, the
    #: representative pair above already says everything.
    variants: tuple[TrackChange, ...] = ()

    @property
    def uniform(self) -> bool:
        """Whether every file that changed changed the same way."""
        return not self.variants

    @property
    def summary(self) -> str:
        """What to show in place of the values, for a row whose values are not
        readable. "Replaced" and "added" are different facts to a user deciding
        whether they mind."""
        if self.before is None:
            return "added"
        if self.after is None:
            return "removed"
        return "replaced"

    @property
    def reach(self) -> str:
        """The annotation beside the value: how much of the album this touched.

        An album-scoped field that moved on every track is just "album" — it is
        one logical change, and saying "all 18 tracks" would invite the reader
        to check eighteen things. A field that moved on only some tracks always
        gets the count, whatever its scope, because that is the case where the
        album has no single answer.
        """
        if self.tracks < self.total:
            return f"{self.tracks} of {self.total} tracks"
        if self.scope is Scope.ALBUM:
            return "album"
        return "all tracks" if self.total > 1 else "1 track"


def group_by_action(
    events: Sequence[Any],
    detail: Mapping[int, Any],
) -> dict[int, tuple[FieldChange, ...]]:
    """Attach each tagging's field-first summary to the history row that should
    carry it, as `{event_id: rows}`.

    One tagging writes one `tag.track` row per file, all sharing an `action_id`
    with the activity entry above them (#84). The summary hangs off **that
    activity entry** — "Re-tagged" — rather than off any one file's audit line.

    Two reasons, both learned from looking at the rendered page. History is
    newest-first, so anchoring to the earliest audit row put the summary at the
    BOTTOM of the group, reading as though it described only the last file
    listed. And the activity entry is the user-facing record of the action,
    so the summary survives the "Show details" toggle that hides raw audit text
    — which is right, because a plain-language list of what changed is exactly
    the kind of thing that toggle is meant to leave visible.

    Falls back to the earliest audit row when an action has no activity entry
    (a background pass with no user-facing outcome).

    Rows written before #84 landed carry no `action_id` at all. They degrade to
    per-event summaries — each file's changes against its own audit line —
    which is honest about what was recorded at the time and needs no back-fill
    of data nobody captured.
    """
    if not detail:
        return {}

    with_detail: dict[str, list[Any]] = {}
    anchors: dict[str, Any] = {}
    out: dict[int, tuple[FieldChange, ...]] = {}
    for event in events:
        action_id = event.action_id
        if action_id is None:
            if event.id in detail:
                rows = summarise([detail[event.id]])
                if rows:
                    out[event.id] = rows
            continue
        if event.source == "activity":
            # The action's user-facing outcome. First one wins; there is only
            # ever one per action, but a defensive `setdefault` beats assuming.
            anchors.setdefault(action_id, event)
        elif event.id in detail:
            with_detail.setdefault(action_id, []).append(event)

    for action_id, grouped in with_detail.items():
        # In file order — the order they were recorded — so the per-track
        # expansion reads down the album rather than however the page sorted.
        ordered = sorted(grouped, key=lambda e: e.id)
        rows = summarise([detail[e.id] for e in ordered])
        if rows:
            anchor = anchors.get(action_id) or ordered[0]
            out[anchor.id] = rows
    return out


def display(value: Any) -> str | None:
    """One stored value as the page shows it, or None when it was absent.

    `None`, `""` and `[]` all collapse to absent, matching `owned._absent` —
    the record stores raw values so a revert can restore them exactly, but the
    reader must not draw a distinction the tagger doesn't make.
    """
    if value is None or value == "" or value == []:
        return None
    if isinstance(value, (list, tuple)):
        return "; ".join(str(v) for v in value)
    return str(value)


def _pair(entry: Any) -> tuple[Any, Any] | None:
    """A stored `[before, after]`, or None if it isn't one.

    Defensive on purpose: the payload is permanent, unversioned JSON, so a row
    written by a future build may hold a shape this one doesn't expect. Skipping
    it loses one row; raising loses the page.
    """
    if isinstance(entry, (list, tuple)) and len(entry) == 2:
        return entry[0], entry[1]
    return None


def summarise(records: Sequence[Any]) -> tuple[FieldChange, ...]:
    """Invert per-file records into field-first rows.

    `records` is one tagging's `activity_store.TagChanges`, in the order they
    were written (which is file order). The result is ordered by `_ORDER`, so
    album-level fields come before per-track ones and artwork sits last.
    """
    total = len(records)
    if not total:
        return ()

    # field -> [(file, position, before, after)], in file order.
    by_field: dict[str, list[tuple[str, str | None, Any, Any]]] = {}
    for record in records:
        for field, entry in record.changes.items():
            pair = _pair(entry)
            if pair is None:
                continue
            by_field.setdefault(field, []).append((record.file, record.position, *pair))

    rows = [_row(field, entries, total) for field, entries in by_field.items()]
    return tuple(sorted(rows, key=lambda r: (_ORDER.get(r.field, len(_ORDER)), r.label)))


def _row(field: str, entries: list[tuple[str, str | None, Any, Any]], total: int) -> FieldChange:
    """One field's row: the representative change, and the outliers if any."""
    pairs = [(display(b), display(a)) for _, _, b, a in entries]
    before, after = _representative(pairs)

    variants: tuple[TrackChange, ...] = ()
    if len(set(pairs)) > 1:
        # Only when they actually differ — a compilation where every track's
        # artist changed to something different has no single answer, and
        # showing one track's as if it were the album's would be the quiet lie
        # the count pill exists to prevent.
        variants = tuple(
            TrackChange(file=f, position=p, before=display(b), after=display(a))
            for f, p, b, a in entries
        )

    opaque = field == ARTWORK
    before_runs: tuple[Run, ...] = ()
    after_runs: tuple[Run, ...] = ()
    if before is not None and after is not None and not opaque:
        # Two sha256 digests differ in nearly every character, so emphasis on
        # them would be noise on top of noise.
        before_runs, after_runs = diff_runs(before, after)

    return FieldChange(
        field=field,
        label=_LABELS.get(field, field),
        before=before,
        after=after,
        opaque=opaque,
        tracks=len(entries),
        total=total,
        scope=SCOPE.get(Owned(field)) if field in _ORDER and field != ARTWORK else None,
        before_runs=before_runs,
        after_runs=after_runs,
        variants=variants,
    )


def _representative(pairs: list[tuple[str | None, str | None]]) -> tuple[str | None, str | None]:
    """The change to show when the files don't all agree.

    The most common `(before, after)` pair; on a tie, **the first file's**.
    Deliberately the same rule `compare.consensus` uses for the album panel,
    stated the same way — "when your tracks disagree evenly, Harmonist shows
    what the first one says" — because a reader who has learned it in one place
    should not find a different rule in the other.

    The pair is kept together rather than resolved field-by-field: taking the
    most common `before` and the most common `after` independently could
    manufacture a transition that no track actually made.
    """
    counts = Counter(pairs)
    top = max(counts.values())
    tied = {p for p, c in counts.items() if c == top}
    return next(p for p in pairs if p in tied)
