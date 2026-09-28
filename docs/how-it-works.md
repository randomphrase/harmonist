# How Harmonist works

The few ideas behind everything Harmonist does. If something it's doing
surprises you, the reason is almost certainly on this page. For fixes to
specific problems, see [troubleshooting](troubleshooting.md).

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

- **If MusicBrainz is wrong, fix MusicBrainz.** Harmonist has no per-album
  override. Edit the release on MusicBrainz and the correction comes back to
  your files (and everyone else's) on the next check.
- **If a release isn't on MusicBrainz, add it.** Harmonist links to
  [Harmony](https://harmony.pulsewidth.org.uk), which imports a release from a
  store page (Bandcamp, Qobuz, Deezer and others) in a few clicks. Then search
  again in Harmonist.

Harmonist's tags are [Picard](https://picard.musicbrainz.org)-compatible, so you
can use both on the same library.

## What Harmonist changes, and what it never touches

Harmonist writes:

- **Tags** in your audio files: FLAC, MP3, AAC/ALAC (`.m4a`), Ogg Vorbis and
  Opus.
- **Artwork** embedded in those files and, if you turn it on, a `cover.jpg`
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

Harmonist treats **each folder of audio files as one album**. A folder holding
several albums is marked *Inconsistent*: split it into separate folders first.

The reverse is fine: **one album can span several folders**, such as per-disc
subfolders. If the folders are tagged to the same release and hold different
tracks of it, Harmonist treats them as one album. It doesn't move anything.

## Inbox and Library

Harmonist works out each album's state from its files and sidecar every time it
looks. Albums that need something from you are in the **Inbox**, grouped by what
they need. Albums that are done are in the **Library**.

| Inbox group | What it means | What clears it |
|---|---|---|
| **New** | Harmonist has found the folder but doesn't know which release it is. | Search MusicBrainz or paste an MBID. |
| **Needs MBID** | No confirmed release yet. There may be a *suggested match* waiting for you to review. | Confirm the suggestion, or search for the release. If it isn't on MusicBrainz, add it with Harmony. |
| **Needs Linking** | Matched, but not yet linked to the Bandcamp purchase it came from. | Run a Bandcamp sync. If you didn't buy it on Bandcamp, press the **×** beside the Bandcamp badge. |
| **Inconsistent** | Files in one folder disagree about which album they belong to. | Split the folder (Picard can help), then refresh. |
| **Tagging** | Harmonist is writing tags right now. | Nothing; it clears by itself. |

Harmonist only tags an album on its own when the match is exact. Anything less
certain, such as a release whose track lengths don't quite line up, becomes a
suggestion for you to confirm.

The Library's filters find albums that are finished but could be better:

| Filter | Shows albums that… |
|---|---|
| **Incomplete** | have fewer tracks than their release. |
| **Partially tagged** | have some files matched and some not. |
| **No artwork** | have no cover at all. |
| **Mixed formats** | mix codecs, such as one MP3 among FLACs. |
| **Update available** | have newer information on MusicBrainz than in their tags. |
| **Possible mismatch** | may be matched to the wrong release. See [Giving back](#giving-back-to-musicbrainz). |
| **MB contributions** | have a release that's missing something you could add. |

Each album has its own page with everything Harmonist knows about it: your tags
beside MusicBrainz's, the tracklist, the artwork, and its history.

## Bandcamp sync

**Sync Bandcamp** reads your Bandcamp collection, downloads new purchases, and
tags them. It needs your Bandcamp login cookies (see
[getting started](getting-started.md#set-up-bandcamp-sync)).

A sync runs in one of two modes:

- **Link-only**, automatically, while any album is in **Needs Linking**. It
  connects purchases to albums you already have and **downloads nothing**. This
  is what stops your first sync re-downloading a library you already own.
- **Full**, once everything is linked. New purchases download as normal, up to
  the **Max downloads per sync** limit in Settings. The rest wait for the next
  sync.

A purchase Harmonist can't confidently tie to an album on disk is never
downloaded on a guess. It appears in the Inbox as a **potential download**, and
you choose: download it, link it to an album you already have, or don't download
it. Purchases you set aside are listed under **Won't download** in Settings, where
you can restore them.

**Re-download** on an album's page fetches it from Bandcamp again, for a better
format or tracks the artist added later. Your current copy is zipped into the
top of your music folder first, and the new copy keeps the same match.

## Keeping tags up to date

MusicBrainz keeps improving: a date gets corrected, ISRCs get filled in, a
release gets merged into a duplicate. When an album's tags fall behind, it
appears in the Library's **Update available** filter, and its page shows exactly
what would change. Nothing is written until you press **Apply updates**.

Harmonist checks an album whenever you open its page. To check the whole library,
set **Update checks** to **Look and report** in Settings: it checks a few albums
every ten minutes, so every album comes round about weekly. It only reports;
it never writes to your files. It's off by default to go easy on MusicBrainz,
a volunteer-run service.

Each update is labelled by how far it reaches:

- **Enrichment**: details added or corrected, such as a catalogue number.
- **Identity**: the release has been merged into another.
- **Structure**: tracks added, removed or renumbered. If Harmonist can't be sure
  which file is now which track, it asks you to check the pairing.

If you disagree with an update, [fix it on MusicBrainz](#musicbrainz-is-the-source-of-truth).
While your edit waits for approval, tick **Ignore until MusicBrainz changes** on
the album's page to take it off the list. It comes back the next time the
release changes on MusicBrainz, which is usually when your edit lands.

## Artwork

Album art lives in two places: **embedded** in each track, and as a
**`cover.jpg`** (or `cover.png`) in the folder. Players differ in which they read (Plex and
Navidrome prefer the embedded image), so an album can look right in one and
blank in another. The **Artwork** section on an album's page shows both.

Harmonist's rules:

- A track with **no** embedded artwork gets the album's image when it's tagged.
- Existing embedded artwork is **not** replaced unless you ask.
- For `cover.jpg`, the largest image wins, whether it comes from your files or
  the [Cover Art Archive](https://coverartarchive.org) (MusicBrainz's image
  library). When there's a bigger one, the album's page offers it. A missing
  `cover.jpg` is only created if you turn on **Missing folder cover** in
  Settings.
- Albums where tracks have different images, such as compilations, keep them.

![The Artwork section, in two columns: Now and After Apply. "All 3 tracks,
320×320" has nothing after it: the embedded art is kept. "cover.png, 320×320"
becomes "the Cover Art Archive, 960×960". Under "Also available" is the
archive's image with a "Use this artwork" link, and an Apply artwork button sits
at the bottom.](images/artwork.png)

Bigger isn't always better, so **Use this artwork** on any image lets you choose
it anyway.

## Undo and history

Every tagging is recorded field by field, and the album's **History** shows what
changed. **Undo tag changes** puts the old values back. It won't touch a field
you've changed since, and it checks every file before writing any.

Artwork has its own undo. Harmonist keeps a copy of any image it replaces, for
the last five artwork changes to each album (up to 500 MB in total, adjustable
in [configuration](configuration.md#artwork_store)). If it can't keep a copy, it
leaves the image alone rather than replace it.

The **Activity** tab lists everything Harmonist has done, newest first, across
the whole library.

## Giving back to MusicBrainz

For albums you downloaded (from Bandcamp or a store like Qobuz), your files
carry evidence MusicBrainz may not have: the store's URL and the album's
barcode (UPC). Harmonist uses that evidence two ways:

- **Possible mismatch**: your evidence points at a *different* release in the
  same release group. Your download might be matched to the CD edition, say. The
  album's page explains why and lets you switch releases, add the correct
  release through Harmony, or say the current match is right.
- **MB contributions**: the matched release is right but is missing your store
  URL or barcode. The album's page links to the MusicBrainz editor with the value
  ready to copy.

Both are suggestions. Harmonist never edits MusicBrainz for you.
