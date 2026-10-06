# Staying current

Two things change underneath Harmonist: the files, edited by other tools or by
hand, and MusicBrainz, edited by its community. Each needs noticing without the
user having to ask.

## Noticing changes on disk

Three mechanisms, in increasing order of how little they assume. All of them
request the same scan, so any number of triggers between two scans still
produces one.

**The file watcher** hears about changes to the music folder and rescans once it
has been quiet for a few seconds, so copying an album in produces one scan
rather than one per file. It only works on a local filesystem: a library the
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
release *skips* an album, and never *clears* its flag.

**Nothing computed here is stored.** The flag and its significance are rebuilt
from the cache and the files whenever needed. **Only new updates are
announced.** One the user hasn't taken yet isn't announced again when the
release changes further, and one Harmonist is seeing for the first time (on a
new install, or after the cache was refilled) isn't announced at all. Nor is
one that only brings an album into line with the user's settings: they chose it
on the Settings page, so it's never news. For the same reason, a waiting
Settings change doesn't count as an update already announced, or the album would
go quiet about real edits for as long as it waited.

### The background update check

**Off by default.** It writes nothing to files, so the default isn't protecting
the library; it's protecting MusicBrainz, a volunteer service, from load an
upgrade started without anyone deciding to.

**It only reports.** Every significance level currently needs review (see
[tagging](tagging.md#how-significant-a-change-is)), so the one thing that can be
said about a background job running while nobody watches is that it can't damage
the library. Letting a user trust some levels to apply themselves is the planned
next step (#273).

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

Ignoring writes nothing to files, and the album keeps its Update badge, because
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
