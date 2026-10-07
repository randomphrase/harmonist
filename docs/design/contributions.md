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

Owning a Bandcamp purchase of the same album doesn't qualify a CD rip as a
download. Neither does a `BARCODE` tag, which may have been written from a
previous MusicBrainz match. An original `UPC` tag, consistent across every file,
is kept apart from `BARCODE` and survives tagging and undo, because it is the
barcode a download was sold under. But it isn't proof of a download on its own:
CD rippers write it too (#632). An album spread over several folders has an
original barcode only if every folder agrees on it.

The owner's explicit Origin choice supersedes this inference (#694), including
Harmonist's download record: the audio may have been replaced while its old tags
survived. Display and checks share the chosen origin. It supplies no missing
barcode, disc ID or URL, and a Bandcamp link inherited by files now identified as
another source is not evidence to contribute to MusicBrainz.

### CD rips

A **CD rip whose ripper wrote a UPC** qualifies too (#633), for the match half
only. The UPC is the one thing a rip carries about which release it is, so it
gets the same check a download's does, turned round where the two differ:

- Its candidates are the group's **physical** releases, not its digital ones,
  and the review that switches the match accepts the same.
- The media reason reverses: a download matched to a CD is suspect, and so is a
  CD rip matched to a digital release.
- Its UPC is **never a contribution**. It came from the ripper's metadata
  provider, not off the disc, so it's evidence about the match and nothing to
  offer MusicBrainz. And there's no **Add Release** through Harmony, which finds
  releases in digital stores.

A rip with no UPC has nothing to check. A disc ID would be a second kind of
evidence, but a table of contents in the tags may have been synthesised by a
transcoder, so only one recorded by the ripper itself would count.

## Settle the match first

A contribution to the *wrong* release would be worse than none, so the match is
settled before any edit is suggested. An album is in at most one of the two
Library filters.

### Possible mismatch

An album goes into **Possible mismatch** when its evidence points to a different
release in the same release group than the one it's matched to: another release
has its URL or barcode, the matched release's media don't fit the files
(physical for a download, digital for a CD rip), or its barcode differs from the
files' UPC. Each reason must be something the files have and the
matched release lacks. A URL both releases link isn't evidence against either.

Two cases are deliberately weaker:

- **Another page on the same Bandcamp store** may be the same product under a
  renamed page, or a different product the store also sells. One Bandcamp item
  can even back two releases once tracks are added to it. So it's shown as a
  reason to look, never as a match — and not at all when another release links
  the exact URL, which already settles it. Offering the weaker evidence beside
  the stronger only equivocates (#652).
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
  belong to that other release. Accepting the current match does not override
  this: a store can remove tracks or replace a download while keeping its URL,
  so an older purchase can correctly match a different release from the one
  sold there today. Other contributions remain available independently.

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
