"""Demo mode — sandboxed sample library + mocked external services.

When `HARMONIST_DEMO_MODE=1` is set:
  * A small, established library mixes older rips and downloads, healthy albums,
    focused metadata improvements, and purchases to link or download.
  * HARMONIST_DEMO_ADOPTION=1 starts those files without Harmonist sidecars.
  * HARMONIST_DEMO_DELAY=0 disables presentation pacing.
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
import os
import shutil
import time
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mutagen.flac import FLAC
from mutagen.id3 import COMM, ID3, TCON
from mutagen.mp4 import MP4

from . import (
    activity,
    activity_store,
    audit,
    cover_art,
    formats,
    id_registry,
    images,
    mb_cache,
    mb_lookup,
    pending_downloads,
    redownloads,
)
from . import sidecar as sidecar_mod
from .formats.m4a import (
    ATOM_COMMENT,
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
_SEEDING: ContextVar[bool] = ContextVar("demo_seeding", default=False)
_WRITE_TAGS = formats.write_tags


def _paced_write_tags(path: Path, tagset: formats.TagSet, cover: bytes | None) -> dict[str, Any]:
    result = _WRITE_TAGS(path, tagset, cover)
    if not _SEEDING.get() and any((parent / DEMO_MARKER).exists() for parent in path.parents):
        time.sleep(_delay())
    return result


def _delay() -> float:
    try:
        return min(2.0, max(0.0, float(os.environ.get("HARMONIST_DEMO_DELAY", "0.25"))))
    except ValueError:
        return 0.25


AUDIO_ASSETS = {
    "alac": "sine.m4a",
    "aac-hi": "sine-aac.m4a",
    "mp3-320": "sine.mp3",
    "mp3-v0": "sine.mp3",
    "mp3": "sine.mp3",
    "flac": "sine.flac",
    "opus": "sine.opus",
}


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


# A small collection accumulated across downloads and CD rips. Each difference
# has a purpose; unmentioned fields start exactly as MusicBrainz would write.
LIBRARY: list[dict[str, Any]] = [
    {
        "artist": "Wyld Stallion",
        "album": "A Most Excellent Journey",
        "tracks": [
            "Be Excellent To Each Other",
            "Party On Dudes",
            "Strange Things Are Afoot at the Circle K",
        ],
        "mbid": "demo-rel-wyld",
        "cover": "wyld.png",
        "fmt": "alac",
        "history": True,
        "store": "https://wyldstallion.bandcamp.com/album/a-most-excellent-journey",
        "item_id": None,
    },
    {
        "artist": "Sex Bob-omb",
        "album": "We Are Here To Make You Sad",
        "tracks": ["Garbage Truck", "Threshold", "Summertime"],
        "mbid": "demo-rel-sex-bob-omb",
        "cover": "sex-bob-omb.png",
        "fmt": "flac",
        "track_tags": {1: {"title": "GARBAGE TRUCK"}, 2: {"title": "Threshold "}},
        "store": "https://sexbobomb.bandcamp.com/album/we-are-here-to-make-you-sad",
        "item_id": 1001,
    },
    {
        "artist": "Sonic Death Monkey",
        "album": "Top 5 Records For A Wednesday",
        "tracks": [
            "Top 5 Side One Track Ones",
            "Top 5 Songs About Death",
            "Top 5 Tracks For Lovers In Trouble",
        ],
        "mbid": "demo-rel-sonic-death-monkey",
        "cover": "sonic.png",
        "fmt": "mp3",
        "unmatched": True,
    },
    {
        "artist": "The Thamesmen",
        "album": "Gimme Some Money",
        "tracks": ["Gimme Some Money", "(Listen to the) Flower People", "Cups and Cakes"],
        "mbid": "demo-rel-thamesmen",
        "cover": "thamesmen.png",
        "fmt": "alac",
        "unmatched": True,
        "candidate": True,
        "store": "https://thamesmen.bandcamp.com/album/gimme-some-money",
        "item_id": 1002,
    },
    {
        "artist": "Dingoes Ate My Baby",
        "album": "Little Bit o' Hoot, Whole Lotta Nanny",
        "tracks": ["Pavlov's Bell", "Hellmouth Lullaby", "Cordelia's Theme"],
        "mbid": "demo-rel-dingoes",
        "cover": "dingoes.png",
        "fmt": "alac",
        "art": "gaps",
        "store": "https://dingoes.bandcamp.com/album/little-bit-o-hoot",
        "item_id": 1004,
    },
    {
        "artist": "Barry Jive and the Uptown Five",
        "album": "After Hours",
        "tracks": ["Last Orders", "Wednesday Night", "One More for the Road"],
        "mbid": "demo-rel-barryjive",
        "cover": "barry.png",
        "archive_cover": "barry-archive.png",
        "lengths_ms": [1000, 2500, 3000],
        "fmt": "flac",
        "history": True,
        "personal": True,
    },
    {
        "artist": "Various Artists",
        "album": "The Rural Juror (OST)",
        "tracks": [
            "Main Title (The Rural Juror)",
            "Urban Fervor",
            "Closing Credits (Urinal Gerber)",
        ],
        "mbid": "demo-rel-rural-juror",
        "cover": "juror.png",
        "track_cover": "juror-track.png",
        "fmt": "alac",
        "credits": ["Jenna Maroney", "Frank Rossitano & Toofer", "Jenna Maroney"],
        "track_tags": {2: {"artist": "Frank Rossitano | Toofer"}},
    },
    {
        "artist": "Dr. Teeth and the Electric Mayhem",
        "album": "Can You Picture That?",
        "tracks": [
            "Can You Picture That?",
            "Mahna Mahna",
            "Movin' Right Along",
            "Rainbow Connection",
        ],
        "mbid": "demo-rel-electric-mayhem",
        "cover": "mayhem.png",
        "fmt": "flac",
        "discs": True,
    },
    {
        "artist": "The Soggy Bottom Boys",
        "album": "Man of Constant Sorrow",
        "tracks": ["Man of Constant Sorrow", "In the Jailhouse Now"],
        "mbid": "demo-rel-soggy",
        "file_id": "demo-rel-soggy-dupe",
        "cover": "soggy.png",
        "fmt": "flac",
    },
    {
        "artist": "Stillwater",
        "album": "Fever Dog",
        "tracks": ["Fever Dog", "Love Thing", "Chelsea Hotel"],
        "mbid": "demo-rel-fever-std",
        "cover": "stillwater.png",
        "fmt": "mp3",
        # Earlier metadata counted a fourth track; MB now has the three files
        # actually owned. Known track identities survive assignment review.
        "tags": {"track_total": 4},
    },
    {
        "artist": "Mouse Rat",
        "album": "The Awesome Album",
        "tracks": ["5000 Candles in the Wind", "The Pit", "Sex Hair"],
        "mbid": "demo-rel-mouserat",
        "archive_cover": "mouse-archive.png",
        "cover": "mouse.png",
        "fmt": "mp3",
        "personal": True,
    },
    {
        "artist": "The Blues Brothers",
        "album": "Rawhide",
        "tracks": ["Rawhide", "Stand By Your Man", "Minnie the Moocher"],
        "mbid": "demo-rel-blues-brothers",
        "cover": "blues.png",
        "fmt": "flac",
        "tags": {"label": [], "catalog_number": [], "date": "2024"},
    },
]

PENDING_PURCHASES: list[dict[str, Any]] = [
    {
        "artist": "Mouse Rat",
        "album": "The Awesome Album",
        "tracks": ["5000 Candles in the Wind", "The Pit", "Sex Hair"],
        "mbid": "demo-rel-mouserat",
        "archive_cover": "mouse-archive.png",
        "cover": "mouse.png",
        "store": "https://mouserat.bandcamp.com/album/the-awesome-album",
        "item_id": 2003,
    },
    {
        "artist": "CB4",
        "album": "Straight Outta Lowcash",
        "tracks": ["Straight Outta Lowcash", "M-O-N-E-Y", "The Real Thing"],
        "mbid": "demo-rel-cb4",
        "cover": "cb4.png",
        "store": "https://cb4.bandcamp.com/album/straight-outta-lowcash",
        "item_id": 2001,
    },
    {
        "artist": "Autobahn",
        "album": "Nagelbett",
        "tracks": ["Karl Hungus", "Marmot Shall Inherit", "Ve Believe in Nuthing"],
        "mbid": "demo-rel-autobahn",
        "cover": "autobahn.png",
        "store": "https://autobahn.bandcamp.com/album/nagelbett",
        "item_id": 2002,
    },
]


def _catalogue() -> dict[str, Release]:
    releases = {}
    for spec in [*LIBRARY, *PENDING_PURCHASES]:
        release = _release(
            spec["mbid"],
            spec["artist"],
            spec["album"],
            spec["tracks"],
            lengths_ms=[6000, 7000, 5500] if spec.get("candidate") else spec.get("lengths_ms"),
            track_artists=spec.get("credits"),
        )
        if spec.get("discs"):
            tracks = release["medium-list"][0]["track-list"]
            release["medium-list"] = [
                {
                    "position": str(disc),
                    "format": "CD",
                    "track-list": [
                        {**track, "position": str(i)} for i, track in enumerate(part, 1)
                    ],
                }
                for disc, part in enumerate((tracks[:2], tracks[2:]), 1)
            ]
        releases[spec["mbid"]] = release
        spec["sidecar"] = {
            "mb_release_id": None
            if spec.get("unmatched") or spec in PENDING_PURCHASES
            else spec.get("file_id", spec["mbid"]),
            "tagged": not spec.get("unmatched") and spec not in PENDING_PURCHASES,
        }
        if spec.get("store"):
            spec["sidecar"].update(store_url=spec["store"], bandcamp_item_id=spec["item_id"])
        if spec.get("candidate"):
            spec["sidecar"]["mb_match_candidate"] = {
                "mb_release_id": spec["mbid"],
                "confidence": "approximate",
                "deltas_ms": [5000, 6000, 4500],
            }
    return releases


MB_RELEASES = _catalogue()
MERGED_INTO = {"demo-rel-soggy-dupe": "demo-rel-soggy"}
URL_RELS = {s["store"]: s["mbid"] for s in [*LIBRARY, *PENDING_PURCHASES] if s.get("store")}
PURCHASE_ITEM_IDS = {
    s["store"]: s["item_id"]
    for s in [*LIBRARY, *PENDING_PURCHASES]
    if s.get("store") and s.get("item_id")
}
PURCHASE_ITEM_IDS[LIBRARY[0]["store"]] = 1000

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
        [
            3,
            os.environ.get("HARMONIST_DEMO_ADOPTION", "0"),
            LIBRARY,
            PENDING_PURCHASES,
            MB_RELEASES,
            URL_RELS,
            MERGED_INTO,
            PURCHASE_ITEM_IDS,
        ],
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


def seed(music_dir: Path, *, persistent_history: bool = False) -> None:
    """Populate music_dir with the demo library + mark the dir as demo."""
    music_dir.mkdir(parents=True, exist_ok=True)
    if persistent_history:
        activity_store.init(music_dir / ".demo-activity.db")
    for spec in LIBRARY:
        if os.environ.get("HARMONIST_DEMO_ADOPTION") == "1":
            spec = {**spec, "sidecar": None, "history": False}
        token = _SEEDING.set(True)
        try:
            _materialise(music_dir, spec)
            candidate = (spec.get("sidecar") or {}).get("mb_match_candidate")
            if candidate:
                # This fixture represents a lookup already performed. Preserve
                # its full answer as well as the sidecar suggestion, so ordinary
                # read-only review can display it without a network request.
                release = fetch_release(candidate["mb_release_id"])
                activity_store.store_release(
                    str(release["id"]), mb_cache._key(mb_lookup.RELEASE_INCLUDES), release
                )
        finally:
            _SEEDING.reset(token)
    (music_dir / DEMO_MARKER).write_text(
        f"Harmonist demo data — safe to delete.\nversion: {data_version()}\n"
    )
    pending_downloads.reset()
    # A re-download awaited against the OLD library refers to an album this
    # re-seed has just recreated from scratch (#132) — leaving the card up would
    # have the inbox waiting forever for something already there.
    redownloads.reset()


def reset(music_dir: Path, *, persistent_history: bool = False) -> None:
    """Wipe music_dir contents (refuses unless demo marker is present), then re-seed."""
    if music_dir.exists() and any(music_dir.iterdir()) and not is_demo_dir(music_dir):
        raise RuntimeError(
            f"refusing to reset {music_dir}: not a demo dir (no {DEMO_MARKER} marker)"
        )
    activity_store.init_memory()
    if music_dir.exists():
        for child in music_dir.iterdir():
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
    seed(music_dir, persistent_history=persistent_history)


def ensure_seeded(music_dir: Path, *, persistent_history: bool = False) -> bool:
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
            reset(music_dir, persistent_history=persistent_history)
        elif persistent_history:
            activity_store.init(music_dir / ".demo-activity.db")
        return True
    seed(music_dir, persistent_history=persistent_history)
    return True


def run_demo_sync(
    music_dir: Path,
    *,
    link_only: bool = False,
    download_format: str = "alac",
    max_downloads_per_sync: int = 5,
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

    Every downloaded album goes through `post_download_callback` when supplied,
    exactly as in the real sync: matching and tagging are application work.
    """

    class _Result:
        def __init__(self) -> None:
            self.new_items_downloaded = False
            self.new_items = 0
            self.unmatched: list[tuple[int, str, str]] = []

        def unmatched_purchases(self) -> list[tuple[int, str, str]]:
            return self.unmatched

    if download_format not in AUDIO_ASSETS:
        raise ValueError(f"Demo downloads support: {', '.join(AUDIO_ASSETS)}")

    def download(spec: dict[str, Any]) -> None:
        _download(
            music_dir, {**_as_fresh_download(spec), "fmt": download_format}, progress_callback
        )
        if post_download_callback:
            # Match the real Bandcamp hook: one tagging action per album,
            # separate from its download, with all tracks sharing one Undo.
            with activity_store.action():
                post_download_callback(music_dir / _safe(spec["artist"]) / _safe(spec["album"]))

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

    pending: list[PendingPurchase] = []
    for purchase in unmatched:
        iid = _purchase_item_id(purchase)
        folder = music_dir / _safe(purchase["artist"]) / _safe(purchase["album"])
        exists = folder.exists() and any(formats.is_supported(p) for p in folder.rglob("*"))
        # A purchase with an existing unlinked copy is a linking decision even
        # during a forced full sync. Never write new files into that album.
        approved = pending_downloads.is_approved(iid)
        if exists or (not approved and (link_only or result.new_items >= max_downloads_per_sync)):
            pending.append(
                PendingPurchase(
                    item_id=iid,
                    band=purchase["artist"],
                    title=purchase["album"],
                    url=purchase["sidecar"]["store_url"],
                    fmt=download_format,
                )
            )
        else:
            download(purchase)
            result.new_items_downloaded = True
            result.new_items += 1
    pending_downloads.replace_all(pending)

    for spec in _archived_redownloads(on_disk):
        download(spec)
        result.new_items_downloaded = True
        result.new_items += 1
    return result


def _archived_redownloads(on_disk: set[int]) -> list[dict[str, Any]]:
    """Seeded albums the user re-downloaded: approved, and no longer on disk
    because the re-download archived them away (#132)."""
    out = []
    for spec in LIBRARY:
        sc_spec = spec.get("sidecar") or {}
        iid = sc_spec.get("bandcamp_item_id") or PURCHASE_ITEM_IDS.get(sc_spec.get("store_url"))
        if iid and iid not in on_disk and pending_downloads.is_approved(int(iid)):
            out.append({**spec, "sidecar": {**sc_spec, "bandcamp_item_id": iid}})
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
    return {
        **spec,
        "sidecar": sidecar,
        "file_mbid": None,
        "file_mbid_tracks": None,
        "fresh": True,
        "history": False,
        "art": None,
    }


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
    time.sleep(0 if _delay() == 0 else STEP_DELAY_SECONDS)
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
            fmt=spec.get("fmt", "alac"),
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
        time.sleep(0 if _delay() == 0 else STEP_DELAY_SECONDS)
    return patched


# ---------------------------------------------------------------------------
# Mock service implementations (monkey-patched into mb_lookup / mb_search /
# cover_art at install() time)
# ---------------------------------------------------------------------------


def fetch_release(mbid: str) -> Release:
    # The redirect a merged id gets on the real service (#268). Rebound rather
    # than looked up separately so everything below — including the release's own
    # `id`, which is what tells the caller a merge happened — comes from the
    # surviving release, exactly as it does over the wire.
    mbid = MERGED_INTO.get(mbid, mbid)
    if mbid not in MB_RELEASES:
        # `ReleaseGoneError`, not a bare MBError: in demo the catalogue is the
        # whole world, so an id that is not in it genuinely does not exist —
        # which is precisely what a 404 means against the real service. It is an
        # MBError subclass, so every existing handler is unaffected, and it makes
        # the deleted-release state (#194, #210) reachable in demo mode, where
        # this project expects flows to be exercised.
        from .mb_lookup import ReleaseGoneError

        raise ReleaseGoneError(f"demo: no MB release for {mbid}")
    return {
        **MB_RELEASES[mbid],
        "url-relation-list": [
            {"target": url, "type": "purchase for download"}
            for url, related in URL_RELS.items()
            if related == mbid
        ],
    }


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


def browse_release_group_editions(release_group_mbid: str) -> tuple[list[Release], int]:
    releases = [
        fetch_release(mbid)
        for mbid, rel in MB_RELEASES.items()
        if (rel.get("release-group") or {}).get("id") == release_group_mbid
    ]
    return releases[:100], len(releases)


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
                    "track_count": sum(len(m["track-list"]) for m in rel["medium-list"]),
                    "label": "Demo Records",
                    "catalog_number": "DEMO-001",
                }
            )
        if len(results) >= limit:
            break
    return results


def _archive_asset(release_mbid: str) -> Path | None:
    """One source of bytes for discovery, inspection and accepted writes."""
    canonical = MERGED_INTO.get(release_mbid, release_mbid)
    spec = next(
        (s for s in [*LIBRARY, *PENDING_PURCHASES] if s.get("mbid") == canonical),
        None,
    )
    if spec is None or not spec.get("cover"):
        return None
    return ASSETS_DIR / str(spec.get("archive_cover", spec["cover"]))


def front_image(
    release_mbid: str,
    release_group_mbid: str | None = None,
    *,
    client: Any = None,
) -> cover_art.Front | None:
    """Supply the selected release's image without writing a library cover."""
    path = _archive_asset(release_mbid)
    if path is None:
        return None
    data = path.read_bytes()
    mime = "image/png" if path.suffix == ".png" else "image/jpeg"
    cover_art.cache_image(release_mbid, data, mime)
    return cover_art.Front(data=data, mime=mime)


def fetch_image(release_mbid: str, url: str, *, client: Any = None) -> Path | None:
    """Explicit inspection uses exactly the image described by check_front."""
    front = front_image(release_mbid)
    if front is None:
        return None
    return cover_art.cached_image(release_mbid)


def check_front(
    release_mbid: str,
    *,
    release_group_mbid: str | None = None,
    known: activity_store.CachedCoverArt | None = None,
    keep_if_wider_than: int | None = None,
    client: Any = None,
) -> activity_store.CachedCoverArt:
    """An offline archive answer backed by the exact preview image."""
    time.sleep(_delay())
    path = _archive_asset(release_mbid)
    if path is None:
        return activity_store.CachedCoverArt(fetched_at=datetime.now(UTC))
    data = path.read_bytes()
    size = images.dimensions(data)
    mime = "image/png" if path.suffix == ".png" else "image/jpeg"
    if size and keep_if_wider_than is not None and size.width > keep_if_wider_than:
        cover_art.cache_image(release_mbid, data, mime)
    return activity_store.CachedCoverArt(
        fetched_at=datetime.now(UTC),
        etag=hashlib.sha256(data).hexdigest(),
        image_url=f"https://coverartarchive.org/release/{release_mbid}/front",
        width=size.width if size else None,
        height=size.height if size else None,
        length=len(data),
        mime=mime,
        source="release",
    )


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
    mb_lookup.browse_release_group_editions = browse_release_group_editions
    mb_search.search_releases = search_releases
    cover_art.front_image = front_image
    cover_art.check_front = check_front
    cover_art.fetch_image = fetch_image
    formats.write_tags = _paced_write_tags
    log.info("demo mode: monkey-patched mb_lookup, mb_search, cover_art")


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _safe(name: str) -> str:
    """Filesystem-safe version of `name` — strips slashes and other path bombs."""
    return name.replace("/", "_").replace(":", "_").strip()


def _personal_tags(path: Path, comment: str) -> None:
    """User-owned tags: deliberately outside Harmonist's metadata contract."""
    if path.suffix == ".m4a":
        audio = MP4(path)
        audio[ATOM_COMMENT] = [comment]
        audio["\xa9gen"] = ["Personal favourites"]
        audio.save()
    elif path.suffix == ".flac":
        audio = FLAC(path)
        audio["comment"] = [comment]
        audio["genre"] = ["Personal favourites"]
        audio.save()
    elif path.suffix == ".mp3":
        tags = ID3(path)
        tags.add(COMM(encoding=3, lang="eng", desc="", text=[comment]))
        tags.add(TCON(encoding=3, text=["Personal favourites"]))
        tags.save(path)


def _materialise(music_dir: Path, spec: dict[str, Any]) -> None:
    """Create valid audio files, complete baseline tags, then intentional drift."""
    from . import tagger

    album_dir = music_dir / _safe(spec["artist"]) / _safe(spec["album"])
    release = MB_RELEASES[spec["mbid"]]
    baseline = tagger.tagsets_for(release, frozenset())
    fixture = ASSETS_DIR / AUDIO_ASSETS[spec.get("fmt", "alac")]
    folders: set[Path] = set()
    for i, tags in enumerate(baseline, 1):
        folder = album_dir / f"CD{tags.disc_num}" if spec.get("discs") else album_dir
        folder.mkdir(parents=True, exist_ok=True)
        folders.add(folder)
        target = folder / f"{tags.track_num:02d} {_safe(tags.title)}{fixture.suffix}"
        shutil.copy(fixture, target)
        # Downloads and unidentified rips carry descriptive tags, not invented MB IDs.
        if spec.get("unmatched") or spec.get("fresh"):
            tags = formats.TagSet(
                mb_album_id="",
                album=spec["album"],
                album_artist=spec["artist"],
                title=tags.title,
                artist=tags.artist,
                track_num=tags.track_num,
                track_total=tags.track_total,
            )
        else:
            tags = replace(
                tags, mb_album_id=spec.get("file_id", tags.mb_album_id), **spec.get("tags", {})
            )
            tags = replace(tags, **spec.get("track_tags", {}).get(i, {}))
        formats.write_tags(target, tags, None)
        if spec.get("personal") or spec.get("store"):
            _personal_tags(target, spec.get("store", "Collected over the years; keep my notes."))

    for folder in sorted(folders):
        cover_name = spec.get("cover")
        if cover_name:
            shutil.copy(ASSETS_DIR / cover_name, folder / f"cover{Path(cover_name).suffix}")
        _embed_art(folder, spec, cover_name)
        if (sc_spec := spec.get("sidecar")) is not None:
            sidecar_mod.write(folder, _build_sidecar(sc_spec, spec))

    if spec.get("history") and not spec.get("fresh") and spec.get("sidecar") is not None:
        # A real earlier update, with genuine before/after values and an undo.
        files = sorted(p for folder in folders for p in folder.iterdir() if formats.is_supported(p))
        for path, tags in zip(files, baseline, strict=True):
            formats.write_tags(path, replace(tags, date="2024"), None)
        with activity_store.action():
            tagger.tag_album(album_dir, release, files=files)
            activity.info(
                "Updated tags from MusicBrainz",
                album_id=sidecar_mod.album_id_for(album_dir),
                album_label=f"{spec['artist']} — {spec['album']}",
            )


def _embed_art(album_dir: Path, spec: dict[str, Any], cover_name: str | None) -> None:
    """Embed the sleeve, leaving one deliberate gap on the featured album."""
    if cover_name is None:
        return
    data = (ASSETS_DIR / cover_name).read_bytes()
    files = sorted(p for p in album_dir.iterdir() if formats.is_supported(p))
    for i, path in enumerate(files):
        if spec.get("art") == "gaps" and i == 1:
            continue
        image = (
            (ASSETS_DIR / spec["track_cover"]).read_bytes()
            if spec.get("track_cover") and i == len(files) - 1
            else data
        )
        formats.write_cover(path, image)


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
