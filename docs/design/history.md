# History and undo

[Transparency](principles.md#transparency-over-perfect-automation) depends on
two things: a record of everything Harmonist did, and the ability to reverse it.
The records are shaped for reversal first and for reading second.

## Two records, one store

- **Activity** is for the user: one plain-language entry per outcome ("Matched",
  "Unlinked"), shown in the Activity tab and on album pages. A suggestion is not
  an outcome: finding one is only flashed, and confirming it is the entry, naming
  the release and how it was found (#639). Nor is ticking a box such as **Don't
  warn me about this** or **Better artwork wanted**: the box already shows where
  the setting stands, so the toggle is neither entered nor flashed, and only the
  audit log has it (#911).
- **The audit log** is for finding out exactly what happened: structured
  `event key=value` lines, as many as an operation takes, for every download,
  file change, sidecar change, demotion and cleared checkpoint.

Both are appended to the same table in `activity.db`, marked by kind, so a
single query returns everything about an album. The audit log also goes to the
server log under its own logger name, so it can be filtered with grep.

**Warnings reach the user without anyone deciding to tell them.** Anything logged
at warning level or above is mirrored into the Activity feed, because on an
unattended NAS the log is otherwise unread. The consequence is that a warning must
be news the user should see. Measurements, like "this operation was slow", are
kept out of the feed, and a condition that repeats on a timer is reported once,
not on every tick.

**Everything one action causes is grouped under one action ID**, so an entry in
the feed can show exactly the audit lines beneath it. The grouping is per
*outcome*, not per run: a sync that downloads ten albums is ten actions, so each
can be understood and undone on its own.

**Records name the album as it was.** An entry keeps the album's name from the
time it was written, and follows the album through re-identification by way of
the recorded aliases (see [the model](model.md#identity)).

The store is append-only, and nothing prunes it. Text is cheap; the images that
aren't are kept elsewhere, under a size limit
([artwork](artwork.md#a-backup-is-what-permits-a-replacement)).

## What a tagging records

Every tagging records each field's value before and after, **per file**. Per
file, because the "before" values need not match across an album's tracks: an
album tagged unevenly over the years has different values on different tracks,
and a single album-level "before" would have to pick one and then write that
guess over the tracks it didn't come from.

**The record is complete, not filtered.** Harmonist only writes fields it owns,
so "everything written" is already bounded, and deciding what's interesting is
the display's job, not the record's.

**Each file's record names its track four ways**: file name, release-track ID,
recording ID and disc-and-track position. Each fails under a different later
change (a rename, a re-match, a renumbering) and no two fail together. Which file
carried which identity can only be observed at the moment of writing, and the
table is append-only, so a record can't gain a better identifier later.

**Images are recorded as digests**, which double as the keys of kept artwork.

**A tagging that changed nothing records only that it ran.** "Found the files
already correct" is a different fact from "never ran", but it needs one line,
not one per file.

**The history is shown by field, not by file**: one row per field that changed,
saying how far it reached ("all tracks", "track 9", "4 of 18 tracks"). Nobody
asks what happened to track 9; they ask what changed, and a per-file listing of
an eighteen-track album buries the answer in near-identical rows.

**Where tracks disagree, no one value is the album's**, so the row lists each
track's own before and after beneath the field instead of passing one track's
off as the album's. Picking the commonest pair, or counting the distinct ones,
were both tried: the first is a guess dressed as a fact, and the second (a count
beside a count of tracks) said nothing about the change. The lines are capped
per field, with the rest folded on that row, so a release switch that rewrites
every recording ID still costs a few lines per field and the list's height stays
bounded by its fields.

The rejected alternative is grouping those changes by track, one entry per track
listing the fields that moved. It tells some updates' story better (a track
moved to a remix recording changes title, ISRC and recording together) but its
height grows with the tracklist on a release switch, and it would set a second
layout beside the field rows on every list.

The records use the owned tags' names, which makes those names a permanent
vocabulary: a record written today may name a field a later version has renamed.
The display falls back to the raw name rather than failing.

## Undoing a tagging

**The tagging is the unit, not the field.** Undo puts back everything one tagging
changed. Reverting one field while its neighbours keep their new values (the
artist but not its sort name or ID) would build a state the files never had, and
one that looks out of date forever.

**A field changed since is left alone.** A field goes back only if the file still
carries what that tagging wrote. Anything edited since, by a later re-tag or in
Picard, is kept and named in the result. That's what makes offering Undo on an
old entry safe rather than a trap.

**Everything is read before anything is written.** A missing or unreadable file
refuses the undo, rather than leaving the album half reverted.

**Renamed files are still found**, by release-track ID, then recording ID and
position, then position alone, always within the album. Ambiguity refuses the
undo rather than weakening the evidence: putting one disc's tags on the other
disc's file would be a lie about the user's tags that reports success.

**The release ID moves on every file or none**, and the sidecar follows it.
Reverting it on some files would leave the album disagreeing with itself. When
the undo takes the album off its release, the album goes back to Needs MBID with
that release offered as a suggestion, so confirming it again is one click. The
"wrong match" pencil makes the same move but offers no suggestion, since the
user has just called that release wrong.

**The pencil has an Undo of its own** (#639), and it needs nothing stored: the
pencil leaves the tags alone, so the files still name the release it cleared.
The Undo links the album back to that release, as adoption links an album from
its own tags. It is offered only while the album has no release and every file
names the same one, since a file changed since leaves no single release to go
back to.

**An undo is itself recorded**, and can be undone. An undo with nothing left to
change records nothing, like a re-tag that changed nothing.

## Undoing an artwork change

Artwork has its own Undo, separate from tags, because it has its own store and
its own availability. One button per store, each honest about what it can do.

- A **replacement** is undone by putting the kept image back. If the backup has
  gone, no Undo is offered.
- An **addition** is undone by removing the image, or deleting a folder cover
  Harmonist created, so it needs nothing from the store.
- A target that no longer holds what the change wrote has been changed since,
  and is left alone and named.
- The undo keeps a copy of anything it overwrites, and refuses entirely if it
  can't: an undo is itself a destructive write.

Undoing artwork never rewrites tags, and undoing tags never touches artwork.

What to undo is worked out from the album's own records, by the history entry the
user clicked. A request can name an entry, but never a file or an image.

## What an artist NFO write records

A write into an `artist.nfo` (#981) is recorded the way a tagging is: each
mapped field's value before and after, and no copy of the file. Whole-file
backups were proposed (#917) and turned down. A whole-file restore can only work
while nothing else has touched the file, and Kodi and Jellyfin rewrite NFOs as a
matter of course, so the first rescan by a media server would end the undo for
good. Field values survive that. The price is that a write the writer gets wrong
has no backup to fall back on, which is why the writer checks its own work (see
[tagging](tagging.md#artist-nfos)).

**History follows a write, not an alias.** An album's ID is the album's alone,
so an old ID can be aliased to its new one. An artist's MBID belongs to the
artist, not to one file. Identifying a file takes it off its path-hash page and
onto the artist's, but undoing that can't alias the MBID back to the path hash,
or every link to the artist would lead to one file's page. So each write records
the page key it moved the file away from, beside the one it's under afterwards.
An artist's History is the writes that brought a file to its key or took one
away, so both pages show an identification and its undo. An old link to a
path-hash page leads, through the newest write away from it, to wherever the
file went.

## Undoing an NFO write

The same rules as a tagging's undo, for the same reasons. **The write is the
unit**: Undo puts back everything one write changed. **A field changed since is
left alone** and named in the result, so an Undo offered on an old entry is safe
after a media server or a person has edited the file. Undoing an identification
removes the MBID, and the file is Not identified again. An undo is itself a
write, recorded the same way, so it can be undone; one with nothing left to put
back records nothing. Undo isn't offered once the file has gone, or when it
can't be written safely, since a button that would only fail is worse than none.

As for albums, a request names a History entry and never a file: the file and
the values are worked out from the stored record, and only from an entry in
that artist page's own History.
