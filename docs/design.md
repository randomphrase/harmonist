# Harmonist — Design

**Status:** draft for review
**Audience:** the implementation team
**Scope:** the prototype that runs locally and deploys to Synology

---

## 1. Purpose

Harmonist streamlines the workflow from *album purchased on Bandcamp* to *fully tagged file in Plex / Navidrome*, using MusicBrainz (MB) as the source of truth. It is a **workflow tool**, not a tagger. Picard-style tagging is a step we automate inside that workflow.

### Guiding principle

**Transparency + user control over perfection.** Harmonist does not chase bulletproof de-duplication or perfect automation. Every download, link, move, and tag is **visible and reversible** — so when best-effort matching slips (it will, especially adopting a decades-old mixed-provenance library), the user sees it happened and fixes it in a click. Imperfect automation degrades to *"more clicks,"* never to a silent duplicate or silent data loss. Best-effort matching sits on top of a transparent, auditable, user-controllable base — not the other way around.

Concretely, this shows up as:

- A dedicated **audit log** (`harmonist.audit`) for every potentially-destructive op — downloads (id + target path + format), file moves/overwrites, sidecar rewrites (old → new), demotes/surrenders, checkpoint clears, case-collisions.
- **No automatic directory reshuffling.** Harmonist only *logs* a case-collision (e.g. `variant/` next to `Variant/`); it never renames or moves directories. The user tidies folders by hand.
- **Unmatched purchases are never auto-downloaded during adoption** — each is surfaced as a *potential download* for an explicit Download / Match / Don't-download decision, so a matching gap costs a click, not a duplicate.

Besides data loss, **usability is a top-tier concern**, not an afterthought.

### MusicBrainz is canonical

"Source of truth" above is meant literally, and it is the rule that settles a
whole class of design questions before they are argued: **Harmonist applies what
MusicBrainz says, and keeps no local exception to it.** If a release's data is
wrong, the fix is to edit it on MusicBrainz, where it benefits everyone and comes
back to the files on the next check for free. This is not a general-purpose
tagger; Picard is that, and Harmonist deliberately does not compete with it.

Two things follow, and both are load-bearing:

- **There is no per-album override.** No "don't apply MusicBrainz's album title
  to this album", no stored rejection of a particular change. Beyond being
  against the principle, such a thing cannot be built honestly: what would be
  overridden is a *diff*, and a diff is not a stable object — it is computed
  between two moving sides, so it merges and splits as MusicBrainz is edited and
  as the files change. There is no key that survives. #271 has the long version.
- **A user can still decline to be nagged.** Ignoring an update (#271) is a
  *bookmark*, not a rejection: it stops an album being listed as work until
  MusicBrainz next changes the release, which is exactly what happens when the
  edit the user went off to make lands. So the escape valve is temporal and
  self-clearing rather than a permanent exception.

Where Harmonist *does* accept a value MusicBrainz did not state, it is because a
**rule** says the two spellings mean the same thing — Picard's release
disambiguation in the album title (§5, #283), a release country Picard chose from
the release's own events (#329). Those are computed fresh every time from what
the release itself says, never remembered per album, and each one is a deliberate
addition to this section rather than a user preference.

### Non-goals

The following are explicitly out of scope for this prototype:

- **No in-app MusicBrainz seeding form.** When an album isn't on MB, we link out to [Harmony](https://harmony.pulsewidth.org.uk). Harmony does the seeding work.
- **No database.** State lives in `.harmonist.json` sidecars next to each album. bandcampsync's `ignores.txt` is the source of truth for "what's downloaded".
- **No multi-user / no auth.** Single-user app behind the user's network.
- **No format conversion.** Files are downloaded in the requested format (default FLAC) and tagged in place.
- **No fix-it-yourself for inconsistent dirs.** Picard exists for that. See §13.2.
- **No transcoding, no folder splitting.** See §13.4.
- **No genre writing, for now** (#12 — deferred, not ruled out). Every other tag Harmonist writes is something MusicBrainz *knows*: this release's barcode, this recording's ISRC, this track's position. A genre is a folksonomy — user-applied, aggregated, contested, inconsistent between neighbouring releases by the same artist — so there is no single authoritative value to copy, and picking one is not the exact, scoped, unique match the guiding principle asks for. Genre is also the tag most likely to carry deliberate user curation, and Plex/Navidrome have their own. Importing genres is a real possibility (see #12 for what would have to be settled first: a curation policy, and a rule that doesn't churn a whole library unattended under #32). Until then Harmonist reads and displays a genre, compares it against nothing, and **does not write or clear one** — it is absent from `formats.owned.Owned` and deliberately excluded from every backend's clear-before-write mapping, so a genre set by another tool survives every re-tag.

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

### 2.3 Recheck a Needs-MBID album

1. User previously seeded a release in Harmony (an album with a `store_url` but no MB match).
2. User clicks **Recheck** on that album.
3. Harmonist re-runs the MB URL lookup. If now matched, it tags; if still unmatched, the album stays in Needs MBID.

### 2.4 Re-tag from MB

1. User edits a release in MB (track titles, dates, etc.) — or just wants to refresh tags.
2. User clicks **Re-tag from MB** on a Library album's page.
3. Harmonist re-fetches the MB release and rewrites the file tags. Per-track embedded artwork is preserved unless the user forces **Replace artwork**.
4. If the release now lists **more** tracks than the album has files, the tagger's
   count guard refuses (§15.3) and the refusal is presented as a decision rather
   than an error: both counts, plus a **Re-tag as incomplete** control that
   re-runs the same re-tag in incomplete mode (#252). Nothing is written until
   that control is pressed; the album then derives `INCOMPLETE` from the totals
   the re-tag wrote (§13.3) and stays in the Library.

   The guard cannot be keyed off the album's derived state alone. `COMPLETE` vs
   `INCOMPLETE` says whether the files were short of what MusicBrainz said **at
   tagging time** (§3, #195); the guard asks what it says **now**. The two
   diverge on precisely the album a MusicBrainz correction has grown, and no
   derived fact can settle that — only the user can, so the endpoint asks.
   `incomplete = file_count < track_count` at the call site is explicitly *not*
   the answer (#133): it accepts any shortfall silently, which is the one thing
   the guard exists to prevent.

### 2.4.1 Re-download from Bandcamp (#132)

A re-tag rewrites tags; sometimes the **files themselves** are the problem — MP3s
the user would rather have as FLAC, or a release the artist has added tracks to
since the purchase (the live@heartlandgathering case, which derives INCOMPLETE
against MusicBrainz with a live store URL sitting right there). The fix is to let
the sync fetch a purchase it already has.

**Why the files have to go.** Three independent mechanisms stop a re-fetch, and
each of them keys off the album being on disk:

1. `sync_items` unions `library_index.item_ids()` into bandcampsync's ignore set,
   so a sidecar'd `item_id` blocks the download even with a lost `ignores.txt`.
2. `sync_item` short-circuits on a `store_url` it finds in that index (exact, then
   slug, then `slug_copies` as a backstop).
3. `LocalMedia.is_locally_downloaded` reads the `bandcamp_item_id.txt` the
   original download left in the directory.

There is no variant of this that keeps the files where they are, so the flow is:

1. **Archive** every directory of the album (§13.5 — one release, one zip) into
   `<music_root>/<Artist> — <Album> (archived YYYY-MM-DD).zip`, stored not
   deflated, colliding names suffixed rather than overwritten.
2. **Verify** — reopen, CRC-check, and compare the manifest and every member's
   size against what went in. Only then is anything deleted. A failure at any
   point leaves the album untouched and removes the partial zip.
3. **Delete** the directories, and any parent they just emptied.
4. **Un-ignore** the purchase, *including* bandcampsync's auto-managed region —
   the one `_remove_user_ignore` refuses to touch, because there it would mean
   duplicating an album that is still on disk. Here there is no copy left.
5. **Approve** the download (`pending_downloads.approve`), the existing flag that
   gets an item past link-only mode and the per-sync cap, and **clear the
   collection checkpoint**, or an incremental sync starts past an old purchase.
6. **Start a sync.**

**Ordering is the safety argument.** The zip is proved good before a byte is
deleted, and the un-ignore happens between syncs rather than during one —
bandcampsync snapshots `ignores.txt` at startup and rewrites it wholesale, so a
concurrent edit is silently discarded. A re-download is therefore refused while a
sync is in flight.

**The archive is the escape hatch** out of the interim state, and the only one
offered: unzipping it at the music root restores the album exactly, sidecar and
all, so it comes back matched rather than as a NEW album to re-reconcile. Nothing
prunes archives; they are the user's.

**No new state, no sidecar field.** Between the archive and the download the
album has no directory, so it is in no state at all — it is held in
`redownloads`, an in-memory store rendered as an inbox card, cleared *by
derivation* when `library_index` sees the purchase again (so any route home
clears it, including the user restoring the zip by hand).

**The release is carried through the round trip.** Re-downloading says the
*files* are wrong, not the match — the user is looking at an album they already
accepted as this release and replacing its audio. So the archived album's
`mb_release_id` is held in `redownloads` and the replacement is tagged as that
same release (`_tag_as_redownloaded`, off the sync's `post_download_callback`),
rather than re-resolving the store URL and re-opening a question nobody asked.
Re-resolution can land on a different release, or on none, turning a finished
album into inbox work.

This is **Confirm's semantics, not a guess** — an explicit user decision to tag
an album as a named release — so the match-confidence assessment is skipped
exactly as Confirm skips it. What is *not* skipped is the tagger's own count
guard: whether a tagging is representable is not something a user's assertion can
make true.

**Whether the copy was INCOMPLETE is carried too**, and this is not a separate
judgement — it is part of the match the user accepted. An album short of its
release, re-downloaded to go and get the rest, may well come back just as short
(the artist hasn't added them after all, or Bandcamp hasn't caught up). That is
the state it was already in, so it is tagged in the tagger's incomplete mode and
stays INCOMPLETE. Refusing would charge the user their tags for a shortfall that
predates the button.

The reverse does **not** get the same treatment: an album that was COMPLETE
coming back short is a bad download, and gets the strict guard. Both directions
are pinned by tests, because the rule is wrong if either half is dropped — and
the first version of this shipped without it, which the demo library caught by
turning its own INCOMPLETE fixture into inbox work.

**Every case it can't settle falls through to `_resolve_by_store_url`**, i.e. to
what a first-time download does, so the fallback is never worse than the ordinary
path:

- The replacement **outgrew** the release (`file_count > track_count`, an error in
  both tagger modes, §13.3) — MusicBrainz may not have caught up with a release
  the artist has added to.
- A previously **COMPLETE** album arrived short (above).
- The carried release has been **deleted from MusicBrainz** since the archive
  (#194), or MB is unreachable.
- The carried match was **lost to a restart** (it is in-memory, like everything
  else here).

All four leave a sidecar with a `store_url` and no release — **NEEDS_MBID**, in
the inbox with the side-by-side and Confirm / Confirm as Incomplete on it. Worth
stating plainly, because it remains a real cost of the operation: an album that
was COMPLETE can come back needing a click. The archive is what makes that
recoverable rather than a loss.

**Two lifetimes, two dicts.** The inbox card clears the moment the files are back
(`prune`, on `library_index.item_ids()`); the carried release must outlive it,
because the download writes its sidecar — and so populates that index — *before*
the tagging runs, and the inbox polls every couple of seconds during a sync.
Holding both in one dict meant a poll landing in that gap silently discarded the
match. The carried one is instead consumed by `take_match` at the tagging, which
is also the only thing that clears it on the happy path.

**Album history spans the round trip.** The archive and delete are recorded under
the album's `mb_release_id`; the fresh download starts under a path-derived
`temp_uid`; tagging it back to that same release records the `temp_uid → MBID`
alias, and `album_history` unions over the alias chain — so the album's page
shows what happened to it, not just what has happened since. Carrying the release
is what makes this the normal outcome rather than a coincidence. Where it can't
be carried, nothing joins the two ids and the archive stays on the old album's
history: findable in the Activity feed, absent from the new album's page. Both
halves are pinned by tests; the alias is what carries it, so removing it fails
them.

**Known limitation.** That store and the download approval are both in-memory. A
restart in the seconds between the archive and its sync loses them, and on a
library with unlinked albums the next sync then runs link-only and surfaces the
purchase as an ordinary *potential download* — one click to fetch, but beside a
"Don't download" that would strand it. Accepted rather than persisted, for the
reasons `pending_downloads` gives for refusing a JSON of its own: the decision
already persists in the two places that matter (the id is out of `ignores.txt`,
the directory is off disk), which is what makes any later sync fetch it.

**Not offered** without a single `bandcamp.item_id` — a manual/CD-rip album has no
purchase, and one carrying only `candidate_item_ids` has several it might be.
Fetching a guess is the no-guessing invariant in reverse.

### 2.5 Per-album reconciliation

Instead of a "bootstrap" event, reconciliation is **continuous and per-album**. Whenever the scanner encounters an album that has MBID-tagged files but no `.harmonist.json` sidecar, the reconciler runs once for that album to derive the right sidecar.

For each such album (`harmonist.reconcile.reconcile_album`):

1. Read the `MusicBrainz Album Id` atom from the album's tracks.
2. Read the `©cmt` (comment) tag from the same file.
3. Fetch the release's URL relationships from MB (`mb_lookup.fetch_release_urls`).
4. **If `©cmt` mentions any `bandcamp.com` URL AND MB has at least one Bandcamp URL relationship for the release:** write a sidecar with `store_url` set to MB's canonical Bandcamp URL, `bandcamp.item_id=None` (filled in later by sync). The album shows as **Needs Link** until the next sync resolves the item_id.
5. **Otherwise:** write a sidecar with no `store_url`. Album shows as **Complete** (already tagged).

The `©cmt` evidence rule prevents false-positive "purchased on Bandcamp" classifications when a user happens to own an album that's *also* available on Bandcamp but they bought it elsewhere (Beatport, CD rip, etc.).

**Untagged Bandcamp downloads (no MBID atom).** A purchase the user downloaded
by hand and copied in has no MusicBrainz tags at all, so steps 1–5 don't apply.
The Bandcamp `store_url` is recovered from two sources, **no guessing** (no
artist-page scraping):

- **At reconcile** (`url_recovery.recover_store_url`): if the `©cmt` carries
  **any** Bandcamp URL — a precise `/album/` (or `/track/`) URL if present,
  else the bare artist-root form — write a sidecar with that `store_url` and no
  MBID, advancing **New → Needs MBID**. An artist-root URL is still recorded:
  it's evidence the album is a Bandcamp purchase, and the sync links it by title
  later. (No Bandcamp URL at all → stays New. No scraping — we never invent an
  `/album/` slug we don't have.)
- **At tag time** (`reconcile.store_url_for_tagging`, called from
  `_tag_with_release`): when the album is being tagged to an MBID and has no
  `store_url` yet, derive one in preference order — embedded `/album/` URL →
  MB's canonical Bandcamp url-rel for that release → the artist-root `©cmt` URL
  as a last-resort placeholder. All three are gated by `©cmt` Bandcamp evidence,
  so a CD rip (or a release MB has no Bandcamp link for) doesn't get a spurious
  `store_url`.

Because tagging records the `store_url`, a manually-assigned download lands in
**Needs Link** (not Complete), and the next sync fills in `item_id`. When the
placeholder is only an artist-root URL (no `/album/` slug), the sync can't match
it by slug, so the backfill links it in its **title-fallback** pass instead (see
below) — tagged `©alb` title ⟷ purchase title, the same exact-match rule used for
edition mismatches.

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

#### The backfill: a two-phase matcher

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
   tagged `©alb` title vs the purchase's item title, lowercased and reduced to
   alphanumerics. (The album is tagged, so its title is authoritative; the
   enclosing **folder name is ignored** — it's arbitrary user naming.) Link only
   a **unique** match; then link a lone 1-album/1-purchase remainder by elimination.
3. Purchases the title couldn't pin to an album → record them as an **ambiguous
   link**: store the candidate item_ids on the album (`bandcamp.candidate_item_ids`)
   with no single `item_id`. The album leaves Needs Link for **Complete** — it's
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
tagged `©alb` title (same normalization), **ignoring the URL**. A unique match links it;
ambiguous or absent → left for surrender. **Slug-less** albums (an artist-root
placeholder `store_url`, e.g. a manual download with no precise URL anywhere — §2.4)
have no slug to match in phase 1, so they're added directly to this title pass.

A phase-2 link of a **slug-bearing** album **always** has a URL mismatch by
construction (that's why it fell out of phase 1), so the tagged release's
store_url differs from the matched purchase's URL. That can mean the tag is the
wrong edition (a mis-tag), OR a correctly-tagged edition whose MB URL is the
shared public page — indistinguishable
without comparing tracklists. So we **link and log a WARNING** ("possible mis-tag",
naming both URLs) into the Activity feed for the user to judge. (A slug-less album
has no precise URL to disagree with the purchase, so no such warning is logged.)
This is the *one place* matching crosses the no-guessing line: we act on strong
unique-title evidence but never claim certainty.

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

#### Forcing a full sync

A normal sync stops at bandcampsync's collection checkpoint
(`.bandcampsync-state.json`) and never re-pages older purchases — so an album
waiting to link (**Needs Link**) whose purchase is *old* would never be seen. So
if any album is in Needs Link at sync start, Harmonist **clears the checkpoint**
for that run, forcing a full re-page (bandcampsync writes a fresh checkpoint at
the end, so subsequent syncs return to incremental). Self-limiting: a full sync
resolves every Needs Link album — it either links it, or surrenders it.

#### Linking via a release's other Bandcamp URLs

Bandcamp linking keys on the album **slug** (`/album/<slug>`), but an MB release
often lists *several* Bandcamp URLs (e.g. `/album/x` and `/album/x-2`, or an
artist page plus a label page), and the purchase frequently uses a different one
than the slug the album was tagged with — so the plain slug match misses and the
album would wrongly surrender. After downloads, before mis-tag detection,
`_link_unmatched_by_release_urls` fetches each unmatched Needs-Sync album's MB
`url-rels` and links it to an unmatched purchase whose slug is **any** of the
release's Bandcamp URLs (only when exactly one matches). Cost is one MB call per
unmatched album — bounded by the small failed set, same budget as mis-tag
detection.

#### Surrender — when nothing matches

After the backfill and the post-sync mis-tag pass, an album still in Needs Link
on a **full** sync has genuinely no matching purchase. Rather than nag forever,
Harmonist **surrenders** it: demote to Needs MBID, keeping its current release as
a **read-only** suggestion (`mb_match_candidate.unmatched_purchase`) plus a "no
purchase found" note, so the user can seed the release on Harmony or correct the
store URL. Surrender fires **only on a full sync** (`collection_checkpoint_token
is None`) — on a partial sync, "no match" might just mean "not paged this run",
so we only warn there and leave the album alone. If a surrendered album is tagged
as the *same* MB release as one already linked to a purchase, a non-committal
WARNING flags a possible duplicate copy — or a release legitimately split across
directories (§13.3), which we don't try to tell apart.

After a **full** sync, an album reaches surrender for exactly one of three
reasons — its `store_url` slug matched no purchase slug *and* its tagged title
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

**Surrender is non-destructive.** `_demote_to_needs_mbid` only rewrites the
**sidecar** — it clears `mb_release_id` but stashes the original release in
`mb_match_candidate` (`unmatched_purchase=true`), and it **never touches the
on-disk file tags**. So the album stays correctly tagged on disk; it just
re-appears in the inbox as Needs MBID with its release pre-loaded as a read-only
suggestion and a one-click Confirm.

**Known limitation (deferred).** Surrender can't tell a *machine-derived* tag
from one the **user manually assigned** — both are cleared from the sidecar and
re-inboxed. So a user-assigned, correctly-tagged manual download whose purchase
can't be found on a full sync re-appears in Needs MBID, costing a re-confirm
click. We accept this for now because it's non-destructive (nothing is erased;
one click restores it). A future refinement would record tag provenance and
skip surrender for user-assigned tags; until then this behavior is pinned by
`test_surrender_leaves_on_disk_file_tags_intact`.

The inbox also surfaces a Needs Link album with two manual affordances:
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

- Library is **internally consistent** per §13.2. Bulk-import does not
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
| absent | — | — | — | — | **New** | yes | "Reconcile from tags" / search-by-name / manual MBID form |
| present | null | null | n/a | — | **Needs MBID** | yes | If `store_url`: "Open in Harmony" + "Recheck"; always: manual MBID form |
| present | null | set | n/a | — | **Needs MBID** (with suggestion) | yes | Adaptive card: side-by-side files vs MB release (per-track green/amber length deltas) + "Confirm" / "Confirm as Incomplete" / "Dismiss suggestion", with the find/assign tools available under a disclosure. Sorted first in the group. |
| present | set | n/a | no | — | **Tagging** (transient) | yes (briefly) | spinner |
| present, `store_url` is bandcamp, `bandcamp.item_id=None` | set | n/a | yes | — | **Needs Link** | yes | "Try a different URL" / "Mark purchased elsewhere" |
| present | set | n/a | yes | equal | **Complete** | no | (hidden — visible in library) |
| present | set | n/a | yes | less | **Incomplete** | no | library badge "N of M tracks"; "Recheck — maybe more tracks now" |

**Needs MBID is a single state** whether or not a `mb_match_candidate`
suggestion is attached — there is no separate "Needs Review" state. The
card adapts: with a suggestion it leads with the side-by-side + Confirm;
without, it leads with the find/assign tools. This avoids a confusing
round-trip (reject → re-assign) when the user just wants to swap a wrong
MBID — they can do that inline, and dismissing a suggestion stays put.

**A suggestion need not carry per-track rows.** `track_comparisons` comes from
an MB fetch, and two paths attach a candidate without one: an unlink after an
undo (#158) suggests the album's own former release, and nothing here makes a
network call. The card renders the Confirm and the notes and omits the
side-by-side, rather than drawing a comparison table with no rows in it.

**Two refinements from the purchase-matcher (§2.5):**

- **Ambiguous link → Complete, not Needs Link.** A bandcamp album with
  `bandcamp.item_id=None` is normally Needs Link — *unless* it carries
  `bandcamp.candidate_item_ids` (several editions share one store URL and a
  title tiebreak couldn't pin a single one). That's as resolved as we can get
  without per-item track data, so it scans as **Complete**, not Needs Link. The
  Library badge's tooltip shows the candidate ids.
- **Surrender = Needs MBID with a read-only suggestion.** When a full sync finds
  no matching purchase, the album is demoted to Needs MBID with its *own* current
  release as the `mb_match_candidate`, flagged `unmatched_purchase=true`. The card
  renders this read-only (no Confirm — re-confirming would loop straight back to
  Needs Link) with a "no purchase found" note and the seed/fix tools.

`Complete` vs `Incomplete` is derived at scan time from **the files' own
tags** — `scanner.expected_tracks`. A tagging writes the release's counts into
every file (`trkn`'s total per medium, `disk`'s total for the release; Picard
writes the same), so the number is already on disk, from MusicBrainz, as of the
moment the album was tagged.

Three shapes:

- **Single medium** — the files agree on a track total; that is the release's.
- **Every medium present** — sum each disc's own total. A 2-disc release of
  11 + 10 is 21 tracks, with no lookup.
- **A medium entirely absent** — `disc_total` says 2 and only disc 1 has files.
  Certainly Incomplete, but the total is *unknowable*: the absent disc's length
  was only ever recorded in the files that are missing. `expected_track_count`
  is None and the UI says "Incomplete" without a denominator rather than
  inventing one.

Files carrying no totals give "don't know", which derives **Complete** — an
album Harmonist has never tagged is not accused of missing tracks.

**An absent medium that was video does not count against the album** (#206).
A release whose bonus DVD the user never ripped is not missing 44 tracks in any
sense they can act on — Harmonist cannot tag video (#66). So a medium that is
*entirely* absent is forgiven when MusicBrainz says its tracks are all video.

The word doing the work is *entirely*. A partly-present video medium is still
Incomplete, on the principle that if the user has one video they should have the
rest — which separates "I chose not to rip the DVD" from "my rip failed halfway".

This is the one release fact the files cannot carry, because the files that would
carry it are the missing ones. So it is persisted as `sidecar.video_media` (§4),
filled by a lookup bounded to albums that actually have an absent medium —
knowable from the tags alone, and a small set. `None` means "not asked" and `()`
means "asked, none are video"; without that distinction a release with no video
would be re-fetched on every pass forever.

The album page **lists the absent media** regardless, off the release it has
already fetched for its comparison. "Complete" must not quietly mean "we stopped
mentioning two whole discs".

**Video files count as present tracks** (#193). Harmonist cannot tag a `.m4v`
— that is #66, and they are deliberately absent from `album_files.audio_files`
so nothing that writes can reach them — but Picard tags them with the same disc
and position atoms as the audio, and the user *has* them. Without this a CD+DVD
release reads as missing every track on the DVD. They are tracks for the purpose
of "do you have this album" and for the album page's tracklist, which lists
them as present and marks them video (#226) — a `.m4v` is an MP4 container, so
Picard states its album, disc, position and length in the same atoms as the
audio beside it. Their fields come back `ONLY_DISK`: nothing about a video is
compared against MusicBrainz, because Harmonist can never write it and the
findings would be ones no re-tag could settle. The track count, the album-level
Tags panel and the tagger all still see audio only, for the same reason.

There is no `incomplete` flag and no stored count. `sidecar.track_count_expected`
held this number from v1.0.0 to v1.9.0 and was retired in #195: it duplicated
what the tags already said, from the same source and the same moment, and the
copy could fall out of step while the tags could not. It also meant an adopted
library — where nothing had ever written the field — could never derive
Incomplete at all until one rate-limited MusicBrainz call per album filled it in
(#187). A retired key in an existing sidecar is ignored, not an error; see the
`sidecar` skill.

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
    NEW --> NEEDS_MBID: reconcile recovers<br/>embedded ©cmt Bandcamp URL
    NEW --> NEEDS_MBID: manual MBID<br/>(approximate → suggestion)
    NEW --> COMPLETE: manual MBID<br/>(exact)

    NEEDS_MBID --> NEEDS_MBID: recheck / paste MBID<br/>(approximate → suggestion)
    NEEDS_MBID --> NEEDS_MBID: dismiss suggestion / recheck (no match)
    NEEDS_MBID --> COMPLETE: Confirm / recheck / paste MBID<br/>(non-bandcamp store_url → tagged)
    NEEDS_MBID --> NEEDS_SYNC: Confirm / recheck / paste MBID<br/>(bandcamp store_url → awaits item_id)
    NEEDS_MBID --> INCOMPLETE: Confirm as Incomplete
    NEEDS_MBID --> COMPLETE: Move to Library<br/>(surrendered, no purchase;<br/>purchase_unavailable)
    NEEDS_MBID --> COMPLETE: Link a potential download<br/>(surrendered; un-surrenders)

    NEEDS_SYNC --> COMPLETE: Sync matches<br/>purchase (item_id filled)
    NEEDS_SYNC --> COMPLETE: Ambiguous link<br/>(candidate_item_ids set)
    NEEDS_SYNC --> COMPLETE: Mark purchased<br/>elsewhere
    NEEDS_SYNC --> NEEDS_MBID: Surrender<br/>(full sync, no purchase)
    NEEDS_SYNC --> NEEDS_SYNC: Update URL<br/>(retry on next sync)

    COMPLETE --> [*]: Re-download<br/>(archived to a zip,<br/>files off disk — §2.4.1)
    INCOMPLETE --> [*]: Re-download<br/>(archived to a zip,<br/>files off disk — §2.4.1)
    COMPLETE --> NEW: Forget<br/>(sidecar deleted)
    COMPLETE --> NEEDS_MBID: Wrong match<br/>(pencil — tags left on disk)
    COMPLETE --> NEEDS_MBID: Undo the linking<br/>tagging (#157/#158)
    COMPLETE --> INCOMPLETE: Re-tag as incomplete<br/>(MB gained tracks — §2.4)

    INCOMPLETE --> NEW: Forget
    INCOMPLETE --> NEEDS_MBID: Recheck<br/>(MB tracklist changed → suggestion)
    INCOMPLETE --> NEEDS_MBID: Wrong match / undo<br/>the linking tagging
    INCOMPLETE --> COMPLETE: Recheck<br/>(missing tracks now on disk)

    INCONSISTENT --> NEW: user fixes on-disk<br/>tags via Picard
```

Notes:

- `COMPLETE` and `INCOMPLETE` are both terminals, distinguished by
  whether the on-disk file count matches what the files' own tags expect
  (the MB track count recorded at tagging time — see §4). No `incomplete`
  flag in the sidecar; state is sufficient.
- `TAGGING` is omitted: it's a transient state visible only while the
  tagger is mid-write (typically <1s).
- **A re-download leaves the state machine entirely** (§2.4.1). Every other
  state is derived from something on disk; an archived album has no directory,
  so it derives nothing. It is held in the in-memory `redownloads` store —
  rendered as an inbox card naming its zip — and re-enters at `[*]` when the
  sync writes the replacement, exactly as a first-time download does.
- `INCONSISTENT` is purely derived from on-disk file tags; user resolves
  via Picard (§13.2). No sidecar action needed.
- Forget adds the path to an in-memory exemption set so auto-reconcile
  doesn't immediately reverse it.
- **Unlinking is one operation, `sidecar.unlink`**, reached two ways: the
  "wrong match" pencil, and undoing the tagging that linked the album (§4.x,
  #158). It clears everything that describes the release — `mb_release_id`,
  `tagged_at` — and keeps the store link, which is not
  a claim about which MusicBrainz release this is. The two differ only in the
  `mb_match_candidate` they pass, and that difference is the point: the pencil
  passes none, because re-offering a release the user just called wrong would
  undo their own judgement, while the undo passes the release it unlinked so
  Confirm is the one-click way back.

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
  null, the album is in **Needs Link** until the next sync resolves it —
  *unless* `candidate_item_ids` is set.
  - `candidate_item_ids` (optional, list of ints): the purchase ids this album
    *could* be when several editions share one store URL and a title tiebreak
    couldn't pin a single one (§2.5). Set instead of `item_id`; takes the album
    out of Needs Link (it scans as Complete). A future re-download can collapse
    the set to one id by comparing tracklists.
- `mb_match_candidate` (optional) is a proposed-but-unconfirmed match (§"Match
  confidence"). Beyond the track comparison it can carry **mis-tag provenance**
  (`mistag_owned_url/label/disambig`, `mistag_tagged_*`, `mistag_release_group_mbid`)
  when the suggestion is a different owned edition in the same release group, and
  `unmatched_purchase=true` when it's a **surrender** suggestion (the album's own
  release, kept read-only after a full sync found no purchase — §3).
- `mb_release_id` is the MBID string when matched; `null` when not yet
  matched (state derives from sidecar shape, not from this field alone).
- `tracks_unavailable` (optional, bool) is set when the user accepts an
  **Incomplete** album as finished: the tracks it lacks are not obtainable, so
  there is nothing to act on. A Blu-ray where only the stereo mixes were ripped;
  a hidden CD track never ripped from a disc since thrown away.

  The control is labelled with what it **does** — *Don't warn me about this* —
  because that is the whole of its effect: the badge is demoted and the album
  leaves the Incomplete filter. It was originally phrased as a claim about the
  source ("there are no more tracks to get"), on the reasoning that a checkable
  claim keeps the field from becoming a general "ignore this album". Two things
  were wrong with that. The claim isn't checkable by the person making it —
  whether a hidden track can still be got *somewhere* is not a fact about their
  own shelf — and it names tracks, under a badge that may be reporting a missing
  disc (#245).

  The cost of the change is recorded here rather than lost: the data no longer
  distinguishes "as complete as this album can be" from "the user muted a
  shortfall that is genuinely fixable" — a Bandcamp album whose artist has since
  added tracks, which *can* be fetched (#132). Nothing reads that distinction
  today; a future gardener pass that offers to fetch newly-available tracks
  (#32) would want it, and would need to ask rather than infer.

  It does **not** change state. The album really is short, `INCOMPLETE` says so
  truthfully, and both the tile and the album page still report the count — in
  neutral rather than amber, since it is no longer work. What changes is the
  Library's **Incomplete filter**, which exists to find defects the user can fix;
  an accepted one is not such a defect, so it drops out. Set and unset from a
  checkbox beside the album page's completeness badge, which is the statement it
  governs (#227).

  Not derivable at any price: a decision with no evidence on disk. Same shape as
  `purchase_unavailable` one level over (#196).
- `purchase_unavailable` (optional, bool) is set when the user accepts a
  **surrendered** album via **Move to Library** — a full sync found no purchase
  and there is none to find (the Bandcamp release was withdrawn, or it was bought
  elsewhere / ripped). It makes the scanner treat the album as terminal
  (Complete/Incomplete) despite a bandcamp `store_url` + missing `item_id`, so no
  future sync re-surrenders it. Absent → `false`.
- `video_media` (optional, list of ints) names the release's video media by
  position (#206), so the scanner can forgive a bonus DVD the user never ripped
  without a MusicBrainz call of its own (§3). `null` means "not asked yet" and
  `[]` means "asked, none are video" — collapsing the two would re-fetch every
  video-free release forever. The **scanner is its only reader**: anything
  holding the release already computes the same answer from it
  (`mb_lookup.video_media_of`), because a restored-from-backup sidecar can be
  older than the discs MusicBrainz has since added (#237).
- All timestamps are ISO 8601 UTC with `Z` suffix.

**Persistence philosophy:** The sidecar holds load-bearing state only —
fields driving a user-visible affordance, recovery from restart, or
read by another module's logic. MB rate-limiting and lookup audit
data are deliberately NOT persisted: rate limiting is process-wide
(see `MB_RATE_LIMIT_SECONDS` in `web/reconcile_runner.py`), and audit
history belongs in server logs. Speculative "might be useful later"
fields don't go here.

### Caching MusicBrainz releases

MusicBrainz rate-limits at **one request per second, per request rather than per byte**. That single fact settles the design. A conditional GET saves bandwidth but still consumes a slot, and MusicBrainz's ETag is not a content validator anyway — three identical requests return three different ETags — so the only thing that saves the budget is **not asking**. `mb_cache` puts a TTL over `mb_lookup`'s by-id fetches, storing each payload in `activity.db` (#127).

**Keyed on `(mbid, inc)`, never on the MBID alone.** Harmonist fetches releases with different `inc=` parameters in different places — the full tracklist for the tagger, `url-rels` alone for reconciliation — and those are different payloads for the same release. Serving one to a caller expecting the other is wrong data that still parses. `mb_lookup` owns the includes as module constants and `mb_cache` keys off the same tuples, so a key cannot drift from the request that filled it.

**Who may be served a cached answer** is one rule: *reads that display or compare may be cached; writes, and anything the user pressed to force a re-check, fetch fresh.* The album page's comparison, the mis-tag sweep and candidate assessment are cached. `_tag_with_release` is not — it writes tags to the user's files, and doing that from an hour-old payload would put metadata on disk Harmonist had already been told was superseded. **Recheck** is not, because its entire meaning is "I have just edited MusicBrainz". A forced fetch still goes *through* `mb_cache` rather than round it, so the stored row is refreshed by the request that was happening anyway.

**A row outlives its TTL on purpose.** `max_age` decides whether a row may be *served*, not how long it is *kept*. An expired row is still the last thing MusicBrainz said, which is what makes it the change-detection baseline the gardener compares a fresh fetch against (#32) — so expiry means "ask again", never "forget", and a fetch that fails does not fall back to it. There is deliberately no eviction: once findings exist (#271), a row is the evidence its finding was raised against.

**`fetched_at` is why this needs no sidecar field.** It answers "how current is what I'm looking at?" on the album page — rendered as the panel's **Checked** date, with a re-read control beside it, which is the escape hatch that keeps a cached comparison from being a dead end. It is also the clock the gardener's incremental scheduling reads to decide which albums are due. That last reader is what dissolves the derived-state tension #32 carried: "when was this last checked" lives here, keyed by MBID, and needs nothing on disk beside the album.

---

## 5. Tagging contract (Picard-compatible)

The tagger writes the full set of MBID atoms on MP4/M4A files plus a refresh of standard text tags from the MB release payload. This is what makes Plex and Navidrome treat the album as MB-tagged.

The format-agnostic `TagSet` (in `formats/types.py`) is the single source of truth for what gets written; each per-format backend (`formats/m4a.py`, `formats/mp3.py`, `formats/_vorbis.py`) serialises it to that format's native tag layer. To add a tag, add a `TagSet` field, populate it in `tagger._build_tagset`, and map it in each backend.

### MP4 atom names (Picard convention — note: spaces, not underscores)

Per-album (same on every track):

- `----:com.apple.iTunes:MusicBrainz Album Id` — release MBID
- `----:com.apple.iTunes:MusicBrainz Album Artist Id` — release-artist MBID(s)
- `----:com.apple.iTunes:MusicBrainz Release Group Id`
- `----:com.apple.iTunes:MusicBrainz Album Type` — **lower-cased** (`album`, not
  `Album`), matching Picard, and **multi-valued**: the primary type followed by
  the release group's secondary types (`album`, `live`), in that order. One tag
  holding both, exactly as Picard builds it — `releasetype = primary +
  secondary` in its `mbjson.py`. The secondary types are what say an album is
  live, a remix or a soundtrack, and Navidrome reads this tag and has no other
  source for it (#331). They cost no extra request: `secondary-type-list` rides
  along with the `release-groups` include §4 already asks for.
- `----:com.apple.iTunes:MusicBrainz Album Status` — **lower-cased** (`official`),
  matching Picard.
- `----:com.apple.iTunes:MusicBrainz Album Release Country`

Per-track:

- `----:com.apple.iTunes:MusicBrainz Track Id` — recording MBID
- `----:com.apple.iTunes:MusicBrainz Release Track Id` — release-track MBID
- `----:com.apple.iTunes:MusicBrainz Artist Id` — track-artist MBID(s)
- `----:com.apple.iTunes:ISRC` — the recording's ISRC(s) (`TSRC` / `ISRC` for ID3 / Vorbis); fetched via the `isrcs` MB include, written only when present

Standard text tags refreshed from MB:

- `©nam` (title), `©alb` (album), `©ART` (artist), `aART` (album artist)
- `©day` (date)
- `trkn` (track / total), `disk` (disc / total)
- `----:com.apple.iTunes:LABEL`, `----:com.apple.iTunes:CATALOGNUMBER`, `----:com.apple.iTunes:BARCODE`, `----:com.apple.iTunes:MEDIA`, `----:com.apple.iTunes:ASIN` when present. **`LABEL` and `CATALOGNUMBER` are multi-valued** (#334): a release names every label and catalogue number it carries, and the two are collected independently of each other — Picard's `label_info_from_node`. Taking both off the first `label-info` entry dropped every label after the first, and dropped the catalogue number entirely whenever the first entry had a label and no number.

**Genre (`©gen` / `TCON` / `GENRE`) and copyright (`cprt`) are NOT written**, and never have been. This list claimed both until the claim was checked against the code: genre is absent from `formats.owned.Owned` and excluded from every backend's clear-before-write mapping (deferred — §1, #12), and `cprt` appears nowhere in `src/` at all. `owned.Owned` is the authoritative set — a tag named here but missing from it is a documentation bug, and the mechanical check that keeps the backends honest (`test_every_backend_maps_every_owned_field`) cannot see this prose.

Sort names, multi-value artists, original date, and script (Picard parity — these drive correct alphabetisation and "original year" columns in Plex/Navidrome). The per-format mapping:

| TagSet field        | Source                                           | MP4                                    | ID3v2.4         | Vorbis            |
| ------------------- | ------------------------------------------------ | -------------------------------------- | --------------- | ----------------- |
| `album_artist_sort` | release artist-credit `sort-name`s               | `soaa`                                 | `TSO2`          | `ALBUMARTISTSORT` |
| `artist_sort`       | track artist-credit `sort-name`s                 | `soar`                                 | `TSOP`          | `ARTISTSORT`      |
| `artists`           | per-artist names, no join phrases                | `----:com.apple.iTunes:ARTISTS`        | `TXXX:ARTISTS`  | `ARTISTS`         |
| `album_artists`     | release credit's per-artist names                | `----:com.apple.iTunes:ALBUMARTISTS`   | `TXXX:ALBUMARTISTS` | `ALBUMARTISTS` |
| `original_date`     | release-group `first-release-date`               | `----:com.apple.iTunes:originaldate`   | `TDOR`          | `ORIGINALDATE`    |
| `original_date[:4]` | year derived from the above                      | `----:com.apple.iTunes:originalyear`   | — (in `TDOR`)   | `ORIGINALYEAR`    |
| `script`            | release `text-representation.script` (e.g. Latn) | `----:com.apple.iTunes:SCRIPT`         | `TXXX:SCRIPT`   | `SCRIPT`          |

Sort phrases keep the artist-credit join phrases (`A feat. B` → `A feat. B, The`); each `artists` value is a bare name. Every field is written only when present, so a release missing (say) a release-group date or sort-names just omits those tags. ID3v2.4 has no separate "original year" frame — `TDOR` carries the full original date and consumers derive the year.

The existing `©cmt` (Bandcamp comment) is **preserved** if present — it's the fallback URL recovery path and other tools may rely on it. We never strip user data.

The current code's `MUSICBRAINZ_RELEASEID` atom is **non-Picard** and gets removed by the tagger when it writes the correct atoms.

### Which file is which track

One ladder, `compare.assign`, answers this for everything — the album page's
tracklist and the tagger both — and it is tried in order: the file's
**MusicBrainz Release Track Id**, then its **disc-and-track number**, then
**file order** (#232).

Only the first rung is an identity. Harmonist writes that id on everything it
tags and Picard writes the same one, so for any album either tool has touched
the question is answered outright, and it survives MusicBrainz renumbering or
reordering the release's media underneath it. The rungs below are guesses kept
for files nobody has tagged yet, and #136 — let the user re-pair a file with
its track by hand — is still the escape hatch out of a wrong one.

**Duration is not one of the rungs.** Matching on length-similarity was how the
tagger picked tracks until #232: two recordings of the same length are ordinary,
the odds get worse the longer the release, and the failure is silent and
targeted — one file in sixteen given another track's title and ids, on an album
that otherwise looks right, which nobody re-checks.

### The release a tagging follows, when MusicBrainz has moved it

MusicBrainz **merges** releases routinely, and it does not 404 a merged MBID — it *redirects*, so `fetch_release(old)` answers with the surviving release under a different `id`. That difference is the whole notification: cheap, exact, and available on a request Harmonist was making anyway.

**A tagging follows the release it actually got.** The sidecar records `release["id"]`, not the id that was asked for, because the tagger writes `release["id"]` into every file. Recording the requested one instead left the two disagreeing, and the album then derived `TAGGING` from its own freshly written files — so the Inbox picked it up and reconcile rewrote the identity from the tags, laundering an identity change through machinery meant for "the user re-tagged in Picard" (#268). It self-healed, which is why it went unnoticed; at the scale of #32's nightly pass it would be continuous churn.

**A merge is named, not just applied.** The album's History gets an audit line carrying both MBIDs and a plain-language activity entry saying what happened, because `sidecar.update` alone — which already renders `mbid=old->new` for any identity change — cannot be told apart from the user re-matching the album by hand. The alias linking the old id to the new one needs no special call: `sidecar.write` records one whenever an album's canonical id changes, and this is one of those.

**A merge always applies, and is never held for review** — including under #32's unattended pass. There is nothing to authorise: the merge has already happened on MusicBrainz, and Harmonist is only noticing it. Offering a choice would imply the old release still exists to stay on, which it does not. This is deliberately *not* the classifier's business (#267): that map decides which **tag changes** may auto-apply, and a merge is an identity fact arriving alongside them, not one of them.

The escape hatch is the one every tagging has — **Undo**, which restores the previous tags from the album's own history (#157) — plus fixing the merge on MusicBrainz, which is where a wrong one is actually wrong. What a merge must never be is *silent*, which is what the record above is for.

A **deletion** is the sibling case and behaves differently: MusicBrainz 404s, `fetch_release` raises `ReleaseGoneError`, and nothing is written at all (§2.4.1, #194).

### The tags Harmonist owns

`formats/owned.py` names them, and the list is the concrete form of the promise not to touch tags Harmonist doesn't understand: **these fields, and only these, are the ones it writes, overwrites, and removes.** Each backend maps `Owned` to its native keys and clears that mapping before writing, so a field absent from the new `TagSet` — a release with no catalogue number, say — is *removed* rather than left stale from a previous tagging. Before #149 only the Vorbis backend did this, so a mis-tag correction left the wrong release's label on MP3 and M4A files indefinitely.

Everything not in the set is left exactly as found: the comment carrying a recovered Bandcamp URL, a genre tagged elsewhere (Harmonist writes none — see #12), and any arbitrary tag another tool put there (ReplayGain, encoder settings, user ratings).

`Owned` is split by **scope** — whether a field's value depends on which track it is:

- **Album** — `mb_album_id`, `album`, `album_artist`, `album_artist_sort`, `album_artists`, `mb_album_artist_ids`, `mb_release_group_id`, `mb_album_type`, `mb_album_status`, `mb_album_country`, `compilation`, `date`, `original_date`, `script`, `label`, `catalog_number`, `barcode`, `asin`, `disc_total`.
- **Track** — `title`, `artist`, `artist_sort`, `artists`, `track_num`, `track_total`, `disc_num`, `disc_subtitle`, `media`, `mb_track_id`, `mb_release_track_id`, `mb_artist_ids`, `isrcs`.

`media`, `disc_subtitle`, `disc_num` and `track_total` are derived from the *medium*, so they are track-scoped even though they look album-level: on a 2-disc release, or a CD+DVD set, they genuinely differ between tracks. The scope drives the tagging audit records (#86), which record an album-level change once per album rather than once per track.

**`compilation` is set by an identity check, not a name match** (#323). It is written exactly when the release artist is MusicBrainz's special **Various Artists** artist, `89ad4ac3-39f7-470e-963a-56509c546377` — the same id Harmonist already writes into `mb_album_artist_ids`, so this is a comparison against a value MusicBrainz gives us rather than a guess at the string "Various Artists", which would flag a real band called that and miss a release credited in another language.

It is **not** the release group's `compilation` *secondary type*, and the distinction is the whole point: that type describes the release group's nature, so a greatest-hits album by one artist carries it — and flagging one of those is precisely what makes a player shatter the album into one album per track artist, the failure the tag exists to prevent. Harmonist writes the primary type only (see §5).

Like every optional field it is written only when set, so "not a compilation" is the tag's *absence*; because it is owned, an album that stops being one on a re-match has the tag removed by the same clear that handles every other field, with no code of its own.

The `Owned` member values are exactly the `TagSet` attribute names, and a test asserts the two sets match. That guards drift in both directions: a new `TagSet` field nobody classified would be written but never cleared, and an `Owned` member with no field behind it would clear a tag Harmonist never writes.

**Artwork is deliberately not in the set.** Embedded cover art is not a tag here — `tagger.tag_album` passes `cover=None` when the tracks carry differing per-track images precisely so `write_tags` leaves them alone, and clearing artwork with the owned set would make that protection a no-op. Per-track artwork is a third category that fits neither scope, which neither MusicBrainz nor Picard really models; it is handled in the tagger. See #131.

### The tags Harmonist does *not* write

`Owned` says what is written; this says what is deliberately left out, which the code cannot. Measured against [Picard's own tag documentation](https://picard-docs.musicbrainz.org/en/latest/variables/tags_basic.html), Harmonist's 32 owned fields land as 33 of the 38 **basic** tags and none of the ~19 **advanced** (relationship-derived) ones.

The table is the standing answer to "why doesn't Harmonist write X" — so it is kept to the *reason*, and never restates the covered set, which would rot the moment `Owned` changed.

| Picard tag(s) | Verdict | Why |
| --- | --- | --- |
| `genre` | Deferred (#12) | Read-only today: a genre tagged elsewhere is displayed and never clobbered. Costlier than it looks: musicbrainzngs has **no `genres` include** for a release — only `tags` and `user-tags` — so doing it as Picard does (which reads all four, `picard/mbjson.py`) needs a client change or a raw ws2 request, not an entry in `RELEASE_INCLUDES`. Plus a policy on genre-vs-folksonomy-tag. |
| `composer`, `composersort`, `lyricist`, `writer`, `arranger`, `conductor`, `performer:*`, `producer`, `engineer`, `mixer`, `djmixer`, `remixer`, `director`, `work`, `musicbrainz_workid`, `musicbrainz_composerid`, `language`, `license`, `website` | Undecided | The entire advanced set is one `RELEASE_INCLUDES` change away, in the same single request — but that tuple **is the `mb_cache` key** (§4), so adding to it invalidates every cached release and re-fetches the library at one rate-limited request per album. `Owned` roughly doubles, landing on the comparison (#106), the audit records (#86) and undo (#157); ID3 needs the `TMCL`/`TIPL` multi-value frames, which no backend writes. Relationships also churn in MusicBrainz far more than release facts, so #32's pass would report updates constantly. Worth splitting: composer/lyricist/work is the classical and soundtrack case; the credits half is a much larger surface for much less benefit. |
| `comment` | Never | Claimed as **user space**. It carries a recovered Bandcamp URL and is excluded from every backend's owned mapping so a re-tag preserves it. Picard puts the release disambiguation here; Harmonist shows that on the album page instead. |
| `originalalbum`, `originalartist`, `musicbrainz_originalalbumid`, `musicbrainz_originalartistid` | Undecided, leaning no | Needs the release group's *release list*, which the release fetch doesn't carry — one extra lookup per release group, against the call budget. `original_date` already carries the part users sort on. |
| `lyrics`, `syncedlyrics` | Out of scope here | MusicBrainz serves no lyrics, only relationships to lyric sites. A second provider is a scope question about what Harmonist is, not a tag mapping. |
| `bpm`, `key` | No source | AcousticBrainz is shut down. Would require local audio analysis, which Harmonist does not otherwise do. |
| `acoustid_id`, `acoustid_fingerprint`, `musicip_fingerprint`, `musicip_puid` | Never | Audio fingerprinting. An album is identified by its Bandcamp URL or its MBID, never by analysing the audio; MusicIP is long dead. |
| `musicbrainz_discid` | Never | Derived from a physical CD's table of contents. Harmonist never sees a disc. |
| `albumsort`, `titlesort` | Never | MusicBrainz has no sort title for a release or a track — only for artists, which *are* written. Picard leaves these empty too. |
| `copyright`, `encodedby`, `encodersettings`, `originalfilename` | Never | Facts about the file or the rip, not about the release. Preserved as found. |
| `podcast`, `podcasturl`, `show`, `showsort`, `itunes_cddb_1`, `gapless` | Never | Podcast, TV and iTunes-store plumbing. |
| `showmovement`, `subtitle` | Rides on the row above | Classical movement handling, only meaningful with the work relationships. |
| `releasedate` | Nothing to do | Picard never fills it from MusicBrainz either — `picard/const/tags.py` marks it `is_from_mb=False`. (It has a slot of its own in every format, `TDRL` / `----:RELEASEDATE` / `RELEASEDATE`, so the earlier reason here — that it shares `date`'s slot — was wrong about Picard even though the verdict was right.) |

One apparent gap that isn't: `originalyear` is written on Vorbis and MP4 but not MP3, because **ID3v2.4 has no frame for it** — `TDOR` carries the full original date and consumers derive the year.

### How this section is kept honest

**Every mechanical check in the repo is self-referential.** `test_every_backend_maps_every_owned_field` and `test_complete_inventory_against_picard_spec` are both built from Harmonist's own `ATOM_*` constants, so they catch an accidental extra tag or a backend that forgot a field — and can never catch a tag *name*, *case* or *arity* that disagrees with Picard. That is not an oversight to fix with a better assertion: the facts they would have to check live in Picard's source, which is not a dependency here.

So this section is verified by **periodic manual audit against the Picard source**, and the audit is the only thing that can find that class of defect. #331 (release type written as a scalar where Picard writes a list), #333 (original-date atoms in the wrong case) and #334 (label and catalogue number truncated to the first of each) were all found that way, and all three were invisible to the suite, to the album page and to the update check simultaneously — a truncating reader compares equal to a truncating writer.

**Last audited:** 2026-09-01, against Picard `3.0.0rc1` (`5ff1656fd`). Worth repeating when Picard cuts a major release, and worth checking the five axes rather than the tag list: name per format, arity, casing, join, and which corner of the payload the value comes from.

**Everything above is still preserved.** "Not written" is not "removed": anything outside `Owned` is left exactly as found, so a library tagged by Picard with composers and performers keeps them through a Harmonist re-tag.

### A second correct spelling of the album title

Picard has an option to append the release's **disambiguation comment** to the album title, so a library tagged with it on carries `Selected Ambient Works, Volume II (expanded edition)` where MusicBrainz's release title is `Selected Ambient Works, Volume II`. That is the same album by the user's own deliberate setting, and reporting it as a difference costs more than a wrong row on the album page: `album` would differ on *every* pass forever, so the write-skip above could never fire on those albums and the classifier's Identity verdict would put the whole library in the Inbox on the gardener's first night (#283).

So both places that judge a disk title against MusicBrainz's accept it: `compare.album_fields` for the album panel, and the tagging diff for the write-skip. The accepted spelling is built by `models.title_with_disambiguation` from the release already in hand, which costs no extra MusicBrainz request — `disambiguation` is a core field on the release entity.

**Exactly one string, never a pattern.** `models.titles_match` would accept this pair too, and would accept `(deluxe edition)` and `(2019 remaster)` just as readily, since it judges on words alone. That latitude is earned where it is used, inside an artist-scoped and uniqueness-guarded purchase match. Here the release *states* its disambiguation, so accepting anything looser would be guessing an identity that was available for free — which review-gate item 2 forbids. Picard applies several other title transforms besides this one; recognising them is #284, and it is a survey of what is exactly checkable rather than a loosening of this rule.

**Only the comparison is tolerant, not the write.** A re-tag that happens for some other reason still puts MusicBrainz's plain title on the file. Harmonist writes what MusicBrainz says; preserving a spelling it did not derive is a different question, and it is the one that needs a setting to match Picard's — deferred until someone wants it. The consequence is worth naming: on an album where nothing else has changed the disambiguated title survives indefinitely, and on one where something else has changed it does not.

### A release comes out in more than one country

MusicBrainz records a release as a **list of release events** — `(area, date)` pairs — and collapses that list to the scalar `country` and `date` the tag can hold. So [*Amok*](https://musicbrainz.org/release/3587efcb-c42a-4da5-839b-2a9f9b8d933e) came out in Germany on 2013‑06‑07, the UK on the 10th and the US on the 11th, and carries `DE` / `2013-06-07`.

The tag stays scalar and Harmonist keeps writing `release["country"]`. Picard's `releasecountry` is a scalar for the same reason — *"if more than one release country was specified, this tag will contain the first one in the list"* (`picard/const/tags.py`) — and its full list lives in the hidden `~releasecountries`, which is never written to a file. Two consequences, one for each surface:

- **The album page names them all** (#329). The Country row's value is the tag, unchanged; beside it sits every country the release names, with the dates, and which one the tags carry. `release-event-list` comes back with any release lookup, so this reads a corner of the payload the page already holds — no extra `includes`, no extra request.
- **A second release country is not a difference** (#346). Picard writes whichever country `preferred_release_countries` matches (`picard/mbjson.py`, `release_to_metadata`), so a library tagged that way carries a code that is not MusicBrainz's first and is not stale either. Both places that judge a disk country accept any country **this release** names — `compare.album_fields` for the panel and the tagging diff for the write-skip — exactly as they accept the disambiguated album title above, and with the same limit: the release's own release events, never "any country". A code MusicBrainz does not list for the release is genuinely stale and still reported.

**Only the comparison is tolerant, not the write** — the same sentence as the album title's, and the same consequence. A re-tag that happens for another reason puts MusicBrainz's `country` back on the file.

Picard also lets the user *pick* a preferred country, which changes `releasecountry` and nothing else — `date` is `node['date']` regardless. Offering that setting is a separate question and is not answered here.

### How significant a change is, and whether it needs review

`Owned` is split a second way, by **significance**: what kind of change this is (#267). Deliberately *not* whether it needs a person — those are two questions, and an earlier draft answered them with one word. A change can be slight and still want an eye on it; a change can be far-reaching and still be one a particular user is happy to have applied for them. Significance is a property of the change. Review is a **policy over** significance.

The levels, ordered by how much of the album they call into question:

- **Cosmetic** — whitespace, casing or typography only; the same value spelled differently. Never declared for a field, only ever reached at runtime (see below).
- **Enrichment** — MusicBrainz filling in or correcting a detail: `album_artist_sort`, `artist_sort`, `mb_album_status`, `mb_album_country`, `date`, `original_date`, `script`, `label`, `catalog_number`, `barcode`, `asin`, `disc_subtitle`, `media`, `isrcs`.
- **Structure** — the same album, laid out differently: `track_num`, `track_total`, `disc_num`, `disc_total`.
- **Identity** — what the album or one of its tracks *is*: `album`, `album_artist`, `album_artists`, `artist`, `artists`, `title`, `mb_album_type`, and every MusicBrainz id.
- **Artwork** — the cover image. Its own level rather than a rank among the others, because "let it update my cover art" is a trust decision people make separately from anything about tags.

`SIGNIFICANCE` lives beside `SCOPE` in `owned.py` and is keyed exactly as a tagging diff is keyed — the `Owned` values plus `ARTWORK` — so a classifier iterating a plan's changes needs no special case for the one key that isn't an owned field. A totality test over that vocabulary is the point of the placement: **a field added to `Owned` later cannot slip through unclassified**, because the test fails until someone places it. The cost of forgetting `SCOPE` is a mis-rendered history row; the cost of forgetting this is a change whose significance nothing can state, in the table a trust setting is read through.

**Every level currently goes to review.** `AUTO_APPLY` is empty and `needs_review` is true for everything. That is deliberate rather than unfinished: nothing has watched this classification run against a real library, and the way to find out whether `mb_album_type` really belongs under Identity is to see it arrive in the Inbox, not to argue it out in advance. Starting closed also means the first cut of the runner cannot write anything unattended, whatever else is wrong with it. `AUTO_APPLY` is the seam #273 turns into a setting: a user who trusts Enrichment gets that level in the set, and everything else keeps going to review.

**No identifier is classified below Identity**, including the ones that can only have moved because MusicBrainz merged the entity behind them. The cheaper reading — the one §5's merge rule takes for the *release* id, that a merge has already happened so there is nothing to authorise — was considered and not taken. That rule rests on the merge being **provable**, and it is, exactly once, at the fetch, where the redirect names both the old id and the new. An id that simply arrives different inside an unchanged release payload carries no such evidence: it could be a merge, a re-point, or an edit that replaced the track outright, and nothing downstream can tell those apart.

**Some fields' significance depends on their values.** `title` should read as cosmetic for a spacing or casing tidy-up and as identity for a real retitle, which no single table entry can say. `models.norm_title` already draws exactly that line for `TrackComparison.title_differs`, so the classifier borrows it rather than inventing a second notion of cosmetic: a title the album page reports as unchanged must not be one the gardener treats as a retitle. Such fields are **declared at their higher significance and lowered** when the change turns out to be one of the small ones, never raised — overstating a change costs a glance, while understating a retitle as a spacing fix is, under a trust setting, a write nobody agreed to.

The mechanism is two tables keyed alike: `owned.BY_VALUE` says how far a field may drop, and `tagger.LOWERED_WHEN` holds the comparison deciding whether this particular change qualifies. Split that way because one of the comparisons needs `models.norm_title` and `formats.owned` is a leaf that does not reach up into the model layer — and so a rule cannot invent a level of its own. A field declared in the first table with no rule in the second raises rather than quietly staying at its declared level.

**A credit list arriving is enrichment; a credit list moving is identity** (#389). `album_artists` and `artists` are the second rule, and a different kind of smallness from the title's — the values are not equivalent, the tag simply was not there. Both are new in Picard as well (`PICARD-700`, `3.0.0rc1`), so no library predating it carries either, and ranking every collaboration in an adopted library as Identity put a tag one week old above a genuine retitle in the same Inbox. Filling one in is MusicBrainz supplying a detail the scalar twin beside it has been carrying all along; names that have *moved* say the album is credited to somebody else, and stay Identity however few of them there are. This is only the rank: the absence is still a reason to re-tag whenever the list would hold more than one name, since the joined phrase cannot be split safely — "Nick Cave & the Bad Seeds" is one artist containing an ampersand (#337).

What `norm_title` forgives is **a spelling of the same title**: NFKC, then every dash folded to one dash and everything Unicode names a quotation mark, apostrophe or prime folded to one apostrophe, then whitespace collapsed and the result casefolded (#379). It is stated as a rule over Unicode's own classification rather than as a table of characters, because a table is a chase with no end and runs out first in the scripts Harmonist handles least — an adopted library arrives with ASCII apostrophes where MusicBrainz has `’`, with full-width brackets in CJK titles, and with decomposed accents from whatever wrote the files. One character of that in one track was enough to put a whole album into the Inbox as a retitle. The fold **canonicalises and never strips**: `Live?` and `Live!` must stay different titles, since this rule may only ever lower a change's significance and one that fires wrongly is a write nobody agreed to.

**Identity is settled upstream of significance.** The diff the classifier reads must be computed against the release the album is *now known to be*, not the one it was last recorded as. A merged release arrives as a changed `mb_album_id`, and §5 settles that a merge always applies and is never held for review — so that correction belongs at the fetch, and by the time a diff reaches the classifier, `mb_album_id` moving means the album is being re-pointed at a genuinely different release.

### Recording what a tagging changed

Every tagging records its per-field before/after, so an album's History can say `artist: Boards Of Canada → Boards of Canada` rather than just "this track was rewritten" (#86). The record is complete rather than filtered: Harmonist only writes fields it owns, so "everything we wrote" is already bounded, and noise control belongs in the rendering rather than in what gets kept.

**The before state is read before the write, not from it.** `write_tags` does return the owned fields as they were, from the mutagen handle it already holds — and that was how the record was captured until the tagger began deciding *whether* to write at all (#266). Deciding needs the before state first, so the tagger reads it with `formats.read_owned` and the write's own return goes unused. The second read that costs is paid only on files something is actually going to be written to; a file that turns out to need nothing is read once and left alone, which is cheaper than the unconditional rewrite it replaced.

**Absent is one state.** `None`, `""` and `[]` all mean "this tag isn't there", because Harmonist never writes an empty tag — a field with no value is removed rather than blanked, and reverting one restores its absence. The stored record still keeps the raw values on both sides, since a revert has to restore what was really there rather than a normalised version of it.

**Nothing is written when nothing changed, and nothing is recorded.** A re-tag computes each file's diff first and skips the file entirely when it is empty — no write, no `tag.track` line, no detail. The file keeps its mtime, which matters because `reconcile.looks_externally_retagged` reads a file newer than the sidecar's `tagged_at` as "somebody tagged this in Picard" (#220). The `tag.album` line still records that the pass ran: "found the files already correct" is a different fact from "never ran", and only that line carries it. This matters most for #32, which will run one tagging per album per night.

**The one thing a field diff cannot see** is a tag a write would *remove* and never write back — an older spelling of an owned field, kept in the format's owned map only so it can be cleared. MP4's legacy `MUSICBRAINZ_RELEASEID` is the live example. `read_owned` cannot report it, so a file adopted from an older Picard can match a release on all thirty owned fields and still carry a stale MBID under the retired name. `formats.has_superseded_tags` asks about those separately, and a file carrying one is written even when its diff is empty. This is deliberately *not* "any tag `read_owned` skips": `originalyear` is also unread, but a write re-derives it from `originaldate`, so its presence is the normal state of a correct file — treating it as residue would rewrite every dated album on every pass, forever. Since #333 the original-date pair sits on **both** sides of that line, which reads like a contradiction and is not: the lower-case `originaldate` / `originalyear` are Picard's spelling and are re-derived, while the upper-case `ORIGINALDATE` / `ORIGINALYEAR` that Harmonist wrote before #333 are genuine residue and *are* listed as superseded. Same tag, two spellings, opposite dispositions.

**Storage: one row per file**, in the `tag_changes` side table (`activity_store`), keyed to the `tag.track` audit row it details. A side table rather than a column on `events` because that table is scanned on every feed poll. Per file rather than per album because the *before* values need not be identical across tracks — an album tagged unevenly over the years is exactly what `compare.consensus` exists for, so recording one album-level "before" would have to pick one, and a revert would then write that guess over the tracks it didn't come from.

Each row carries **four ways to name its track** — the file name, the release-track MBID, the recording MBID, and the disc-track position — because each fails under a different future edit (a rename, a re-match to another release, a renumber) and no two fail together. The table is append-only, so a row cannot gain a better identifier later, and which file held which MusicBrainz identity is observable only at the instant it is written. This is the same argument the `album_aliases` table makes, one level down.

**Artwork is recorded as sha256 digests** alongside the owned fields, under its own key. `_has_per_track_art` already reads every cover to decide whether per-track art needs preserving, so keeping the hashes is free, and #131 will store the images content-addressed under exactly those digests.

**Rendering is field-first** (`tag_history`): per-file rows are inverted into one row per *field*, annotated with how far the change reached — "album", "all tracks", "3 of 18 tracks", straight from `owned.Scope`. The height of the result is bounded by the number of fields that moved rather than by the size of the album. Where files disagree, the row shows the most common `(before, after)` **pair**, tie broken by file order — the same rule `compare.consensus` uses, kept as a pair so that resolving the two sides independently can't manufacture a transition no track made.

### Keeping the artwork a tagging overwrote

Embedding a cover overwrites whatever image the track already carried, and that image used to be gone for good. `artwork_store` keeps it so the change can be undone (#131).

**Content-addressed files, not database rows.** Cover art runs 200 KB–5 MB per track and is mostly identical between tracks of one album, so the natural handling is dedup by digest: an eight-track album usually costs one file, not eight. Keeping the bytes out of `activity.db` also keeps that store cheap to poll, since it is read on every feed refresh. The digest recorded by the tagging audit *is* the lookup key, so the store needs no index of its own.

**Only what is about to be destroyed.** The copy is taken *after* the per-track-art decision, not during the digest pass — `_has_per_track_art` can still cancel the embed, and an image that survives has no business being backed up. So `_keep_doomed_art` re-reads only the files whose art differs from the incoming cover, and an album already carrying that cover reads nothing at all.

**Bounded, and honest about it.** The store has a size cap (default 500 MB) and evicts oldest-first, which makes restore best-effort by design: an old enough change becomes unrevertable. The album page checks availability *before* rendering, so no Undo button is offered for a change whose image has gone — a button that would fail is worse than none. Usage is shown in Settings, because "how much disk is this costing me" is the question the cap exists to answer.

**Restore resolves everything before writing anything.** A partial restore would leave the album in a state that never existed and neither half revertable, so `restore_artwork` reads every image first and raises `ArtworkUnavailableError` if any is missing. It keeps what it overwrites — an undo is itself a destructive write — and skips files already correct, so running it twice is a no-op. It writes through `formats.write_cover`, deliberately separate from `write_tags`: undoing an artwork change must not silently rewrite tags as well.

The plan is rebuilt server-side from the album's own stored records, keyed by the history row the user clicked, so a client can name a row but never a file or a digest.

The stored JSON keys are `Owned` values, which makes them a **persisted vocabulary**: these records are permanent and unversioned, so a payload written today may name a field a later build has renamed or dropped. The renderer falls back to the raw key for names it doesn't recognise and skips malformed entries, rather than letting one old row take an album page down.

### Undoing a tagging

The records above exist to be readable, but they were shaped to be *reversible*: raw values on both sides, one row per file, four ways to name each track. `tagger.revert_tags` is what spends that (#157).

**The tagging is the unit, not the field.** One Undo per history row, put back everything that tagging changed. A field row on the page is a rendering — records are per file, and `summarise` inverts them — so undoing one field would build a state that never existed: revert `artist` while `artist_sort` and `mb_artist_ids` keep the new value and Harmonist reports the album as drifted against MusicBrainz forever.

**Per-field staleness guard.** A field goes back only when the file still carries what that tagging wrote; anything changed since — a later re-tag, an edit in Picard — is left alone and named in the outcome. That is what makes an *old* row safe to offer rather than a way to overwrite newer work. `owned.values_differ` answers "changed?" for both this and `diff`, because a revert that refused a field the history says it changed would be the two disagreeing.

**`write_owned`, not `write_tags`.** A `TagSet` cannot express absence — `title`, `album` and `artist` are required and written unconditionally — so reverting a first tagging through it would write empty tags instead of removing them. `formats.write_owned` takes a complete owned snapshot and sets or removes each field. It takes the *whole* snapshot rather than a patch because ID3 packs number and total into one `TRCK` frame and MP4 into one `trkn` atom, so a writer handed half a pair would drop the other half. A test replays one snapshot through both paths and asserts they read back identically; without it the two mappings would drift, which is exactly how `media` broke in #149.

**Resolve everything before writing anything**, as the artwork restore does: every file is read first, and a missing or unreadable one raises `RevertUnavailableError` rather than leaving the album half-reverted. Running it twice is a no-op, and a no-op writes no history entry — the same silence a re-tag that changed nothing keeps.

**The undo records itself** as `tag.revert` with its own per-field detail, so it appears in History and is undoable in turn.

**`mb_album_id` is all-or-nothing, and the sidecar follows it** (#158). Identity is the one field that isn't per-file: the sidecar records exactly one release, so a revert that moved the id on some files and left it on others would derive as `INCONSISTENT` and leave nothing coherent to write down. So `_identity_revert` decides it once for the album — every file in the plan must agree on the before-value, still carry the after-value, and account for every file in the directory — or the field is left alone and reported stale.

When it does move, the sidecar goes with it: `mb_release_id` and `tagged_at` are cleared and the album derives as `NEEDS_MBID`. Leaving the sidecar naming a release the files no longer carry would derive as `TAGGING` (§3) — the transient spinner, with no action on it and no way out.

**The release is kept as a confirmable suggestion**, not discarded: the Needs MBID card then offers Confirm & Tag, which is the one-click way back, with a note saying why the album is there. Undoing a *re-match* reverts the files to the older release, and that older release — not whichever one the sidecar was holding — is what gets suggested, because it is what the user asked to return to. The candidate carries no `track_comparisons`: building them needs an MB fetch, and an undo makes no network call. Confirming re-tags through the ordinary path, which rewrites the release's own totals into the files, so nothing here has to guess a track count.

This is the same transition the "wrong match" pencil makes, and deliberately so: both go through `sidecar.unlink` (§3). It differs only in that the pencil leaves the on-disk tags alone and passes no candidate, since there the release was *wrong* rather than merely undone.

Artwork is not in the plan: it is not an owned tag, and it has its own store, its own availability check and its own button (#131). One button per store, each honest about what it can do.

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
  config.py             Pydantic config model + env/TOML loading
  models.py             Album, Sidecar, AlbumState, MatchCandidate, BandcampInfo, …
  sidecar.py            Read/write .harmonist.json sidecars atomically
  scanner.py            Walk music dir → Album objects; group directories by release id (#197)
  album_files.py        The audio/video files in ONE directory (the album-wide question is Album.paths, #197)
  reconcile.py          Derive a sidecar from MBID tag + ©cmt + MB url-rels (orphan recovery)
  url_recovery.py       Recover an embedded Bandcamp URL from ©cmt (precise or artist-root; no scraping)
  bandcamp_hook.py      bandcampsync Syncer subclass: download cap, sidecar capture, purchase↔album linking
  pending_downloads.py  In-memory "potential downloads" (unmatched purchases awaiting a decision)
  redownloads.py        In-memory "archived, awaiting its replacement download" (#132)
  archive.py            Zip an album's dirs into the music root, verify, then delete them (#132)
  mb_lookup.py          MB by-id / by-url fetch (1 req/sec budget)
  mb_cache.py           TTL cache over mb_lookup's by-id fetches, in activity.db (#127)
  mb_search.py          MB free-text search (manual-ingest path)
  match.py              Disk-vs-MB comparison (assess_match): confidence + per-track deltas
  compare.py            Field-by-field tag-vs-MB comparison primitives (Tags section + tracklist)
  tagger.py             Picard-compatible tag writer (+ embedded cover), and the undo of one (#157)
  cover_art.py          Cover Art Archive fetch + cover.* writing
  formats/              Per-format tag I/O (m4a, mp3, flac, ogg, opus; _vorbis shared; types)
                        owned.py names the tags Harmonist writes, per-album vs per-track
                        write_owned sets/removes a whole owned snapshot — what a revert needs
                        quality.py reads the stream itself — rate, depth, bitrate (#130)
  tag_history.py        Invert per-file tag-change records into one row per field; build a revert plan
  artwork_store.py      Content-addressed copies of overwritten cover art (undo for #131)
  activity.py           In-memory ring-buffer log for the Activity tab
  audit.py              Audit log for destructive ops (downloads, moves, sidecar rewrites, …)
  activity_store.py     SQLite persistence behind the activity/audit log
  live_counts.py        Single source of truth for state counts (reset per scan + live moves)
  library_index.py      In-memory sidecar/dedup index (one update point)
  id_registry.py        Stable UUID for albums without an MBID
  demo.py               Demo-mode monkey-patches + seeded sample library
  web/
    main.py             FastAPI app — create_app() + all routes
    security.py         CSRF / TrustedHost / optional Basic-auth middleware
    sync_runner.py      Bandcamp sync wrapper (background thread) + status
    reconcile_runner.py Reconciliation pass over the library (rate-limited MB)
    scan_runner.py      Cache-backed library scan + status
    dir_watcher.py      watchfiles watcher → rescan when the music dir settles
    periodic.py         Generic interval task → the hourly rescan + update check
```

Templates and static assets live at the **project root** (`/templates`,
`/static`), not under `src/` — `web/main.py` walks up to find them.

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

[gardener]
level = "off"      # off | review  (#273 adds enrich); also editable in Settings

[test]
mode = "fixture"   # fixture | cassette | live
unignore_item_ids = []
```

Validation runs at startup via Pydantic; the app refuses to start with an invalid config.

---

## 8. HTTP API surface

Routes return **HTML fragments** (HTMX swaps) except a few JSON status
endpoints: `/healthz`, `/status` (a consolidated sync + reconcile + scan poll),
`/sync/status`, `/reconcile/status`, `/scan/status`. `web/main.py` is the
authoritative list; the shape is:

- **Pages**: `GET /` (inbox), `/about`.
- **Content fragments**: `GET /tasks` (inbox), `/library`, `/activity`.
- **Background jobs**: `POST /sync`, `POST /reconcile`.
- **Per-album actions** (keyed by album id): `/confirm/{id}`, `/reject/{id}`,
  `/recheck/{id}`, `/manual/{id}/…` (search / candidates / assign),
  `/retag/{id}`, `/forget/{id}`, `/surrender/{id}/keep` (Move to Library),
  `/library/{id}/…` (detail / unlink / redownload).
- **Potential downloads** (keyed by purchase item_id): `/pending/{id}/…`
  (match / download / skip).
- **Cover art**: `GET /cover/{id}`; other static assets under `/static/`.

**Album IDs** are the MusicBrainz release id once the album is tagged, otherwise
a UUID assigned by `id_registry` — never a hash of the path.

---

## 9. UX flows

### 9.1 Live sync

- User clicks **Sync**. Button POSTs to `/sync`.
- Server returns a "Sync running…" fragment with `hx-trigger="every 1500ms"` polling `/sync/status` and a sibling polling `/tasks`.
- Each /tasks fetch re-renders the inbox; albums appear as bandcampsync writes them and the per-album MB lookup runs.
- When `/sync/status` returns `state == "idle"` post-run, the polling stops and the button re-enables.
- Per-sync limit: `max_downloads_per_sync` downloads at most N **new** albums per run (enforced per item in `sync_item`); the rest are deferred to the next sync (not marked ignored, so they retry). The finish message reports "N more reached the per-sync limit — run Sync again". Already-on-disk albums are skipped by `sync_item` and never count toward the limit.

### 9.2 Needs MBID → Recheck

- Card has an "Open in Harmony" link (`https://harmony.pulsewidth.org.uk/release?url=<store_url>`) and a "Recheck" button.
- Recheck POSTs to `/recheck/{id}`. On success, the card swaps into the Tagging spinner, then disappears (album moves to Complete).

### 9.3 Manual ingest

- New card includes a manual MBID form alongside "Reconcile from tags".
- Form takes either a full MB release URL/MBID *or* runs the search helper (`/manual/{id}/search?artist=...&title=...`) and presents matches to pick.
- On selection, POST to `/manual/{id}/assign`. The assign tags the album and
  derives its Bandcamp `store_url` from the MBID + `©cmt` (see §2.5), so a manual
  download reaches Needs Link rather than Complete.

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

**Permissions.** Startup (`_validate_runtime_paths` in the lifespan) logs the
process `uid/gid/groups` and probe-writes both dirs, failing fast if either
isn't writable — a permission problem otherwise looks like a stuck scan
(reconcile runs but every sidecar write fails). The Synology gotcha: `user:`
sets uid + *primary* gid only, **not** supplementary groups, so a `1026:100`
process has `groups=[100]` and lacks `administrators` (101) that the host login
carries; if the share grants write via that group or a DSM ACL ("owner" in File
Station is an ACL concept, not the POSIX owner), the container is denied. Fix:
grant **Authenticated Users** / the `users` group Read+Write recursively on the
shares (matches the container's gid across the whole tree), or `group_add` the
granting gid.

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
    # Run as a host uid:gid so sidecars/files written into /music and /config
    # are owned correctly. Docker-native — the image needs no PUID/PGID
    # entrypoint plumbing. Use your own user's ids (`id -u` / `id -g`); 100 is
    # the Synology `users` group. Omit to run as root (root-owned files).
    user: "1026:100"
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

### 10.4 Picking up manual changes (watcher, hourly rescan, per-album refresh)

Three mechanisms, in increasing order of how little they assume: the watcher
hears about a change, the hourly rescan goes and looks, and the album page
re-reads the one album you are looking at. They coalesce through the same `_dirty` flag on
`ScanRunner`, so any number of triggers between two scans still produces one
scan.

#### The file watcher

Files added or removed outside the app — copied straight into the music dir,
or deleted by hand — don't pass through the in-app rescan path. A background
watcher (`web/dir_watcher.py`, built on `watchfiles`) closes that gap: it
watches the music dir and triggers a rescan once activity **settles** (the dir
is quiet for `library.watch_settle_seconds`, default 5s — long enough that
copying many files in lands as one scan, not a scan mid-copy). The per-album
mtime cache keeps the rescan cheap. Configure via `[library]
watch_settle_seconds` in `harmonist.toml` or `HARMONIST_WATCH_SETTLE_SECONDS`.

**Caveat — local mounts only.** The watcher relies on the kernel's inotify,
which fires for changes to a *local* filesystem (the Synology bind-mount of
`/volume1/music` — including writes that arrive there over SMB from another
machine). It does **not** fire when the *container itself* mounts a network
share (the Pi-dev SMB recipe above, or any NFS/SMB `/music`): inotify events
don't cross the network, so the watcher silently sees nothing and the watcher
fails soft (logs, no crash). It can also be killed outright — an exhausted
inotify watch limit on a very large tree ends the task, and from then on
nothing rescans until a restart. That is what the hourly rescan below is for.

#### The hourly rescan

A rescan on a timer, regardless of what the watcher did or didn't hear
(`web/periodic.py`, engaged from the lifespan beside the watcher). It is a
**backstop for a watcher that isn't working**, not a routine mechanism: when
the watcher fires, this finds nothing every time. Both failures it covers —
the network mount, the dead watcher — are silent, which is what makes it worth
its cost; neither is common, which is why it stays out of sight.

That cost is low enough to need no tuning: a no-change rescan is a `stat` per
file and no tag reads at all, because the mtime cache answers every directory
from memory. So the interval is a **constant** (`main._RESCAN_INTERVAL`, one
hour), not a setting — the user has nothing to trade off against, and a knob
would only invite tuning a thing nobody should have to think about.

`web/periodic.run_periodically` is deliberately generic — it owns no state and
decides nothing about what a tick means — so the metadata gardener's paced pass
(#270) can use the same timer rather than inventing a second one.

**It is invisible.** A scan normally advertises itself: a *Scanning* pill,
and the **inbox busy lock** that dims `#task-list` and turns off its pointer
events so a click can't land on a snapshot being rebuilt. That is protection
for a scan the user set in motion. Applied hourly on a timer it is just the
inbox freezing under their cursor, so `ScanRunner.request_quiet_rescan()` runs
the same scan without publishing a status. If a real trigger arrives mid-rescan
the scan in flight is **promoted** — the pill and the lock appear at the moment
there is something to protect.

The completed-scan counter the client refreshes off (`ScanStatus.seq`) now
advances only when the snapshot is genuinely **different**. Otherwise the
hourly rescan over an untouched library would re-render the inbox every hour to
show exactly what was already there.

**It never runs during a sync or a reconcile pass** (`_periodic_rescan_if_idle`).
The watcher waits for the directory to go quiet precisely so it can't scan
mid-copy; a timer has no such instinct, and a rescan landing mid-download
would read a part-written album and record it as one Harmonist has "started
tracking" — a permanent Activity entry about a transient. Skipping the tick is
enough: both runners request a scan of their own when they finish.

**Repetition is its failure mode.** Anything the `harmonist` logger
emits at WARNING or above is mirrored into the Activity feed, so a condition
that doesn't clear on its own — an unmounted volume, one corrupt sidecar —
posts an identical entry every interval, forever. Two places guard against it:
`run_periodically` logs only the FIRST of a run of failures (and logs recovery
at INFO), and `scanner._warn_once` complains about an unbuildable directory only
when its signature has changed since the last complaint, so "I tried to fix it
and it's still wrong" is still reported while "still broken, unchanged" is not.

A third guard is different in kind: `extra={"_diagnostic": True}` keeps a record
out of the feed **entirely**, rather than rate-limiting it. For measurements —
`timing.warn_if_slow`, which warns when an operation crosses a threshold — the
level is right for the log and wrong for the feed: nothing failed, nothing was
lost, and there is nothing to act on. The distinction is worth holding onto when
adding any new WARNING: the mirror's rule is *every warning is news the user
should see*, which is true of a failure and false of a stopwatch.

#### The background update check

The second caller of that timer, and the scheduled half of the metadata
gardener (#270): `gardener.sweep`, on a worker thread, asking MusicBrainz about
the albums whose release payload is stalest and setting `update_available` from
what comes back. It is what makes the Library's **Update available** filter
report on albums nobody has opened, rather than only on the ones a human has
already looked at.

**Off unless asked for** (`[gardener] level`, `off` by default). The pass writes
nothing to anybody's files, so the default is not protecting the library — it is
protecting the MusicBrainz budget, which is a volunteer service and one request
per second for everything Harmonist does. `review` is the only other level
today; #273 adds `enrich`, the level at which some of what the pass finds gets
applied on its own.

**The level is a live setting, so the timer is not conditional** (#312). It sits
on the Settings page as **Background update checks** — the internal name is not
a user-facing one — beside the other knobs that apply without a restart, and
`_update_check_if_idle` re-reads `app.state.cfg` on every tick and returns early
when the level is `off`. The task is therefore created unconditionally, which
inverts the earlier reasoning that a default install should carry no idle timer:
a config change cannot retroactively start a task that was never created, and
one sleeping asyncio task costs less than the restart it saves. The lifespan
closure holds the *startup* config, so reading the level from there would leave
the setting saved, looking applied, and silently doing nothing — worse than
having required the restart.

**And a way out of the first empty interval.** `run_periodically` fires one full
interval after startup and never at startup, so turning the check on buys ten
minutes in which the library looks exactly as it did. `POST
/settings/update-check` — **Check now**, beside the level — runs one pass
immediately. It runs an *ordinary* tick, a couple of albums rather than a sweep:
giving the button its own larger budget would put the burst back in at the one
moment somebody is certainly sitting in front of the app. It shares the tick's guards rather than bypassing them, `off`
included: the level is what the button asks permission from, so it cannot be the
way round it. Whichever guard declines says so in the flash, because a control
answered with silence reads as broken whether it ran or refused.

**Detect-only is the classifier's answer, not a phase.** `owned.AUTO_APPLY` is
empty, so every change needs a person; until #271 gives a finding somewhere to
live there is nothing for an unattended pass to hand one to. The consequence
worth stating is that this pass *cannot damage a library* — the strongest thing
that can be said about a background job that runs while nobody is watching.

**The early exit is the idempotency invariant, mechanically.** The stored
payload is read before the fetch and an unchanged one ends that album there: no
file reads, no plan, nothing recorded. A second pass over a library MusicBrainz
has not touched costs its requests and nothing else. Note the asymmetry that
makes this correct — an unchanged payload *skips* an album, it never *clears*
its flag, because "MusicBrainz has not moved" and "the files have nothing
outstanding" are different facts and an album whose update was never applied
still has one.

**The queue is dated off the cache**, `mb_cache.fetch_times()`, so scheduling
needs no state of its own: `fetched_at` already says when each release was last
read, never-fetched sorts first, and an album asked about inside
`gardener.RECHECK_AFTER` is skipped. Two cases escape that clock and would
otherwise cost a request per pass forever, because neither leaves a refreshed
row under the id the sidecar names — a **merged** release, whose row lands under
the id MusicBrainz redirected to (§5), and a **deleted** one, which stores
nothing at all because a negative is never cached. `gardener._asked` remembers
the ids this process has spent a request on, which closes both.

**The rate is derived from a goal, not chosen** (#349). `gardener.SWEEP_WINDOW`
(24h) says how long a full sweep of everything currently due should take, and a
tick — `gardener.SWEEP_TICK`, ten minutes — asks about `tick / window` of the
queue, so two or three albums at a time. The point is that the rate answers to
the size of the backlog: a first run with the whole library due gets a
proportionally larger slice and still finishes inside the day, while steady state
settles at `len(library) / RECHECK_AFTER` a day, which is the demand rather than
a multiple of it.

This replaced a hand-set pair — a hundred albums an hour — that worked out at
roughly eight times what the goal needed and spent it in hundred-request bursts.
Two properties of the replacement are worth stating because neither is obvious:

* **The window must be well under `RECHECK_AFTER`.** The queue settles where
  inflow meets outflow: albums come due at `len(library) / RECHECK_AFTER` a day
  and clear at `due / SWEEP_WINDOW`. A day-long window holds the queue at a
  seventh of the library; a week-long one settles with the whole library
  permanently overdue.
* **Sizing the slice is also what stops the pattern repeating.** `_due` orders
  off the fetch times an earlier pass wrote, so whatever is swept together comes
  due together one `RECHECK_AFTER` later and the shape recurs. Under the old cap
  that shape was a hundred-album burst, stamped on the library by its first run
  and replayed weekly; under a two-album slice there is nothing left to recur.
  Deliberately no jitter: a start time nobody coordinates and a rate that varies
  with the library are spread enough, and randomising the clock would be a knob
  to document for no gain.

The tick lives in `gardener` beside the window rather than in `web/main.py`,
because the constant that schedules the pass and the constant the pass divides by
are the same fact; keeping them in two modules is how the old pair drifted.

**It stands aside** for a sync, a reconcile pass, or a library that has not been
scanned yet, and refuses to start on top of a pass still running. One reason
between them: the rate limit is a single shared queue and anything the user set
in motion is waiting on it, while the check's albums have waited a week and can
wait another ten minutes. And when MusicBrainz keeps failing it gives up rather
than spending a request per album to learn the same thing each time — a 404 is an
answer and does not count towards that, or a library with five deleted releases
would abort every pass at the fifth. That failure run is held **across** ticks
(`gardener._consecutive_failures`): a tick is a handful of albums now, so a
counter reset every ten minutes could never reach the threshold, and an outage
would be met with a fresh slice of requests every tick forever. Held, the
threshold is reached once and every later tick ends on its first failure until
something answers — one request and one line per ten minutes, and the loud
warning fires only on the transition.

#### The per-album refresh

The album page re-reads *its own* album's directories before rendering
(`ScanRunner.refresh_album`), so what it shows is what is on disk now — not
what the last scan happened to see, which on a network mount could be startup.
The album page is where the user decides whether to re-tag, and deciding that
against a stale reading is the failure that costs something.

It is one directory listing plus a `stat` per file, and on a signature hit
nothing more: no tags are read. That is affordable per page view; a full
`refresh_now()` (which walks the entire library) is not, which is why the page
does not just call one. Only the album's known directories are re-read, so an
album gaining a *whole new* directory is still the hourly rescan's job.

This is what makes the runner retain its per-directory `ScannedDir` entries:
`merge_by_identity` is one-way, so a merged Album can no longer say which files
came from which folder, and rebuilding one album's entry without them would
mean re-walking the library to put the other discs back.

**What none of this can tell you.** A scan derives current state from current
files and keeps no record of what was there before, so a refresh can show a new
value but never report that it *changed*. An external edit that doesn't affect a
derived state — a hand-fixed title, an artist credit corrected in Picard —
leaves no trace in the album's history. One that *does* (an MBID diverging from
the sidecar) already surfaces as mis-tag/inconsistent. Emitting "changed outside
Harmonist" records needs the last-written state of #86 and belongs with #32.

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
6. **Per-sync download limit:** download at most `HARMONIST_MAX_DOWNLOADS_PER_SYNC` new albums per run; defer the rest to the next sync (a large first sync trickles in N at a time rather than failing). Enforced per item in `sync_item`, not as a pre-sync abort.

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

## 12. Audio format support

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

## 13. Best-effort handling of imperfect libraries

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

### 13.1 Partial tagging

Some tracks in an album dir have the MB Album Id atom, others don't.
Common cause: user added a track to an existing album without re-tagging,
or Picard was interrupted partway through.

**Detection:** scanner reads MB Album Id from every file. If N of M files
are tagged with the matching MBID (M > N > 0), the album is *partially
tagged*.

**State:** stays `COMPLETE` (the existing logic treats "any file matches"
as tagged). The scanner's Album object gains a `partial_tag_count` field
(`"N/M"`-style) — not persisted, just derived at scan time.

**UI:** the album page shows a "5/6 tracks tagged" badge. In v1
this is informational only; the in-app resolution (a Re-tag button that
re-runs the tagger across all files, backfilling untagged ones
idempotently) ships with the §2.4 Re-tag use case post-v1. In the
meantime the user can re-tag externally with Picard.

No new state — partial tagging is a quality issue, not ambiguity.

### 13.2 Inconsistent dirs (multiple albums in one folder)

Tracks in an album dir disagree on album title (`©alb` / `TALB` / `ALBUM`)
or MB Album Id. Common cause: messy filesystem; user dumped multiple
albums into one folder.

**Detection:** scanner reads album title + MB Album Id from every file in
each album dir. Varying MB Album Ids are always `INCONSISTENT`. A varying
album title is `INCONSISTENT` only when some file lacks an MB Album Id —
**a release id every file agrees on settles it, and the titles are not
consulted at all.** The MBID is the release identity; the album title is a
display string each ripper derives its own way, and XLD folds a named medium
into it (disc 2 of *U.F.Orb* reads `U.F.Orb - bonus disc`), which the old
title-or-MBID rule accused of being two albums (#381). One file without an
MBID puts the titles back in charge: nothing vouches for a stray dropped into
the dir, so its own title has to. Compilations (same album title + MBID,
varying track artists) are NOT flagged — that's legitimate.

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

### 13.3 Incomplete albums

On-disk track count is **less than** the MB release's track count, but
the user has a valid reason (CD rip missing a hidden track, intentional
selection, vinyl-only edition where the digital MB release has bonus
tracks, etc.). Without special handling these stall in `NEEDS_MBID`
forever because `TagMismatchError` would block the tagger.

**Handling:** the suggestion card (Needs MBID with a candidate) gains a
button next to Confirm / Dismiss suggestion:

- **Confirm as Incomplete** — runs the tagger in incomplete mode. The
  tagging writes the release's own track/disc totals into every file it
  touches, so the album's state becomes `INCOMPLETE`, derived at scan time
  from those tags (§3). Nothing about the count is persisted separately.

A Library album that is already tagged reaches the same mode by the same kind of
decision, offered where the problem appears: **Re-tag as incomplete**, on the
refusal a re-tag returns when the release has since grown (§2.4 step 4). Same
tagger mode, same lack of persistence — the only difference is that the album
already has an MBID, so the answer belongs on its page rather than on a
suggestion card.

**Tagger incomplete mode:** doesn't raise `TagMismatchError` on
`file_count < track_count`. Matching the on-disk files to a subset of MB
tracks goes through `compare.assign` like everywhere else — release track
id, then disc-and-track number, then file order (#232). MB tracks without
a matched file are skipped.

**State after Confirm as Incomplete:** `INCOMPLETE`. This is a distinct
terminal state, not a flagged variant of `COMPLETE` — the state enum
alone tells the UI what to render, no sidecar metadata peek required.
The album page shows a small "incomplete" badge plus a
per-track list of which MB tracks weren't on disk.

**Promotion to Complete:** if the user later adds the missing tracks on
disk, the next scan sees every expected track present and the state promotes
to `COMPLETE` — no Recheck and no lookup, since the expectation was already
in the files. Conversely, if MB upstream gains new tracks, a Re-tag rewrites
the higher total into the files and the album either stays `INCOMPLETE` (if
the new count still exceeds what is on disk) or routes back through
`NEEDS_MBID` (with a fresh suggestion) for re-confirmation.

**Out of scope:** file_count > track_count (extra tracks on disk) — same
class as inconsistent; user resolves externally.

### 13.4 Explicitly out of scope

- **Folder splitting** (separating two albums in one dir into two dirs):
  filesystem-level operation; user does this with Finder/CLI/Picard.
- **Tag editing of individual files** outside MB lookups: Picard's job.
- **Recursive directory disagreement** (nested album dirs): scanner
  treats every dir with audio files as a single album. Atypical layouts
  must be flattened first — with the one exception of a release split
  across per-disc directories, covered in §13.5.
- **Format conversion**: Harmonist never transcodes.

### 13.5 An album is its release, not its folder

A library assembled over decades keeps one release in several directories —
`Album/CD1` + `Album/CD2`, a box set filed disc by disc, a compilation split
into its component EPs. Treated as one album per directory they are several
Library tiles, each with a fraction of a tracklist and each wrong about what it
has.

**Identity, not location.** An album is the files that name its
`MusicBrainz Album Id`, wherever they sit. The scan already reads that id for
every file, so grouping on it costs nothing it does not already pay
(`scanner.merge_by_identity`). The directory is where the files happen to live;
the release id is what the album *is*.

There is deliberately **no containment rule** — no common ancestor, no depth
bound. `Hybrid/Wide Angle` + `Live Albums/Hybrid/Live Angle` is a reasonable way
to organise a library, and a boundary would rule it out to prevent merges that
are correct anyway.

**Duplicates must not merge**, and that is what makes dropping the boundary safe.
Two directories of one release are two *parts* of it only if they hold different
tracks:

- **Release-track ids** when the files carry them. Every track of a release has
  its own, so different discs have disjoint sets and two copies of one disc have
  identical ones. This reads the thing in question rather than a proxy for it —
  it establishes not that the parts *claim* to differ but that they *do*.
- **Distinct disc numbers** as the fallback, for a rip carrying no track ids.
  Weaker (what the files claim, not what they hold) but still exact.
- A **mixture is refused**, and so is a part with any untagged file: a partial
  set makes disjointness meaningless, because two copies with half their files
  untagged would compare as disjoint on the tagged half.

No evidence either way means no merge.

**Nothing on disk changes.** Grouping is a reading of what is already there —
no file moves, no sidecar written, no migration. Every part keeps its own
complete sidecar: none is primary, none is a shard, and a folder moved out of the
group is still a correct album on its own. That property is the point, because
reorganising folders is exactly what this exists to tolerate.

The album's sidecar is the **merge** of its parts' — earliest `added_at`, latest
`tagged_at`, any `store_url`, and a decision recorded on any part
(`purchase_unavailable`, `tracks_unavailable`) taken as made about the album,
since that is what it was about.

`Album.path` is the primary directory — the one holding the most tracks, ties
broken by path so the choice is stable across scans — because something has to
answer "where is this album" in one line. `Album.paths` carries the truth, and
the album page lists every folder (#198).

**The tagger must be given the album's files**, not its directory: `album_path`
is only the primary one, and tagging what is under that alone would silently
leave the rest of the album on its old tags.

This replaced a directory-based rule (#16: "a sidecar'd parent with no audio of
its own owns everything beneath it"), which could not pass either real case —
both put an album's parts alongside sibling albums, and those siblings counted as
"leftovers" that blocked the merge. That is the normal layout, not an exotic one.

## 14. Store support

Sidecars carry a single `store_url` field — any storefront URL Harmony
recognises. The first-class store is Bandcamp (full sync + match flow);
others are accepted as URL inputs into the manual / reconcile paths and
handed to Harmony for MB seeding.

### 14.1 Bandcamp (first-class)

- **Purchase listing + download**: `bandcampsync` (subclassed as
  `HarmonistSyncer`).
- **URL → MB MBID**: MusicBrainz URL-relationship lookup
  (`mb_lookup.lookup_by_bandcamp_url`).
- **Tag-time evidence**: `©cmt` comment on downloaded `.m4a` files
  contains the Bandcamp album URL (used by `url_recovery` to seed a
  store_url on New albums).

### 14.2 Other stores (URL-only)

For any other store (Beatport, Discogs, Deezer, etc.) the sidecar can
hold a `store_url`. The reconcile/recheck flow then asks Harmony to seed
the MB release from that URL, and tagging proceeds via the existing
MB-by-MBID path. This adds no store-specific code.

### 14.3 Beatport — why no first-class support

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

Decision: accept Beatport **URLs** in `store_url` (free, via §14.2), but
do not build a Beatport-specific scraper, syncer, or metadata enricher.
Users with Beatport purchases manage downloads out-of-band and paste
the release URL when reconciling.

## 15. Open questions

- **Cover art serving:** the inbox UI references covers via `/static/music/...`. Simplest path is a FastAPI mount of the music dir, scoped to image files only. Decision pending.
- **Cover art library optimisation:** future enhancement, not in scope here. If the library grows big enough to matter, a separate batch tool can downsize covers across all albums. Keep that out of the tagger's hot path.
- **Multiple cover art types:** CAA has front, back, booklet, etc. Prototype embeds front only and stops there. Other types deferred.
- **Re-tag cover behaviour:** if user has manually replaced `cover.jpg`, do we re-fetch from CAA on retag (overwriting their choice) or trust the local file? Current design trusts local; flag in the manual-test plan.
- **MB rate limiting:** musicbrainzngs imposes 1 req/sec by default. For batch tagging across many tracks during a single match, we may need to sequence carefully. Probably fine for the prototype's scale.
- **Single-writer assumption on the ignores file** — if the user runs bandcampsync standalone outside the container, are concurrent writes possible? In practice almost certainly no, but worth flagging.
- **Backup before tag write?** Optionally write `<file>.bak` before mutagen.save() during the prototype phase, removable by config later. QA's call.

## 16. Future enhancements

Decided-but-deferred features. Captured so the state model and UI don't preclude
them. (Re-tag from MB and the Activity feed have since shipped — §2.4, §6.)

- **Re-download from Bandcamp** — a per-album action for a fully-synced
  (`COMPLETE`, bandcamp-sourced, `item_id` known) album that forces
  bandcampsync to fetch it again. Use cases: the user changed their
  default download format and wants existing albums re-fetched in the
  new format, or deleted the local files expecting Bandcamp to
  re-supply them. Tricky because it must deliberately override the two
  dedup mechanisms that normally prevent re-downloads — the sidecar
  `store_url` short-circuit in `bandcamp_hook.sync_item` *and* the
  item's entry in `ignores.txt` — for that one album only, while still
  respecting the per-sync download cap. Surfaces on the album page
  alongside Re-tag / Forget. Deferred for that complexity.
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
  reconcile pass already publishes live inbox/library/New/Needs Link
  counts as it files each orphan (base captured at start + running
  outcome tallies, no mid-pass rescan — see `reconcile_runner.py`'s
  `ReconcileStatus` and `reconcile_pending_orphans`, and the live-count
  panel in `tasks.html`). The **sync** phase does not: `_detect_mistags`,
  `_report_unmatched`, and the closing `request_scan` all run at the *end*
  of the sync `runner_fn`, so the inbox/library numbers only refresh once
  sync completes (snap-at-end). Extend the same base + tallies approach to
  sync — as each purchase links its `item_id` and an album moves
  NEEDS_SYNC → COMPLETE, decrement Needs Link / increment Library live —
  without a full mid-sync rescan (which would hammer the network mount).
  Low priority: the end-of-sync snap is functionally correct; this is
  purely a responsiveness polish.
