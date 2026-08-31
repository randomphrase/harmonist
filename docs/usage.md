# Using Harmonist

What to do with Harmonist once it's running. For getting it running, see
**[installation.md](installation.md)**; for exposing it safely, see
**[deployment.md](deployment.md)**. If you want the mechanism rather than the
workflow — the state machine's exact derivation rules, the sidecar schema, the
tagging contract — that's **[design.md](design.md)**, which is written for people
changing the code.

Everything here can be tried without touching your own music: start Harmonist in
[demo mode](installation.md#demo-mode) and the sample library covers every state
described below.

## The shape of the app

One page, three tabs, plus Settings:

- **Inbox** — albums that need something from you, grouped by what they need.
- **Library** — albums that are done, searchable, with filters for the ones that
  are done but not *right*.
- **Activity** — everything Harmonist has changed, newest first.

**Sync** lives in the header and is available from any tab.

## Onboarding an existing library

Point Harmonist at a music library you already have and it does its best to
**adopt** it — recognizing what's already tagged, linking your previous Bandcamp
downloads to your purchases, and flagging the rest for review — all **without
re-downloading anything you own**.

It works best on a library that's already in reasonable shape. Harmonist assumes:

- **One album per folder** — each directory of audio files is treated as a single
  album. A folder mixing several albums is flagged **Inconsistent**; split it with
  [Picard](https://picard.musicbrainz.org) first.
- **One album can live in several folders** — per-disc subfolders, a box set
  filed disc by disc, or a compilation you split into its component EPs. If the
  folders are tagged to the same MusicBrainz release and demonstrably hold
  *different tracks* of it, Harmonist treats them as one album, wherever they
  are in your library. Nothing is moved or rewritten, and two copies of the same
  release are left alone — that check is the point. The album's page lists every
  folder it drew from.
- **Already tagged** — ideally Picard-tagged. Harmonist reads the MusicBrainz
  Album ID from your files to recognize what's matched; anything untagged lands in
  the inbox for you to match by hand.

**Recommended order:**

1. **Get everything matched first.** Work through **Needs MBID** — this is the one
   step that genuinely needs you; untagged/unmatched albums are the main thing
   Harmonist can't do on its own. Aim to clear it *before* your first sync.
2. **Then run your first Sync.** While unlinked albums remain, that sync runs in
   **link-only** mode: it links your on-disk albums to your Bandcamp purchases and
   **downloads nothing new**. Anything it can't confidently match surfaces as a
   *potential download* to Match / Download / skip.
3. Once everything's linked, later syncs fetch genuinely new purchases as normal.

## The Inbox

Every album is in exactly one state, **derived** from what's on disk each time
Harmonist looks — there's no status field to get out of step with your files.
The inbox groups by state, and each group says what will clear it.

<!-- screenshot: docs/screenshots/inbox.png -->

**New** — no sidecar yet: an album Harmonist has seen but knows nothing about.
Search MusicBrainz by name or paste an MBID. If the files already carry a
MusicBrainz Album ID, a reconcile derives the rest from the tags.

**Needs MBID** — no confirmed MusicBrainz release. Three ways out, and the card
leads with whichever fits:

- *With a suggestion.* Harmonist found a candidate but isn't certain (usually the
  track lengths are outside tolerance). The card shows your files against the MB
  release side by side, per track. **Confirm** tags the album from it;
  **Confirm as Incomplete** accepts it when you knowingly have fewer tracks;
  **Dismiss suggestion** leaves the album where it is.
- *Without one.* Search by artist + title, or paste an MBID directly.
- *Not in MusicBrainz yet.* Common for Bandcamp-only releases. **Open in Harmony**
  seeds it to [Harmony](https://harmony.pulsewidth.org.uk) in a couple of clicks,
  then **Recheck** picks it up — so every gap you hit makes MusicBrainz better for
  the next person.

**Needs Link** — tagged from MusicBrainz, but not yet tied to the Bandcamp
purchase it came from. **Sync** links these; that's the common case. If a sync
can't find the purchase, use **Try a different URL** or **Mark purchased
elsewhere**.

**Files that describe a different release.** If your tags say the album is a
single disc while the MusicBrainz release it's matched to has three, the album's
page says so. It isn't incomplete — by its own tags it's fine — but the tags are
either stale (**Re-tag from MB** brings them into line) or the release is the
wrong one (**Wrong MusicBrainz match**). Harmonist won't guess which.

**A release that's vanished from MusicBrainz.** Editors occasionally delete a
release (usually a duplicate). The album's page says so rather than showing a
fetch error, and **Find a new release** sends it back to Needs MBID with its
store link intact — a Recheck usually finds the replacement straight away. Re-tag
is disabled meanwhile, since there's nothing to re-tag from. Nothing happens
until you press the button: your files keep their tags, and the album stays in
the Library if you'd rather deal with it later. Tags and Tracks still list what's
on your files, with no MusicBrainz column beside them — that's what you'll be
searching on to find the replacement release.

![The banner shown on such an album: "This release is gone from MusicBrainz — it
was deleted there, usually because it was a duplicate. Your files are untouched
and still carry its tags, but Harmonist can't compare them against anything or
re-tag them until the album points at a release that exists." A Find a new
release button sits to its right.](images/release-gone.png)

**Inconsistent** — the files in one folder disagree about album title or MBID,
which normally means several albums share a directory. Harmonist won't guess:
split them in Picard and refresh.

**Tagging** — transient, shown while a tagging runs.

Albums that are **Complete** or **Incomplete** aren't in the inbox at all; they're
in the Library.

## Syncing

**Sync** in the header pages your Bandcamp collection and reconciles it against
what's on disk. The popover next to it carries the options — including the
per-run download cap — and the status bar reports progress.

A sync runs in one of two modes, and says which:

- **Link-only**, automatically, while any album is still unlinked. It matches
  purchases to albums you already own and **downloads nothing**. This is what
  makes adoption safe: your first sync can't re-download a library you already
  have.
- **Full**, once everything's linked. New purchases download as normal.

**Potential downloads.** A purchase a sync can't confidently tie to an album on
disk isn't downloaded on a hunch — it surfaces as a card for you to resolve:
**Download** it, mark **Already in your library?** to search your own albums for
it by artist and title, or **Don't download** to set it aside.

That last one isn't a one-way door: Settings lists everything under **Won't
download** with a **Restore** button beside each.

**When a purchase can't be found at all.** A *full* sync that pages your whole
collection and still can't match an album has learned something real, so it says
so: the album is demoted to Needs MBID with a read-only "no purchase found" note,
keeping its current release as context. **Move to Library** accepts it as yours
and done.

**When the match looks wrong.** After a sync, Harmonist checks whether you
actually own a *different edition* of what an album is tagged as — a live version
where the files claim the standard release, say. If so the album is flagged
**Possibly mis-tagged**, with the edition you own offered as the fix.

## The Library

Everything that's finished. Tiles are paged — **Previous**/**Next**, with a
**Show N per page** control that's remembered between visits — and the page rides
in the URL, so Back, a bookmark, and returning from an album all land you where
you left off.

**Search** finds an album by artist or title. Type and press Enter; every term
has to match, but where and in what order doesn't, so `aphex ambient` finds
*Aphex Twin — Selected Ambient Works*. Case, accents and punctuation are ignored
(`bjork` finds Björk, `85 92` finds *…Works 85-92*).

<!-- screenshot: docs/screenshots/library-filters.png -->

Finished isn't the same as right, so the filter chips find the albums that are
done but still wrong — and the album whose MusicBrainz entry has improved since
you tagged it — each with its count:

- **Incomplete** — fewer tracks on disk than the MusicBrainz release has. The tile
  badges "N of M". The count comes from your files' own tags, which Harmonist and
  Picard both write, so this works on an adopted library straight away with no
  lookups. When a whole disc of a set is missing the tile just says *Incomplete* —
  nothing on disk records how long the absent disc was. Video tracks (`.m4v`)
  count towards completeness even though Harmonist can't tag them yet, so a
  CD+DVD release you've ripped in full reads as complete. If you *didn't* rip the
  bonus DVD at all, that's not counted against you either — the album's page
  lists the discs that aren't on disk, so nothing is hidden. A DVD you ripped
  only part of does still count as incomplete.

  The album's own page says the same thing in words, under the MusicBrainz and
  Bandcamp badges, so you don't have to work it out from the tracklist below it:
  *"10 of 11 tracks on disk"*, or — where the tile could only manage
  *Incomplete* — the disc you're missing, *"Disc 2 of 2 is missing"*.

  Some albums are incomplete on purpose and always will be — you ripped only the
  stereo mixes off a Blu-ray, or the hidden track was never ripped and the CD is
  long gone. Tick **Don't warn me about this**, beside that badge, and the album
  drops out of this list. It stays marked "N of M" — the album really is short,
  and that goes on being true — but the badge turns from amber to grey, here and
  on the tile: a statement rather than something to fix. Untick it if you change
  your mind.
- **Partially tagged** — some files carry the MusicBrainz Album Id and some don't.
  A half-finished tagging run, or Picard applied to part of a folder, leaves this.
- **No artwork** — correctly tagged, fully linked, and still a grey square in
  Plex or Navidrome.
- **Update available** — nothing is wrong with these. MusicBrainz has simply
  learned something since you tagged them: an ISRC filled in, a catalogue number
  corrected, a release date fixed, a track retitled. Re-tag from the album's page
  to take it, and the album drops off the list.

  The album's own page says *what* the update is. The Tags comparison covers
  every album-level tag Harmonist writes, and the tracklist grows a column for
  each per-track tag that has actually moved — so a sort name MusicBrainz has
  corrected on one track appears against *that* track. Between them, most of an
  update is visible where you would look for it anyway. Anything left over — more
  differing tags than the table will take — goes in a short list under the
  comparison, **Other tags a re-tag would change**, each with its before and
  after, laid out the way the History entry will be once you've applied it.

  Harmonist works this out from the MusicBrainz releases it has already fetched,
  so the filter costs no lookups and fills in as you browse — an album you open
  is checked while you're there. That does mean the list is what Harmonist has
  had reason to look at, not the whole library: an album nobody has opened since
  Harmonist met it hasn't been compared yet, so it won't be here even if there is
  something waiting. The filter under-reports rather than inventing work.

  A flagged album also carries a purple **Update** badge on its Library tile, so
  you meet one while browsing rather than only when you go looking for them. The
  badge is a positive signal only: it appears where there *is* something waiting,
  and its absence means "nothing found, or not compared yet" — never "this one is
  fine".

  You can have Harmonist go and look instead of waiting to be asked. Set
  **Background update checks** to *Look and report* on the **Settings** page and
  a small background pass works through the library, checking the albums it has
  looked at least recently against MusicBrainz — roughly a hundred an hour while
  nothing else is happening, so every album comes round about weekly. It only
  ever *looks*: taking an update is still a button you press. It waits for any
  sync, reconcile or scan to finish rather than competing with them, and it is
  off until you turn it on.

  The setting applies straight away — no restart — but the first pass is
  otherwise up to an hour off, which looks like nothing happened. **Check now**,
  beside the setting, runs one immediately; what it finds turns up here and as
  the purple **Update** badge on the tiles. (`level` under `[gardener]` in
  `harmonist.toml` is the same setting, for a config-managed install.)

Search and the filters compose: searching inside a filter narrows within it, and
the chip counts follow the search, so each one tells you what it would actually
show. When both are on, the box says which filter it's searching inside, and an
empty result offers a way out of each separately.

The active filter and the search ride in the URL too, so either view is a link you
can share or bookmark. Neither is remembered between visits — a page size is a
standing preference, but a filter or a search is a question you asked once, and
one silently restored weeks later just looks like your library has shrunk.

## An album's page

Clicking a tile opens a summary that answers "is this the right release?"; from
there, the album's own page at `/album/<id>` has the whole story. It has an
address, so it can be shared, bookmarked, and gone back from.

This page re-reads the album's folders every time you open it, so what it shows
is what is on disk now — a track added by hand, or a title you fixed in Picard a
moment ago, is already there. That matters here more than anywhere else, because
this is the page where you decide whether to re-tag.

<!-- screenshot: docs/screenshots/album-tags.png -->

**Tags** compares your files against MusicBrainz field by field and shows **only
what differs**, with small changes marked inside the value — so a pipe-joined
artist credit against MusicBrainz's join phrase, or a bare year against a full
date, is visible at a glance rather than needing a character-by-character read.
When your own tracks disagree with each other, it says what's wrong ("missing on
1 track"), and clicking that lists the tracks.

The MusicBrainz IDs among those fields — the album artist's, the release
group's — read as the artist and release they name, and each is a link to
MusicBrainz; hover one to see the ID itself. An ID your files carry that
MusicBrainz has moved away from has no name to show, so it stays as its raw
value, which is what makes that row's difference visible.

MusicBrainz allows one request per second, so the release behind this comparison
is cached and re-used for an hour rather than re-fetched every time you open the
page. The note above the fields says when it was last read — "read 20 minutes
ago" — with **read again** beside it if you've just edited MusicBrainz and want
to see the edit now. Re-tagging and **Recheck** always go to MusicBrainz itself,
so an action never runs on a cached answer.

**Tracks** compares the tracklist, flagging tracks that are missing, unreadable,
or absent from MusicBrainz. Video files are listed as present and marked *video*;
Harmonist reads their tags but never writes them, so they aren't compared against
MusicBrainz.

Its columns fit the album rather than being fixed. Number, title and length are
always there; beyond those, a per-track tag gets a column when it has something
to say — it differs from MusicBrainz on some track, the tracks disagree with each
other, or a track is credited to somebody the album isn't. On a single-artist
album where every track agrees, an Artist column would be one name repeated all
the way down, so it isn't drawn; on a compilation, or an album with a guest on
track 6, it is. The tags that were checked and agreed are named under the table —
*"Artist, ISRC and 3 others are the same on every track and match MusicBrainz"* —
with their values one press away, so a tag Harmonist looked at never reads like
one it ignored.

Artist credits are shown as the artists they name: *Rafael Anton Irisarri feat.
Julia Kent* is two links to MusicBrainz, joined the way MusicBrainz joins them.

**History** gathers everything Harmonist has recorded about the album, including
records written before it was last re-identified, so history doesn't rot when a
release is re-matched or renamed.

The actions live on the page too: **Re-tag from MB** (also the remedy for a
partially-tagged album — it writes the id to the files missing it), the
wrong-match pencil beside the release badge (which sends the album back to Needs
MBID to re-pick, leaving your files' current tags alone until you re-tag), and
**Forget**, which deletes the sidecar and returns the album to New without
touching a single audio file.

**When MusicBrainz has gained tracks since you were tagged.** A re-tag won't
write a release's tags onto fewer files than it lists — that guard is what stops
a half-finished download being tagged as the whole album. So if an editor has
added tracks in the meantime, Re-tag stops and tells you both numbers, with a
**Re-tag as incomplete** button beside them. Press it and your files take the
release's current tags anyway; the album is then listed as incomplete, saying how
many tracks it's short, and stays in your Library. Nothing is written until you
press it. (If your copy is short because the *artist* added tracks, **Re-download**
below is the fix that gets you the missing audio.)

### Re-downloading an album

**Re-download** fetches the album from Bandcamp again. Two reasons to want it:
you bought MP3s years ago and would rather have FLAC, or the artist has added
tracks to the release since you bought it and your copy is short. It appears on
any album Harmonist has linked to a single Bandcamp purchase — not on a CD rip,
and not on one of the rare albums whose store URL matches several purchases,
where Harmonist can't tell which one to fetch.

It is the one action here that removes files, so it works like this:

1. **Your current files are zipped into the top of your music folder**, named
   `Artist — Album (archived 2026-08-24).zip`. Everything goes in — audio,
   artwork, and Harmonist's own sidecar — so unzipping it *there* puts the album
   back exactly as it was, still matched to its release.
2. **The zip is checked before anything is deleted.** If it can't be written, or
   comes back short, nothing is removed and you're told why.
3. The album is taken off disk, its purchase is taken out of your Bandcamp
   ignores, and a sync starts. The album shows in the Inbox as *Re-downloading*,
   naming its archive, until the new copy lands and it returns to your Library.

Nothing ever deletes the archive — it's yours to keep or bin once the new
download looks right. The format is whichever one you've set in Settings, and the
confirmation says which before you commit.

**The new copy keeps the release you'd already matched.** Re-downloading says the
files are wrong, not the match — so Harmonist tags the replacement as the same
MusicBrainz release the old copy had, rather than looking the album up afresh and
possibly landing somewhere else. The album keeps its history across the round
trip, and normally goes straight back to the Library.

If the album was already incomplete, it's allowed to come back incomplete — you
re-downloaded to *try* for the missing tracks, and if they still aren't there
that's the same album you had, not a new problem. It keeps its tags and its "2 of
4 tracks on disk" badge.

It can still come back needing attention, when the new files genuinely don't fit
that release: more tracks than MusicBrainz lists (the artist added them and
MusicBrainz hasn't caught up), fewer than it lists on an album that was
previously complete (a bad download — Harmonist won't quietly accept that), or
the release has since been deleted from MusicBrainz. Then it waits in the Inbox
under **Needs MBID** with the side-by-side and the usual Confirm and search
tools. Worth knowing before you start: an album that was finished can come back
needing a click. It's also why the archive exists.

You can't start a re-download while a sync is running; wait for it to finish.

## Undoing a tagging

Every tagging is recorded field by field — the value before and the value after,
for each file — so an album's History shows what each one actually changed, with
how far each change reached and a per-track breakdown behind a disclosure. A
re-tag that changed nothing says nothing.

<!-- screenshot: docs/screenshots/history-tagging.png -->

**Undo tag changes** on that entry puts those values back. What it refuses is the
point:

- **The tagging is the unit, not the field.** Reverting one field while its
  neighbours keep the new value would build a state your files never had.
- **A field you've changed since is left alone** — in Picard, or by a later re-tag
  — and named in the outcome. An old row is safe to offer rather than a trap.
- **Every file is read before any is written**, so a failure leaves the album as
  it was.
- **Undoing the tagging that linked an album to MusicBrainz unlinks it too**, so
  your files and Harmonist's own record can't quietly disagree. The album returns
  to Needs MBID with that release kept as a one-click suggestion.

The undo is itself recorded, so it can be undone in turn.

**Cover art has its own Undo.** Artwork that a re-tag overwrites is kept in a
bounded store (500 MB) and can be put back from the Artwork row in History.
Settings shows what's held under **Kept artwork**.

## Activity

Every change, newest first, in plain language — "Tagged", "Unlinked",
"Auto-tagged after sync" — with each album named and linked. Entries that changed
something on disk expand to show exactly what:

```
14:22:07  ●  Boards of Canada — Geogaddi · Tagged     ▸ what changed · 4
                 tag.album  release=… tracks=12 art=embedded mode=full
                 tag.track  file="01 Ready Lets Go.m4a" track=1 …
                 sidecar.update  mbid=None->2f0e…
                 cover.write  file=cover.jpg source=caa overwrote=False
```

<!-- screenshot: docs/screenshots/activity.png -->

**Show details** reveals the raw audit records, each sitting under the entry that
caused it, so one big action doesn't fill the page. The feed pages back through
older history.

What makes the record worth reading rather than just kept:

- **Durable.** SQLite in your config dir, so it survives restarts. Nothing is
  pruned or evicted.
- **Complete.** Tag writes, sidecar rewrites, file moves and overwrites, cover
  art, checkpoint clears, surrenders — and erasing sidecars names every album that
  lost one, because that's the most destructive thing Harmonist can do.
- **Attributable.** Every record is tied to the action that caused it, so the
  detail under an entry is *that* action's work, not everything that happened at
  the same moment.
- **Still readable later.** Entries keep the album's name as it was, and follow an
  album across re-identification.
- **Honest when it can't answer.** If Harmonist can't read its own history it says
  so, rather than showing an empty feed. "Nothing happened" and "I couldn't tell
  you" are different claims, and only one of them is ever a guess.

## Settings

- **Bandcamp sync** — paste or upload the `cookies.txt` that lets the sync log in.
- **Preferences** — download format, max downloads per sync, cover art size,
  MusicBrainz user agent, log level. Saved to `harmonist.toml` and applied right
  away; see [installation.md](installation.md#configuration) for the file itself.
- **Won't download** — the purchases you've set aside, each with **Restore**.
- **Kept artwork** — the images re-tags have overwritten, still recoverable.
- **Maintenance** — erase all `.harmonist.json` sidecars. Audio files aren't
  touched, but every match and sync link is removed; this is the uninstall step,
  not a routine one.
