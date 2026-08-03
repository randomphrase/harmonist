# Changelog

User-visible changes to Harmonist, newest first — read this on upgrade.
Format loosely follows [Keep a Changelog](https://keepachangelog.com);
versions follow [semantic versioning](https://semver.org).

## [Unreleased]

### Upgrading

- **Nothing to do** about `ignores.txt` — existing entries keep working. Choices
  made before this release sit below the file's separator line, so they won't
  appear in the new "Won't download" list; move a line above the separator if you
  want it listed. **Don't delete the file:** albums you've downloaded are
  recognised from their sidecars, but a "Don't download" choice exists nowhere
  else, and removing it lets the purchase download again.

### Added

- Settings now lists the purchases you told Harmonist not to download, with a
  Restore button — an ignore is no longer a one-way door. Only choices you made
  yourself are listed, never the albums Harmonist has already downloaded.
- Activity entries written by a sync or by auto-reconcile now name and link their
  album too — including "Auto-tagged … after sync", the one worth clicking when
  Harmonist has tagged something on its own initiative. Previously only entries
  from actions you took yourself were linked.

### Fixed

- Deciding the same purchase twice no longer adds a duplicate line to
  `ignores.txt`.
- "Don't download" is now recorded in the user section of `ignores.txt` rather
  than the section bandcampsync manages automatically. Previously the choice was
  indistinguishable from an already-downloaded album, and could be lost entirely
  if it was made while a sync was running.
- Demo mode no longer writes to your real `ignores.txt` — trying "Don't
  download" on a demo purchase used to add that fixture to your genuine ignores.

## [1.1.0] - 2026-07-31

### Added

- Activity entries about a particular album now lead with that album's name, and
  the name is a link — click it and the album's detail opens over
  your Library. It's a normal URL (`/?album=<id>`), so you can bookmark or share
  it and it still works after a reload. The name is recorded with the entry, so
  older entries stay readable even after the album is renamed, re-identified, or
  removed; only the link goes quiet. Action messages no longer repeat the album
  title now that the entry names it up front.
- The activity feed now persists across restarts, backed by a SQLite store
  (`activity.db`) in the config dir; audit records (downloads, file/tag rewrites,
  demotions) are written there durably too, so the record of what Harmonist did
  survives a restart. The feed still shows only user-facing activity, not audit
  detail.

### Fixed

- Links and bookmarks to an album keep working after Harmonist re-identifies it
  (tagging it, or correcting its MusicBrainz match) — previously the old link
  dead-ended, and a restart made it unrecoverable. Harmonist now remembers that
  an album's identity moved, so its history stays joined to it.
- The "that album isn't in your library any more" notice can now be dismissed,
  and no longer reappears when you refresh.
- Demo mode no longer writes into your real activity history, and no longer logs
  a start-up error trying to open the activity database — it now keeps its events
  in memory, matching how it already leaves your music dir alone.
- A store URL is now recognised only when its *host* is the store's domain (or a
  subdomain of it). Previously a lookalike host like `notbandcamp.com`, or a URL
  carrying `bandcamp.com` only in its path or query, was accepted as a Bandcamp
  purchase URL and could be recorded as an album's store URL; Beatport and
  Discogs had the same flaw.
- Shutting down while a reconcile pass was finishing no longer logs a spurious
  "reconcile run failed — Event loop is closed" error; the pass's trailing
  rescan request is simply dropped.
- Linking a potential download from the "Verify album" dialog now actually links
  it — previously the dialog closed without sending the request (same root cause
  as #40: closing from `onclick` detached the button before HTMX could act).
  Modals now use the native `<dialog>` element, which closes without destroying
  its contents, removing this class of bug; Esc and backdrop-click behave as
  before.
- "Move to Library" no longer flickers the inbox — the album now resolves in a
  single render instead of triggering a background rescan that briefly dimmed and
  reloaded the list.

## [1.0.1] - 2026-07-28

### Added

- The library "verify tagging" view now flags tracks whose on-disk title differs
  from MusicBrainz: the header reports the count instead of claiming "exact match",
  and the differing rows are highlighted.
- You can now correct a wrong MusicBrainz match on a Library album — a pencil beside
  the MusicBrainz badge (in the album detail) sends it back to Needs MBID so you can
  pick the right release and re-tag. Your files keep their tags until you do.

### Changed

- The disk-vs-MusicBrainz length Δ column now shows whole seconds, and a
  within-tolerance difference is muted (the lengths are effectively the same) while
  an over-tolerance difference stays highlighted.
- The ambiguous "Wrong match" button in the album detail is gone. Correcting a wrong
  MusicBrainz release is now a pencil beside the MusicBrainz badge (see Added). The
  Bandcamp link-removal controls (the old "Wrong match" and "Unlink") are temporarily
  removed until a Library album can be re-linked to a purchase.

### Fixed

- Album-detail actions that close the modal now actually perform their action.
  Previously the modal closed before the request could fire, so the control did
  nothing.
- Manual "Re-tag from MB" now shows a progress spinner while it runs and refreshes
  the album details view when it finishes, so the disk-vs-MusicBrainz comparison
  reflects the just-written tags without reopening the album.
- Re-tagging from MusicBrainz now writes the per-release track title, not the
  underlying recording title. Track titles edited on a release (e.g. cleaning up a
  featured-artist credit) are picked up on re-tag instead of silently keeping the
  old name.

## [1.0.0] - 2026-07-10

- Initial release.
