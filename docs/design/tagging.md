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

**Audio format is read-only file evidence.** It follows the track view's scope:
one value when the files agree, individual readings when they differ, so the
reader can locate a different encode beside its track. It has no MusicBrainz
counterpart and never counts as a tag change. Ordinary variable-bitrate
variation is part of one encode, not evidence of inconsistent formats (#720).

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
| Genre | Deferred (#12) | Every other tag is a fact MusicBrainz states; genre is folksonomy: user-voted, contested, inconsistent between an artist's releases. Picking one isn't an exact match. It's also the tag most likely to be curated by the user, and players keep their own. A genre from elsewhere is never touched, and isn't shown on the album page: there is nothing to compare it with, so all it could report is the files disagreeing among themselves (#224). |
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

Automatic detection needs positive evidence: a store's own mark for a download,
or AccurateRip's verification for a rip. Neither, or both, is no answer. Marks
can survive copying metadata between files, so the owner's explicit choice can
overrule them without erasing the evidence.

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

### The origin

All of that evidence comes to one conclusion: the album's **origin**, the single
place it points to (Bandcamp, Qobuz, Amazon, Beatport or CD), or **Unknown** when
it points to none or to more than one (#634). Two stores' marks are as
inconclusive as a store's mark beside a rip's. Harmonist's own Bandcamp download
settles automatic detection.

The origin names the files, not the purchase. A CD bought from Bandcamp and then
ripped is a CD. Inherited purchase comments cannot disprove the owner's knowledge
of that rip: replacing an iTunes library entry's audio can retain the old
download's metadata (#694).

An explicit choice overrides both tags and Harmonist's download record, since
files can be replaced outside Harmonist. It survives retagging, rematching and
scanning; a new Re-download starts with the replacement's provenance. Unknown
is a real choice, distinct from returning to detection. The choice is recorded
in every folder of the album; conflicting choices on separately adopted parts
derive Unknown until resolved. The tags stay visible, and a user choice is never
described as verification of the audio.

It's one conclusion shared by its readers. The album page shows it, and
[contributions](contributions.md#which-albums-qualify) treats an album as a
download exactly when its origin is a store. Barcode discovery also uses it to
[narrow Qobuz suggestions](matching.md#barcode-search). So the page can't name
an origin the checks disagree with.

### Origin and additional tags

The album page groups origin with the album's identity. These tags and the
comment appear by their literal names in a section of their own. They answer
"where did these files come from", not "does this match MusicBrainz", so they don't belong in the Album
section's comparison. The comment was shown there, never compared, until #634
moved it.

A tag's value follows the Album section's consensus: the value most tracks
carry, with the tracks that disagree one click away. The exception is a tag
where every track carries its own value. There's no majority to show, and
calling track 1's value the answer and the rest outliers would read as a
problem, so the row says the values differ on every track and lists each one.
`QBZ:TID` and AccurateRip's per-track CRCs always look like that, but Harmonist
doesn't need to know which tags are meant to vary.

The section shows only tags Harmonist reads for analysis, which keeps it from
becoming a tag dump. An album with none of them, and nothing else settling its
origin, has no section at all.

## How significant a change is

Every owned field has a **significance**, which says how much of the album a
change to it calls into question:

- **Settings**: a value moving from one spelling the release legitimately has
  to the one the user's [spelling settings](#transforms) select. The lowest level,
  because it's the only one whose content the user chose: whoever trusts
  MusicBrainz's casing fixes trusts the changes they asked for, and not
  necessarily the reverse. Never declared for a field; only reached when the
  release proves the value on disk is one of its own spellings.
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
can be configured to write them differently. Each accepted spelling is derived
exactly from the release in hand, never from a pattern, which would be a guess.

- **Album title with the release's disambiguation**, as Picard's option writes it:
  *Selected Ambient Works Volume II (expanded edition)*. Only that exact string is
  accepted. A looser rule accepting any bracketed suffix would guess at an
  identity the release states outright.
- **Artist names as credited or standardized** (see [artist names](#artist-names)):
  *Florence and the Machine* or *Florence + the Machine*, in the display credit, the sort credit and
  the artist lists.
- **Any country the release was issued in.** MusicBrainz reduces a release's
  events to one country; Picard lets the user prefer another of the release's own
  countries. A country the release doesn't list is still reported.

They're treated differently, because only some have a setting. The title and
artist spellings a library carries are the user's [settings](#transforms) to
choose, so a supported spelling other than the chosen one is an update of the
Settings level. A **supported spelling** is exactly a value some combination of
the settings writes for this release: Harmonist runs the write under each and
collects the results, rather than describing them by rule. That keeps the set to
the release's own strings and in step with the write by construction, and it
means a value mixing two settings (one artist standardized, another not) is no
setting's spelling and keeps its ordinary significance. No setting chooses a
country, so a second one is tolerated: it isn't an update, and the album page
doesn't show it as a difference, because a difference the user can only silence
by giving in isn't worth showing. Counting it would put a Picard-tagged library
in the Inbox on the update check's first night (#283, #346).

Either way, accepted spellings matter only where Harmonist decides whether
there's **an update to take** and how significant it is, never in the record of
what a tagging changes. That record is also what undo is built from, and a write
replaces the whole tag set regardless. Filtering an accepted spelling out of it
once made a re-tag replace the user's album title and leave no way back (#545). A
change that isn't a reason to tag is still written, and still recorded.

### Transforms

A **transform** is a named, user-enabled choice of which accepted spelling a
tagging *writes*: the exactly derivable equivalent of a Picard option or tagger
script. One exists: adding the disambiguation to the album title.

**A transform only chooses among spellings the release has.** The accepted set
never depends on which transforms are enabled; the setting picks a member of it.
That's what lets a change be classified Settings at all: the release proves the
value on disk was one of its own spellings, and the new value is the one the user
chose. A value that isn't in the set, such as a MusicBrainz retitle arriving at
the same time, keeps its ordinary significance. A transform is also a function of
the release, never of what's on the file, so applying it twice is applying it
once.

**The library converges on the setting**, existing albums included, whichever
way the setting points (#685). The update check plans exactly what a re-tag would
write, under every spelling setting, so an album carrying another supported
spelling has an update and one carrying the chosen spelling has none. Changing a
setting re-judges the albums whose stored release would be written differently
under the new settings, without reading any files to decide or making any
requests.

That replaced "transforms affect future taggings only", which kept the update
check free of settings and turning a transform on free of consequences, but left
a library permanently split between conventions, with no way to bring the albums
on disk into line short of re-tagging each for some other reason. Convergence is
what a user setting a convention wants; the costs are contained by the level
rather than by hiding the change:

- **Off is a choice like any other.** A tolerant "leave existing titles alone"
  option was considered and not taken: a user who turns disambiguations off wants
  them off everywhere. Turning one off offers updates that remove data from
  titles, so the Settings page warns before it's saved that the updates will be
  pending.
- **It's a flood the user caused**, so it isn't news. An album with only a
  Settings change is never announced. The same goes for one an upgrade produces.
- **Ignore doesn't apply.** Ignore waits for MusicBrainz to change, and nothing
  about MusicBrainz is in question; the remedy is the setting. An album with
  only a Settings change offers no Ignore, and an album ignored for a
  MusicBrainz change keeps its Settings change waiting until the ignore lapses.
  Per-album exceptions to a setting are a separate question.

The accepted spellings and the write that chooses between them are kept together,
because the album page and the tagger computing "the same album" separately has
already caused one library-wide false alarm (#283). Only choices that can be
derived exactly from the release will ever become transforms (#284).

### Artist names

Picard 3.0's **Standardize artist names** (#678) is copied as Picard has it:
the same three choices, the same multi-value checkbox, under Picard's own option
names and values so a user can match the two tools by name. A credit names an
artist twice: the name printed on this release (*Florence and the Machine* on
*Dog Days Are Over*) and the artist's own (*Florence + the Machine*). "Do not
standardize" writes the first; "variations and name changes" writes the second;
"variations only" writes the second unless the credit is a **former name**, a
name the artist used to go by (*Mos Def*, now *Yasiin Bey*). What counts as a
former name is MusicBrainz's alias typing, not how famous the change was:
Prince's releases as *The Artist (Formerly Known as Prince)* use a name
MusicBrainz records as a search hint, so both standardizing choices write
*Prince*, as Picard does. "Always standardize
multi-valued artist tags" applies the artist's own name to the lists players
group by, whatever the display credit says. A kept name sorts under its own
alias's sort name where it is one, as in Picard; Harmonist used to sort it under
the artist, which is what "name changes" writes, so that change is a Settings one
too.

**The default is "variations only"**, Picard 3.0's own. That's a change for
existing Harmonist libraries, which were written as credited: albums with a
credited variation get a Settings update after upgrading. Matching where Picard
is going was preferred to matching where existing Picard libraries came from;
older Picard installs defaulted to not standardizing, and a user with that
library can choose it.

**A former name is an ended alias, and the evidence comes with the release.**
Telling a former name from a variation needs the artist's aliases, so they're
included in the release request rather than fetched per artist: the request
budget is per request, and an alias edit then moves the release's version like
any other edit, so it reaches the update check and lapses an ignore. Adding them
orphaned the release cache once, refilled as albums are looked up again.

Harmonist reads MusicBrainz as XML, and the XML has no "ended" flag for an
alias, only dates. So an alias counts as ended when it has an end date. One
marked ended without a date reads as current, and the credit is standardized
where Picard, reading the JSON, keeps it. That divergence is visible as a
Settings update, and dating the alias on MusicBrainz removes it; reading
MusicBrainz as JSON would remove it everywhere (#690). Fetching aliases from
the JSON service separately was considered and declined: a second request per
artist, for a gap MusicBrainz's own data can close.

No warning accompanies the choice, unlike turning the disambiguation off.
Standardizing changes how a credit is displayed, not what the files know: the
artist's identity stays in the IDs either way.

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
