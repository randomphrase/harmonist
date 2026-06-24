# Harmonist — Design

**Status:** draft for review
**Audience:** the implementation team
**Scope:** the prototype that runs locally and deploys to Synology

---

## 1. Purpose

Harmonist streamlines the workflow from *album purchased on Bandcamp* to *fully tagged file in Plex / Navidrome*, using MusicBrainz (MB) as the source of truth. It is a **workflow tool**, not a tagger. Picard-style tagging is a step we automate inside that workflow.

### Non-goals

The following are explicitly out of scope for this prototype:

- **No automatic polling of Bandcamp.** Sync is a button. The user buys infrequently and Bandcamp has no purchase webhook.
- **No in-app MusicBrainz seeding form.** When an album isn't on MB, we link out to [Harmony](https://harmony.pulsewidth.org.uk). Harmony does the seeding work.
- **No database.** State lives in `.harmonist.json` sidecars next to each album. bandcampsync's `ignores.txt` is the source of truth for "what's downloaded".
- **No C++ bindings.** The earlier readme mentioned them; they don't exist and we're not adding them.
- **No multi-user / no auth.** Single-user app behind the user's network.
- **No format conversion.** Files are downloaded in the requested format (default FLAC) and tagged in place.
- **No fix-it-yourself for inconsistent dirs.** Picard exists for that. See §15.2.
- **No transcoding, no folder splitting.** See §15.4.

---

## 2. Use cases

### 2.1 Bandcamp sync (the canonical flow)

Bandcamp setup is **deferred, not up-front**. On a fresh install with no
cookies configured, the header shows **Set up Bandcamp sync** instead of
**Sync Bandcamp** — a standing reminder that onboarding is incomplete.
Clicking it opens a modal to paste or upload a `cookies.txt` (with a link
to the bandcampsync instructions); saving it writes the cookies file and
flips the button to **Sync Bandcamp**. Until then the rest of the app
(manual ingest, reconcile, tagging) is fully usable — this deferral is
the whole reason the `NEEDS_SYNC` state exists.

1. User buys an album on Bandcamp out-of-band.
2. User opens Harmonist, clicks **Sync** (after one-time cookie setup, above).
3. Harmonist downloads new items via bandcampsync. For each item, it captures the public Bandcamp album URL and writes a `.harmonist.json` sidecar.
4. Inbox updates live as albums land (HTMX poll while sync is in-flight).
5. For each new album, MB lookup runs by Bandcamp URL.
6. If MB has a release linked to that URL → Harmonist tags the files Picard-compatibly. The album disappears from the inbox.
7. If MB has no match → the album sits in the inbox as **Needs MBID** with an "Open in Harmony" button and a "Recheck" button.

### 2.2 Manual ingest (non-Bandcamp music)

1. User drops an album directory into the music dir.
2. User clicks **Add Manual** in the inbox (or Harmonist offers it for a directory it sees with no sidecar).
3. User pastes an MB release URL/MBID, or uses a name-based MB search helper.
4. Harmonist writes a sidecar with no `store_url` (the manual case) and the resolved `mb_release_id`, then tags the files.

### 2.3 Held-album recheck

1. User has previously seeded a release in Harmony.
2. User clicks **Recheck** on a Held album (or **Recheck All**).
3. Harmonist re-runs the MB URL lookup. If now matched, it tags. If still unmatched, the album stays Held.

### 2.4 Re-tag from MB *(planned — post-v1)*

1. User edits a release in MB (track titles, dates, etc.).
2. User clicks **Re-tag** on a matched album.
3. Harmonist re-fetches the MB release and rewrites the file tags.

**Not in the initial release.** Captured here so the state model and UI
don't preclude it later. No `COMPLETE → COMPLETE` or
`INCOMPLETE → INCOMPLETE` "Re-tag" self-loop appears in the §3.1
diagram until this use case is in scope.

### 2.5 Per-album reconciliation

Instead of a "bootstrap" event, reconciliation is **continuous and per-album**. Whenever the scanner encounters an album that has MBID-tagged files but no `.harmonist.json` sidecar, the reconciler runs once for that album to derive the right sidecar.

For each such album (`harmonist.reconcile.reconcile_album`):

1. Read the `MusicBrainz Album Id` atom from the album's tracks.
2. Read the `©cmt` (comment) tag from the same file.
3. Fetch the release's URL relationships from MB (`mb_lookup.fetch_release_urls`).
4. **If `©cmt` mentions any `bandcamp.com` URL AND MB has at least one Bandcamp URL relationship for the release:** write a sidecar with `store_url` set to MB's canonical Bandcamp URL, `bandcamp.item_id=None` (filled in later by sync). The album shows as **Needs Sync** until the next sync resolves the item_id.
5. **Otherwise:** write a sidecar with no `store_url`. Album shows as **Complete** (already tagged).

The `©cmt` evidence rule prevents false-positive "purchased on Bandcamp" classifications when a user happens to own an album that's *also* available on Bandcamp but they bought it elsewhere (Beatport, CD rip, etc.).

#### Linking purchases to on-disk albums

When the user runs Sync (cookies present), `bandcamp_hook.HarmonistSyncer`
iterates their Bandcamp purchases and ties each to an album already on disk —
filling in `bandcamp.item_id` (and `band_id`) **without re-downloading**. There
are two entry points, because bandcampsync treats already-downloaded items
differently from new ones:

- **New / not-yet-ignored purchases** flow through `sync_item` during the
  download loop. Before downloading, it tries an **exact store_url** match
  (`find_existing_album_by_url`), then a **slug fallback**
  (`find_existing_album_by_slug`, see below); a hit fills in the item_id and
  skips the download, a miss downloads as normal.
- **Already-downloaded purchases** are in `ignores.txt`, so bandcampsync skips
  them entirely — `sync_item` is never called for them. They are handled by a
  separate pre-pass, `_backfill_ignored_purchases`, run once at the start of
  every sync. This is where the **bulk** of linking happens: after a nuke, or
  for any library already on disk, *every* purchase is ignored.

**The slug.** All matching below the exact-URL rung is on the **release slug** —
the `/album/<slug>` (or `/track/<slug>`) path segment, subdomain stripped.
Bandcamp routinely cross-lists one release under several subdomains (a label page
**and** the artist's own page), so an on-disk `store_url` of
`thelabel.bandcamp.com/album/home` (from MusicBrainz's relationship) and a
purchase at `theartist.bandcamp.com/album/home` share the slug `album/home`. The
slug is Bandcamp's **stable per-release handle** — minted once, immutable even as
the artist renames the band or re-letters the title — which is what makes it a
safe key. The item-type segment is kept so `album`/`track` can't collide.

##### The backfill: a two-phase matcher

`survey_album_links` walks the library once into two structures: unlinked albums
grouped by store_url slug, and the set of item_ids **already** linked to an
album. Candidate purchases are those that are ignored AND not already linked —
the linked-id guard stops a purchase correctly attached to one album from being
re-attached to a sibling that merely shares a slug (a standard + a long-form
edition sold from the same page).

**Phase 1 — per store_url slug** (`_resolve_slug_group`): for each slug, take its
unlinked albums and the candidate purchases whose URL carries that slug.

1. One album + one purchase → **link directly**.
2. Several editions share the page (so several albums and/or purchases share the
   slug) → separate them by an **exact normalized title match**: the album's
   folder name vs the purchase's item title, lowercased and reduced to
   alphanumerics. Link only a **unique** match; then link a lone
   1-album/1-purchase remainder by elimination.
3. Purchases the title couldn't pin to an album → record them as an **ambiguous
   link**: store the candidate item_ids on the album (`bandcamp.candidate_item_ids`)
   with no single `item_id`. The album leaves Needs Sync for **Complete** — it's
   as resolved as we can get without per-item track data; a future re-download
   can collapse the set by fetching each candidate's tracklist.
4. An album with no candidate purchase for its slug is handed to phase 2.

**Phase 2 — title fallback across a URL mismatch.** Some editions sit on one
public Bandcamp page but each *purchase* carries its own URL — e.g. a standard
and a long-form edition where MB records only the public page on both releases,
yet the long-form purchase resolves to a different slug. Phase 1 links the
standard (its purchase URL matches the album's store_url); the long-form album
matches no purchase by slug and falls to phase 2. Here, an album still unlinked
is matched to the one **remaining** purchase whose title uniquely equals its
folder name (same normalization), **ignoring the URL**. A unique match links it;
ambiguous or absent → left for surrender.

A phase-2 link **always** has a URL mismatch by construction (that's why it fell
out of phase 1), so the tagged release's store_url differs from the matched
purchase's URL. That can mean the tag is the wrong edition (a mis-tag), OR a
correctly-tagged edition whose MB URL is the shared public page — indistinguishable
without comparing tracklists. So we **link and log a WARNING** ("possible mis-tag",
naming both URLs) into the Activity feed for the user to judge. This is the
*one place* matching crosses the no-guessing line: we act on strong unique-title
evidence but never claim certainty.

**Why title matching is allowed here (a deliberate reversal).** An earlier design
forbade *any* artist/title fallback as "noise", and broad fuzzy artist+title
search against all of MusicBrainz is still forbidden (that belongs to manual
ingest, §2.2). What's different is the **scope and strictness**: phase-1/2 title
matching runs inside an already-narrow set (purchases the user provably owns vs
on-disk albums), requires an **exact** normalized match (a near-miss falls
through rather than mis-linking), and demands **uniqueness**. The edition
qualifier that fuzzy matching erases (`[lp edition]`, `(long-form edition)`) is
exactly what makes the exact match *discriminate* editions instead of colliding
them. Title is signal here *because* it's exact and scoped; the old objection was
to loose, unscoped matching.

**On any hit** (slug or title), sync fills in `item_id`/`band_id`, adopts the
**purchase's** URL as the new `store_url` (where the user actually bought it —
the on-disk URL was the stale MB-relationship one), appends the id to
`ignores.txt`, and skips the download.

##### Forcing a full sync

A normal sync stops at bandcampsync's collection checkpoint
(`.bandcampsync-state.json`) and never re-pages older purchases — so an album
waiting to link (**Needs Sync**) whose purchase is *old* would never be seen. So
if any album is in Needs Sync at sync start, Harmonist **clears the checkpoint**
for that run, forcing a full re-page (bandcampsync writes a fresh checkpoint at
the end, so subsequent syncs return to incremental). Self-limiting: a full sync
resolves every Needs Sync album — it either links it, or surrenders it.

##### Surrender — when nothing matches

After the backfill and the post-sync mis-tag pass, an album still in Needs Sync
on a **full** sync has genuinely no matching purchase. Rather than nag forever,
Harmonist **surrenders** it: demote to Needs MBID, keeping its current release as
a **read-only** suggestion (`mb_match_candidate.unmatched_purchase`) plus a "no
purchase found" note, so the user can seed the release on Harmony or correct the
store URL. Surrender fires **only on a full sync** (`collection_checkpoint_token
is None`) — on a partial sync, "no match" might just mean "not paged this run",
so we only warn there and leave the album alone. If a surrendered album is tagged
as the *same* MB release as one already linked to a purchase, a non-committal
WARNING flags a possible duplicate copy — or a release legitimately split across
directories (§15.3), which we don't try to tell apart.

After a **full** sync, an album reaches surrender for exactly one of three
reasons — its `store_url` slug matched no purchase slug *and* its folder title
matched no unique purchase title:

1. **No purchase exists.** Acquired outside Bandcamp (CD rip, promo, gift, or a
   free/name-your-price download that isn't in the *purchase* collection) but
   carrying a bandcamp-ish `store_url`. Benign — there is genuinely nothing to
   link.
2. **Wrong/stale `store_url` *and* a non-matching title.** A wrong-edition URL,
   or a renamed folder that `_norm_title` can't bridge. Here the **tag itself
   may be wrong**.
3. **An uncaught mis-tag.** The post-sync mis-tag pass only fires when the user
   owns a *sibling edition in the same MB release group*, and exactly one. A
   wrong release in a *different* release group, ≥2 owned editions (ambiguous),
   or not owning the correct edition's purchase all slip past it — and the album
   really is mis-tagged, just unprovably.

This is precisely why surrender **defers to the user instead of silently marking
the album Complete.** "Not a *detectable* mis-tag" is a far weaker claim than
"proven correctly tagged": cases 2 and 3 put the tag itself in doubt, and case 3
would bury a real mis-tag in the Library where it would never be seen again. The
only thing definitely missing is the Bandcamp `item_id` (a re-download handle),
but the *tag's* correctness is exactly what we can't assert — so the album stays
in the inbox until the user resolves it. (Auto-marking these Complete was
considered and rejected for this reason.)

The inbox also surfaces a Needs Sync album with two manual affordances:
**Try a different URL** (supply the correct Bandcamp URL → next sync re-matches)
and **Mark purchased elsewhere** (clear `store_url`, drop the bandcamp block →
Complete).

Bandcamp credentials remain optional throughout: the tool is fully usable for a non-Bandcamp library (per use case 2.2). Reconciliation works without cookies — it just leaves bandcamp-sourced albums in `NEEDS_SYNC` indefinitely (which is fine if the user never plans to add cookies).

### 2.6 Bulk import of an existing library

A user with a pre-existing library (hundreds to tens of thousands of
albums, typically already Picard-tagged) points Harmonist at their music
dir for the first time.

**Mechanically identical to the other use cases.** The scanner walks the
tree; every album dir becomes `NEW`; auto-reconciliation iterates them
via the existing `ReconcileRunner`. Already-MBID-tagged albums (the
common case for a Picard-managed library) flow straight to `COMPLETE`
(or `NEEDS_SYNC` if `©cmt` evidence + MB URL relationship point to
Bandcamp). Albums without an MBID atom stay in `NEW` and surface in the
inbox for user attention.

**No new states, transitions, or schema fields.** The only thing that
differs from the canonical Bandcamp-sync flow is volume.

**What volume implies, in practice:**

- **Pacing**: `ReconcileRunner` already rate-limits MB queries at
  1 req/sec (`MB_RATE_LIMIT_SECONDS`) per the MB ToS. A 5,000-album
  bulk reconcile takes ~80 minutes of wall time, dominated by network.
  Acceptable for a one-time onboarding; the user closes the tab and
  comes back later.
- **Progress UI**: the existing `reconcile/status` JSON
  (`current_item`, `completed`, `total`) is the right primitive.
  The inbox already polls it during a run.
- **Inbox triage**: a bulk import surfaces the user's actual
  problem albums (untagged, partial-tag, inconsistent) as a working
  set. The state grouping (§3) is what makes a thousand-row inbox
  navigable — the user works one state at a time.
- **No "import" button**: the user just drops files in the music dir
  (or mounts an existing dir). `/tasks` auto-kicks reconcile when it
  sees any `NEW` album. No special bulk-import mode.

**Assumptions / out of scope:**

- Library is **internally consistent** per §15.2. Bulk-import does not
  attempt to untangle mixed-album dirs; those land in `INCONSISTENT`
  and the user resolves with Picard.
- Library is not actively being written to by another tool during the
  import. Concurrent Picard runs against the same dir could race the
  scanner; user is expected to do one or the other.
- No deduplication, no MD5/fingerprint matching across the library —
  Harmonist treats each album dir independently.

---

## 3. State machine

Every album in the music dir is in exactly one state, derived from the presence/contents of its `.harmonist.json` sidecar plus the file tags.

| Sidecar | `mb_release_id` | `mb_match_candidate` | Files tagged | File count vs MB tracks | State | Inbox? | UI affordances |
|---|---|---|---|---|---|---|---|
| absent | — | — | — | — | **New** | yes | "Reconcile from tags" / "Recover store URL" / manual MBID form |
| present | null | null | n/a | — | **Needs MBID** | yes | If `store_url`: "Open in Harmony" + "Recheck"; always: manual MBID form |
| present | null | set | n/a | — | **Needs MBID** (with suggestion) | yes | Adaptive card: side-by-side files vs MB release (per-track green/amber length deltas) + "Confirm" / "Confirm as Incomplete" / "Dismiss suggestion", with the find/assign tools available under a disclosure. Sorted first in the group. |
| present | set | n/a | no | — | **Tagging** (transient) | yes (briefly) | spinner |
| present, `store_url` is bandcamp, `bandcamp.item_id=None` | set | n/a | yes | — | **Needs Sync** | yes | "Try a different URL" / "Mark purchased elsewhere" |
| present | set | n/a | yes | equal | **Complete** | no | (hidden — visible in library) |
| present | set | n/a | yes | less | **Incomplete** | no | library badge "N of M tracks"; "Recheck — maybe more tracks now" |

**Needs MBID is a single state** whether or not a `mb_match_candidate`
suggestion is attached — there is no separate "Needs Review" state. The
card adapts: with a suggestion it leads with the side-by-side + Confirm;
without, it leads with the find/assign tools. This avoids a confusing
round-trip (reject → re-assign) when the user just wants to swap a wrong
MBID — they can do that inline, and dismissing a suggestion stays put.

**Two refinements from the purchase-matcher (§2.5):**

- **Ambiguous link → Complete, not Needs Sync.** A bandcamp album with
  `bandcamp.item_id=None` is normally Needs Sync — *unless* it carries
  `bandcamp.candidate_item_ids` (several editions share one store URL and a
  title tiebreak couldn't pin a single one). That's as resolved as we can get
  without per-item track data, so it scans as **Complete**, not Needs Sync. The
  Library badge's tooltip shows the candidate ids.
- **Surrender = Needs MBID with a read-only suggestion.** When a full sync finds
  no matching purchase, the album is demoted to Needs MBID with its *own* current
  release as the `mb_match_candidate`, flagged `unmatched_purchase=true`. The card
  renders this read-only (no Confirm — re-confirming would loop straight back to
  Needs Sync) with a "no purchase found" note and the seed/fix tools.

`Complete` vs `Incomplete` is derived at scan time by comparing the
album's file count against `sidecar.track_count_expected` (the MB
release's track count recorded at tagging time — see §4). Equal →
Complete; fewer → Incomplete. There is no `incomplete` flag in the
sidecar — state is sufficient.

**Transitions are idempotent.** Running sync, recheck, or tag twice on the same album is safe and produces the same result.

### 3.1 State transition diagram

```mermaid
stateDiagram-v2
    direction TB
    [*] --> NEW: scanner finds<br/>album dir (no sidecar)
    [*] --> INCONSISTENT: files disagree<br/>on album/MBID
    [*] --> COMPLETE: scanner finds<br/>sidecar+tagged files<br/>(file_count == expected)
    [*] --> INCOMPLETE: scanner finds<br/>sidecar+tagged files<br/>(file_count < expected)

    NEW --> NEEDS_SYNC: reconcile<br/>(MBID + bandcamp ©cmt)
    NEW --> COMPLETE: reconcile<br/>(MBID, non-bandcamp)
    NEW --> NEEDS_MBID: recover store URL<br/>from ©cmt
    NEW --> NEEDS_MBID: manual MBID<br/>(approximate → suggestion)
    NEW --> COMPLETE: manual MBID<br/>(exact)

    NEEDS_MBID --> NEEDS_MBID: recheck / paste MBID<br/>(approximate → suggestion)
    NEEDS_MBID --> NEEDS_MBID: dismiss suggestion / recheck (no match)
    NEEDS_MBID --> COMPLETE: Confirm suggestion<br/>or exact recheck / paste MBID
    NEEDS_MBID --> INCOMPLETE: Confirm as Incomplete

    NEEDS_SYNC --> COMPLETE: Sync matches<br/>purchase (item_id filled)
    NEEDS_SYNC --> COMPLETE: Ambiguous link<br/>(candidate_item_ids set)
    NEEDS_SYNC --> COMPLETE: Mark purchased<br/>elsewhere
    NEEDS_SYNC --> NEEDS_MBID: Surrender<br/>(full sync, no purchase)
    NEEDS_SYNC --> NEEDS_SYNC: Update URL<br/>(retry on next sync)

    COMPLETE --> NEW: Forget<br/>(sidecar deleted)

    INCOMPLETE --> NEW: Forget
    INCOMPLETE --> NEEDS_MBID: Recheck<br/>(MB tracklist changed → suggestion)
    INCOMPLETE --> COMPLETE: Recheck<br/>(missing tracks now on disk)

    INCONSISTENT --> NEW: user fixes on-disk<br/>tags via Picard
```

Notes:

- `COMPLETE` and `INCOMPLETE` are both terminals, distinguished by
  whether the on-disk file count matches `sidecar.track_count_expected`
  (the MB track count recorded at tagging time — see §4). No `incomplete`
  flag in the sidecar; state is sufficient.
- `TAGGING` is omitted: it's a transient state visible only while the
  tagger is mid-write (typically <1s).
- `INCONSISTENT` is purely derived from on-disk file tags; user resolves
  via Picard (§15.2). No sidecar action needed.
- Forget adds the path to an in-memory exemption set so auto-reconcile
  doesn't immediately reverse it.

### Match confidence (when MB has the URL but the files might not match)

A URL → MBID match from [MusicBrainz](https:://musicbrainz.org) is exact, but the local files on disk might not be the same release variant the user has on Bandcamp (different mastering, bonus tracks, single-disc edit, etc.). Before auto-tagging, the orchestrator runs a confidence check (`harmonist.match.assess_match`):

- **Exact:** file count matches MB track count AND every per-track duration is within ±4 seconds of MB's recorded length. Auto-promote: write `mb_release_id`, run tagger, transition to Tagging → Complete with no user intervention.
- **Approximate:** file count matches but at least one track length differs significantly. Stash the candidate MBID + per-track diff in `mb_match_candidate`; do NOT tag. The album stays in Needs MBID with the suggestion attached; its card surfaces a Picard-style side-by-side with green/amber per-track indicators and Confirm / Confirm as Incomplete / Dismiss suggestion buttons (find/assign tools remain available under a disclosure).
- **No match:** file count differs from MB track count. Treated like Approximate from the user's perspective (suggestion shown, explicit Confirm required) but the side-by-side has to handle uneven rows.

Track lengths compared are the per-release **track** lengths, not the recording lengths (which can differ by seconds across releases).

Confirm → promote candidate to `mb_release_id`, clear candidate, run tagger.
Dismiss suggestion → clear candidate; the album stays in Needs MBID so a different release can be assigned.

Tracks where MB has no recorded length are shown as "unknown" (gray) and don't trigger downgrade on their own, but they don't get to vote for "exact" either — an album with all-unknown lengths and matching count is treated as Approximate.

---

## 4. Sidecar JSON schema

File: `<album_dir>/.harmonist.json`. UTF-8, two-space indent, written atomically (write-tmp-then-rename).

```json
{
  "schema_version": 1,
  "store_url": "https://myartist.bandcamp.com/album/my-album",
  "bandcamp": {
    "item_id": 67890,
    "band_id": 12345
  },
  "downloaded_at": "2026-05-05T12:34:56Z",
  "mb_release_id": "abc-123-...",
  "tagged_at": "2026-05-05T13:00:02Z",
  "track_count_expected": 12,
  "notes": null
}
```

**Field rules:**

- `schema_version` is mandatory; the loader rejects unknown versions for now.
- `store_url` (optional) is the canonical purchase URL from any store
  Harmony accepts (Bandcamp, Beatport, Discogs, etc.). Absence means
  "no store source recorded" (the manual case). Store identity is
  derived from the URL host (see `harmonist.models.store_name`).
- `bandcamp` block (optional) holds Bandcamp-specific identifiers
  (`item_id`, `band_id`, `is_private`) and only appears when at least one is set.
  When `store_url` is on a bandcamp.com host but `bandcamp.item_id` is
  null, the album is in **Needs Sync** until the next sync resolves it —
  *unless* `candidate_item_ids` is set.
  - `candidate_item_ids` (optional, list of ints): the purchase ids this album
    *could* be when several editions share one store URL and a title tiebreak
    couldn't pin a single one (§2.5). Set instead of `item_id`; takes the album
    out of Needs Sync (it scans as Complete). A future re-download can collapse
    the set to one id by comparing tracklists.
- `mb_match_candidate` (optional) is a proposed-but-unconfirmed match (§"Match
  confidence"). Beyond the track comparison it can carry **mis-tag provenance**
  (`mistag_owned_url/label/disambig`, `mistag_tagged_*`, `mistag_release_group_mbid`)
  when the suggestion is a different owned edition in the same release group, and
  `unmatched_purchase=true` when it's a **surrender** suggestion (the album's own
  release, kept read-only after a full sync found no purchase — §3).
- `mb_release_id` is the MBID string when matched; `null` when not yet
  matched (state derives from sidecar shape, not from this field alone).
- `track_count_expected` (optional) is the MB release's track count
  recorded at tagging time. The scanner uses it to distinguish
  **Complete** (`file_count == track_count_expected`) from **Incomplete**
  (`file_count < track_count_expected`). There is no `incomplete` flag;
  state is the marker, and `track_count_expected` is what persists user
  intent across MB upstream changes.
- All timestamps are ISO 8601 UTC with `Z` suffix.

**Persistence philosophy:** The sidecar holds load-bearing state only —
fields driving a user-visible affordance, recovery from restart, or
read by another module's logic. MB rate-limiting and lookup audit
data are deliberately NOT persisted: rate limiting is process-wide
(see `MB_RATE_LIMIT_SECONDS` in `web/reconcile_runner.py`), and audit
history belongs in server logs. Speculative "might be useful later"
fields don't go here.

---

## 5. Tagging contract (Picard-compatible)

The tagger writes the full set of MBID atoms on MP4/M4A files plus a refresh of standard text tags from the MB release payload. This is what makes Plex and Navidrome treat the album as MB-tagged.

The format-agnostic `TagSet` (in `formats/types.py`) is the single source of truth for what gets written; each per-format backend (`formats/m4a.py`, `formats/mp3.py`, `formats/_vorbis.py`) serialises it to that format's native tag layer. To add a tag, add a `TagSet` field, populate it in `tagger._build_tagset`, and map it in each backend.

### MP4 atom names (Picard convention — note: spaces, not underscores)

Per-album (same on every track):

- `----:com.apple.iTunes:MusicBrainz Album Id` — release MBID
- `----:com.apple.iTunes:MusicBrainz Album Artist Id` — release-artist MBID(s)
- `----:com.apple.iTunes:MusicBrainz Release Group Id`
- `----:com.apple.iTunes:MusicBrainz Album Type`
- `----:com.apple.iTunes:MusicBrainz Album Status`
- `----:com.apple.iTunes:MusicBrainz Album Release Country`

Per-track:

- `----:com.apple.iTunes:MusicBrainz Track Id` — recording MBID
- `----:com.apple.iTunes:MusicBrainz Release Track Id` — release-track MBID
- `----:com.apple.iTunes:MusicBrainz Artist Id` — track-artist MBID(s)

Standard text tags refreshed from MB:

- `©nam` (title), `©alb` (album), `©ART` (artist), `aART` (album artist)
- `©day` (date), `©gen` (genre — first MB tag), `cprt` (copyright if present)
- `trkn` (track / total), `disk` (disc / total)
- `----:com.apple.iTunes:LABEL`, `----:com.apple.iTunes:CATALOGNUMBER`, `----:com.apple.iTunes:BARCODE`, `----:com.apple.iTunes:MEDIA`, `----:com.apple.iTunes:ASIN` when present

Sort names, multi-value artists, original date, and script (Picard parity — these drive correct alphabetisation and "original year" columns in Plex/Navidrome). The per-format mapping:

| TagSet field        | Source                                           | MP4                                    | ID3v2.4         | Vorbis            |
| ------------------- | ------------------------------------------------ | -------------------------------------- | --------------- | ----------------- |
| `album_artist_sort` | release artist-credit `sort-name`s               | `soaa`                                 | `TSO2`          | `ALBUMARTISTSORT` |
| `artist_sort`       | track artist-credit `sort-name`s                 | `soar`                                 | `TSOP`          | `ARTISTSORT`      |
| `artists`           | per-artist names, no join phrases                | `----:com.apple.iTunes:ARTISTS`        | `TXXX:ARTISTS`  | `ARTISTS`         |
| `original_date`     | release-group `first-release-date`               | `----:com.apple.iTunes:originaldate`   | `TDOR`          | `ORIGINALDATE`    |
| `original_date[:4]` | year derived from the above                      | `----:com.apple.iTunes:originalyear`   | — (in `TDOR`)   | `ORIGINALYEAR`    |
| `script`            | release `text-representation.script` (e.g. Latn) | `----:com.apple.iTunes:SCRIPT`         | `TXXX:SCRIPT`   | `SCRIPT`          |

Sort phrases keep the artist-credit join phrases (`A feat. B` → `A feat. B, The`); each `artists` value is a bare name. Every field is written only when present, so a release missing (say) a release-group date or sort-names just omits those tags. ID3v2.4 has no separate "original year" frame — `TDOR` carries the full original date and consumers derive the year.

The existing `©cmt` (Bandcamp comment) is **preserved** if present — it's the fallback URL recovery path and other tools may rely on it. We never strip user data.

The current code's `MUSICBRAINZ_RELEASEID` atom is **non-Picard** and gets removed by the tagger when it writes the correct atoms.

### Cover art (mandatory)

Plex with the MusicBrainz agent can fetch its own artwork from external sources, but **Navidrome does not** — it reads from embedded tags and `cover.jpg` only. Navidrome is the strict consumer; we design for it.

**The tagger always:**

1. Fetches the front cover from the [Cover Art Archive](https://coverartarchive.org) using the MB release ID:
   - `GET https://coverartarchive.org/release/{mbid}/front` (follows redirects to the actual image)
   - If unavailable, falls back to `release-group/{mbgid}/front` (release-group-level art).
   - If CAA has nothing (common for a fresh / private Bandcamp release not yet in CAA), falls back to art **already embedded** in one of the album's audio files — Bandcamp downloads ship with cover art baked in, so this guarantees a folder `cover.*` even off-CAA.
   - If still nothing (no CAA match, no embedded art), the album is tagged but with no cover; logged, surfaced in the inbox.
2. Embeds the image in every track's `covr` atom (`mutagen.mp4.MP4Cover` with `FORMAT_JPEG` or `FORMAT_PNG`).
3. Writes the same image to `<album_dir>/cover.jpg` (or `.png`, matching format) for tools that prefer the sidecar (Navidrome, MPD, foobar2000, etc.).

**Resolution policy:** `original` (full CAA resolution). Lossless audio is the dominant cost in this library; an extra 10 MB of cover art per album is negligible by comparison. Configurable via `cover_art_size` in `harmonist.toml` (`250 | 500 | 1200 | original`) so a constrained deployment can downsize, but this is not the primary use case. Library-wide cover-art optimisation (clipping / recompressing) is a separate, future enhancement — not in scope here.

**Caching:** the downloaded image goes to `<album_dir>/cover.<ext>` first, and the embed step reads it from there. This means re-tagging an album doesn't refetch CAA, and the user can manually replace `cover.jpg` to override the embedded art on next retag.

---

## 6. Module map

```
src/harmonist/
  config.py            NEW   env-var + TOML config loader, single source of truth
  models.py            NEW   Album dataclass, AlbumState enum, Sidecar dataclass
  sidecar.py           NEW   read/write/migrate .harmonist.json atomically
  scanner.py           REPLACE  walk music dir → list of Albums (sidecar-driven, not tag-driven)
  bandcamp_hook.py     NEW   Syncer subclass; intercepts post-download, writes sidecar
  mb_lookup.py         NEW   MB URL-relationship lookup; full-release fetch for tagging
  mb_search.py         NEW   name-based search helper (manual path only)
  match.py             NEW   compare local files to MB release: confidence + per-track deltas
  reconcile.py         NEW   per-album reconciliation: derive sidecar from MBID tag + ©cmt + MB url-rels
  cover_art.py         NEW   Cover Art Archive fetch, resize, cache, write to album dir
  tagger.py            NEW   Picard-compatible tag writer (incl. embedded covr atom)
  url_recovery.py      NEW   fallback: reconstruct Bandcamp album URL from ©cmt artist URL
  web/
    main.py            REWRITE  FastAPI routes
    sync_runner.py     NEW   in-process job runner with status polling
  fixtures/            NEW   small ALAC fixtures + sample sidecars for tests

DELETE:
  src/harmonist/syncer.py                       (replaced by bandcamp_hook.py)
  src/harmonist/bandcamp_scraper.py             (Harmony does seeding)
  src/harmonist/mb_searcher.py                  (replaced by mb_lookup.py + mb_search.py)
  src/harmonist/setup_demo.py                   (replaced by fixtures/)
  src/harmonist/web/templates/index.html        (stub; real templates are at root)
  templates/partials/verify_seeding.html        (no in-app seeding)
  readme.md~ , .envrc~                          (cruft)
```

---

## 7. Configuration

### Env vars (highest precedence)

| Variable | Default (Docker) | Default (local) |
|---|---|---|
| `HARMONIST_CONFIG_DIR` | `/config` | `~/.config/harmonist` |
| `HARMONIST_MUSIC_DIR` | `/music` | `./music` |
| `HARMONIST_DOWNLOAD_FORMAT` | `flac` | `flac` |
| `HARMONIST_HOST` | `0.0.0.0` | `127.0.0.1` |
| `HARMONIST_PORT` | `8000` | `8000` |
| `HARMONIST_MAX_DOWNLOADS_PER_SYNC` | `5` | `5` |
| `HARMONIST_TEST_MODE` | unset | unset |
| `HARMONIST_LOG_LEVEL` | `info` | `info` |
| `PUID` / `PGID` | unset (root) | n/a |

### Config file (`${CONFIG_DIR}/harmonist.toml`, optional, env vars win)

```toml
[paths]
music_dir = "/music"

[bandcamp]
download_format = "flac"
max_downloads_per_sync = 5
ignores_file = "/config/ignores.txt"
cookies_file = "/config/cookies.txt"

[musicbrainz]
user_agent = "Harmonist/0.1 ( harmonist@girtby.net )"

[server]
host = "0.0.0.0"
port = 8000

[test]
mode = "fixture"   # fixture | cassette | live
unignore_item_ids = []
```

Validation runs at startup via Pydantic; the app refuses to start with an invalid config.

---

## 8. HTTP API surface

All routes return HTML fragments unless noted. JSON only for `/healthz`, `/sync/status`.

| Method | Route | Purpose | Response |
|---|---|---|---|
| GET  | `/` | Inbox page | full HTML |
| GET  | `/tasks` | Inbox content (tasks partial) | HTML fragment |
| POST | `/sync` | Start sync | HTML fragment + `HX-Trigger: sync-started` |
| GET  | `/sync/status` | Poll sync state | JSON `{state, progress, current_item}` |
| POST | `/tag/{album_id}/{mbid}` | Apply tagging with given MBID | HTML fragment |
| POST | `/recheck/{album_id}` | Re-run MB lookup for held album | HTML fragment |
| POST | `/recheck` | Recheck all held | HTML fragment |
| POST | `/manual/{album_id}` | Open manual MBID entry/search | HTML fragment |
| POST | `/manual/{album_id}/search` | Run name-based MB search helper | HTML fragment |
| POST | `/recover/{album_id}` | Try to recover store URL for a New album | HTML fragment |
| GET  | `/healthz` | Health for Docker | JSON |
| GET  | `/static/...` | Static assets (cover art via symlink to music dir is one option) | binary |

Album IDs remain MD5-of-path (matches existing convention; survives across runs as long as the album doesn't move).

---

## 9. UX flows

### 9.1 Live sync

- User clicks **Sync**. Button POSTs to `/sync`.
- Server returns a "Sync running…" fragment with `hx-trigger="every 1500ms"` polling `/sync/status` and a sibling polling `/tasks`.
- Each /tasks fetch re-renders the inbox; albums appear as bandcampsync writes them and the per-album MB lookup runs.
- When `/sync/status` returns `state == "idle"` post-run, the polling stops and the button re-enables.
- Cap: `max_downloads_per_sync` aborts the sync with a visible error if the would-download set exceeds it.

### 9.2 Needs MBID → Recheck

- Card has an "Open in Harmony" link (`https://harmony.pulsewidth.org.uk/release?url=<store_url>`) and a "Recheck" button.
- Recheck POSTs to `/recheck/{id}`. On success, the card swaps into the Tagging spinner, then disappears (album moves to Complete).

### 9.3 Manual ingest

- New card includes a manual MBID form alongside "Reconcile from tags" and "Recover store URL".
- Form takes either a full MB release URL/MBID *or* runs the search helper (`/manual/{id}/search?artist=...&title=...`) and presents matches to pick.
- On selection, POST to `/manual/{id}/assign`.

---

## 10. Deployment

### 10.1 Image build & distribution

- Base: `python:3.14-slim` (slim, glibc, multi-arch). The CSS bundle is
  pre-built and committed, so the image needs no Node/Tailwind toolchain — just
  Python + the runtime deps (`pip install -e .`).
- Healthcheck: `python -c "... urlopen('http://127.0.0.1:8000/healthz')"` (slim
  has no `curl`).
- **Published to GHCR by CI** (`.github/workflows/publish.yml`) — never built on
  the NAS. Built for `linux/amd64` (the Synology target) via Buildx:
  - push to `main` → `ghcr.io/randomphrase/harmonist:edge` (rolling dev image)
  - tag `vX.Y.Z` → `:X.Y.Z`, `:X.Y`, `:X`, and `:latest` (stable release)
- The GHCR package is **public**, so the NAS pulls with no login. (One-time:
  after the first publish, set the package visibility to Public in the repo's
  Packages settings — new GHCR packages start private.) The `github-actions`
  Dependabot ecosystem keeps the workflow's actions current.
- Releasing is just `git tag vX.Y.Z && git push --tags`; the NAS then pulls the
  new tag.

### 10.2 Volume layout (the contract)

```
host:/volume1/docker/harmonist/config   →  container:/config
host:/volume1/music                     →  container:/music
```

Sidecars live next to music inside `/music`. Config dir holds `ignores.txt`, `cookies.txt`, optional `harmonist.toml`.

### 10.3 Run recipes

**Synology (compose):**
```yaml
services:
  harmonist:
    image: ghcr.io/randomphrase/harmonist:latest
    restart: unless-stopped
    ports: ["8000:8000"]
    volumes:
      - /volume1/docker/harmonist/config:/config
      - /volume1/music:/music
    environment:
      PUID: 1026
      PGID: 100
```

**macOS local dev:**
```bash
HARMONIST_MUSIC_DIR=$HOME/Music/harmonist-dev \
HARMONIST_CONFIG_DIR=$HOME/.config/harmonist \
uvicorn harmonist.web.main:app --reload
```

**Pi dev (Synology share over SMB):**
```yaml
services:
  harmonist:
    image: ghcr.io/randomphrase/harmonist:latest
    volumes:
      - ./config:/config
      - /mnt/synology-music:/music   # mounted via /etc/fstab
    ports: ["8000:8000"]
```

---

## 11. Testing strategy

QA is a first-class agent role. The flagship test is the live sync flow end-to-end.

### 11.1 Test pyramid

```
              ┌────────────────────────┐
              │  Live (opt-in, manual) │   real Bandcamp + real MB
              │   1 album, 1 path      │
              └────────────────────────┘
            ┌────────────────────────────┐
            │  E2E (cassette + fixtures) │   sync flow with recorded HTTP
            │       ~5 scenarios          │
            └────────────────────────────┘
          ┌──────────────────────────────────┐
          │  Integration (TestClient)         │   FastAPI routes × demo paths
          │           ~20 tests                │
          └──────────────────────────────────┘
        ┌────────────────────────────────────────┐
        │  Unit                                   │   per module
        │           ~60 tests                     │
        └────────────────────────────────────────┘
```

### 11.2 Test modes (selected via `HARMONIST_TEST_MODE`)

- **`fixture`** — purely local. No network. Fixtures in `src/harmonist/fixtures/`. Default for `pytest`.
- **`cassette`** — replays recorded HTTP via `pytest-recording` (VCR) for MB and Bandcamp. Default for CI.
- **`live`** — hits real services. Opt-in. Uses `unignore_item_ids` from config to pick test targets. **Always uses a temp copy of the ignores file**, never the user's real one.

### 11.3 Selective live testing (Bandcamp citizenship)

The live mode workflow:
1. Read user's real ignores file (read-only).
2. Copy to a temp file.
3. Remove the entries listed in `[test].unignore_item_ids` from the temp copy.
4. Point bandcampsync at the temp copy and a sandbox music dir.
5. Run sync, assert state, clean up.
6. **Hard cap:** if the would-download count exceeds `HARMONIST_MAX_DOWNLOADS_PER_SYNC`, abort with a clear error before any download starts.

### 11.4 Fixtures

Committed to `src/harmonist/fixtures/`:

- 3 ALAC `.m4a` files (~50 KB each, generated from a sine wave via `ffmpeg`). Royalty-free, deterministic.
- Sample sidecars covering each state: orphan (none), held-bandcamp, held-manual, tagged.
- A captured Bandcamp collection-items API response (anonymised; real `url_hints` shapes).
- Captured MB URL-lookup responses (matched + unmatched).
- Captured MB release-fetch response with full release data.

The integration test must be hermetic — it must pass on a clean checkout without anything from the user's filesystem. The current dependency on `/Users/alastair/Music/Traktor/02 Declino.m4a` is the canonical example of what we won't do again.

### 11.5 Flagship test (must pass before "prototype" is declared done)

```
test_live_sync_flow_end_to_end (cassette mode):
  given: empty music dir, ignores with 359/360 entries, 1 unignored
  when:  POST /sync, poll until /sync/status is idle
  then:  exactly 1 album appears in /tasks
         sidecar exists with store_url populated (bandcamp.com host)
         MB lookup ran (assert via cassette interaction)
         either tagged (Complete, not in /tasks) or Needs MBID with Harmony URL
  cleanup: scrub temp dirs
```

The same scenario runs in `live` mode manually before each release, against a single chosen `item_id` from the user's real collection.

### 11.6 Manual test plan

A checklist in `docs/manual-tests.md` (separate doc, owned by QA):

- Sync flow against real Bandcamp on macOS
- Sync flow against real Bandcamp on Pi (over SMB-mounted Synology share)
- Tag write over SMB doesn't corrupt files; Plex picks up the MBID
- Held → Recheck after seeding in Harmony eventually transitions to Done
- Manual ingest with a non-Bandcamp album

---

## 12. Build order

Each step ends with green tests at its level. No agent moves on until QA signs off.

1. **Foundations** — `config.py`, `models.py`, `sidecar.py`. Unit tests for sidecar I/O (round-trip, atomic writes, schema rejection).
2. **Tagger** — `tagger.py` against ALAC fixture files. Unit tests assert exact atom set written + re-read. This blocks nothing else and is independently valuable.
2a. **Cover art** — `cover_art.py`: CAA fetch, resize to configured size, write `cover.jpg` + embed `covr`. Cassette tests for matched / no-art / release-group fallback. Tagger gets extended to call this.
3. **Scanner rewrite** — sidecar-driven. Unit tests across all states (orphan / held / tagged) with fixture trees.
4. **MB lookup** — `mb_lookup.py` URL-relationship endpoint + full-release fetch. Cassette tests for matched / unmatched / API error.
5. **bandcampsync hook** — `bandcamp_hook.py`. Unit tests against a mocked `BandcampItem` shape; integration test against a fixture collection-items response.
6. **Web layer** — FastAPI routes, `sync_runner.py`, templates. Integration tests via `TestClient` covering each route × each state.
7. **UX live updates** — HTMX polling, status indicator, sync button states. Browser-tested manually.
8. **Manual + recheck flows** — `mb_search.py`, manual-entry templates, recheck routes.
9. **URL recovery fallback** — `url_recovery.py` for New albums with only a `©cmt` artist URL. Lower priority.
10. **Containerise** — Dockerfile, multi-arch buildx, healthcheck, PUID/PGID entrypoint, compose recipes.
11. **Flagship E2E test** — cassette-mode test of the full sync flow. Becomes the gate for "prototype done".
12. **Manual test pass** — QA runs through `docs/manual-tests.md` on macOS and Pi.

---

## 14. Audio format support

Harmonist supports common audio container formats. The scanner walks for
all supported extensions; the tagger dispatches by file extension to the
right per-format implementation.

| Format | Extension | Tag spec | mutagen class | Status |
|---|---|---|---|---|
| ALAC / AAC in MP4 | `.m4a`, `.mp4` | iTunes-style MP4 atoms (Picard spec) | `mutagen.mp4.MP4` | Implemented |
| MP3 | `.mp3` | ID3v2 frames (Picard spec) | `mutagen.mp3.MP3` | Implemented |
| FLAC | `.flac` | Vorbis comments + native picture | `mutagen.flac.FLAC` | Implemented |
| Ogg Vorbis | `.ogg`, `.oga` | Vorbis comments + `METADATA_BLOCK_PICTURE` | `mutagen.oggvorbis.OggVorbis` | Implemented |
| Opus | `.opus` | Vorbis comments (in Ogg) | `mutagen.oggopus.OggOpus` | Implemented |

Out of scope: WAV (no standardised tag scheme), AIFF (rare for libraries),
WMA, format conversion. Harmonist never transcodes — files stay in their
original container.

**Architecture:** the `harmonist.formats` package owns all audio-tag I/O.
`formats/__init__.py` is a dispatcher that selects a per-format submodule
by file extension and exposes a format-agnostic surface:

```
formats.is_supported(path)            -> bool
formats.supported_extensions()        -> (".m4a", ".mp3", ".flac", ...)
formats.read_album_id(path)           -> str | None   # MB Album Id
formats.read_album_title(path)        -> str | None
formats.read_artist(path)             -> str | None
formats.read_track_title(path)        -> str | None
formats.read_comment(path)            -> str | None   # Bandcamp-URL fallback
formats.read_duration_ms(path)        -> int | None
formats.write_tags(path, tagset, cover)
```

The orchestrating `tagger.py` builds a format-agnostic `TagSet` per track
from an MB release and calls `formats.write_tags`. The scanner, reconcile,
url_recovery, and match modules read tags only through this surface —
mutagen stays inside `formats/`. Adding a format = a new submodule
(`EXTENSIONS` + the read/write functions) registered in `_MODULES`.

FLAC, Ogg Vorbis, and Opus share `formats/_vorbis.py` (the `VorbisTagger`)
since they use the same Vorbis-comment scheme; the per-format wrappers
only inject the mutagen class and the cover-embedding strategy.

Each per-format module conforms to Picard's documented mapping for that
format (https://picard.musicbrainz.org/docs/mappings/). The comment field
(`©cmt` / `COMM` / `COMMENT`) is never overwritten on tagging so a
recovered store URL survives a retag.

## 15. Best-effort handling of imperfect libraries

Harmonist heavily biases the **curated user** — Picard-tagged, sane folder
structure, purchased / legitimately-obtained library. For chaotic
libraries (mixed dirs, partial tagging, downloaded-from-Napster mess), the
rule is **best effort, never silent corruption**. When the on-disk state
is ambiguous, surface it to the user with enough info to decide;
otherwise do nothing.

**Core principle:** *the user should never need to find, edit, or delete
a `.harmonist.json` sidecar by hand to escape a state.* If they do, that's
a UX bug. Every state must have a path out via on-disk file edits, Picard,
or a button in the UI.

### 15.1 Partial tagging

Some tracks in an album dir have the MB Album Id atom, others don't.
Common cause: user added a track to an existing album without re-tagging,
or Picard was interrupted partway through.

**Detection:** scanner reads MB Album Id from every file. If N of M files
are tagged with the matching MBID (M > N > 0), the album is *partially
tagged*.

**State:** stays `COMPLETE` (the existing logic treats "any file matches"
as tagged). The scanner's Album object gains a `partial_tag_count` field
(`"N/M"`-style) — not persisted, just derived at scan time.

**UI:** library expanded view shows a "5/6 tracks tagged" badge. In v1
this is informational only; the in-app resolution (a Re-tag button that
re-runs the tagger across all files, backfilling untagged ones
idempotently) ships with the §2.4 Re-tag use case post-v1. In the
meantime the user can re-tag externally with Picard.

No new state — partial tagging is a quality issue, not ambiguity.

### 15.2 Inconsistent dirs (multiple albums in one folder)

Tracks in an album dir disagree on album title (`©alb` / `TALB` / `ALBUM`)
or MB Album Id. Common cause: messy filesystem; user dumped multiple
albums into one folder.

**Detection:** scanner reads album title + MB Album Id from every file in
each album dir. If either varies across files, derive state `INCONSISTENT`.
Compilations (same album title + MBID, varying track artists) are NOT
flagged — that's legitimate.

**State:** new `INCONSISTENT`. **Purely derived from on-disk file tags;
no sidecar field involved.** Auto-reconcile skips these (they're not
New — scanner pre-empts the new classification).

**UI:** inbox card shows a per-track summary table with the conflicting
fields highlighted, and an instruction:

> *Sort these into separate folders with Picard, then refresh. Harmonist
> won't guess at conflicting tags — Picard exists for exactly this case.*

**No "Ignore" action.** Per the core principle, we don't write a sidecar
field that requires hand-editing JSON to escape. Once the user fixes the
on-disk tags via Picard, the next scan re-classifies the dir naturally
(likely New → auto-reconcile resolves it). Chaotic dirs the user
genuinely doesn't care about will sit in the inbox indefinitely — that
is the deliberate cost of the principle.

**Sidecar interaction:** if a sidecar already exists when files become
inconsistent (e.g. user dropped a stray file into a Complete album dir),
INCONSISTENT trumps the sidecar's state. The sidecar isn't deleted —
once the user fixes the on-disk reality, the scanner will read the
consistent state and the sidecar resumes driving the state machine.

**Known limitation:** files internally consistent but **disagreeing
with the sidecar's `mb_release_id`** (user re-tagged via Picard to a
different MBID) currently surfaces as `TAGGING` (the existing "files
not yet tagged with matching MBID" check). This is misleading — the
files ARE tagged, just with a different MBID than the sidecar
remembers. Future work: detect this and either auto-update the sidecar
to match the files ("user's most recent Picard action wins") or
surface a "Sidecar Stale" state.

**Rationale:** tagging an inconsistent dir is high-risk silent
corruption. We refuse to guess; Picard exists for this case.

### 15.3 Incomplete albums

On-disk track count is **less than** the MB release's track count, but
the user has a valid reason (CD rip missing a hidden track, intentional
selection, vinyl-only edition where the digital MB release has bonus
tracks, etc.). Without special handling these stall in `NEEDS_MBID`
forever because `TagMismatchError` would block the tagger.

**Handling:** the suggestion card (Needs MBID with a candidate) gains a
button next to Confirm / Dismiss suggestion:

- **Confirm as Incomplete** — runs the tagger in incomplete mode and
  records `track_count_expected` (the MB release's track count) on the
  sidecar. The album's state becomes `INCOMPLETE`, derived at scan time
  from `file_count < track_count_expected`.

**Tagger incomplete mode:** doesn't raise `TagMismatchError` on
`file_count < track_count`. Uses **length-similarity** to match the
on-disk files to a subset of MB tracks (best-fit assignment, falling
back to positional matching when lengths are unknown / equal). MB tracks
without a matched file are skipped.

**State after Confirm as Incomplete:** `INCOMPLETE`. This is a distinct
terminal state, not a flagged variant of `COMPLETE` — the state enum
alone tells the UI what to render, no sidecar metadata peek required.
The library expanded view shows a small "incomplete" badge plus a
per-track list of which MB tracks weren't on disk.

**Promotion to Complete:** if the user later adds the missing tracks
on disk and clicks Recheck, the scanner sees `file_count ==
track_count_expected` and the state naturally promotes to `COMPLETE`.
Conversely, if MB upstream gains new tracks, Recheck refreshes
`track_count_expected` and the album either stays `INCOMPLETE` (if
the new MB count still exceeds file count) or routes back through
`NEEDS_MBID` (with a fresh suggestion) for re-confirmation.

**Out of scope:** file_count > track_count (extra tracks on disk) — same
class as inconsistent; user resolves externally.

### 15.4 Explicitly out of scope

- **Folder splitting** (separating two albums in one dir into two dirs):
  filesystem-level operation; user does this with Finder/CLI/Picard.
- **Tag editing of individual files** outside MB lookups: Picard's job.
- **Recursive directory disagreement** (nested album dirs): scanner
  treats every dir with audio files as a single album. Atypical layouts
  must be flattened first.
- **Format conversion**: Harmonist never transcodes.

## 16. Store support

Sidecars carry a single `store_url` field — any storefront URL Harmony
recognises. The first-class store is Bandcamp (full sync + match flow);
others are accepted as URL inputs into the manual / reconcile paths and
handed to Harmony for MB seeding.

### 16.1 Bandcamp (first-class)

- **Purchase listing + download**: `bandcampsync` (subclassed as
  `HarmonistSyncer`).
- **URL → MB MBID**: MusicBrainz URL-relationship lookup
  (`mb_lookup.lookup_by_bandcamp_url`).
- **Tag-time evidence**: `©cmt` comment on downloaded `.m4a` files
  contains the Bandcamp album URL (used by `url_recovery` to seed a
  store_url on New albums).

### 16.2 Other stores (URL-only)

For any other store (Beatport, Discogs, Deezer, etc.) the sidecar can
hold a `store_url`. The reconcile/recheck flow then asks Harmony to seed
the MB release from that URL, and tagging proceeds via the existing
MB-by-MBID path. This adds no store-specific code.

### 16.3 Beatport — why no first-class support

Beatport has a v4 OAuth API (`api.beatport.com/v4/`) but it is gated.
New API keys are not issued through normal channels. Community plugins
(e.g. `beets-beatport4`) work around this by scraping the `client_id`
out of Beatport's Swagger UI and using `authorization_code` with the
user's Beatport credentials. This is technically functional but:

- ToS gray area — API is "non-commercial only" and the gating bypass
  arguably contravenes the spirit of their access model.
- Fragile — a `client_id` rotation or Swagger page change breaks it.
- No equivalent of `bandcampsync` for downloading purchases via API.
  Beatport's "My Beatport" downloads are a web-session flow, not API.

Decision: accept Beatport **URLs** in `store_url` (free, via §16.2), but
do not build a Beatport-specific scraper, syncer, or metadata enricher.
Users with Beatport purchases manage downloads out-of-band and paste
the release URL when reconciling.

## 17. Open questions

- **Cover art serving:** the inbox UI references covers via `/static/music/...`. Simplest path is a FastAPI mount of the music dir, scoped to image files only. Decision pending.
- **Cover art library optimisation:** future enhancement, not in scope here. If the library grows big enough to matter, a separate batch tool can downsize covers across all albums. Keep that out of the tagger's hot path.
- **Multiple cover art types:** CAA has front, back, booklet, etc. Prototype embeds front only and stops there. Other types deferred.
- **Re-tag cover behaviour:** if user has manually replaced `cover.jpg`, do we re-fetch from CAA on retag (overwriting their choice) or trust the local file? Current design trusts local; flag in the manual-test plan.
- **MB rate limiting:** musicbrainzngs imposes 1 req/sec by default. For batch tagging across many tracks during a single match, we may need to sequence carefully. Probably fine for the prototype's scale.
- **Single-writer assumption on the ignores file** — if the user runs bandcampsync standalone outside the container, are concurrent writes possible? In practice almost certainly no, but worth flagging.
- **Backup before tag write?** Optionally write `<file>.bak` before mutagen.save() during the prototype phase, removable by config later. QA's call.

## 18. Future enhancements

Decided-but-deferred features. Not in the initial release; captured so
the state model and UI don't preclude them.

- **Re-tag from MB** — see §2.4.
- **Re-download from Bandcamp** — a per-album action for a fully-synced
  (`COMPLETE`, bandcamp-sourced, `item_id` known) album that forces
  bandcampsync to fetch it again. Use cases: the user changed their
  default download format and wants existing albums re-fetched in the
  new format, or deleted the local files expecting Bandcamp to
  re-supply them. Tricky because it must deliberately override the two
  dedup mechanisms that normally prevent re-downloads — the sidecar
  `store_url` short-circuit in `bandcamp_hook.sync_item` *and* the
  item's entry in `ignores.txt` — for that one album only, while still
  respecting the per-sync download cap. Surfaces in the library
  expanded view alongside Re-tag / Forget. Deferred for that
  complexity.
- **Recent-messages popup** — a panel showing the last ~20 status
  messages (sync/reconcile transitions, action results); the status
  bar only shows the most recent. *(Shipped as the Activity tab.)*
- **Ignored-but-not-present items** — a sync skips purchases listed in
  `ignores.txt` (already downloaded). If an ignored item is no longer in
  the library (deleted, or ignored without ever being kept), it's
  invisible. Detect ignored purchases whose `bandcamp.item_id` has no
  matching on-disk sidecar and surface a count to the Activity log, e.g.
  "N purchased items are ignored but not in your library". Stretch: let
  the user un-ignore them to re-download — the same mechanism as
  **Re-download from Bandcamp** above (drop the entry from `ignores.txt`),
  so the two should share UI.
- **Live count updating during the sync phase** *(nice to have)* — the
  reconcile pass already publishes live inbox/library/New/Needs Sync
  counts as it files each orphan (base captured at start + running
  outcome tallies, no mid-pass rescan — see `reconcile_runner.py`'s
  `ReconcileStatus` and `reconcile_pending_orphans`, and the live-count
  panel in `tasks.html`). The **sync** phase does not: `_detect_mistags`,
  `_report_unmatched`, and the closing `request_scan` all run at the *end*
  of the sync `runner_fn`, so the inbox/library numbers only refresh once
  sync completes (snap-at-end). Extend the same base + tallies approach to
  sync — as each purchase links its `item_id` and an album moves
  NEEDS_SYNC → COMPLETE, decrement Needs Sync / increment Library live —
  without a full mid-sync rescan (which would hammer the network mount).
  Low priority: the end-of-sync snap is functionally correct; this is
  purely a responsiveness polish.
