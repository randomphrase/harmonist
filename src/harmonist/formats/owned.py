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
    MB_ALBUM_ARTIST_IDS = "mb_album_artist_ids"
    MB_RELEASE_GROUP_ID = "mb_release_group_id"
    MB_ALBUM_TYPE = "mb_album_type"
    MB_ALBUM_STATUS = "mb_album_status"
    MB_ALBUM_COUNTRY = "mb_album_country"
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
    Owned.MB_ALBUM_ARTIST_IDS: Scope.ALBUM,
    Owned.MB_RELEASE_GROUP_ID: Scope.ALBUM,
    Owned.MB_ALBUM_TYPE: Scope.ALBUM,
    Owned.MB_ALBUM_STATUS: Scope.ALBUM,
    Owned.MB_ALBUM_COUNTRY: Scope.ALBUM,
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


def _absent(value: object) -> bool:
    """Whether a value counts as "this tag isn't there".

    `None`, `""` and `[]` all mean the same thing, because Harmonist never
    writes an empty tag — so a field with no value is removed rather than
    blanked, and reverting one restores its absence. Collapsing them here is
    what stops a first tag of an untagged album recording `None -> ""` on every
    field it didn't actually set.
    """
    return value is None or value == "" or value == []


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
