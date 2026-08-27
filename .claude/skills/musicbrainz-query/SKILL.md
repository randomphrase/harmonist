---
name: musicbrainz-query
description: How Harmonist talks to MusicBrainz — which function to call, whether the answer may come from the cache, and what the payload can do to you. Consult BEFORE writing or moving any code that fetches from MusicBrainz, before adding an `includes` entry, and before deciding a stored answer is good enough. Every rule here is one a correct-looking call can break silently: the wrong function still returns the right data and merely costs a request that did not need spending, and a cached negative still parses.
---

# Querying MusicBrainz

One request per second, shared across everything Harmonist does, on a service
that is a volunteer project rather than a CDN. Every rule below exists because
breaking it produces code that *works* — right data, no exception, tests green —
and quietly costs something.

Two neighbours own the parts this doesn't: **`review-gate` item 6** is the call
budget as a commit gate, and `mb_cache`'s module docstring is the authority on
who may be served a stored answer and why the row outlives its TTL. Read those
rather than expecting them repeated here. This skill is about picking the call
and surviving the response.

## 1. Which function — and is it cached?

| Call | Shape | Cached? |
|---|---|---|
| `mb_cache.fetch_release(mbid)` | by id | **yes** |
| `mb_cache.fetch_release_urls(mbid)` | by id | **yes** (separate row — different `inc`) |
| `mb_lookup.lookup_by_bandcamp_url(url)` | URL → mbids | no, deliberately |
| `mb_lookup.candidate_summaries_for_url(url)` | URL → summaries | no, deliberately |
| `mb_lookup.browse_release_group_releases(rg)` | browse | no |
| `mb_lookup.fetch_video_media(mbid)` | by id | no — the **sidecar** is its cache |
| `mb_search.search_releases(artist, title)` | search | no, deliberately |

**Never call `mb_lookup.fetch_release` or `fetch_release_urls` directly.** They
work perfectly, return the right release, and spend a request the cache would
have saved. Nothing fails, so nothing catches it but review — which is exactly
why `review-gate` asks about it by name.

The third pattern in that table is worth knowing about before you invent a
fourth: `fetch_video_media` is a by-id fetch that is *not* in `mb_cache`,
because its answer is stored in the sidecar (`video_media`) and the album is
never asked again. When the answer is a durable fact about the album rather
than a snapshot of a release, the sidecar can be the cache — but that is a
sidecar field, so it goes through the `sidecar` skill and review-gate item 3
before it exists.

## 2. Freshness is the caller's decision, and it is a real one

`max_age=mb_cache.FRESH` forces a live fetch. Pass it when:

- **you are about to write to the user's files.** Tagging from an hour-old
  payload writes metadata Harmonist has already been told was superseded;
- **the user pressed something meaning "look again".** Recheck, the album page's
  re-read control. Serving those a stored answer makes the button a silent
  no-op with nothing on screen to say why.

Everything that merely displays or compares may take the stored answer.

**Force freshness by passing `FRESH`, never by reaching round the cache to
`mb_lookup`.** Going through keeps the row refreshed by the fetch that was
happening anyway, and that row is #32's change-detection baseline — bypassing it
leaves the gardener comparing against something older than the data it just had
in its hand.

## 3. Never store a negative

"MusicBrainz doesn't have this yet" is the one answer most likely to be wrong by
tomorrow. It is the state a user is actively trying to leave — they seed the
release in Harmony precisely so the next look finds it — and a cached negative
would make their own edit invisible to them for the length of a TTL.

Today this holds **structurally rather than by policy**, which is sturdier and
worth preserving:

- the lookups that can answer "no" — URL lookup, search, browse — don't go
  through the cache at all, so there is nowhere for a negative to be stored;
- `mb_cache.fetch_release` reaches `store_release` only on success.
  `ReleaseGoneError` and `MBError` propagate first, so neither a 404 nor a
  network failure ever becomes a row.

The same rule holds one layer over, where the sidecar is the cache.
`reconcile.record_video_media` deliberately raises rather than recording an
empty tuple when the lookup fails, because "none of this release's media are
video" written down on evidence nobody gathered marks the album incomplete
forever. A failed request and a genuine empty answer must never be stored the
same way.

If you ever put a caching layer in front of a lookup that can return empty, that
property stops being free and you have to build it. An empty result is not an
answer worth keeping.

## 4. A 404 is an answer, not a failure

`ReleaseGoneError` means MusicBrainz deleted or merged away the release, and the
album page has a whole response built for it (#194/#210, and #32 wants the same
outcome delivered unattended). Catching it alongside `MBError` and returning an
empty list turns a fact the user needs into a shrug. See the `error-handling`
skill for the general form of that mistake; this is its most expensive instance.

## 5. Follow the id the release actually has

MusicBrainz **redirects** a merged MBID, so `fetch_release(old)` returns the
target release under a *different* `id`. That difference is the only merge
notification there is.

Always read `release["id"]` afterwards; never assume it is the mbid you asked
for. Writing the requested id anywhere — a sidecar, a cache row, a comparison —
while the files get the returned one is #268 exactly: the album derived its
state from a release that no longer existed, and self-healed through machinery
meant for "the user re-tagged in Picard". `docs/design.md` §5 carries the rule.

## 6. `includes` is part of the cache key

A cache row is keyed by `(mbid, inc)`, where `inc` is the sorted `includes`
tuple. Two consequences:

- **adding an entry to `RELEASE_INCLUDES` is a cache-key change**, and it
  happens automatically *because* the key is derived from the same tuple used
  for the request. Introduce a second, hand-written list of includes and that
  stops being true: every stale row keeps being served under an unchanged key,
  parsing perfectly, with nothing able to detect the drift. One definition, used
  for both;
- the key is **sorted**, so reordering the tuple cannot orphan every existing
  row.

Prefer adding an include to making a second call. The rate limit is **per
request, not per byte** — a fatter payload is free, a second round trip is not.
That is the whole reason `RELEASE_INCLUDES` is one tuple rather than a few
targeted fetches, and it is the opposite of the instinct most API work teaches.

The row's storage — the table, its columns, migrating it — is the
`schema-migration` skill's business, not this one's.

## 7. Demo mode patches module attributes

`demo.py` replaces `mb_lookup.fetch_release`, `lookup_by_bandcamp_url`,
`browse_release_group_releases`, `fetch_video_media` and
`mb_search.search_releases` **on the module object**. `mb_cache` calls them
through that attribute, which is what keeps demo mode working underneath the
cache.

So `from .mb_lookup import fetch_release` and then calling the bare name binds
the real function at import time and the patch never lands. Demo mode then makes
live MusicBrainz requests while appearing to work — the worst available failure,
since the whole point of demo mode is that it touches nothing real. Call through
the module: `mb_lookup.fetch_release(...)`.

## 8. Count the requests

The request count *is* the feature. An otherwise-correct cache can get it wrong
with every assertion still passing, because the data is right either way — so
assert on the number of calls, not on the payload. `review-gate` item 6 requires
this; the `testing` skill's rung question decides where it goes.

Bound the count by a constant or by the size of the user's explicit selection.
Never by library size in a loop the user did not ask for — that is the rule
#32's paced runner exists to respect rather than to break at scale.

## Before you commit

1. Is every by-id fetch going through `mb_cache` rather than `mb_lookup`?
2. Does anything that writes files, or that the user pressed to force a
   re-check, pass `max_age=mb_cache.FRESH` — rather than bypassing the cache?
3. Can any new cached path store an empty or failed result? It must not.
4. Does `ReleaseGoneError` still reach something that can act on it?
5. Is the code reading `release["id"]` rather than the mbid it requested?
6. If `includes` changed: is the cache key still derived from the same tuple the
   request uses?
7. Are MusicBrainz functions called through their module, so demo mode's patch
   lands?
8. Is there a test asserting how many requests this makes?
