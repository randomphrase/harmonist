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

## Screenshots

<!-- TODO: drop images in docs/screenshots/ and reference them here, e.g.
![Inbox](docs/screenshots/inbox.png)
![Library](docs/screenshots/library.png)
![Activity](docs/screenshots/activity.png)
-->
_Screenshots coming soon — the Inbox task list, the Library grid, and the Activity feed._

## Running

### Docker (recommended)

A pre-baked Compose file lives at `docker-compose.yml`. Bind-mount your music
library at `/music` and a persistent config dir at `/config` (holds
`harmonist.toml`, `cookies.txt`, `ignores.txt`, and the album-id registry).

```bash
docker compose up -d --build       # then visit http://<host>:8000
```

For machine-specific paths (NAS share locations, etc.), use a gitignored
`docker-compose.override.yml` rather than editing the tracked file.

**Multi-arch build** (amd64 for most Synology NAS, arm64 for Pi / newer NAS):

```bash
docker buildx build --platform linux/amd64,linux/arm64 -t harmonist:latest .
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

If Harmonist starts and immediately logs `PermissionError` on `cookies.txt` or
the album registry, this is the cause.

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
