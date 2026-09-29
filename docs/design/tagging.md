# Tagging

## The owned set

Harmonist promises not to touch tags it doesn't understand. The **owned set** is
that promise made concrete: the fields Harmonist writes, overwrites and
removes, and no others. Everything else on a file is left exactly as found: the
comment carrying a Bandcamp URL, a genre set by another tool, ReplayGain, ratings.

Each format maps the owned set to its own tag names and **clears that mapping
before writing**. So a field the new release doesn't have, such as a catalogue
number, is removed rather than left over from the previous release. When only one
format did this, correcting a wrong match left the wrong label on MP3 and M4A
files indefinitely (#149). Defining the set once, for every format, is what makes
the formats agree.

Fields are split by **scope**: album fields have the same value on every track,
track fields may differ. Some track fields look album-level but aren't: media,
disc subtitle, disc number and track total come from the *disc*, so they differ
between the discs of a set. Scope decides how a change is recorded and shown: an
album-level change is listed once, not once per track.

**Artwork is not in the owned set.** A compilation's per-track covers are user
data a re-tag must not destroy, and clearing artwork with the other owned fields
would erase them. Artwork has its own rules (see [artwork](artwork.md)).

**The comment is the user's.** It carries the Bandcamp URL that matching depends
on, and other tools use it too. Picard puts the release's disambiguation there;
Harmonist shows that on the album page instead.

## Picard compatibility

Harmonist writes the tags Picard writes, under Picard's names, so a library can
move between the two and players read it the same way. A few details matter more
than they look:

- **Release type is multi-valued and lower-case**: the primary type then the
  release group's secondary types (`album`, `live`), as Picard builds it.
  Navidrome has no other source for "this is a live album".
- **Labels and catalogue numbers are multi-valued**, collected separately, since
  a release can name several of each.
- **Compilation is decided by identity, not by name.** The flag is written when
  the release artist is MusicBrainz's *Various Artists* entity, by ID. Matching
  the string "Various Artists" would flag a real band of that name and miss a
  release credited in another language. It is deliberately *not* the release
  group's "compilation" secondary type: a single artist's greatest hits carries
  that, and flagging it makes players split the album into one album per track
  artist, the failure the flag exists to prevent.

### Why Picard parity is audited by hand

The tests that check the tag mappings are built from Harmonist's own constants,
so they catch a missing or extra tag but can never catch a *name*, *case* or
*number of values* that disagrees with Picard. The facts they'd need live in
Picard's source, which isn't a dependency.

So parity is checked by periodically auditing the mappings against Picard's
source, across five things per tag: its name in each format, how many values it
holds, its case, how values are joined, and which part of MusicBrainz's data it
comes from. That audit found release type written as a single value, original
date in the wrong case, and labels cut to the first one. All three were
invisible to the tests, the album page and the update check at once, because a
reader that truncates compares equal to a writer that truncates.

Last audited 2026-09-01, against Picard 3.0.0rc1. Worth repeating after each
major Picard release.

### What isn't written, and why

"Not written" doesn't mean "removed": anything outside the owned set is kept, so
a library tagged by Picard with composers and performers keeps them.

| Picard tags | Decision | Why |
|---|---|---|
| Genre | Deferred (#12) | Every other tag is a fact MusicBrainz states; genre is folksonomy: user-voted, contested, inconsistent between an artist's releases. Picking one isn't an exact match. It's also the tag most likely to be curated by the user, and players keep their own. A genre from elsewhere is shown and never touched. |
| Composer, lyricist, work, performers, producer, engineer and the other relationship credits | Undecided | One more thing to include in the same request, but changing that list re-fetches the whole library ([external services](external-services.md#the-cache)), roughly doubles the owned set, needs ID3 frames no backend writes yet, and relationships change often enough that the update check would report churn. Composer, lyricist and work (the classical and soundtrack case) are worth considering separately from the rest. |
| Comment | Never | The user's; see above. |
| Original album and original artist | Leaning no | Needs a second lookup per release group; the original date already covers what users sort on. |
| Lyrics | Out of scope | MusicBrainz has none. Another source is a question about what Harmonist is. |
| BPM, key | No source | Would need audio analysis, which Harmonist doesn't do. |
| AcoustID, MusicIP fingerprints | Never | Albums are identified by URL, barcode or MBID, never by analysing audio. |
| Disc ID | Never | Comes from a physical CD's table of contents. |
| Album and title sort | Nothing to write | MusicBrainz has sort names for artists only, which are written. |
| Copyright, encoder, original filename | Never | Facts about the file, not the release. |
| Podcast, TV and iTunes-store tags | Never | Not music-library data. |
| Release date | Nothing to write | Picard doesn't fill it from MusicBrainz either. |

## Tags read for provenance

The other half of the split the owned set draws: tags read **only** as evidence
of where an album's files came from (#632). They're never written, never cleared
and never compared with MusicBrainz, and a tag joins the set only when something
reads it. What they decide is whether an album is a download, which
[contributions](contributions.md#which-albums-qualify) depends on.

Only positive evidence counts: a download needs a store's own mark, and a rip
needs AccurateRip's verification. Neither, or both, is no answer.

| Tag | Proves | Why |
|---|---|---|
| `UPC` | Nothing on its own | The barcode a download was sold under, once something else proves it's a download. CD rippers write it too; dBpoweramp does, from its metadata providers. |
| Comment with a Bandcamp URL | Download (Bandcamp) | The first comment, as Bandcamp writes it. Names the release as well as the store. |
| `QBZ:TID` | Download (Qobuz) | Qobuz's per-track ID. It survives XLD's FLAC-to-ALAC transcode. |
| Comment `Amazon.com Song ID: …` | Download (Amazon) | In any comment frame. |
| Comment `Purchased at Beatport.com` | Download (Beatport) | In any comment frame, since Beatport's genre comment comes first. |
| `AccurateRipResult`, `AccurateRipDiscID` | Rip | dBpoweramp's verification of a physical disc. Nothing but a rip produces one. |

Deliberately not evidence:

- **Hi-res audio** proves the files aren't from a CD, not that they were
  downloaded: vinyl, SACD and Blu-ray rips are hi-res too.
- **A CD table of contents** (`iTunes_CDDB_1`) is synthesised by XLD from the
  track lengths on any transcode, so a download can carry one.
- **`MEDIA`** is owned: Picard and Harmonist write it from the release, so it
  describes the match, not the files.
- **Juno's and eMusic's marks** don't name their store clearly enough.

## How significant a change is

Every owned field has a **significance**, which says how much of the album a
change to it calls into question:

- **Cosmetic**: the same value spelled differently (case, spacing,
  punctuation). Never declared for a field; only reached when a change turns out
  to be small.
- **Enrichment**: MusicBrainz filling in or correcting a detail: a date, label,
  catalogue number, ISRC, sort name.
- **Structure**: the layout: track and disc numbers and totals. The layout is
  evidence of which release this is, so a changed tracklist is always reviewed
  track by track before it's applied (#530).
- **Identity**: what the album or a track *is*: titles, artists, release type,
  every MusicBrainz ID.

**Significance is not whether a change needs review.** Those are two questions,
and answering both with one word was an earlier mistake: a slight change can
still want a look, and a far-reaching one can be something a particular user is
happy to have applied. Significance is a property of the change; review is a
policy over it. Today every level needs review, deliberately: the classification
hasn't been watched against a real library yet, and starting closed means an
unattended pass can't write anything. Letting a user trust a level is the planned
next step (#273).

**A field is declared at its highest significance and lowered, never raised.** A
title change is Identity unless it's only a spelling of the same title, in which
case it's Cosmetic. Overstating a change costs the user a glance; understating a
retitle as a spelling fix would, under a trust setting, be a write nobody agreed
to.

**"The same spelling" canonicalises and never strips.** It folds Unicode forms,
every kind of dash, every kind of quotation mark and apostrophe, whitespace and
case, and nothing else, because an adopted library arrives with straight
apostrophes where MusicBrainz has curly ones, full-width brackets in CJK titles,
and decomposed accents. It's a rule over Unicode's own character classes rather
than a list of characters, since a list is never finished and runs out first in
the scripts Harmonist handles least. It never removes characters: `Live?` and
`Live!` stay different titles.

**A credit list appearing is Enrichment; a credit list changing is Identity.**
The multi-artist tags are recent additions to Picard, so most libraries lack
them. Filling one in where the single credit already said the same thing is
MusicBrainz adding detail; names that *change* mean the album is credited to
someone else.

**Identifiers never drop below Identity**, even when the change is probably a
merge on MusicBrainz. A release merge is provable, once, when the fetch redirects
([external services](external-services.md#merges-and-deletions)). An ID that
simply arrives different inside a release gives no such evidence: it could be a
merge, a re-pointing, or a track replaced outright.

**Artwork isn't on this scale.** It says nothing about a picture. An image write
is described by what it does instead (see [artwork](artwork.md)).

## Accepted spellings

Some fields have more than one correct value for the same release, because Picard
can be configured to write them differently. Counting those as updates would put
a Picard-tagged library in the Inbox on the update check's first night (#283).
Each accepted spelling is derived exactly from the release in hand, never from a
pattern, which would be a guess.

- **Album title with the release's disambiguation**, as Picard's option writes it:
  *Selected Ambient Works Volume II (expanded edition)*. Only that exact string is
  accepted. A looser rule accepting any bracketed suffix would guess at an
  identity the release states outright.
- **Any country the release was issued in.** MusicBrainz reduces a release's
  events to one country; Picard lets the user prefer another of the release's own
  countries. A country the release doesn't list is still reported.

The tolerance applies where Harmonist decides whether there's **an update to
take**, never in the record of what a tagging changes. That record is also what
undo is built from, and a write replaces the whole tag set regardless. Filtering
an accepted spelling out of it once made a re-tag replace the user's album title
and leave no way back (#545). A change that isn't a reason to tag is still
written, and still recorded.

The album page draws the two differently, on purpose. A disambiguated title is
shown as a difference, because a setting can resolve it (below). A second
release country isn't, because nothing can: there's no setting for it, so showing
it would leave a difference the user can only silence by giving in.

### Transforms

A **transform** is a named, user-enabled choice of which accepted spelling a
tagging *writes*: the exactly derivable equivalent of a Picard option or tagger
script. One exists: adding the disambiguation to the album title.

The invariant that makes transforms cheap: **a transform only chooses among
spellings that are accepted unconditionally.** The accepted set never depends on
which transforms are enabled. So:

- turning one on doesn't flag the library, since the other spelling is still
  accepted;
- the unattended update check reads no settings and still reaches the same
  verdict as the write;
- a transform is a function of the release, never of what's on the file, so
  applying it twice is applying it once.

The visible effect is therefore bounded: new albums get the chosen spelling, an
album re-tagged for another reason gains it, and nothing else is rewritten.

The accepted spellings and the write that chooses between them are kept together,
because the album page and the tagger computing "the same album" separately has
already caused one library-wide false alarm (#283). Only choices that can be
derived exactly from the release will ever become transforms (#284).

## Writing only what changed

A tagging first reads each file's owned tags and works out what would change. A
file with nothing to change isn't written at all, and nothing is recorded for it.

That matters beyond saving work. An untouched file keeps its modification time,
and a file newer than the sidecar's last tagging is how Harmonist notices that
someone re-tagged it in Picard. Rewriting unchanged files would make every
tagging look like an outside edit.

The one thing a per-field comparison can't see is an old spelling of an owned
tag that a write would remove, such as an MBID under a name Picard used to use. A
file carrying one is written even when nothing else changed. A derived tag, like
original year beside original date, isn't treated as leftover: a write produces
it again, so its presence is normal.

**Absent is one state.** No value, an empty string and an empty list all mean
the tag isn't there, because Harmonist never writes an empty tag; a field with no
value is removed.
