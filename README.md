# Harmonist

**A self-hosted music librarian that tends your collection's metadata and artwork
using [MusicBrainz](https://musicbrainz.org/), with changes previewed, logged, and
reversible.**

- **Tag all your music.** CD rips, Bandcamp purchases, and downloads from other
  stores get [Picard](https://picard.musicbrainz.org)-compatible metadata.
- **Your folders, your formats.** Keep your own directory layout and your choice
  of FLAC, MP3, AAC, ALAC, Ogg Vorbis, and Opus.
- **Link your albums to MusicBrainz** for canonical release metadata.
- **Keep your tags current.** Harmonist continually finds metadata updates on
  MusicBrainz for you to review and apply.
- **Fill missing covers and upgrade your artwork.** Compare images from your
  files and the Cover Art Archive, then apply the covers you choose.
- **Undo tag and artwork changes.** See exactly what changed, restore previous
  tags, and recover saved covers.
- **Give back to MusicBrainz.** Find opportunities in your collection to improve
  the metadata everyone shares.
- **Download and tag your Bandcamp purchases.** Sync new albums, or re-download
  favourites to upgrade audio quality or pick up newly added tracks.

Manage everything from your browser, with an **Inbox** for albums that need
attention, a **Library** to browse your collection, and an **Activity** feed to
see what's changed.

## Demo

A short walk-through of the flow — inbox triage, matching, and a link-only sync:

**This video is quite out of date; an updated walkthrough is coming soon.**

https://github.com/user-attachments/assets/dc08c85f-43f8-402a-85a5-09388200c239

## Quickstart

Try Harmonist with a sample library using Docker. No Bandcamp account or music
folders needed; the demo uses simulated services and leaves your music untouched.

```bash
docker run --rm --platform linux/amd64 \
  -p 127.0.0.1:8000:8000 \
  -e HARMONIST_DEMO_MODE=1 \
  ghcr.io/randomphrase/harmonist:latest
```

Open [localhost:8000](http://localhost:8000) and explore, or follow the
[demo walkthroughs](docs/demo.md). Press Ctrl+C to stop and remove the demo
container.

To use your own collection, follow the [installation guide](docs/installation.md).

## Documentation

- **[Usage guide](docs/usage.md)** — onboarding an existing library, working the
  inbox, syncing, the Library and its filters, an album's page, undo, activity.
- **[Installation](docs/installation.md)** — Docker, from source, demo mode,
  configuration, uninstall.
- **[Deployment & security](docs/deployment.md)** — reverse proxy, hostname
  allow-listing, built-in auth. **Read this before exposing Harmonist.**
- **[Design](docs/design.md)** — the internal spec: state machine, sidecar schema,
  tagging contract, module map. Written for people changing the code.
- **[Contributing](CONTRIBUTING.md)** — development setup, checks, and contribution
  guidelines.

## How Harmonist compares

Harmonist focuses on ongoing care of your music collection, with MusicBrainz as
the source of metadata and you in control of changes.

- **[MusicBrainz Picard](https://picard.musicbrainz.org)** handles desktop
  matching and tagging. Harmonist keeps that metadata current over time, using
  compatible tags so you can work with both.
- **[Lidarr](https://lidarr.audio)** monitors artists and downloads new releases.
  Harmonist maintains music you own and syncs your Bandcamp purchases.
- **[beets](https://beets.io)** offers a flexible command-line workflow for
  organising and tagging music. Harmonist puts library maintenance and review
  in a web interface.
- **[bandcampsync](https://github.com/meeb/bandcampsync)** downloads your Bandcamp
  purchases. Harmonist builds on it, adding MusicBrainz matching, tagging,
  artwork, and a review inbox.

## Built with AI assistance

People set Harmonist's direction, make design decisions, and test it in real use.
AI coding assistants help with implementation, backed by human review and
automated checks:

- **Every change is reviewed and approved by a human** before it lands.
- Type-checked with **mypy `--strict`** and linted/formatted with **Ruff**, both
  enforced in CI on every push.
- Automated tests run in CI across **Python 3.12, 3.13, and 3.14**, complemented
  by browser tests and manual checks of user workflows.

**Using Harmonist needs no AI.** It calls no language model and requires no AI
API key. Matching uses MusicBrainz data and explicit rules, with uncertain
matches left for you to review.

If something falls short of that bar, please open an issue.

**Tech:** Python, FastAPI, HTMX + Jinja2, Tailwind CSS, and mutagen.

## License

[GPL-3.0-or-later](LICENSE). Harmonist depends on `mutagen` (GPL), so the
combined work is GPL.

## Acknowledgements

[MusicBrainz](https://musicbrainz.org) & the [Cover Art Archive](https://coverartarchive.org),
[Harmony](https://harmony.pulsewidth.org.uk), [bandcampsync](https://github.com/meeb/bandcampsync),
and [MusicBrainz Picard](https://picard.musicbrainz.org) for the tag mappings.
