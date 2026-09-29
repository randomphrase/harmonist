# Rejected directions

Things Harmonist deliberately doesn't do, and why. Each is a decision, not an
oversight, and reversing one is a design change.

**A general-purpose tagger.** Editing arbitrary tags, fixing one file by hand,
scripting what gets written: that's Picard's job, and Harmonist is built to
coexist with it rather than replace it. Harmonist's writes are limited to what
MusicBrainz states (see [tagging](tagging.md)).

**Per-album exceptions to MusicBrainz.** There's no way to say "keep my title for
this album". The correction belongs on MusicBrainz, and an exception can't be
keyed stably anyway (see [principles](principles.md#musicbrainz-is-canonical)).

**Moving, renaming or splitting folders.** Folder layout is the user's. A folder
holding two albums is reported, not split, and a case-only name collision is
logged, not fixed. Harmonist tolerates reorganised folders instead (see
[the model](model.md#an-album-is-its-release-not-its-folder)).

**Converting formats.** Files stay in the format they arrived in. Re-download
fetches a new copy in a new format; it never transcodes the old one.

**A database of the library.** The library's state is its files and their
sidecars. `activity.db` holds Harmonist's own history and caches, never the
authoritative state of an album (see [storage](storage.md)).

**Adding releases to MusicBrainz from inside Harmonist.** Harmony already does
this well, from a store URL or a barcode. Harmonist links to it, then searches
again.

**Identifying albums by their audio.** No AcoustID or other fingerprinting:
albums are identified by store URL, barcode or MBID. Fingerprinting would be a
guess dressed as an identity, and a large new dependency.

**Scraping store pages.** No fetching a Bandcamp artist page to find an album on
it, and no constructing URLs from titles. The evidence is what the files, the
purchase and MusicBrainz say.

**First-class support for other stores.** Any store Harmony understands can be an
album's store URL. Downloading purchases is Bandcamp-only because bandcampsync
exists and works within Bandcamp's terms. Beatport, for example, has an API that
isn't open to new applications; working around that means scraping credentials
from its documentation pages, which is fragile and against the spirit of its
terms.

**Writing genres**, for now (#12). See [tagging](tagging.md#what-isnt-written-and-why).

**Tagging video.** Video files are counted as tracks, so a CD+DVD release isn't
reported as incomplete, but their tags are never written or compared.
