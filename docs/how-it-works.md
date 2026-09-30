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

Albums with nothing to decide are in the **Library**. Its filters find the ones
that are finished but could be better:

![The Library: a search box, then filter chips with counts (All 9, Incomplete
1, Partially tagged 0, No artwork 0, Mixed formats 0, Update available 1,
Possible mismatch 0, MB contributions 0), then a row of album tiles. The tile
for Fever Dog carries an "Update" badge and a "3 of 4" badge; two others are
marked "New".](images/library.png)

| Filter | Shows albums that… |
|---|---|
| **Incomplete** | have fewer tracks than their release. |
| **Partially tagged** | have some files matched and some not. |
| **No artwork** | have no cover at all. |
| **Mixed formats** | mix codecs, such as one MP3 among FLACs. |
| **Update available** | have newer information on MusicBrainz than in their tags. See [keeping tags up to date](#keeping-tags-up-to-date). |
| **Possible mismatch** | may be matched to the wrong release. See [when the match might be wrong](#when-the-match-might-be-wrong). |
| **MB contributions** | have a release that's missing something you could add. See [giving back to MusicBrainz](#giving-back-to-musicbrainz). |

Each album has its own page with everything Harmonist knows about it: your tags
beside MusicBrainz's, the tracklist, the artwork, and its history.

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
appears in the Library's **Update available** filter, and its page shows exactly
what would change. Nothing is written until **Apply updates** is pressed.

Harmonist checks an album whenever its page is opened. Setting **Update checks**
to **Look and report** in Settings extends that to the whole library: a few
albums every ten minutes, so every album comes round about weekly. It only
reports, and never writes to your files. It's off by default to go easy on
MusicBrainz, a volunteer-run service.

Each update is labelled by how far it reaches:

- **Cosmetic**: a title that differs only in capitalisation or spacing.
- **Enrichment**: details added or corrected, such as a catalogue number or a
  more precise date.
- **Structure**: tracks added, removed or renumbered. If Harmonist can't be sure
  which file is now which track, it asks for the pairing to be checked.
- **Identity**: the album now points at a different release, for instance
  after a merge on MusicBrainz.

An update that looks wrong is a problem with the release on MusicBrainz, and
[the correction belongs there](#musicbrainz-is-the-source-of-truth). An edit can
take a while to be accepted; meanwhile, **Ignore until MusicBrainz changes** on
the album's page takes it off the list. It comes back the next time the release
changes on MusicBrainz, which is usually when the edit lands.

## Artwork

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
  `cover.jpg` is only created when **Missing folder cover** is turned on in
  Settings.
- Albums where tracks have different images, such as compilations, keep them.

![The Artwork section, in two columns: Now and After Apply. "All 3 tracks,
320×320" has nothing after it: the embedded art is kept. "cover.png, 320×320"
becomes "the Cover Art Archive, 960×960". Under "Also available" is the
archive's image with a "Use this artwork" link, and an Apply artwork button sits
at the bottom.](images/artwork.png)

Bigger isn't always better, so **Use this artwork** on any image selects it
regardless of size.

## Undo and history

Every tagging is recorded field by field, and the album's **History** shows what
changed. **Undo tag changes** puts the old values back. It won't touch a field
that has changed since, and it checks every file before writing any.

Artwork has its own undo. Harmonist keeps a copy of any image it replaces, for
the last five artwork changes to each album (up to 500 MB in total, adjustable
in [configuration](configuration.md#artwork_store)). If it can't keep a copy, it
leaves the image alone rather than replace it.

The **Activity** tab lists everything Harmonist has done, newest first, across
the whole library.

## When the match might be wrong

A download often matches more than one release in its release group: the
Bandcamp edition, the CD, a regional release. Picking the wrong one gives your
files the wrong barcode, label or tracklist. For albums you downloaded, your
files carry evidence of which release you actually bought: the store's URL
(Bandcamp puts it in the comments), or the album's barcode (UPC), which stores
like Qobuz embed.

An album counts as a download only when its files carry a store's own mark: a
Bandcamp URL, Qobuz's track id, or Amazon's or Beatport's purchase note. These
survive converting FLAC to ALAC with XLD. A barcode on its own isn't enough,
because CD rippers write one too. Files with AccurateRip tags are a CD rip. When
the marks disagree — a rip's and a store's, or two stores' — the origin is
unknown and the album isn't checked. The album's page shows the origin, and the
tags it rests on, under **Additional info**.

Harmonist compares that evidence with every release in the group. An album goes
into the **Possible mismatch** filter when the evidence points somewhere other
than its current match, for example:

- your store URL, or your barcode, belongs to a different release;
- the matched release has a barcode, and it isn't yours;
- your download came from a Bandcamp page that another release links to, and
  the matched one doesn't;
- the matched release is a CD or other physical release.

A CD rip whose ripping software wrote a barcode (UPC) is checked the same way,
turned round: the other releases it could be are the group's CDs and other
physical releases, and a match to a digital release is the suspect one. Its
barcode came from the ripper's metadata lookup rather than off the disc, so it's
used only to check the match, never offered to MusicBrainz.

The album's page lists the reasons, then the matched release beside the other
releases in the group it could be, with the evidence that supports each. From
there, the album can be switched to another release (after reviewing its tracks
and artwork), the correct release can be added to MusicBrainz through Harmony,
or **Don't warn me about this** records that the current match is the release
you bought.

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

The album's page shows one finding at a time, with a link to the release's
editor on MusicBrainz and the value ready to copy. After the edit is accepted,
refreshing the album's MusicBrainz check moves on to the next finding, if there
is one.

A barcode that disagrees with the one on MusicBrainz is shown as something to
look into, not a replacement to make. Private Bandcamp downloads are never
suggested as links, since their URLs aren't public. And Harmonist never edits
MusicBrainz itself.
