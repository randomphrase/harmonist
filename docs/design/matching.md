# Matching

Two questions sit under almost everything Harmonist does: which MusicBrainz
release is this album, and which of its files is which track. Both are answered
from evidence, and both hand anything uncertain to the user
([no guessing](principles.md#no-guessing)).

## Adopting an album

When Harmonist meets a folder without a sidecar, it looks for evidence of what
the album is, in order of strength:

1. **A MusicBrainz release ID in the tags**, written by Picard or a previous
   Harmonist. That's an identity, and the album is adopted as matched.
2. **A store URL in the comment tag.** Bandcamp writes the album's URL there.
   It's evidence the album was bought, not of which release it is, so the album
   goes to Needs MBID with the URL recorded, ready for a Store URL search.
3. **A barcode**, consistent across every file, such as the UPC that Qobuz
   embeds. It's searched on MusicBrainz, and a single result is offered as a
   suggestion.
4. Nothing: the album stays New, for a search by name.

**An adopted, already-matched album is only treated as a Bandcamp purchase when
both sides agree.** The files' comment must mention Bandcamp *and* the
MusicBrainz release must link to Bandcamp. Otherwise someone who owns a CD of an
album that happens to be sold on Bandcamp would find it waiting to be linked to
a purchase they never made.

**A comment URL is recorded even when it's only the artist's page** (`artist.bandcamp.com`
with no `/album/` path). It proves the album came from Bandcamp, and the sync can
link it by title later. Harmonist never fetches that page to find the album on it;
it doesn't invent the URL it doesn't have.

### Barcode search

A barcode search retrieves every exact barcode match. Artist and title in
store metadata often differ from MusicBrainz's, and
filtering on them would hide the right release or, worse, hide the ambiguity
that should stop an automatic choice. Search results retain every format: an
exact, unique match may be a CD, and the comparison shows it.

Barcodes are compared in their zero-padded 14-digit form, since the same code
has 8-, 12-, 13- and 14-digit spellings. The file's own spelling is kept for
links to Harmony, which doesn't accept the padded form for Qobuz.

A single result is **still only a suggestion**, even when every track length
agrees, because barcodes are reused between releases. A search that hit its result
limit can't prove anything is unique.

**Known Qobuz origin can narrow a small, complete set of barcode results.** If
exactly one release links to Qobuz, has only digital media and has the same number
of tracks as the files, it becomes a suggestion for review. The effective Origin
includes the owner's explicit choice. This combines exact barcode evidence with
positive source evidence; an absent Qobuz link is not proof that another edition
was never sold there. Duration still decides confidence, and nothing is tagged
automatically. Dismissing the suggestion leaves the ordinary search and manual
assignment tools available.

Without that unique compatible candidate, all barcode results remain choices.
Unknown or conflicting origin cannot narrow them. Only sets of at most five
results are enriched with release data, bounding the MusicBrainz cost; larger
sets remain manual choices. Initial adoption may reuse cached releases, while an
explicit lookup refreshes them.

## Match confidence

A release found by URL or barcode is exact as an *identity*, but the files on
disk may be a different variant of it: another master, a bonus track, a single
disc of a set. Before tagging automatically, the files are compared with the
release:

- **Exact**: the same number of tracks, and every track within four seconds of
  the release's length for that track. (The release-track length, not the
  recording's, which varies between releases.)
- **Approximate**: anything less. The release is stored as a suggestion and
  nothing is written.

A track MusicBrainz has no length for can't vote for exact, so an album whose
lengths are all unknown is approximate.

**Exact isn't enough to tag automatically: the winner must also be unique.** Two
releases in one release group with identical tracklists and lengths rank the
same, and choosing between them would be taking whichever MusicBrainz listed
first. Harmonist tags nothing and suggests nothing, since a suggestion would be
the same coin toss, and lists the releases for the user to choose.

Only a first, exact, unique match is tagged unasked. Everything else, including
every search the user runs, produces a suggestion to review.

**A lookup never replaces a suggestion awaiting the user** (#638). The search
tools are hidden while one waits, so a barcode or store-URL lookup arriving then
isn't one the user asked for: it is answered without asking MusicBrainz, and
checked again before writing, since a lookup takes seconds and another can land
meanwhile. A suggestion changes only when the user confirms or dismisses it.

### Length ranks candidates, but is never a finding

Track lengths decide confidence and rank competing releases. They are **not**
shown as something to fix on a matched album, because nothing Harmonist could do
would change them: tags don't store durations. A length that differs means the
files are a different master or MusicBrainz is wrong, and both remedies are
outside Harmonist. The album page shows MusicBrainz's lengths beside the files'
and explains that, rather than presenting a warning no button could clear.

## Which file is which track

One method answers this everywhere: the album page, the tagger, and match
confidence. It tries, in order:

1. the file's **release-track ID**, the only real identity. Harmonist and Picard
   both write it, so for any album either has tagged the answer is a lookup, and
   it survives MusicBrainz renumbering or reordering the release;
2. the **disc and track number**, used only where the number is unique among the
   files and among the release's tracks;
3. **file order**, for whatever is left.

The last two are guesses. The user can correct them with the track assignment
editor, and the release-track IDs written by that correction make the answer an
identity from then on.

**Duration is deliberately not a rung.** Pairing by similar length was how tracks
were matched until #232. Two tracks of the same length are ordinary, and the
failure is silent: one file in sixteen is given another track's title and IDs, on
an album that otherwise looks right and that nobody will re-check.

### Reviewing track assignments

When the pairing is uncertain, or MusicBrainz has changed the release's
tracklist, the user reviews it before anything is written.

- **The draft lives only in the page.** Nothing about a pairing is stored: once
  confirmed, the release-track IDs written into the files *are* the record, and
  every later comparison starts from them.
- **Proposals are constrained.** Beyond the IDs, the editor proposes a pairing
  only from a recording ID that identifies one remaining track, or a disc and
  track number that is unique on both sides, or a single file left opposite a
  single track. Anything ambiguous stays a gap.
- **A merged release is read through its recording IDs.** When MusicBrainz folds
  one release into another it issues new release-track IDs, so the old ones name
  tracks that no longer exist, but recordings usually survive (#602). This
  applies only when the file's own release ID is evidence of the merge; a
  release-track ID that vanishes from the *same* release is a conflict.
- **Files may be left unassigned.** They get only the album's release ID, and keep
  all their other tags. A file that already carries a track ID can't be left
  unassigned, because its tags would then claim a track it isn't paired with.
- **A changed tracklist always goes through review.** Additions, removals,
  reordering or changed totals open the editor before an update can be applied,
  because the layout is evidence of the release's identity.

## When the counts differ

A tagging won't write a release's tags onto fewer files than the release has,
because that's how a half-finished download would be tagged as the whole album.
The user can choose otherwise:

- **Confirming a suggestion with tracks missing**, or **re-tagging as incomplete**
  after MusicBrainz added tracks, writes the release's totals into the files the
  user has, and the album derives Incomplete from them. Nothing else is stored.
- **More files than tracks** is only possible through an explicit assignment
  review, which says which files are left over.

The count check itself is never skipped, even when the user has already named the
release. Whether a tagging makes sense isn't something an assertion can make
true.
