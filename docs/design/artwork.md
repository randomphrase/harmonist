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

**Except where neither reason holds: an album with no folder cover, where
embedding costs little** (#872). With no `cover.*` there is no high-resolution
file beside the tracks for anyone to have chosen. The album just has a small
image. So there, the archive's front replaces the tracks' image when it is larger
by the size rule and cheap to embed. It's a replacement like any other: nothing
writes it unattended, and it's backed up first.

What counts as cheap depends on the audio. Beside lossless audio, a larger
embedded image is a rounding error: a 1200px front in every track of an ALAC
album adds about 2%. So a lossless album takes it whatever it weighs. On lossy
audio the cost is real but measurable, and the front goes in while it grows the
album's files by at most 10%: about 800 KB per four-minute track at 256 kbps,
enough for a typical 1200px front and not for most 3000px scans. The cap is
measured against the files' size on disk, which the scan already has, rather
than against the audio stream alone, which would mean reading every file. An
album whose size isn't known isn't shown to grow little. The archive's byte size
is known only once the original is fetched, so the background check fetches any
larger front such an album could take, and the plan weighs the bytes.

A lossless album that has a folder cover keeps #479's layout, since that is the
reason left standing there. A setting was rejected for #479's reason, a rule a
user can hold in their head.

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
presses Use, since that's when the plan needs its bytes, or looks at it full
size, since that's asking to see it. Its size, type and
weight are what a choice is weighed by, though, so each image is measured the
first time the carousel shows it, off the original's first 64 KB as the archive
check measures its front. That's one small request per image someone looks at,
never per image listed. An archive image id names one upload for good, so a
measurement never needs repeating. A request names an
image by the id the archive's listing gave it, never by URL, so nothing a page
sends can point a fetch anywhere the archive didn't.

**Full size is the picker, maximized** (#918). The choice is nearly always
between two renderings of one cover, a small embedded copy against a large scan,
and one image at a time left the comparison to memory. So the row's image and
the archive's are side by side, drawn at one size so a spot on one lines up with
the same spot on the other, and at 100% the smaller is scaled up to match, which
is where its softness shows. It is the picker's own panel in the top layer
rather than a viewer of its own: the controls are the page's, so nothing is kept
in step between two copies, and putting it back leaves the picker where the user
moved it. A separate viewer remains for images with nothing to compare against.

**A choice that changes nothing says so.** Choosing an image a row already
carries, byte for byte, is a no-op, and once the original is in hand the row
says **Unchanged** where the incoming image would go, and Use becomes a ✓, rather
than offering a press that writes nothing. Offering it anyway once left an album
with a preview, no After Apply column and no button, which read as broken. Until
the original is fetched, only the thumbnail is known, and a thumbnail's bytes
say nothing about the original's.

## An image over the size limit is written squished

**The largest image still wins, but what's written may not be the picked image
byte for byte.** An image over the **image size limit**, 20,000,000 bytes, is
re-encoded to fit as it's written, into the tracks or the folder (#888). The
limit is Navidrome's default `MaxImageSize`, the largest image a player is known
to read. Past it, Navidrome refuses the image and keeps whatever art it held
before.

Refusing such an image was the alternative, and it would cost the best source
there is. The archive's originals are sometimes scans saved at JPEG quality 100,
far over any limit for no visible gain: the 34 MB cover that prompted this came
out at 16 MB at quality 95, with an SSIM of 0.990 against the original.

**Squishing does the least that fits.** Quality comes first, at the same pixel
dimensions; the dimensions shrink only once quality has run out. Keeping them
keeps the size rule settled too, since a tie goes to the image already there.
The result is always a JPEG. A PNG over the limit can only shrink by losing what
made it a PNG.

**A folder cover is named for what it holds** (#905): `cover.jpg` for a JPEG,
`cover.png` for a PNG, decided by the image as it's written, so a squished PNG
is a `cover.jpg`. The type comes from the image's own first bytes, not from a
tag's label or a file's extension, which are claims another tool may have got
wrong. A replacement of the other type renames the file. Keeping the
name was the alternative, and it left a `cover.png` holding a JPEG, which every
reader that goes by the extension then misdescribes. The rename never lands on
another `cover.*` already beside it: `cover.jpg` is the one read when both
exist, so a `cover.png` there is a file the user was never shown, and the cover
is written in place instead.

**A cover already under the other type's name is offered the rename on its
own** (#907), its image as it is. Nobody looks for a `cover.jpg` that's really
a PNG, so Harmonist does. The file is moved rather than rewritten, so it keeps
everything about it but its name.

**The record carries both names, and Undo puts the old one back.** The record
names the file as it is afterwards, which is where Undo finds it, and a rename
adds the name it had. Undo then refuses if something has taken the old name
since, for the reason the rename never lands on one.

**A copy counts as its original.** The copy is a different file from the image
that was picked, and everything that asks whether an album carries an image
would otherwise answer no: the size rule and a choice (a copy shrunk to fit is
smaller than its original, so it would be offered that original on every pass),
a replaced front ([below](#a-replaced-front-gives-way-too)), a stand-in, and a
watch. Which image a copy was made from is knowable only at the write, so it's
recorded then, in `activity.db` rather than the sidecar: it's Harmonist's record
of its own act, as the old front is (see [storage](storage.md)). The records of
each file still name the copy, since it's what the file holds and what Undo
checks for; a stand-in names it too, as the album carries it.

If that record is lost, a copy reads as an image of its own. It has the same
dimensions as its original, or smaller only where quality ran out, so the size
rule mostly keeps it; what's lost is recognising it as a replaced front or an
honoured choice. An image that can't be decoded is written as it is, which
loses nothing the album had.

The After Apply column says the image will be **reduced to fit** rather than
showing the copy's size, which would mean encoding the image to draw the page.

**An image the album already has over the limit is offered squished** (#893).
Holding the wanted image byte for byte normally means nothing to write, but
holding the original over the limit doesn't: keeping it means writing it again,
squished, so the plan makes it a Replacement of each carrier with the image it
already holds. That's a change like any other: found by the Library from the
scan alone, routed by the user's threshold, never written unattended, and
undone from the backup every replacement keeps. The alternative was an action
on the album page alone, which would leave each such album to be found by hand,
when the scan already knows every image's length and a player is silently
refusing the image meanwhile. A compilation's folder cover is offered too, since
it isn't one of the sleeves; the row's Keep declines it, as it declines any
change.

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
the check clears the record, and the album's History says so. The background artwork
check reads the same record and proposes the same replacement; it leaves the
record and music untouched until the user reviews the album.

## A replaced front gives way too

The archive's front is sometimes wrong: another album's cover uploaded to the
wrong release, written faithfully into every track. When an editor removes it
or demotes it, the album still carries it, and the size rule would keep it
over a smaller successor for good. A change of front is a correction, not an
upgrade, so whatever the album carries of the old front gives way to the front
now, whatever either measures (#886).

This is the stand-in rule reached from the other side, and it needs nothing
recorded in the sidecar. A stand-in's reason can't be worked out later, but
this one can: Harmonist writes an archive image only from the listing it was
asked about, so an album carrying the release's old front got it from there.
What's needed is the old front itself, which the archive no longer names. The
archive check keeps it beside the stored answer when a refresh finds the front
changed, and carries it forward on later checks until the front changes again.
It is identified by digest, and the cached image is hashed just before a
refresh discards it. A stored answer from before digests were kept, whose
image has already been evicted, falls back to byte length and pixel size
together. Only the same file is likely to share both, and that is enough to
offer a replacement that is never written unasked.

The successor is whatever front the archive check finds: the release's own
first, and the release group's once the release has none. The group's front
is MusicBrainz's choice among the group's releases, so the background check
doesn't browse those releases itself. The album page's picker still steps
through them. Because the check is sized by what the album carried before it
learnt of the change, it downloads a smaller successor straight afterwards
rather than waiting for the next check. Routing is a replacement's like any
other ([below](#where-an-artwork-update-waits)): never written unattended, and
**Don't suggest this image** or keeping the row turns the offer down.

## Finding changes without opening an album

Artwork has its own background check under the same opt-in as tag updates.
Its clock belongs to the CAA observation, because a new cover need not change
MusicBrainz's tag payload and visiting a tag comparison must not postpone an
artwork check. Its worker is separate so the archive's long responses cannot
hold up tag discovery. Work is divided into small oldest-first slices with a
time budget; failed attempts back off without replacing the last observation.

A failure delays the worker across releases, because a delay for one release
alone merely sends an outage to the next album. Successful checks restore the
normal pace. Failures and the recovery that follows them go to the container
log, with every attempt's traceback, and never to Activity. Nothing is lost,
the worker never gives up, and an archive outage offers the user nothing to do.
Summarising failures in the feed was tried: under an archive that failed
intermittently for days, the summaries crowded out everything else there.
Backoff belongs to this worker, so an explicit check on the album page remains
available.

The scan keeps compact descriptions of every audio track's image and the folder
cover, never their bytes. The Library applies the shared plan to those facts and
the archive observation held in memory, so filtering spends no requests and
opens no audio or image files. Restart rebuilds archive observations from the
durable cache; local changes replace the scan's facts, and settings are read when
the plan is judged. There is no persisted artwork status to invalidate.

One CAA check serves every local copy of a release, but each copy has its own
plan. The download threshold considers every copy's folder cover, rather than
the largest embedded image: a large embedded image must not hide a candidate
that could improve a smaller folder cover. Embedded art counts only on an album
with no folder cover, the one album whose tracks a larger front can replace.
Stand-ins and empty albums can use
any image, including one whose dimensions cannot be read.

The Library separates local observations from proposed writes, so a fact about
what the files carry can be combined with a change available to them. No artwork
can overlap Addition, while a plan with any replacement belongs only to
Replacement. Missing embedded images and different images across tracks are
neutral observations: the latter may be intentional, and neither implies that
an update is available. Unreadable images are not evidence of absence. Both tag
and artwork update counts describe albums, not fields or images to be written. A
tag-update ignore is about a MusicBrainz version and cannot suppress artwork
whose source changes independently. Background proposals require review; source
preferences and automatic writes remain separate decisions.

## Where an artwork update waits

**The same threshold as a tag update's, on a scale of its own** (#852). An
artwork update is ranked by what it does to the album's images, not by how far
it reaches, so it can't share the tag levels. Addition comes before
Replacement, because only a replacement loses anything. The Inbox takes the
chosen operation and everything after it, so a replacement can never be
handled less carefully than an addition.

**The Library by default.** That is where every artwork update waited before
there was a choice. Nothing about artwork is urgent: an album without it still
plays everywhere, and its proposal is one filter away.

**An album is judged by its whole plan.** One that replaces anything is a
replacement, whatever it also adds, as a tag update takes the label of its
furthest-reaching change. Splitting the album by operation would send its
additions one way and its replacements another, and the card would no longer
describe what applying the album's page writes.

**A watched album always waits in the Inbox.** "Better artwork wanted" was a
request to be told when something turns up, so the threshold doesn't apply to it.

**Turning down an image is the existing rejection, on the album page.** There
is no artwork equivalent of ignoring a tag update. That ignore waits for a
MusicBrainz version to change, and the archive has no version to wait on.
**Don't suggest this image** already names the upload and survives later
checks. The Inbox card doesn't offer it: the card shows one change of the
update to decide whether to take it, and refining what is taken (another image,
a row kept, an image turned down) is the album page's, which shows every row and
lists the release's images.

**Only what the Inbox takes is announced**, and only when the check first sends
it there, as for tag updates: an update left to the Library filter is one the
user chose not to be told about, and one about to be applied records itself.

## Applying artwork without asking

**Additions only** (#853). An addition loses nothing, so it needs no backup and
undoing it just removes the image again. A replacement overwrites something the
user may have chosen, and "bigger" isn't "better" to someone who picked the
cover they have. So the apply threshold stops at Addition, as the tag one stops
short of Structure, and nothing written with nobody watching overwrites an
image.

**The whole plan or nothing.** An album whose plan would also replace anything
waits for a person, additions and all, for the reason it is ranked as a
replacement: half an update applied unattended would leave the card describing
something the files no longer match.

**Never a watched album.** "Better artwork wanted" asked to be shown what turns
up, so what it finds waits in the Inbox even when it only adds.

**Written from the cache, judged against the files as they are now.** The
archive's image comes from the local cache, as on the album page, so applying
spends no requests. The plan is rebuilt from the files and the folder cover at
write time, because the scan that routed the album can be older than both: an
update that has grown into a replacement since is held for a person, and only
additions are written.

**What can't be written waits in the Inbox, with the reason**, kept in memory
against the plan it was held at, so a failure isn't retried every ten minutes,
and is retried once the files or the archive's answer change what it would
write.

## Applying a selection

Artwork cards in the Inbox can be ticked and applied together (#859), as tag
updates can. It is the album page's Apply for several albums at once, with the
differences that follow from someone having looked at a card rather than every
row.

**The card is the review, so its plan travels with it.** Each card carries the
fingerprint of the plan it showed, and the plan is rebuilt from the files and
folder cover at write time. An album whose plan has moved since is left alone,
as a reviewed confirmation refuses a release that changed under its review.

**Replacements and watched albums can be selected.** The unattended pass holds
them back because nobody has looked; here someone has. A replacement still
needs its backup first, as on the album page, and a watch ends once everything
planned is written.

**A failure is reported, not held.** Nothing retries a selection, so the reason
goes to Activity and the card stays, unticked, to be looked at again.

## Watching for alternatives

“Better artwork wanted” is an explicit request to watch the matched release and
its release group, even with the general background check switched off. It does
not browse sibling releases. A user borrowing a sibling's cover can therefore
wait for their own release to get one, without claiming that the current match
or image is wrong. It also works for inherited covers with no stand-in record.

A watch broadens the ordinary artwork proposal to include smaller front images,
including existing alternatives. The Library's Artwork updates filter and the
album's After Apply preview use the same plan. A group's copy of the same upload
is considered only once, and distinct per-track sleeves remain protected.
What it finds waits in the Inbox like any other artwork update
([above](#where-an-artwork-update-waits)), not in a parallel workflow.

“Don't suggest this image” excludes an upload from automatic proposals while
keeping it in the picker. It is reversible and does not prevent an explicit Use.
The decision names the upload, rather than a URL or resolution, so the same
image arriving through another listing does not bypass it. Rejection survives
turning the watch off. Applying artwork successfully ends the watch; a failed
apply leaves it on. Rematching lapses it because it names the old release.

Only these decisions go in the sidecar. Listings and originals use the existing
cache, and Library filtering reads warmed descriptions without disk or network
requests. The paced worker checks watches on the same TTL and failure backoff
as other artwork, downloading only as far as an eligible alternative. No watch
authorizes an unattended write.

## Additions and replacements

Every write is one of two kinds, and the difference is whether anything is lost:

- an **addition** puts an image where there was none: a track with no art, or a
  folder cover the album lacks;
- a **replacement** overwrites an existing image.

**A rename on its own is an addition** (#907). It loses nothing, the image a
player reads doesn't change, and it lands on a name that held nothing. A third
kind was the alternative, and it would have added a value to both threshold
settings for a change that needs no more care than an addition does.

**A tagging nobody reviewed makes additions only** (#418), and so does applying
artwork nobody reviewed ([above](#applying-artwork-without-asking)).
Replacements happen only through an action where the user has seen what will be
replaced: applying updates, applying artwork, or confirming a release with
artwork included.

**Resolution doesn't say which image is better, which is why a replacement
waits for a person.** The Cover Art Archive's images vary widely in quality.
Art that was distributed digitally is generally clean, but much of the rest is
scanned from physical sleeves, and a scan can be poor in ways no measurement
sees: soft, skewed, unevenly lit, or carrying the print's dot pattern, at any
resolution. A high-resolution scan of a CD booklet can be worse than the small
image the album already has. Pixels and bytes are all the size rule can
measure, so it proposes, and someone looking at both decides. An addition needs
no such judgment: an image where there was none loses nothing if it turns out
to be a poor one. This is a constraint on every way artwork can improve, not
only on the size rule: no measure of quality Harmonist could compute would make
an unreviewed replacement safe.

**A missing folder cover is suggested only if the user turns that on**, and it
ships off. The two mistakes don't cost the same. An album without the file still
plays correctly everywhere: Plex and Navidrome prefer an image file in the folder,
but fall back to the embedded art when there isn't one. The
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

**Retention favours each album's recent changes**: the last five artwork changes
to each album are removed last (#408). Backups are only removed above the byte
cap (1 GB), and even recent changes can go if that is necessary to fit. A global
cap alone would let an overnight pass over five hundred albums evict the backup
behind an Undo that another album was still
offering. What's protected is computed from the same history records the page
offers Undo for, so the two can't disagree. The album page checks that a backup
exists before offering Undo, since a button that fails is worse than none.

An addition needs no backup: undoing it just removes the image again, so it stays
undoable however full the store gets.
