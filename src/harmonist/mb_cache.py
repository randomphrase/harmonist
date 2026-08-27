"""A TTL cache in front of MusicBrainz's get-by-id fetches (#127).

MusicBrainz rate-limits at **one request per second, per request rather than per
byte**. That single fact shapes everything here:

* a conditional GET would not help. MusicBrainz's ETag is not a content
  validator — three identical requests return three different ETags — and a
  `304` consumes a rate-limit slot anyway, so it would save bandwidth and
  parsing but not the constraint that actually binds. `musicbrainzngs` cannot
  send `If-None-Match` regardless (#26);
* **not asking at all** is the only real saving, which is what a TTL does, and
  it needs no client change whatsoever.

Hence a layer *above* `mb_lookup` rather than inside it. `mb_lookup` stays a
plain API client that knows nothing about SQLite, and demo mode keeps working by
monkey-patching its functions — this module calls them through the module
attribute, so a patched fetch is still what gets called.

## Who gets a cached answer and who does not

The rule is one sentence: **reads that display or compare may be cached; writes
and anything the user pressed to force a re-check fetch fresh.**

* the album page's disk-vs-MB comparison, the mis-tag sweep, candidate
  assessment — cached. All of them are looking at a release, not acting on it,
  and the sweep's cost is bounded by library size rather than by a user's
  selection, which is the review-gate's call-budget concern exactly.
* `_tag_with_release` — **never cached.** It writes tags to the user's files.
  Doing that from an hour-old payload would write metadata Harmonist had already
  been told was superseded.
* **Recheck** — never cached. The entire meaning of that button is "I just
  edited MusicBrainz"; serving it a stored answer would make it a no-op and the
  user would have no way to tell.

## The row outlives the TTL on purpose

`max_age` decides whether a row may be *served*, not how long it is *kept*. An
expired row is still the last thing MusicBrainz said, and #32's gardener detects
change by comparing a fresh fetch against it — so expiry means "ask again", never
"forget". Eviction is deliberately absent for the same reason: once findings
exist (#271), a row is the evidence its finding was raised against, and dropping
it would leave a review the user could no longer be shown the basis for.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from . import activity_store, mb_lookup
from .models import Release

log = logging.getLogger(__name__)

# How stale a served payload may be. Configured at startup; the default matches
# `MusicBrainzConfig.cache_ttl_seconds` so an unconfigured process (tests, a
# script) behaves like a default install rather than like a disabled cache.
_ttl = timedelta(hours=1)

# What a caller passes to mean "ask MusicBrainz, whatever is stored". Named
# rather than written as `timedelta(0)` at ten call sites, because it is a
# decision — "this caller must not be served a cached answer" — and a bare zero
# reads like an oversight.
#
# Note it still goes THROUGH this module rather than round it: a forced fetch
# refreshes the stored row, which keeps the gardener's baseline current. Calling
# `mb_lookup` directly would bypass the fetch and the recording both.
FRESH = timedelta(0)


def configure(ttl: timedelta) -> None:
    """Set the freshness window (called once from `create_app`).

    A zero (or negative) TTL disables *serving* — every read fetches — while
    still recording what came back, so the gardener's baseline keeps accruing and
    turning the cache off does not turn change detection off with it.
    """
    global _ttl
    _ttl = ttl


def _key(includes: tuple[str, ...]) -> str:
    """The `inc` half of a cache row's key.

    Sorted, so a reordering of the includes tuple cannot silently orphan every
    row written under the old order — the request is order-insensitive, and the
    key has to be too.
    """
    return "+".join(sorted(includes))


def _fresh(cached: activity_store.CachedRelease, max_age: timedelta) -> bool:
    if max_age <= timedelta(0):
        return False
    age = datetime.now(UTC) - cached.fetched_at
    # A negative age means the row was stamped in the future — a clock that has
    # gone backwards, which on a NAS after an NTP correction is ordinary. Treat
    # it as stale rather than as infinitely fresh, so the worst a bad clock can
    # do is cost a request.
    return timedelta(0) <= age < max_age


def fetch_release(mbid: str, *, max_age: timedelta | None = None) -> Release:
    """`mb_lookup.fetch_release`, served from the cache when it is fresh enough.

    `max_age=timedelta(0)` forces a live fetch and refreshes the stored row —
    what Recheck and the album page's re-read control pass.

    `ReleaseGoneError` and `MBError` propagate untouched. A cache miss must not
    turn a deleted release into a stale-but-served one: #268's sibling case is
    that a 404 is an *answer*, and the album page has a whole response built for
    it (#194/#210).
    """
    inc = _key(mb_lookup.RELEASE_INCLUDES)
    cached = activity_store.cached_release(mbid, inc)
    if cached is not None and _fresh(cached, _ttl if max_age is None else max_age):
        return cached.payload
    release = mb_lookup.fetch_release(mbid)
    # Stored under the id the release ACTUALLY has, which after a merge is not
    # the one asked for (#268). Keying the row by the requested id would cache
    # the surviving release under a dead MBID — served to nobody, and leaving
    # the real id uncached.
    activity_store.store_release(str(release["id"]), inc, release)
    return release


def stored_release(mbid: str) -> Release | None:
    """What MusicBrainz last said about `mbid`, or None if we never asked.

    **Reads the store and never the network**, whatever the row's age — the one
    caller that wants that is #287's warm-up, which rebuilds the update-available
    flags after a restart and must cost zero rate-limited requests. Age is
    irrelevant to it: a stale row is still the last thing MusicBrainz said, which
    is exactly the baseline the flag is derived against, and a fresher answer is
    the background pass's job to go and get (#270).

    Distinct from `fetch_release(max_age=...)`, which answers "give me a release"
    and will spend a request to do it. This answers "have we got one already",
    and a None is an answer rather than a reason to go and ask.
    """
    cached = activity_store.cached_release(mbid, _key(mb_lookup.RELEASE_INCLUDES))
    return cached.payload if cached is not None else None


def fetch_release_urls(mbid: str, *, max_age: timedelta | None = None) -> list[str]:
    """`mb_lookup.fetch_release_urls`, served from the cache when fresh enough.

    A separate row from `fetch_release`'s under the same MBID, because it is a
    different `inc=` and therefore a different payload — the whole reason the
    key is composite.
    """
    inc = _key(mb_lookup.RELEASE_URL_INCLUDES)
    cached = activity_store.cached_release(mbid, inc)
    if cached is not None and _fresh(cached, _ttl if max_age is None else max_age):
        return _urls_of(cached.payload)
    urls = mb_lookup.fetch_release_urls(mbid)
    # Stored in the shape MusicBrainz returned, not as the flat list, so the row
    # stays a faithful record of the response — the gardener compares payloads,
    # and a list of targets has already thrown away the relationship types.
    activity_store.store_release(mbid, inc, {"url-relation-list": [{"target": u} for u in urls]})
    return urls


def _urls_of(payload: dict[str, Any]) -> list[str]:
    rels = payload.get("url-relation-list") or []
    return [r["target"] for r in rels if isinstance(r, dict) and r.get("target")]


def fetched_at(mbid: str) -> datetime | None:
    """When the album's release payload was last read from MusicBrainz, or None
    if it never has been.

    The album page's "read N ago" (#106) — the affordance that makes a cached
    comparison honest, since a user looking at a diff needs to know whether an
    edit they just made upstream is in it.
    """
    cached = activity_store.cached_release(mbid, _key(mb_lookup.RELEASE_INCLUDES))
    return None if cached is None else cached.fetched_at
