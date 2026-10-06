# Getting started

From nothing to a matched, synced library. The background is in
[how Harmonist works](how-it-works.md), though this page doesn't depend on it.

## Trying the demo

The demo runs a small sample library with simulated Bandcamp and MusicBrainz
services. It needs no account and doesn't touch your music:

```bash
docker run --rm --platform linux/amd64 -p 127.0.0.1:8000:8000 -e HARMONIST_DEMO_MODE=1 ghcr.io/randomphrase/harmonist:latest
```

It's then at <http://localhost:8000>. **Reset Demo** puts everything back, and
Ctrl+C stops it.

## Installing

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

**The `user:` line matters.** Harmonist writes into both folders, so it has to
run as a user who can write there, normally the folders' owner. `id -u` and
`id -g` print your own IDs; on Synology that's usually a uid of 1026 or more and
gid 100. If Harmonist can't write to either folder, it stops at startup and says
which one (see [troubleshooting](troubleshooting.md#startup-fails-with-not-writable-by-this-process)).

Harmonist holds your Bandcamp login and isn't built to face the internet
directly. [Security](security.md) covers making it reachable from other
machines.

## The first scan

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

## Matching what's in the Inbox

Albums in **New** and **Needs MBID** are waiting for a release. Each one either
has a suggested match, to check against the side-by-side tracklist and confirm,
or can be searched for on MusicBrainz by name, barcode or store URL.

![A Needs MBID card for "Gimme Some Money" by The Thamesmen. Under "Suggested
match", the three files on disk sit beside the three MusicBrainz tracks, with
matching titles; each MusicBrainz length differs from the file's by a few
seconds ("Δ 5s"). Below are "Tag changes", the artwork ("Keep existing
artwork"), and Confirm suggestion and Dismiss suggestion
buttons.](images/suggested-match.png)

A release missing from MusicBrainz can be added with **Open in Harmony**, after
which a search finds it. [Troubleshooting](troubleshooting.md#an-album-is-stuck-in-new-or-needs-mbid)
has more for albums that won't match.

This is worth doing **before the first Bandcamp sync**. Matching is the part only
you can do, and the more albums are matched, the more of them the sync can link
to your purchases.

## Setting up Bandcamp sync

This is only needed for Bandcamp purchases. Harmonist logs in to Bandcamp with
your browser's login cookies:

1. Sign in to Bandcamp in your browser.
2. Export its cookies as a `cookies.txt` file. The
   [bandcampsync instructions](https://pypi.org/project/bandcampsync/) explain how.
3. In Harmonist, click **Set up Bandcamp sync** in the header and paste or upload
   the file.

The button changes to **Sync Bandcamp**.

## The first sync

While any album is still in **Needs Linking**, **Sync Bandcamp** runs
**link-only**: it connects your purchases to albums you already have and
downloads nothing. Your existing library is never downloaded again.

Purchases it can't confidently match appear in the Inbox as
**potential downloads**, each with three choices: **Download**, link it to an
album you already have (**Already in your library?**), or **Don't download**.

Once everything is linked, later syncs download new purchases as normal: five
per sync by default, adjustable in Settings.

## After that

- **Library updates** (**Find updates for review**) in Settings has Harmonist check the
  whole library against MusicBrainz in the background. See
  [keeping tags up to date](how-it-works.md#keeping-tags-up-to-date).
- The Library's filters list albums that are incomplete, missing artwork, or
  have updates waiting.
- [Troubleshooting](troubleshooting.md) covers the problems people run into.
