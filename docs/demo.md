# Demonstrating Harmonist

The sample collection represents a library cared for over several years: older
MP3 downloads, FLAC rips, M4A purchases, a two-disc album, and a few incomplete
or outdated tags. MusicBrainz remains the canonical source of release metadata.
Bandcamp sync works alongside that existing collection.

## Start and reset

In the development container, run `make demo` and open
<http://localhost:8000/?tab=library>. No music or store account is needed.
The store, MusicBrainz, and Cover Art Archive responses are local fixtures.
External links still lead to real websites; the fictional release IDs are not
real MusicBrainz entries. Audio files contain short test tones, not songs.

**Reset Demo** restores the selected starting library, its prior activity,
purchase decisions, archived downloads, cached metadata, and kept artwork.
Settings and browser preferences are retained. For these recipes, select FLAC
as the download format and turn background checks off until you demonstrate
them. Keep the download cap above zero for ordinary new downloads (explicitly
approved purchases and re-downloads bypass that cap). Leave **Create missing
folder covers** off for these recipes: updating an existing cover still works.
Wait for running operations to finish before resetting.

The established library's history survives server restarts inside the demo
sandbox. Its two earlier updates were performed through the real tagger, so
their change records and Undo controls work. Changing the dataset or adoption
recipe automatically reseeds the sandbox on startup.

Tag writes pause briefly so in-progress controls remain visible. Set
`HARMONIST_DEMO_DELAY=0 make demo` for fast exploration, or use a value such as
`0.5` for slower recordings. This is seconds per tag write or archive check,
capped at two seconds. Sync also pauses between albums; setting zero disables
all demo pacing. This exposes existing progress reporting without inventing
track counters or progress percentages.

Mock downloads support FLAC, ALAC, MP3, AAC, and Opus. Other store formats are
not simulated and report an error instead of silently choosing a different one.

## 1. Adopt an existing library

Stop the server and run:

```sh
HARMONIST_DEMO_ADOPTION=1 make demo
```

This starts the same files without Harmonist sidecars or prior activity.
The normal startup scan and reconciliation adopt them. Start recording before
opening the page if you want to capture that initial progress.

1. Show that recognisable albums arrive in the Library without a download.
   Open **Electric Mayhem — Can You Picture That?**: both existing disc folders
   belong to one release after reconciliation.
2. Open **Barry Jive — After Hours** to show an older FLAC rip. Its personal
   genre and comment remain in the files when Harmonist updates metadata.
3. Find **Sonic Death Monkey** in the Inbox. Its files have readable titles but
   no MusicBrainz identity. Search by artist/title and confirm the release.
4. Show that some adopted albums have updates waiting, while already-current
   albums require no work. Adoption and bringing all metadata current are
   separate steps.

Finish with the original organisation intact and the remaining decisions
visible. Stop the server and run `make demo` again for the established-library
recipes below.

## 2. Link purchases, download new music, upgrade an old copy

Reset the established demo. Select **FLAC** as the download format.

1. Show **Wyld Stallion** under Needs Link and **Mouse Rat** already in the
   Library as MP3s. Their different store evidence is deliberate.
2. Press **Sync**. The first pass runs link-only: Wyld Stallion links
   automatically, and existing audio files remain unchanged.
3. In potential downloads, choose **Already in your library?** for Mouse Rat
   and link **The Awesome Album**. No second copy is downloaded.
4. Approve **CB4** for download and sync again. It arrives as FLAC, matches
   MusicBrainz, and is tagged automatically. Autobahn can remain pending or
   be downloaded as a second example.
5. Open Mouse Rat and choose **Re-download**. Harmonist archives the MP3 album,
   fetches a fresh FLAC copy, and carries the confirmed release through the
   replacement. Show the format change and its Activity/History entries.
6. Show the ZIP in the demo music root: it contains the original MP3 files.
   This is a fresh lossless download, not conversion of the lossy files.

These can be three short chapters: linking, new purchases, and re-download.
Reset restores the MP3 copy and removes the demonstration archive.

## 3. Review, update, and undo tags and artwork

Reset the established demo. In Settings, enable **Look and report**, then
**Check now**, to demonstrate discovery. Alternatively, open the featured album
directly; its comparison also discovers the update.

| Album | What to show |
| --- | --- |
| Sex Bob-omb | Cosmetic casing/whitespace changes to two titles |
| Blues Brothers — Rawhide | A more precise date and missing label/catalogue details |
| Stillwater — Fever Dog | An older four-track total against the current three-track release: **Review assignments** retains known track identities |
| The Rural Juror soundtrack | One artist credit written with a different separator; one track has its own artwork |
| Soggy Bottom Boys | A MusicBrainz release merged into its surviving entry |
| Dingoes Ate My Baby | One missing embedded cover; the other tracks already have the right image |
| Mouse Rat | A 320-pixel local cover and a 960-pixel copy available from the archive |
| Barry Jive — After Hours | A smaller, visibly different archive alternative; harmless duration differences are informational |

1. Open **Rawhide**. Review the few proposed tag changes, apply them, and show
   the settled comparison. Expand History to inspect precisely what changed.
2. Choose **Undo tag changes** on that update. Show the earlier values return
   and the update becomes available again. Undo itself is recorded.
3. On **Mouse Rat**, use the Cover Art Archive check in the Artwork section.
   Compare the current and larger candidate images, then **Apply updates**.
   The resolution upgrade changes the folder cover; it preserves the embedded
   images. Inspect and undo that artwork change independently of tags.
4. Optionally show the merged-release case to explain why significant changes
   deserve review, or Dingoes to demonstrate filling a gap without replacing
   artwork that is already present.

For an explicit artwork choice, open Barry Jive and load the archive candidate.
Merely inspecting it changes nothing. Choose **use this artwork**, then apply
the proposal: the smaller alternative is allowed because you chose it. Undo
restores the previous images. To demonstrate a combined action, first undo
Barry's seeded date update, then choose the candidate. **Include artwork** on
**Apply updates** starts ticked; you can exclude it. Tag and artwork changes
have separate Undo controls in History.

Stillwater's **Review assignments** is a separate flow: inspect the retained
pairings, then accept the release. The confirmation's **Use artwork from the
selected release** starts unticked. Do not confuse this with the default
inclusion on ordinary updates.

Background checking currently looks and reports. Automatic application of
low-impact changes is a future direction, not something this recording claims.

## The visual assets

The sleeves are original geometric drawings with a limited palette and initials,
generated reproducibly by `python scripts/demo_artwork.py`. Mouse Rat's two sizes
use the same drawing. The assets contain no movie stills or reproduced sleeves.
The old recovery catalogue and colour placeholders remain test fixtures; they
are not the default public collection.
