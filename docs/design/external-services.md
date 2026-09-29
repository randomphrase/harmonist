# MusicBrainz and the Cover Art Archive

Harmonist asks two services about a release. Their costs are different in kind,
and the caching in front of each follows from that difference.

## MusicBrainz

### One request per second, per request

MusicBrainz allows one request per second, and the limit counts **requests, not
bytes**. That settles several things at once:

- **A bigger payload is free and a second request isn't.** So a release is
  fetched once with everything any part of Harmonist needs, rather than in
  several targeted calls.
- **A conditional request saves nothing.** A `304 Not Modified` still uses a
  request, and MusicBrainz's ETags don't identify content anyway: three identical
  requests return three different ETags. The only saving is **not asking**.
- **Every request made on the user's behalf is bounded** by a constant or by
  what they selected, never by library size
  ([principles](principles.md#spend-musicbrainzs-budget-carefully)).

### The cache

Releases fetched by ID are stored in `activity.db` and reused for an hour. The
cache is a layer above the MusicBrainz client, not inside it. That keeps the
client a plain API wrapper, and lets demo mode replace the client's functions
while everything above it still works.

**A row is served for its TTL but kept forever.** Expiry means "ask again", not
"forget". An expired row is still the last thing MusicBrainz said, which is what
the update check compares a fresh answer against, and what any finding shown to
the user was based on. Nothing is evicted.

**The TTL decides when to ask again, not whether an answer may be shown.** An
album page draws its comparison from the stored release whatever its age, and
if that's past the TTL, fetches a fresh one after the page is up. The cost is the
same request either way, spent behind a finished page rather than in front of a
blank one. While that request is outstanding, the actions that write from the
release are held, since the release on screen may not be the one that answers.

**Who is served a stored answer:**

- anything that only displays or compares: yes;
- a write the user hasn't reviewed, and anything the user pressed meaning "look
  again": no, it fetches fresh. A "look again" button served from the cache
  would silently do nothing;
- a write the user *has* reviewed: always the exact stored release they reviewed,
  whatever its age, checked against a fingerprint of what they saw. If it's
  missing or has changed, they review again. A reviewed write never fetches,
  because a fresh answer would be something they didn't review (#532).

A forced fetch still goes through the cache, so it refreshes the stored row
rather than leaving an older one behind.

**The cache key includes what was asked for.** The same release fetched with
different parts included is a different payload, and serving one to a caller
expecting the other would be wrong data that still parses. The key is derived
from the very list that makes the request, so the two can't drift. The price is
that adding anything to that list orphans every stored row on every install, and
the library is gradually fetched again. That's accepted rather than worked
around: a fallback to older rows would be a second source of truth, and an older
row missing a list reads as an empty list.

### Never store a negative

"MusicBrainz doesn't have this yet" is the answer most likely to be wrong
tomorrow. It's the state a user is actively trying to leave, by adding the
release through Harmony, and a cached negative would hide their own edit from
them. So URL lookups and searches aren't cached at all, and a by-ID fetch is
stored only when it succeeds.

The one exception is the stored list of a release group's releases, used to
place albums in the Possible mismatch and MB contributions filters without a
request per album. Because it records absences, it's never used to decide what
to *offer*: the album page always fetches its own before suggesting a change (see
[contributions](contributions.md)).

### Merges and deletions

MusicBrainz **merges** duplicate releases, and it doesn't 404 a merged ID: it
redirects, answering with the surviving release under a different ID. That
difference is the only notice of a merge there is.

- **A tagging follows the release it actually got.** The sidecar records the ID
  that came back, which is the one written into the files. Recording the
  requested ID made the two disagree, and the album then went round a loop
  meant for re-tagging in Picard (#268).
- **A merge is always applied, never held for review**, even by an unattended
  pass. It has already happened on MusicBrainz, and there's no old release left
  to stay on. It's recorded in the album's history by name, with both IDs.

A **deletion** is a 404. It's an answer, not a failure: the album page says the
release is gone and offers to find another, and nothing is written. Treating it
as a network error would hide a fact the user needs to act on.

## The Cover Art Archive

The archive's costs are the opposite of MusicBrainz's. There's no request budget,
but a check is up to three requests through a redirect to the Internet Archive,
and one has been measured at sixteen seconds. Its ETags do identify content, so
a conditional request genuinely saves the transfer.

So the same caching shape is tuned differently:

- **The TTL is a week**, not an hour. Cover art changes far less often than tags.
- **A stale answer is sent back with the question**, and unchanged art costs one
  request and no transfer.
- **A check never delays a page.** The Artwork section is drawn from the stored
  answer, and the check runs afterwards. A failed check leaves the old answer and
  its date, and doesn't retry by itself, or a page would ask forever.
- **"No cover" is stored**, which MusicBrainz's rule forbids. It's the commonest
  answer for a private Bandcamp release, and it's safe here only because it
  expires: art uploaded today is found within a week, and the refresh control is
  there for not waiting.
- **Only the original image is fetched.** The largest image is the one Harmonist
  prefers, and it's the one the album page measures, so a preview and the
  tagging after it compare the same picture.

### The image cache

The archive's winning images are kept on disk so the album page can show them and
a later tagging can write them without downloading again. Once the page checks
the archive by itself, browsing alone fills this cache, so it's capped (1 GB by
default) and evicts the images viewed least recently.

Unlike [kept artwork](artwork.md#a-backup-is-what-permits-a-replacement), these
are copies the archive still has, so the cache makes no promises and needs no
protected set. A newer answer from the archive discards the older image rather
than risk showing it as the new one, and an evicted image is fetched again the
next time its album is opened.
