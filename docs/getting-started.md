# Getting started

From nothing to a matched, synced library. Read
[how Harmonist works](how-it-works.md) if you want the background first; you
don't need it to follow this page.

## Try it first

The demo runs a small sample library with simulated Bandcamp and MusicBrainz
services. It needs no account and doesn't touch your music:

```bash
docker run --rm --platform linux/amd64 -p 127.0.0.1:8000:8000 -e HARMONIST_DEMO_MODE=1 ghcr.io/randomphrase/harmonist:latest
```

Open <http://localhost:8000>. **Reset Demo** puts everything back. Press Ctrl+C to
stop it.

## Install

Harmonist runs in Docker. The image is `linux/amd64`, which covers most NAS
boxes (including Synology) and PCs.

Save this as `docker-compose.yml`, change the two host paths and the `user:`
line, and run `docker compose up -d`:

```yaml
services:
  harmonist:
    image: ghcr.io/randomphrase/harmonist:latest
    container_name: harmonist
    restart: unless-stopped
    ports:
      - "8000:8000"
    volumes:
      - /path/to/music:/music     # your music library
      - /path/to/config:/config   # Harmonist's settings and history
    user: "1000:1000"             # the owner of the two folders above
```

Then open `http://<your-server>:8000`.

**Get `user:` right.** Harmonist writes into both folders, so it must run as a
user who can write there. Use the folders' owner: `id -u` and `id -g` print your
own IDs. On Synology that's usually a uid of 1026 or more and gid 100.
If Harmonist can't write to either folder, it stops at startup and says which
one; see [troubleshooting](troubleshooting.md#startup-fails-with-not-writable-by-this-process).

**Before you open it to other machines,** read [security](security.md).
Harmonist holds your Bandcamp login and isn't built to face the internet
directly.

## Let it scan your library

On first start, Harmonist scans your music folder and reads the tags already in
your files. Nothing is moved or renamed.

- Albums already tagged with **Picard** (or anything else that writes
  MusicBrainz IDs) are recognised straight away. They go to the **Library**, or
  to **Needs Linking** if they came from Bandcamp.
- Albums with a **barcode** or **Bandcamp URL** in their tags get a suggested
  match for you to confirm.
- Everything else lands in the **Inbox** under **New** or **Needs MBID**.

It works best on a library with **one album per folder**. See
[albums, folders and releases](how-it-works.md#albums-folders-and-releases).

## Match what's in the Inbox

Work through **New** and **Needs MBID**. For each album, either confirm the
suggested match after checking the side-by-side tracklist, or search MusicBrainz
by name, barcode or store URL.

![A Needs MBID card for "Gimme Some Money" by The Thamesmen. Under "Suggested
match", the three files on disk sit beside the three MusicBrainz tracks, with
matching titles; each MusicBrainz length differs from the file's by a few
seconds ("Δ 5s"). Below are "Tag changes", the artwork ("Keep existing
artwork"), and Confirm suggestion and Dismiss suggestion
buttons.](images/suggested-match.png)

If the release isn't on MusicBrainz, **Open in Harmony** helps you add it. Then
search again. See [troubleshooting](troubleshooting.md#an-album-is-stuck-in-new-or-needs-mbid)
if an album won't match.

Do this **before your first Bandcamp sync**. It's the part only you can do, and
the more albums are matched, the better the sync can link them to your
purchases.

## Set up Bandcamp sync

Skip this if you don't buy from Bandcamp.

Harmonist logs in to Bandcamp with your browser's login cookies:

1. Sign in to Bandcamp in your browser.
2. Export its cookies as a `cookies.txt` file. The
   [bandcampsync instructions](https://pypi.org/project/bandcampsync/) explain how.
3. In Harmonist, click **Set up Bandcamp sync** in the header and paste or upload
   the file.

The button changes to **Sync Bandcamp**.

## Run your first sync

Click **Sync Bandcamp**. While any album is still in **Needs Linking**, the sync
runs **link-only**: it connects your purchases to albums you already have and
downloads nothing. Your existing library is never downloaded again.

Purchases it can't confidently match appear in the Inbox as
**potential downloads**. For each, choose **Download**, link it to an album you
already have (**Already in your library?**), or **Don't download**.

Once everything is linked, later syncs download new purchases as normal: five
per sync by default, adjustable in Settings.

## Keep it tidy

- Turn on **Update checks** (**Look and report**) in Settings to have Harmonist
  check your whole library against MusicBrainz in the background. See
  [keeping tags up to date](how-it-works.md#keeping-tags-up-to-date).
- Browse the Library's filters for albums that are incomplete, missing artwork,
  or have updates waiting.
- If something looks wrong, check [troubleshooting](troubleshooting.md).
