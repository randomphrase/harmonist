"""Demo mode — sandboxed sample library + mocked external services.

When `HARMONIST_DEMO_MODE=1` is set:
  * The music dir is seeded once with a curated set of demo albums covering
    every Album state — including a mis-tag and a non-Bandcamp-comment album —
    plus owned "Bandcamp purchases" that surface as potential downloads in a
    link-only sync (one recovered via the fuzzy library match).
  * The MB lookup, MB search, Cover Art Archive, and Bandcamp sync layers
    are monkey-patched to return canned demo data — no real network calls.
  * `/demo/reset` wipes the music dir and re-seeds it.

A `.harmonist-demo` marker file is written at seed time. Reset refuses to
run unless that marker is present, as a safety guard against pointing demo
mode at a real music library.

All demo-only code lives in this single module. Nothing in `demo.py` is
imported in the non-demo runtime path.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import shutil
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mutagen.mp4 import MP4

from . import activity, activity_store, audit, id_registry, pending_downloads, redownloads
from . import sidecar as sidecar_mod
from .formats.m4a import (
    ATOM_ALBUM,
    ATOM_ARTIST,
    ATOM_COMMENT,
    ATOM_MB_ALBUM_ID,
    ATOM_TITLE,
    ATOM_TRACK_NUM,
)
from .models import (
    SIDECAR_FILENAME,
    BandcampInfo,
    MatchCandidate,
    Release,
    Sidecar,
    TrackComparison,
)
from .pending_downloads import PendingPurchase

log = logging.getLogger(__name__)


DEMO_MARKER = ".harmonist-demo"
ASSETS_DIR = Path(__file__).parent / "_demo_assets"
SINE = ASSETS_DIR / "sine.m4a"

# Wall-clock delay between sync steps in demo mode. Lets the UI render
# the "running" state and intermediate progress messages so the user
# can see what sync is doing. Tests monkeypatch to 0.
STEP_DELAY_SECONDS = 0.6


# ---------------------------------------------------------------------------
# Demo dataset
# ---------------------------------------------------------------------------
#
# Every album spec is a dict so it serialises cleanly to JSON if we ever
# want to externalise. Fields:
#   artist, album, tracks: track titles
#   cover: filename in _demo_assets/; None for an album that deliberately has no
#     artwork at all (the Library's No-artwork filter needs one to find, #174)
#   file_mbid: optional MB Album Id atom on each .m4a (so reconcile works)
#   file_mbid_tracks: optional [track_number, …] — write `file_mbid` to ONLY these
#     tracks, leaving the rest bare. Produces a partially tagged album: COMPLETE,
#     because a file missing the atom neither counts as tagged nor votes on
#     consistency, yet plainly unfinished (#174)
#   file_comment: optional ©cmt value on each .m4a (Bandcamp evidence)
#   file_tags: optional {atom: value} written to every track — used to make an
#     album's tags DISAGREE with its MB release, so the album page's comparison
#     has something to show (#106)
#   file_tags_track_one: same, but track 1 only, so the tracks disagree with
#     each other and the "2 of 3" consensus pill has a case
#   file_track_tags: optional {track_number: {atom: value}} — per-track drift, so
#     the tracklist comparison has real per-track differences to show (#135)
#   sidecar: optional sidecar spec (None → NEW state, {} → empty sidecar)
#
# Sidecar spec keys mirror the Sidecar dataclass; `mb_match_candidate` if
# present is filled in with a synthetic side-by-side.

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
) -> Release:
    """One MusicBrainz release, as the demo's stubbed client returns it.

    `track_artists` gives a track its own artist credit, the way a compilation
    or an OST really does. Without it every track inherits the release credit,
    and the tracklist comparison's per-track Artist column can only ever agree —
    which makes the divergence that matters most (#106 names it) unreachable in
    the very mode people meet the feature in.
    """
    if lengths_ms is None:
        lengths_ms = [1000] * len(tracks)
    credits = track_artists or [artist] * len(tracks)
    return {
        "id": mbid,
        "title": title,
        "disambiguation": disambiguation,
        "status": "Official",
        "country": "US",
        "date": "2024-01-01",
        "barcode": None,
        "artist-credit": [
            {"artist": {"id": f"demo-art-{mbid}", "name": artist}, "name": artist},
        ],
        "release-group": {
            "id": rg or f"demo-rg-{mbid}",
            "primary-type": "Album",
        },
        "label-info-list": [
            {"label": {"name": "Demo Records"}, "catalog-number": "DEMO-001"},
        ],
        "medium-list": [
            {
                "position": "1",
                "format": "Digital Media",
                "track-list": [
                    {
                        "id": f"demo-rt-{mbid}-{i}",
                        "position": str(i),
                        "title": title,
                        "artist-credit": [
                            {
                                "artist": {"id": f"demo-art-{mbid}-{i}", "name": credit},
                                "name": credit,
                            },
                        ],
                        "recording": {
                            "id": f"demo-rec-{mbid}-{i}",
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


# ---------------------------------------------------------------------------
# Seed / reset / sync
# ---------------------------------------------------------------------------


def is_demo_dir(music_dir: Path) -> bool:
    return (music_dir / DEMO_MARKER).exists()


def data_version() -> str:
    """Short hash of the current demo dataset. Used to detect stale on-disk
    demo data after a code update that changed LIBRARY/MB_RELEASES/etc.
    """
    payload = json.dumps(
        [LIBRARY, PENDING_PURCHASES, list(MB_RELEASES.keys()), URL_RELS],
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def _marker_version(music_dir: Path) -> str | None:
    """Parse the data-version line out of the marker file, or None if absent."""
    marker = music_dir / DEMO_MARKER
    if not marker.exists():
        return None
    text = marker.read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.startswith("version:"):
            return line.split(":", 1)[1].strip()
    return None


def seed(music_dir: Path) -> None:
    """Populate music_dir with the demo library + mark the dir as demo."""
    music_dir.mkdir(parents=True, exist_ok=True)
    for spec in LIBRARY:
        _materialise(music_dir, spec)
    (music_dir / DEMO_MARKER).write_text(
        f"Harmonist demo data — safe to delete.\nversion: {data_version()}\n"
    )
    pending_downloads.replace_all([])
    # A re-download awaited against the OLD library refers to an album this
    # re-seed has just recreated from scratch (#132) — leaving the card up would
    # have the inbox waiting forever for something already there.
    redownloads.reset()


def reset(music_dir: Path) -> None:
    """Wipe music_dir contents (refuses unless demo marker is present), then re-seed."""
    if music_dir.exists() and any(music_dir.iterdir()) and not is_demo_dir(music_dir):
        raise RuntimeError(
            f"refusing to reset {music_dir}: not a demo dir (no {DEMO_MARKER} marker)"
        )
    if music_dir.exists():
        for child in music_dir.iterdir():
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
    seed(music_dir)


def ensure_seeded(music_dir: Path) -> bool:
    """Seed once if the dir is empty; auto-reset if seeded against an older
    demo dataset; refuse to overwrite a non-demo dir.

    Returns True if seeding ran or the existing data is current demo data,
    False if we refused because the dir holds non-demo content.
    """
    if music_dir.exists() and any(music_dir.iterdir()):
        if not is_demo_dir(music_dir):
            return False
        existing_version = _marker_version(music_dir)
        if existing_version != data_version():
            log.info(
                "demo: data version mismatch (on disk: %s, code: %s) — resetting",
                existing_version,
                data_version(),
            )
            reset(music_dir)
        return True
    seed(music_dir)
    return True


def run_demo_sync(
    music_dir: Path,
    *,
    link_only: bool = False,
    ignores_file: Path | None = None,
    progress_callback: Callable[[str], None] | None = None,
    post_download_callback: Callable[[Path], None] | None = None,
) -> Any:
    """Mirror the real syncer's adoption behaviour:
      1. Link every already-on-disk album whose Bandcamp store_url matches a
         known purchase (fills `bandcamp.item_id`). Mirrors sync_item's
         existing-on-disk path — drains "Needs Link".
      2. The owned purchases with no on-disk copy (by item_id) and not skipped
         are the residue. In **link-only** mode they surface as POTENTIAL
         DOWNLOADS for review (download nothing); any the user already approved
         (clicked Download) are fetched. In a **full** sync they all download.
      3. Bring back any LIBRARY album archived off disk by a re-download (#132):
         an approved purchase that is one of the seeded albums rather than one of
         the pending ones. Like the real path, an approval bypasses link-only.
    Returns a stub matching the attributes the sync runner introspects — including
    `unmatched_purchases()`, which the post-sync mis-tag detection reads.

    `post_download_callback` is invoked for **re-downloads only**. The pending
    purchases' fixtures already carry their release, so they land tagged with no
    resolution needed; a re-download must go through the real callback because
    that is the thing being demonstrated — it is what tags the replacement as the
    release the archived copy had, and a demo that skipped it would show the right
    outcome while proving nothing about the code that produces it.
    """

    class _Result:
        def __init__(self) -> None:
            self.new_items_downloaded = False
            self.unmatched: list[tuple[int, str, str]] = []

        def unmatched_purchases(self) -> list[tuple[int, str, str]]:
            return self.unmatched

    result = _Result()
    _fill_in_existing_item_ids(music_dir, progress_callback=progress_callback)

    on_disk = _on_disk_item_ids(music_dir)
    ignored = _read_ignored_ids(ignores_file)
    # Owned purchases (by known URL→id) not linked on any sidecar — the input to
    # mis-tag detection (e.g. the Fever Dog live edition the album isn't tagged as).
    result.unmatched = [
        (iid, url, _purchase_label(url))
        for url, iid in PURCHASE_ITEM_IDS.items()
        if iid not in on_disk
    ]

    unmatched = [
        p
        for p in PENDING_PURCHASES
        if _purchase_item_id(p) not in on_disk and _purchase_item_id(p) not in ignored
    ]

    if link_only:
        pending: list[PendingPurchase] = []
        for p in unmatched:
            iid = _purchase_item_id(p)
            if pending_downloads.is_approved(iid):
                _download(music_dir, p, progress_callback)
                result.new_items_downloaded = True
            else:
                pending.append(
                    PendingPurchase(
                        item_id=iid,
                        band=p["artist"],
                        title=p["album"],
                        url=p["sidecar"]["store_url"],
                        fmt="alac",
                    )
                )
        pending_downloads.replace_all(pending)
    else:
        for p in unmatched:
            _download(music_dir, p, progress_callback)
            result.new_items_downloaded = True
        pending_downloads.replace_all([])

    for spec in _archived_redownloads(on_disk):
        _download(music_dir, _as_fresh_download(spec), progress_callback)
        result.new_items_downloaded = True
        if post_download_callback:
            album_dir = music_dir / _safe(spec["artist"]) / _safe(spec["album"])
            with contextlib.suppress(Exception):
                post_download_callback(album_dir)
    return result


def _archived_redownloads(on_disk: set[int]) -> list[dict[str, Any]]:
    """Seeded albums the user re-downloaded: approved, and no longer on disk
    because the re-download archived them away (#132)."""
    out = []
    for spec in LIBRARY:
        sc_spec = spec.get("sidecar") or {}
        iid = sc_spec.get("bandcamp_item_id")
        if iid and iid not in on_disk and pending_downloads.is_approved(int(iid)):
            out.append(spec)
    return out


def _as_fresh_download(spec: dict[str, Any]) -> dict[str, Any]:
    """The same album as it arrives from Bandcamp: audio and a purchase link, no
    MusicBrainz anything.

    The seeded spec describes the album in its *settled* state — tagged, with a
    release on the sidecar and an MBID atom on the files. A download has none of
    that yet; the tagging happens afterwards, in the app. Handing the settled
    spec back would leave nothing for the re-download's tagging to do, and the
    demo would show a working feature whatever the code did."""
    sidecar = {
        k: v
        for k, v in (spec.get("sidecar") or {}).items()
        if k not in ("mb_release_id", "tagged", "mb_match_candidate")
    }
    return {**spec, "sidecar": sidecar, "file_mbid": None, "file_mbid_tracks": None}


def _purchase_label(url: str) -> str:
    """ "Artist / Title" for a purchase URL (from the mocked MB release), for the
    mis-tag detection's activity line. Falls back to the URL."""
    mbid = URL_RELS.get(url)
    rel = MB_RELEASES.get(mbid) if mbid else None
    if rel:
        artist = (rel.get("artist-credit") or [{}])[0].get("name", "?")
        return f"{artist} / {rel.get('title', '?')}"
    return url


def _download(
    music_dir: Path, spec: dict[str, Any], progress_callback: Callable[[str], None] | None
) -> None:
    """Materialise one purchase as a freshly-downloaded album."""
    if progress_callback:
        with contextlib.suppress(Exception):
            progress_callback(f"{spec['artist']} / {spec['album']}")
    time.sleep(STEP_DELAY_SECONDS)
    album_dir = music_dir / _safe(spec["artist"]) / _safe(spec["album"])
    # Scoped so the sidecar audit _materialise writes attaches to this entry as
    # its "what changed" (#97), and labelled so the entry links to the album.
    with activity_store.action():
        # Mirrors the real download path (bandcamp_hook.sync_item): mint the id
        # BEFORE the sidecar exists, so the `download` row can be attached to the
        # album, and sidecar.write's identity normalisation then adopts the same
        # registry UUID as `temp_uid`. Without this row the demo album's history
        # starts at `sidecar.create` and looks like it appeared from nowhere —
        # demo is where people form their first impression of the feature (#107).
        album_id = id_registry.get_or_mint(album_dir)
        audit.record(
            "download",
            album_id=album_id,
            item_id=_purchase_item_id(spec),
            fmt="alac",
            path=album_dir,
        )
        _materialise(music_dir, spec)
        activity.info(
            "Downloaded from Bandcamp",
            album_id=sidecar_mod.album_id_for(album_dir),
            album_label=f"{spec['artist']} — {spec['album']}",
        )


def _purchase_item_id(spec: dict[str, Any]) -> int:
    return int(spec["sidecar"]["bandcamp_item_id"])


def _on_disk_item_ids(music_dir: Path) -> set[int]:
    """item_ids already linked on any on-disk sidecar (mirrors library_index)."""
    ids: set[int] = set()
    if not music_dir.exists():
        return ids
    for harmonist_json in music_dir.rglob(SIDECAR_FILENAME):
        try:
            sc = sidecar_mod.read(harmonist_json.parent)
        except Exception:
            continue
        if sc and sc.bandcamp and sc.bandcamp.item_id is not None:
            ids.add(int(sc.bandcamp.item_id))
    return ids


def _read_ignored_ids(ignores_file: Path | None) -> set[int]:
    """item_ids the user chose "Don't download" (appended to ignores.txt)."""
    if not ignores_file:
        return set()
    try:
        text = Path(ignores_file).read_text(encoding="utf-8")
    except Exception:
        return set()
    ids: set[int] = set()
    for line in text.splitlines():
        token = line.split("#", 1)[0].strip()
        if token.isdigit():
            ids.add(int(token))
    return ids


def _fill_in_existing_item_ids(
    music_dir: Path, *, progress_callback: Callable[[str], None] | None = None
) -> int:
    """For each existing album whose store_url is a known demo purchase
    and whose bandcamp.item_id is None, patch the sidecar with the
    item_id from PURCHASE_ITEM_IDS. Returns the number patched.
    """
    if not music_dir.exists():
        return 0
    patched = 0
    for harmonist_json in music_dir.rglob(SIDECAR_FILENAME):
        album_dir = harmonist_json.parent
        try:
            sc = sidecar_mod.read(album_dir)
        except Exception:
            continue
        if sc is None or not sc.store_url:
            continue
        item_id = PURCHASE_ITEM_IDS.get(sc.store_url)
        if item_id is None:
            continue
        if sc.bandcamp is not None and sc.bandcamp.item_id is not None:
            continue
        existing_band_id = sc.bandcamp.band_id if sc.bandcamp else None
        new_sc = Sidecar(
            schema_version=sc.schema_version,
            store_url=sc.store_url,
            bandcamp=BandcampInfo(item_id=item_id, band_id=existing_band_id),
            downloaded_at=sc.downloaded_at,
            added_at=sc.added_at,
            mb_release_id=sc.mb_release_id,
            temp_uid=sc.temp_uid,
            mb_match_candidate=sc.mb_match_candidate,
            tagged_at=sc.tagged_at,
            notes=sc.notes,
        )
        with activity_store.action():
            sidecar_mod.write(album_dir, new_sc)
            activity.info(
                "Linked to its Bandcamp purchase",
                album_id=sidecar_mod.album_id_for(album_dir),
                album_label=f"{album_dir.parent.name} — {album_dir.name}",
            )
        patched += 1
        if progress_callback:
            with contextlib.suppress(Exception):
                progress_callback(f"Linked: {album_dir.parent.name} / {album_dir.name}")
        time.sleep(STEP_DELAY_SECONDS)
    return patched


# ---------------------------------------------------------------------------
# Mock service implementations (monkey-patched into mb_lookup / mb_search /
# cover_art at install() time)
# ---------------------------------------------------------------------------


def fetch_release(mbid: str) -> Release:
    if mbid not in MB_RELEASES:
        # `ReleaseGoneError`, not a bare MBError: in demo the catalogue is the
        # whole world, so an id that is not in it genuinely does not exist —
        # which is precisely what a 404 means against the real service. It is an
        # MBError subclass, so every existing handler is unaffected, and it makes
        # the deleted-release state (#194, #210) reachable in demo mode, where
        # this project expects flows to be exercised.
        from .mb_lookup import ReleaseGoneError

        raise ReleaseGoneError(f"demo: no MB release for {mbid}")
    return MB_RELEASES[mbid]


def fetch_release_urls(mbid: str) -> list[str]:
    return [url for url, m in URL_RELS.items() if m == mbid]


def fetch_video_media(mbid: str) -> tuple[int, ...]:
    """Demo counterpart of the video-only-media lookup (#206).

    The seeded catalogue has no video tracks, so every release answers "none are
    video" — which is the honest answer for it, and keeps the request off the
    network like every other demo lookup.
    """
    fetch_release(mbid)  # raises ReleaseGoneError for an id the demo doesn't have
    return ()


def lookup_by_bandcamp_url(bandcamp_url: str) -> list[str]:
    mbid = URL_RELS.get(bandcamp_url)
    return [mbid] if mbid else []


def browse_release_group_releases(release_group_mbid: str) -> list[tuple[str, list[str]]]:
    """Siblings in a release group → [(release_mbid, [bandcamp urls])]. Drives the
    post-sync mis-tag detection (the demo's Fever Dog std ↔ live pairing)."""
    out: list[tuple[str, list[str]]] = []
    for mbid, rel in MB_RELEASES.items():
        if (rel.get("release-group") or {}).get("id") == release_group_mbid:
            out.append((mbid, fetch_release_urls(mbid)))
    return out


def search_releases(artist: str, title: str, limit: int = 10) -> list[dict[str, Any]]:
    a = (artist or "").strip().lower()
    t = (title or "").strip().lower()
    results: list[dict[str, Any]] = []
    for rel in MB_RELEASES.values():
        rel_artist = ""
        for ac in rel.get("artist-credit") or []:
            if isinstance(ac, dict):
                rel_artist = ac.get("name") or ac.get("artist", {}).get("name", "")
                break
        rel_title = rel.get("title", "")
        a_match = (not a) or (a in rel_artist.lower())
        t_match = (not t) or (t in rel_title.lower())
        if a_match and t_match:
            results.append(
                {
                    "id": rel["id"],
                    "title": rel_title,
                    "artist": rel_artist,
                    "date": rel.get("date"),
                    "country": rel.get("country"),
                    "status": rel.get("status"),
                    "track_count": len(rel["medium-list"][0]["track-list"]),
                    "label": "Demo Records",
                    "catalog_number": "DEMO-001",
                }
            )
        if len(results) >= limit:
            break
    return results


def ensure_cover(
    album_dir: Path,
    release_mbid: str = "",
    release_group_mbid: str | None = None,
    size: str = "original",
    *,
    client: Any = None,
) -> Path | None:
    """Demo cover fetcher — returns existing cover.jpg if present, else copies a placeholder."""
    for name in ("cover.jpg", "cover.png"):
        p = album_dir / name
        if p.exists():
            return p
    placeholder = ASSETS_DIR / "cover-7.jpg"  # generic green default
    if placeholder.exists():
        target = album_dir / "cover.jpg"
        shutil.copy(placeholder, target)
        return target
    return None


def install() -> None:
    """Monkey-patch demo implementations into the modules the web routes use.

    Idempotent. Called once at app construction when demo mode is on.
    """
    from . import cover_art, mb_lookup, mb_search

    mb_lookup.fetch_release = fetch_release
    mb_lookup.fetch_release_urls = fetch_release_urls
    mb_lookup.fetch_video_media = fetch_video_media
    mb_lookup.lookup_by_bandcamp_url = lookup_by_bandcamp_url
    mb_lookup.browse_release_group_releases = browse_release_group_releases
    mb_search.search_releases = search_releases
    cover_art.ensure_cover = ensure_cover
    log.info("demo mode: monkey-patched mb_lookup, mb_search, cover_art")


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _safe(name: str) -> str:
    """Filesystem-safe version of `name` — strips slashes and other path bombs."""
    return name.replace("/", "_").replace(":", "_").strip()


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

    # Distinguish "no sidecar" (sentinel None) from "empty sidecar" ({}).
    sc_spec = spec.get("sidecar")
    if sc_spec is not None:
        sidecar_mod.write(album_dir, _build_sidecar(sc_spec, spec))


def _build_sidecar(sc_spec: dict[str, Any], album_spec: dict[str, Any]) -> Sidecar:
    """Build a Sidecar dataclass from a spec-dict.

    Keys recognised:
      store_url, bandcamp_item_id, mb_release_id, tagged,
      mb_match_candidate (nested dict with deltas_ms list).
    """
    now = datetime.now(UTC)
    store_url = sc_spec.get("store_url")
    bandcamp = None
    if "bandcamp_item_id" in sc_spec:
        bandcamp = BandcampInfo(item_id=sc_spec.get("bandcamp_item_id"))

    candidate = None
    if cand_spec := sc_spec.get("mb_match_candidate"):
        deltas = cand_spec.get("deltas_ms", [])
        comparisons = []
        for i, (track_title, delta_ms) in enumerate(
            zip(album_spec["tracks"], deltas, strict=False), start=1
        ):
            mb_len = 1000 + delta_ms  # file is 1000ms; mb is 1000+delta
            comparisons.append(
                TrackComparison(
                    file_name=f"{i:02d} {_safe(track_title)}.m4a",
                    file_duration_ms=1000,
                    file_title=track_title,
                    mb_track_title=track_title,
                    mb_track_length_ms=mb_len,
                    delta_ms=abs(delta_ms),
                )
            )
        candidate = MatchCandidate(
            mb_release_id=cand_spec["mb_release_id"],
            confidence=cand_spec.get("confidence", "approximate"),
            file_count=len(album_spec["tracks"]),
            track_count=len(album_spec["tracks"]),
            track_comparisons=comparisons,
            proposed_at=now,
            notes=cand_spec.get("notes", ["Track lengths differ from MB"]),
        )

    # A mis-tag candidate: the album is really the *owned* edition, but tagged as
    # a sibling in the same release group. Renders in the "Possibly mis-tagged"
    # section; Confirm re-tags to `owned_mbid`.
    if mistag := sc_spec.get("mistag"):
        candidate = MatchCandidate(
            mb_release_id=mistag["owned_mbid"],
            confidence="no_match",
            file_count=len(album_spec["tracks"]),
            track_count=len(album_spec["tracks"]),
            track_comparisons=[
                TrackComparison(
                    file_name=f"{i:02d} {_safe(t)}.m4a",
                    file_duration_ms=1000,
                    file_title=t,
                    mb_track_title=t,
                    mb_track_length_ms=1000,
                    delta_ms=0,
                )
                for i, t in enumerate(album_spec["tracks"], start=1)
            ],
            proposed_at=now,
            mistag_owned_url=mistag["owned_url"],
            mistag_owned_label=mistag["owned_label"],
            mistag_owned_disambig=mistag.get("owned_disambig"),
            mistag_tagged_mbid=mistag["tagged_mbid"],
            mistag_tagged_label=mistag["tagged_label"],
            mistag_tagged_disambig=mistag.get("tagged_disambig"),
            mistag_release_group_mbid=mistag["release_group_mbid"],
        )

    # NOTE: there is deliberately no way to seed a pre-surrendered album (#87).
    # A surrender is a conclusion the sync reaches by paging the whole collection
    # and finding no matching purchase, so faking it at seed time showed the user
    # a verdict Harmonist hadn't reached. The Barry Jive fixture is seeded as an
    # ordinary unlinked album and surrendered by the real post-sync pass instead.

    tagged_at = now if sc_spec.get("tagged") else None

    return Sidecar(
        store_url=store_url,
        bandcamp=bandcamp,
        downloaded_at=(now if store_url else None),
        added_at=(None if store_url else now),
        mb_release_id=sc_spec.get("mb_release_id"),
        mb_match_candidate=candidate,
        tagged_at=tagged_at,
    )
