"""Recovery catalogue retained for regression tests, not the public demo."""

from __future__ import annotations

import hashlib
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mutagen.mp4 import MP4

from harmonist import activity_store, demo, formats
from harmonist import sidecar as sidecar_mod
from harmonist.demo import ASSETS_DIR, SINE, _build_sidecar, _safe
from harmonist.formats.m4a import (
    ATOM_ALBUM,
    ATOM_ARTIST,
    ATOM_COMMENT,
    ATOM_MB_ALBUM_ID,
    ATOM_MB_RELEASE_TRACK_ID,
    ATOM_MB_TRACK_ID,
    ATOM_TITLE,
    ATOM_TRACK_NUM,
)
from harmonist.models import Release

LIBRARY: list[dict[str, Any]] = [
    {
        # State: NEW — no sidecar, but has MBID atom + Bandcamp ©cmt.
        # Reconcile derives a sidecar (transitions to NEEDS_SYNC).
        "artist": "Wyld Stallion",
        "album": "A Most Excellent Journey",
        "tracks": [
            "Be Excellent To Each Other",
            "Party On Dudes",
            "Strange Things Are Afoot at the Circle K",
        ],
        "cover": "cover-1.jpg",
        "file_mbid": "demo-rel-wyld",
        "file_comment": "Visit https://wyldstallion.bandcamp.com",
        "sidecar": None,
    },
    {
        # State: NEEDS_MBID — sidecar with store URL but no MB match yet.
        # Recheck looks up MB → exact match → tags → COMPLETE.
        "artist": "Sex Bob-omb",
        "album": "We Are Here To Make You Sad",
        "tracks": ["Garbage Truck", "Threshold", "Summertime"],
        "cover": "cover-2.jpg",
        # The files carry a different image from cover.jpg, so the Artwork
        # section has the case worth catching: a re-tag would replace what the
        # tracks have (#155).
        "art": "stale",
        "sidecar": {
            "store_url": "https://sexbobomb.bandcamp.com/album/we-are-here-to-make-you-sad",
            "bandcamp_item_id": 1001,
        },
    },
    {
        # State: NEEDS_MBID — no store URL, awaiting manual MBID assignment.
        # Manual ingest form (paste MBID or search MB by name) tags it.
        "artist": "Sonic Death Monkey",
        "album": "Top 5 Records For A Wednesday",
        "tracks": [
            "Top 5 Side One Track Ones",
            "Top 5 Songs About Death",
            "Top 5 Tracks For Lovers In Trouble",
        ],
        "cover": "cover-6.jpg",
        "sidecar": {},
    },
    {
        # State: NEEDS_MBID (with suggestion) — candidate stashed but confidence
        # is "approximate" (lengths off). Side-by-side renders.
        # Confirm → tags from MB; Reject → back to NEEDS_MBID.
        #
        # NO `file_mbid`, deliberately: an album awaiting confirmation has not
        # been tagged yet — tagging happens ON confirm (§3.1) — so its files
        # carry no MusicBrainz id. This used to seed one, which made Confirm
        # re-tag fields that were already there without ever ESTABLISHING the
        # album's identity, and left the demo with no way to exercise the
        # commonest first tagging of all (#168).
        #
        # The tagged-files-but-unlinked shape is real — it is what the "wrong
        # match" pencil leaves behind — but it needs no fixture: click the
        # pencil on any Library album to reach it.
        "artist": "The Thamesmen",
        "album": "Gimme Some Money",
        "tracks": ["Gimme Some Money", "(Listen to the) Flower People", "Cups and Cakes"],
        "cover": "cover-3.jpg",
        # Per-track artwork, which the tagger preserves — the Artwork section
        # must offer no replacement here (#155).
        "art": "mixed",
        "sidecar": {
            "store_url": "https://thamesmen.bandcamp.com/album/gimme-some-money",
            "bandcamp_item_id": 1002,
            "mb_match_candidate": {
                "mb_release_id": "demo-rel-thamesmen",
                "confidence": "approximate",
                "deltas_ms": [5000, 6000, 4500],  # all over the 4s tolerance
            },
        },
    },
    {
        # State: NEEDS_SYNC — files tagged, Bandcamp store_url known,
        # item_id=None. "Try a different URL" / "Mark purchased elsewhere".
        "artist": "Dingoes Ate My Baby",
        "album": "Little Bit o' Hoot, Whole Lotta Nanny",
        "tracks": ["Pavlov's Bell", "Hellmouth Lullaby", "Cordelia's Theme"],
        "cover": "cover-4.jpg",
        # One track with no embedded art: #397 on screen, where filling the one
        # gap rewrites the two that were already right.
        "art": "gaps",
        "file_mbid": "demo-rel-dingoes",
        "file_comment": "Visit https://dingoes.bandcamp.com",
        "sidecar": {
            "store_url": "https://dingoes.bandcamp.com/album/little-bit-o-hoot",
            "bandcamp_item_id": None,
            "mb_release_id": "demo-rel-dingoes",
            "tagged": True,
        },
    },
    {
        # State: NEEDS_SYNC before the first sync, then NEEDS_MBID via SURRENDER
        # after it. Seeded as an ordinary tagged-but-unlinked album, NOT
        # pre-surrendered (#87): "no matching Bandcamp purchase" is a conclusion a
        # sync reaches by paging the whole collection, so asserting it on a fresh
        # install told the user something Harmonist hadn't worked out yet.
        #
        # The surrender is EARNED by the real post-sync pass, not faked here: its
        # store_url is deliberately not a known purchase, so the sync can't link
        # it, and `_report_unmatched_after_sync` demotes it with the read-only
        # "couldn't link" candidate. (Demo's result stub has no
        # collection_checkpoint_token, so every demo sync counts as full — which
        # is the condition that makes surrendering conclusive.)
        #
        # "Move to Library" (surrender_keep) then accepts it as a terminal Library
        # album — the action that must resolve without an inbox flicker (#11).
        "artist": "Barry Jive and the Uptown Five",
        "album": "Withdrawn from Sale",
        "tracks": ["No Longer Listed", "Gone from the Store", "Yours to Keep"],
        "cover": "cover-8.jpg",
        "file_mbid": "demo-rel-barryjive",
        "sidecar": {
            "store_url": "https://barryjive.bandcamp.com/album/withdrawn-from-sale",
            "bandcamp_item_id": None,
            "mb_release_id": "demo-rel-barryjive",
            "tagged": True,
        },
    },
    {
        # State: COMPLETE — fully tagged & confirmed. Hidden from inbox;
        # appears in the Library section.
        "artist": "Various Artists",
        "album": "The Rural Juror (OST)",
        "tracks": [
            "Main Title (The Rural Juror)",
            "Urban Fervor",
            "Closing Credits (Urinal Gerber)",
        ],
        "cover": "cover-5.jpg",
        "file_mbid": "demo-rel-rural-juror",
        # The "tags have drifted from MusicBrainz" album. Every difference here
        # is one a real Bandcamp download actually produces, and each exercises a
        # different part of the comparison (#106):
        #   album artist — pipe-joined, as Bandcamp writes multi-artist credits,
        #                  against MusicBrainz's join phrase. Stacked pair, and
        #                  the separator is marked in place.
        #   date         — a bare year against MusicBrainz's full date. Inline,
        #                  with only the added precision highlighted.
        # Track 1 keeps the full date, so the tracks disagree with each other and
        # the "2 of 3" pill has a real case.
        #
        # Deliberately NOT skewing ©alb or the MB Album Id: those are what
        # `scanner._check_consistency` watches, so disagreeing on one flips the
        # album to INCONSISTENT and out of the Library — changing the state this
        # album exists to demonstrate. Date and album-artist are display fields
        # and carry no state.
        "file_tags": {
            "aART": "Various | Artists",
            "\xa9day": "2024",
        },
        "file_tags_track_one": {"\xa9day": "2024-01-01"},
        # Per-track drift for the tracklist (#135). Both are differences a real
        # Bandcamp download produces, and neither is a mistake:
        #   track 1 — the file kept a featured credit in the title that
        #             MusicBrainz keeps in the artist credit instead. One
        #             contiguous run, marked in place.
        #   track 2 — a pipe-joined credit against MusicBrainz's join phrase.
        #             Invisible at a glance, which is exactly why the differing
        #             characters are underlined.
        # Track 3 is left alone, so most of the tracklist reads as it should:
        # plain lines, no findings.
        # Every track names its own artist, because the release does — a file
        # left carrying the album artist would make all three rows differ, and
        # "everything is wrong" is the reading this comparison exists to avoid.
        "file_track_tags": {
            1: {
                "\xa9nam": "Main Title (The Rural Juror) [feat. Jenna Maroney]",
                "\xa9ART": "Jenna Maroney",
            },
            2: {"\xa9ART": "Frank Rossitano | Toofer"},
            3: {"\xa9ART": "Jenna Maroney"},
        },
        "sidecar": {
            "store_url": "https://variousartists.bandcamp.com/album/the-rural-juror-ost",
            "bandcamp_item_id": 1003,
            "mb_release_id": "demo-rel-rural-juror",
            "tagged": True,
        },
    },
    {
        # State: INCOMPLETE — tagged & synced, but only 2 of the MB release's 4
        # tracks are on disk. Shows in the Library with the "2 of 4" badge.
        #
        # `release_tracks` is what makes it incomplete (#195): the files carry
        # the RELEASE's track total, as a real tagging writes it, not the count
        # of files in the folder. Without it the album would report 2 of 2.
        "artist": "Dr. Teeth and the Electric Mayhem",
        "album": "Can You Picture That?",
        "tracks": ["Can You Picture That?", "Mahna Mahna"],
        "release_tracks": 4,
        "cover": "cover-7.jpg",
        "file_mbid": "demo-rel-electric-mayhem",
        "sidecar": {
            "store_url": "https://electricmayhem.bandcamp.com/album/can-you-picture-that",
            "bandcamp_item_id": 1005,
            "mb_release_id": "demo-rel-electric-mayhem",
            "tagged": True,
        },
    },
    {
        # State: COMPLETE, but its release has been DELETED from MusicBrainz —
        # the real case #194 came from, where an editor removed a duplicate
        # release out from under an album that was already tagged to it. The
        # files are fine and still carry its tags; the album simply names
        # something that is no longer there. Opening its page raises the banner
        # offering to send it to Needs MBID (#210).
        #
        # `demo-rel-deleted` is deliberately absent from MB_RELEASES — that
        # absence IS the deletion.
        "artist": "The Soggy Bottom Boys",
        "album": "Man of Constant Sorrow",
        "tracks": ["Man of Constant Sorrow", "In the Jailhouse Now"],
        "cover": "cover-3.jpg",
        "file_mbid": "demo-rel-deleted",
        "sidecar": {
            "store_url": "https://soggybottomboys.bandcamp.com/album/constant-sorrow",
            "bandcamp_item_id": 1009,
            "mb_release_id": "demo-rel-deleted",
            "tagged": True,
        },
    },
    {
        # State: COMPLETE, but its release has been MERGED AWAY on MusicBrainz —
        # the sibling of the album above, and much the commoner of the two
        # (#268). An editor folded a duplicate release into another one, so the
        # id these files name now redirects; the album is flagged Update
        # available on the strength of it, and its page says so beside the
        # MusicBrainz badge, which is the thing whose meaning changed (#361).
        # Re-tagging follows the merge and settles it.
        #
        # `demo-rel-folksmen-dupe` is absent from MB_RELEASES and present in
        # MERGED_INTO — that pair IS the merge, exactly as the absence above is
        # the deletion.
        "artist": "The Folksmen",
        "album": "Old Joe's Place",
        "tracks": ["Old Joe's Place", "Never Did No Wanderin'"],
        "cover": "cover-1.jpg",
        "file_mbid": "demo-rel-folksmen-dupe",
        "sidecar": {
            "store_url": "https://thefolksmen.bandcamp.com/album/old-joes-place",
            "bandcamp_item_id": 1010,
            "mb_release_id": "demo-rel-folksmen-dupe",
            "tagged": True,
        },
    },
    {
        # State: NEEDS_SYNC, tagged as the STANDARD "Fever Dog" — but the user owns
        # the "Live at the Riot House" edition (sibling in the same release group).
        # The FIRST sync can't link the std URL (no purchase for it), so post-sync
        # mis-tag detection browses the release group, spots the owned live edition,
        # and demotes this to "Possibly mis-tagged" — the realistic flow (a mis-tag
        # surfaces AFTER a sync, not pre-seeded). Confirm re-tags to the live
        # edition; a further sync then links it → Library.
        "artist": "Stillwater",
        "album": "Fever Dog",
        "tracks": ["Fever Dog", "Love Thing", "Chelsea Hotel"],
        "cover": "cover-3.jpg",
        "file_mbid": "demo-rel-fever-std",
        "file_comment": "Visit https://stillwater.bandcamp.com",
        "sidecar": {
            "store_url": "https://stillwater.bandcamp.com/album/fever-dog",
            "bandcamp_item_id": None,
            "mb_release_id": "demo-rel-fever-std",
            "tagged": True,
        },
    },
    {
        # State: COMPLETE, but a NON-BANDCAMP ©cmt — purchased on Bandcamp, yet
        # the comment points at the artist's own site, so reconcile finds no
        # bandcamp.com URL and it lands in the Library unlinked. The matching
        # queued purchase recovers it via the fuzzy "already in your library?"
        # potential-download match (the "36" scenario).
        "artist": "Mouse Rat",
        "album": "The Awesome Album",
        "tracks": ["5000 Candles in the Wind", "The Pit", "Sex Hair"],
        "cover": "cover-6.jpg",
        "file_mbid": "demo-rel-mouserat",
        "file_comment": "Visit https://mouserat.net",
        "sidecar": {
            "mb_release_id": "demo-rel-mouserat",
            "tagged": True,
            # No store_url + no item_id → COMPLETE (Library), unlinked.
        },
    },
    {
        # State: COMPLETE — and wrong anyway. Only track 1 carries the MB Album Id
        # atom, which derives COMPLETE rather than INCONSISTENT: `_files_tagged_with`
        # is an `any()`, and a file missing the field doesn't vote on consistency.
        # So it sits in the Library looking finished, with nothing but a "1/3
        # tagged" line on the tile to say otherwise — the exact shape the
        # Partially-tagged filter exists to find (#174). A half-finished tagging
        # run, or Picard applied to some of a folder, leaves this behind.
        "artist": "The Blues Brothers",
        "album": "Rawhide",
        "tracks": ["Rawhide", "Stand By Your Man", "Minnie the Moocher"],
        "cover": "cover-5.jpg",
        "file_mbid": "demo-rel-blues-brothers",
        "file_mbid_tracks": [1],
        "sidecar": {
            "store_url": "https://bluesbrothers.bandcamp.com/album/rawhide",
            # 1006 is the Stillwater live edition's; item_id is the dedup key, so
            # two albums sharing one would collide in `library_index`.
            "bandcamp_item_id": 1007,
            "mb_release_id": "demo-rel-blues-brothers",
            "tagged": True,
        },
    },
    {
        # State: COMPLETE with NO cover art — `cover: None` skips the artwork the
        # other albums get. Terminal, correctly tagged, fully linked, and still a
        # grey square in Plex/Navidrome, which is what the No-artwork filter is
        # for (#174). Common in an adopted library: a rip or an old download that
        # never had a folder image.
        "artist": "Otis Day and the Knights",
        "album": "Shout",
        "tracks": ["Shout", "Shama Lama Ding Dong"],
        "cover": None,
        "file_mbid": "demo-rel-otis-day",
        "sidecar": {
            "store_url": "https://otisday.bandcamp.com/album/shout",
            "bandcamp_item_id": 1008,
            "mb_release_id": "demo-rel-otis-day",
            "tagged": True,
        },
    },
    {
        # State: COMPLETE — and it will stay COMPLETE, because the album's own
        # files agree there are two of two. MusicBrainz has since grown the
        # release to four (an editor merged in the reissue's bonus tracks), so
        # pressing Re-tag from MB meets the count guard and gets the #252 offer
        # rather than a stack trace.
        #
        # No `release_tracks`: the default writes the FILES' own count into them,
        # which is exactly the point — the two facts (what MB said at tagging
        # time, what it says now) have to disagree for this state to exist, and
        # the disagreement lives between the files and MB_RELEASES below. The
        # incomplete album above is the other way round and is not this case.
        "artist": "The Wonders",
        "album": "Play!",
        "tracks": ["That Thing You Do!", "Dance With Me Tonight"],
        "cover": "cover-5.jpg",
        "file_mbid": "demo-rel-wonders",
        "sidecar": {
            "store_url": "https://thewonders.bandcamp.com/album/play",
            "bandcamp_item_id": 1010,
            "mb_release_id": "demo-rel-wonders",
            "tagged": True,
        },
    },
]


# Owned Bandcamp purchases NOT matched to an on-disk album by store_url. In a
# LINK-ONLY sync these surface as "potential downloads" for review; in a full
# sync they download. Mouse Rat also matches the on-disk Library album by
# artist/title, so its card shows "Already in your library?"; CB4 + Autobahn are
# genuinely new (Download). item_id → the id the card carries.
PENDING_PURCHASES: list[dict[str, Any]] = [
    {
        # Matches the on-disk "Mouse Rat / The Awesome Album" (Library, unlinked)
        # by artist+title → recovered via the fuzzy match instead of re-downloaded.
        "artist": "Mouse Rat",
        "album": "The Awesome Album",
        "tracks": ["5000 Candles in the Wind", "The Pit", "Sex Hair"],
        "cover": "cover-6.jpg",
        "sidecar": {
            "store_url": "https://mouserat.bandcamp.com/album/the-awesome-album",
            "bandcamp_item_id": 2003,
        },
    },
    {
        "artist": "CB4",
        "album": "Straight Outta Lowcash",
        "tracks": ["Straight Outta Lowcash", "M-O-N-E-Y", "The Real Thing"],
        "cover": "cover-7.jpg",
        "sidecar": {
            "store_url": "https://cb4.bandcamp.com/album/straight-outta-lowcash",
            "bandcamp_item_id": 2001,
        },
    },
    {
        "artist": "Autobahn",
        "album": "Nagelbett",
        "tracks": ["Karl Hungus", "Marmot Shall Inherit", "Ve Believe in Nuthing"],
        "cover": "cover-8.jpg",
        "sidecar": {
            "store_url": "https://autobahn.bandcamp.com/album/nagelbett",
            "bandcamp_item_id": 2002,
        },
    },
]


# Synthetic MB releases for everything that has an MBID. Shape mirrors what
# musicbrainzngs returns under release[...]: enough for tagger + assess_match.


def _demo_mbid(kind: str, mbid: str, index: int = 0) -> str:
    """A stable UUID-shaped id for a demo recording, release track or artist.

    Shaped like a real MBID rather than spelled `demo-rec-<album>-3`, and the
    difference is not cosmetic: an MBID is a random UUID, so the album page
    shortens one to its first characters and trusts that to distinguish two
    (#319). Readable synthetic ids share a long prefix, so every id on the album
    trimmed to the same `demo-rec…` — the demo library being the one place that
    exercised a rendering nobody with a real library will ever see.

    Derived from the name so it is stable across a reseed, and so the same artist
    keeps one id everywhere they are credited.
    """
    digest = hashlib.sha1(f"{kind}:{mbid}:{index}".encode()).hexdigest()[:32]
    return f"{digest[:8]}-{digest[8:12]}-{digest[12:16]}-{digest[16:20]}-{digest[20:32]}"


#: The areas a demo release can be issued in, by ISO 3166-1 code. Named rather
#: than left as bare codes because the album page's release-event list shows the
#: AREA — "United Kingdom", not "GB" — and a demo that only ever exercised the
#: code fallback would be the one place that rendering is met wrong.
_AREAS: dict[str, str] = {"US": "United States", "GB": "United Kingdom", "JP": "Japan"}

#: One date per release event, in order. Staggered by a fortnight, the way a
#: real staged release is — which is also what makes the Date row's value
#: explainable: it is the first of these, not the only one.
_EVENT_DATES: tuple[str, ...] = ("2024-01-01", "2024-01-15", "2024-02-01")


def _release(
    mbid: str,
    artist: str,
    title: str,
    tracks: list[str],
    lengths_ms: list[int] | None = None,
    *,
    rg: str | None = None,
    disambiguation: str = "",
    track_artists: list[str] | None = None,
    countries: tuple[str, ...] = ("US",),
) -> Release:
    """One MusicBrainz release, as the demo's stubbed client returns it.

    `track_artists` gives a track its own artist credit, the way a compilation
    or an OST really does. Without it every track inherits the release credit,
    and the tracklist comparison's per-track Artist column can only ever agree —
    which makes the divergence that matters most (#106 names it) unreachable in
    the very mode people meet the feature in.

    `countries` is the same argument in a different field: MusicBrainz issues a
    release in a LIST of countries and collapses that to the one `country` the
    tag holds, so the album page annotates its Country row only for a release
    with several (#329). Most demo albums have one, because most releases do;
    two have three, or the annotation is unreachable in the mode people meet it.
    """
    if lengths_ms is None:
        lengths_ms = [1000] * len(tracks)
    credits = track_artists or [artist] * len(tracks)
    # One id per distinct artist NAME, and the release credit's own id for the
    # release artist. MusicBrainz gives one artist one id however many tracks
    # credit them; minting a fresh id per track made a single name ambiguous —
    # two ids behind one phrase — which #309's credit lookup correctly refuses to
    # choose between. The demo was then the one place the artists-as-links
    # rendering could never appear, which is the mode people meet it in.
    artist_ids = {artist: _demo_mbid("art", mbid)}
    for name in credits:
        artist_ids.setdefault(name, _demo_mbid("art", mbid, len(artist_ids)))
    return {
        "id": mbid,
        "title": title,
        "disambiguation": disambiguation,
        "status": "Official",
        # `country` and `date` are the FIRST release event, which is what
        # MusicBrainz does and what Picard writes. The rest are reachable only
        # through `release-event-list` below.
        "country": countries[0],
        "date": _EVENT_DATES[0],
        "barcode": None,
        "artist-credit": [
            {"artist": {"id": artist_ids[artist], "name": artist}, "name": artist},
        ],
        "release-group": {
            "id": rg or f"demo-rg-{mbid}",
            # MusicBrainz always sends this with the `release-groups` include, and
            # the album page's comparison shows the release group by NAME (#298).
            # Without it the demo library is the one place that only ever
            # exercises the raw-MBID fallback — the state nobody is meant to
            # meet, in the mode people meet the feature in.
            "title": title,
            "primary-type": "Album",
        },
        "release-event-list": [
            {"date": date, "area": {"name": _AREAS[c], "iso-3166-1-code-list": [c]}}
            for c, date in zip(countries, _EVENT_DATES, strict=False)
        ],
        "label-info-list": [
            {"label": {"name": "Demo Records"}, "catalog-number": "DEMO-001"},
        ],
        "medium-list": [
            {
                "position": "1",
                "format": "Digital Media",
                "track-list": [
                    {
                        "id": _demo_mbid("rt", mbid, i),
                        "position": str(i),
                        "title": title,
                        "artist-credit": [
                            {
                                "artist": {"id": artist_ids[credit], "name": credit},
                                "name": credit,
                            },
                        ],
                        "recording": {
                            "id": _demo_mbid("rec", mbid, i),
                            "title": title,
                            "length": str(length),
                        },
                    }
                    for i, (title, length, credit) in enumerate(
                        zip(tracks, lengths_ms, credits, strict=True), start=1
                    )
                ],
            }
        ],
    }


MB_RELEASES: dict[str, Release] = {
    "demo-rel-wyld": _release(
        "demo-rel-wyld",
        "Wyld Stallion",
        "A Most Excellent Journey",
        [
            "Be Excellent To Each Other",
            "Party On Dudes",
            "Strange Things Are Afoot at the Circle K",
        ],
    ),
    "demo-rel-sex-bob-omb": _release(
        "demo-rel-sex-bob-omb",
        "Sex Bob-omb",
        "We Are Here To Make You Sad",
        ["Garbage Truck", "Threshold", "Summertime"],
    ),
    "demo-rel-sonic-death-monkey": _release(
        "demo-rel-sonic-death-monkey",
        "Sonic Death Monkey",
        "Top 5 Records For A Wednesday",
        [
            "Top 5 Side One Track Ones",
            "Top 5 Songs About Death",
            "Top 5 Tracks For Lovers In Trouble",
        ],
    ),
    "demo-rel-thamesmen": _release(
        "demo-rel-thamesmen",
        "The Thamesmen",
        "Gimme Some Money",
        ["Gimme Some Money", "(Listen to the) Flower People", "Cups and Cakes"],
        lengths_ms=[6000, 7000, 5500],  # off by enough to land "approximate"
    ),
    "demo-rel-dingoes": _release(
        "demo-rel-dingoes",
        "Dingoes Ate My Baby",
        "Little Bit o' Hoot, Whole Lotta Nanny",
        ["Pavlov's Bell", "Hellmouth Lullaby", "Cordelia's Theme"],
    ),
    # The drifted album. Real per-track credits, so the tracklist's Artist column
    # has something to compare — on an OST they legitimately differ from the
    # album artist, and getting that to read as normal rather than as a fault is
    # the point of the comparison (#106).
    "demo-rel-rural-juror": _release(
        "demo-rel-rural-juror",
        "Various Artists",
        "The Rural Juror (OST)",
        ["Main Title (The Rural Juror)", "Urban Fervor", "Closing Credits (Urinal Gerber)"],
        # Three release events, so the album page's Country row has more than
        # one country to name (#329) — reachable only on an album that reaches
        # the LIBRARY, which is where that row is drawn.
        countries=("US", "GB", "JP"),
        track_artists=[
            "Jenna Maroney",
            "Frank Rossitano & Toofer",
            "Jenna Maroney",
        ],
    ),
    "demo-rel-cb4": _release(
        "demo-rel-cb4",
        "CB4",
        "Straight Outta Lowcash",
        ["Straight Outta Lowcash", "M-O-N-E-Y", "The Real Thing"],
    ),
    "demo-rel-autobahn": _release(
        "demo-rel-autobahn",
        "Autobahn",
        "Nagelbett",
        ["Karl Hungus", "Marmot Shall Inherit", "Ve Believe in Nuthing"],
    ),
    "demo-rel-electric-mayhem": _release(
        "demo-rel-electric-mayhem",
        "Dr. Teeth and the Electric Mayhem",
        "Can You Picture That?",
        # 4 tracks on MB; the seeded album only has the first 2 on disk.
        ["Can You Picture That?", "Mahna Mahna", "Movin' Right Along", "Rainbow Connection"],
    ),
    # Two editions of ONE release group — the mis-tag detection pairs them after a
    # sync (album tagged as the std edition; the user owns the live one).
    "demo-rel-fever-std": _release(
        "demo-rel-fever-std",
        "Stillwater",
        "Fever Dog",
        ["Fever Dog", "Love Thing", "Chelsea Hotel"],
        rg="demo-rg-fever-dog",
    ),
    "demo-rel-fever-live": _release(
        "demo-rel-fever-live",
        "Stillwater",
        "Fever Dog",
        ["Fever Dog", "Love Thing", "Chelsea Hotel"],
        rg="demo-rg-fever-dog",
        disambiguation="Live at the Riot House",
    ),
    "demo-rel-mouserat": _release(
        "demo-rel-mouserat",
        "Mouse Rat",
        "The Awesome Album",
        ["5000 Candles in the Wind", "The Pit", "Sex Hair"],
    ),
    "demo-rel-blues-brothers": _release(
        "demo-rel-blues-brothers",
        "The Blues Brothers",
        "Rawhide",
        ["Rawhide", "Stand By Your Man", "Minnie the Moocher"],
    ),
    "demo-rel-otis-day": _release(
        "demo-rel-otis-day",
        "Otis Day and the Knights",
        "Shout",
        ["Shout", "Shama Lama Ding Dong"],
    ),
    # The surviving side of a merge. Nothing else names it: the album that does
    # names the id it was merged FROM, and only follows this one once re-tagged.
    "demo-rel-folksmen": _release(
        "demo-rel-folksmen",
        "The Folksmen",
        "Old Joe's Place",
        ["Old Joe's Place", "Never Did No Wanderin'"],
    ),
    "demo-rel-wonders": _release(
        "demo-rel-wonders",
        "The Wonders",
        "Play!",
        # 4 tracks on MB; the seeded album has the first 2 on disk AND its files
        # say "2 of 2" — the release grew after the tagging (#252). That is the
        # whole fixture: the album derives COMPLETE, so Re-tag sends
        # `incomplete=False` and the guard refuses against this count.
        [
            "That Thing You Do!",
            "Dance With Me Tonight",
            "All My Only Dreams",
            "Little Wild One",
        ],
    ),
}


# Merged-away MBID → the release MusicBrainz serves in its place (#268, #361).
#
# MusicBrainz *redirects* a merged id rather than 404ing it, and that redirect is
# the only notice Harmonist ever gets — so this map is what makes a merge
# reachable in demo mode, the way an id missing from MB_RELEASES makes a deletion
# reachable. A key here must NOT also be a key of MB_RELEASES: a release cannot
# both survive and have been merged away, and `fetch_release` resolves this first.
MERGED_INTO: dict[str, str] = {
    "demo-rel-folksmen-dupe": "demo-rel-folksmen",
}


# Bandcamp URL → MB release MBID. Used by lookup_by_bandcamp_url + by
# fetch_release_urls (reverse direction).
URL_RELS: dict[str, str] = {
    "https://wyldstallion.bandcamp.com/album/a-most-excellent-journey": "demo-rel-wyld",
    "https://sexbobomb.bandcamp.com/album/we-are-here-to-make-you-sad": "demo-rel-sex-bob-omb",
    "https://thamesmen.bandcamp.com/album/gimme-some-money": "demo-rel-thamesmen",
    "https://dingoes.bandcamp.com/album/little-bit-o-hoot": "demo-rel-dingoes",
    "https://variousartists.bandcamp.com/album/the-rural-juror-ost": "demo-rel-rural-juror",
    "https://cb4.bandcamp.com/album/straight-outta-lowcash": "demo-rel-cb4",
    "https://autobahn.bandcamp.com/album/nagelbett": "demo-rel-autobahn",
    # Both Fever Dog editions carry a Bandcamp URL on MB, so post-sync mis-tag
    # detection can browse the release group and spot the owned (live) sibling.
    "https://stillwater.bandcamp.com/album/fever-dog": "demo-rel-fever-std",
    "https://stillwater.bandcamp.com/album/fever-dog-live": "demo-rel-fever-live",
    "https://bluesbrothers.bandcamp.com/album/rawhide": "demo-rel-blues-brothers",
    "https://otisday.bandcamp.com/album/shout": "demo-rel-otis-day",
    "https://thewonders.bandcamp.com/album/play": "demo-rel-wonders",
}


# Demo "Bandcamp purchases" by URL — the item_id that would come back from
# the real Bandcamp purchase listing. Sync iterates this map and fills in
# missing `bandcamp.item_id` for any already-on-disk album whose store_url
# matches (mirrors HarmonistSyncer.sync_item's existing-on-disk path).
PURCHASE_ITEM_IDS: dict[str, int] = {
    "https://wyldstallion.bandcamp.com/album/a-most-excellent-journey": 1000,
    "https://sexbobomb.bandcamp.com/album/we-are-here-to-make-you-sad": 1001,
    "https://thamesmen.bandcamp.com/album/gimme-some-money": 1002,
    "https://dingoes.bandcamp.com/album/little-bit-o-hoot": 1004,
    "https://variousartists.bandcamp.com/album/the-rural-juror-ost": 1003,
    # The Stillwater mis-tag's OWNED edition — so confirming the mis-tag (which
    # re-tags to this URL) then syncing links it → Library, completing the story.
    "https://stillwater.bandcamp.com/album/fever-dog-live": 1006,
}


def _materialise(music_dir: Path, spec: dict[str, Any]) -> None:
    """Lay out one demo album: dirs + .m4a files (with tags) + cover + sidecar."""
    album_dir = music_dir / _safe(spec["artist"]) / _safe(spec["album"])
    album_dir.mkdir(parents=True, exist_ok=True)

    # The MB release's track count, which is what a tagging writes into every
    # file and what the scanner reads back to derive COMPLETE vs INCOMPLETE
    # (#195). Defaults to the files present — the ordinary complete album.
    n_tracks = spec.get("release_tracks", len(spec["tracks"]))
    for i, title in enumerate(spec["tracks"], start=1):
        target = album_dir / f"{i:02d} {_safe(title)}.m4a"
        shutil.copy(SINE, target)
        audio = MP4(target)
        audio[ATOM_TITLE] = [title]
        audio[ATOM_ALBUM] = [spec["album"]]
        audio[ATOM_ARTIST] = [spec["artist"]]
        audio[ATOM_TRACK_NUM] = [(i, n_tracks)]
        # `file_mbid_tracks` narrows the atom to some of the tracks, so demo has a
        # partially tagged album for the Library filter to find (#174). Absent —
        # the normal case — means every track carries it.
        only_tracks = spec.get("file_mbid_tracks")
        if (mbid := spec.get("file_mbid")) and (only_tracks is None or i in only_tracks):
            audio[ATOM_MB_ALBUM_ID] = [mbid.encode("utf-8")]
            audio[ATOM_MB_RELEASE_TRACK_ID] = [_demo_mbid("rt", mbid, i).encode()]
            audio[ATOM_MB_TRACK_ID] = [_demo_mbid("rec", mbid, i).encode()]
        if cmt := spec.get("file_comment"):
            audio[ATOM_COMMENT] = [cmt]
        # Tags that deliberately DISAGREE with the MusicBrainz release, so the
        # album page's comparison has something to compare (#106). Without these
        # every demo album can only produce additions — MusicBrainz has a label
        # and a date, the files have neither — and the stacked pair, the
        # in-value emphasis and the "2 of 3" consensus pill are unreachable in
        # demo, which is where people first meet the feature.
        for atom, value in (spec.get("file_tags") or {}).items():
            audio[atom] = [value]
        # …and one track that disagrees with its own album, for the pill.
        if i == 1:
            for atom, value in (spec.get("file_tags_track_one") or {}).items():
                audio[atom] = [value]
        # Per-track drift, for the tracklist comparison (#135). Written last so
        # it wins over the title/artist set from the spec above — the whole point
        # is that the file says something the MusicBrainz release doesn't.
        for atom, value in (spec.get("file_track_tags") or {}).get(i, {}).items():
            audio[atom] = [value]
        audio.save()

    # An explicit `"cover": None` means "this album has no artwork" and must not
    # fall through to the default asset — that is the whole point of the album
    # seeding it (#174). Only an ABSENT key takes the default.
    cover_name = spec.get("cover", "cover-7.jpg")
    if cover_name:
        cover_asset = ASSETS_DIR / cover_name
        if cover_asset.exists():
            shutil.copy(cover_asset, album_dir / "cover.jpg")

    _embed_art(album_dir, spec, cover_name)

    # Distinguish "no sidecar" (sentinel None) from "empty sidecar" ({}).
    sc_spec = spec.get("sidecar")
    if sc_spec is not None:
        sidecar_mod.write(album_dir, _build_sidecar(sc_spec, spec))


def _embed_art(album_dir: Path, spec: dict[str, Any], cover_name: str | None) -> None:
    """Put embedded artwork into the album's files, per the spec's `art` key.

    Demo albums used to carry a folder `cover.jpg` and nothing inside the files,
    so every one of them read as "cover.jpg only, no track carries an embedded
    image" — one of the Artwork section's seven states, and the least
    interesting (#155). The section is judged by looking at it, so demo has to
    be able to show the other six.

    `art` values, chosen to name the SHAPE rather than the mechanics:

    - absent  — every track carries the folder cover. The norm, and what an
                album Harmonist has tagged actually looks like.
    - "none"  — nothing embedded at all.
    - "stale" — every track carries a DIFFERENT image from the folder cover, so
                a re-tag would replace it. The case worth catching: it is how an
                album ends up downgraded to a smaller cover.
    - "gaps"  — every track but the second, which has none. The art is a
                different image from the folder cover, deliberately: that is
                #397 on screen, where filling the one gap rewrites the two that
                were already right. With the folder cover embedded instead, the
                rewrite is a no-op and the case looks harmless.
    - "mixed" — a different image per track, as a compilation legitimately has.
                Preserved by the tagger, so nothing is offered to overwrite it.
    """
    shape = spec.get("art")
    if shape == "none":
        return
    # An album seeded with NO folder cover is the Library's No-artwork fixture
    # (#174), and `has_cover` is true of embedded art as well — so embedding a
    # default here would quietly empty that filter. Only an explicit shape puts
    # art into a coverless album.
    if cover_name is None and shape is None:
        return
    default = ASSETS_DIR / (cover_name or "cover-7.jpg")
    if not default.exists():
        return
    files = sorted(p for p in album_dir.iterdir() if p.suffix == ".m4a")
    # The assets are all small JPEGs, so a "different image" only has to be a
    # different one of them — no image generation, and what the section shows is
    # a real cover rather than a coloured square.
    others = [ASSETS_DIR / f"cover-{n}.jpg" for n in (3, 5, 1, 8, 2, 4, 6)]
    others = [p for p in others if p.exists() and p.name != default.name]
    if not others:
        return

    for i, path in enumerate(files):
        if shape == "gaps" and i == 1:
            continue
        source = default
        if shape in ("stale", "gaps"):
            source = others[0]
        elif shape == "mixed":
            source = others[i % len(others)]
        formats.write_cover(path, source.read_bytes())


def front_image(
    release_mbid: str,
    release_group_mbid: str | None = None,
    *,
    client: Any = None,
) -> Any:
    """Demo answer from the archive for a tagging: a placeholder from the demo
    assets, never a request.

    Kept in the real candidate cache like the live answer, so the album page
    afterwards shows the image a tagging would have weighed. Returns a
    `cover_art.Front` (typed `Any` so this module need not import `cover_art`
    outside `install`, as for every other patch here).
    """
    from harmonist import cover_art

    placeholder = ASSETS_DIR / "cover-7.jpg"  # generic green default
    if not placeholder.exists():
        return None
    data = placeholder.read_bytes()
    cover_art.cache_image(release_mbid, data, "image/jpeg")
    return cover_art.Front(data=data, mime="image/jpeg")


def check_front(
    release_mbid: str,
    *,
    release_group_mbid: str | None = None,
    known: activity_store.CachedCoverArt | None = None,
    keep_if_wider_than: int | None = None,
    client: Any = None,
) -> activity_store.CachedCoverArt:
    """Demo answer from the Cover Art Archive.

    It exists at all because the check is no longer something a user presses:
    since #436 an album page asks the archive by itself when the stored answer
    is stale, so an unpatched `check_front` would have demo mode making live
    requests to coverartarchive.org just for opening an album — the one thing
    demo mode promises it will never do.

    Answers with a cover that LOSES — 350px against the demo library's 400px
    art. Two demo-able states in one: the also-ran row (#433, #441), and the
    control that fetches its picture anyway because bigger is not the same as
    better (#448). It first answered "the archive holds nothing", which is the
    commonest real answer and shows a placeholder and no more, so the half of
    this feature that has anything to do could not be seen in the demo at all.

    No etag, so a re-check answers freshly rather than 304-ing against a
    conditional request nothing here would honour.
    """
    return activity_store.CachedCoverArt(
        fetched_at=datetime.now(UTC),
        image_url=f"https://coverartarchive.org/release/{release_mbid}/front",
        width=350,
        height=350,
        length=140_000,
        mime="image/jpeg",
        source="release",
    )


def fetch_image(release_mbid: str, url: str, *, client: Any = None) -> Path | None:
    """Demo fetch of the archive's image: a placeholder from the demo assets,
    kept in the real candidate cache.

    #448 made loading a losing cover something a user can press, which in demo
    mode would have been a live download from coverartarchive.org. It goes
    through `cache_image` rather than round it, so what the demo exercises is
    the real cache — only the bytes are local.

    The bytes are made DISTINCT from the asset they start as, because every
    `cover-N.jpg` is already carried by some demo album and the page deduplicates
    images by digest (`ArtworkView.distinct`, #155). Handing back an album's own
    picture would make the archive's row point at that album's popover, captioned
    with the tracks carrying it — which is right for one image in two places and
    nonsense as a demonstration of a cover from somewhere else. Trailing bytes
    after a JPEG's end marker are ignored by every decoder and change nothing but
    the digest, which is all that is wanted here.
    """
    from harmonist import cover_art

    placeholder = ASSETS_DIR / "cover-3.jpg"
    if not placeholder.exists():
        return None
    data = placeholder.read_bytes() + b"demo-cover-art-archive"
    return cover_art.cache_image(release_mbid, data, "image/jpeg")


def install(monkeypatch):
    for name in (
        "LIBRARY",
        "PENDING_PURCHASES",
        "MB_RELEASES",
        "MERGED_INTO",
        "URL_RELS",
        "PURCHASE_ITEM_IDS",
        "_materialise",
        "_embed_art",
        "front_image",
        "check_front",
        "fetch_image",
    ):
        monkeypatch.setattr(demo, name, globals()[name])
