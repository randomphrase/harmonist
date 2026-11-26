# Recording the Harmonist README demo

**Lead with *After Hours* by Barry Jive and the Uptown Five.** Its three FLAC
files give only the year. The sample MusicBrainz release has the full date.
Show that difference, write it to the files, then put the old year back.
The artwork and Bandcamp stories work as separate clips, each with its own reset.

| Take | Opening frame | Closing frame |
| --- | --- | --- |
| 1 · Keep your library current · 60 s | Library, with the covers visible | The History for *After Hours* shows the original year restored across three files |
| 2 · Choose your artwork · 75 s | Mouse Rat’s Artwork section, with **Embiggen** visible | The original embedded and folder covers are back |
| 3 · Sync your Bandcamp purchases · 2 min | Inbox, with **Needs Linking** visible | The same Mouse Rat album now shows FLAC |

These shots use the public demo in **1.34.0**. Tag writes, artwork replacement,
purchase linking, ZIP creation and Undo all operate on files in the sandbox.
The Bandcamp, MusicBrainz and Cover Art Archive responses are fixtures. The
sample tracks contain short test tones. Keep system audio muted and stay on
Harmonist’s pages when showing the fictional releases.

## Set up a take

Start the demo from the repository root in the [development container](development.md).

```sh
make demo
```

Open <http://localhost:8000/?tab=library> and check for the **Demo Mode** banner.
Use the same window size, theme and zoom for all takes. Size the window so the
album comparison and both artwork rows are legible at README playback size.

The three main clips use Harmonist’s defaults. On a fresh demo you can go
straight to the clip’s preparation. If you’ve used this demo before, Settings
survive **Reset Demo** and server restarts. For example, the default tag
**Inbox** column has only **Structure** ticked, and Bandcamp downloads default
to FLAC. The other defaults are in [Configuration](configuration.md).

1. For a retake, wait for any running operation to finish. Choose **Reset
   Demo**, confirm and let the scan settle. This restores the sample files
   and starting history, clears purchase decisions and removes demo archives.
2. Open **Library** and choose **Clear** if a search or filter is active.
   In album History leave **Show details** off so the field changes are legible.
   Then do the preparation for the chosen clip.

A fresh established demo settles at **Library 9** and **Inbox 6**. The Inbox
has three **Needs MBID** albums, one **Needs Linking** album and two **Artists
not identified**. *A Most Excellent Journey* needs a purchase link, while
*Wyld Stallion/artist.nfo* needs an artist identity. Opening albums discovers
more findings, so use titles and badges as cues once the take starts.

For faster rehearsal use `HARMONIST_DEMO_DELAY=0 make demo`. For slower tag
writes and artwork lookups, stop the server and use `HARMONIST_DEMO_DELAY=0.5 make demo`.
FLAC is the fixture used in take 3. The demo also has ALAC, MP3 and AAC downloads.

## 1. Keep your library current

**Prepare off camera.** After a demo reset, open **Barry Jive and the Uptown
Five — After Hours**. The sample History includes an earlier date update.
Choose **Undo tag changes** on **Updated tags from MusicBrainz** and confirm.
Wait for the comparison to show **2024 → 2024-01-01** and **Apply updates**.
Return to the unfiltered Library. The cover now has an **Enrichment** badge.

| Time | On screen | Voiceover or caption |
| --- | --- | --- |
| 0–10 s | Start on the Library covers. Pause on the **Enrichment** badge on *After Hours* and click the cover. | “Here’s *After Hours*, by Barry Jive and the Uptown Five, in the sample library. Harmonist has found an update for it.” |
| 10–25 s | Hold on **Date**. The files show **2024** and MusicBrainz shows **2024-01-01**. | “My files have the year. MusicBrainz has the release down to the day. That’s the only tag that would change.” |
| 25–40 s | Press **Apply updates**. Scroll to **History** and pause on the newest **Re-tagged** entry and its **Date** row. | “One click writes the full date to all three tracks. Here’s the old value alongside the new one.” |
| 40–60 s | Choose **Undo tag changes** on that entry and confirm. Hold on **Tags put back — 1 field across 3 files**. | “If I want the year back as it was, I can undo that change right here.” |

**End check.** Scroll back to the comparison. The short date and the Enrichment
proposal should be back. If preferred, use this as the closing shot.

**Optional cutaway for background checks.** In **Settings → Background Updates**,
show **Check MusicBrainz for updates → In the background** and the two routing
grids. By default, Structure goes to the Inbox and Enrichment
stays in the Library for review. Other settings can route eligible updates to
the Inbox or apply them automatically. The grids tell that story in one shot.
Restore **Off** if you saved the change.

## 2. Choose your artwork

**Prepare off camera.** Reset, open **Mouse Rat — The Awesome Album** and scroll
to **Artwork**. Wait for **Now**, **After Apply** and the archive candidate.
Both current rows show 320×320. The initial preview upgrades only `cover.png`.
Select **All 3 tracks**, which still says **Unchanged**, and leave the picker
on **This release**. For the zoom shot, size the Safari window so the two
whole sleeves fit side by side and **Fit** shows less than 100% when enlarged.
Return to the album page before rolling.

| Time | On screen | Voiceover or caption |
| --- | --- | --- |
| 0–10 s | Show the two 320×320 **Now** rows, then click **Embiggen** on camera. | “These MP3s have a small cover. Let’s take a closer look at the one the Cover Art Archive has.” |
| 10–22 s | Stay in the enlarged picker. Let the original load. Hold on both whole sleeves and their captions: **All 3 tracks** at 320×320 on the left, **This release** at 960×960 on the right. | “Same sleeve. On the left is the copy in my files. On the right is the archive image, three times as wide.” |
| 22–35 s | Click the **MR** lettering in either sleeve to zoom both panes. Hold on the same letters in each and the readout **100% · smaller one at 3.0×**. | “Now the archive image is at its actual size. Harmonist enlarges my copy to match, so I can compare the lettering in the same spot.” |
| 35–48 s | Press **Use** between the images while still enlarged. The picker returns to the page. Pause on **After Apply** showing 960×960 for both the tracks and `cover.png`. | “I’ll use this one for the tracks. The folder cover already has the same upgrade lined up.” |
| 48–60 s | Press **Apply artwork**. Hold on both **Now** rows at 960×960. | “That writes the new cover to the three MP3s and the folder.” |
| 60–75 s | In **History**, choose **Undo** on the artwork change and confirm. Return to the two **Now** rows at 320×320. | “And that puts both originals back.” |

**End check.** Both **Now** rows are 320×320 again. The folder upgrade can be
offered again under **After Apply**. If **Use** shows a tick before you have
selected the upgrade, check both the row and the source. **This album** is the
copy already on disk. **This release** is the archive copy for this shot.

### Other artwork inserts

- **Barry Jive — After Hours.** Its archive front is smaller. Use **This release**
  separately for **All 3 tracks** and `cover.png`, then apply and undo.
- **The Rural Juror (OST).** Step through **This album** images to show the
  distinct track sleeve. Select the shared row to preview a change while
  leaving that individual track alone.
- **Electric Mayhem — Can You Picture That?** Use **Next image** to browse to
  the other pressing’s cover. End the shot on that sleeve.
- **Dingoes Ate My Baby.** Show the proposed fill for one track missing its
  embedded cover. On the same album, **Tracks** shows the proposed composer,
  lyricist and writer credits beneath the song titles.

## 3. Sync your Bandcamp purchases

**Prepare off camera.** Reset with FLAC selected. Start in the Inbox with **A
Most Excellent Journey** under **Needs Linking**. Save the first sync for the
take. The newer artist NFO card for Wyld Stallion can stay in the Inbox.

| Time | On screen | Voiceover or caption |
| --- | --- | --- |
| 0–20 s | Hover over **Sync Bandcamp** to show **Link-only** ticked. Leave it as set and press the sync button. Wait for **Potential downloads**. | “I already have this Wyld Stallion album. While it needs linking, sync connects the purchase and lists what else I own.” |
| 20–45 s | On Mouse Rat’s card, click the suggested album under **Already in your library?** to verify **MP3** in a new tab. Close that tab and press **Link** beside the local album. | “Here’s another purchase I already have as MP3s. I can link that copy and keep listening to it.” |
| 45–70 s | Choose **Don’t download** for **Autobahn — Nagelbett**, then **Download** for **CB4 — Straight Outta Lowcash**. Press **Sync Bandcamp** again. Open CB4 in Library and show its FLAC format and matched release. | “I’ll skip Autobahn and fetch CB4 as FLAC. Its Bandcamp release has an exact MusicBrainz match, so Harmonist tags the download.” |
| 70–120 s | Return to Mouse Rat’s album page. Show **MP3**, press **Re-download** and pause on the prompt naming FLAC and the ZIP. Accept and stay on the page. The archiving status can pass quickly with three small files. Hold on **FLAC** and the same matched album when the replacement arrives. | “To upgrade this copy, Harmonist archives my original MP3s and downloads fresh FLAC files from Bandcamp.” |

**End check.** Mouse Rat has three FLAC tracks. The ZIP at the top of the demo
music folder contains the three original MP3s. CB4 is matched and Autobahn is
under **Won't download** in Settings. Skipping Autobahn *before* the second
sync matters because a full sync can fetch other eligible purchases. For a ZIP
insert, find the sandbox path under **Settings → Server → Music library**.

## Follow-up takes

### Review two albums in the Inbox (about 45 seconds)

Reset. In **Settings → When a tag update is found**, tick **Inbox** for
**Enrichment** and save. Leave the **Apply** column clear. Open **Rawhide**
and **Dingoes Ate My Baby — Little Bit o’ Hoot, Whole Lotta Nanny** to load
their comparisons. Start the take on the Inbox’s **Tag updates · Enrichment**
group. Rawhide has release details to add, while the Dingoes tracks have work
and songwriting credits. Select both cards and press the group’s **Apply**.
End on **Tag update applied (enrichment)** and **Undo tag changes** in either
album’s History. Afterwards, return the tag **Inbox** column to **Structure**
only and save.

### Identify an artist file (about 45 seconds)

Reset and run the first **Sync Bandcamp** off camera. Linking *A Most Excellent
Journey* gives the empty `Wyld Stallion/artist.nfo` a Library album beneath it.
In **Inbox → Artists not identified**, the **Wyld Stallion** card now offers
**From contained albums**. Before that sync the card opens on **By name**.

Show the suggested album credit **Wyld Stallyns**, then choose **Identify**.
The card leaves the Inbox. Open **Artists → Wyld Stallion** and hold on the
**Artist info** fields *San Dimas rock duo*, *Group* and *Formed 1988*.
Choose **Undo NFO changes** in History and confirm. The file is again
**Not identified**.

> The album credits tell me which artist this file belongs to. Identifying it
> fills in the local artist file, and History shows each field.

### Review a different pressing (about 60 seconds)

Reset and open **Dr. Teeth and the Electric Mayhem — Can You Picture That?**.
Wait for the editions check. Return through **Library → Possible mismatch**.
Show the detected **CD** origin, both disc-folder paths and the original UPC
under **Additional tags**. Beside the UK pressing marked **Barcode matches**,
choose **Review**. In the dialog, inspect the four track pairings and **Tag
changes**, then **Confirm suggestion**. End on the UK pressing with both disc
folders and all four tracks. Its barcode matched a sibling in the same release
group. The demo has no general barcode or disc-ID search result.

### Review track assignments (about 45 seconds)

- **Correct a track total.** From a reset, choose **Library → Structural gaps →
  Incomplete → Fever Dog**. Its files say 3 of 4. Let the **Structure** comparison
  open the assignment editor, or choose **Review assignments**. Keep the three
  pairings, inspect **Tag changes** and press **Apply assignments**. All three
  files now carry the correct total and the Incomplete badge clears.
- **Find the second disc.** On a fresh Inbox, choose **Review** on **Sextina
  Aquafina — Aquafina After Dark (The Remixes)**. Pause on the proposed **Disc
  2** folder and its four tracks beside disc 1’s four. This works as a
  preview-only insert. The second disc is already on disk.

### Apply a tagging preference (about 60 seconds)

From a reset, open **A Most Excellent Journey** under **Needs Linking**.
The default artist-name preference offers *Wyld Stallion* for the credited
*Wyld Stallyns*. In **Settings → Tagging**, set **Album title** to **Title +
release disambiguation** and save. Reopen the album to preview *(San Dimas
edition)* in its title, then apply the reviewed update. Restore **Title only**
for the next take. Use the live example in Settings for the **if needed**
option. The public collection has one confirmed pressing per group at most.

### Adopt an existing collection (about 60–90 seconds)

Stop the demo server and start it with `HARMONIST_DEMO_ADOPTION=1 make demo`.
This recipe seeds the same music without sidecars or prior activity. For a
startup shot, start capture before launching the server. The normal scan adopts
the albums that already carry MusicBrainz tags. Open Electric Mayhem to show
both original disc folders under one release. In the Inbox, search **Sonic
Death Monkey** by name, **Review** its release and confirm the track match.
Return to Rawhide to show that an adopted album can still have an update awaiting
review. Stop the server, unset the adoption variable if exported, and start
`make demo` again to return to the established collection.

## The README cut

Trim waits between a click and its result. Leave the before and after on
screen long enough to read the values. Keep the Demo Mode banner in the opening
frame and use the voiceover lines as captions for muted playback.

Once take 1 is uploaded, use its URL in the [README Demo section](../README.md#demo)
with this caption.

> **Keep your library current.** Review a MusicBrainz update, apply it to three
> files and undo it from the album’s history.

Put the uploaded URL on its own line so GitHub embeds the video. The artwork
and Bandcamp clips can follow under their own captions as they become available.
Play each embed in the rendered README before publishing.

The demo sleeves are original drawings generated by
[`scripts/demo_artwork.py`](../scripts/demo_artwork.py). Mouse Rat’s two sizes
use the same drawing. For footage about giving back to MusicBrainz, a positive
contribution example would need a new fixture. The existing CD barcode
mismatch supplies the pressing-review shot above.
