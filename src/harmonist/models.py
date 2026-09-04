"""Core data types: Album, AlbumState, Sidecar."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

# MusicBrainz JSON shapes. `musicbrainzngs` returns plain untyped dicts whose
# schema varies with the `includes=` we request and is riddled with optional,
# nested keys we read defensively. The value type is genuinely Any, so a
# TypedDict would only assert a shape we never validate at the boundary. These
# aliases document *which* MB shape a dict represents at each call site.
type Release = dict[str, Any]
type Track = dict[str, Any]

# The `.harmonist.json` sidecar schema version. Lives here (not in `sidecar.py`)
# so `Sidecar.schema_version` can default to it without an import cycle —
# `sidecar.py` imports `models`, not the other way round. `sidecar.py` re-exports
# it for `from harmonist.sidecar import CURRENT_SCHEMA_VERSION`.
CURRENT_SCHEMA_VERSION = 1

# The per-album sidecar's filename. Lives here, next to the schema version and
# for the same reason: `models` imports nothing from the package, so anything
# that needs to recognise a sidecar can read the name without importing the
# module that reads its contents.
#
# `album_files` is why that matters. It has to ask whether a directory declares
# itself an album, which is a question about a filename — but importing
# `sidecar` for it closed a cycle (sidecar → library_index → url_recovery →
# album_files → sidecar) that made `import harmonist.sidecar` fail outright
# whenever it was the first harmonist import (#200).
SIDECAR_FILENAME = ".harmonist.json"


class AlbumState(StrEnum):
    NEW = "new"
    # NEEDS_MBID covers "no confirmed MB release yet" — whether or not a
    # suggestion (mb_match_candidate) is attached. The card adapts: with a
    # candidate it shows the side-by-side + Confirm; without, the assign/find
    # tools. (Formerly split into NEEDS_MBID + NEEDS_REVIEW.)
    NEEDS_MBID = "needs_mbid"
    TAGGING = "tagging"
    # Displayed as "Needs Link" — the album needs its on-disk copy linked to the
    # Bandcamp purchase (fill item_id). The enum name/value stay "needs_sync"
    # (the mechanism, and the /status JSON key the frontend reads); only the
    # user-facing label changed. See docs/design.md §3.
    NEEDS_SYNC = "needs_sync"
    COMPLETE = "complete"
    INCOMPLETE = "incomplete"
    INCONSISTENT = "inconsistent"


MatchConfidence = Literal["exact", "approximate", "no_match"]


@dataclass
class InconsistentTrack:
    """One row in the INCONSISTENT card's per-file summary table.

    Surfaced when files in a single album dir disagree on MB Album Id
    (`----:com.apple.iTunes:MusicBrainz Album Id`), or on album title
    (`©alb`) without one release id on every file to settle it (§13.2).
    Compilations (varying artist, consistent album+MBID) don't appear here
    — they're legitimate, not inconsistent.
    """

    file_name: str
    album_title: str | None
    mb_album_id: str | None


@dataclass
class BandcampInfo:
    """Bandcamp-specific identifiers. Optional — only set when the album
    came from Bandcamp AND we know the item_id (typically captured during
    bandcampsync at download time).
    """

    item_id: int | None = None
    band_id: int | None = None
    # True when the Bandcamp release is marked private/unlisted (its public
    # URL 404s). Load-bearing: suppresses the "Open in Harmony" + Recheck
    # affordances, since a private URL should not be added to MusicBrainz.
    is_private: bool = False
    # Set ONLY when the album couldn't be pinned to a single purchase: several
    # editions share one store URL (so one purchase per edition, all on the same
    # Bandcamp page) and a title tiebreak couldn't separate them. Holds the
    # purchase ids it could be — narrowed but not resolved. Load-bearing: it
    # takes the album OUT of NEEDS_SYNC (an ambiguous link is as resolved as we
    # can get without per-item track data), and a future re-download must refuse
    # while it's set (no single id to fetch). Empty/None once `item_id` is set.
    candidate_item_ids: list[int] | None = None


def _canonical_mark(ch: str) -> str:
    """One character, with a typographic respelling folded onto one spelling.

    A **rule over Unicode's own classification**, not a table of characters to
    forgive — which is the whole point (#379). A list is a chase with no end, and
    it runs out first in the scripts we handle least: the next album is as likely
    to arrive with a full-width bracket or a Greek dash as with a curly quote,
    and nobody can enumerate those in advance. A category and a name pattern
    cover the code points Unicode has and the ones it adds later.

    Two classes, because these are the marks that have several spellings meaning
    the same thing:

    * **`Pd`** — every dash Unicode has: hyphen-minus, en, em, figure, small,
      ideographic. A dash is a dash;
    * **anything Unicode names a quotation mark, apostrophe or prime** — `'`,
      `’`, `‘`, `“`, `«`, `′`, the modifier-letter apostrophe. Single and double
      fold together too: a title cannot mean something different for having been
      typeset with one rather than the other.

    Anything else is returned untouched — see `norm_title` on why this
    canonicalises rather than strips.

    The `isalnum` guard is the fast path, not a rule: a letter or digit can never
    be either class, and skipping `unicodedata.name` for it keeps this off the
    album page's render cost.
    """
    if ch.isalnum():
        return ch
    if unicodedata.category(ch) == "Pd":
        return "-"
    name = unicodedata.name(ch, "")
    if "QUOTATION MARK" in name or "APOSTROPHE" in name or "PRIME" in name:
        return "'"
    return ch


def norm_title(s: str) -> str:
    """Light normalisation for comparing a disk title against an MB title:
    fold typographic respellings, collapse whitespace, casefold. Deliberately
    *not* the aggressive `_norm_*` used for matching — here we want to surface
    real differences, so only noise is ignored.

    Public because it draws the line between a cosmetic title change and a real
    one in two places now: `TrackComparison.title_differs`, and the significance
    classifier (#267), which auto-applies a spacing or casing tidy-up and holds
    a genuine retitle. Those two must agree — a title the album page reports as
    unchanged is not one the gardener may treat as a retitle — so they share the
    definition rather than each having a notion of "cosmetic".

    **NFKC is where the non-English half is won** (#379). It composes a
    decomposed accent, so a file written on a filesystem that stores `é` as
    `e` + U+0301 stops differing from MusicBrainz's composed one; it folds the
    full-width brackets and punctuation a CJK title arrives in onto their ASCII
    forms; it turns `…` into `...` and a non-breaking space into a space. All of
    that is a spelling of the same title, and none of it is enumerable by hand.

    **Canonicalise, never strip.** Dropping punctuation outright would fold
    `Live?` onto `Live!` and call a retitle cosmetic — and `owned.BY_VALUE` is
    explicit that this rule may only ever *lower* a change's significance, so a
    fold that fires wrongly is a write nobody agreed to. Presence, absence and
    meaning of a mark all survive; only its typeface does not.

    Not to be confused with `bandcamp_hook._norm_title`, which is the aggressive
    matching normaliser and deliberately answers a different question.
    """
    folded = "".join(_canonical_mark(ch) for ch in unicodedata.normalize("NFKC", s))
    return " ".join(folded.split()).casefold()


def title_with_disambiguation(title: str | None, disambiguation: str | None) -> str | None:
    """`Title (disambiguation)` — how Picard spells an album title when its
    "use release disambiguation comment in album title" option is on.

    A library tagged that way carries `Selected Ambient Works Volume II
    (expanded edition)` where MusicBrainz's release title is `Selected Ambient
    Works Volume II`. That is the same album by the user's own deliberate
    setting, so it is not a mismatch — and reporting it as one costs more than a
    wrong row on a page: `album` would differ on every pass forever, so #266's
    write-skip would never fire and #267's IDENTITY verdict would put the whole
    library in the Inbox on the gardener's first night (#283).

    None when either half is missing, meaning there is no second spelling to
    accept. A release with no disambiguation has exactly one title, and a
    bracketed suffix on such an album is a different title rather than the same
    one spelled Picard's way.

    Deliberately **not** `titles_match`, which would also accept this pair. That
    rule is a word-level subsequence test, so it accepts *any* parenthetical
    suffix — `(deluxe edition)`, `(2019 remaster)` — on the strength of the
    words alone. It earns that latitude where it is used, inside an
    artist-scoped and uniqueness-guarded purchase match. Here the release states
    its disambiguation exactly, so accepting anything looser would be guessing
    an identity that was available for free (review-gate item 2). Picard applies
    several other title transforms besides this one; recognising them is #284,
    and it is a survey rather than a loosening of this rule.
    """
    title = (title or "").strip()
    disambiguation = (disambiguation or "").strip()
    if not title or not disambiguation:
        return None
    return f"{title} ({disambiguation})"


@dataclass
class TrackComparison:
    """One row in a side-by-side files-vs-MB-release comparison.

    All fields are nullable to allow padding rows when the counts don't match:
    - file_* None → an MB track with no corresponding file on disk
    - mb_* None  → a file on disk with no corresponding MB track
    """

    file_name: str | None
    file_duration_ms: int | None
    file_title: str | None  # from the ©nam tag if present, else filename stem
    mb_track_title: str | None
    mb_track_length_ms: int | None
    delta_ms: int | None  # None when either side's length is unknown

    @property
    def title_differs(self) -> bool:
        """True when both titles are present and differ after light
        normalisation. A padding row (either side missing) is a count mismatch,
        not a metadata discrepancy, so it returns False. Derived, never stored."""
        if not self.file_title or not self.mb_track_title:
            return False
        return norm_title(self.file_title) != norm_title(self.mb_track_title)


@dataclass
class MatchCandidate:
    """A proposed-but-not-confirmed MBID match for an album.

    Stashed in the sidecar when the MB lookup found a release but the
    file/track shape doesn't perfectly fit. The user must Confirm or
    Reject before tagging proceeds.
    """

    mb_release_id: str
    confidence: MatchConfidence
    file_count: int
    track_count: int
    track_comparisons: list[TrackComparison] = field(default_factory=list)
    proposed_at: datetime | None = None
    notes: list[str] = field(default_factory=list)
    # Set ONLY when this candidate was proposed because the album looks
    # mis-tagged: it's tagged as one release but the user owns a *different*
    # release in the same MusicBrainz release group (matched by store URL).
    # These let the UI name both sides of the swap (each linked to MB) and the
    # purchase URL. None for an ordinary suggestion. Store-URL driven —
    # independent of the track-count match quality (`confidence` / `notes`).
    # The owned (suggested) release is this candidate's `mb_release_id`.
    mistag_owned_url: str | None = None  # Bandcamp purchase URL
    mistag_owned_label: str | None = None  # owned release "Artist / Title"
    mistag_owned_disambig: str | None = None  # owned release MB disambiguation, if any
    mistag_tagged_mbid: str | None = None  # the release it's currently (wrongly) tagged as
    mistag_tagged_label: str | None = None  # that release's "Artist / Title"
    mistag_tagged_disambig: str | None = None  # that release's MB disambiguation, if any
    mistag_release_group_mbid: str | None = None  # the shared release group (for an MB link)
    # Set when a full sync couldn't find ANY matching Bandcamp purchase for an
    # album, so it was dropped back to NEEDS_MBID for the user to seed/fix. The
    # candidate IS the album's existing (correct) release, kept as a read-only
    # suggestion — the card shows it as context + a "couldn't link" note, with
    # NO Confirm (re-confirming would just loop straight back to NEEDS_SYNC).
    # Load-bearing: drives that read-only render. Distinct from a mis-tag (where
    # the candidate is a *different*, confirmable release).
    unmatched_purchase: bool = False

    @property
    def title_mismatch_count(self) -> int:
        """How many track rows have an on-disk title that differs from MB.
        Derived from `track_comparisons`, never stored — lets the verify-tagging
        view flag a metadata discrepancy that the length-based `confidence`
        can't see."""
        return sum(1 for tc in self.track_comparisons if tc.title_differs)


@dataclass
class Sidecar:
    """Per-album persisted state — the on-disk `.harmonist.json`.

    `store_url` carries the canonical purchase URL from any store Harmony
    supports (Bandcamp, Beatport, Discogs, etc.). Store identity is derived
    from the URL host; absence of store_url means "no store source recorded".

    Identity: exactly one of `(mb_release_id, temp_uid)` is non-null on
    any persisted sidecar. `temp_uid` holds the album's stable URL id
    until an MBID lands, at which point sidecar.write() drops it. The
    scanner reads whichever is set and assigns it to `Album.id`.
    """

    schema_version: int = CURRENT_SCHEMA_VERSION
    store_url: str | None = None
    bandcamp: BandcampInfo | None = None
    downloaded_at: datetime | None = None
    added_at: datetime | None = None
    mb_release_id: str | None = None
    temp_uid: str | None = None
    mb_match_candidate: MatchCandidate | None = None
    tagged_at: datetime | None = None
    notes: str | None = None
    # Set when the user accepts a SURRENDERED album as done: a full sync found no
    # matching Bandcamp purchase (release withdrawn from Bandcamp, bought
    # elsewhere, or ripped) and they chose "Keep in Library". Load-bearing: the
    # scanner then treats the album as terminal (COMPLETE/INCOMPLETE) despite the
    # Bandcamp store_url + missing item_id, and the surrender pass leaves it alone.
    # Without it the album would re-classify NEEDS_SYNC and re-surrender on every
    # full sync — there is no purchase to link, ever.
    purchase_unavailable: bool = False
    # Set when the user accepts an INCOMPLETE album as finished: the tracks it is
    # missing are not obtainable, so there is nothing to act on. The Pink Floyd
    # Blu-ray where only the stereo mixes were ripped; the hidden CD track never
    # ripped from a disc since thrown away (#196).
    #
    # A claim about the SOURCE — "there are no more tracks to get" — not about
    # the UI. That distinction is what keeps it from becoming a general "ignore
    # this album": the honest reading is checkable, and it is the reason a
    # Bandcamp album whose artist has since added tracks is the wrong candidate
    # (#132 can genuinely fetch those).
    #
    # Load-bearing: the Library's Incomplete filter is for defects the user can
    # do something about, and an accepted one is no longer such a defect, so this
    # takes the album out of it. Deliberately does NOT change the state — the
    # album really is short, `INCOMPLETE` says so truthfully, and the tile still
    # reports the count. Not derivable at any price: it is a decision, with no
    # evidence on disk. Same shape as `purchase_unavailable` one level over.
    tracks_unavailable: bool = False
    # Which of the release's media hold nothing but video, by position — the one
    # fact about a release that cannot be read off the files, because a medium
    # the user never ripped has no files to carry it (#206).
    #
    # `None` means "not asked"; `()` means "asked, none are video". The
    # difference is load-bearing: it is what stops the lookup being repeated
    # forever on a release that turns out to have no video at all.
    #
    # Load-bearing: an album missing ONLY video media derives COMPLETE, since
    # Harmonist cannot tag video (#66) and telling the user forever that their
    # CD is missing 44 DVD tracks is noise. A partially-present video medium is
    # still INCOMPLETE — if you have one video you should have the rest.
    video_media: tuple[int, ...] | None = None


@dataclass
class Album:
    """An album as observed on disk, derived from sidecar + filesystem.

    `id` is the album's stable URL id. The scanner assigns it from the
    sidecar's `mb_release_id` (preferred) or `temp_uid` (fallback). For
    albums with no sidecar it comes from `id_registry`, which derives it
    from the path relative to the library root — so it survives a restart
    and records written against such an album stay attached to it (#114).
    """

    id: str
    path: Path
    title: str
    artist: str
    track_count: int
    state: AlbumState
    sidecar: Sidecar | None = None
    cover_path: Path | None = None
    # Populated only when state == INCONSISTENT — per-file summary of the
    # conflicting fields for the UI table. Empty list otherwise.
    inconsistent_tracks: list[InconsistentTrack] = field(default_factory=list)
    # `(tagged_count, total_count)` when only some files in the dir have
    # the matching MB Album Id atom (0 < tagged < total). None otherwise.
    # Not persisted — purely scanner-derived. Independent of INCOMPLETE:
    # partial tagging is about MBID atoms on present files, INCOMPLETE
    # is about how many files are present vs MB's tracklist.
    partial_tag_count: tuple[int, int] | None = None
    # Human label for the album's audio format(s), e.g. "ALAC", "FLAC", or
    # "Mixed" when files disagree. Scanner-derived; not persisted. Confirms
    # the download format the user chose actually landed.
    audio_format: str | None = None
    # What that format actually is — "44.1 kHz · 16 bit", "44.1 kHz · 320 kbps
    # CBR" (#130). Scanner-derived; not persisted, like `audio_format` beside
    # it. Says whether a download is the quality the user paid for, and whether
    # two copies of an album are really the same. Carries its own outlier
    # clause when the album's files disagree; None when nothing could be read.
    audio_quality: str | None = None
    # True if a cover is available — a folder cover.* OR embedded art in the
    # tracks. Drives whether the UI requests /cover (avoids 404 floods) and
    # lets /cover serve the embedded art without writing it to disk.
    has_cover: bool = False
    # True if the tracks carry a MusicBrainz Album Id atom. For a NEW album
    # (no sidecar) this is what makes it *reconcilable* — reconcile derives a
    # sidecar from the tag MBID. Untagged orphans (no MBID) are never
    # reconcilable, so the inbox uses this to avoid kicking reconcile for them.
    has_tag_mbid: bool = False
    # The release's track count as the FILES report it (#195) — `trkn`/`disk`
    # totals, written by the tagging from MusicBrainz. Scanner-derived, never
    # persisted: it replaced a sidecar field holding the same number from the
    # same source, which is duplicated data the tags already carry.
    #
    # None when unknowable — the files carry no totals, or a whole medium is
    # absent so nothing on disk records its length. None is NOT "complete":
    # state comes from `scanner.expected_tracks(...).complete`, which answers
    # the missing-medium case that no total can express.
    expected_track_count: int | None = None
    # Every directory this album's files come from, in path order, and always
    # non-empty (a one-folder album holds just `path`).
    #
    # An album is the files that name its MusicBrainz release, wherever they sit
    # (#197) — the directory is where they happen to live, not what the album
    # IS. `path` remains the album's primary folder for display and for
    # anything that needs one name; `paths` is the truth about where it lives,
    # and the album page lists them (#198).
    paths: tuple[Path, ...] = ()
    # Medium positions with no files of any kind on disk. Scanner-derived from
    # the tags; drives the bounded MusicBrainz pass that fills `video_media`
    # (#206), which is the only thing that can say whether their absence matters.
    absent_media: frozenset[int] = frozenset()
    # How many media the files say the release has, when they agree on it.
    # Scanner-derived. Compared against MusicBrainz's actual medium count on the
    # album page (#204): the files claiming a 1-disc release while the sidecar
    # names a 3-disc one is a disagreement worth surfacing, and it is invisible
    # to everything else — the album derives COMPLETE because, by its own tags,
    # it is.
    disc_total: int | None = None
    # When this album's audio files were last written, from the mtimes the scan
    # already stats. Compared against `sidecar.tagged_at` to notice a re-tag done
    # OUTSIDE Harmonist — in Picard, which is what Harmonist asks users to do
    # (#220). Scanner-derived; not persisted.
    files_written_at: datetime | None = None
    # True when a re-tag against the release MusicBrainz currently holds would
    # change at least one owned tag (#287). NOT scanner-derived and NOT
    # persisted: it is set by `gardener.refresh_flag` — from the album page's
    # comparison, from the startup warm-up, and later from the background pass
    # (#270) — and it lives here so the Library can filter on it for free.
    #
    # False therefore means "nothing to take, as far as we have looked", which
    # on a cold start is not the same as "nothing to take". The flag
    # UNDER-reports by design: an album we have never compared, or one whose
    # files could not be read, stays False. That is the safe direction — the
    # filter can miss an album, but it can never invite the user to act on an
    # update that isn't there.
    #
    # Survives a rescan for the usual single-directory album, because
    # `resolve_dir` returns the cached Album on a signature hit and
    # `merge_by_identity` passes a one-part album straight through — the same
    # object, flag intact. An album whose files or sidecar changed is rebuilt
    # and starts False again, which is the self-invalidation we want: a re-tag
    # that took the update clears the flag by construction.
    update_available: bool = False
    # How far the outstanding update reaches — `owned.Significance` (#271). Set
    # by the same `gardener.refresh_flag` call as the flag beside it, from the
    # same plan, so the two can never describe different releases. None when
    # there is nothing outstanding, and None on a cold start for the same
    # reason the flag is False.
    #
    # Derived and unpersisted, like the flag: it is what the review list sorts
    # and groups by, and it is rebuilt from the cached release payloads at
    # startup at the cost of no MusicBrainz requests at all.
    update_significance: str | None = None
    # Which version of the cached MusicBrainz payload the two fields above were
    # computed against — `gardener.release_version`. None until something has
    # looked.
    #
    # Load-bearing for Ignore (#271): ignoring an update records the version it
    # was ignored at, and the ignore holds only while this still matches. Kept
    # here rather than re-derived at render time so deciding whether a tile is
    # muted is a string compare rather than a database read and a hash, which at
    # Library scale is the difference between free and unusable.
    mb_version: str | None = None

    @property
    def folders(self) -> tuple[Path, ...]:
        """`paths`, falling back to `path` for an Album built without them —
        every test fixture that constructs one by hand, and every caller that
        predates #197."""
        return self.paths or (self.path,)

    @property
    def label(self) -> str:
        """ "Artist — Title", for anything that has to name this album in prose.

        Chiefly the `album_label` column: events and gardener findings both
        freeze a label at write time, because ids move and albums leave the
        library, and a record that can no longer be read is not a record.

        The dash goes when one half is missing, so an album with no artist yet
        reads as its title rather than as "— Title"."""
        return f"{self.artist} — {self.title}".strip(" —")


# ---------------------------------------------------------------------------
# Store URL helpers
# ---------------------------------------------------------------------------


def host_is(url: str | None, domain: str) -> bool:
    """True if the URL's host is `domain` itself or a subdomain of it.

    The dot in the suffix is load-bearing: a bare `endswith("bandcamp.com")`
    also accepts `notbandcamp.com`, and a substring test accepts anything with
    the domain in its path or query. Store URLs reach us from file tags and
    user input, so a lookalike host must not be classified as the real store.
    """
    if not url:
        return False
    host = (urlparse(url).hostname or "").lower()
    return host == domain or host.endswith(f".{domain}")


def is_bandcamp_url(url: str | None) -> bool:
    """True if the URL is on a bandcamp.com domain (any subdomain)."""
    return host_is(url, "bandcamp.com")


def store_name(url: str | None) -> str | None:
    """Identify the store from a URL's hostname. Returns None when the
    URL is absent or the store is unrecognised.
    """
    if host_is(url, "bandcamp.com"):
        return "bandcamp"
    if host_is(url, "beatport.com"):
        return "beatport"
    if host_is(url, "discogs.com"):
        return "discogs"
    return None


def title_words(s: str | None) -> tuple[str, ...]:
    """An album/release title as a tuple of words: casefold, '&' → 'and',
    punctuation dropped. The unit for order-preserving approximate title matching
    (`titles_match`)."""
    s = (s or "").casefold().replace("&", " and ")
    return tuple(re.sub(r"[^a-z0-9]+", " ", s).split())


def titles_match(a: tuple[str, ...], b: tuple[str, ...]) -> bool:
    """True if two titles are the *same words in the same order* — i.e. one is a
    word-level **subsequence** of the other. Absorbs MusicBrainz-vs-Bandcamp
    differences (a trailing "EP"/"Single", a "(Deluxe Edition)" suffix, a dropped
    "The", …) without enumerating them. Empty titles never match; safety comes from
    the caller's UNIQUENESS guard (ambiguous → no match), not from this rule."""
    if not a or not b:
        return False
    return _is_word_subsequence(a, b) or _is_word_subsequence(b, a)


def _is_word_subsequence(short: tuple[str, ...], long: tuple[str, ...]) -> bool:
    """True if every word of `short` appears in `long` in order (gaps allowed)."""
    it = iter(long)
    return all(word in it for word in short)
