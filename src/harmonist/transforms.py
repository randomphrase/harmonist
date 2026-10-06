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

from collections.abc import Collection
from enum import StrEnum

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


def can_move(release: Release, transform: TagTransform) -> bool:
    """Whether turning `transform` on or off can change what a tagging of
    `release` writes — so whether an album on it needs re-checking when the
    setting changes (#685).

    Answered from the release alone, never the files: it decides which albums
    to READ, and a release with only one spelling has nothing for the setting to
    choose between, whatever its files say.
    """
    match transform:
        case TagTransform.ALBUM_DISAMBIGUATION:
            return len(accepted_album_titles(release)) > 1


def accepted_album_titles(release: Release) -> frozenset[str]:
    """Every album title this release legitimately has, on disk or from us.

    **Not conditioned on the enabled set**, per the module invariant above: both
    spellings are accepted whichever one a write would emit, so flipping the
    setting cannot turn a library into a library of differences.

    Two members at most, and usually one — MusicBrainz's title, plus Picard's
    disambiguated spelling of it where the release carries a disambiguation
    (#283). Exactly these strings, never a pattern: `models.titles_match` would
    accept `(deluxe edition)` and `(2019 remaster)` just as readily on the
    strength of the words alone, and here the release states its disambiguation
    for free.
    """
    title: str = release.get("title", "")
    spellings = {title} if title else set()
    if disambiguated := title_with_disambiguation(title, release.get("disambiguation")):
        spellings.add(disambiguated)
    return frozenset(spellings)
