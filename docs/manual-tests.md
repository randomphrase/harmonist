# Manual Test Plan

Checklist for verifying the prototype on real hardware against real services.
Automated tests (199+ as of writing) cover unit + integration + the demo-mode
flagship E2E flow. Manual tests verify everything that can't be automated:
real Bandcamp credentials, real MusicBrainz responses, network mounts, Plex
and Navidrome consumption.

Run before each "deploy to Synology" milestone. Each section is a separate
session — don't try to power through all of them in one sitting.

---

## Prerequisites

- [ ] Working `cookies.txt` for the user's Bandcamp account at
      `$HARMONIST_CONFIG_DIR/cookies.txt`
- [ ] Working `ignores.txt` at `$HARMONIST_CONFIG_DIR/ignores.txt` (copied
      from the user's existing bandcampsync setup)
- [ ] One Bandcamp purchase chosen as the **test target** — its `item_id`
      will be removed from a *temp copy* of `ignores.txt` so it re-downloads
- [ ] `HARMONIST_MAX_DOWNLOADS_PER_SYNC=2` (lower than default; safety net)
- [ ] Local checkout up to date, dependencies installed (`pip install -e .`),
      `make css` run
- [ ] Plex + Navidrome libraries pointed at the same music directory

---

## Section A — Live sync on macOS (developer machine)

1. [ ] Pick one **test target** item from `ignores.txt`. Note the `item_id`
       and the band/album. **Document which one** in a scratch file.
2. [ ] Copy `ignores.txt` → `ignores.test.txt`. Remove ONLY the test
       target's line.
3. [ ] Run with the temp ignores:
       ```
       HARMONIST_CONFIG_DIR=$HOME/.config/harmonist \
       HARMONIST_MUSIC_DIR=$HOME/Music/harmonist-test \
       HARMONIST_MAX_DOWNLOADS_PER_SYNC=2 \
       make run
       ```
4. [ ] Visit `http://localhost:8000`. Inbox should be empty (or show whatever
       was pre-existing in `~/Music/harmonist-test`).
5. [ ] Click **Sync**. Watch `/sync/status` go to "running". The chosen test
       album should download.
6. [ ] When sync completes, the new album should appear in the inbox as
       **Held (Bandcamp)** with a working "Open in Harmony" link and a
       "Recheck" button.
7. [ ] Verify on disk:
       ```
       ls ~/Music/harmonist-test/<Band>/<Album>/
       cat ~/Music/harmonist-test/<Band>/<Album>/.harmonist.json
       ```
       Sidecar should have `source: bandcamp`, `bandcamp.url`,
       `bandcamp.item_id`. `mb_release_id` should be null (recheck not run
       yet).
8. [ ] Verify ignores.test.txt got the item_id appended by bandcampsync
       (back-fill happens during sync).
9. [ ] Click **Recheck**. One of three outcomes:
       - [ ] Exact match → album auto-tags → DONE (disappears from inbox)
       - [ ] Approximate match → **Needs Confirmation** card with
             side-by-side table; click **Confirm** → DONE
       - [ ] No MB match → album stays Held. Open in Harmony, seed there,
             wait, click Recheck again.
10. [ ] When Done, open the file in mutagen (`python -c
        "from mutagen.mp4 import MP4; m=MP4('...'); print(m.tags)"`) and
        verify the Picard MBID atoms are written.

**Pass criteria:** the chosen item lands in DONE state with full MBID + cover
atoms; sidecar reflects the journey; no other items downloaded; cap respected.

---

## Section B — Plex picks up the tagged album

1. [ ] In Plex Media Server, rescan the library that contains the test
       album.
2. [ ] Verify Plex shows: artist artwork, album cover, release year, all
       track titles, label/catalog if set.
3. [ ] Verify the "Lookup MusicBrainz Release ID" → matches what's in the
       sidecar.

**Known caveat:** Plex's MusicBrainz agent must be enabled in the library's
metadata settings or it won't read MBID atoms.

---

## Section C — Navidrome picks up the tagged album

1. [ ] In Navidrome, trigger a library scan.
2. [ ] Verify the album shows correct cover, track titles, artist.
3. [ ] Verify the album appears under the right artist (not "Various
       Artists" unless intentional).

**Known caveat:** Navidrome does NOT fetch external metadata — embedded
cover + tags is all it sees. This is the strict test for our tagger.

---

## Section D — Pi against Synology SMB share

1. [ ] On the Pi, mount the Synology music share at `/mnt/synology-music`.
2. [ ] Verify write access: `touch /mnt/synology-music/.harmonist-write-test`
       then delete.
3. [ ] Run Harmonist (Docker or direct uvicorn) with
       `HARMONIST_MUSIC_DIR=/mnt/synology-music` and the user's cookies.
4. [ ] Repeat Section A's flow on Pi.
5. [ ] Confirm tag writes succeed over SMB (slow but should work).
6. [ ] Confirm sidecar writes succeed over SMB (the atomic rename via
       `os.replace`).

**Known caveat:** SMB tag writes are slow (5-10s per album). Acceptable for
prototype.

---

## Section E — Held → Recheck after Harmony seeding

1. [ ] Pick an album from your collection that's NOT on MusicBrainz yet.
2. [ ] Sync it via Section A. It should land as Held (Bandcamp).
3. [ ] Click "Open in Harmony". Verify the URL pre-populates the Bandcamp
       URL into Harmony's lookup field.
4. [ ] Seed the release on Harmony manually (out-of-band).
5. [ ] After MB has indexed the new release (can take minutes-to-hours),
       click **Recheck**. Should transition to Done.

---

## Section F — Manual ingest of a non-Bandcamp album

1. [ ] Drop a non-Bandcamp album (e.g. a CD rip) into the music dir.
       Should not have `.harmonist.json` yet.
2. [ ] Refresh the inbox. Album should appear as **Orphan**.
3. [ ] Click **Reconcile from tags**: if files have MBID atoms (e.g. from
       previous Picard tagging), should write a `source: manual` sidecar
       and transition to DONE.
4. [ ] If no MBID tags: paste an MB release URL into the manual form, or
       use the artist+title search helper. Verify chosen MBID triggers
       tagger → DONE.

---

## Section G — Cap-exceeded safety net

1. [ ] Set `HARMONIST_MAX_DOWNLOADS_PER_SYNC=0`.
2. [ ] Point at a test ignores file with at least 1 item missing (so
       sync *would* try to download).
3. [ ] Click **Sync**. Should abort with a clear error in the banner
       before any download starts.
4. [ ] Verify no album was added to disk.

This is the "wrong ignores file pointed at huge collection" insurance — a
must-pass before any production deploy.

---

## Section H — Cover art at original resolution

1. [ ] Tag an album that has cover art on CAA.
2. [ ] Open the cover.jpg file from disk; verify it's the full-resolution
       image from CAA (not a 250/500/1200 thumbnail).
3. [ ] Open one of the album's .m4a files in a tag editor — verify the
       covr atom contains the same bytes as cover.jpg.

---

## Sign-off

The prototype is considered "release-ready" when:
- [ ] Sections A, B, C pass on macOS
- [ ] Section G passes (cap enforced)
- [ ] Section D and section E + F pass on at least one platform

Section H and Pi-specific runs are nice-to-haves but not blockers.
