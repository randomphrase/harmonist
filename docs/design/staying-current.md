# Staying current

Two things change underneath Harmonist: the files, edited by other tools or by
hand, and MusicBrainz, edited by its community. Each needs noticing without the
user having to ask.

## Noticing changes on disk

Three mechanisms, in increasing order of how little they assume. Changes to
known album files need only those folders re-read; discovering a changed folder
layout still needs a walk of the library.

**The file watcher** hears about changes to the music folder and refreshes once it
has been quiet for a few seconds, so copying an album in produces one scan
rather than one per file. It retains the changed paths: files in known album
folders refresh those albums, while new, removed or uncertain directory changes
request a full scan. A tagging already refreshes its own folders, counts and
indexes, so its delayed file events find the same signatures and read no tags.
No folder is muted for a time window: an external edit immediately after a
tagging must still be noticed. A full scan already in flight preserves albums
refreshed since it began, so it cannot put back the pre-tagging reading.

The watcher only works on a local filesystem: a library the
container mounts over NFS or SMB produces no events, and a very large tree can
exhaust the system's watch limit and stop the watcher entirely. Both failures are
silent.

**The hourly rescan** is the backstop for exactly those failures. It's cheap
enough to need no setting: an unchanged library costs one `stat` per file and no
tag reads, because the scan remembers each folder by its files' modification
times. So the interval is a constant, since a setting would only invite tuning
something nobody should have to think about. It's also invisible: a scan the user
started shows progress and locks the Inbox against clicks landing on a list being
rebuilt, but a timed one that did that would just freeze the Inbox under the
user's cursor. It never runs during a sync or reconcile, because reading a
half-downloaded album would record it as a new one; those finish with a scan of
their own anyway. And it only refreshes the page when the result actually
differs.

**The album page re-reads its own album** before drawing it. That's where the
user decides whether to re-tag, and deciding against a stale reading is the
failure that costs something. It's one directory listing and a `stat` per file,
affordable on every page view where a whole-library scan isn't. A new folder
joining the album is still the rescan's job.

**Its tags are read again only for files that have changed.** A file's tags are
remembered alongside its `stat`, and a file whose `stat` is unchanged is served
from memory. Reading them on every view was the slow part of the page, not
MusicBrainz: mutagen reads the whole tag block, embedded artwork included, and
an album carrying large images took seconds on a NAS to report tags nothing had
touched (#904). The test is stricter than the scan's: it adds the inode change
time, because an editor can put back the modification time it changed and
nothing can put back the inode change time. Harmonist's own writes don't depend
on the test at all, since a filesystem with coarse timestamps can't tell a read
from a write in the same tick; every write forgets that file's reading.

None of these can say what changed. A scan derives state from the current files
and keeps no record of the previous ones. What Harmonist can notice is that files
were written after its last tagging, which means another tool re-tagged them (see
[the model](model.md#when-the-files-and-sidecar-disagree)).

## Noticing changes on MusicBrainz

An album has an **update available** when re-tagging it against the release
MusicBrainz holds now would change any owned tag. Working that out needs a
current copy of the release, so there are three ways to get one: opening the
album's page; the cached releases, re-checked after a restart without any
requests; and the **background update check**, which fetches releases on a timer
and is the only way to find an update nobody went looking for.

### What decides "update available"

It's a dry run of the actual tagging, over exactly the owned fields. The album
page's comparison isn't used, because it's shaped for display: it shows fields
it never compares, and calls things differences that a re-tag can't write.
Using the dry run means the flag, the history record and Undo all speak the same
vocabulary.

The dry run applies the user's tagging settings as they stand when it runs,
because the write would. So "update available" also covers an album that
doesn't yet follow a setting (see [transforms](tagging.md#transforms)), and
changing a setting re-judges the albums it can move. That re-check reads the
stored releases, like the restart warm-up, and makes no requests.

**It compares the files with MusicBrainz, never MusicBrainz with an earlier
MusicBrainz.** Comparing a fresh copy of a release with the cached one is
tempting and cheap, and wrong both ways. A release that hasn't changed still has
an outstanding update if the files never took the last one. A release edited and
then reverted has nothing outstanding, but would be flagged until someone cleared
it by hand. The dry run gets both right, since it's empty exactly when the files
already say what MusicBrainz says.

The release comparison still earns its keep as a shortcut: if a freshly fetched
release is identical to the cached one, the files needn't be read. An unchanged
release *skips* an album, and never *clears* its flag. The skip holds because
the other input is covered elsewhere: a rescan that rebuilds an album because
its files changed re-judges it from the stored release, again with no requests,
so the background check never has to notice an edit to the files.

**Nothing computed here is stored.** The flag and its significance are rebuilt
from the cache and the files whenever needed. **Only new updates are
announced.** One the user hasn't taken yet isn't announced again when the
release changes further, and one Harmonist is seeing for the first time (on a
new install, or after the cache was refilled) isn't announced at all. Nor is
one that changing a setting brought: the user caused it on the Settings page,
so it's never news. A Settings update that a MusicBrainz edit brought, such as
a disambiguation arriving under the setting that writes it, is news like any
other, though announced only to a user who asks for that level (see
[what gets recorded](#what-gets-recorded)). A waiting Settings change doesn't
count as an update already announced, or the album would go quiet about real
edits for as long as it waited.

### The background update check

**Off by default.** It writes nothing to files, so the default isn't protecting
the library; it's protecting MusicBrainz, a volunteer service, from load an
upgrade started without anyone deciding to.

**It only reports.** Finding updates and writing them are separate jobs, so the
check itself can't damage the library whatever it finds. Writing is the apply
pass's (below), and only for the levels the user has trusted.

### Applying without asking

**Two thresholds on one scale.** Apply takes the chosen level and those below
it, Inbox the chosen level and those above, so neither can treat a level more
cautiously than one further along. Applying wins where they overlap: reviewing
an update that is about to be written would be asking twice.

**Never Structure.** A changed tracklist can need its track assignments
reviewed, which has no unattended answer, and can call the match itself into
question.

**Written from the stored release.** The album was judged against the cached
payload, so that is what gets written, as a reviewed confirmation writes what
its review showed. Fetching afresh could write a release nobody had judged, and
would spend a MusicBrainz request per album. Each album is re-judged at write
time, and one whose update now reaches further than the user trusts is held
rather than written.

**Tags, and artwork additions.** The tag write leaves artwork alone. Artwork has
a setting of its own, and only ever adds: a replacement needs a backup before it
overwrites, and nobody asked for this write (see
[artwork](artwork.md#applying-artwork-without-asking)).

**Its own tick, independent of the update check.** The check's switch guards
the MusicBrainz budget, and applying spends none of it, so an update found by
an album page or a Settings re-check is applied all the same. Never by the page
visit itself: an album changing because someone looked at it would be
surprising, and its page already offers the update.

**What can't be written waits in the Inbox, with the reason.** An album missing
tracks (whose re-tag is a decision about that album), one needing track
assignments reviewed, one whose write failed. The hold is kept in memory against
the MusicBrainz version it was held at, like the flag it qualifies, so a failure
isn't retried every ten minutes, and is retried once MusicBrainz moves or
Harmonist restarts.

**One action per album.** Each write opens its own action scope, so its History
entry carries its own tag changes and its own Undo.

### Applying a selection

Updates waiting in the Inbox can be selected and applied together. It is the
same write as the unattended one, for the same reasons, with three
differences that follow from someone having looked.

**The selection is a review, so it is checked like one.** Each card was drawn
against one version of the stored release, and that version travels with the
selection. An update whose release has moved since is left alone, as a
reviewed confirmation refuses a release that changed under its review. The
apply threshold doesn't apply: the user chose these.

**Never Structure, still.** Selecting a card shows its field changes, but a
changed tracklist can need its assignments reviewed, which the album page does
and a list can't. Structure updates aren't selectable.

**A failure is reported, not held.** The unattended pass holds a failure so it
isn't retried every ten minutes. Here nothing retries: the reason goes to
Activity, and the card stays, unticked, so it is looked at again before it is
selected again.

**One pass at a time**, shared with the unattended pass, so no album is written
twice. A selection made while a pass runs is refused with a reason rather than
queued, since the Inbox it was made from may not survive that pass. The pass is
claimed before the request returns, so the Inbox the response redraws already
shows each selected card waiting its turn.

### Where an update waits

**A threshold, not a choice per level.** The user picks the lowest level that
waits in the Inbox; that level and everything above it do, and the rest are
found through the Library's update filter. Five independent choices could send
an identity change to the Library while its album's enrichment waited in the
Inbox. The verdict is the album's furthest-reaching change, so that album would
then never be reviewed at all.

**Structure alone by default.** A changed tracklist can call the match itself
into question, so it is the one level that cannot sensibly wait to be looked
for. Everything else is a fact the Library already shows on demand.

**Library albums only.** An album still in an Inbox state is listed under that
state, which is the next thing to do about it, and listing it twice would
suggest two separate jobs.

**The setting applies immediately, so the timer always runs.** It checks the
current setting on each tick. A timer created only when the check was enabled at
startup would leave the setting saved, looking applied, and doing nothing until
a restart.

**The schedule is read from the cache.** Each cached release records when it was
fetched, so the check needs no queue of its own: the longest-unchecked albums go
first, and anything checked in the last week is skipped. A merged or deleted
release leaves no fresh row under the ID it was asked for, so the check also
remembers what it has asked this session, or it would ask about those every time.

**The rate is derived from a goal, not chosen** (#349). A full pass over
everything due should take about a day, so each ten-minute tick takes that
fraction of the queue: usually two or three albums. A first run with the whole
library due takes proportionally more per tick and still finishes in a day, and
the steady state settles at the library's size divided by a week, which is the
real demand. Two consequences aren't obvious:

- **The day must be much shorter than the week.** Albums come due at
  (library ÷ week) per day and leave at (queue ÷ day), so a day-long pass keeps
  about a seventh of the library waiting, while a week-long one would leave the
  whole library permanently overdue.
- **Small slices stop bursts repeating.** Albums checked together come due
  together a week later. A fixed hundred-album batch stamped that shape on the
  library and replayed it weekly; a slice of two or three leaves nothing to
  repeat. There's no random jitter, because it isn't needed and would be one more
  thing to explain.

**It stands aside** for a sync, a reconcile, a library not yet scanned, or its own
previous pass still running. Anything the user started is waiting on the same
request budget, and albums that have waited a week can wait ten more minutes.

**It gives up when MusicBrainz keeps failing**, rather than spending a request per
album to learn the same thing. The run of failures is counted across ticks: with
only a few albums per tick, a count that reset every tick could never reach the
limit. Once reached, each tick stops at its first failure until something
succeeds. A 404 isn't a failure, since it's an answer; otherwise a library with a
few deleted releases would end every pass early.

### What gets recorded

**Activity has a threshold of its own** (#1024). The Inbox answers "does this
need me?"; Activity answers "should this be on the record?" They're two
questions, and answering both with the Inbox threshold was the mistake
[significance and review](tagging.md#how-significant-a-change-is) had already
made once. It treated Activity as a notification the user had declined by
leaving an update in the Library. But Activity is a record, read when the user
chooses, and the Inbox is the notification. An update left in the Library also
has no other record of arriving: the flag is derived and never stored, so once
the update is taken, nothing says MusicBrainz ever changed the album.

**Every level from MusicBrainz by default.** Only transitions are recorded (see
[above](#what-decides-update-available)), and real edits across a library are
rare, so the feed stays quiet without being filtered. A burst, such as an artist
renamed across a discography, arrives a few albums per tick over the week's
schedule, and a pass that finds more than a handful still records one counted
line. The threshold is there for a library where that judgement is wrong.
Settings is the one level off by default: what it records is a spelling the
user chose, arriving.

**A threshold, like the Inbox's**, for the same reason: no level is treated as
less worth recording than one below it.

**Only a fresh answer from MusicBrainz can record anything**, wherever it was
asked for. The album page's refresh is a live check like the background one,
and an update it turns up is recorded by the same rules; leaving it out would
lose the update for good, because by the time the background check reached
the album it would no longer be new. An answer served from the cache, at
startup, after a rescan or on a page view, says nothing new about MusicBrainz,
and a payload identical to the stored one says nothing either, so opening an
album records nothing however often it's opened. Two checks fetching the same
payload at once record it once: whichever judges the album second finds it
already judged against that payload.

**Not what's about to be applied.** The write records itself, with its changes
and its Undo, within minutes; an entry for the update as well would record the
same change twice. An applied level's tick on the Settings page is therefore
fixed: the write is always recorded, so the box can't say otherwise.

### Ignoring an update

A user who thinks an update is wrong should correct the release on MusicBrainz.
While that edit waits for approval, **Ignore until MusicBrainz changes** takes the
album off the list.

The ignore is a **bookmark, not a rejection.** A stored rejection of particular
changes can't be keyed. Keyed to the release's version, any unrelated edit
brings the rejected change back; keyed to the fields, it becomes a permanent
per-album override of MusicBrainz, which the project rules out
([principles](principles.md#musicbrainz-is-canonical)). An ignore instead records
the release version the user is waiting to see change, and any change lapses it.
That's the point: the change may well be their own edit landing, and Harmonist
can't tell whose it was.

Ignoring writes nothing to files, and the album keeps its update badge, because
the difference is still real. Re-tagging clears the ignore, since there's then
nothing to wait for. It's stored in `activity.db` rather than the sidecar (see
[storage](storage.md#activitydb)).

**Ignore is about MusicBrainz**, so it isn't offered for an update that only
brings an album into line with the user's settings: nothing upstream is in
question, and the remedy is the setting. An ignore that still matches the
release never hides one either, since the album page would then offer no box to
take it back. An album ignored for a MusicBrainz change is muted whole, its
Settings change with it, which keeps a single bookmark keyed only on the release
and so nothing to migrate.
