"""Optional, user-enabled reshapings of the values a tagging writes (#544).

Picard's own tagging is configurable: options and tagger scripts let a user
decide that *their* library spells an album title a particular way. Harmonist
writes what MusicBrainz says, which is the right default and the wrong answer
for someone who has already made that decision — a re-tag undoes it, and every
album arriving fresh from a sync lands on the other convention.

A **transform** is one named, user-enabled choice about which spelling a write
emits. It is not a scripting language; it is the short list of choices that can
be made *exactly*, and the module is deliberately shaped so each entry could
later become one script (#284).

### The invariant every transform obeys

**A transform may only choose among spellings the comparison already accepts
unconditionally.**

Each field with a transform has an *accepted set* — every spelling THIS release
legitimately has, derived from the release in hand and never from a pattern
(review-gate item 2). The setting picks which member of that set a write emits.
It never narrows the set, and the set never depends on the setting.

Two consequences follow, and they are what make it safe to ship:

- **A change it causes can be told apart from one MusicBrainz caused.** The
  library converges on the setting, existing albums included (#685): an album
  carrying the other spelling has an update. Because the value on disk is a
  member of the accepted set and the new one is the setting's choice, that
  update is classified `Significance.SETTINGS` — the lowest level, never
  announced, never ignorable — rather than the IDENTITY a retitle is. A value
  outside the set keeps its own significance, so nothing MusicBrainz did can
  ride along under the user's setting.
- **It stays idempotent.** A transform is a function of the RELEASE, never of
  what is already on the file, so applying it twice is applying it once. A rule
  that read the existing tag could grow the title a little on every pass.

It used to be the other way round: the set was the tolerance, a file holding
either spelling had nothing to take, and the setting reached only future
taggings. That kept the update check free of configuration, at the price of a
library left permanently split between conventions. The check now reads the
setting (`gardener.configure`), and `can_move` below says which albums a change
to it has to re-judge.

The write half and the accepted half of each transform live in this one module
on purpose. They are two halves of one fact, and #283 has already paid for what
happens when the page's idea of "the same album" and the tagger's are computed
in different places.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from .models import Release, title_with_disambiguation


class TagTransform(StrEnum):
    """A named transform the user may enable. Values are the config spelling.

    One member for now. New members are the output of #284's survey — which
    Picard transforms occur in real libraries AND can be checked exactly from
    the release Harmonist already holds — rather than a list to fill in.
    """

    #: Append the release's disambiguation comment to the album title, so
    #: `Selected Ambient Works Volume II` is written `Selected Ambient Works
    #: Volume II (expanded edition)`. Picard's "use release disambiguation
    #: comment in album title", and the tagger script users write for it.
    ALBUM_DISAMBIGUATION = "album_disambiguation"


class ArtistNames(StrEnum):
    """Picard 3.0's "Standardize artist names" (#678). Values are Picard's own
    config spelling (`picard.options.StandardizeArtistNames`), so a user can
    match the two tools by name."""

    #: Write each artist as credited on the release.
    NONE = "none"
    #: Write the artist's own name, unless the credit is a name they used to go
    #: by — Picard's 3.0 default.
    VARIATIONS = "variations"
    #: Write the artist's own name, always.
    ALL = "all"


@dataclass(frozen=True)
class TaggingChoices:
    """Every user setting that decides which spelling a tagging writes.

    One value rather than a parameter per setting, because each of them has to
    reach the same places — the write, the dry run the update check plans, the
    album page's MusicBrainz column — and a setting threaded through only some
    of them is how the page and the tagger come to disagree about "the same
    album" (#283). Built from the config by `TaggingConfig.choices`.

    Default-constructed, it is MusicBrainz's own spelling of everything: what a
    tagging wrote before any of these settings existed. The CONFIG's defaults
    are the user's, and differ (`config.TaggingConfig`).
    """

    transforms: frozenset[TagTransform] = frozenset()
    #: Field names are Picard's option names, like the values.
    standardize_artist_names: ArtistNames = ArtistNames.NONE
    always_standardize_multivalue_artist: bool = False


#: `TaggingChoices()`, once: the default for callers that write MusicBrainz's own
#: spelling. Safe to share because the dataclass is frozen.
NO_CHOICES = TaggingChoices()


@dataclass(frozen=True)
class ArtistSpelling:
    """How one artist of a credit is written, under the user's choices."""

    #: In the display credit, `artist` / `albumartist`, between the join phrases.
    name: str
    #: In the sort credit; empty when MusicBrainz gave no sort name to use.
    sort_name: str
    #: In the multi-value list, `artists` / `albumartists`.
    list_name: str


#: The alias types Picard counts as a name the artist went by. An untyped alias
#: counts too (`picard.mbjson.should_standardize_artist_name`); a search hint or
#: a legal name does not.
_NAME_ALIAS_TYPES = frozenset({None, "Artist name"})


def is_former_name(credited: str, artist: Mapping[str, Any]) -> bool:
    """Whether `credited` is a name `artist` no longer goes by (#678).

    Picard's test, read through the XML Harmonist fetches: an alias of type
    Artist name (or untyped) that has ENDED, matched on its name or its sort
    name. The XML has no `ended` flag for an alias, only dates, so "ended" is
    "has an end date" — an alias marked ended without one reads as current, and
    such a credit is standardized where Picard keeps it. The fix for that
    belongs on MusicBrainz (date the alias), and the way to parity is #690.
    """
    for alias in artist.get("alias-list") or []:
        if (
            alias.get("type") in _NAME_ALIAS_TYPES
            and alias.get("end-date")
            and credited in (alias.get("alias"), alias.get("sort-name"))
        ):
            return True
    return False


def _standardizes(names: ArtistNames, credited: str, artist: Mapping[str, Any]) -> bool:
    match names:
        case ArtistNames.NONE:
            return False
        case ArtistNames.ALL:
            return True
        case ArtistNames.VARIATIONS:
            return not is_former_name(credited, artist)


def _alias_sort_name(artist: Mapping[str, Any], credited: str) -> str | None:
    """The sort name of the alias `credited` IS, when there is one — Picard's
    `_select_sort_name_from_aliases`, so "Scarecrow Boat" sorts as "Boat,
    Scarecrow" rather than under the band's current name."""
    for alias in artist.get("alias-list") or []:
        if alias.get("alias") == credited and alias.get("sort-name"):
            return str(alias["sort-name"])
    return None


def artist_spelling(entry: Mapping[str, Any], choices: TaggingChoices) -> ArtistSpelling:
    """One entry of an artist credit, spelled the way the user chose (#678).

    The credited name is kept unless the setting standardizes it; a kept name
    sorts by its own alias where it is one, a standardized one by the artist.
    The list name is the artist's own whenever the multi-value checkbox is on,
    so a player groups the album under one name while the display credit says
    what the release says.

    A function of the RELEASE only, never of the file, so applying it twice is
    applying it once — the same property `album_title` has.
    """
    artist: Mapping[str, Any] = entry.get("artist") or {}
    own_name = str(artist.get("name") or "")
    own_sort = str(artist.get("sort-name") or "")
    credited = str(entry.get("name") or own_name)
    if _standardizes(choices.standardize_artist_names, credited, artist):
        name, sort_name = own_name, own_sort
    else:
        name, sort_name = credited, _alias_sort_name(artist, credited) or own_sort
    list_name = own_name if choices.always_standardize_multivalue_artist else name
    return ArtistSpelling(name=name, sort_name=sort_name, list_name=list_name)


def album_title(release: Release, enabled: Collection[TagTransform]) -> str:
    """The album title a tagging writes — MusicBrainz's, or the user's spelling.

    Falls back to MusicBrainz's plain title whenever the transform is off OR the
    release has no disambiguation to append, which is the same answer by two
    routes: a release with no disambiguation has exactly one title, so there is
    nothing for the setting to choose between.
    """
    title: str = release.get("title", "")
    if TagTransform.ALBUM_DISAMBIGUATION in enabled:
        return title_with_disambiguation(title, release.get("disambiguation")) or title
    return title


def every_choice() -> tuple[TaggingChoices, ...]:
    """Every combination of the spelling settings — the space a library's
    existing tags may have been written under, by Harmonist or by Picard.

    What "a spelling the release supports" means (#678): a value some
    combination writes for this release. Derived by running the write under
    each, never from a pattern, so it is exactly the strings the release states
    (review-gate item 2) — `models.titles_match` would accept `(deluxe edition)`
    where only the release's own disambiguation is a supported title. And not
    conditioned on the user's choice, so whichever spelling a file carries,
    the setting's verdict about it is the same.

    Small by construction: each title transform on or off, times Picard's three
    artist choices, times the list checkbox.
    """
    transform_sets = [frozenset[TagTransform]()]
    for transform in TagTransform:
        transform_sets += [s | {transform} for s in transform_sets]
    return tuple(
        TaggingChoices(
            transforms=transforms,
            standardize_artist_names=names,
            always_standardize_multivalue_artist=lists,
        )
        for transforms in transform_sets
        for names in ArtistNames
        for lists in (False, True)
    )
