"""Demo mode — sandboxed sample library + mocked external services.

When `HARMONIST_DEMO_MODE=1` is set:
  * The music dir is seeded once with a curated set of demo albums covering
    every Album state, plus a queue of pending "Bandcamp purchases".
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

import hashlib
import json
import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from mutagen.mp4 import MP4

from . import sidecar as sidecar_mod
from .models import BandcampInfo, MatchCandidate, Sidecar, TrackComparison


log = logging.getLogger(__name__)


DEMO_MARKER = ".harmonist-demo"
ASSETS_DIR = Path(__file__).parent / "_demo_assets"
SINE = ASSETS_DIR / "sine.m4a"


# ---------------------------------------------------------------------------
# Demo dataset
# ---------------------------------------------------------------------------
#
# Every album spec is a dict so it serialises cleanly to JSON if we ever
# want to externalise. Fields:
#   artist, album, tracks: track titles
#   cover: filename in _demo_assets/
#   file_mbid: optional MB Album Id atom on each .m4a (so reconcile works)
#   file_comment: optional ©cmt value on each .m4a (Bandcamp evidence)
#   sidecar: optional sidecar spec (None → Orphan state)
#
# Sidecar spec keys mirror the Sidecar dataclass; `mb_match_candidate` if
# present is filled in with a synthetic side-by-side.

LIBRARY: list[dict] = [
    {
        # State: ORPHAN — no sidecar, but has MBID atom + Bandcamp ©cmt.
        # Reconcile derives a sidecar (transitions to UNCONFIRMED_BANDCAMP).
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
        # State: HELD_BANDCAMP — sidecar with URL but no MB match yet.
        # Recheck looks up MB → exact match → tags → DONE.
        "artist": "Sex Bob-omb",
        "album": "We Are Here To Make You Sad",
        "tracks": ["Garbage Truck", "Threshold", "Summertime"],
        "cover": "cover-2.jpg",
        "sidecar": {
            "source": "bandcamp",
            "bandcamp_url": "https://sexbobomb.bandcamp.com/album/we-are-here-to-make-you-sad",
            "bandcamp_item_id": 1001,
        },
    },
    {
        # State: HELD_MANUAL — non-Bandcamp source, awaiting MBID.
        # Manual ingest form (paste MBID or search MB by name) tags it.
        "artist": "Sonic Death Monkey",
        "album": "Top 5 Records For A Wednesday",
        "tracks": [
            "Top 5 Side One Track Ones",
            "Top 5 Songs About Death",
            "Top 5 Tracks For Lovers In Trouble",
        ],
        "cover": "cover-6.jpg",
        "sidecar": {
            "source": "manual",
        },
    },
    {
        # State: NEEDS_CONFIRMATION — files tagged, candidate stashed but
        # confidence is "approximate" (lengths off). Side-by-side renders.
        # Confirm → tags from MB; Reject → back to Held (Bandcamp).
        "artist": "The Thamesmen",
        "album": "Gimme Some Money",
        "tracks": ["Gimme Some Money", "(Listen to the) Flower People", "Cups and Cakes"],
        "cover": "cover-3.jpg",
        "file_mbid": "demo-rel-thamesmen",
        "sidecar": {
            "source": "bandcamp",
            "bandcamp_url": "https://thamesmen.bandcamp.com/album/gimme-some-money",
            "bandcamp_item_id": 1002,
            "mb_match_candidate": {
                "mb_release_id": "demo-rel-thamesmen",
                "confidence": "approximate",
                "deltas_ms": [5000, 6000, 4500],  # all over the 4s tolerance
            },
        },
    },
    {
        # State: UNCONFIRMED_BANDCAMP — files tagged, source=bandcamp,
        # item_id=None. "Try a different URL" / "Mark purchased elsewhere".
        "artist": "Dingoes Ate My Baby",
        "album": "Little Bit o' Hoot, Whole Lotta Nanny",
        "tracks": ["Pavlov's Bell", "Hellmouth Lullaby", "Cordelia's Theme"],
        "cover": "cover-4.jpg",
        "file_mbid": "demo-rel-dingoes",
        "file_comment": "Visit https://dingoes.bandcamp.com",
        "sidecar": {
            "source": "bandcamp",
            "bandcamp_url": "https://dingoes.bandcamp.com/album/little-bit-o-hoot",
            "bandcamp_item_id": None,
            "mb_release_id": "demo-rel-dingoes",
            "tagged": True,
        },
    },
    {
        # State: DONE — fully tagged & confirmed. Hidden from inbox; counts
        # in the "X total" stat at the top of the page.
        "artist": "Various Artists",
        "album": "The Rural Juror (OST)",
        "tracks": [
            "Main Title (The Rural Juror)",
            "Urban Fervor",
            "Closing Credits (Urinal Gerber)",
        ],
        "cover": "cover-5.jpg",
        "file_mbid": "demo-rel-rural-juror",
        "sidecar": {
            "source": "bandcamp",
            "bandcamp_url": "https://variousartists.bandcamp.com/album/the-rural-juror-ost",
            "bandcamp_item_id": 1003,
            "mb_release_id": "demo-rel-rural-juror",
            "tagged": True,
        },
    },
]


# Pending "Bandcamp purchases" — popped one per Sync click.
PENDING_PURCHASES: list[dict] = [
    {
        "artist": "CB4",
        "album": "Straight Outta Lowcash",
        "tracks": ["Straight Outta Lowcash", "M-O-N-E-Y", "The Real Thing"],
        "cover": "cover-7.jpg",
        "sidecar": {
            "source": "bandcamp",
            "bandcamp_url": "https://cb4.bandcamp.com/album/straight-outta-lowcash",
            "bandcamp_item_id": 2001,
        },
    },
    {
        "artist": "Autobahn",
        "album": "Nagelbett",
        "tracks": ["Karl Hungus", "Marmot Shall Inherit", "Ve Believe in Nuthing"],
        "cover": "cover-8.jpg",
        "sidecar": {
            "source": "bandcamp",
            "bandcamp_url": "https://autobahn.bandcamp.com/album/nagelbett",
            "bandcamp_item_id": 2002,
        },
    },
]


# Synthetic MB releases for everything that has an MBID. Shape mirrors what
# musicbrainzngs returns under release[...]: enough for tagger + assess_match.

def _release(mbid: str, artist: str, title: str, tracks: list[str], lengths_ms: Optional[list[int]] = None) -> dict:
    if lengths_ms is None:
        lengths_ms = [1000] * len(tracks)
    return {
        "id": mbid,
        "title": title,
        "status": "Official",
        "country": "US",
        "date": "2024-01-01",
        "barcode": None,
        "artist-credit": [
            {"artist": {"id": f"demo-art-{mbid}", "name": artist}, "name": artist},
        ],
        "release-group": {
            "id": f"demo-rg-{mbid}",
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
                        "recording": {
                            "id": f"demo-rec-{mbid}-{i}",
                            "title": title,
                            "length": str(length),
                        },
                    }
                    for i, (title, length) in enumerate(zip(tracks, lengths_ms), start=1)
                ],
            }
        ],
    }


MB_RELEASES: dict[str, dict] = {
    "demo-rel-wyld": _release(
        "demo-rel-wyld", "Wyld Stallion", "A Most Excellent Journey",
        ["Be Excellent To Each Other", "Party On Dudes", "Strange Things Are Afoot at the Circle K"],
    ),
    "demo-rel-sex-bob-omb": _release(
        "demo-rel-sex-bob-omb", "Sex Bob-omb", "We Are Here To Make You Sad",
        ["Garbage Truck", "Threshold", "Summertime"],
    ),
    "demo-rel-sonic-death-monkey": _release(
        "demo-rel-sonic-death-monkey", "Sonic Death Monkey", "Top 5 Records For A Wednesday",
        ["Top 5 Side One Track Ones", "Top 5 Songs About Death", "Top 5 Tracks For Lovers In Trouble"],
    ),
    "demo-rel-thamesmen": _release(
        "demo-rel-thamesmen", "The Thamesmen", "Gimme Some Money",
        ["Gimme Some Money", "(Listen to the) Flower People", "Cups and Cakes"],
        lengths_ms=[6000, 7000, 5500],  # off by enough to land "approximate"
    ),
    "demo-rel-dingoes": _release(
        "demo-rel-dingoes", "Dingoes Ate My Baby", "Little Bit o' Hoot, Whole Lotta Nanny",
        ["Pavlov's Bell", "Hellmouth Lullaby", "Cordelia's Theme"],
    ),
    "demo-rel-rural-juror": _release(
        "demo-rel-rural-juror", "Various Artists", "The Rural Juror (OST)",
        ["Main Title (The Rural Juror)", "Urban Fervor", "Closing Credits (Urinal Gerber)"],
    ),
    "demo-rel-cb4": _release(
        "demo-rel-cb4", "CB4", "Straight Outta Lowcash",
        ["Straight Outta Lowcash", "M-O-N-E-Y", "The Real Thing"],
    ),
    "demo-rel-autobahn": _release(
        "demo-rel-autobahn", "Autobahn", "Nagelbett",
        ["Karl Hungus", "Marmot Shall Inherit", "Ve Believe in Nuthing"],
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
}


# ---------------------------------------------------------------------------
# Pending queue (in-memory, resets on restart or /demo/reset)
# ---------------------------------------------------------------------------

_pending_queue: list[dict] = []


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


def _marker_version(music_dir: Path) -> Optional[str]:
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
    global _pending_queue
    _pending_queue = list(PENDING_PURCHASES)


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
                existing_version, data_version(),
            )
            reset(music_dir)
        return True
    seed(music_dir)
    return True


def run_demo_sync(music_dir: Path) -> Any:
    """Pop the next pending purchase and materialise it. Returns a stub
    matching the bandcampsync.Syncer attribute the runner introspects.
    """
    class _Result:
        new_items_downloaded = False

    if not _pending_queue:
        return _Result()
    spec = _pending_queue.pop(0)
    _materialise(music_dir, spec)
    _Result.new_items_downloaded = True
    return _Result()


# ---------------------------------------------------------------------------
# Mock service implementations (monkey-patched into mb_lookup / mb_search /
# cover_art at install() time)
# ---------------------------------------------------------------------------


def fetch_release(mbid: str) -> dict:
    if mbid not in MB_RELEASES:
        from .mb_lookup import MBError
        raise MBError(f"demo: no MB release for {mbid}")
    return MB_RELEASES[mbid]


def fetch_release_urls(mbid: str) -> list[str]:
    return [url for url, m in URL_RELS.items() if m == mbid]


def lookup_by_bandcamp_url(url: str) -> Optional[str]:
    return URL_RELS.get(url)


def search_releases(artist: str, title: str, limit: int = 10) -> list[dict]:
    a = (artist or "").strip().lower()
    t = (title or "").strip().lower()
    results: list[dict] = []
    for mbid, rel in MB_RELEASES.items():
        rel_artist = ""
        for ac in rel.get("artist-credit") or []:
            if isinstance(ac, dict):
                rel_artist = ac.get("name") or ac.get("artist", {}).get("name", "")
                break
        rel_title = rel.get("title", "")
        a_match = (not a) or (a in rel_artist.lower())
        t_match = (not t) or (t in rel_title.lower())
        if a_match and t_match:
            results.append({
                "id": rel["id"],
                "title": rel_title,
                "artist": rel_artist,
                "date": rel.get("date"),
                "country": rel.get("country"),
                "status": rel.get("status"),
                "track_count": len(rel["medium-list"][0]["track-list"]),
                "label": "Demo Records",
                "catalog_number": "DEMO-001",
            })
        if len(results) >= limit:
            break
    return results


def ensure_cover(album_dir: Path, *, release_mbid: str = "", release_group_mbid: Optional[str] = None,
                 size: str = "original", **_kwargs) -> Optional[Path]:
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
    mb_lookup.lookup_by_bandcamp_url = lookup_by_bandcamp_url
    mb_search.search_releases = search_releases
    cover_art.ensure_cover = ensure_cover
    log.info("demo mode: monkey-patched mb_lookup, mb_search, cover_art")


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _safe(name: str) -> str:
    """Filesystem-safe version of `name` — strips slashes and other path bombs."""
    return name.replace("/", "_").replace(":", "_").strip()


def _materialise(music_dir: Path, spec: dict) -> None:
    """Lay out one demo album: dirs + .m4a files (with tags) + cover + sidecar."""
    album_dir = music_dir / _safe(spec["artist"]) / _safe(spec["album"])
    album_dir.mkdir(parents=True, exist_ok=True)

    n_tracks = len(spec["tracks"])
    for i, title in enumerate(spec["tracks"], start=1):
        target = album_dir / f"{i:02d} {_safe(title)}.m4a"
        shutil.copy(SINE, target)
        audio = MP4(target)
        audio["\xa9nam"] = [title]
        audio["\xa9alb"] = [spec["album"]]
        audio["\xa9ART"] = [spec["artist"]]
        audio["trkn"] = [(i, n_tracks)]
        if mbid := spec.get("file_mbid"):
            audio["----:com.apple.iTunes:MusicBrainz Album Id"] = [mbid.encode("utf-8")]
        if cmt := spec.get("file_comment"):
            audio["\xa9cmt"] = [cmt]
        audio.save()

    cover_asset = ASSETS_DIR / spec.get("cover", "cover-7.jpg")
    if cover_asset.exists():
        shutil.copy(cover_asset, album_dir / "cover.jpg")

    if sc_spec := spec.get("sidecar"):
        sidecar_mod.write(album_dir, _build_sidecar(sc_spec, spec))


def _build_sidecar(sc_spec: dict, album_spec: dict) -> Sidecar:
    """Build a Sidecar dataclass from a spec-dict.

    Keys recognised:
      source, bandcamp_url, bandcamp_item_id, mb_release_id, tagged,
      mb_match_candidate (nested dict with deltas_ms list).
    """
    now = datetime.now(timezone.utc)
    bandcamp = None
    if "bandcamp_url" in sc_spec:
        bandcamp = BandcampInfo(
            url=sc_spec["bandcamp_url"],
            item_id=sc_spec.get("bandcamp_item_id"),
        )

    candidate = None
    if cand_spec := sc_spec.get("mb_match_candidate"):
        deltas = cand_spec.get("deltas_ms", [])
        comparisons = []
        for i, (track_title, delta_ms) in enumerate(zip(album_spec["tracks"], deltas), start=1):
            mb_len = 1000 + delta_ms  # file is 1000ms; mb is 1000+delta
            comparisons.append(TrackComparison(
                file_name=f"{i:02d} {_safe(track_title)}.m4a",
                file_duration_ms=1000,
                file_title=track_title,
                mb_track_title=track_title,
                mb_track_length_ms=mb_len,
                delta_ms=abs(delta_ms),
            ))
        candidate = MatchCandidate(
            mb_release_id=cand_spec["mb_release_id"],
            confidence=cand_spec.get("confidence", "approximate"),
            file_count=len(album_spec["tracks"]),
            track_count=len(album_spec["tracks"]),
            track_comparisons=comparisons,
            proposed_at=now,
            notes=cand_spec.get("notes", ["Track lengths differ from MB"]),
        )

    tagged_at = now if sc_spec.get("tagged") else None

    return Sidecar(
        schema_version=1,
        source=sc_spec["source"],
        bandcamp=bandcamp,
        downloaded_at=(now if sc_spec["source"] == "bandcamp" else None),
        added_at=(now if sc_spec["source"] == "manual" else None),
        mb_release_id=sc_spec.get("mb_release_id"),
        mb_match_candidate=candidate,
        tagged_at=tagged_at,
    )
