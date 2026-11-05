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
- **Satisfy your inner perfectionist.** Find the gaps and inconsistencies hiding
  in your collection with [focused library filters](docs/how-it-works.md#the-library).
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
docker run --rm \
  -p 127.0.0.1:8000:8000 \
  -e HARMONIST_DEMO_MODE=1 \
  ghcr.io/randomphrase/harmonist:latest
```

Open [localhost:8000](http://localhost:8000) and explore. Press Ctrl+C to stop
and remove the demo container.

To use your own collection, see [getting started](docs/getting-started.md).

## Documentation

- **[Getting started](docs/getting-started.md)**: install, adopt your library,
  set up Bandcamp sync.
- **[How it works](docs/how-it-works.md)**: the ideas behind what Harmonist does.
- **[Troubleshooting](docs/troubleshooting.md)**: problems and their fixes.
- **[Configuration](docs/configuration.md)**: every setting.
- **[Security](docs/security.md)**: read this before exposing Harmonist beyond
  your own machine.

Want to help, with or without code? See [contributing](CONTRIBUTING.md). Changing
the code? See the [design](docs/design/README.md) too.

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

## Where Harmonist is going

The aim is rich metadata kept in the library itself: in your files' tags, and in
file-based standards like Kodi's `artist.nfo`. Metadata that lives with the music
leaves you free to choose any music server or player. Tagging should be
satisfying, and ideally fun.

What Harmonist won't become is a general-purpose tagger. Its metadata and
artwork come from shared databases, MusicBrainz and the Cover Art Archive first.
When those are wrong or missing something, the fix belongs there, where it helps
every library and not just yours. Harmonist points you to those fixes rather
than patching around them locally. Hand edits are what
[Picard](https://picard.musicbrainz.org) is for, and Harmonist keeps whatever it
doesn't manage.

Some ideas for where it could go next:

- Kodi-style `artist.nfo` files, identified and kept up to date
- Genres from MusicBrainz, without churning the genres already in your files
- Lyrics, including synchronized lyrics, from a source other than MusicBrainz
  (which doesn't carry them), reviewed before they're written
- Picard-compatible file renaming
- Purchase sync from more stores
- More metadata sources, such as Discogs, AllMusic and Wikipedia
- More file-based metadata standards

## Human-Designed, Built With AI Assistance

Humans set Harmonist's direction, make design decisions, and test it in real use.
AI coding assistants help with implementation, backed by human review and
automated checks:

- **Every change is reviewed and approved by a human** before it lands.
- Type-checked with **mypy `--strict`** and linted/formatted with **Ruff**, both
  enforced in CI on every push.
- Automated tests run in CI across **Python 3.14 and 3.15**, complemented
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
