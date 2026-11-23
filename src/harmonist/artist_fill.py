"""What an artist NFO lacks that Harmonist can fill in, and why it couldn't.

Identifying an artist (#1018) and reading it from MusicBrainz again (#1022)
both fill in a biography where the file has none, from the Wikipedia article
MusicBrainz's Wikidata link leads to. What the lookup found is kept beside the
artist's stored answer, so the artist page can say why there is no biography
without asking anyone when it is drawn.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from . import activity_store, nfo_history, wikimedia

log = logging.getLogger(__name__)

#: Keeps a failed lookup's detail out of the Activity feed: the action it was
#: part of reports the failure there itself.
_LOG_ONLY = {"_activity": True}


@dataclass(frozen=True)
class Filled:
    """What a biography lookup found to fill in: the biography, or none, and
    whether the lookup failed rather than found nothing."""

    biography: wikimedia.Biography | None = None
    failed: bool = False

    @property
    def credits(self) -> dict[str, nfo_history.Credit]:
        found = self.biography
        return {"biography": nfo_history.Credit(found.url, found.revision)} if found else {}


def biography(
    mbid: str, answer: Mapping[str, Any], *, languages: Sequence[str], user_agent: str
) -> Filled:
    """The biography for the artist MusicBrainz answered with as `answer`: the
    introduction of the article its Wikidata item links to, in the first of
    `languages` that has one. Never found by name, so without a Wikidata link
    on MusicBrainz there is none, and Wikipedia isn't asked.

    What it found is kept with the artist's stored answer, which the caller has
    stored already."""
    item = wikimedia.wikidata_item(answer)
    if item is None:
        return Filled()
    try:
        found = wikimedia.biography(item, languages=languages, user_agent=user_agent)
    except wikimedia.WikimediaError as exc:
        log.warning("could not fetch a biography for %s: %s", item, exc, extra=_LOG_ONLY)
        _keep(mbid, item, languages, "failed")
        return Filled(failed=True)
    _keep(mbid, item, languages, "found" if found else "missing")
    return Filled(biography=found)


def note(stored: activity_store.CachedArtist | None) -> str | None:
    """Why a file with no biography has none, from the artist's stored answer:
    the last word on it, as of when MusicBrainz was read. None when nothing
    says why, such as an artist never read, or a lookup about an item
    MusicBrainz no longer links."""
    if stored is None:
        return None
    item = wikimedia.wikidata_item(stored.payload)
    if item is None:
        return "MusicBrainz links no Wikidata item"
    lookup = stored.biography or {}
    if lookup.get("item") != item:
        return None
    if lookup.get("status") == "missing":
        return f"No Wikipedia article in {' or '.join(lookup.get('languages') or ())}"
    if lookup.get("status") == "failed":
        return "Wikipedia couldn't be reached"
    return None


def unlinked(stored: activity_store.CachedArtist | None) -> bool:
    """Whether MusicBrainz, as last read, links the artist to no Wikidata item:
    the one reason for no biography the user can put right, on MusicBrainz
    (#1025). False for an artist never read, since nothing says so yet."""
    return stored is not None and wikimedia.wikidata_item(stored.payload) is None


def _keep(mbid: str, item: str, languages: Sequence[str], status: str) -> None:
    activity_store.store_artist_biography(
        mbid, {"item": item, "languages": list(languages), "status": status}
    )
