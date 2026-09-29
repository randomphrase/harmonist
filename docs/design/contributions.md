# Contributions and possible mismatches

A downloaded album carries evidence MusicBrainz may not have: the URL of the
store it was bought from, and often a barcode. Harmonist uses that evidence to
spot two things: that an album may be matched to the wrong release in its
release group, and that its release is missing data the user could add. Neither
writes anything, to the files or to MusicBrainz; both are suggestions.

## Which albums qualify

Only albums whose files demonstrably came from a download, because a CD rip's
evidence is about the CD. The evidence must be positive, and about the files
themselves:

- **Harmonist downloaded them** (recorded in the sidecar), or
- **a store left its mark on them**: a Bandcamp URL in the comment, or a tag only
  that store writes (see [tagging](tagging.md#tags-read-for-provenance)),
- and **nothing points elsewhere**: no file carries AccurateRip's verification,
  which only a rip produces, and no second store's mark. Evidence pointing to
  more than one [origin](tagging.md#the-origin) proves nothing.

Owning a Bandcamp purchase of the same album doesn't qualify a CD rip. Neither
does a `BARCODE` tag, which may have been written from a previous MusicBrainz
match. An original `UPC` tag, consistent across every file, is kept apart from
`BARCODE` and survives tagging and undo, because it is the barcode a download
was sold under. But it isn't proof of a download on its own: CD rippers write it
too (#632). An album spread over several folders has an original barcode only if
every folder agrees on it.

## Settle the match first

A contribution to the *wrong* release would be worse than none, so the match is
settled before any edit is suggested. An album is in at most one of the two
Library filters.

### Possible mismatch

An album goes into **Possible mismatch** when its evidence points to a different
release in the same release group than the one it's matched to: another release
has its URL or barcode, the matched release is physical, or its barcode differs
from the files' UPC. Each reason must be something the files have and the
matched release lacks. A URL both releases link isn't evidence against either.

Two cases are deliberately weaker:

- **Another page on the same Bandcamp store** may be the same product under a
  renamed page, or a different product the store also sells. One Bandcamp item
  can even back two releases once tracks are added to it. So it's shown as a
  reason to look, never as a match.
- **Private downloads** can still be checked for the right media, but their URLs
  are never offered as links or sent to Harmony, and the public release may be a
  different mix. Privacy is known only when Bandcamp says so; a URL's spelling
  proves nothing.

**Nothing is flagged until the whole release group has been compared.** A group
with more than 100 releases, or a release whose media MusicBrainz hasn't
specified, can't be compared in full, and an incomplete comparison makes no
finding at all, not even one it happened to see. A failed request is never
treated as "no results". Until a group has been compared, there's no reason to
doubt the match, so the filter fills in gradually as albums are opened or checked
in the background.

The user can switch to another release (after the usual review of tracks and
artwork), add the right release through Harmony, or say the current match is the
one they bought. That last decision is recorded against the *release*, so it
lapses by construction if the album is ever matched to something else, and
Harmonist then takes the user at their word: nothing further is claimed about
other releases.

### MB contributions

Once the match is settled, **MB contributions** lists what the matched release
is missing: first a barcode that disagrees with the files' UPC, then a missing
store link, then a missing barcode. One at a time, since fixing one may resolve
the next.

- A **disagreeing barcode** is shown as something to investigate, not a value to
  replace: barcodes are legitimately reused, and "no barcode" is sometimes
  MusicBrainz's correct answer. A physical release's barcode is never compared
  with a download's UPC.
- A **missing store link** is only suggested after a complete comparison of the
  group has found no other release linking that URL; otherwise the link may
  belong to that other release.

Harmonist opens MusicBrainz's release editor with the value ready to copy.
MusicBrainz's editor can only pre-fill links when *adding* a release, so the
user pastes it in.

## How the filters stay cheap

Placing albums in these filters can't cost a request per album per page view. So
the last comparison of each release group is stored, and the filters read that.
It's the one stored record of an *absence* ("no release links this URL") in
Harmonist, which is why it's never used to decide what to offer: the album page
always compares the group afresh before suggesting a switch or an edit. A stale
filter entry is corrected by opening the album.
