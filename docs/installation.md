# Installing Harmonist

How to get Harmonist running and configured. Once it's up, see
**[usage.md](usage.md)** for what to do with it, and **[deployment.md](deployment.md)**
before you expose it beyond your own machine.

## Docker (recommended)

Copy this into a `docker-compose.yml`, edit the two host paths and the `user:`,
then `docker compose up -d` and visit `http://<host>:8000`:

```yaml
services:
  harmonist:
    image: ghcr.io/randomphrase/harmonist:latest   # published to GHCR, linux/amd64
    container_name: harmonist
    restart: unless-stopped
    ports:
      - "8000:8000"
    volumes:
      - /path/to/music:/music     # your music library
      - /path/to/config:/config   # persistent: harmonist.toml, cookies.txt, ignores.txt, id registry, activity.db
    # Run as the OWNER of the two host dirs above, so sidecars, tags and cover.*
    # files aren't written as root. `id -u` / `id -g` to find yours; omit to run
    # as root. (Synology: usually a 1026+ uid and gid 100 — the `users` group.)
    user: "1000:1000"
    # Only if you expose Harmonist beyond localhost: restrict the hostname
    # allow-list (DNS-rebinding protection). Loopback is always allowed.
    # environment:
    #   - HARMONIST_ALLOWED_HOSTS=harmonist.example.com,nas.local
```

**Permissions.** Make the host `music`/`config` dirs writable by that `user:`
*before* starting — Docker won't `chown` them for you (`sudo chown -R 1000:1000
/path/to/music /path/to/config`). On startup Harmonist probe-writes both and
fails fast with a clear message if either isn't writable, so a permission problem
announces itself instead of looking like a stuck scan.

**Synology / ACL shares:** `user:` sets the uid and *primary* gid only — not
your supplementary groups. So `1026:100` has `groups=[100]` even though your
login is also in `administrators` (101); if the share grants write via that
group or a DSM ACL, the container is denied despite the "right" uid. Cleanest
fix: grant **Authenticated Users** (or the `users` group) Read/Write
**recursively** on the music + config shared folders.

## From source (dev)

```bash
pip install -e ".[dev]"
uvicorn harmonist.web.main:app --reload      # http://127.0.0.1:8000
```

## Demo mode

Explore with a mocked, sandboxed sample library — no real Bandcamp/MusicBrainz
traffic, and your real `music_dir` is never touched:

```bash
HARMONIST_DEMO_MODE=1 uvicorn harmonist.web.main:app --reload
```

The sample library covers every album state, including a mis-tag and an album
with no cover art, so the flows in [usage.md](usage.md) can be tried end to end
before you point Harmonist at anything you care about. A **Reset Demo** button
puts the sandbox back to its original state.

## Configuration

Config is read at startup from `harmonist.toml` in the config dir
(`~/.config/harmonist/` by default, `/config` in Docker), overridable by
`HARMONIST_*` environment variables. Most settings (download format, download
cap, MB user-agent, update checks, log level) are editable live from the
**Settings** page; library/config paths require a restart.

```toml
# ~/.config/harmonist/harmonist.toml
[paths]
music_dir = "/path/to/music"      # absolute (TOML doesn't expand ~)

[bandcamp]
download_format = "flac"
max_downloads_per_sync = 25       # safety cap

[musicbrainz]
user_agent = "Harmonist/1.0 ( you@example.com )"
cache_ttl_seconds = 3600          # re-serve a fetched release for this long

[cover_art]
cache_ttl_seconds = 604800        # re-serve a stored archive answer for a week

[library]
watch_settle_seconds = 5          # quiet time before a watched change rescans

[gardener]
level = "off"                     # "off" | "review" — background update checks
                                  # (also on the Settings page)

[tagging]
folder_cover = "never"            # "never" | "if_missing" — create a missing
                                  # cover.jpg (also on the Settings page)
transforms = []                   # named reshapings of what gets written, e.g.
                                  # ["album_disambiguation"] (also in Settings)
```

Harmonist watches the music dir and rescans when files change under it, but
that only works on a **local** filesystem — if the container mounts your library
over NFS or SMB, the kernel never reports the change and the watcher sees
nothing. The watcher can also be killed outright by an exhausted system watch
limit on a very large tree. Neither says so, so Harmonist rescans the library
once an hour as a backstop. There is nothing to configure: an unchanged library
costs a `stat` per file and no tag reads, and the hourly rescan is meant to go
unnoticed — no **Scanning** banner, no locked inbox, and it waits for any sync
or reconcile to finish rather than competing with it.

An album's own page is never stale regardless: it re-reads that album's folders
before it renders.

MusicBrainz allows one request per second, so Harmonist caches each release it
fetches and re-serves it for `cache_ttl_seconds` rather than asking again. Past
that, the album page still draws its comparison from the stored answer and asks
for a newer one in the background, so it never waits on MusicBrainz to show
something it already has. An album's page shows when its release was last read,
as the **Checked** date in the album panel with a refresh button beside it, so you
can always force a fresh look after editing MusicBrainz — and re-tagging and
**Recheck** never use the cache. Set it to `0` to ask on every page view.

The Cover Art Archive is asked the same way, with its own
`[cover_art] cache_ttl_seconds` — **a week** rather than an hour, because cover
art changes far less often than tags and the archive is the slowest thing
Harmonist talks to. An album's page asks it when the stored answer is older than
that, in the background once the page is up, and reports it as the **CAA
checked** date with its own refresh button beside it.

**`[cover_art] size` and `HARMONIST_COVER_ART_SIZE` are no longer read.**
Harmonist always fetches the archive's **original** image: it is the largest on
offer, and it is the one the album page measures, so a preview and the tagging
that follows compare the same picture. A `harmonist.toml` still naming `size`
loads exactly as before — Harmonist warns once at startup rather than discarding
the setting quietly. If yours asked for 250, 500 or 1200px images, expect larger
downloads, a larger cover-art cache, and larger images embedded in the files
whose artwork Harmonist fills in or replaces.

`[gardener] level` decides whether Harmonist checks your library against
MusicBrainz on its own. It ships **`off`**, and the only other setting today is
**`review`**: a small, paced background pass that asks MusicBrainz about the
albums it has looked at least recently and updates the Library's **Update
available** filter from the answers. It never writes to your files — applying an
update is still something you press a button for. Turning it on means Harmonist
asks MusicBrainz about roughly a hundred albums an hour while it is idle, so
every album is re-checked about weekly; it stands aside for any sync, reconcile
or scan rather than competing with them for the one-request-per-second budget.

It is also on the **Settings** page, as **Background update checks**, and takes
effect there without a restart — this is the setting most likely to be changed
after install, since it ships off and its whole point is turning it on once you
trust it. The first pass is otherwise up to an hour away, so the control has a
**Check now** beside it that runs one straight away.

`[tagging] folder_cover` decides whether Harmonist creates a `cover.jpg` (or
`cover.png`) for an album that hasn't got one. It ships **`never`**; set it to
`if_missing` — or pick **Create it from the album's best artwork** under
**Settings → Tagging** — to have one written from whichever image wins for that
album.

It ships off because a folder cover is rarely the thing standing between you and
working artwork: Plex and Navidrome both read the image embedded in the files
first, and most libraries have that. What the file does cost is noise — while
the setting is on, **every album without a `cover.jpg` shows as having an update
available**, which on a library adopted from elsewhere can be nearly all of it,
and an update filter that lists everything lists nothing. Turning it on writes a
few megabytes into each of those folders, so it is a decision worth making
deliberately rather than one to inherit from an upgrade.

Neither value touches artwork you already have. Existing `cover.*` files are
left exactly where they are whichever way this is set, and one that exists is
still offered an upgrade when a better image turns up — this governs only
whether a missing one gets created.

`[tagging] transforms` turns on **tag transforms** — named reshapings of what
Harmonist writes, matching choices Picard offers through its options and tagger
scripts. It ships empty, so nothing is reshaped. One transform exists today:

| Name | What it writes |
| --- | --- |
| `album_disambiguation` | The release's disambiguation comment appended to the album title, so *Selected Ambient Works Volume II* becomes *Selected Ambient Works Volume II (expanded edition)*. Releases with no disambiguation are unaffected. |

They are also on the **Settings** page, under **Tagging**, and take effect
without a restart. See [the user guide](usage.md#tag-transforms) for what
turning one on does and does not do to a library you already have.

Bandcamp sync needs a `cookies.txt` (exported from a logged-in browser) — paste
or upload it via the in-app **Set up Bandcamp sync** prompt.

## Uninstall

All of Harmonist's state lives in `.harmonist.json` **sidecar** files next to your
albums — your audio, its MusicBrainz tags, and `cover.*` art are just your library
and carry nothing Harmonist-specific. To remove it cleanly:

1. **Settings → Erase sidecars.** This deletes every `.harmonist.json`; your
   tagged audio and cover files are not touched.
2. **Stop the app straight away — don't return to the Inbox.** Opening the inbox
   triggers a re-scan and reconcile, which would re-derive the sidecars from your
   files' tags. Shut down first (e.g. `docker compose down`) and the library is
   left sidecar-free.

Your music stays fully tagged and Picard-compatible, with no trace of Harmonist.
To also drop the stored Bandcamp cookie and settings, delete the config dir.
