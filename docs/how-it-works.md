# How Harmonist works

The few ideas behind everything Harmonist does. When it does something
surprising, the reason is almost certainly on this page. Fixes for specific
problems are in [troubleshooting](troubleshooting.md).

## MusicBrainz is the source of truth

[MusicBrainz](https://musicbrainz.org) is a free, community-edited database of
music releases. Harmonist tags your files with exactly what MusicBrainz says,
and keeps no exceptions of its own.

A few MusicBrainz terms come up everywhere:

- A **release** is one specific edition of an album: the 2003 UK CD, the
  Bandcamp download, the 20th-anniversary remaster. Each has its own tracklist,
  date, label and barcode.
- A **release group** gathers all the releases of the same album.
- An **MBID** (MusicBrainz ID) is the unique ID of a release. An album is
  *matched* once Harmonist knows its release's MBID.

Two things follow from MusicBrainz being the source of truth:

- **When MusicBrainz is wrong, the correction belongs on MusicBrainz.**
  Harmonist has no per-album override. Once a release is edited there, the
  correction reaches your files (and everyone else's) on the next check.
- **When a release isn't on MusicBrainz, it can be added.** Harmonist links to
  [Harmony](https://harmony.pulsewidth.org.uk), which imports a release from a
  store page (Bandcamp, Qobuz, Deezer and others) in a few clicks. A search in
  Harmonist then finds it.

Harmonist's tags are [Picard](https://picard.musicbrainz.org)-compatible, so the
two can be used on the same library.

## What Harmonist changes, and what it never touches

Harmonist writes:

- **Tags** in your audio files: FLAC, MP3, AAC/ALAC (`.m4a`), Ogg Vorbis and
  Opus.
- **Artwork** embedded in those files and, if that setting is on, a `cover.jpg`
  beside them.
- A small **`.harmonist.json`** file in each album folder (the *sidecar*),
  recording which release the album is matched to and which purchase it came
  from.

Harmonist never:

- moves, renames or deletes your folders or files (the one exception is
  [Re-download](#bandcamp-sync), which zips the old copy first);
- converts between formats;
- writes or clears genres, or overwrites comments;
- rewrites tags you haven't reviewed. The one thing it does unasked is tag a
  new download whose release it found exactly.

Every change is recorded and can be undone. See [Undo and history](#undo-and-history).

## Albums, folders and releases

Harmonist treats **each folder of audio files as belonging to one album**. A
folder holding several albums is marked *Inconsistent*, and stays that way until
it's split into separate folders.

The reverse is fine: **one album can span several folders**, such as per-disc
subfolders. If the folders are tagged to the same release and hold different
tracks of it, Harmonist treats them as one album. It doesn't move anything.

## Artists and local metadata

Artists come from the Library's embedded **album-artist credits**, including
separate collaborators. MusicBrainz artist IDs keep different people with the
same name apart and let one collaborative album appear under each artist. A name
without a usable ID stays an unidentified credit scoped to its album; it does
not silently become the same person as another credit with that spelling.

When only some tracks lack artist IDs, consistent artist credits on the other
tracks can identify the album's artist if all tracks name the same release.
The artist remains a single entry, with an **Unidentified credit** badge naming
the affected album. The badge reflects the files and clears when their missing
IDs are filled in; browsing does not write them.

Existing Kodi/Jellyfin `artist.nfo` files can supply a biography, dates, sort
name and other artist information. Their artist ID must match the embedded
album-artist ID. They can live above the audio files or in a separate
[Artist Information Folder](configuration.md#paths), without imposing an audio
directory layout. Multiple matching files require a source choice. A source
choice belongs to the page URL, so it can be bookmarked without modifying music
or metadata files.

Artist browsing reads local tags, NFOs and local raster artwork. It makes no
MusicBrainz or image-provider requests, does not generate NFOs and never changes
tags based on them. Remote image URLs in an NFO are not downloaded. With local
NFOs and image files, the artist pages remain available offline. The MusicBrainz
link opens the artist's page separately. NFO discographies do not count as owned
albums. **Read again** reloads the local metadata; an external edit is also
noticed on the next visit.

## The Inbox

Harmonist works out each album's state from its files and sidecar every time it
looks. Albums that need a decision from you are in the **Inbox**, grouped by
what they need, and each group says what will clear it.

![The Inbox, "5 need attention", in three groups. "New · 1": "These albums need
a MusicBrainz release — search by name to locate it, or paste an MBID", above an
album card with search fields. "Needs MBID · 1": "No confirmed MusicBrainz
release yet. Where a suggestion was found, review the side-by-side and confirm;
otherwise search by name, barcode or store URL, paste an MBID, or seed a missing
release via Harmony and search again", above a card offering search by Name,
Barcode or Store URL. "Needs Linking · 3": "Sync Bandcamp to link these albums.
While any remain, that sync links your existing albums and downloads nothing
new; new purchases download on the next run", above a card with MusicBrainz and
Bandcamp badges and a × button.](images/inbox.png)

| Inbox group | What it means | What clears it |
|---|---|---|
| **New** | Harmonist has found the folder but doesn't know which release it is. | A search of MusicBrainz, or a pasted MBID. |
| **Needs MBID** | No confirmed release yet. There may be a *suggested match* waiting for review. | Confirming the suggestion, or finding the release by search. A release missing from MusicBrainz can be added with Harmony. |
| **Needs Linking** | Matched, but not yet linked to the Bandcamp purchase it came from. | A Bandcamp sync. For an album bought elsewhere, the **×** beside the Bandcamp badge. |
| **Inconsistent** | Files in one folder disagree about which album they belong to. | Splitting the folder (Picard can help). |
| **Tagging** | Harmonist is writing tags right now. | Nothing; it clears by itself. |

Harmonist only tags an album on its own when the match is exact. Anything less
certain, such as a release whose track lengths don't quite line up, becomes a
suggestion to confirm.

## The Library

Albums with nothing to decide are in the **Library**. Its filters narrow the
collection by origin or audio format, or find albums that could be better.
Filters combine: an album must match every selected filter, as well as any
artist or album search. Origin, codec, artwork observations, tag updates and
artwork updates each have one choice at a time, so changing origin keeps the
other restrictions. Counts describe albums, not individual images or tag fields.

![The Library: a search box, then filter chips with counts (All 9, Incomplete
1, Partially tagged 0, No artwork 0, Mixed formats 0, Update available 1,
Possible mismatch 0, MB contributions 0), then a row of album tiles. The tile
for Fever Dog carries an "Update" badge and a "3 of 4" badge; two others are
marked "New".](images/library.png)

| Filter | Shows albums that… |
|---|---|
| **Origin** | came from Bandcamp, Qobuz, Amazon, Beatport or CD, or have an Unknown origin, using the same detection or explicit choice as the album page. |
| **New** | were added to Harmonist within the last seven days, regardless of source, matching the New badge. |
| **Incomplete** | are missing tracks or discs, based on the totals in their tags. Albums accepted with **Don't warn me about this** are left out. |
| **Partially tagged** | have the matched release's MusicBrainz ID in some files but not others. |
| **Artwork** | have no local artwork, tracks without embedded artwork, or different images across tracks. These are observations; different track images may be intentional. |
| **Format** | contain a file of the selected codec, including mixed albums. **Mixed formats only** independently narrows to albums mixing codecs, such as one MP3 among FLACs; differing bitrates or sample rates alone do not make an album mixed. |
| **Tag updates** | have tag changes waiting for review, including missing metadata and differences from MusicBrainz or the current tagging settings. Updates can be narrowed to **Structure**, **Identity**, **Enrichment**, **Cosmetic** or **Settings**; [keeping tags up to date](#keeping-tags-up-to-date) explains each. |
| **Artwork updates** | have an **Addition** or **Replacement** waiting for review. An addition fills empty places; a replacement overwrites at least one existing image and may also fill gaps. |
| **Possible mismatch** | may be matched to the wrong release. See [when the match might be wrong](#when-the-match-might-be-wrong). |
| **MB contributions** | have a release that's missing something you could add. See [giving back to MusicBrainz](#giving-back-to-musicbrainz). |

Each album has its own page with everything Harmonist knows about it: your tags
beside MusicBrainz's, the tracklist, the artwork, and its history.

Files that belong to the release but aren't paired with a MusicBrainz track are
marked **Tracks unassigned**, separately from partial tagging. The tracklist
shows the gaps; [track assignments](troubleshooting.md#files-are-paired-with-the-wrong-tracks)
can be reviewed. Missing or inconsistent tag values appear in the comparison
with MusicBrainz. Files that disagree about which album they belong to go to
**Inconsistent** in the [Inbox](#the-inbox).

## Bandcamp sync

**Sync Bandcamp** reads your Bandcamp collection, downloads new purchases, and
tags them. It needs your Bandcamp login cookies (see
[getting started](getting-started.md#setting-up-bandcamp-sync)).

A sync runs in one of two modes:

- **Link-only**, automatically, while any album is in **Needs Linking**. It
  connects purchases to albums you already have and **downloads nothing**. This
  is what stops a first sync re-downloading a library you already own.
- **Full**, once everything is linked. New purchases download as normal, up to
  the **Max downloads per sync** limit in Settings. The rest wait for the next
  sync.

A purchase Harmonist can't confidently tie to an album on disk is never
downloaded on a guess. It appears in the Inbox as a **potential download**, with
three choices: download it, link it to an album already on disk, or don't
download it. Purchases set aside are listed under **Won't download** in
Settings, where they can be restored.

**Re-download** on an album's page fetches it from Bandcamp again, for a better
format or tracks the artist added later. The current copy is zipped into the top
of the music folder first, and the new copy keeps the same match.

## Keeping tags up to date

MusicBrainz keeps improving: a date gets corrected, ISRCs get filled in, a
duplicate release gets merged into another. When an album's tags fall behind, it
appears in the Library's **Tag updates** filter, and its page shows exactly
what would change. Nothing is written until **Apply updates** is pressed.

Harmonist checks an album whenever its page is opened. Setting **Check
MusicBrainz for updates** to **In the background** in Settings extends that to
the whole library: a few albums every ten minutes, so every album comes round
about weekly. Checking only finds updates; what happens to them is set
separately, below. It's off by default to go easy on MusicBrainz, a
volunteer-run service.

Each update is labelled by how far it reaches:

- **Settings**: tags that don't yet follow a tagging setting, such as an album
  title without the disambiguation the setting adds, or an artist written as
  credited on the release where the setting writes the artist's own name. These
  come from Harmonist's settings, not MusicBrainz, so they're never announced and
  can't be ignored; changing the setting back withdraws them.
- **Cosmetic**: a title that differs only in capitalisation or spacing.
- **Enrichment**: details added or corrected, such as a catalogue number, a
  more precise date, or a collaboration's artists stored as one value
  (`Artist A/Artist B`) written out as separate artists.
- **Identity**: the album now points at a different release, for instance
  after a merge on MusicBrainz.
- **Structure**: tracks added, removed or renumbered. If Harmonist can't be sure
  which file is now which track, it asks for the pairing to be checked.

An album's update takes the label of its furthest-reaching change, in the order
above. **When a tag update is found** in Settings decides what happens to each
level, beside the number of albums currently waiting at each. Each level is
ticked in at most one column:

- **Apply**: ticks run up from Settings, never as far as
  Structure. Nothing is applied by default. Saving applies the updates already
  waiting at once, and later ones are applied within about ten minutes of being
  found. Each is recorded in the album's History, where **Undo tag changes**
  reverses it; only tags are written, never artwork.
- **Inbox**: ticks run down from Structure. By default that is
  Structure alone, since a changed tracklist can call the match itself into
  question. Updates other than Structure can be applied from the Inbox several
  at once. As with automatic applying, only tags are written, each album gets
  its own History entry and Undo, and an update that changed on MusicBrainz
  after it was selected is left for another look.
- A level ticked in neither is left in the Library, under the **Tag updates**
  filter.

An update Harmonist was trusted to apply but couldn't, such as one needing its
track assignments reviewed or an album missing tracks, waits in the Inbox with
the reason. Only updates bound for the Inbox are announced in the Activity feed.

An update that looks wrong is a problem with the release on MusicBrainz, and
[the correction belongs there](#musicbrainz-is-the-source-of-truth). An edit can
take a while to be accepted; meanwhile, **Ignore until MusicBrainz changes** on
the album's page takes it off the list. It comes back the next time the release
changes on MusicBrainz, which is usually when the edit lands.

## Artwork

**Check MusicBrainz for updates → In the background** also checks the Cover Art Archive
in the background, independently of tag changes. The checks are paced in small
batches, using the archive cache's freshness interval (a week by default).
They propose changes without writing to the music files. With background checks
off, opening an album still checks the archive when its cached answer is stale;
the CAA refresh control asks again immediately.

**Better artwork wanted** considers alternatives on an album's matched release
and release group, including smaller images that may be more appropriate than a
borrowed cover. They appear through the ordinary artwork update proposal.
**Don't suggest this image** excludes an image until that checkbox is cleared;
it stays available for manual selection. Applying artwork ends the watch.
The watch runs even when general background checks are off, and never applies
artwork automatically.

The Library's artwork findings use the same proposal as the album page. A missing
cover can have no image available to add, while an album that already has a cover
can still have tracks without embedded artwork. **No artwork** and **Addition**
can therefore overlap. Unchecked or unreadable artwork is not a claim that the
album needs no changes. Ignoring a tag update does not hide an artwork proposal.

**When an artwork update is found** in Settings decides what happens to each:

- **Apply**: additions can be applied automatically, never replacements, so
  nothing is overwritten without someone looking. Nothing is applied by
  default. Saving applies the additions already waiting at once, and later ones
  are applied within about ten minutes of being found. Each is recorded in the
  album's History, where it can be undone.
- **Inbox**: replacements, or additions as well. Cards can be ticked and
  applied several at once, replacements included, each with its own History
  entry and Undo; an album whose artwork changed after it was selected is left
  for another look. The album's page shows every image and is where an update
  is changed before it is applied: another image chosen, a row kept as it is, or
  an image no longer suggested.
- Anything ticked in neither stays in the Library under **Artwork updates**,
  which is where all of them stay by default.

An album whose update both adds and replaces artwork counts as a replacement,
so none of it is applied automatically. An album with **Better artwork wanted**
waits in the Inbox whatever is chosen. An addition Harmonist was trusted to
apply but couldn't waits in the Inbox with the reason. Only updates bound for
the Inbox are announced in the Activity feed.

Album art lives in two places: **embedded** in each track, and as a
**`cover.jpg`** (or `cover.png`) in the folder. Players differ in which they
read (Plex and Navidrome prefer the embedded image), so an album can look right
in one and blank in another. The **Artwork** section on an album's page shows
both.

Harmonist's rules:

- A track with **no** embedded artwork gets the album's image when it's tagged.
- Existing embedded artwork is **not** replaced unless you choose to replace it.
- For `cover.jpg`, the largest image wins, whether it comes from your files or
  the [Cover Art Archive](https://coverartarchive.org) (MusicBrainz's image
  library). When there's a bigger one, the album's page offers it. A missing
  `cover.jpg` is only suggested when **Create cover.jpg in album folders** is enabled in
  Settings, though you can choose an image for it either way.
- Albums where tracks have different images, such as compilations, keep them
  unless you choose an image for one of them.

![The Artwork section, with columns Now, After Apply and Available. "cover.png,
320×320" becomes "the Cover Art Archive, 960×960", with an × beside it. The "All
3 tracks, 320×320" row is selected and has an empty box after it: the embedded
art is kept. Under Available is the archive's "This release, 960×960" image,
with a Use button pointing at the selected row and an arrow to the next
image.](images/artwork.png)

Bigger isn't always better, so any row can take any image the Cover Art Archive
has for the release, its release group or the group's other releases,
regardless of size, or keep the image it has instead of the one suggested for
it. A choice reaches only its own row, so a compilation's other covers stay as
they are.

## Undo and history

Every tagging is recorded field by field, and the album's **History** shows what
changed. **Undo tag changes** puts the old values back. It won't touch a field
that has changed since, and it checks every file before writing any.

Artwork has its own undo. Harmonist keeps a copy of any image it replaces, up to
1 GB in total (adjustable in [configuration](configuration.md#artwork_store)).
Backups are removed only when the store is full, prioritising each album's last
five artwork changes. If it can't keep a copy, it leaves the image alone rather
than replace it.

The **Activity** tab lists everything Harmonist has done, newest first, across
the whole library.

## Where the files came from

An album's page shows its **Origin**: Bandcamp, Qobuz, Amazon, Beatport, CD or
Unknown. Nothing records where a file came from when it's copied into a library,
so Harmonist deduces it from marks that particular stores and rippers leave in
the tags: a Bandcamp URL in the comment, Qobuz's track id, Amazon's or
Beatport's purchase note, or AccurateRip's verification of a ripped disc. The
stores' marks survive converting FLAC to ALAC with XLD. The tags the origin
rests on are listed under **Additional tags**.

Only those marks count. A barcode isn't enough, because CD rippers write one
too, and neither is hi-res audio, which vinyl rips have as well. When there are
no marks, or they disagree (a rip's and a store's, or two stores'), the origin
is Unknown. An album Harmonist downloaded from Bandcamp itself is detected as
Bandcamp.

The origin describes the files, not the purchase: a CD bought on Bandcamp and
then ripped is a CD.

For Qobuz albums with several barcode matches, a single release linked to Qobuz
can become the suggested match when its digital format and track count agree
with the files. It still needs confirmation, and any track-length differences
remain visible in the comparison.

An explicit Origin choice takes precedence when the tags tell the wrong story:
for example, a CD rip may retain Amazon comments copied from an older download.
The choice affects mismatch checks, contribution checks and Qobuz barcode
suggestions without changing those tags.
It survives scans, retagging and rematching, and applies to every folder of the
album. Re-download archives that choice with the old files; the replacement uses
automatic detection. Choosing Unknown suppresses detection, while Automatic
detection removes the choice. Files replaced outside Harmonist keep the choice
until it is changed or removed.

## When the match might be wrong

A download often matches more than one release in its release group: the
Bandcamp edition, the CD, a regional release. Picking the wrong one gives your
files the wrong barcode, label or tracklist. For albums you downloaded, your
files carry evidence of which release you actually bought: the store's URL
(Bandcamp puts it in the comments), or the album's barcode (UPC), which stores
like Qobuz embed.

An album counts as a download only when its
[origin](#where-the-files-came-from) is a store. When the origin is Unknown, the
album isn't checked.

Harmonist compares that evidence with every release in the group. An album goes
into the **Possible mismatch** filter when the evidence points somewhere other
than its current match, for example:

- your store URL, or your barcode, belongs to a different release;
- the matched release has a barcode, and it isn't yours;
- your download came from a Bandcamp page that another release links to, and
  the matched one doesn't;
- the matched release is a CD or other physical release.

A CD rip is checked against the release's media even without an original UPC:
a match to a digital release is suspect. The other releases it could be are the
group's CDs and other physical releases. If its ripping software wrote a barcode
(UPC), that also checks the match. That barcode came from the ripper's metadata
lookup rather than off the disc, so it's never offered to MusicBrainz.

The album's page lists the reasons, then the matched release beside the other
releases in the group it could be, with the evidence that supports each. From
there, the album can be switched to another release (after reviewing its tracks
and artwork), the correct release can be added to MusicBrainz through Harmony,
or its warning can be silenced until a better match is found. New compatible
siblings of a mismatched media type, or new exact store URL or barcode evidence
on a sibling, bring the warning back. Unrelated metadata edits do not. These
are candidates for review; the match never changes automatically. Background
discovery follows the configured MusicBrainz update checks; opening the album
also checks the group. Changing the Origin choice clears the dismissal.

Earlier **Don't warn me about this** decisions and explicitly confirmed release
choices retain their original meaning. Removing that acceptance restores
warnings; a subsequent dismissal uses the conditional behaviour.

Nothing is flagged until the whole release group has been compared, and a group
that can't be compared in full (more than 100 releases, or releases with no
media listed) is left alone. So the filter fills in gradually, as albums are
opened or checked in the background.

## Giving back to MusicBrainz

Once an album's release is settled, the same evidence can show what that
release is missing on MusicBrainz: your store URL, or its barcode. Those albums
are in the **MB contributions** filter.

A store URL already linked from another release in the group is not offered,
even when the current match is accepted. A store's tracklist can change over
time while an older download still belongs to the original release.

A download provisionally matched to a physical release does not establish that
its URL or barcode belongs on that release. Where a source URL or UPC is known,
adding the missing release through Harmony remains available after dismissal.

The album's page shows one finding at a time, with a link to the release's
editor on MusicBrainz and the value ready to copy. After the edit is accepted,
refreshing the album's MusicBrainz check moves on to the next finding, if there
is one.

A barcode that disagrees with the one on MusicBrainz is shown as something to
look into, not a replacement to make. Private Bandcamp downloads are never
suggested as links, since their URLs aren't public. And Harmonist never edits
MusicBrainz itself.
