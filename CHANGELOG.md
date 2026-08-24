# Changelog

User-visible changes to Harmonist, newest first — read this on upgrade.
Format loosely follows [Keep a Changelog](https://keepachangelog.com);
versions follow [semantic versioning](https://semver.org).

## [Unreleased]

### Added

- The **Format** row on an album's page now says what the format actually is,
  not just its name (#130) — "ALAC · 44.1 kHz · 16 bit", "MP3 · 44.1 kHz ·
  320 kbps CBR". That's what tells you whether a download is the quality you
  paid for, and whether two copies of an album are really the same. Lossless
  albums get sample rate and bit depth, lossy ones get the bitrate (and, for
  MP3, whether it's constant or variable); Opus shows no sample rate because
  the format records none. When an album's own files disagree — half 16-bit,
  half 24-bit — it reports what most tracks are and how many aren't.
- **Re-download an album from Bandcamp** (#132) — for upgrading MP3s to FLAC, or
  picking up tracks the artist has added to a release since you bought it. The
  button is on the album's own page, for any album linked to a Bandcamp
  purchase. Your current files are zipped to the top of your music folder first,
  as `Artist — Album (archived 2026-08-24).zip`, and only removed once the zip
  has been checked; unzip it there to put the album back. The inbox shows the
  album as re-downloading until the replacement lands, which is then tagged as
  the same MusicBrainz release the old copy had — re-downloading says the files
  are wrong, not the match — so the album keeps its history and normally goes
  straight back to the Library. An album that was already incomplete may come
  back just as short if the tracks still aren't there; it keeps its tags and its
  count rather than landing in the inbox.

### Changed

- An album's own page now states how much of the release is on disk — "10 of 11
  tracks on disk", under the MusicBrainz and Bandcamp badges — instead of
  leaving that to the Library tile you just came from (#227). When the shortfall
  is a whole disc it names it: "Disc 2 of 2 is missing" (#245).
- Accepting an incomplete album is now a checkbox beside that badge — **Don't
  warn me about this** — rather than a button in the action row, so it shows
  whether the album is accepted instead of making you read it backwards off the
  button's label. Ticking it turns the badge from amber to grey; the album still
  says how short it is (#227, #245).
- Video tracks are now marked with a video-camera icon instead of a play
  triangle, which looked like a button you could press. Same icon MusicBrainz
  uses (#249).
- The Library no longer repeats itself above the grid: the `LIBRARY · N done`
  heading is gone, since the tab directly above already names the Library and
  counts it — and "done" quietly disagreed with the Incomplete filter sitting
  underneath. The **Show N per page** control has moved down beside the pager,
  where you reach for it after reading a page (#217).

## [1.10.2] - 2026-08-22

### Fixed

- Your files are now paired with MusicBrainz tracks by the per-track id they
  carry, so an album stays on its own tracks after MusicBrainz renumbers or
  reorders the release's discs (#232).
- Re-tagging an album that's missing tracks no longer picks a file's track by
  comparing durations, which could write another track's title and ids (#232).
- An album whose only missing discs are video can be re-tagged again, instead
  of refusing with "16 audio files but MB release has 69 tracks" (#235, #237).
- Re-tagging no longer resets parts of an album's record — a surrendered album
  stays surrendered, and one accepted as incomplete stays accepted (#239).
- An album page now lists the video files on disk, marked as video, instead of
  reporting a part-ripped DVD as a disc you don't have at all (#226).
- The History panel's per-track lines now number tracks from 1, matching the
  files and the tracklist above them (#240).

## [1.10.1] - 2026-08-21

### Fixed

- A re-tag done in Picard is now noticed for real — 1.10.0 announced this, but
  outside the tests it never fired once (#230).
- An album whose MusicBrainz release has been deleted now shows your files' own
  tags and tracklist, which is what you'd search on to find the replacement
  release (#228).
- A MusicBrainz fetch that fails now says so in both the Tags and Tracks
  sections, instead of leaving Tracks looking like it's still working (#228).

## [1.10.0] - 2026-08-21

### Added

- A release you keep in several folders — `Album/CD1` + `Album/CD2`, a box set
  filed disc by disc — is now recognised as one album, wherever those folders
  are. Nothing on disk moves (#16, #197, #198).
- A multi-disc album's tracks are grouped by disc, named where MusicBrainz names
  them. A disc you don't have at all is reported once instead of track by track
  (#216).
- An album that's incomplete *on purpose* can be accepted as finished:
  **No more tracks to get** takes it out of the Library's Incomplete list
  without pretending the tracks are there (#196).

### Fixed

- Albums adopted from an existing library can now be found with the Library's
  **Incomplete** filter — the expected track count now comes from your files'
  own tags, so it works without contacting MusicBrainz (#187, #195).
- A MusicBrainz release that's been deleted now says so plainly instead of
  reporting a raw "HTTP Error 404", and offers to send the album back to Needs
  MBID (#194, #210).
- An album whose files describe a different release from the one it's matched to
  now says so on its page (#204).
- A re-tag done in Picard that keeps the release but corrects everything under
  it — disc numbers, titles — is now noticed and recorded (#220).
- Re-tagging no longer strips the disc subtitle Picard wrote; Harmonist now
  writes it too (#218).
- A CD+DVD release with everything ripped no longer reports its video tracks as
  missing (#193).
- An album missing only video discs — the bonus DVD you never ripped — is no
  longer reported as incomplete. The absent discs are still listed on its page
  (#206).

## [1.9.0] - 2026-08-19

### Added

- The Library can be searched by artist or album title, matching loosely enough
  to forgive case, accents and punctuation. The search rides in the URL and
  narrows the filters alongside it, so "the albums with no artwork by Aphex Twin"
  is a link you can share (#180).

### Changed

- The README is now the pitch alone; running and using Harmonist are documented
  in `docs/` — a new `usage.md` guide, plus `installation.md` and
  `deployment.md` (#178).

### Fixed

- Artist sort and Album artist sort no longer run a collaboration's artists
  together — "zakè & rhubiqs" was tagged as "zakèrhubiqs". Re-tag an affected
  album to correct it (#183).
- An album's history no longer shows one track's value as though the whole album
  got it. A field that changed differently on each track — Recording and Release
  track always do — now reads "38 different values", with the values themselves
  under "Show which tracks" (#185).

## [1.8.0] - 2026-08-18

### Added

- Any tagging in an album's History can now be undone: "Undo tag changes" puts
  back the values the files carried before it, leaving alone — and naming — any
  field you've changed since (#157).
  - Undoing the tagging that linked an album to MusicBrainz now unlinks it too,
    so it moves to Needs MBID with its release kept as a one-click suggestion
    (#158).
- The Library can be filtered to the albums that are finished but not right —
  Incomplete, Partially tagged, or No artwork — each with a count, and the
  filter rides in the URL so a filtered view is a link you can share (#174).

### Changed

- The album page's tag comparison is now its own Tags section, a peer of Tracks
  and History, so the actions no longer sit below a long field list (#160).
- When your own tracks disagree about a tag, the album page now says what is
  wrong — "missing on 1 track" rather than "6 of 7" — and clicking it lists the
  tracks (#164).
- An album's History no longer prefixes a newly added tag with "— →", and the
  longest field labels no longer wrap onto a second line (#159).

### Fixed

- An album whose files don't all carry the MusicBrainz id now says so on its own
  page, beside the Re-tag from MB button that fixes it (#175).
- "All N fields match MusicBrainz" no longer counts Genre and Comment, which have
  no MusicBrainz counterpart — an album with 7 comparable fields said 9 (#164).
- Marking a MusicBrainz match as wrong now clears the track count that came with
  it, instead of describing a release the album is no longer linked to (#166).
- Demo mode's album awaiting confirmation now starts untagged, like a real one,
  so the tagging and undo flows can be tried end to end (#168).
- Resetting demo mode now clears the per-field tagging detail along with
  everything else (#165).

## [1.7.0] - 2026-08-16

### Added

- An album's History now shows what each tagging changed, field by field, with
  how far each change reached and a per-track breakdown behind a disclosure — and
  a re-tag that changed nothing says nothing (#86).
- Artwork that a re-tag overwrites is now kept, and can be put back from the
  Artwork row in an album's History, within a 500 MB store (#131).

### Fixed

- Re-tagging an MP3 or M4A now removes Harmonist's tags that the new release
  doesn't carry, instead of leaving the wrong release's values behind (#149).
- The Media field (CD, Vinyl, …) now reads back correctly from MP3 files, which
  had made every MP3 album show Media as missing on the album page (#149).

## [1.6.0] - 2026-08-10

### Added

- The Library is now paged: Previous/Next replace Load more, and the page rides
  in the URL, so Back, a bookmark and returning from an album all land you where
  you left off (#139).
- A "Show N per page" control offering 20, 40 or 60 albums, remembered between
  visits; the default is now 20, down from 30 (#144).
- The album page's tracklist compares every track against MusicBrainz — title,
  artist, number and length — and flags tracks that are missing, unreadable, or
  absent from MusicBrainz (#135).

### Fixed

- A track numbered by vinyl side (`A1`, `B2`) no longer breaks the album page on
  FLAC, Ogg and Opus files (#137).
- MusicBrainz error messages are escaped before being shown, so angle brackets
  in an upstream error render as text rather than as markup (#142).

## [1.5.0] - 2026-08-09

### Added

- The album page compares your tags against MusicBrainz field by field, showing
  only what differs — with small changes marked inside the value, and a count
  when your own tracks disagree (#106).

- Albums Harmonist has never touched are recorded the first time it sees them,
  so an album you already owned has a start to its history rather than beginning
  at whatever first wrote a sidecar (#107).

### Changed

- "Technical detail" is now a "Show details" checkbox, and the records it
  reveals sit under the entry that caused them instead of being interleaved —
  one big action no longer fills the page (#123).
- The album page's history has the same "Show details" toggle, on by default
  (#123).
- Clicking an album in the Library opens its own page instead of a dialog, so
  every album has an address you can share, bookmark and go back from — the
  dialog is gone (#129).

### Fixed

- An audio file Harmonist can't read now counts as a missing track rather than
  an untagged one, so a corrupted file shows up as damage to find instead of
  quietly making a tagged album look untagged (#112).
- Re-tagging an album you'd confirmed as incomplete no longer fails (#133).
- Inbox cards show the album's path relative to your music library, like the
  rest of the UI, instead of in full (#121).
- The Activity feed no longer greys itself out every couple of seconds, and an
  unchanged feed is no longer re-sent — on a large library that was 154 KB every
  2 seconds (#118).
- An entry's "what changed" shows the first 20 records rather than all of them;
  the count still reports the true total (#118).
- An album with no sidecar keeps the same id across restarts, so what Harmonist
  records about it stays attached to it (#114).
- Demo downloads are recorded in the audit log like real ones, so a demo album's
  history no longer begins mid-story (#107).

## [1.4.0] - 2026-08-04

### Added

- Albums now have their own page at `/album/<id>`, with the full tracklist and
  everything Harmonist has recorded about them — including from before the album
  was last re-identified (#103).
- Clicking a library tile still opens a summary; it answers "is this the right
  release?" and links through to the page for the tracklist and actions (#103).

### Fixed

- A history store Harmonist can't read now says so, instead of showing an empty
  Activity feed and "Nothing recorded for this album yet" (#104).
- A broken history store no longer reports that an album doesn't exist (#104).
- A sidecar that can't be read is no longer skipped in silence during a sync,
  where it could offer an album you already own as a new download (#104).
- A sidecar that can't be read is no longer overwritten during a sync (#104).
- "Erased N sidecar(s)" no longer counts sidecars it failed to delete (#104).
- Failures that used to be invisible — a dropped activity or audit record, an
  unreadable ignores file, a Restore that did nothing — are now logged (#104).
- The sync options popover no longer opens over a disabled Sync button, where
  its "Sync with these options" started a sync anyway (#110).
- Syncing is refused while the first library scan is still running (#110).
- Sync Bandcamp is disabled on the Settings page, where it would have read
  settings you're partway through changing (#108).

## [1.3.0] - 2026-08-03

### Fixed

- Starting a sync writes one Activity entry instead of two, and says whether it's
  link-only or full; a reconcile with nothing to do stays quiet (#101).
- Demo mode no longer says an album has no matching Bandcamp purchase before
  you've synced (#87).
- The header status message no longer flickers (#93).
- The Activity tab no longer flickers while it's open (#91).

### Added

- Activity entries expand to show exactly what Harmonist changed on disk (#84).
- Tagging, erasing sidecars, surrendering an album and saving cover art are now
  recorded in the audit log (#88).
- The Activity feed pages back through older history, with a "Technical detail"
  toggle for the raw audit records (#14).
- Downloads, purchase links and possible mis-tags now name and link their album,
  like the rest of the feed (#97).
- Audit records show paths relative to your music library rather than in full
  (#98).

## [1.2.0] - 2026-08-02

### Upgrading

- **Nothing to do** about `ignores.txt` — existing entries keep working. Choices
  made before this release sit below the file's separator line, so they won't
  appear in the new "Won't download" list; move a line above the separator if you
  want it listed. **Don't delete the file:** albums you've downloaded are
  recognised from their sidecars, but a "Don't download" choice exists nowhere
  else, and removing it lets the purchase download again.

### Added

- Settings lists the purchases you told Harmonist not to download, with a Restore
  button — an ignore is no longer a one-way door (#19).
- Activity entries written by a sync or auto-reconcile now name and link their
  album too (#75).

### Fixed

- "Don't download" is recorded in the user section of `ignores.txt`, so the
  choice can't be mistaken for an already-downloaded album or lost mid-sync (#77).
- Deciding the same purchase twice no longer duplicates a line in `ignores.txt`
  (#79).
- Demo mode no longer writes to your real `ignores.txt` (#77).

## [1.1.0] - 2026-07-31

### Added

- Activity entries name their album and link to it, at a URL you can bookmark or
  share (#65).
- The activity feed and audit records persist across restarts, in a SQLite store
  in the config dir (#33).

### Fixed

- Links to an album keep working after Harmonist re-identifies it — tagging it,
  or correcting its MusicBrainz match (#33).
- The "that album isn't in your library any more" notice can be dismissed, and no
  longer reappears on refresh (#71).
- Demo mode no longer writes into your real activity history, or logs a start-up
  error opening the activity database (#69).
- A store URL is recognised only when its *host* is the store's domain, so a
  lookalike like `notbandcamp.com` is no longer accepted (#63).
- Shutting down mid-reconcile no longer logs a spurious "Event loop is closed"
  error (#52).
- Linking a potential download from the "Verify album" dialog now actually links
  it; modals use the native `<dialog>` element (#53).
- "Move to Library" no longer flickers the inbox (#11).

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
