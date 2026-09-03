"""The tags Harmonist owns — the single definition every format shares (#149).

Harmonist promises not to touch tags it doesn't understand. This module is the
concrete form of that promise: **these fields, and only these, are the ones
Harmonist writes, overwrites, and removes.** Everything else on a file — the
comment carrying a recovered Bandcamp URL, a genre tagged elsewhere (#12), any
arbitrary tag the user or another tool put there — is left exactly as found.

Each format module maps `Owned` to its own native keys and clears that mapping
before writing. Owning the definition here rather than per-backend is what makes
the three agree; when it lived only in `_vorbis.py` they didn't, and two
bugs followed:

- a field absent from the new `TagSet` was removed on FLAC and silently left
  stale on MP3 and M4A, so the label of a *wrong* release survived the mis-tag
  correction that was supposed to clear it;
- MP3 wrote `media` to `TMED` and read it back from `TXXX:MEDIA`, so it never
  round-tripped and the album page reported it missing on every MP3 it had
  itself tagged.

**Artwork is deliberately absent from this set.** Embedded cover art is not a
tag here: `tagger.tag_album` passes `cover=None` when the tracks carry differing
per-track images, precisely so `write_tags` leaves them alone (a compilation's
per-track art is user data a re-tag must not destroy). Adding artwork to the
owned set would clear it before every write and make that protection a no-op.
Per-track artwork is a third category that fits neither scope below — MusicBrainz
and Picard don't really model it either — and it is handled in the tagger, not
here. See #131.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Any


class Scope(StrEnum):
    """Whether a field's value depends on which track of the album it is.

    The split is load-bearing for the tagging audit records (#86), which record
    an album-level change once per album rather than once per track — the
    difference between one line and twenty identical ones in an album's history.

    The test is deliberately "can this vary between tracks", not "does it
    usually". `media`, `disc_num` and `track_total` are derived from the medium,
    so on a multi-disc release — or a CD+DVD set — they genuinely differ per
    track, and calling them album-level would make the audit record wrong on
    exactly the releases where it matters most. A field that *happens* to be
    constant across an album is still recognised as constant at render time;
    a field wrongly declared constant here cannot be recovered from.
    """

    ALBUM = "album"
    TRACK = "track"


class Owned(StrEnum):
    """One tag Harmonist owns.

    The values are exactly the `TagSet` attribute names — not a coincidence and
    not decoration. It lets the format mappings be checked mechanically against
    `TagSet` (see `test_formats.py`), and it gives #86 a stable vocabulary for
    the persisted before/after records without inventing a second set of names
    that would have to be kept in step with this one.
    """

    # --- Album-level ---
    MB_ALBUM_ID = "mb_album_id"
    ALBUM = "album"
    ALBUM_ARTIST = "album_artist"
    ALBUM_ARTIST_SORT = "album_artist_sort"
    ALBUM_ARTISTS = "album_artists"
    MB_ALBUM_ARTIST_IDS = "mb_album_artist_ids"
    MB_RELEASE_GROUP_ID = "mb_release_group_id"
    MB_ALBUM_TYPE = "mb_album_type"
    MB_ALBUM_STATUS = "mb_album_status"
    MB_ALBUM_COUNTRY = "mb_album_country"
    COMPILATION = "compilation"
    DATE = "date"
    ORIGINAL_DATE = "original_date"
    SCRIPT = "script"
    LABEL = "label"
    CATALOG_NUMBER = "catalog_number"
    BARCODE = "barcode"
    ASIN = "asin"
    DISC_TOTAL = "disc_total"

    # --- Per-track ---
    TITLE = "title"
    ARTIST = "artist"
    ARTIST_SORT = "artist_sort"
    ARTISTS = "artists"
    TRACK_NUM = "track_num"
    TRACK_TOTAL = "track_total"
    DISC_NUM = "disc_num"
    DISC_SUBTITLE = "disc_subtitle"
    MEDIA = "media"
    MB_TRACK_ID = "mb_track_id"
    MB_RELEASE_TRACK_ID = "mb_release_track_id"
    MB_ARTIST_IDS = "mb_artist_ids"
    ISRCS = "isrcs"


SCOPE: dict[Owned, Scope] = {
    Owned.MB_ALBUM_ID: Scope.ALBUM,
    Owned.ALBUM: Scope.ALBUM,
    Owned.ALBUM_ARTIST: Scope.ALBUM,
    Owned.ALBUM_ARTIST_SORT: Scope.ALBUM,
    # Derived from the RELEASE credit, so unlike `artists` it cannot vary
    # between tracks — a compilation's tracks differ in `artists` while every
    # one of them names the same album artists.
    Owned.ALBUM_ARTISTS: Scope.ALBUM,
    Owned.MB_ALBUM_ARTIST_IDS: Scope.ALBUM,
    Owned.MB_RELEASE_GROUP_ID: Scope.ALBUM,
    Owned.MB_ALBUM_TYPE: Scope.ALBUM,
    Owned.MB_ALBUM_STATUS: Scope.ALBUM,
    Owned.MB_ALBUM_COUNTRY: Scope.ALBUM,
    # A property of the RELEASE artist — whether it is MusicBrainz's Various
    # Artists — so it cannot vary between tracks, however varied their credits.
    Owned.COMPILATION: Scope.ALBUM,
    Owned.DATE: Scope.ALBUM,
    Owned.ORIGINAL_DATE: Scope.ALBUM,
    Owned.SCRIPT: Scope.ALBUM,
    Owned.LABEL: Scope.ALBUM,
    Owned.CATALOG_NUMBER: Scope.ALBUM,
    Owned.BARCODE: Scope.ALBUM,
    Owned.ASIN: Scope.ALBUM,
    Owned.DISC_TOTAL: Scope.ALBUM,
    Owned.TITLE: Scope.TRACK,
    Owned.ARTIST: Scope.TRACK,
    Owned.ARTIST_SORT: Scope.TRACK,
    Owned.ARTISTS: Scope.TRACK,
    Owned.TRACK_NUM: Scope.TRACK,
    Owned.TRACK_TOTAL: Scope.TRACK,
    Owned.DISC_NUM: Scope.TRACK,
    # Track-scoped for the same reason as `disc_num` and `media`: it describes
    # the MEDIUM, which differs between tracks on a multi-disc release even
    # though it reads like an album-level fact.
    Owned.DISC_SUBTITLE: Scope.TRACK,
    Owned.MEDIA: Scope.TRACK,
    Owned.MB_TRACK_ID: Scope.TRACK,
    Owned.MB_RELEASE_TRACK_ID: Scope.TRACK,
    Owned.MB_ARTIST_IDS: Scope.TRACK,
    Owned.ISRCS: Scope.TRACK,
}

ALBUM_FIELDS: tuple[Owned, ...] = tuple(f for f in Owned if SCOPE[f] is Scope.ALBUM)
TRACK_FIELDS: tuple[Owned, ...] = tuple(f for f in Owned if SCOPE[f] is Scope.TRACK)


#: Key under which a tagging records an artwork change, alongside the owned
#: fields but deliberately NOT one of them — see the module docstring. Named
#: here so the writer and the renderer can't drift on the spelling, and kept
#: distinct from every `Owned` value so a reader iterating `Owned` skips it
#: rather than trying to render a sha256 as a tag.
ARTWORK = "artwork"


class Significance(StrEnum):
    """What KIND of change this is — deliberately *not* whether it needs review.

    Those are two different questions and an earlier draft of this enum answered
    them with one word, which was wrong: a change can be slight and still want a
    person's eye, and a change can be far-reaching and still be one a particular
    user is happy to have applied for them. Significance is a property of the
    change; review is a policy over significance, and it belongs in `AUTO_APPLY`
    below so #273 has somewhere to attach a per-level trust setting.

    The levels are ordered by how much of the album they call into question, and
    that ordering is the only thing a trust setting needs to be intelligible:
    someone who trusts `STRUCTURE` almost certainly trusts `ENRICHMENT` too.
    """

    #: Whitespace or casing only — the same value, spelled differently. Never
    #: declared in `SIGNIFICANCE`; only ever reached at runtime by a `BY_VALUE`
    #: field whose two values turn out to differ this little.
    COSMETIC = "cosmetic"
    #: MusicBrainz filling in or correcting a detail — a catalogue number it
    #: didn't have, a date narrowed from a year to a day. Nothing about what the
    #: album is, or how it is laid out, moves.
    ENRICHMENT = "enrichment"
    #: How the album is laid out: track and disc numbers and totals. The album is
    #: still the same album; which track is which has changed.
    STRUCTURE = "structure"
    #: What the album or one of its tracks IS — its name, its artist, or any of
    #: the MusicBrainz ids that say which entity it points at.
    IDENTITY = "identity"
    #: The cover image. Its own level rather than a rank among the others,
    #: because "let it update my cover art" is a trust decision people make
    #: separately from anything about tags.
    COVER_ART = "artwork"


#: What kind of change each key of a tagging diff represents, keyed exactly as
#: `diff` keys its result — the `Owned` values plus `ARTWORK`. Keyed by string
#: for that reason: a classifier iterates a plan's changes, and artwork arrives
#: in them under a key that is deliberately not an owned field. One lookup table
#: for the whole diff means no caller has to remember which key is the exception.
#:
#: Placed here, beside `SCOPE`, so the totality test can hold: a field added to
#: `Owned` later **cannot** slip through unclassified, because
#: `test_every_owned_field_has_a_significance` fails until someone places it.
#: That is the same discipline that keeps the three format backends in step, and
#: it matters more here — the cost of forgetting `SCOPE` is a mis-rendered
#: history row, the cost of forgetting this one is a change whose significance
#: nothing can state, in the table a trust setting will be read through.
SIGNIFICANCE: dict[str, Significance] = {
    # --- Enrichment: MusicBrainz filling in or correcting a detail ---
    Owned.ALBUM_ARTIST_SORT: Significance.ENRICHMENT,
    Owned.ARTIST_SORT: Significance.ENRICHMENT,
    Owned.MB_ALBUM_STATUS: Significance.ENRICHMENT,
    Owned.MB_ALBUM_COUNTRY: Significance.ENRICHMENT,
    Owned.DATE: Significance.ENRICHMENT,
    Owned.ORIGINAL_DATE: Significance.ENRICHMENT,
    Owned.SCRIPT: Significance.ENRICHMENT,
    Owned.LABEL: Significance.ENRICHMENT,
    Owned.CATALOG_NUMBER: Significance.ENRICHMENT,
    Owned.BARCODE: Significance.ENRICHMENT,
    Owned.ASIN: Significance.ENRICHMENT,
    Owned.DISC_SUBTITLE: Significance.ENRICHMENT,
    Owned.MEDIA: Significance.ENRICHMENT,
    Owned.ISRCS: Significance.ENRICHMENT,
    # --- Identity: what the album, or one of its tracks, IS ---
    Owned.ALBUM: Significance.IDENTITY,
    Owned.ALBUM_ARTIST: Significance.IDENTITY,
    Owned.ALBUM_ARTISTS: Significance.IDENTITY,
    Owned.ARTIST: Significance.IDENTITY,
    Owned.ARTISTS: Significance.IDENTITY,
    # Identity even though it reads like a descriptor: MusicBrainz re-typing a
    # release group from Album to EP is it saying this is a different sort of
    # thing from what Harmonist recorded. The debatable one of this group — it
    # would not be absurd as ENRICHMENT, and watching it in the Inbox is how
    # that gets decided rather than by argument here.
    Owned.MB_ALBUM_TYPE: Significance.IDENTITY,
    # Every MusicBrainz id, including the ones that can only have moved because
    # MusicBrainz merged the entity behind them. The cheaper reading — "a merge
    # already happened, so there is nothing to authorise", which is what #268
    # settled for the RELEASE id — was considered and not taken: that decision
    # rests on the merge being *provable*, and it is, exactly once, at the fetch,
    # where a redirect names both ids. An id that simply arrives different inside
    # an unchanged release payload carries no such evidence. It is a merge, a
    # re-point, or a MusicBrainz edit that replaced the track, and nothing here
    # can tell those apart.
    # A restatement of who the release artist is: the flag is set exactly when
    # that artist is MusicBrainz's Various Artists, so it moves only when
    # `album_artist` does, and it is Identity for the same reason. Its
    # consequence argues the same way — a player reads it to decide whether the
    # album is one album at all.
    Owned.COMPILATION: Significance.IDENTITY,
    Owned.MB_ALBUM_ID: Significance.IDENTITY,
    Owned.MB_RELEASE_GROUP_ID: Significance.IDENTITY,
    Owned.MB_ALBUM_ARTIST_IDS: Significance.IDENTITY,
    Owned.MB_ARTIST_IDS: Significance.IDENTITY,
    Owned.MB_TRACK_ID: Significance.IDENTITY,
    Owned.MB_RELEASE_TRACK_ID: Significance.IDENTITY,
    # Identity by default, and the one field that can be less than that — see
    # BY_VALUE below.
    Owned.TITLE: Significance.IDENTITY,
    # --- Structure: the same album, laid out differently ---
    Owned.TRACK_NUM: Significance.STRUCTURE,
    Owned.TRACK_TOTAL: Significance.STRUCTURE,
    Owned.DISC_NUM: Significance.STRUCTURE,
    Owned.DISC_TOTAL: Significance.STRUCTURE,
    # --- Artwork ---
    ARTWORK: Significance.COVER_ART,
}

#: The tag levels, least far-reaching first — the ordering `Significance`'s
#: docstring claims, written down so it can be used and checked rather than
#: merely asserted in prose.
#:
#: What needs it: a diff usually contains changes of several kinds at once, and a
#: finding (#271) records ONE verdict for the album. The verdict is the furthest-
#: reaching change in the diff, because that is what decides how much of the
#: album is in question — an ISRC arriving beside a retitle does not make the
#: retitle an enrichment.
#:
#: COVER_ART IS ABSENT, and its absence is the enum's own position: cover art is
#: "its own level rather than a rank among the others", so it has no place in a
#: line the others sit on. `ranked` refuses it rather than guessing where it
#: would go. Nothing is lost today — the gardener plans with `cover_path=None`,
#: so artwork cannot appear in a diff it classifies (#269 owns art, on its own
#: cadence) — and when something does classify artwork it will need a verdict of
#: its own, not a rank pretending to compare with a retitle.
ORDER: tuple[Significance, ...] = (
    Significance.COSMETIC,
    Significance.ENRICHMENT,
    Significance.STRUCTURE,
    Significance.IDENTITY,
)


def ranked(significance: Significance) -> int:
    """How far `significance` reaches, as a sortable number.

    Raises `ValueError` for COVER_ART — see `ORDER`. A caller that can produce
    one has to say what it means before it can be compared, and the failure to
    do that must not be silently resolved to "least significant", which is where
    a `.get(..., 0)` would put it.
    """
    return ORDER.index(significance)


#: Levels whose changes may be applied without asking anyone.
#:
#: **Empty, deliberately.** Every change goes to review, whatever its
#: significance, because nothing has yet watched this classification run against
#: a real library — and the way to find out whether `mb_album_type` really
#: belongs under IDENTITY is to see it arrive in the Inbox, not to argue about
#: it here. Starting closed also means the first version of #32's runner cannot
#: write anything unattended, whatever else is wrong with it.
#:
#: This is the seam #273 turns into a setting: a user who trusts ENRICHMENT gets
#: that level in this set, and everything else keeps going to review. Widening
#: it is a decision about somebody's files, so it does not happen by a default
#: drifting — `test_no_level_applies_itself_yet` fails if this set gains a
#: member without that being the point of the change.
AUTO_APPLY: frozenset[Significance] = frozenset()


def needs_review(significance: Significance) -> bool:
    """Whether a change of this kind has to be shown to a person first.

    The policy over the classification, kept apart from it so the two can move
    independently: significance describes the change and is a property of
    MusicBrainz's data, while this describes how much a particular user trusts
    Harmonist and is a property of their configuration (#273).
    """
    return significance not in AUTO_APPLY


#: Fields whose significance cannot be given at field level, because it depends
#: on how far the value actually moved. Only `title` so far: MusicBrainz tidying
#: the spacing or casing of a track title is COSMETIC, and renaming the track is
#: IDENTITY, and one entry in the table cannot say both.
#:
#: These are declared at their HIGHER significance and lowered when the values
#: turn out to differ trivially — never raised. That direction is deliberate: a
#: rule that fails to fire overstates a change, which costs a glance, while one
#: that fires wrongly understates a retitle as a spacing fix, and under a trust
#: setting that is a write nobody agreed to.
#:
#: The comparison itself lives with the caller (`tagger.significance_of`), which
#: is where `models.norm_title` — the definition of "cosmetic" the album page
#: already uses — can be imported without this module reaching upwards for it.
BY_VALUE: frozenset[Owned] = frozenset({Owned.TITLE})


#: Multi-value credit lists whose ABSENCE is not a defect while they would hold
#: a single name — the scalar twin beside them already says it (#337).
#:
#: Mapped to that twin rather than listed on their own, so the entry states WHY
#: it is exempt and can be checked rather than taken on trust. Same idiom as
#: `compare._SUBSUMED_BY` and `_ALBUM_COUNTERPART`.
#:
#: The problem this answers: `albumartists` is new in Picard as well as here —
#: `PICARD-700`, 2026-08-25, in `3.0.0rc1` only — so NO existing library carries
#: it, and one IDENTITY-classified field put every album in a real library into
#: the Inbox on its first pass. That is the failure `SIGNIFICANCE` above already
#: warns about (#283, #290), met for the first time.
#:
#: Load-bearing on a real collaboration and redundant otherwise, which is what
#: makes the exemption conditional rather than a blanket opt-out: the joined
#: phrase cannot be split safely — "Nick Cave & the Bad Seeds" is ONE artist
#: containing an ampersand — so with two names the list is the only way a player
#: can recover them, and with one the phrase already is the name.
DUPLICATES_WHEN_SINGLE: dict[Owned, Owned] = {
    Owned.ALBUM_ARTISTS: Owned.ALBUM_ARTIST,
    Owned.ARTISTS: Owned.ARTIST,
}


def is_opportunistic(field: str, before: object, after: object) -> bool:
    """Whether this change is one to make while tagging, but not a reason to tag.

    True only for an ABSENCE that would be filled with a single name. Three
    things are deliberately outside it:

    * A field not in `DUPLICATES_WHEN_SINGLE` — the exemption is per-field and
      declared, never inferred from a value that happens to look redundant.
    * A value that is present and disagrees. That is a defect whatever its
      length, and reading "opportunistic" as "never worth flagging" would let a
      wrong credit sit unreported forever.
    * A value that would hold more than one name, where the list is the only
      record of where one artist ends and the next begins.

    Note what this does NOT do: it never stops the change being WRITTEN, or
    recorded. A re-tag that happens for any other reason fills the tag in and
    says so in the history. See `gardener.refresh_flag` for why the distinction
    has to live at the flag rather than in `diff`.
    """
    twin = next((f for f in DUPLICATES_WHEN_SINGLE if f.value == field), None)
    if twin is None or not _absent(before):
        return False
    return isinstance(after, list) and len(after) <= 1


def _absent(value: object) -> bool:
    """Whether a value counts as "this tag isn't there".

    `None`, `""` and `[]` all mean the same thing, because Harmonist never
    writes an empty tag — so a field with no value is removed rather than
    blanked, and reverting one restores its absence. Collapsing them here is
    what stops a first tag of an untagged album recording `None -> ""` on every
    field it didn't actually set.
    """
    return value is None or value == "" or value == []


#: How a set boolean tag is spelled in the text formats — ID3's `TCMP` and the
#: Vorbis `COMPILATION` comment both carry `"1"`, which is what Picard writes and
#: what `as_flag` reads back. MP4's `cpil` is a native boolean atom and needs
#: none of this. Named so the writer and the reader cannot drift on the spelling.
FLAG_TRUE = "1"


def as_flag(raw: object) -> bool | None:
    """A boolean tag as `True`, or None when it isn't set (#323).

    The three backends spell the same flag three ways — `"1"` in a Vorbis
    comment, `"1"` in ID3's `TCMP`, a native `cpil` bool in MP4 — and all three
    have to hand `_read_owned` a value shaped exactly like the `TagSet`
    attribute, or the field reports a phantom change on every re-tag. One
    reading, shared, is what keeps them from each inventing their own.

    **Never `False`.** Harmonist writes the flag only when true, so "not a
    compilation" is the tag's ABSENCE — and `_absent` below already collapses
    `None`, `""` and `[]` into one answer. Returning `False` would add a fourth
    spelling of nothing that `values_differ` treats as a value, so an ordinary
    album would differ from MusicBrainz on this field forever. A file carrying
    an explicit `COMPILATION=0` therefore reads as absent, which is what a write
    would leave it as anyway.
    """
    if isinstance(raw, bool):
        return raw or None
    if isinstance(raw, (list, tuple)):
        raw = raw[0] if raw else None
    return True if str(raw).strip().lower() in {"1", "true", "yes"} else None


def values_differ(a: object, b: object) -> bool:
    """Whether two values of one owned field are meaningfully different.

    The single answer to that question, shared by `diff` and by the undo's
    staleness check (#157). They must not disagree: if the undo decided a file
    had changed on a distinction `diff` didn't record, it would refuse to put
    back a field that the history says it changed.
    """
    if _absent(a) and _absent(b):
        return False
    return a != b


def diff(before: Mapping[str, Any], after: Mapping[str, Any]) -> dict[str, list[Any]]:
    """What changed between two owned-field snapshots, as `{field: [before, after]}`.

    Only fields that actually changed appear. That is what keeps a re-tag with
    no MusicBrainz changes silent instead of writing a record saying nothing
    happened — the difference between a history worth reading and one the
    gardener (#32) floods nightly.

    Values are stored raw, exactly as they were read and written: the
    normalisation above decides *whether* something changed, but the record
    keeps `[]` as `[]` and `None` as `None`, because a revert has to restore
    what was really there rather than a tidied version of it.
    """
    changed: dict[str, list[Any]] = {}
    for field in Owned:
        was, now = before.get(field.value), after.get(field.value)
        if values_differ(was, now):
            changed[field.value] = [was, now]
    return changed


#: The human name of every owned field, plus artwork. ONE name per field, shared
#: by History, the re-tag plan (#291) and the album comparison (#295) — a field
#: called "Original date" in one place and "Original release date" in another
#: reads as two different facts about the same tag.
#:
#: Lives here rather than in a renderer because the label belongs to the field,
#: and because two renderers each keeping their own list is exactly how the
#: comparison came to omit twenty-one fields without anyone noticing.
#:
LABELS: dict[str, str] = {
    Owned.MB_ALBUM_ID: "MusicBrainz release",
    Owned.ALBUM: "Album",
    Owned.ALBUM_ARTIST: "Album artist",
    Owned.ALBUM_ARTIST_SORT: "Album artist sort",
    Owned.ALBUM_ARTISTS: "Album artists",
    Owned.MB_ALBUM_ARTIST_IDS: "Album artist IDs",
    Owned.MB_RELEASE_GROUP_ID: "Release group",
    Owned.MB_ALBUM_TYPE: "Release type",
    Owned.MB_ALBUM_STATUS: "Release status",
    Owned.MB_ALBUM_COUNTRY: "Country",
    Owned.COMPILATION: "Compilation",
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
    Owned.DISC_SUBTITLE: "Disc subtitle",
    Owned.MEDIA: "Media",
    Owned.MB_TRACK_ID: "Recording",
    Owned.MB_RELEASE_TRACK_ID: "Release track",
    Owned.MB_ARTIST_IDS: "Artist IDs",
    Owned.ISRCS: "ISRC",
    ARTWORK: "Artwork",
}
