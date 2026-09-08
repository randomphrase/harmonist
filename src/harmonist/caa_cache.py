"""A TTL cache in front of the Cover Art Archive's front-cover check (#436).

`mb_cache` for the other service Harmonist asks about a release, and deliberately
the same shape: **ask when the stored answer is missing or older than a TTL, and
serve it from the store otherwise.** That is the property that makes the
MusicBrainz check invisible while browsing, and the reason the archive check was
not — nothing consulted the stored answer's age, so the choice was between asking
every single time and asking never, and never was the safe one.

Hence a layer *above* `cover_art` rather than inside it, exactly as `mb_cache`
sits above `mb_lookup`: `cover_art` stays a plain archive client that knows
nothing about SQLite, and demo mode keeps working by monkey-patching its
functions — this module calls `check_front` through the module attribute, so a
patched check is still what gets called.

## Where the two services differ, and why the TTL is so much longer

MusicBrainz rate-limits **per request**, so a conditional GET saves nothing and
not asking is the only real economy. The archive's constraint is the opposite:
its cost is *bytes and seconds*. A check is up to three requests, it is served
from the Internet Archive, and one measured **sixteen seconds** over a remote
link — while `If-None-Match` genuinely works (a 304 with zero bytes), because
unlike MusicBrainz's, the archive's ETag is a real content validator.

Two consequences:

* **the TTL is days, not hours.** An album's cover art changes far less often
  than its tags, and the archive is the slowest thing Harmonist talks to;
* **a stale row is still worth sending back.** `check_front` is given the stored
  answer whatever its age, so a re-check of a release whose art has not changed
  costs one request and no transfer. Expiry means "ask again", never "forget" —
  the same rule `mb_cache` states, reached from the other direction.

## Storing "the archive has nothing" is safe *because* of the TTL

`musicbrainz-query` rule 3 forbids caching a negative, and this module does cache
one: "no front cover for this release" is the commonest answer for a private
Bandcamp release, and #276 stores it so the next check does not ask again. What
makes that survivable here is exactly what was missing before — the answer now
expires. A cover uploaded to the archive today is found within a TTL by itself,
and the album page's control (#419) is the way to not wait.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from . import activity_store, cover_art, timing

log = logging.getLogger(__name__)

# How stale a stored answer may be before the page asks again. Configured at
# startup; the default matches `CoverArtConfig.cache_ttl_seconds` so an
# unconfigured process (tests, a script) behaves like a default install rather
# than like a disabled cache.
_ttl = timedelta(days=7)

# What a caller passes to mean "ask the archive, whatever is stored" — the album
# page's re-ask control, which exists precisely to say "look again". Named for
# the reason `mb_cache.FRESH` is: a bare `timedelta(0)` at a call site reads like
# an oversight rather than a decision.
#
# It still goes THROUGH this module rather than round it: a forced check refreshes
# the stored row, and passes the stored etag, so the forced path is the cheap one
# too.
FRESH = timedelta(0)

# When a check is slow enough to be worth a line in the log (#300).
#
# Far above `mb_cache._SLOW_FETCH`, because a healthy check here is far slower: it
# is up to three requests (a release listing, a release-group listing, a ranged
# read of the image), served through a redirect to the Internet Archive, and one
# measured sixteen seconds over a remote link. A threshold near MusicBrainz's
# would warn on ordinary checks, which is the warning nobody reads.
#
# Sixty seconds is past the point where two of `cover_art.DEFAULT_TIMEOUT`'s
# thirty-second windows have gone by — so the line means "this did not merely
# take the long path", which is the only thing worth waking someone for.
_SLOW_CHECK = timedelta(seconds=60)


def configure(ttl: timedelta) -> None:
    """Set the freshness window (called once from `create_app`).

    A zero (or negative) TTL disables *serving*, so every album page open asks
    the archive — the same meaning `mb_cache.configure` gives it, and a costlier
    one here. The check lands out of band either way, so the page still paints.
    """
    global _ttl
    _ttl = ttl


def _fresh(known: activity_store.CachedCoverArt, max_age: timedelta) -> bool:
    """Whether a stored answer may be served without asking.

    The same predicate as `mb_cache._fresh`, negative age included — a row
    stamped in the future is a clock that has gone backwards, which on a NAS
    after an NTP correction is ordinary, and treating it as stale means the worst
    a bad clock can do is cost a request. Its own six lines rather than a shared
    helper: the two caches store different types, and neither module has any
    other reason to import the other.
    """
    if max_age <= timedelta(0):
        return False
    age = datetime.now(UTC) - known.fetched_at
    return timedelta(0) <= age < max_age


def stored(mbid: str) -> activity_store.CachedCoverArt | None:
    """What the archive last said about `mbid`, or None if we never asked.

    **Reads the store and never the network**, whatever the row's age — what
    every render of the Artwork section uses, so drawing a page costs nothing
    however old the answer is. Age is the caller's to report, not this
    function's to act on: the panel says how old it is (#276) and offers the
    control that gets a newer one (#419).
    """
    return activity_store.cached_cover_art(mbid)


def due(mbid: str) -> bool:
    """Whether this release is worth asking the archive about again.

    Asked by the album page to decide whether to send the out-of-band check —
    which is why it is a question of its own rather than a side effect of
    `front`. The section must render from what is known *before* anything leaves
    the machine, so the decision has to be made without making the request.
    """
    known = stored(mbid)
    return known is None or not _fresh(known, _ttl)


def front(
    mbid: str,
    *,
    release_group_mbid: str | None = None,
    keep_if_wider_than: int | None = None,
    max_age: timedelta | None = None,
) -> activity_store.CachedCoverArt:
    """`cover_art.check_front`, served from the store when it is fresh enough.

    `max_age=FRESH` forces a live check and refreshes the stored row — what the
    album page's re-ask control passes.

    `CoverArtError` propagates untouched. A transport failure must never be
    recorded as "the archive has nothing": those are different facts, and the
    caller's job is to leave the stored answer — and its timestamp — exactly
    where they were, which is the honest signal that the check did not happen.
    """
    known = stored(mbid)
    if known is not None and _fresh(known, _ttl if max_age is None else max_age):
        return known
    with timing.warn_if_slow("Cover Art Archive check", _SLOW_CHECK, mbid=mbid):
        # Through the module attribute, so demo mode's patch lands.
        #
        # `known` is passed even though it was too old to serve: it carries the
        # etag, and an unchanged listing then answers 304 with no body. Staleness
        # decides whether to ASK, not whether the previous answer is of any use.
        answer = cover_art.check_front(
            mbid,
            release_group_mbid=release_group_mbid,
            known=known,
            keep_if_wider_than=keep_if_wider_than,
        )
    activity_store.store_cover_art(mbid, answer)
    return answer
