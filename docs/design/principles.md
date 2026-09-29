# Principles

Harmonist runs unattended for months on someone's NAS, rewriting tags in a
library that may be decades old and that its owner cares about far more than
they care about Harmonist. Nearly every design decision follows from taking that
seriously. The `review-gate` skill checks each change against the principles
below.

## MusicBrainz is canonical

Harmonist writes what MusicBrainz says and keeps no local exceptions to it. When
a release is wrong, the correction belongs on MusicBrainz, where it benefits
everyone and comes back to the files on the next check.

This settles a class of questions before they're argued. There is no per-album
override ("don't apply MusicBrainz's title to this album"), and one couldn't be
built honestly anyway: what would be overridden is a *difference*, and a
difference isn't a stable object. It's computed between two sides that both move,
so it merges and splits as MusicBrainz is edited and the files change, and there
is no key that survives that (#271).

What a user *can* do is decline to be nagged: ignoring an update is a bookmark
that lapses when the release next changes (see
[staying current](staying-current.md#ignoring-an-update)).

Where Harmonist accepts a value MusicBrainz didn't state, it's because a rule
says the two spellings mean the same thing, derived fresh from the release each
time and never remembered per album (see [tagging](tagging.md#accepted-spellings)).

## Transparency over perfect automation

Matching a mixed, decades-old library will sometimes be wrong. The design goal
is not to be right every time, but that being wrong costs a click and never a
silent duplicate or silent data loss. So every download, link, tag and image
write is visible, recorded, and reversible, and uncertain decisions go to the
user rather than being made for them.

## No guessing

Identity comes only from authoritative sources: a MusicBrainz ID in the files, a
store URL, a barcode, or the user's own choice. Harmonist never scrapes a page,
invents a URL (constructing an `/album/` slug from a title, say), or ranks fuzzy
candidates and takes the best one.

Where matching can't be avoided, it must be all three of:

- **exact**: normalised equality, never similarity scoring;
- **scoped**: searched only inside a context already established, such as one
  user's purchases or one release's tracks, never globally;
- **unique**: an ambiguous match is no match, and goes to the user.

Loosening any of the three is a design change, not an implementation detail.
Title matching during Bandcamp linking is the one deliberate case of matching on
something other than an identifier, and it is allowed only because it meets all
three (see [Bandcamp](bandcamp.md#why-title-matching-is-allowed)).

## State is derived, never stored

An album's state is computed from its files and its sidecar each time Harmonist
looks. There is no status field, no `incomplete` flag, no `needs_review`
boolean.

A stored status can disagree with the files it describes, and then something has
to decide which is right. Deriving it means there is nothing to disagree:
editing tags in Picard, copying a missing track in, or deleting a sidecar all
simply change the answer.

The same rule applies to anything persisted, not just state. A stored value must
be **load-bearing**: a decision or observation that can't be re-derived, with a
reader that changes what the user sees. And if the same fact is already recorded
somewhere more authoritative (usually the tags), it isn't stored again. See
[storage](storage.md#what-earns-a-place-in-the-sidecar).

## Never destroy user data

Harmonist writes only the tags it owns, and leaves every other tag exactly as it
found it (see [tagging](tagging.md#the-owned-set)). It never moves, renames or
reorganises folders. It never deletes audio except in Re-download, which first
zips the album and proves the zip good. Any image it replaces is kept first, and
if it can't be kept, the image isn't replaced.

## Every state has a way out

A user must never need to find, edit or delete a sidecar by hand. Each state is
left by a button, by editing tags (in Picard, say), or by changing files on
disk.

Staleness counts as a state. Anything shown from a stored answer, like a cached
MusicBrainz release, has to say how old it is and offer a way to ask again. A
config setting alone is not a way out.

## Spend MusicBrainz's budget carefully

MusicBrainz is a volunteer service that allows one request per second, shared by
everything Harmonist does. The number of requests a user action makes must be
bounded by a constant or by the size of what the user selected, never by the size
of the library. The background update check is off by default for the same
reason. See [external services](external-services.md).

## Transitions are idempotent

Running a sync, a reconcile or a tagging twice gives the same result as running
it once: no duplicate downloads, no second history entry for a change that
changed nothing, no sidecar that flips between two values. An unattended
background pass would otherwise turn any lack of idempotence into continuous
churn.

## Failures are loud

Nobody watches the log of a NAS. A failure that's swallowed and returned as an
empty result is indistinguishable from a genuine empty answer, so errors
propagate to somewhere that can tell the user. "I couldn't tell" and "there's
nothing" are different claims, and only the second may be shown as nothing. The
`error-handling` skill covers the mechanics.

## Usability is not secondary

The user is the fallback for everything automation can't settle, so the
decisions put to them must be clear, few, and made where the evidence is. The
`web-ui` skill carries the details.
