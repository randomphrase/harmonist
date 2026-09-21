# Harmonist

**A self-hosted metadata librarian that tends your existing music collection,
with canonical release metadata from [MusicBrainz](https://musicbrainz.org).**

Adopt a library collected over years, find outdated or missing tags and artwork,
preview improvements, and inspect or undo changes. Harmonist writes
[Picard](https://picard.musicbrainz.org)-compatible tags, works with your existing
folders, and **asks before it guesses**. It also syncs your Bandcamp purchases.

## What it does

- **Adopt and maintain an existing library**, including non-Bandcamp albums,
  mixed audio formats, and releases split across disc folders. MusicBrainz
  supplies release metadata; your personal genre tags and comments are preserved.
- **Find metadata improvements** with optional background checks, preview the
  changes, and apply the updates you choose. Inspect and update artwork separately.
- **Sync** your Bandcamp library (via [bandcampsync](https://github.com/meeb/bandcampsync)),
  capturing each album's store URL — and **re-download** any album later to upgrade
  its files or pick up tracks the artist has added, with the old copy zipped aside.
- **Auto-match** each download against MusicBrainz by its Bandcamp URL. An exact
  match is tagged and filed automatically; anything ambiguous — or not yet in
  MusicBrainz — lands in a tidy inbox.
- **A task-oriented inbox** groups albums by what they need: a MusicBrainz ID, a
  review of an approximate match, or a sync to link the purchase. Seed any missing
  releases straight to [Harmony](https://harmony.pulsewidth.org.uk).
- **Picard-compatible tagging** across `.m4a`, `.mp3`, `.flac`, `.ogg`, and
  `.opus`, embedding the MusicBrainz Release ID and cover art.
- **A library view** that finds the albums that are finished but not *right* —
  incomplete, partially tagged, or missing artwork — and compares any album's
  tags and tracklist against MusicBrainz, field by field.
- **A full record of every change**, and a way back from it: Harmonist never edits
  your files silently, and every tagging can be undone (see
  [Nothing happens silently](#nothing-happens-silently) and
  [Nothing is a one-way door](#nothing-is-a-one-way-door)).

The UI is a single page with **Inbox / Library / Activity** tabs, built with
HTMX — no SPA, no build step at runtime.

## Demo

A short walk-through of the flow — inbox triage, matching, and a link-only sync:

https://github.com/user-attachments/assets/dc08c85f-43f8-402a-85a5-09388200c239

Or try it yourself against a sandboxed sample library, with no Bandcamp account
and no real network traffic:

```bash
HARMONIST_DEMO_MODE=1 uvicorn harmonist.web.main:app --reload
```

[Three focused walkthroughs](docs/demo.md): adopt an existing library, link and
sync Bandcamp purchases (including MP3-to-FLAC re-download), and review/apply/undo
tag and artwork updates.

## Quickstart

```yaml
# docker-compose.yml — edit the two paths and the uid:gid, then `docker compose up -d`
services:
  harmonist:
    image: ghcr.io/randomphrase/harmonist:latest
    restart: unless-stopped
    ports: ["8000:8000"]
    volumes:
      - /path/to/music:/music     # your music library
      - /path/to/config:/config   # settings, cookies, history
    user: "1000:1000"             # the OWNER of those two dirs
```

Then visit `http://<host>:8000`. Full instructions — permissions, Synology/ACL
shares, running from source, configuration — are in
**[docs/installation.md](docs/installation.md)**.

## Documentation

- **[Usage guide](docs/usage.md)** — onboarding an existing library, working the
  inbox, syncing, the Library and its filters, an album's page, undo, activity.
- **[Installation](docs/installation.md)** — Docker, from source, demo mode,
  configuration, uninstall.
- **[Deployment & security](docs/deployment.md)** — reverse proxy, hostname
  allow-listing, built-in auth. **Read this before exposing Harmonist.**
- **[Design](docs/design.md)** — the internal spec: state machine, sidecar schema,
  tagging contract, module map. Written for people changing the code.

## Where Harmonist fits

Harmonist maintains the metadata of music you already own, whether it came from
a CD rip, Bandcamp, or another store. It recognises existing MusicBrainz tags,
finds improvements, and lets you review and apply them from a self-hosted web UI.
Optional background checks look and report; applying those updates is currently
a user decision.

MusicBrainz is the canonical source for release metadata: Harmonist is not a
generic tag editor and keeps no private overrides of that metadata. A directory
must not mix albums, but one release can span several folders. Existing personal
tags outside Harmonist's ownership, including genre and comments, are preserved.

For Bandcamp purchases, the store URL provides another route to identifying the
release through MusicBrainz relationships. Confident matches can be tagged after
download automatically; ambiguous ones go to the Inbox. A release missing from
MusicBrainz can be seeded through [Harmony](https://harmony.pulsewidth.org.uk).

- **[MusicBrainz Picard](https://picard.musicbrainz.org)** is the gold-standard
  *manual* desktop tagger — you cluster and match files by hand. Harmonist
  maintains an identified library and writes the **same
  Picard-compatible tags**, so your files stay fully Picard-editable. Reach for
  Picard for manual matching and editing; use Harmonist for ongoing maintenance
  and routine purchase ingestion.
- **[Lidarr](https://lidarr.audio)** (the *arr suite) is a broad collection
  manager — it monitors artists and pulls releases from various indexers to
  grow a library. Harmonist focuses on maintaining the music already in your
  collection and syncing your Bandcamp purchases.
- **[beets](https://beets.io)** is a powerful CLI library manager and
  autotagger, and **[beetcamp](https://github.com/snejus/beetcamp)** extends it
  by using *Bandcamp itself* as a metadata source. That's a great combination —
  but note it approaches the problem from the opposite direction: beetcamp
  treats Bandcamp pages as the source of truth, while Harmonist resolves each
  purchase's store URL directly to its MusicBrainz release, so you get canonical
  release IDs, community-curated metadata, and files that stay consistent with
  the rest of a Picard-tagged library. Harmonist also trades the command line
  for a self-hosted web UI built around library maintenance, and keeps a human in
  the loop — it asks before it guesses rather than auto-applying a best
  match. If you already live in beets, bandcampsync + beets + beetcamp is a
  solid pipeline; Harmonist is the integrated, review-first alternative.
- **[bandcampsync](https://github.com/meeb/bandcampsync)** handles the
  *download* half of this problem so well that Harmonist builds directly on it
  (see Acknowledgements). Related projects like
  **[bandcamp-sync-flask](https://github.com/subdavis/bandcamp-sync-flask)**
  wrap it in a one-click web trigger, and
  **[bandcamp-collection-downloader](https://framagit.org/Ezwen/bandcamp-collection-downloader)**
  covers the same ground as a standalone CLI. All of these get your purchases
  onto disk; none of them tag. Harmonist adds the MusicBrainz matching, the
  review inbox, and the Picard-compatible tagging on top.

## Nothing happens silently

Harmonist edits files you care about — it rewrites tags, saves cover art and
moves downloads into place, often while you're not watching. So every one of
those changes is recorded, and the record is meant to be *read*, not just kept.

The **Activity feed** shows outcomes in plain language ("Tagged", "Unlinked",
"Auto-tagged after sync"). Each entry that changed something on disk expands to
show precisely what:

```
14:22:07  ●  Boards of Canada — Geogaddi · Tagged     ▸ what changed · 4
                 tag.album  release=… tracks=12 art=embedded mode=full
                 tag.track  file="01 Ready Lets Go.m4a" track=1 …
                 sidecar.update  mbid=None->2f0e…
                 cover.write  file=cover.jpg source=caa overwrote=False
```

It's durable (SQLite in your config dir, nothing pruned), complete (down to
sidecar rewrites, file moves and every album that loses a sidecar), attributable
to the action that caused it, still readable after an album is re-matched or
renamed — and honest when it *can't* answer, because "nothing happened" and "I
couldn't tell you" are different claims and only one of them is ever a guess.
[More in the usage guide](docs/usage.md#activity).

## Nothing is a one-way door

Recording a change is half the promise; being able to take it back is the other
half. Every tagging is stored field by field — the value before and the value
after, for each file — so an album's **Album** section shows what differs from
MusicBrainz, its **History** shows what each tagging actually changed, and
**Undo tag changes** puts those values back.

The undo is careful about what it refuses. The tagging is the unit, not the
field, because reverting one field while its neighbours keep the new value would
build a state your files never had. A field you've changed since — in Picard, or
by a later re-tag — is left alone and named in the outcome, so an old row is safe
to offer rather than a trap. Every file is read before any is written, so a
failure leaves the album as it was.

Undoing the tagging that linked an album to MusicBrainz unlinks it too, so your
files and Harmonist's own record can't quietly disagree: the album returns to
Needs MBID with that release kept as a one-click suggestion. The undo is itself
recorded, so it can be undone in turn. Cover art has its own Undo, from a bounded
store of the images that re-tags have overwritten.
[More in the usage guide](docs/usage.md#undoing-a-tagging).

## How it's built

Harmonist is built with extensive help from an AI coding assistant (Claude),
with rigorous automated and manual verification and review to hold it to a
production-quality bar:

- **Every change is reviewed and approved by a human** before it lands.
- Type-checked with **mypy `--strict`** and linted/formatted with **Ruff**, both
  enforced in CI on every push.
- An extensive test suite — **~91% line coverage** (`make coverage`), run in CI
  across Python 3.12 / 3.13.

**Using Harmonist needs no AI.** It calls no language model and needs no API key
at runtime: matching is a deterministic MusicBrainz URL lookup, by design. AI
helped *build* it; it has no part in *running* it — which is exactly the idea
behind "asks before it guesses": exact, auditable matches, never a model's guess.

If something falls short of that bar, please open an issue.

**Tech:** Python 3.12+, FastAPI, HTMX + Jinja2, Tailwind CSS (via
`pytailwindcss`), `mutagen`, `musicbrainzngs`, `bandcampsync`, `httpx` +
BeautifulSoup, Pydantic, `tomlkit`.

## Contributing

```bash
make check     # ruff lint + format check + mypy --strict + pytest
make css       # rebuild static/harmonist.css (Tailwind v4, no Node)
```

CI runs the same gate on Python 3.12 / 3.13 plus a CSS-drift check. See
[CONTRIBUTING.md](CONTRIBUTING.md).

## License

[GPL-3.0-or-later](LICENSE). Harmonist depends on `mutagen` (GPL), so the
combined work is GPL.

## Acknowledgements

[MusicBrainz](https://musicbrainz.org) & the [Cover Art Archive](https://coverartarchive.org),
[Harmony](https://harmony.pulsewidth.org.uk), [bandcampsync](https://github.com/meeb/bandcampsync),
and [MusicBrainz Picard](https://picard.musicbrainz.org) for the tag mappings.
