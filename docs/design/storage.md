# What is stored where

Harmonist keeps three kinds of state, and each lives where its lifetime and its
readers put it:

| What | Where | Why there |
|---|---|---|
| What the user decided about an album, and its link to a release and a purchase | The **sidecar**, `.harmonist.json`, in the album's folder | It travels with the music. Move, copy or back up the folder and the decisions go with it. |
| What Harmonist did, and what it has heard from MusicBrainz and the Cover Art Archive | **`activity.db`**, SQLite in the config folder | Append-only history and caches, polled constantly, and nothing a user would want inside their music folders. |
| What has been downloaded from Bandcamp | **`ignores.txt`** in the config folder | It's bandcampsync's own record, and bandcampsync is what downloads. |

Everything else is either derived or held in memory and rebuilt.

## The sidecar

### Why a file beside the music, not a database

The sidecar holds the only things about an album that its tags can't: which
purchase it came from, and what the user decided. Keeping them beside the audio
means they survive anything that happens to Harmonist: a lost config folder, a
fresh install, the library mounted on a new machine. The scan reads them along
with the tags it reads anyway, and there's no second store to keep in step with
the folders.

The cost is that sidecars live on the user's hardware, beyond reach. There is no
migration runner and no way to fix a bad format change after release, so the
sidecar is treated as a published interface with no version negotiation:

- **The schema version is a hard gate, and it stays at 1.** A sidecar with any
  other version is refused, which makes every album vanish. Bumping it is a last
  resort needing explicit sign-off.
- **Unknown keys are ignored.** That's what keeps change cheap: a new field is
  compatible in both directions, and a retired field just stops being read.
  Renaming or repurposing a field is not compatible, and isn't done.
- **Defaults are omitted when written**, so a person debugging their own library
  sees only the fields that say something.

The `sidecar` skill covers the mechanics of changing a field.

### What earns a place in the sidecar

A field must record a **decision or observation that can't be re-derived**, have
a **reader that changes what the user sees**, and not duplicate something
recorded more authoritatively elsewhere. The last test is the one most often
missed: the expected track count passed the first two and was still removed
(#195), because every tagged file already carries it.

Status fields fail the first test by definition; see
[principles](principles.md#state-is-derived-never-stored). Audit-like
breadcrumbs ("matched via barcode on…") belong in `activity.db`.

### The fields

| Field | Why it exists |
|---|---|
| `store_url` | Where the album was bought. It is evidence of provenance for matching, and the key Bandcamp linking starts from. Any store Harmony understands; the store is read from the host. |
| `bandcamp.item_id`, `band_id` | The purchase this album is linked to: what Re-download fetches, and what keeps a sync from downloading it again. |
| `bandcamp.candidate_item_ids` | Several purchases could be this album and nothing exact tells them apart. Recorded instead of guessing one, and enough to take the album out of Needs Linking. |
| `bandcamp.is_private` | Bandcamp said the purchase is private, so its URL is never offered to MusicBrainz or Harmony. Absence doesn't mean public. |
| `bandcamp_downloaded` | Harmonist itself downloaded these files. The only proof of download provenance that doesn't depend on tags; see [contributions](contributions.md). |
| `mb_release_id` | The confirmed release. Its presence is what separates Needs MBID from everything after it. |
| `temp_uid` | A stable ID until a release is confirmed. Exactly one of this and `mb_release_id` is set. |
| `mb_match_candidate` | A suggested release awaiting the user, with the comparison that justified it. Also carries why it was suggested, when that's a possible mis-tag or a surrender, and `found_by`: how it was found (store URL, barcode, name search, MBID, undo). That is the breadcrumb rule above, not an exception to it: the lookup and the confirm are separate requests, so the suggestion carries the fact until the confirm writes it to `activity.db`, and it goes with the suggestion (#639). |
| `tagged_at` | When Harmonist last tagged the files, so a later change by another tool can be noticed. |
| `added_at` | When Harmonist first met the album, shown on its page. |
| `purchase_unavailable` | The user accepted that there is no Bandcamp purchase to link. Without it the album would be surrendered again on every full sync. |
| `tracks_unavailable` | The user accepted an incomplete album as finished ("Don't warn me about this"). It removes the album from the Incomplete filter but doesn't change the state: the album really is short. The label describes what the control does rather than claiming the tracks can't be had, because the user can't know that (#245). |
| `accepted_release_id` | The release the user says they bought, when the evidence suggested another. Keyed to the release rather than a flag, so matching a different release lapses it by construction. |
| `video_media` | Which of the release's media are video, the one release fact the files can't carry, since the missing discs have no files. `null` means not asked and `[]` means asked and none, a distinction that stops a video-free release being asked about forever. |
| `borrowed_artwork` | Archive images Harmonist wrote as stand-ins because the release had no front of its own: the release group's or another release's, each with the listing it came from and the release it stood in for. The bytes look the same whether an image was borrowed for want of one or chosen over the release's own, and only a borrowed one should give way when the release gets a front (see [artwork](artwork.md#a-stand-in-gives-way)). Held only while the album carries the image and is matched to that release, so a rematch or a different choice lapses it. |
| `downloaded_at`, `notes` | Legacy. Written and carried, but nothing reads them. |

### Which fields are audited

A change to a field recording identity, provenance, a user decision, or an
observation that changes how the album is classified is recorded in the audit
log, since if it moved behind the user's back they'd need to know what it was. A suggestion is not audited: it's rewritten on every re-check, and would
bury the real changes. The timestamps are covered by the events that set them.

## `activity.db`

One SQLite file holds several stores with one thing in common: they're
Harmonist's own record and cache, not the user's data, so nothing about them
belongs in a music folder.

- **Events**: the Activity feed and the audit log, append-only (see
  [history](history.md)).
- **Tag changes**: per-file before-and-after values for every tagging, which is
  what makes undo possible.
- **Album aliases**: old ID to new ID, recorded when an album is re-identified
  (see [the model](model.md#identity)).
- **The MusicBrainz and Cover Art Archive caches** (see
  [external services](external-services.md)).
- **Ignored updates**: which albums the user muted, and against which version of
  the release (see [staying current](staying-current.md#ignoring-an-update)).
  Kept here rather than in the sidecar because muting fifty albums would
  otherwise be fifty audited writes into the music folders, each waking the file
  watcher.

Schema changes go through the `schema-migration` skill: migrations only ever
append, since a user's database can't be reached to repair.

Two things kept in the config folder are files rather than rows, because they
hold images: **kept artwork**, the images Harmonist replaced (see
[artwork](artwork.md#a-backup-is-what-permits-a-replacement)), and the **Cover
Art Archive image cache**. Images would bloat a database that's read on every
feed refresh.

The image stores are siblings: undo backups in `artwork`, downloaded images in
`artwork-cache` (#688). Keeping the disposable cache outside the backup store
makes their different backup and retention needs explicit. Upgrading moves only
the old nested cache, with one directory rename; the irreplaceable backups never
move. A conflicting destination or a failed rename stops startup rather than
choosing which images to discard or leaving a partially copied store.

## `ignores.txt`

bandcampsync's list of purchases it considers downloaded. Harmonist doesn't keep
a second list: a second record would have to agree with this one, and
bandcampsync reads this one. Linking an album already on disk adds its purchase
here, which is what stops the next sync downloading it again; Re-download removes
it.

bandcampsync reads the file when a sync starts and rewrites it whole at the end,
so it's edited only between syncs. That's why Re-download refuses to start
during a sync.

## Held only in memory

Some state is deliberately not persisted, because a restart can rebuild it and
the decisions that matter are already recorded somewhere durable:

- **Potential downloads**, purchases a sync couldn't match. The next sync finds
  them again; the user's decisions go to the sidecar and `ignores.txt`.
- **Albums being re-downloaded.** The decision to re-download persists as a
  missing folder plus a purchase removed from `ignores.txt`, which is exactly
  what makes any later sync fetch it. A restart in the seconds between the
  archive and the sync loses only the Inbox card and the carried release; the
  purchase then appears as a potential download.
- **The scan snapshot and its indexes** (albums by purchase, by URL, by slug) and
  **the state counts**. Each full scan rebuilds them, so drift from a missed
  update corrects itself. Between scans they're updated from the one place every
  sidecar write passes through, not by each caller remembering to.
- **Whether an update is available.** Recomputed from the cached release and the
  files, and rebuilt from the cache after a restart without any requests.

A JSON file for any of these would add a format, and a migration, for something
that can be recomputed.
