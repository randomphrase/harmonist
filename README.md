# Harmonist

**A self-hosted music tagger that turns your Bandcamp purchases into an organized
library, with metadata from [MusicBrainz](https://musicbrainz.org).**

Complete [Picard](https://picard.musicbrainz.org)-compatible tags and cover art,
ready for Plex and Navidrome — and it **asks before it guesses**, so nothing
gets mislabeled.

> **Status:** early but usable; actively dogfooded. Feedback welcome — please
> open an issue.

## What it does

- **Sync** your Bandcamp library (via [bandcampsync](https://github.com/meeb/bandcampsync)),
  capturing each album's store URL.
- **Auto-match** each download against MusicBrainz by its Bandcamp URL. An exact
  match is tagged and filed automatically; anything ambiguous lands in a tidy
  inbox.
- **A task-oriented inbox** groups albums by what they need: a MusicBrainz ID, a
  review of an approximate match, or a sync to link the purchase. Seed missing
  releases straight to [Harmony](https://harmony.pulsewidth.org.uk).
- **Picard-compatible tagging** across `.m4a`, `.mp3`, `.flac`, `.ogg`, and
  `.opus`, embedding the MusicBrainz Release ID and cover art.
- **Library view** of everything done, with an on-demand "verify tagging vs
  MusicBrainz" check.
- **Activity feed** of recent syncs, matches, and errors.

The UI is a single page with **Inbox / Library / Activity** tabs, built with
HTMX — no SPA, no build step at runtime.

## Where Harmonist fits

Harmonist sits between your **purchases** (Bandcamp) and your **media server**
(Plex, Navidrome) — it automates the *tagging* step for music you already own.
It's deliberately narrow, and complements rather than replaces the usual tools:

- **[MusicBrainz Picard](https://picard.musicbrainz.org)** is the gold-standard
  *manual* desktop tagger — you cluster and match files by hand. Harmonist
  automates that for the Bandcamp→library flow and writes the **same
  Picard-compatible tags**, so your files stay fully Picard-editable. Reach for
  Picard on a gnarly one-off; let Harmonist handle the routine purchases.
- **[Lidarr](https://lidarr.audio)** (the \*arr suite) is a broad collection
  manager — it monitors artists and pulls releases from various indexers to
  grow a library. Harmonist is narrower and purchase-oriented: it syncs and
  tags the music you've **bought on Bandcamp** (with other stores possibly to
  follow). Lidarr automates *growing* a collection; Harmonist focuses on
  cleanly tagging what you've purchased.
- **[beets](https://beets.io)** is a powerful CLI library manager and
  autotagger. Harmonist trades the command line for a self-hosted web UI built
  around the Bandcamp purchase flow, and keeps a human in the loop — it asks
  before it guesses rather than auto-applying a best match.

In short: if you buy music on Bandcamp and want it correctly tagged and dropped
into Plex or Navidrome without hand-tagging every album, that's the gap
Harmonist fills.

## Screenshots

<!-- TODO: drop images in docs/screenshots/ and reference them here, e.g.
![Inbox](docs/screenshots/inbox.png)
![Library](docs/screenshots/library.png)
![Activity](docs/screenshots/activity.png)
-->
_Screenshots coming soon — the Inbox task list, the Library grid, and the Activity feed._

## Running

### Docker (recommended)

A pre-baked Compose file lives at `docker-compose.yml`. It pulls the
CI-published image from GHCR (`ghcr.io/randomphrase/harmonist`, `linux/amd64`).
Bind-mount your music library at `/music` and a persistent config dir at
`/config` (holds `harmonist.toml`, `cookies.txt`, `ignores.txt`, and the
album-id registry).

```bash
docker compose up -d       # pulls the image, then visit http://<host>:8000
```

For machine-specific paths (NAS share locations, etc.), use a gitignored
`docker-compose.override.yml` rather than editing the tracked file.

**Building locally** (instead of pulling the published image) — swap the
`image:` line in `docker-compose.yml` for `build: .`, or:

```bash
docker build -t harmonist:local .
```

#### File ownership: setting UID / GID

The container runs as root by default. If your bind-mounted host directories
are owned by a non-root user (almost always the case on a NAS), Harmonist will
write files with **root ownership** unless you tell Compose otherwise — at best
inconvenient, at worst it fights your other tools (Plex, Navidrome, Samba) for
the same files.

The fix is one line in `docker-compose.yml`:

```yaml
services:
  harmonist:
    # ... build / image / ports / volumes as above ...
    user: "1000:1000"   # match the OWNER of /music and /config on the host
```

Find the right values:

```bash
# On Linux / macOS — look up your user, or the user that owns the dirs.
id
# uid=1000(alice) gid=1000(alice) groups=1000(alice),...

# Synology: System Control Panel → User & Group, the UID column.
# Synology share-folder accounts typically use UID 1026+ and GID 100 (users).
```

Then make sure the host directories are writable by that user **before** you
start the container — Docker won't fix permissions for you:

```bash
mkdir -p ./config ./music
sudo chown -R 1000:1000 ./config ./music   # or the UID/GID you picked
```

On startup Harmonist logs its `uid/gid/groups` and probe-writes `/music` and
`/config`, failing fast with a clear message if either isn't writable — so a
permission problem announces itself instead of looking like a stuck scan.

**Synology / ACL shares (the gotcha that bites everyone):** `user:` sets the
uid and *primary* gid only — it does **not** carry your supplementary groups.
So a process started as `1026:100` has `groups=[100]` even though your SSH login
is also in `administrators` (101). If the share grants write via the
`administrators` group (or a DSM ACL — note "owner" in File Station is an ACL
concept, *not* the POSIX owner), the container is denied despite the "right"
uid. The clean fix is to grant **Authenticated Users** (or the `users` group)
Read/Write **recursively** on the music + config shared folders — that matches
the container's credentials across the whole tree, regardless of who owns each
album subfolder. (`group_add: ["101"]` in compose is the alternative, but
granting `users`/Authenticated-Users is safer.)

### From source (dev)

```bash
pip install -e ".[dev]"
uvicorn harmonist.web.main:app --reload      # http://127.0.0.1:8000
```

### Demo mode

Explore with a mocked, sandboxed sample library — no real Bandcamp/MusicBrainz
traffic, and your real `music_dir` is never touched:

```bash
HARMONIST_DEMO_MODE=1 uvicorn harmonist.web.main:app --reload
```

## Configuration

Config is read at startup from `harmonist.toml` in the config dir
(`~/.config/harmonist/` by default, `/config` in Docker), overridable by
`HARMONIST_*` environment variables. Most settings (download format, MB
user-agent, cover-art size, download cap, log level) are editable live from the
**Settings** page; library/config paths require a restart.

```toml
# ~/.config/harmonist/harmonist.toml
[paths]
music_dir = "/path/to/music"      # absolute (TOML doesn't expand ~)

[bandcamp]
download_format = "flac"
max_downloads_per_sync = 25       # safety cap

[musicbrainz]
user_agent = "Harmonist/0.1 ( you@example.com )"
```

Bandcamp sync needs a `cookies.txt` (exported from a logged-in browser) — paste
or upload it via the in-app **Set up Bandcamp sync** prompt.

## Deployment & security

Harmonist stores a Bandcamp session cookie (a real credential — it's how the
sync logs in to your account) and exposes destructive actions: bulk tagging,
"Forget", and "erase all sidecars". It is **not** built to face the public
internet directly. The expected deployment is single-user, on a private network
or behind a reverse proxy that handles authentication.

**What ships in the box.** Three layers of defense apply automatically:

1. **Loopback by default.** `server.host = 127.0.0.1` unless you change it.
   (The Docker image overrides this to `0.0.0.0` because container networking
   requires it — see below.)
2. **CSRF protection.** All state-changing requests require an `HX-Request:
   true` header (sent by HTMX, not by a malicious cross-origin form) plus a
   matching `Origin`/`Referer`. This blocks drive-by CSRF even if you're
   already logged in.
3. **Hostname allow-listing** via `server.allowed_hosts` (DNS-rebinding
   protection). Default is `["*"]` (permissive — see below).

**Recommended deployment:** put Harmonist behind a reverse proxy on its own
hostname, with TLS (e.g. Let's Encrypt) and authentication handled by the
proxy. Caddy, nginx, Traefik, Authelia, Authentik, and Tailscale Serve all
work; pick what you already run. Then lock down the hostname allow-list to
match:

```toml
[server]
host = "0.0.0.0"                              # for Docker / LAN bind
allowed_hosts = ["harmonist.example.com",     # your real hostname
                 "localhost", "127.0.0.1"]    # keep healthchecks working
```

**If you can't put a proxy in front**, enable the built-in HTTP Basic auth as
a fallback:

```bash
python -m harmonist.web.security
# Password: ********
# Confirm:  ********
#
# password_hash = "pbkdf2_sha256$600000$...$..."
```

```toml
[auth]
enabled = true
username = "alice"
password_hash = "pbkdf2_sha256$600000$...$..."   # paste from the CLI above
```

Restart, and every request except `/healthz` is gated by Basic auth.
Basic auth without TLS sends the password in plaintext on every request —
**always pair it with HTTPS** (i.e. with a reverse proxy or a Tailscale tunnel).

**Do not expose Harmonist's raw port to the internet.** The combination of a
credential-holding tagger and destructive endpoints is not something you want
behind nothing but luck.

## Development

```bash
make check     # ruff lint + format check + mypy --strict + pytest
make css       # rebuild static/harmonist.css (Tailwind v4, no Node)
```

CI (GitHub Actions) runs the same gate on Python 3.12 / 3.13 plus a CSS-drift
check. The Tailwind CLI is pinned for reproducible output.

**Tech:** Python 3.12+, FastAPI, HTMX + Jinja2, Tailwind CSS (via
`pytailwindcss`), `mutagen`, `musicbrainzngs`, `bandcampsync`, `httpx` +
BeautifulSoup, Pydantic, `tomlkit`.

## How this was built

Harmonist is written with heavy AI assistance (Claude) — worth being upfront
about. The aim is production-quality, maintainable software, not a throwaway
prototype:

- Every change is **reviewed by a human** before it lands.
- The codebase is type-checked with **mypy `--strict`** and linted/formatted
  with **Ruff**, enforced in CI on every push.
- An extensive automated test suite gives **~91% line coverage**
  (`make coverage`), run in CI across Python 3.12 / 3.13.

If something doesn't meet that bar, please open an issue.

## License

[GPL-3.0-or-later](LICENSE). Harmonist depends on `mutagen` (GPL), so the
combined work is GPL.

## Acknowledgements

[MusicBrainz](https://musicbrainz.org) & the [Cover Art Archive](https://coverartarchive.org),
[Harmony](https://harmony.pulsewidth.org.uk), [bandcampsync](https://github.com/meeb/bandcampsync),
and [MusicBrainz Picard](https://picard.musicbrainz.org) for the tag mappings.
