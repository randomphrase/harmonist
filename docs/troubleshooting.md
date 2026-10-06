# Troubleshooting

Problems, grouped by where you'd notice them. Each one says what's going on and
what to do. For the ideas behind the answers, see
[how Harmonist works](how-it-works.md).

- **Running Harmonist:** [not writable](#startup-fails-with-not-writable-by-this-process) ·
  [Synology permissions](#synology-the-user-is-right-but-it-still-cant-write) ·
  [artwork cache migration](#startup-fails-with-artwork-cache-migration) ·
  [invalid host header](#the-browser-shows-invalid-host-header) ·
  [changes not showing](#changes-to-my-files-dont-show-up)
- **Matching:** [stuck in the Inbox](#an-album-is-stuck-in-new-or-needs-mbid) ·
  [suggested but not tagged](#harmonist-suggested-a-release-but-didnt-tag-it) ·
  [Inconsistent](#an-album-is-marked-inconsistent) ·
  [wrong release](#an-album-is-matched-to-the-wrong-release) ·
  [release deleted](#the-release-was-deleted-from-musicbrainz) ·
  [tracks paired wrongly](#files-are-paired-with-the-wrong-tracks)
- **Bandcamp:** [sync fails](#sync-failed) ·
  [first sync downloaded nothing](#my-first-sync-didnt-download-anything) ·
  [purchase never arrived](#a-purchase-never-downloaded) ·
  [not bought on Bandcamp](#an-album-in-needs-linking-wasnt-bought-on-bandcamp) ·
  [no purchase found](#no-purchase-found) ·
  [better format or missing tracks](#i-want-a-better-format-or-tracks-the-artist-added-later)
- **Library:** [Incomplete](#an-album-says-incomplete-but-i-have-every-track) ·
  [release has more tracks](#applying-an-update-says-the-release-has-more-tracks-than-my-files) ·
  [unwanted update](#i-dont-want-an-update-harmonist-is-offering) ·
  [everything has an update](#almost-every-album-shows-update-available) ·
  [artwork in Plex/Navidrome](#plex-or-navidrome-shows-no-cover-or-the-wrong-one) ·
  [track lengths](#track-lengths-dont-match-musicbrainz) ·
  [Mixed formats](#an-album-is-listed-under-mixed-formats)
- **Undoing things:** [undo a change](#undo-a-change) ·
  [undo refused](#undo-is-refused-or-missing) ·
  [remove Harmonist](#remove-harmonist-completely)

## Running Harmonist

### Startup fails with "not writable by this process"

Harmonist checks at startup that it can write to both your music folder and its
config folder, and stops if it can't. The message names the folder and the user
the container runs as.

Set `user:` in your compose file to the folder's owner (`id -u` and `id -g`
print yours), or give that user write access to the folder:

```bash
sudo chown -R 1000:1000 /path/to/music /path/to/config
```

Then restart the container.

### Synology: the user is right but it still can't write

Docker's `user:` sets the user and its main group only, not any other groups
it belongs to. If your shared folder grants write access through another group
(such as `administrators`) or a DSM permission, the container is refused even
with the right user ID.

In DSM, give **Authenticated Users** (or the `users` group) **Read/Write** on
both shared folders, applied to all subfolders.

### Startup fails with "Artwork cache migration"

An upgrade moves downloaded images from `artwork/caa` to `caa-cache` in the
config folder. It stops if both paths exist, the old cache is a symlink or file,
or the directory cannot be renamed; neither store is deleted or merged.

Stop Harmonist and inspect the two paths named in the error. If the destination
already exists, move it aside before restarting so the old cache can move intact.
For a symlink or separate mount, move its cached images to `caa-cache` and
remove the old cache link or mount point while Harmonist is stopped. Correct any
permission error, then restart. Leave the backup files directly inside `artwork`
in place: those are what makes artwork Undo possible.

### The browser shows "Invalid host header"

You've restricted `allowed_hosts` and are visiting Harmonist under a name
that isn't in the list. Add the hostname you use (without the port) to
`allowed_hosts`, or `HARMONIST_ALLOWED_HOSTS`, and restart. See
[security](security.md#limiting-the-hostnames-it-answers-to).

### Changes to my files don't show up

Harmonist notices file changes straight away only when the music folder is on a
**local disk**. Over a network share (NFS or SMB), it can't see them, and picks
them up in its hourly rescan instead.

To see a change now, open the album's page: it always re-reads the album's
folder. Reloading the Inbox also rescans.

## Matching

### An album is stuck in New or Needs MBID

Harmonist couldn't find the release on its own. On the album's card:

1. **Search** by **Name**, **Barcode** or **Store URL**. Barcode and Store URL
   can only be used when your files carry one.
2. If you know the release, find it on [MusicBrainz](https://musicbrainz.org)
   and paste its URL or MBID into **Or paste a MusicBrainz ID**.
3. If a search finds nothing, **Open in Harmony** appears. It helps you add the
   release to MusicBrainz from its store page. Once it's saved there, press
   **Search** again.

![An album card with "Search MusicBrainz by" options Name, Barcode (greyed
out) and Store URL (selected), showing the album's Bandcamp URL and a Search
button. Below, "Or paste a MusicBrainz ID" with an Assign & Tag
button.](images/release-search.png)

### Harmonist suggested a release but didn't tag it

Harmonist only tags on its own when a match is exact. A suggestion means it's
likely but not certain: often track lengths differ by a few seconds, or the
track count doesn't match.

Check the side-by-side tracklist and **Tag changes**, then **Confirm
suggestion**, or **Dismiss suggestion** to search for another release.

### An album is marked Inconsistent

The files in one folder disagree about which album they belong to, usually
because two albums share a folder. Harmonist won't guess which files are which.

Move each album into its own folder
([Picard](https://picard.musicbrainz.org) can help), then reload the Inbox.

### An album is matched to the wrong release

On the album's page, click the **pencil** beside the MB release link. The album
goes back to **Needs MBID** so you can pick the right release. Your files keep
their current tags until you confirm the new one.

If the album is in the Library's **Possible mismatch** filter, its page
explains why and lists the likely alternatives. See
[when the match might be wrong](how-it-works.md#when-the-match-might-be-wrong).

### The release was deleted from MusicBrainz

MusicBrainz editors sometimes delete a release, usually because it duplicated
another. The album's page says so:

![The album page banner: "This release is gone from MusicBrainz — it was
deleted there, usually because it was a duplicate. Your files are untouched and
still carry its tags, but Harmonist can't compare them against anything or
re-tag them until the album points at a release that exists." A Find a new
release button sits to its right.](images/release-gone.png)

Press **Find a new release** to send the album back to **Needs MBID**. A
**Store URL** search usually finds the replacement straight away. Until then,
your files keep their tags.

### Files are paired with the wrong tracks

When track numbers are missing, or the release's tracklist has changed, Harmonist
may not know which file is which track. The album shows **Tracks unassigned**,
or its update says **Track assignments need review**.

Click **Edit track assignments** (beside **Tracks** on the album's page) and
use the arrows to move files into place. **Accept changes**, then apply.
Files left opposite a gap keep their own tags and get only the album's
MusicBrainz ID.

![The Track assignments editor: files on disk on the left, MusicBrainz tracks
on the right, each row with up and down arrows. Below are Accept changes, Reset
and Cancel buttons.](images/track-assignments.png)

## Bandcamp

### Sync failed

A sync that can't log in to Bandcamp usually means your cookies have expired.
Export a fresh `cookies.txt` from a browser signed in to Bandcamp, then in
**Settings → Bandcamp**, click **Reconfigure** and paste it.

The exact error is in the **Activity** tab and the container's log.

### My first sync didn't download anything

That's deliberate. While any album is in **Needs Linking**, a sync only links
your purchases to albums you already have. Once none are left, the next sync
downloads new purchases. See [Bandcamp sync](how-it-works.md#bandcamp-sync).

### A purchase never downloaded

Check, in order:

1. **The Inbox.** A purchase Harmonist couldn't confidently match is waiting
   as a **potential download**. Choose **Download**.
2. **The download limit.** Each sync downloads up to **Max downloads per sync**
   (Settings, default 5). The rest come in later syncs. If it's `0`, downloads
   are paused.
3. **Settings → Bandcamp → Won't download.** If you set it aside, **Restore** it and sync.
4. **`ignores.txt`** in your config folder. It lists every purchase Harmonist
   considers downloaded, one per line: an ID, then `#` and the album's name.
   Delete **that one line** and sync again. Don't empty the file, or Harmonist
   will download your whole collection again.

### An album in Needs Linking wasn't bought on Bandcamp

Click the **×** beside the album's Bandcamp link to mark it purchased
elsewhere. The album and files stay; only the Bandcamp link is removed.

### "No purchase found"

A full sync looked through your whole Bandcamp collection and couldn't find this
album, so it moved it back to **Needs MBID** with a note. If you bought it
somewhere else, press **Move to Library**.

### I want a better format, or tracks the artist added later

On the album's page, click **Re-download**. Harmonist zips your current copy into
the top of your music folder (`Artist — Album (archived <date>).zip`), then
downloads the album again in the format set in Settings. The new copy keeps the
same MusicBrainz match. The zip is yours to keep or delete once you're happy.

Re-download only appears for albums linked to a single Bandcamp purchase, and
not while a sync is running.

## Library

### An album says Incomplete but I have every track

**Incomplete** compares your files with the track count written into their own
tags. The album's page names what's missing: *10 of 11 tracks on disk*, or
*Disc 2 of 2 is missing*, and marks each missing track *Not in your files*.

- **You have a different edition**, such as the standard album matched to the
  deluxe release. Use the pencil beside the MB release link to pick the right
  one. See [wrong release](#an-album-is-matched-to-the-wrong-release).
- **You deliberately didn't rip everything**, such as a hidden track. Tick
  **Don't warn me about this** beside the badge. The album leaves the Incomplete
  filter but still shows *N of M*, in grey.

A video disc you never ripped doesn't count against an album.

### Applying an update says the release has more tracks than my files

Someone has added tracks to the release on MusicBrainz since you tagged it, and
Harmonist won't quietly tag a short album as the full one. It shows both
numbers.

- If the artist really added tracks, **Re-download** from Bandcamp gets them.
- Otherwise, press **Re-tag as incomplete**. Your files get the new tags and the
  album is listed as incomplete.

### I don't want an update Harmonist is offering

Harmonist applies what MusicBrainz says, so if the update is wrong, the fix is
to [edit the release](how-it-works.md#musicbrainz-is-the-source-of-truth).
The album's page links to it.

Meanwhile, tick **Ignore until MusicBrainz changes** to take the album off the
**Update available** list. It returns when the release next changes, usually
when your edit is accepted. Nothing is written either way.

An update labelled **Settings** didn't come from MusicBrainz: it brings the album
into line with a setting in **Settings → Tagging**. For example, with **Add the
release disambiguation to the album title** off, an album titled
*Obreel (expanded edition)* is offered *Obreel*. To keep the other spelling,
change the setting; the update disappears from every album it affects.

### Almost every album shows Update available

The **Create cover.jpg in album folders** setting is probably on. It makes every album
without a `cover.jpg` count as having an update. Set it back to **Never** in
**Settings → Tagging**. Plex and Navidrome use the artwork embedded in your
files, so most albums don't need the file.

### Plex or Navidrome shows no cover, or the wrong one

Open the album's **Artwork** section. It shows every image the album carries,
embedded in each track and as `cover.jpg` (see [artwork](how-it-works.md#artwork)).
The usual causes:

- **No image anywhere.** The Library's **No artwork** filter lists these. The
  album's page offers the Cover Art Archive's image, if there is one.
- **Embedded and `cover.jpg` disagree.** Your player is reading the one you
  don't want. Select the row, choose the image you want for it, then
  **Apply artwork**.
- **Some tracks have no image.** Applying updates fills them from the album's
  artwork.

Your player may cache artwork; refresh the album's metadata there afterwards.

### Track lengths don't match MusicBrainz

Nothing to fix in Harmonist; tags don't store lengths. Either your files are a
different master from the one on MusicBrainz, or MusicBrainz's lengths are wrong
and can be corrected there.

### An album is listed under Mixed formats

The album mixes codecs, such as one MP3 among FLAC files. On the album's page,
**Format** reads *Mixed*; click the note beside it to see which tracks differ.
Replace those files yourself, or Re-download the album from Bandcamp.

## Undoing things

### Undo a change

Open the album's page and expand **History**.

- **Undo tag changes** on a tagging entry puts the previous tag values back.
- Artwork changes have their own **Undo**, separate from tags. It restores an
  image Harmonist replaced, or removes one it added.

![A History entry, "Updated tags from MusicBrainz", with an Undo tag changes
button. Beneath it: "Date 2024 → 2024-01-01".](images/history-undo.png)

Undoing the tagging that first matched an album also unmatches it. The album
returns to **Needs MBID** with that release as a suggestion.

### Undo is refused or missing

- **A field you've changed since** (in Picard, or by a later Harmonist update) is
  left alone, and the result names it.
- **A file has been renamed or removed** and can't be identified. Undo refuses
  before writing anything.
- **No artwork Undo** means Harmonist no longer has the old image. It prioritises
  each album's last five changes when its 1 GB store fills up. See
  [configuration](configuration.md#artwork_store).

### Remove Harmonist completely

Your files stay tagged and Picard-compatible. Only the sidecars are
Harmonist's.

1. In **Settings → Maintenance**, click **Erase sidecars**. This deletes every
   `.harmonist.json` and nothing else.
2. **Stop Harmonist straight away** (`docker compose down`). Don't reload the
   Inbox first: that rescans and recreates the sidecars from your tags.
3. Delete the config folder if you don't want its history or your Bandcamp
   cookies.
