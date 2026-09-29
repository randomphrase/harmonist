# Artwork

Harmonist makes sure an album has local artwork, so it doesn't depend on a
player's own online lookup. An album's art lives in two places at once, embedded
in each track and as a `cover.jpg` (or `cover.png`) in the folder, and players
differ in which they read. So artwork is handled as a whole: every image the
album carries, and every place it could go.

## One plan decides everything

A single **artwork plan** is built from what the album carries (each track's
embedded image, the folder cover) and the Cover Art Archive's candidate. It names
every write needed to put the winning image in place: the target, what it holds
now, and what it will hold.

The album page draws its Artwork section from the plan, and every action that
writes artwork carries it out. Nothing decides twice. The page used to carry its
own copy of the size rule, and the two drifted: the page promised to replace
images the writer then only filled in (#469).

The unit on the page is the **distinct image**, not the file: twelve tracks and a
folder cover showing one picture are one row, because that's one thing to look
at.

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

**Different images on different tracks are never replaced.** That's what a
compilation correctly looks like.

**The user can choose any image** regardless of size (#472). A larger scan can be
softer, badly cropped, or another pressing's sleeve, and only a person looking at
both can tell. Choosing only redraws the section with that image as the winner;
nothing is written until the user applies it.

## Additions and replacements

Every write is one of two kinds, and the difference is whether anything is lost:

- an **addition** puts an image where there was none: a track with no art, or a
  folder cover the album lacks;
- a **replacement** overwrites an existing image.

**A tagging nobody reviewed makes additions only** (#418). Replacements happen
only through an action where the user has seen what will be replaced: applying
updates, applying artwork, or confirming a release with artwork included.

**A missing folder cover is created only if the user turns that on**, and it
ships off. The two mistakes don't cost the same. An album without the file still
plays correctly everywhere, since Plex and Navidrome read embedded art first. The
other way round writes megabytes into thousands of folders, and when it was the
default it made hundreds of albums look like they had an update whose only content
was the missing file. The setting governs creation only; an existing folder cover
is weighed like any other image either way.

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
