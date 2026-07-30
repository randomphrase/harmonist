# Changelog

User-visible changes to Harmonist, newest first — read this on upgrade.
Format loosely follows [Keep a Changelog](https://keepachangelog.com);
versions follow [semantic versioning](https://semver.org).

## [Unreleased]

### Added

- The activity feed now persists across restarts, backed by a SQLite store
  (`activity.db`) in the config dir; audit records (downloads, file/tag rewrites,
  demotions) are written there durably too, so the record of what Harmonist did
  survives a restart. The feed still shows only user-facing activity, not audit
  detail.

### Fixed

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
