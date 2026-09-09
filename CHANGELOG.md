# Changelog

User-visible changes to Harmonist, newest first — read this on upgrade.
Format loosely follows [Keep a Changelog](https://keepachangelog.com);
versions follow [semantic versioning](https://semver.org).

## [Unreleased]

### Added

- **Harmonist can take better artwork from the Cover Art Archive.** The album
  panel gains a **CAA checked** date beside the MusicBrainz one, with a control
  to ask; if the archive's front cover is larger than anything the album has,
  it is kept locally and a re-tag writes it — to the tracks and to the folder
  cover, whose previous image is kept so the change can be undone. A tagging
  never reaches the network, and an archive cover that is no better than yours
  is ignored. The Artwork section shows the
  archive's image beside your own, marked as MusicBrainz's, and serves it from
  Harmonist — so opening an album never tells the Internet Archive which records
  you own (#276). The **CAA checked** row shows on any album with a MusicBrainz
  release, reading *not yet* until you ask — it carries the control that asks,
  so hiding it until the first check left nothing to press (#419). Opening an
  album's page now asks the archive by itself when the stored answer is missing
  or more than a week old, exactly as it asks MusicBrainz — after the page is
  up, so nothing waits on it, and never twice inside the window. The control
  beside the date still asks regardless, and a check that fails leaves the
  previous answer and its date untouched. Tune it with `[cover_art]
  cache_ttl_seconds` (#436).


- **An Artwork section on the album page** — what artwork your files actually
  carry, one row per image, with its dimensions, format and size, and the tracks
  carrying it. Beside each row: what a re-tag would put there, so a folder cover
  about to replace a larger image, or a track quietly missing its art, is
  visible before you press anything. Per-track artwork on a compilation is shown
  as the normal thing it is, and marked as preserved. Every image opens full
  size. It needs no MusicBrainz release, so it works on albums Harmonist hasn't
  identified yet (#155). A row where nothing would change says nothing, the
  folder cover appears as the file you already have rather than only as an
  incoming value, and each image names the disc and track title carrying it
  (#400). A row a re-tag would write to says so in a line, rather than drawing
  the folder cover a second time beside it (#406).


### Changed

- **An album's findings are one section, each with its own button beside it.**
  What MusicBrainz has and what the album's artwork could be were two stacked
  boxes, and only the first carried an action — **Update artwork** was a scroll
  away in the Artwork section, behind a link, which is enough to make a control
  invisible. Both buttons now sit at the same weight on their own finding's
  line. **Ignore until MusicBrainz changes** moves to a quiet foot instead of
  standing beside the primary action, and the upstream sentence goes with it,
  shortened to *"Wrong? Fix it on MusicBrainz — the next check takes your
  edit."* The artwork finding counts files rather than pictures — *"better
  artwork is available for 12 tracks"* rather than *"1 image that could be
  better"* — and names `cover.jpg` when that is what changes (#443).

- **Updating artwork is now its own action, separate from a re-tag.** A re-tag
  fills tracks that have no artwork and never replaces one you already have;
  improving an album's images is an **Update artwork** button in the Artwork
  section, beside the pictures it changes. Replacing artwork is the change most
  likely to be worth undoing on its own, and it has always had its own Undo in
  History — now it has its own way to be done, too (#418).

- **The Cover Art Archive check now falls back to the release group**, where the
  archive very often keeps an album's artwork — so albums that reported "no front
  cover" may have one after all. A group's cover is labelled as the group's
  rather than this edition's (#434).

- **What the Cover Art Archive has is shown as a row like any other image** —
  greyed, with its size and format, and a placeholder saying which kind of
  nothing it is: *not loaded* for a cover that exists but lost, *none* for a
  release the archive holds nothing for. It used to be a purple sentence, which
  read like something about to be written (#433). It now sits in the incoming
  column under **Also considered**, rather than among the images the album
  already has — on an album whose archive cover matches its own, two rows
  stating the same size read as owning the picture twice with one copy broken.
  Which column an image is in now says where it came from, and the hexagon and
  purple say whether it is coming (#441).

- **The top of an album's page says when its artwork wants attention**, with a
  link down to the Artwork section, which is usually well below the fold. And
  the Re-tag buttons no longer claim "per-track embedded artwork is preserved" —
  true once, and since #418 simply: your artwork is left alone (#417).


- **The last five artwork changes to any album can always be undone**, whatever
  else Harmonist has been tagging. Kept artwork used to be bounded only by a
  total size, so a busy night's re-tagging could quietly evict the copy behind
  an Undo a different album was still offering. The size cap remains as a
  backstop, and Settings now states the promise rather than only the number.
  Configurable as `artwork_store.keep_per_album` (#408).


### Fixed

- **An album with no `cover.jpg` can now take the Cover Art Archive's cover.**
  Art embedded in the files with no cover file beside them — the shape an
  adopted library arrives in — was ruled out before its size was even
  considered, so the archive's cover was reported as an also-ran however much
  better it was. It is now judged against the tracks' own image, and written to
  them when it wins; no `cover.jpg` is created where none existed. A
  compilation's per-track artwork is still never overwritten, and now says so in
  the album's History (#442).

- **A release you have two copies of now has two tiles that open two albums.**
  Both copies were addressed by the release's MusicBrainz id, so either tile
  opened the same one and a re-tag or a re-download taken from the second copy
  acted on the first. Each copy now has its own address; a link written before
  the second copy appeared says which copies it could mean rather than picking
  one, and both show the release's history, each entry naming the folder it
  touched (#424).

- **Undo now puts each disc's tags back on its own disc.** An album whose files
  span several folders had every change recorded under a bare filename, so
  undoing a tagging of a two-disc album whose discs both contain an `01.m4a`
  restored one disc's tags onto the other — and where the filenames differed it
  failed outright instead. Records now name the disc, artwork restores address
  the right folder, and a record from before this fix that could mean either
  disc is refused rather than guessed at (#423).

- **A purchase is no longer skipped because a different artist used the same
  album address.** Bandcamp addresses like `/album/home` are unique to one page,
  not to the world; Harmonist treated them as an identity, so owning Zero 7's
  *Home* could quietly file The Gathering's away as already downloaded, or hand
  an unlinked album's store URL to the wrong purchase. Cross-listings — one
  release sold from both a label page and an artist page — are now confirmed
  against MusicBrainz instead of assumed, and anything unconfirmed becomes a
  potential download for you to decide rather than a decision made for you
  (#425).

- **A download that fits two MusicBrainz editions equally is no longer tagged as
  whichever one came back first.** Where a store URL resolves to several
  releases with the same tracklist, nothing is written: Activity says several
  editions fit, and *Look up releases at this URL* on the album's card lets you
  pick the right one — the choice Recheck already offered (#426).

- **The artwork change you just made is the one that can be undone.** A backup
  taken as an image was replaced could be evicted by the store's size cap in the
  same breath — leaving the newest change the only unreversible one, and, for a
  folder cover, overwriting the original on the strength of a backup that was
  already gone. The image being kept now is retained ahead of older history, and
  if it genuinely cannot fit, the cover is left alone (#427).

- **The Artwork section shows what a re-tag would put there**, beside what is
  there now and under a heading that says which is which — where it previously
  asserted "replaced by…" in a way that read equally as something that had
  already happened (#413).

- **A track missing its artwork no longer costs the other tracks theirs.** A gap
  is filled from the image the rest of the album already carries, and those
  tracks are left alone — where a re-tag used to overwrite every one of them
  with the folder cover to fill the one that was empty. The folder cover is
  still embedded where it is genuinely the better image (#397).

- **A re-tag no longer shrinks an album's artwork.** Where the image your tracks
  already carry is larger than the folder `cover.jpg`, the folder file is
  updated from it instead of being embedded over it — five of sixty albums
  sampled from a real library were losing resolution this way, in one case
  5700px replaced by 2000px. The replaced `cover.jpg` is kept, so it can be put
  back from the album's History (#410).


## [1.15.0] - 2026-09-07

### Added

- **A background update check that finds something now says so in Activity** —
  one line per pass ("Update check: 3 albums now have an update available"), and
  nothing at all from a pass that found nothing (#274).

### Fixed

- **A suggested release is now lined up against your files the same way the
  album page and the tagger line them up.** Existing suggestions are re-paired
  by Refresh from MB or the next recheck (#395).

- **A re-tag that changes nothing no longer repeats the note about keeping
  per-track artwork** in the album's History (#272).

- **A collaboration missing its album-artists tag now reads as Enrichment
  rather than Identity**, so it no longer outranks genuine retitles in the
  Inbox (#389).

- **A pending change under the tracklist is drawn like every other one on the
  page** — stacked, with what changed marked inside the value, and no longer
  struck through (#386).

- **A stray Compilation tag now reads as a pending `Yes → No`** rather than as
  a matching field, counting the tracks carrying it and noting that "No" is
  written by removing the tag (#383).

- The popover naming which tracks disagree about a tag now reads "1 carries"
  rather than "1 carry" when a single track holds the value (#384).

- **A multi-disc rip whose discs are named no longer lands in the Inbox as
  Inconsistent** — one MusicBrainz release id on every file settles it, whatever
  the titles say (#381).

## [1.14.0] - 2026-09-04

### Added

- **An album whose release MusicBrainz has merged away now says so on its
  page**, beside the MusicBrainz badge, with the surviving release linked and
  the re-tag that follows the merge offered there (#361).

- **An update you don't want can be ignored until MusicBrainz changes the
  release again.** Tick **Ignore until MusicBrainz changes** on the album's page
  and it leaves the **Update available** filter without anything being written;
  the page also links **Edit the release**, which is where a correction belongs
  (#271).

- **The album page names every country a release came out in, each with its own
  release date** — where it could only show the single country code the tag
  holds (#329).

- **Inbox cards link to their album's page, and that page can now take the same
  decisions** — search or assign a release, accept a surrender, mark an album
  purchased elsewhere (#150).

### Changed

- **An album's update now has a section of its own, under the panel**, holding
  the sentence saying what differs, the button that takes it and the answers to
  it — with a chip saying how far the change reaches, *Enrichment* or *Identity*
  (#366).

- **A tag that differs from MusicBrainz on every track is now shown with the
  tracks**, in the band beneath the tracklist, with its current value and
  MusicBrainz's (#360).

- **An album with no MusicBrainz release is no longer offered a re-tag** that
  could only fail — the actions section below the panel is the way forward for
  those albums (#150).

- **An album now says when Harmonist last read it from MusicBrainz.**
  **Checked** joins Downloaded, Added and Tagged in the album panel, and the
  button that forces a fresh read sits beside it (#355).

- **An album with nothing to fix says nothing at all** — no MusicBrainz band,
  just a **Checked** date saying when that was established (#352, #358).

- **Background update checks now spread their work evenly across the day**
  rather than asking about a hundred albums in a burst every hour (#349).

### Fixed

- **A track title that differs only in typography no longer reads as a
  retitle** — a curly apostrophe where your files have a straight one now ranks
  **Cosmetic** instead of putting the whole album into the Inbox under
  **Identity** (#379).

- **Re-tagging an album whose release was merged away now leaves you on the
  surviving release's page** (#375).

- **A purchase a sync couldn't finish with is retried on the next sync instead
  of being lost for good** — a pre-order Bandcamp wasn't serving yet, or an
  album deferred by the per-sync download limit. The sync now says so when it
  finishes: *"1 pre-order not released yet — each sync retries them"* (#351).

- **A release country your `preferred_release_countries` setting chose is no
  longer reported as out of date.** Any country the release actually names now
  counts as correct, so those albums leave the **Update available** filter
  (#346).

- **Clearing a MusicBrainz match, or unlinking a Bandcamp purchase, writes one
  Activity entry instead of two** (#342).

- **A tag MusicBrainz holds on only some tracks is now shown on the tracks that
  have it**, instead of being reported as removed from all of them (#374).

- **An album whose only difference is a tag stated under the tracklist now gets
  an update section**, with the **Re-tag from MB** that answers it. That change
  is also counted in the sentence above the tracklist, which used to read *"All
  24 tracks match"* over the top of it (#373).

## [1.13.0] - 2026-09-01

### Added

- **Harmonist can check your library against MusicBrainz in the background**, so
  the **Update available** filter finds albums nobody has opened (#270). It is
  off by default and never writes to your files: turn it on under **Background
  update checks** in Settings, where **Check now** runs a pass immediately
  rather than waiting up to an hour for the next one (#312).
- An album with a MusicBrainz update waiting carries an **Update** badge on its
  Library tile (#293).
- **Harmonist writes the `compilation` flag on Various Artists releases** —
  without it, players in the iTunes lineage shatter a 20-track compilation into
  twenty one-track albums, one per track artist (#323).
- **Harmonist writes `albumartists`**, the album-level artist list Picard writes
  beside `albumartist`, so a collaboration files under both names rather than
  under one composite one (#322).

### Changed

- **An album's tracklist shows the per-track tags that differ, as columns**, so a
  change lands beside the track it belongs to instead of being summarised as
  "1 of 7 tracks" in the box below. A tag that reads the same on every track is
  laid out in a band under the table instead (#309, #328).
- **Artist credits read as the artists they name** — "Rafael Anton Irisarri feat.
  Julia Kent" is two links, in both the tracklist and the Album artist row. The
  separate **Artists** column goes with it, being the same credit unjoined
  (#309, #319).
- **The tracklist's identifier columns start hidden**, behind **Show identifiers**
  at the top right of the Tracks heading, so the readable tags get the width
  (#319, #328).
- **A multi-disc release carries each disc's subtitle, medium and track count on
  its heading**, rather than repeating one value all the way down a column of its
  own — and those headings are legible now, where a lost cascade had been drawing
  them at the size of the column headings beneath (#320).
- **The album page draws one MusicBrainz note instead of two**, in the album panel
  beside **Re-tag**: both summaries, when the release was last read, and **read
  again** (#328).
- **Forget has moved to the far right of the album's actions row**, away from
  Re-tag (#328).
- **A track MusicBrainz lists that you don't have reads "Not in your files"**,
  with a dashed ring beside its number — so a half-ripped disc is a column of
  marks rather than a stack of notices (#326).

### Fixed

- **Every dated M4A album no longer reports an update available forever.**
  Harmonist wrote the original-date tags in a case Picard doesn't use, so it
  could never see its own; it now writes Picard's spelling and clears the tags
  it wrote before (#333).
- **A re-tag no longer deletes MusicBrainz secondary release types**, so an album
  keeps *live*, *remix* or *soundtrack* — and **Navidrome has no other source for
  its album *Type* filter**. Albums tagged by an earlier version will show one
  corrective update (#331).
- **Every label and catalogue number a release names is written**, not just the
  first — and a release whose first label entry carried no catalogue number no
  longer ends up with none at all (#334).
- **A library that predates a newly added tag is no longer flagged wholesale** —
  a missing `albumartists` put every album into *Update available*, and its
  absence is now reported only on a genuine collaboration (#337).
- **A tag MusicBrainz has no value for is shown as a pending removal**, so an
  album can no longer be flagged for an update and then show no reason for it
  (#340).
- **Identifier columns are shown from the start when they are the only thing that
  differs** — the tracklist could say *"11 of 11 tracks differ"* above a table
  with nothing marked (#339).
- **The album page can put a name on the MusicBrainz artist ids credited to a
  track**, not just the ones credited to the release (#309).
- **History rows for Disc subtitle** no longer read as the raw tag name
  `disc_subtitle` (#309).
- **The tracklist's # and Length headings are right-aligned** over their columns
  again (#261).
- **The log no longer warns that a MusicBrainz fetch was slow on every fetch** —
  the threshold sat below what a normal fetch costs on a NAS, drowning out the
  stalls it exists to report (#314).

## [1.12.0] - 2026-08-28

### Added

- The Library has an **Update available** filter, for albums whose MusicBrainz
  release has moved on since they were tagged (#287).
- An album with an update waiting **says what the update is**, under the Tags
  comparison (#291, #297).
- Harmonist **caches the MusicBrainz releases it fetches**, so browsing no longer
  spends a rate-limited request per page (#127). Tune with `cache_ttl_seconds`
  under `[musicbrainz]`.
- Harmonist **rescans your library once an hour**, as a backstop for when the
  file watcher is blind (#151).
- **An album's page re-reads that album from disk before it renders** (#151).
- Harmonist **says in its log when an operation took too long** (#300).
- The startup pass that fills the Update available filter **reports its progress
  in the log**, and paces itself against the machine it is on (#299).

### Changed

- **Re-tagging leaves alone any file it wouldn't change** (#266), so your files
  keep their timestamps.

### Fixed

- **The Tags comparison covers every album tag Harmonist writes** — seventeen,
  where it compared six (#295).
- **The MusicBrainz IDs in that comparison read as the artist and release group
  they name**, and link to MusicBrainz (#298).
- **Album Type and Album Status are now written in Picard's lowercase** (#290).
  Albums Harmonist tagged itself will show one corrective update.
- **A Picard disambiguation comment in an album title no longer reads as a
  mismatch** (#283).
- **Tagging follows a merged MusicBrainz release**, and says so in the album's
  History (#268).
- **Decisions you record about an album — Keep in Library, accepting an
  incomplete album — are no longer erased** by a sync, a recheck, or rejecting a
  suggestion (#263).
- **A split-folder album no longer forgets that its absent disc was video**
  (#263), so it stays Complete.
- **Keeping your existing per-track artwork now appears in the album's own
  History** (#260).
- **A failed action no longer writes a second, blank entry** to the Activity
  feed (#258).
- **An unreadable album is reported once** rather than on every scan (#151).

## [1.11.0] - 2026-08-24

### Added

- The **Format** row on an album's page now says what the format actually is,
  not just its name (#130) — "ALAC · 44.1 kHz · 16 bit", "MP3 · 44.1 kHz ·
  320 kbps CBR" — so you can see whether a download is the quality you paid for.
- **Re-download an album from Bandcamp** (#132) — for upgrading MP3s to FLAC, or
  picking up tracks the artist has added to a release since you bought it. The
  button is on the album's own page; your current files are zipped to the top of
  your music folder first, as `Artist — Album (archived 2026-08-24).zip`, and the
  replacement is tagged as the same MusicBrainz release the old copy had.

### Changed

- An album's own page now states how much of the release is on disk — "10 of 11
  tracks on disk", under the MusicBrainz and Bandcamp badges — and names the
  shortfall when it's a whole disc: "Disc 2 of 2 is missing" (#227, #245).
- Accepting an incomplete album is now a checkbox beside that badge — **Don't
  warn me about this** — rather than a button in the action row, so it shows
  whether the album is accepted instead of making you read that backwards off a
  button's label (#227, #245).
- Video tracks are now marked with a video-camera icon instead of a play
  triangle, which looked like a button you could press (#249).
- The Library no longer repeats itself above the grid: the `LIBRARY · N done`
  heading and its **Refresh** button are gone, since the tab above already names
  and counts the Library and the grid refreshes itself after every scan, action
  and sync. **Show N per page** has moved down beside the pager, where you reach
  for it after reading a page (#217).

### Fixed

- AAC files now say **AAC** in an album's Format row instead of **MP4** (#254),
  so the row tells you at a glance whether an `.m4a` album is the lossless
  download or the lossy one.
- Re-tagging an album whose MusicBrainz release has since gained tracks no
  longer fails with a stack trace (#252). It offers **Re-tag as incomplete** —
  press it and your files take the release's current tags; nothing is written
  unless you do.

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
