# Artwork

Harmonist makes sure an album has local artwork, so it doesn't depend on a
player's own online lookup. An album's art lives in two places at once, embedded
in each track and as a `cover.jpg` (or `cover.png`) in the folder, and players
differ in which they read. So artwork is handled as a whole: every image the
album carries, and every place it could go.

## One plan decides everything

A single **artwork plan** is built from what the album carries (each track's
embedded image, the folder cover), the Cover Art Archive's candidate, and any
choices the user made over it. It names every write needed to put the winning
image in place: the target, what it holds now, and what it will hold.

The album page draws its Artwork section from the plan, and every action that
writes artwork carries it out. Nothing decides twice. The page used to carry its
own copy of the size rule, and the two drifted: the page promised to replace
images the writer then only filled in (#469).

The unit on the page is the **distinct image** carried by tracks, not the file:
twelve tracks showing one picture are one row, because that's one thing to look
at. The folder cover is always a row of its own, even when it shows the same
picture, and even when the file doesn't exist (#659). It used to share the
tracks' row while their fates agreed (#400), which saved drawing one picture
twice at the price of rows that merged and split as the plan changed. A row is
what a choice is made on, and one that split the moment the user chose for it
was not something they could choose on.

## Which image wins

**The largest image wins**: strictly larger in both dimensions, with ties going
to what's already there. An image that can't be measured never displaces
anything, though any image beats none. Image sizes are read from the first bytes
of the file rather than with an image library, which would be a new dependency
for two numbers.

**The size rule decides the folder cover only.** A folder cover larger than the
embedded art is a layout someone chose, not a gap: one large file costs a few
megabytes, while the same image embedded in twenty tracks costs twenty times
that, so a library using lossy formats sensibly keeps modest embedded art beside
a high-resolution cover. A bigger folder cover therefore proposes nothing for the
tracks (#479). Tracks with *no* art are still filled, and from the album's own
image where it has one, so the filled track matches its neighbours.

**Different images on different tracks are never replaced by the size rule.**
That's what a compilation correctly looks like.

**The user can choose any image, row by row,** regardless of size (#472, #659).
A larger scan can be softer, badly cropped, or another pressing's sleeve, and
only a person looking at both can tell. The choice is made on a row, because the
row is the thing looked at, and reaches that row's carriers only: every other row
keeps the size rule's suggestion, and a suggestion the user doesn't want can be
dropped the same way. Choosing is therefore also the consent the per-track
protection waits for, scoped to exactly the sleeves that were looked at.
Choosing only redraws the section; nothing is written until the user applies it.

So the plan holds an image per write rather than one for the tracks and one for
the folder cover. The size rule never needs more than those two, but a user
choosing row by row can put a different image on every row, and a compilation's
rows are a dozen of them.

**Any image the archive has is a candidate, not only the front it would pick.**
The picker offers every image the archive lists for the release, then for its
release group (#659). The front the size rule weighs is one archive image among
many, and the image a user wants is often another: a different scan, or the
group's cover when this release has none. A back cover or a booklet page is
rarely wanted, so **Front only** narrows the carousel, ticked unless the archive
lists no fronts. It is a property of what's being looked at, not a preference:
Picard's equivalent is a setting that applies to every album, and a setting
would hide the one back cover a user came looking for. Matrix/runout,
raw/unedited and watermarked images are never offered, even when also marked
Front, because none of them is artwork anyone puts on a sleeve.

**Past the album's own images, the carousel goes on to the group's other
releases**, one at a time. The sleeve a user wants is often another pressing's,
and most of all when this release has none. Looking costs MusicBrainz one browse
of the group and the archive one listing per release, so it happens only when the
user steps there: the next release is listed as they arrive at the last image,
which keeps a step from waiting on two services. The browse is made live, once
per visit to the page, rather than read from the page another screen stored: that
page can predate a release, and a picker that trusted it would never offer one
added since. A release MusicBrainz says has no artwork isn't listed at all.
Another release's listing is kept only for the archive check's TTL, since nothing
else refreshes it; after that it drops out and the next step lists it again. An
image chosen from it stays chosen, because its original was fetched under its id
when it was chosen.

The carousel shows the archive's thumbnails, fetched through Harmonist like
every other image on the page. The original is fetched only when the user
presses Use, since that's when the plan needs its bytes. Its size, type and
weight are what a choice is weighed by, though, so each image is measured the
first time the carousel shows it, off the original's first 64 KB as the archive
check measures its front. That's one small request per image someone looks at,
never per image listed. An archive image id names one upload for good, so a
measurement never needs repeating. A request names an
image by the id the archive's listing gave it, never by URL, so nothing a page
sends can point a fetch anywhere the archive didn't.

**A choice that changes nothing says so.** Choosing an image a row already
carries, byte for byte, is a no-op, and once the original is in hand the row
says **Unchanged** where the incoming image would go, and Use becomes a ✓, rather
than offering a press that writes nothing. Offering it anyway once left an album
with a preview, no After Apply column and no button, which read as broken. Until
the original is fetched, only the thumbnail is known, and a thumbnail's bytes
say nothing about the original's.

## A stand-in gives way

When a release has no front of its own, the artwork Harmonist writes can come
from another listing: the release group's front, which a tagging falls back to
(#434), or one the user picked from the group or another release. That image is
a **stand-in**: the release's own cover would have been used if it existed. If
someone later uploads one, it should replace the stand-in whatever its size,
because it's this release's cover. The size rule would otherwise keep a borrowed
3000px image over the release's new 1200px front for ever (#663).

What can't be worked out later is *why* the album carries another listing's
image. Borrowed for want of a front and chosen over an existing one look
identical on disk, and only the first should give way. So the stand-in is
recorded in the sidecar when it's written, and only then: an image chosen while
the release had a front of its own is a choice, and isn't offered back. The
record is kept only while the album carries the image and is matched to the
release it stood in for, so choosing something else, or rematching, lapses it.

The album page reads it. A row carrying a stand-in says where it was borrowed
from, and once the page's archive check finds the release has a front of its
own, those rows take it as though it had been chosen for them, so the row's ×
can still keep the stand-in. The check downloads that front however it measures,
since it wins regardless. If the front turns out to be the stand-in itself, as it
can when the group's front was this release's all along, nothing was borrowed:
the check clears the record, and the album's History says so. The background update check doesn't read the record
yet: it doesn't look at artwork at all, which is #269's to change.

## Additions and replacements

Every write is one of two kinds, and the difference is whether anything is lost:

- an **addition** puts an image where there was none: a track with no art, or a
  folder cover the album lacks;
- a **replacement** overwrites an existing image.

**A tagging nobody reviewed makes additions only** (#418). Replacements happen
only through an action where the user has seen what will be replaced: applying
updates, applying artwork, or confirming a release with artwork included.

**A missing folder cover is suggested only if the user turns that on**, and it
ships off. The two mistakes don't cost the same. An album without the file still
plays correctly everywhere, since Plex and Navidrome read embedded art first. The
other way round writes megabytes into thousands of folders, and when it was the
default it made hundreds of albums look like they had an update whose only content
was the missing file. The setting governs suggesting the file only: an existing
folder cover is weighed like any other image either way, and a user who chooses an
image for the missing one has asked for it, which is a different thing from three
hundred albums being told they have an update (#659).

A compilation's folder cover comes only from the archive, since its first track's
sleeve isn't the album's cover.

## Writing only what was shown

The page carries a fingerprint of the plan it showed, scoped to what the control
does (the whole plan, or only its additions). The action rebuilds the plan from
disk and writes only if it still matches. If it doesn't, applying artwork writes
nothing and redraws, while a re-tag writes its tags without the artwork and says
why in the album's history. Each target is also re-read just before it's written,
and one that changed in between is left alone.

**Only one image per file is touched.** Every format can hold several pictures,
such as a back cover or booklet pages. Harmonist reads and writes only the first,
the cover, and leaves the others where they are (#489). Removing them would
destroy the only copy of an image while reporting that one file changed.

**Artwork can't block tags.** Tags are written before the artwork is prepared. If
an image can't be read, the tags still land and the artwork is left alone and
reported. An unreadable image is never treated as a missing one (#511).

## A backup is what permits a replacement

Any image a change is about to replace is kept first, so the change can be
undone. The copies are files in the config folder, named by the image's digest:
the tracks of one album usually share one image, so an eight-track album costs
one file, and the digest already recorded in the album's history is the lookup
key.

**No backup, no replacement** (#470). Every image an operation will replace is
kept first, all together, and a target whose image couldn't be kept is left as it
is. Keeping them one at a time let the size cap evict the first backup while the
second was being kept, after the first original had already been overwritten.
The rest of the work still happens: tags are written and additions are made.

**The promise is per album**: the last five artwork changes to any album can be
undone, however busy the rest of the library is (#408). A byte cap (500 MB)
underneath is only a backstop. A global cap alone would let an overnight pass over
five hundred albums evict the backup behind an Undo that another album was still
offering. What's protected is computed from the same history records the page
offers Undo for, so the two can't disagree. The album page checks that a backup
exists before offering Undo, since a button that fails is worse than none.

An addition needs no backup: undoing it just removes the image again, so it stays
undoable however full the store gets.
