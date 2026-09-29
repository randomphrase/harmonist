# Bandcamp

Harmonist downloads Bandcamp purchases through
[bandcampsync](https://github.com/meeb/bandcampsync), extending it rather than
reimplementing it. Most of the design here is about the opposite problem:
**not** downloading an album the user already has, and linking each purchase to
the right album on disk without guessing.

## A first sync must never re-download a library

A user adopting an existing library already has most of their purchases on disk,
under folder names and tags bandcampsync knows nothing about. A naive first sync
would download all of them again.

So while any album is waiting to be linked to its purchase (Needs Linking), a
sync runs **link-only**: it connects purchases to albums on disk and downloads
nothing. Only when nothing is left to link do syncs download new purchases.

Two more rules keep downloads deliberate:

- **A purchase that can't be confidently tied to an album is never downloaded on
  a guess.** It becomes a *potential download*, and the user decides: download
  it, link it to an album they have, or set it aside. A matching gap costs a
  click, never a duplicate.
- **Each sync downloads at most a set number of new purchases.** A large backlog
  arrives over several syncs rather than filling a disk unattended. Purchases the
  user explicitly approved, and re-downloads, aren't counted.

## Linking a purchase to an album

A purchase is tied to an album on disk by the strongest evidence available:

1. the **purchase ID** already in the album's sidecar;
2. the album's **store URL**, exactly;
3. the URL's **slug**, the `/album/<slug>` part, on the same Bandcamp page.

On a match, the album records the purchase and adopts the purchase's URL as its
store URL, since that's where the user actually bought it. The purchase is added
to `ignores.txt`, so no later sync downloads it.

### A slug identifies a release only within one page

Within one artist's or label's Bandcamp page, a slug is minted once and doesn't
change when the album is retitled, which makes it a safe key there. Across pages
it isn't an identity: Zero 7's *Home* and The Gathering's *Home* are different
records, both at `/album/home`. Treating the slug as global made owning one
silently skip the other's purchase (#425).

But the same release is often sold from both a label's page and the artist's, and
refusing those matches would re-download albums already on disk. So a same-slug
album on a *different* page is only a candidate, **confirmed by MusicBrainz**: if
the album's release links the purchase's URL, it's the same release. If it
doesn't, or MusicBrainz can't be asked, neither answer is taken: the purchase
isn't downloaded, isn't marked as downloaded, and becomes a potential download
for the user to decide.

### Purchases already downloaded

bandcampsync never shows Harmonist a purchase that's in `ignores.txt`, and in an
adopted library that's nearly all of them. So each sync starts with a separate
pass that links albums waiting in Needs Linking to purchases that are already
marked as downloaded. It works in two phases:

1. **By slug.** One album and one purchase sharing a slug are linked. When
   several releases share a page (a standard and a deluxe edition, say), they're
   separated by an exact title match between the album's tagged title and the
   purchase's title. Folder names are ignored; they're the user's own naming.
2. **By title alone**, for albums phase 1 couldn't place. Sometimes MusicBrainz
   records one public page for several releases while each purchase carries its
   own URL, so the album's URL matches no purchase. A unique exact title match
   links it, and the Activity feed is told the URLs disagree, since that could
   also mean the album is tagged as the wrong release.

A purchase already linked to one album is never linked to a second. When the
title can't separate several purchases, all of them are recorded on the album as
candidates rather than one being picked. That takes the album out of Needs
Linking, because it's as resolved as it can be without comparing tracklists.

MusicBrainz releases often list several Bandcamp URLs, such as the artist's page
and the label's. An album still unlinked after the passes above is checked
against every Bandcamp URL its release lists, linking only if exactly one
purchase matches. That costs one MusicBrainz request per unlinked album, which
is bounded by how many failed, not by the library's size.

### Why title matching is allowed

An earlier design forbade any matching on artist or title, and a fuzzy search of
all of MusicBrainz by title is still forbidden. What makes title matching
acceptable here is that it meets all three rules of
[no guessing](principles.md#no-guessing):

- **scoped** to purchases the user provably owns against albums on their disk;
- **exact**, after normalising case and punctuation, so a near miss falls through
  instead of linking wrongly;
- **unique**, or nothing is linked.

The edition qualifiers that fuzzy matching would erase, like "(deluxe edition)"
or "[LP version]", are exactly what makes an exact match tell releases apart.

## A full sync, and surrender

bandcampsync stops paging at the last purchase it saw, so an old purchase is
never revisited. If any album is waiting to be linked when a sync starts, that
sync pages the whole collection instead. It's self-limiting: a full sync either
links every waiting album or gives up on it.

**Giving up (surrender) happens only on a full sync.** On a partial one, "no
purchase found" may only mean the purchase wasn't paged this time.

An album a full sync can't link goes back to **Needs MBID**, with its current
release kept as a read-only suggestion and a note saying no purchase was found.
It isn't quietly moved to the Library, because the failure means one of three
things:

1. it wasn't bought on Bandcamp at all (a CD rip, a gift, a free download);
2. its store URL and title are both wrong, which may mean the tags are wrong;
3. it's tagged as the wrong release, in a way no check could prove.

Only the first is harmless, and Harmonist can't tell them apart. Moving it to the
Library would bury the other two where nobody would look again. Surrender only
rewrites the sidecar; the files keep their tags. **Move to Library** records that
there's no purchase, so the album isn't surrendered again.

A known limitation: surrender can't tell a release the user chose by hand from
one Harmonist derived, so a hand-matched album whose purchase can't be found also
comes back to the Inbox, costing one click.

## Re-download

Re-download fetches a purchase again, for a better format or for tracks the
artist has added since.

**The files have to go.** Three separate mechanisms stop a sync fetching a
purchase, and all three depend on the album being on disk: its purchase ID in
the library, its store URL in the library, and a marker file bandcampsync leaves
in the folder. There's no way to re-download while keeping the files in place.

**So the order is the safety argument:**

1. zip every folder of the album into the top of the music folder, stored
   uncompressed, since audio is already compressed;
2. reopen the zip and check every file in it against the originals;
3. only then delete the folders;
4. remove the purchase from `ignores.txt`, approve the download, and start a
   sync.

A failure anywhere before step 3 leaves the album untouched. The zip is the way
back: unzipping it restores the album exactly, sidecar and all. Nothing ever
deletes it.

**The replacement keeps the release.** Re-downloading says the files are wrong,
not the match, so the new copy is tagged as the same release rather than looked
up again, which could land on a different release or none. This has Confirm's
authority, so match confidence is skipped, but the track-count check isn't.

**An incomplete album may come back incomplete.** It was re-downloaded to try for
the missing tracks, and if they still aren't there, that's the album it already
was. A *complete* album coming back short is a bad download, and isn't accepted.
If the replacement can't be tagged as the carried release (it has more tracks
than the release, a complete album came back short, the release has been deleted,
or a restart lost the carried release), it falls back to what a first-time
download does, and lands in Needs MBID. So an album that was complete can come
back needing a click; the zip is what makes that recoverable.

History spans the round trip: tagging the replacement as the same release links
its new ID to the old one, so the album page shows the archive and the download
together.

Re-download is offered only for an album linked to exactly one purchase.
Fetching one of several candidates would be a guess.
