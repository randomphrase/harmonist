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

That is the whole reason a transform is cheap, and the three consequences are
what make it safe to ship:

- **Turning one on does not rewrite the library.** The tagging diff compares
  against the accepted set, so a file already holding another accepted spelling
  reports no change — no library-wide rewrite, and no Inbox full of albums on
  #32's first night, which is #283's failure mode in reverse.
- **The gardener needs no configuration.** Because the accepted set is
  transform-independent, `plan_album` reaches the same verdict under any
  setting, so the pass that runs unattended is not reading a preference it has
  no way to see.
- **It stays idempotent.** A transform is a function of the RELEASE, never of
  what is already on the file, so applying it twice is applying it once. A rule
  that read the existing tag could grow the title a little on every pass.

The bounded visible effect follows from all three and is what the Settings page
has to say out loud: a new album gets the preferred spelling, an album re-tagged
for some other reason gains it, and albums sitting on disk are not rewritten to
acquire it.

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
