# Harmonist

A self-hosted music tagger that turns your Bandcamp purchases into an organized
library, with metadata from MusicBrainz (destinations: Plex / Navidrome).

## What it does

Scans a local music directory, looks each album up on MusicBrainz (by store URL
when available, else by tag-derived search), and presents a task-oriented UI to
resolve ambiguities or seed missing releases. Once matched, files are tagged
Picard-compatibly with the MusicBrainz Release ID and cover art.

## Tech Stack

- **Backend:** Python 3.12+, FastAPI, `mutagen` for audio tag I/O
- **Frontend:** HTMX + Jinja2 templates, Tailwind CSS v4 (built via
  `pytailwindcss` — no Node toolchain), committed `static/harmonist.css`
- **MusicBrainz:** `musicbrainzngs`
- **Bandcamp:** `bandcampsync` + a thin custom hook
- **Config:** Pydantic + `tomlkit`

## Project Layout

```
src/harmonist/
  web/main.py            # FastAPI app — create_app() + all routes
  web/security.py        # CSRF / TrustedHost / Basic auth middlewares
  web/sync_runner.py     # Bandcamp sync task wrapper (background-thread)
  web/reconcile_runner.py# Reconciliation pass over the library
  scanner.py             # Walk music dir → Album objects (state-dispatched)
  models.py              # Album, Sidecar, AlbumState, MatchCandidate, ...
  sidecar.py             # Read/write .harmonist.json sidecars
  tagger.py              # PicardCompatibleTagger — writes MB tags + cover
  mb_lookup.py           # MB by-id / by-url fetches (1 req/s budget)
  mb_search.py           # MB free-text search
  match.py               # Disk-vs-MB comparison (assess_match)
  reconcile.py           # Tag-derived MBID recovery for orphan albums
  url_recovery.py        # Recover Bandcamp URL from embedded tags
  bandcamp_hook.py       # bandcampsync wrapper + post-download callback
  cover_art.py           # CAA fetch + cover.* writing
  activity.py            # In-memory ring-buffer log for the Activity tab
  id_registry.py         # UUID assignment for albums without an MBID
  config.py              # Pydantic config model + env/TOML loading
  formats/               # Per-format tagger backends (m4a, mp3, flac, ogg, opus)
  demo.py                # Demo-mode monkey-patches + seeded sample library
templates/               # Jinja2/HTMX templates (root-level, NOT under src/)
static/                  # Built Tailwind CSS bundle (committed)
test/                    # pytest suite — see test_security.py, test_web.py, ...
```

## Running

```bash
# Dev server (real mode) — loopback by default
uvicorn harmonist.web.main:app --reload

# Demo mode — mocked MB/Bandcamp/cover-art, sandboxed music dir under tmpdir
HARMONIST_DEMO_MODE=1 uvicorn harmonist.web.main:app --reload
```

## Tests

```bash
make test       # pytest
make check      # ruff + ruff format --check + mypy --strict + pytest
make coverage   # line coverage report
```

## CSS build

Tailwind v4 standalone via the `pytailwindcss` Python wrapper. The built
`static/harmonist.css` is committed; re-run after editing templates so new
utility classes are included:

```bash
make css           # one-shot minified build
make css-watch     # watch + rebuild on save
```

The Tailwind version is pinned (`TAILWINDCSS_VERSION` in the Makefile) so
`make css` output is byte-reproducible across machines and CI.

## Album States (`AlbumState` in `models.py`)

| State          | Condition                                            | Inbox? |
| -------------- | ---------------------------------------------------- | ------ |
| `NEW`          | No sidecar; nothing tagged                           | yes    |
| `NEEDS_MBID`   | Sidecar present, no MBID; may carry a suggestion     | yes    |
| `NEEDS_REVIEW` | An approximate MB match exists; needs confirmation   | yes    |
| `NEEDS_SYNC`   | Tagged + URL is Bandcamp, but `bandcamp.item_id=None`| yes    |
| `TAGGING`      | Transient — in the middle of writing tags            | yes    |
| `COMPLETE`     | Tagged, file count matches MB track count            | no     |
| `INCOMPLETE`   | Tagged, but file count < MB track count              | no     |
| `INCONSISTENT` | Sidecar contradicts what's on disk                   | yes    |

Terminal states (`COMPLETE`, `INCOMPLETE`) are hidden from the inbox and shown
in the Library tab. See `docs/design.md` §3 for the full state machine and the
transition diagram in §3.1.

## Key Conventions

- Albums are identified by either their MBID (once tagged) or a UUID minted by
  `id_registry.py` (before they have an MBID).
- Picard-compatible MBID tag: `MUSICBRAINZ_RELEASEID` (atom varies per format —
  see `formats/`).
- The Bandcamp / store URL is the primary MB-lookup key and lives at top-level
  `store_url` in the sidecar.
- `HARMONIST_DEMO_MODE=1` (or `demo_mode = true` in `harmonist.toml`) enables
  demo mode: `demo.py` monkey-patches `mb_lookup`/`mb_search`/`cover_art` and
  seeds a sandbox library at `$TMPDIR/harmonist-demo/` — the configured
  `music_dir` is NEVER touched in demo mode.
- Templates live at project root `/templates/`, not in the `src` tree
  (`web/main.py` walks up to project root to find them).
- Web middleware stack (outermost → innermost): `TrustedHostMiddleware` →
  `CSRFMiddleware` → optional `BasicAuthMiddleware`. See `web/security.py`.

## Sidecar persistence rules

The `.harmonist.json` sidecar holds **load-bearing** state only — fields
that drive a user-visible affordance, are required to recover from a
restart, or are read by another module's logic. Speculative or audit
metadata does NOT go in the sidecar:

- **Rate limiting / debounce timestamps** live in-memory (process-level
  token bucket or `time.sleep` in the runner). They don't need to
  survive restarts; MB's 1 req/sec is a process-wide cap, not per-album.
- **Lookup history / audit trails** belong in server logs, not the
  sidecar. Sidecar is not a log file.
- **"Nice to know later"** is not a use case. If a field has no current
  reader, it doesn't get persisted — add it back when an actual feature
  needs it.

If you find yourself adding a sidecar field with no reader, stop and
either justify it with a concrete current use case or leave it out.
This preference has been raised more than once; respect it without
needing to be reminded again.

## Test client conventions

The CSRF middleware requires `HX-Request: true` on every state-changing
method. HTMX sends this automatically in the browser; pytest's `TestClient`
does not, so existing fixtures construct it with
`TestClient(app, headers={"HX-Request": "true"})`. New web fixtures should
follow the same pattern. See `web/security.py` for the threat model.
