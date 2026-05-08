# Harmonist — Design

**Status:** draft for review
**Audience:** the implementation team
**Scope:** the prototype that runs locally and deploys to Synology

---

## 1. Purpose

Harmonist streamlines the workflow from *album purchased on Bandcamp* to *fully tagged file in Plex / Navidrome*, using MusicBrainz (MB) as the source of truth. It is a **workflow tool**, not a tagger. Picard-style tagging is a step we automate inside that workflow.

### Non-goals

The following are explicitly out of scope for this prototype:

- **No automatic polling of Bandcamp.** Sync is a button. The user buys infrequently and Bandcamp has no purchase webhook.
- **No in-app MusicBrainz seeding form.** When an album isn't on MB, we link out to [Harmony](https://harmony.pulsewidth.org.uk). Harmony does the seeding work.
- **No database.** State lives in `.harmonist.json` sidecars next to each album. bandcampsync's `ignores.txt` is the source of truth for "what's downloaded".
- **No C++ bindings.** The earlier readme mentioned them; they don't exist and we're not adding them.
- **No multi-user / no auth.** Single-user app behind the user's network.
- **No format conversion.** Files are downloaded in the requested format (default ALAC) and tagged in place.

---

## 2. Use cases

### 2.1 Bandcamp sync (the canonical flow)

1. User buys an album on Bandcamp out-of-band.
2. User opens Harmonist, clicks **Sync**.
3. Harmonist downloads new items via bandcampsync. For each item, it captures the public Bandcamp album URL and writes a `.harmonist.json` sidecar.
4. Inbox updates live as albums land (HTMX poll while sync is in-flight).
5. For each new album, MB lookup runs by Bandcamp URL.
6. If MB has a release linked to that URL → Harmonist tags the files Picard-compatibly. The album disappears from the inbox.
7. If MB has no match → the album sits in the inbox as **Held (Bandcamp)** with an "Open in Harmony" button and a "Recheck" button.

### 2.2 Manual ingest (non-Bandcamp music)

1. User drops an album directory into the music dir.
2. User clicks **Add Manual** in the inbox (or Harmonist offers it for a directory it sees with no sidecar).
3. User pastes an MB release URL/MBID, or uses a name-based MB search helper.
4. Harmonist writes a sidecar with `source: "manual"` and the resolved `mb_release_id`, then tags the files.

### 2.3 Held-album recheck

1. User has previously seeded a release in Harmony.
2. User clicks **Recheck** on a Held album (or **Recheck All**).
3. Harmonist re-runs the MB URL lookup. If now matched, it tags. If still unmatched, the album stays Held.

### 2.4 Re-tag from MB

1. User edits a release in MB (track titles, dates, etc.).
2. User clicks **Re-tag** on a matched album.
3. Harmonist re-fetches the MB release and rewrites the file tags. (May not ship in the prototype — captured for the next iteration.)

### 2.5 Bootstrap an existing tagged library

First-run experience for a user pointing Harmonist at a music dir that's already been Picard-tagged.

1. User clicks **Bootstrap** in the UI (chunk E adds the button; the underlying function is in `harmonist.bootstrap`).
2. **Phase A (always, no creds needed):** Harmonist walks the dir; for each album with a `MusicBrainz Album Id` atom but no `.harmonist.json` sidecar, it writes a `source="manual"` sidecar with the MBID copied from the tag and `tagged_at=now`. The library transitions from "all Orphan" to "all Done" without touching the network.
3. **Phase B (only when `cookies.txt` is present):** Harmonist loads the user's Bandcamp purchase list (no downloads), looks up each purchase's MBID via MB URL relationships, and for purchases that match an on-disk sidecar:
   - Upgrades the sidecar to `source="bandcamp"` with the bandcamp block populated.
   - Appends the `item_id` to bandcampsync's `ignores.txt` (idempotent — skips IDs already there).

The result: a subsequent **Sync** safely processes only NEW Bandcamp purchases, never re-downloading anything already on disk.

Albums that aren't on Bandcamp (CD rips, vinyl, Beatport, etc.) stay as `source="manual"` — Bootstrap leaves them alone, Sync ignores them. Bandcamp credentials remain optional throughout: the tool is fully usable for a non-Bandcamp library where the user manually assigns MBIDs (per use case 2.2).

---

## 3. State machine

Every album in the music dir is in exactly one state, derived from the presence/contents of its `.harmonist.json` sidecar plus the file tags.

| Sidecar | `mb_release_id` | `mb_match_candidate` | Files tagged | State | Inbox? | UI affordances |
|---|---|---|---|---|---|---|
| absent | — | — | — | **Orphan** | yes | "Add Manual" / "Recover Bandcamp URL" |
| present, `source=bandcamp` | null | null | n/a | **Held (Bandcamp)** | yes | "Open in Harmony", "Recheck" |
| present, `source=manual` | null | null | n/a | **Held (Manual)** | yes | MB search helper / paste MBID |
| present | null | set | n/a | **Needs Confirmation** | yes | side-by-side: files vs MB release with per-track green/yellow length indicators; "Confirm" / "Reject" buttons |
| present | set | n/a | no | **Tagging** (transient) | yes (briefly) | spinner |
| present | set | n/a | yes | **Done** | no | (hidden — visible in "All" tab if we ship one) |

**Transitions are idempotent.** Running sync, recheck, or tag twice on the same album is safe and produces the same result.

The "Ambiguous" state from the old readme is gone for Bandcamp sources (URL → MB lookup is exact). It only resurfaces inside the Manual search helper, where it's a UI concern, not a system state.

### Match confidence (when MB has the URL but the files might not match)

A URL → MBID match from MusicBrainz is exact, but the local files on disk might not be the same release variant the user has on Bandcamp (different mastering, bonus tracks, single-disc edit, etc.). Before auto-tagging, the orchestrator runs a confidence check (`harmonist.match.assess_match`):

- **Exact:** file count matches MB track count AND every per-track duration is within ±4 seconds of MB's recorded length. Auto-promote: write `mb_release_id`, run tagger, transition to Tagging → Done with no user intervention.
- **Approximate:** file count matches but at least one track length differs significantly. Stash the candidate MBID + per-track diff in `mb_match_candidate`; do NOT tag. State becomes Needs Confirmation. UI surfaces a Picard-style side-by-side with green/yellow per-track indicators and Confirm/Reject buttons.
- **No match:** file count differs from MB track count. Treated like Approximate from the user's perspective (still Needs Confirmation, still requires explicit Confirm) but the side-by-side has to handle uneven rows.

Confirm → promote candidate to `mb_release_id`, clear candidate, run tagger.
Reject → clear candidate, drop back to Held (Bandcamp).

Tracks where MB has no recorded length are shown as "unknown" (gray) and don't trigger downgrade on their own, but they don't get to vote for "exact" either — an album with all-unknown lengths and matching count is treated as Approximate.

---

## 4. Sidecar JSON schema

File: `<album_dir>/.harmonist.json`. UTF-8, two-space indent, written atomically (write-tmp-then-rename).

```json
{
  "schema_version": 1,
  "source": "bandcamp",
  "bandcamp": {
    "url": "https://myartist.bandcamp.com/album/my-album",
    "item_id": 67890,
    "band_id": 12345
  },
  "downloaded_at": "2026-05-05T12:34:56Z",
  "mb_release_id": null,
  "mb_last_checked_at": null,
  "mb_lookup_history": [
    { "at": "2026-05-05T12:35:01Z", "result": "no_match" }
  ],
  "tagged_at": null,
  "notes": null
}
```

**Field rules:**

- `schema_version` is mandatory; the loader rejects unknown versions for now.
- `source` is one of `"bandcamp"` | `"manual"`.
- `bandcamp` block is required when `source == "bandcamp"`, omitted otherwise.
- `mb_release_id` is the MBID string when matched; `null` when held.
- `mb_lookup_history` is bounded to the last 10 entries; rotates.
- All timestamps are ISO 8601 UTC with `Z` suffix.

**Manual variant:**

```json
{
  "schema_version": 1,
  "source": "manual",
  "added_at": "2026-05-05T13:00:00Z",
  "mb_release_id": "abc-123-...",
  "mb_last_checked_at": "2026-05-05T13:00:01Z",
  "tagged_at": "2026-05-05T13:00:02Z",
  "notes": null
}
```

---

## 5. Tagging contract (Picard-compatible)

The tagger writes the full set of MBID atoms on MP4/M4A files plus a refresh of standard text tags from the MB release payload. This is what makes Plex and Navidrome treat the album as MB-tagged.

### MP4 atom names (Picard convention — note: spaces, not underscores)

Per-album (same on every track):

- `----:com.apple.iTunes:MusicBrainz Album Id` — release MBID
- `----:com.apple.iTunes:MusicBrainz Album Artist Id` — release-artist MBID(s)
- `----:com.apple.iTunes:MusicBrainz Release Group Id`
- `----:com.apple.iTunes:MusicBrainz Album Type`
- `----:com.apple.iTunes:MusicBrainz Album Status`
- `----:com.apple.iTunes:MusicBrainz Album Release Country`

Per-track:

- `----:com.apple.iTunes:MusicBrainz Track Id` — recording MBID
- `----:com.apple.iTunes:MusicBrainz Release Track Id` — release-track MBID
- `----:com.apple.iTunes:MusicBrainz Artist Id` — track-artist MBID(s)

Standard text tags refreshed from MB:

- `©nam` (title), `©alb` (album), `©ART` (artist), `aART` (album artist)
- `©day` (date), `©gen` (genre — first MB tag), `cprt` (copyright if present)
- `trkn` (track / total), `disk` (disc / total)
- `----:com.apple.iTunes:LABEL`, `----:com.apple.iTunes:CATALOGNUMBER`, `----:com.apple.iTunes:BARCODE`, `----:com.apple.iTunes:MEDIA`, `----:com.apple.iTunes:ASIN` when present

The existing `©cmt` (Bandcamp comment) is **preserved** if present — it's the fallback URL recovery path and other tools may rely on it. We never strip user data.

The current code's `MUSICBRAINZ_RELEASEID` atom is **non-Picard** and gets removed by the tagger when it writes the correct atoms.

### Cover art (mandatory)

Plex with the MusicBrainz agent can fetch its own artwork from external sources, but **Navidrome does not** — it reads from embedded tags and `cover.jpg` only. Navidrome is the strict consumer; we design for it.

**The tagger always:**

1. Fetches the front cover from the [Cover Art Archive](https://coverartarchive.org) using the MB release ID:
   - `GET https://coverartarchive.org/release/{mbid}/front` (follows redirects to the actual image)
   - If unavailable, falls back to `release-group/{mbgid}/front` (release-group-level art).
   - If still unavailable, the album is tagged but with no cover; logged as a warning, surfaced in the inbox.
2. Embeds the image in every track's `covr` atom (`mutagen.mp4.MP4Cover` with `FORMAT_JPEG` or `FORMAT_PNG`).
3. Writes the same image to `<album_dir>/cover.jpg` (or `.png`, matching format) for tools that prefer the sidecar (Navidrome, MPD, foobar2000, etc.).

**Resolution policy:** `original` (full CAA resolution). Lossless audio is the dominant cost in this library; an extra 10 MB of cover art per album is negligible by comparison. Configurable via `cover_art_size` in `harmonist.toml` (`250 | 500 | 1200 | original`) so a constrained deployment can downsize, but this is not the primary use case. Library-wide cover-art optimisation (clipping / recompressing) is a separate, future enhancement — not in scope here.

**Caching:** the downloaded image goes to `<album_dir>/cover.<ext>` first, and the embed step reads it from there. This means re-tagging an album doesn't refetch CAA, and the user can manually replace `cover.jpg` to override the embedded art on next retag.

---

## 6. Module map

```
src/harmonist/
  config.py            NEW   env-var + TOML config loader, single source of truth
  models.py            NEW   Album dataclass, AlbumState enum, Sidecar dataclass
  sidecar.py           NEW   read/write/migrate .harmonist.json atomically
  scanner.py           REPLACE  walk music dir → list of Albums (sidecar-driven, not tag-driven)
  bandcamp_hook.py     NEW   Syncer subclass; intercepts post-download, writes sidecar
  mb_lookup.py         NEW   MB URL-relationship lookup; full-release fetch for tagging
  mb_search.py         NEW   name-based search helper (manual path only)
  match.py             NEW   compare local files to MB release: confidence + per-track deltas
  bootstrap.py         NEW   first-run library import: derive sidecars from MB tags + reconcile w/ Bandcamp purchases
  cover_art.py         NEW   Cover Art Archive fetch, resize, cache, write to album dir
  tagger.py            NEW   Picard-compatible tag writer (incl. embedded covr atom)
  url_recovery.py      NEW   fallback: reconstruct Bandcamp album URL from ©cmt artist URL
  web/
    main.py            REWRITE  FastAPI routes
    sync_runner.py     NEW   in-process job runner with status polling
  fixtures/            NEW   small ALAC fixtures + sample sidecars for tests

DELETE:
  src/harmonist/syncer.py                       (replaced by bandcamp_hook.py)
  src/harmonist/bandcamp_scraper.py             (Harmony does seeding)
  src/harmonist/mb_searcher.py                  (replaced by mb_lookup.py + mb_search.py)
  src/harmonist/setup_demo.py                   (replaced by fixtures/)
  src/harmonist/web/templates/index.html        (stub; real templates are at root)
  templates/partials/verify_seeding.html        (no in-app seeding)
  readme.md~ , .envrc~                          (cruft)
```

---

## 7. Configuration

### Env vars (highest precedence)

| Variable | Default (Docker) | Default (local) |
|---|---|---|
| `HARMONIST_CONFIG_DIR` | `/config` | `~/.config/harmonist` |
| `HARMONIST_MUSIC_DIR` | `/music` | `./music` |
| `HARMONIST_DOWNLOAD_FORMAT` | `alac` | `alac` |
| `HARMONIST_HOST` | `0.0.0.0` | `127.0.0.1` |
| `HARMONIST_PORT` | `8000` | `8000` |
| `HARMONIST_MAX_DOWNLOADS_PER_SYNC` | `5` | `5` |
| `HARMONIST_TEST_MODE` | unset | unset |
| `HARMONIST_LOG_LEVEL` | `info` | `info` |
| `PUID` / `PGID` | unset (root) | n/a |

### Config file (`${CONFIG_DIR}/harmonist.toml`, optional, env vars win)

```toml
[paths]
music_dir = "/music"

[bandcamp]
download_format = "alac"
max_downloads_per_sync = 5
ignores_file = "/config/ignores.txt"
cookies_file = "/config/cookies.txt"

[musicbrainz]
user_agent = "Harmonist/0.1 ( harmonist@girtby.net )"

[server]
host = "0.0.0.0"
port = 8000

[test]
mode = "fixture"   # fixture | cassette | live
unignore_item_ids = []
```

Validation runs at startup via Pydantic; the app refuses to start with an invalid config.

---

## 8. HTTP API surface

All routes return HTML fragments unless noted. JSON only for `/healthz`, `/sync/status`.

| Method | Route | Purpose | Response |
|---|---|---|---|
| GET  | `/` | Inbox page | full HTML |
| GET  | `/tasks` | Inbox content (tasks partial) | HTML fragment |
| POST | `/sync` | Start sync | HTML fragment + `HX-Trigger: sync-started` |
| GET  | `/sync/status` | Poll sync state | JSON `{state, progress, current_item}` |
| POST | `/tag/{album_id}/{mbid}` | Apply tagging with given MBID | HTML fragment |
| POST | `/recheck/{album_id}` | Re-run MB lookup for held album | HTML fragment |
| POST | `/recheck` | Recheck all held | HTML fragment |
| POST | `/manual/{album_id}` | Open manual MBID entry/search | HTML fragment |
| POST | `/manual/{album_id}/search` | Run name-based MB search helper | HTML fragment |
| POST | `/recover/{album_id}` | Try to recover Bandcamp URL for an Orphan | HTML fragment |
| GET  | `/healthz` | Health for Docker | JSON |
| GET  | `/static/...` | Static assets (cover art via symlink to music dir is one option) | binary |

Album IDs remain MD5-of-path (matches existing convention; survives across runs as long as the album doesn't move).

---

## 9. UX flows

### 9.1 Live sync

- User clicks **Sync**. Button POSTs to `/sync`.
- Server returns a "Sync running…" fragment with `hx-trigger="every 1500ms"` polling `/sync/status` and a sibling polling `/tasks`.
- Each /tasks fetch re-renders the inbox; albums appear as bandcampsync writes them and the per-album MB lookup runs.
- When `/sync/status` returns `state == "idle"` post-run, the polling stops and the button re-enables.
- Cap: `max_downloads_per_sync` aborts the sync with a visible error if the would-download set exceeds it.

### 9.2 Held → Recheck

- Card has an "Open in Harmony" link (`https://harmony.pulsewidth.org.uk/release?url=<bandcamp_url>`) and a "Recheck" button.
- Recheck POSTs to `/recheck/{id}`. On success, the card swaps into the Tagging spinner, then disappears (album moves to Done).

### 9.3 Manual ingest

- Orphan card has "Add Manual" → opens an inline form.
- Form takes either a full MB release URL/MBID *or* runs the search helper (`/manual/{id}/search?artist=...&title=...`) and presents matches to pick.
- On selection, POST to `/tag/{id}/{mbid}`.

---

## 10. Deployment

### 10.1 Dockerfile (sketch)

- Base: `python:3.12-slim` (slim, glibc, multi-arch).
- Two-stage build: build wheels in stage 1, copy into runtime in stage 2.
- Entrypoint: small shell script that handles `PUID`/`PGID` and `exec uvicorn`.
- Healthcheck: `CMD curl -fsS http://127.0.0.1:${HARMONIST_PORT}/healthz || exit 1`.
- Multi-arch via `docker buildx --platform linux/amd64,linux/arm64`.

### 10.2 Volume layout (the contract)

```
host:/volume1/docker/harmonist/config   →  container:/config
host:/volume1/music                     →  container:/music
```

Sidecars live next to music inside `/music`. Config dir holds `ignores.txt`, `cookies.txt`, optional `harmonist.toml`.

### 10.3 Run recipes

**Synology (compose):**
```yaml
services:
  harmonist:
    image: harmonist:latest
    restart: unless-stopped
    ports: ["8000:8000"]
    volumes:
      - /volume1/docker/harmonist/config:/config
      - /volume1/music:/music
    environment:
      PUID: 1026
      PGID: 100
```

**macOS local dev:**
```bash
HARMONIST_MUSIC_DIR=$HOME/Music/harmonist-dev \
HARMONIST_CONFIG_DIR=$HOME/.config/harmonist \
uvicorn harmonist.web.main:app --reload
```

**Pi dev (Synology share over SMB):**
```yaml
services:
  harmonist:
    image: harmonist:latest
    volumes:
      - ./config:/config
      - /mnt/synology-music:/music   # mounted via /etc/fstab
    ports: ["8000:8000"]
```

---

## 11. Testing strategy

QA is a first-class agent role. The flagship test is the live sync flow end-to-end.

### 11.1 Test pyramid

```
              ┌────────────────────────┐
              │  Live (opt-in, manual) │   real Bandcamp + real MB
              │   1 album, 1 path      │
              └────────────────────────┘
            ┌────────────────────────────┐
            │  E2E (cassette + fixtures) │   sync flow with recorded HTTP
            │       ~5 scenarios          │
            └────────────────────────────┘
          ┌──────────────────────────────────┐
          │  Integration (TestClient)         │   FastAPI routes × demo paths
          │           ~20 tests                │
          └──────────────────────────────────┘
        ┌────────────────────────────────────────┐
        │  Unit                                   │   per module
        │           ~60 tests                     │
        └────────────────────────────────────────┘
```

### 11.2 Test modes (selected via `HARMONIST_TEST_MODE`)

- **`fixture`** — purely local. No network. Fixtures in `src/harmonist/fixtures/`. Default for `pytest`.
- **`cassette`** — replays recorded HTTP via `pytest-recording` (VCR) for MB and Bandcamp. Default for CI.
- **`live`** — hits real services. Opt-in. Uses `unignore_item_ids` from config to pick test targets. **Always uses a temp copy of the ignores file**, never the user's real one.

### 11.3 Selective live testing (Bandcamp citizenship)

The live mode workflow:
1. Read user's real ignores file (read-only).
2. Copy to a temp file.
3. Remove the entries listed in `[test].unignore_item_ids` from the temp copy.
4. Point bandcampsync at the temp copy and a sandbox music dir.
5. Run sync, assert state, clean up.
6. **Hard cap:** if the would-download count exceeds `HARMONIST_MAX_DOWNLOADS_PER_SYNC`, abort with a clear error before any download starts.

### 11.4 Fixtures

Committed to `src/harmonist/fixtures/`:

- 3 ALAC `.m4a` files (~50 KB each, generated from a sine wave via `ffmpeg`). Royalty-free, deterministic.
- Sample sidecars covering each state: orphan (none), held-bandcamp, held-manual, tagged.
- A captured Bandcamp collection-items API response (anonymised; real `url_hints` shapes).
- Captured MB URL-lookup responses (matched + unmatched).
- Captured MB release-fetch response with full release data.

The integration test must be hermetic — it must pass on a clean checkout without anything from the user's filesystem. The current dependency on `/Users/alastair/Music/Traktor/02 Declino.m4a` is the canonical example of what we won't do again.

### 11.5 Flagship test (must pass before "prototype" is declared done)

```
test_live_sync_flow_end_to_end (cassette mode):
  given: empty music dir, ignores with 359/360 entries, 1 unignored
  when:  POST /sync, poll until /sync/status is idle
  then:  exactly 1 album appears in /tasks
         sidecar exists with bandcamp.url populated
         MB lookup ran (assert via cassette interaction)
         either tagged (Done, not in /tasks) or Held with Harmony URL
  cleanup: scrub temp dirs
```

The same scenario runs in `live` mode manually before each release, against a single chosen `item_id` from the user's real collection.

### 11.6 Manual test plan

A checklist in `docs/manual-tests.md` (separate doc, owned by QA):

- Sync flow against real Bandcamp on macOS
- Sync flow against real Bandcamp on Pi (over SMB-mounted Synology share)
- Tag write over SMB doesn't corrupt files; Plex picks up the MBID
- Held → Recheck after seeding in Harmony eventually transitions to Done
- Manual ingest with a non-Bandcamp album

---

## 12. Build order

Each step ends with green tests at its level. No agent moves on until QA signs off.

1. **Foundations** — `config.py`, `models.py`, `sidecar.py`. Unit tests for sidecar I/O (round-trip, atomic writes, schema rejection).
2. **Tagger** — `tagger.py` against ALAC fixture files. Unit tests assert exact atom set written + re-read. This blocks nothing else and is independently valuable.
2a. **Cover art** — `cover_art.py`: CAA fetch, resize to configured size, write `cover.jpg` + embed `covr`. Cassette tests for matched / no-art / release-group fallback. Tagger gets extended to call this.
3. **Scanner rewrite** — sidecar-driven. Unit tests across all states (orphan / held / tagged) with fixture trees.
4. **MB lookup** — `mb_lookup.py` URL-relationship endpoint + full-release fetch. Cassette tests for matched / unmatched / API error.
5. **bandcampsync hook** — `bandcamp_hook.py`. Unit tests against a mocked `BandcampItem` shape; integration test against a fixture collection-items response.
6. **Web layer** — FastAPI routes, `sync_runner.py`, templates. Integration tests via `TestClient` covering each route × each state.
7. **UX live updates** — HTMX polling, status indicator, sync button states. Browser-tested manually.
8. **Manual + recheck flows** — `mb_search.py`, manual-entry templates, recheck routes.
9. **URL recovery fallback** — `url_recovery.py` for Orphans with only a `©cmt` artist URL. Lower priority.
10. **Containerise** — Dockerfile, multi-arch buildx, healthcheck, PUID/PGID entrypoint, compose recipes.
11. **Flagship E2E test** — cassette-mode test of the full sync flow. Becomes the gate for "prototype done".
12. **Manual test pass** — QA runs through `docs/manual-tests.md` on macOS and Pi.

---

## 13. Open questions

- **Cover art serving:** the inbox UI references covers via `/static/music/...`. Simplest path is a FastAPI mount of the music dir, scoped to image files only. Decision pending.
- **Cover art library optimisation:** future enhancement, not in scope here. If the library grows big enough to matter, a separate batch tool can downsize covers across all albums. Keep that out of the tagger's hot path.
- **Multiple cover art types:** CAA has front, back, booklet, etc. Prototype embeds front only and stops there. Other types deferred.
- **Re-tag cover behaviour:** if user has manually replaced `cover.jpg`, do we re-fetch from CAA on retag (overwriting their choice) or trust the local file? Current design trusts local; flag in the manual-test plan.
- **MB rate limiting:** musicbrainzngs imposes 1 req/sec by default. For batch tagging across many tracks during a single match, we may need to sequence carefully. Probably fine for the prototype's scale.
- **"Re-tag from MB" use case** — defer to v2?
- **Single-writer assumption on the ignores file** — if the user runs bandcampsync standalone outside the container, are concurrent writes possible? In practice almost certainly no, but worth flagging.
- **Backup before tag write?** Optionally write `<file>.bak` before mutagen.save() during the prototype phase, removable by config later. QA's call.
