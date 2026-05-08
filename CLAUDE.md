# Harmonist

Semi-automated music metadata tool: Bandcamp purchases → MusicBrainz → Plex.

## What it does

Scans a local music directory of `.m4a` files, looks each album up on MusicBrainz, and presents a task-oriented UI to resolve ambiguities or seed missing releases. Once matched, it tags files with the MusicBrainz Release ID.

## Tech Stack

- **Backend:** Python 3.12+, FastAPI, `mutagen` for audio tag I/O
- **Frontend:** HTMX + Jinja2 templates, Tailwind CSS (CDN)
- **MusicBrainz:** `musicbrainzngs`
- **Bandcamp:** `bandcampsync` + custom `httpx`/`BeautifulSoup` scraper

## Project Layout

```
src/harmonist/
  web/main.py          # FastAPI app — all routes
  scanner.py           # Walk music dir, build Album objects from .m4a tags
  metadata.py          # Album model + tag_with_mbid()
  mb_searcher.py       # MusicBrainz search (real + Mock)
  bandcamp_scraper.py  # Bandcamp scraper (real + Mock)
  syncer.py            # bandcampsync wrapper
  setup_demo.py        # Seed demo data into music_demo/
templates/             # Jinja2/HTMX templates (root-level, NOT src/harmonist/web/templates/)
test/
  test_workflow.py     # Full end-to-end integration test
music_demo/            # Demo music files (gitignored)
```

## Running

```bash
# Dev server (real mode)
uvicorn harmonist.web.main:app --reload

# Demo mode (uses MockSearcher + MockScraper, music_demo/ dir)
DEMO_MODE=1 uvicorn harmonist.web.main:app --reload
```

## Tests

```bash
pytest test/
# or
make test
```

## CSS build

Tailwind v4 standalone via the `pytailwindcss` Python wrapper (no Node).
The build output `static/harmonist.css` is committed; re-run after editing
templates so new utility classes are included:

```bash
make css           # one-shot minified build
make css-watch     # watch + rebuild on save
```

The integration test (`test_workflow.py`) requires a real `.m4a` template file at either `music/Album1/track1.m4a` or `/Users/alastair/Music/Traktor/02 Declino.m4a`. Without it, `create_dummy_m4a` creates an empty file that `mutagen` can't read.

## Album States

| State | Condition | User Action |
|---|---|---|
| Needs Seeding | No MB matches found | Review metadata, push seed |
| Ambiguous | Multiple MB matches | Pick the right one |
| Pending Sync | Seeded, awaiting MB indexing | Wait / force refresh |
| Matched | Has MUSICBRAINZ_RELEASEID tag | None |

## Key Conventions

- Albums are identified by an MD5 hash of their directory path (`album.id`)
- `.m4a` tag for MBID: `----:com.apple.iTunes:MUSICBRAINZ_RELEASEID`
- Bandcamp URL is stored in the `©cmt` comment tag and used as the Bandcamp album link
- `DEMO_MODE=1` env var enables mocks — `MockSearcher` returns results only for "Beatles"; `MockScraper` always returns fake metadata
- In-memory `SEARCH_CACHE` in `main.py` avoids re-querying MB on every `/tasks` load
- Templates live at project root `/templates/`, not in the `src` tree
