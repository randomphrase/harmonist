# Demonstrating Harmonist

Record three independent clips: **Keep your library current**, **Choose your
artwork**, and **Sync your Bandcamp purchases**. Lead the README with the first;
it demonstrates the ongoing care of an existing collection that now leads the
product's description. Adoption, release matching and tagging preferences make
useful follow-up clips rather than a long introduction to the main video.

The sample collection mixes FLAC rips, MP3 downloads, M4A purchases, a two-disc
album, and incomplete or outdated tags. The files, tag writes, reviews and Undo
are real application paths; the store, MusicBrainz and Cover Art Archive answers
are local fixtures. Audio is short test tones, not songs. External links still
open real websites, but these fictional releases aren't real MusicBrainz entries.

## Assessment since the original refresh

Assessed against `898561d` through `0a9cbce` (1.26.0 plus the README update).
The collection still supports the core recordings well. The largest change
needed is the story and the choreography, rather than a replacement dataset.

| Delivered since the refresh | What the current demo can show |
| --- | --- |
| Update-significance filters and matching cover badges | Rawhide for Enrichment, Sex Bob-omb for Cosmetic, Stillwater for Structure, Soggy Bottom Boys for Identity; open each album first to discover its update. |
| Origin detection, manual corrections and Library origin filters | Rawhide is Qobuz, Stillwater is Beatport, Electric Mayhem is CD, and linked Bandcamp albums carry store evidence. Mouse Rat starts Unknown. Additional tags show the evidence. |
| Barcode discovery and more precise release review | Electric Mayhem has a UPC pointing to the UK pressing in its release group. **Barcode matches**, inline review and confirmation are demonstrable. General barcode search is not simulated. |
| Separate Possible mismatch and MB contributions findings | Electric Mayhem supplies a useful mismatch. The ordinary seed has no positive contribution example. |
| Row-by-row artwork choices, full-size inspection and browsing other releases | Mouse Rat offers a larger front; Barry Jive offers a smaller alternative; Rural Juror preserves a distinct track image; Electric Mayhem has another pressing's image in the picker. |
| Reviewed changes apply directly; clearer History and Activity | Apply an update once, inspect its field-level record, then Undo. Artwork has its own Undo. Release confirmations record the chosen match. |
| Artist-name standardization and settings applied to existing albums | Wyld Stallyns → Wyld Stallion is a Settings update under the default. Its release disambiguation demonstrates the unconditional album-title setting. |
| Conditional title disambiguation | The Settings page has a live example, but the library has no two owned releases in the same group to demonstrate the actual transition. |
| Smaller-screen layout, stable review/refresh behaviour, clearer audio formats and Re-download progress | These improve every recording. Re-download now visibly says **Archiving…** before the replacement. |

There are still worthwhile fixture gaps, none required for the three main clips:

- **Barcode discovery:** the demo's barcode-search stub returns no results.
  A Qobuz download with an original UPC and competing search results would show
  discovery and the Qobuz-specific narrowing. Electric Mayhem's existing-release
  mismatch uses a group comparison, so it does not fill this gap.
- **Giving back:** add a proven download with a missing store link or original
  UPC, after its release match is settled. Current Bandcamp fixture URLs already
  link to their releases, and the UPC-bearing CD rip cannot supply a contribution.
- **Multiple owned releases:** a second release in an existing group would
  demonstrate **Title + release disambiguation if needed** changing real tags.
  A release available in search or the artwork picker is not an owned copy.
- **Artwork breadth and library hygiene:** archive listings currently offer one
  front per source, not a front/back/booklet set. No artwork, Mixed formats and
  Partially tagged have no positive examples in the public starting library.
  Dingoes has one missing embedded cover, not an album with no artwork at all.
- **Presentation realism:** one-second tracks and repeated Demo Records metadata
  make the sandbox obvious. Avoid dwelling on durations in a promotional clip.
  Barry Jive's description as an old rip isn't backed by rip-provenance tags;
  use Electric Mayhem when demonstrating detected CD origin.

These would be focused future demo changes. The recordings below use the current
fixtures and don't depend on them.

The multi-folder mismatch clip requires the fix for
[#730](https://github.com/randomphrase/harmonist/issues/730), included in this
checkout. In 1.26.0, confirmation leaves the second disc's sidecar on the old
release and splits the album. With the fix, the rehearsed flow keeps both disc
folders and all four tracks together under the UK pressing, including after a
reload. Use this checkout rather than the 1.26.0 image for that recording.

## Start, reset and prepare a take

In the development container, run:

```sh
make demo
```

Open <http://localhost:8000/?tab=library>. Development setup is in
[development.md](development.md). Use the checkout being recorded, so the video
shows the current UI rather than an older installed image.

**Reset Demo** restores the starting files, prior activity, purchase decisions,
archived downloads, cached metadata and kept artwork. It retains Settings and
browser preferences. Wait for running operations to finish before resetting.
The sandbox also survives server restarts; restarting alone isn't a new take.
Changing the dataset or adoption recipe reseeds it on startup.

Before each clip:

1. Reset, wait for scanning to settle, then check **Settings** against this table
   and press **Save settings** if anything changed.
2. Use **Clear** to reset the Library search and filters. Keep browser zoom,
   window size and theme consistent between takes. Turn History's **Show details**
   off to hide raw audit records; field-level change records remain visible.
3. Open the albums listed in the clip's preparation, wait for Album, Tracks and
   Artwork to finish loading, and return to the starting view. Do not apply any
   changes during preparation. Resetting again would discard these checks.

| Setting | Recording baseline |
| --- | --- |
| Check MusicBrainz for updates | Off |
| Create cover.jpg in album folders | Never (existing folder covers can still be upgraded) |
| Album title | Title only |
| Standardize artist names | Standardize artist name variations only |
| Always standardize multi-valued artist tags | Unticked |
| Download format | flac |
| Max downloads per sync | 5 |

An untouched established demo starts with **Inbox 3**, **Library 9** and
**Incomplete 1**. Update and mismatch filters fill as albums are checked;
zero immediately after a reset does not mean the demo has no examples. Don't
script fixed update counts after rehearsal. Wyld Stallion remains in Needs
Linking until sync, so it isn't a Library Settings-filter example before then.

For recording, the default pacing is usually enough. A slower take can use:

```sh
HARMONIST_DEMO_DELAY=0.5 make demo
```

The delay is seconds per tag write or archive check, capped at two seconds.
Sync also pauses between albums. `HARMONIST_DEMO_DELAY=0 make demo` disables
all pacing for rehearsal. Mock downloads support FLAC, ALAC, MP3 and AAC from
the current Settings choices; **vorbis is offered by the product but has no demo
audio fixture**, so use FLAC for this recording.

## Clip 1 — Keep your library current (about 90 seconds)

**Prepare:** reset and use the baseline above. Open **Rawhide**, **We Are Here
To Make You Sad**, **Fever Dog** and **Man of Constant Sorrow**, letting each
comparison settle. Return to Library and clear any filters. Leave their updates unapplied.

| Beat | On screen | Suggested narration or caption |
| --- | --- | --- |
| 0–15 s | Show the library covers. Open **Origin**, choose **Qobuz**, then **Clear**. | “Harmonist looks after the music you already own, from downloads to CD rips.” |
| 15–30 s | Open **Tag updates**, briefly show the significance choices, then choose **Enrichment** and open **Rawhide**. | “Find the albums that could be better, and see exactly what would change.” |
| 30–50 s | Hold on the Enrichment summary and the date, label and catalogue-number differences. Press **Apply updates** once; wait for the differences to clear. | “MusicBrainz adds a more precise date and missing release details. I review them before they reach my files.” |
| 50–70 s | Scroll to **History**. Hold on the date, label and catalogue-number changes. Choose the update's tag Undo and accept its confirmation. | “Every change has a record. Undo restores the previous values.” |
| 70–90 s | Show the proposed update returning, then briefly show **Activity** from the main page. | “The history includes the undo, too. My folders stay where they are.” |

**End check:** Rawhide again has its short date and missing label/catalogue tags,
and the Enrichment update is available. The undo has its own history entry.

For a background-maintenance ending, cut to Settings → **Background updates** →
**Check MusicBrainz for updates: In the background**, save, and show the update-check
status. Say “Checks the library in the background and finds updates for review.”
There is no **Check now** button; don't wait on camera for the ten-minute batch
timer or claim that it applies changes automatically. Restore Off before the
next clip.

## Clip 2 — Choose your artwork (about 75–90 seconds)

**Prepare:** reset, restore the baseline, and open **Mouse Rat — The Awesome
Album**. Let Artwork settle. Start with the **Now**, **After Apply** and
**Available** columns in view.

| Beat | On screen | Suggested narration or caption |
| --- | --- | --- |
| 0–20 s | Show the 320×320 current images and the 960×960 archive front. Open the candidate full size, beside the row's image, then put the picker back. | “Compare the cover in my files with the Cover Art Archive.” |
| 20–40 s | Point to the proposed folder-cover upgrade. Select **All 3 tracks**, then press **Use** for that row. Hold on both rows' **After Apply** images. | “The folder cover has a larger image available. Replacing the embedded covers is my choice.” |
| 40–60 s | Press **Apply artwork** in Artwork. Wait for both current rows to show 960×960. | “Apply exactly the artwork I've reviewed.” |
| 60–90 s | Scroll to History and choose that artwork change's **Undo**; accept the confirmation. Show both rows back at 320×320. | “Artwork changes can be undone independently of tags.” |

**End check:** both embedded images and `cover.png` are back at 320×320. Applying
only the initial automatic proposal would upgrade the folder cover and leave
embedded images alone; the explicit **Use** step is what makes this clip change
both. **Apply artwork** is the section's action; **Apply updates** at the top
can also apply included artwork when reviewing an album update.

Optional inserts, recorded as separate takes:

- **A deliberate smaller choice:** on **Barry Jive — After Hours**, select
  **All 3 tracks**, choose the smaller **This release** image with **Use**, then
  select the `cover.png` row and press **Use** again. Apply artwork and undo it.
  Each row needs its own choice; a single Use no longer means replace everything.
- **Keep a compilation's artwork:** on **The Rural Juror (OST)**, show the
  separate track image. Select only the folder-cover row or the shared-image
  track row before choosing an alternative; leave the unique image alone.
- **Browse another release:** on **Electric Mayhem**, step through Available
  with **Next image** to the other pressing's cover. Inspect it without applying
  it. This is artwork browsing, not a change to the matched release.
- **Fill a gap:** on **Dingoes Ate My Baby**, show the one track with no embedded
  image and the proposed fill. Apply artwork and show the existing images kept.

## Clip 3 — Sync your Bandcamp purchases (about 2 minutes)

**Prepare:** reset and restore the baseline. Don't sync during preparation.
Open Mouse Rat to establish its MP3 format, then return to Inbox. Leave Wyld
Stallion in Needs Linking. This first sync must happen on camera.

| Beat | On screen | Suggested narration or caption |
| --- | --- | --- |
| 0–20 s | Show Wyld Stallion under **Needs Linking**, then press **Sync Bandcamp** and wait for completion. | “The first sync links purchases to albums I already have.” |
| 20–45 s | In potential downloads, choose Mouse Rat's **Already in your library?** and link **The Awesome Album**. | “This purchase is already here as MP3s. I can link it without downloading another copy.” |
| 45–65 s | Choose **Don't download** for Autobahn, then **Download** for **CB4**. Run **Sync Bandcamp** again. | “Choose which purchases to download. A new download with an exact match is tagged automatically.” |
| 65–85 s | Open **CB4 — Straight Outta Lowcash**. Show FLAC, its matched release and History. | “The new album arrives as FLAC, with MusicBrainz metadata.” |
| 85–120 s | Open Mouse Rat, choose **Re-download**, accept the prompt, and let the replacement finish. Show **Archiving…**, then the FLAC result and its History/Activity. | “To upgrade an old copy, Harmonist saves the original in a ZIP and downloads a fresh lossless copy.” |

**End check:** Mouse Rat is FLAC with the same confirmed release; a ZIP in the
demo music root contains its original MP3 files. CB4 is matched and tagged.
Autobahn is under **Won't download** in Settings. Saying “Don't download” before
the second sync makes this take deterministic: a full sync can otherwise fetch
the other new purchase too. Explicitly approved downloads and re-downloads
bypass the ordinary download cap.

The ZIP is useful optional footage, not necessary terminal work in the main
clip. Find the sandbox's **Music library** path under Settings → Server; don't
show a real library. This is a new download, not MP3-to-FLAC conversion. Reset
restores the old MP3s and removes the demonstration archive and skip decision.

## Follow-up clips

### Review a possible mismatch (about 60 seconds)

Reset, open **Electric Mayhem — Can You Picture That?**, wait for the comparison,
then return to Library. Choose **Possible mismatch** and reopen it. Show Origin
**CD**, both disc-folder paths, and the original UPC under **Additional tags**.
The UK pressing has **Barcode matches** beside **Use**. Choose Use, inspect the
four retained track pairings and expand **Tag changes**, then **Confirm
suggestion**. Show the UK pressing as the matched release, both disc-folder
paths and all four tracks, then its History entry.

Caption: “The files contain evidence of another release. I check the tracks
before changing the match.” This is a mismatch review, not an MB contribution
or a global barcode search. The existing cover can remain; don't promise a new
cover from this confirmation. Reset before using the original pressing again.

### Resolve a structural change (about 45 seconds)

Reset and choose **Incomplete → Fever Dog**. Show “3 of 4 tracks on disk”, then
the **Structure** finding. The track-assignment editor opens automatically once
the comparison loads; **Review assignments** returns to it if needed. Keep the
three known pairings, expand **Tag changes**, then press **Accept changes** to
apply them directly. The old total of four becomes three and the Incomplete
badge clears. No audio track has been downloaded or invented.

### Bring tags into line with your preferences (about 60 seconds)

Reset and open **Wyld Stallion — A Most Excellent Journey** from Needs Linking.
Show the **Settings** update from the credited *Wyld Stallyns* to *Wyld Stallion*.
In Settings, change **Album title** to **Title + release disambiguation**, save,
and return: *(San Dimas edition)* is now proposed too. Apply the reviewed update.

Caption: “Tagging preferences apply to music already in my library, with a
preview before anything changes.” Settings-only updates have no Ignore control;
the setting itself determines the spelling. For an optional shot, select
**Title + release disambiguation if needed** and show the live Example library
table in Settings. Present that as an example, not a transition in this seeded
library. Restore the baseline explicitly after the take; Reset doesn't do it.

### Adopt an existing collection (about 60–90 seconds)

Stop the demo server and run:

```sh
HARMONIST_DEMO_ADOPTION=1 make demo
```

This starts the same files without Harmonist sidecars or prior activity. The
normal startup scan and reconciliation adopt them. Start the screen recorder
before launching the server if capturing startup; opening the page later
cannot replay a scan that has finished.

1. Show albums arriving without a download. Open **Electric Mayhem**: the two
   existing disc folders belong to one album, with their paths intact.
2. In the Inbox, search **Sonic Death Monkey** by name, select the release,
   review the tracks and confirm. For a pre-seeded suggestion instead, use
   **The Thamesmen** in the established demo, outside adoption mode.
3. Open Rawhide to show its pending metadata enrichment. Recognition and
   bringing tags up to date are separate decisions.

Caption: “Start with the collection you already have. Existing MusicBrainz tags
are recognised; albums without an identity can be matched for review.” Don't
use Barry Jive's genre as on-screen proof of preservation: Harmonist preserves
it in the files but no longer displays Genre in the album comparison.

Stop the server, unset `HARMONIST_DEMO_ADOPTION` if it was exported, and run
`make demo` again for the established-library clips.

## Publishing the recordings

Record the three main clips separately so a change to one feature doesn't age
the whole walkthrough. Use captions or the suggested narration, pause on the
before/after evidence, and trim waiting rather than racing through controls.
Keep the demo banner visible and external MusicBrainz/Harmony edits out of the
take: the fixtures cannot demonstrate a successful real contribution.

Once the recordings are uploaded, replace the old README video and its
out-of-date notice with the maintenance clip, followed by artwork and Bandcamp
clips under their own short captions. Until then, the existing notice remains
accurate; a script is not a replacement recording.

## The visual assets

The public collection's sleeves are original geometric drawings with a limited
palette and initials, generated reproducibly by `python scripts/demo_artwork.py`.
Mouse Rat's two sizes use the same drawing. The assets contain no movie stills
or reproduced sleeves. Electric Mayhem's alternate pressing uses a legacy
placeholder image. The larger recovery catalogue remains a test fixture, not
the default public collection.
