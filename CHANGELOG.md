# Changelog

User-visible changes to Harmonist, newest first — read this on upgrade.
Format loosely follows [Keep a Changelog](https://keepachangelog.com);
versions follow [semantic versioning](https://semver.org).

## [Unreleased]

### Added

- The library "verify tagging" view now flags tracks whose on-disk title differs
  from MusicBrainz: the header reports the count instead of claiming "exact match",
  and the differing rows are highlighted.
- You can now correct a wrong MusicBrainz match on a Library album — a pencil beside
  the MusicBrainz badge (in the album detail) sends it back to Needs MBID so you can
  pick the right release and re-tag. Your files keep their tags until you do.

### Changed

- The ambiguous "Wrong match" button is gone. Each "wrong match" control now sits
  beside the badge it affects: a × on the Bandcamp badge forgets a wrong purchase
  link, and a pencil on the MusicBrainz badge re-picks a wrong release. Both confirm
  before acting.

### Fixed

- Album-detail actions that close the modal (Unlink, and the new per-badge
  wrong-match controls) now actually perform their action. Previously the modal
  closed before the request could fire, so the control did nothing.
- Manual "Re-tag from MB" now shows a progress spinner while it runs and refreshes
  the album details view when it finishes, so the disk-vs-MusicBrainz comparison
  reflects the just-written tags without reopening the album.
- Re-tagging from MusicBrainz now writes the per-release track title, not the
  underlying recording title. Track titles edited on a release (e.g. cleaning up a
  featured-artist credit) are picked up on re-tag instead of silently keeping the
  old name.

## [1.0.0] - 2026-07-10

- Initial release.
