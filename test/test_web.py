"""Smoke tests for the FastAPI layer.

Not exhaustive — task 13 owns the comprehensive integration test matrix.
These verify wiring: routes load, scanner integration works, state-dispatched
templates render without crashing for each AlbumState.
"""

from __future__ import annotations

import re
import shutil
import threading
import zipfile
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from unittest import mock

import pytest
from fastapi.testclient import TestClient
from mutagen.mp4 import MP4

from harmonist import activity, activity_store, mb_lookup
from harmonist import sidecar as sc
from harmonist.activity_store import Level, Source
from harmonist.config import (
    BandcampConfig,
    Config,
    LibraryConfig,
    PathsConfig,
    ServerConfig,
    TestConfig,
)
from harmonist.models import BandcampInfo, MatchCandidate, Sidecar, TrackComparison
from harmonist.tagger import (
    ATOM_ALBUM,
    ATOM_ARTIST,
    ATOM_COMMENT,
    ATOM_MB_ALBUM_COUNTRY,
    ATOM_MB_ALBUM_ID,
    ATOM_TITLE,
)
from harmonist.web.main import create_app

FIXTURES_DIR = Path(__file__).parent / "fixtures"
SINE_M4A = FIXTURES_DIR / "sine.m4a"


@pytest.fixture
def cfg(tmp_path):
    return Config(
        paths=PathsConfig(
            config_dir=tmp_path / "config",
            music_dir=tmp_path / "music",
        ),
        bandcamp=BandcampConfig(),
        server=ServerConfig(),
        test=TestConfig(mode="fixture"),
    )


@pytest.fixture
def client(cfg):
    cfg.paths.music_dir.mkdir(parents=True, exist_ok=True)
    cfg.paths.config_dir.mkdir(parents=True, exist_ok=True)
    app = create_app(cfg)
    # The CSRF middleware requires HX-Request: true on state-changing
    # methods — every real call in the app comes from HTMX, which sets
    # this. TestClient doesn't, so we inject it as a default header for
    # the whole client. See harmonist.web.security.CSRFMiddleware.
    return TestClient(app, headers={"HX-Request": "true"})


def _make_album(cfg, name: str, *, mbid: str | None = None, comment: str | None = None) -> Path:
    d = cfg.paths.music_dir / "Artist" / name
    d.mkdir(parents=True)
    f = d / "01 Track.m4a"
    shutil.copy(SINE_M4A, f)
    if mbid or comment:
        audio = MP4(f)
        if mbid:
            audio[ATOM_MB_ALBUM_ID] = [mbid.encode("utf-8")]
        if comment:
            audio[ATOM_COMMENT] = [comment]
        audio.save()
    return d


def _id_for(cfg, album_dir: Path) -> str:
    """Return the canonical album id (scanner-assigned). Works for NEW
    albums (registry-minted UUID) and sidecar'd albums (UUID or MBID).
    """
    from harmonist import scanner

    for a in scanner.scan(cfg.paths.music_dir):
        if a.path == album_dir:
            return a.id
    raise AssertionError(f"no album at {album_dir}")


# ---------- Bandcamp setup / deferred sync ----------


def test_header_shows_setup_when_no_cookies(client):
    """Fresh install (no cookies) → header offers setup, not sync."""
    r = client.get("/")
    assert r.status_code == 200
    assert "Set up Bandcamp sync" in r.text
    assert "Sync Bandcamp" not in r.text


def test_bandcamp_setup_modal_renders(client):
    r = client.get("/bandcamp/setup")
    assert r.status_code == 200
    assert "cookies.txt" in r.text
    assert "bandcampsync" in r.text  # instructions link
    assert 'hx-post="/bandcamp/cookies"' in r.text


def test_bandcamp_cookies_saved_from_text_flips_header(client, cfg):
    body = "# Netscape HTTP Cookie File\n.bandcamp.com\tTRUE\t/\tFALSE\t0\tident\tabc\n"
    r = client.post("/bandcamp/cookies", data={"cookies_text": body})
    assert r.status_code == 200
    assert r.headers.get("hx-refresh") == "true"
    # Cookies written to the configured path
    assert cfg.cookies_file.exists()
    assert cfg.cookies_file.read_text() == body
    # Header now offers Sync, not setup
    home = client.get("/")
    assert "Sync Bandcamp" in home.text
    assert "Set up Bandcamp sync" not in home.text


def test_bandcamp_cookies_saved_from_upload(client, cfg):
    import io

    body = b"# Netscape HTTP Cookie File\n.bandcamp.com\tTRUE\t/\tFALSE\t0\tident\txyz\n"
    r = client.post(
        "/bandcamp/cookies",
        files={"cookies_file": ("cookies.txt", io.BytesIO(body), "text/plain")},
    )
    assert r.status_code == 200
    assert r.headers.get("hx-refresh") == "true"
    assert cfg.cookies_file.read_bytes() == body


def test_bandcamp_cookies_empty_returns_error_modal(client, cfg):
    r = client.post("/bandcamp/cookies", data={"cookies_text": "   "})
    assert r.status_code == 200
    # No refresh — modal re-rendered with an error, cookies not written
    assert "hx-refresh" not in {k.lower() for k in r.headers}
    assert "Paste your cookies.txt" in r.text
    assert not cfg.cookies_file.exists()


# ---------- basic routes ----------


def test_index_renders(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Inbox" in r.text


def test_index_has_inbox_busy_lock(client):
    """The inbox carries a busy banner (hidden until the /status poll toggles it)
    so a background op dims + locks the inbox."""
    r = client.get("/").text
    assert 'id="inbox-busy"' in r
    assert 'id="inbox-busy-label"' in r
    # Toggled by the status handler on any running op.
    for label in ("Syncing…", "Reconciling…", "Scanning…"):
        assert label in r
    assert "pointer-events-none" in r  # the lock the JS applies to #task-list


def test_tasks_empty_inbox(client):
    r = client.get("/tasks")
    assert r.status_code == 200
    assert "Inbox is empty" in r.text


def test_healthz(client, cfg):
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["music_dir"] == str(cfg.paths.music_dir)
    assert body["sync_state"] == "idle"


def test_sync_status_idle(client):
    r = client.get("/sync/status")
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "idle"
    assert "current_item" in body  # always present so the JS doesn't NPE


def test_cover_serves_embedded_art_without_folder_cover(client, cfg):
    """No cover.jpg on disk, but the track has embedded art → /cover serves it
    on the fly (200), no need to write it to disk first."""
    from mutagen.mp4 import MP4, MP4Cover

    d = _make_album(cfg, "EmbeddedArt")
    audio = MP4(d / "01 Track.m4a")
    audio["covr"] = [MP4Cover(b"\xff\xd8\xfftest-jpeg-bytes", imageformat=MP4Cover.FORMAT_JPEG)]
    audio.save()
    aid = _id_for(cfg, d)

    r = client.get(f"/cover/{aid}")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/")
    assert b"test-jpeg-bytes" in r.content


def test_inbox_card_omits_cover_request_when_no_cover(client, cfg):
    """A NEW album with no folder cover must NOT emit a /cover <img> — else
    hundreds of them flood the logs with 404s (and fetch even when collapsed)."""
    _make_album(cfg, "NoCoverAlbum")  # no cover.jpg on disk
    r = client.get("/tasks")
    assert 'src="/cover/' not in r.text


def test_inbox_card_lazy_loads_cover_when_present(client, cfg):
    d = _make_album(cfg, "HasCoverAlbum")
    (d / "cover.jpg").write_bytes(b"\xff\xd8\xff\xe0")  # looks like a JPEG header
    r = client.get("/tasks")
    assert 'src="/cover/' in r.text
    assert 'loading="lazy"' in r.text


def test_inbox_card_shows_the_path_relative_to_the_library(client, cfg):
    """Under Docker the library prefix is a container path that means nothing to
    the user, and the tail is what identifies the album — the reason audit
    records went relative in #98. The inbox card was the last place still
    printing it in full (#121). Absolute stays available on hover."""
    d = _make_album(cfg, "RelativeMe")
    body = client.get("/tasks").text

    assert f'title="{d}"' in body  # absolute, one hover away
    rel = str(d.relative_to(cfg.paths.music_dir))
    assert f">{rel}</p>" in body
    # The visible text must not be the absolute path. Checked on the rendered
    # <p> rather than the whole page, since `title` legitimately carries it.
    assert f">{d}</p>" not in body


def test_no_template_renders_a_bare_library_path(cfg):
    """A guard for the whole class, not just the card #121 fixed: `{{ album.path }}`
    is the absolute path, and every place showing it to the user wants
    `rel_path(...)`. Cheap to keep, and it fails on the next one added."""
    import re
    from pathlib import Path

    offenders = []
    for tpl in (Path(__file__).resolve().parents[1] / "templates").rglob("*.html"):
        for n, line in enumerate(tpl.read_text().splitlines(), 1):
            # `{{ album.path }}` used as content. A `title="{{ album.path }}"`
            # attribute is the intended escape hatch and is exempt.
            if re.search(r"(?<!title=\")\{\{\s*album\.path\s*\}\}(?!\")", line):
                offenders.append(f"{tpl.name}:{n}")
    assert not offenders, f"render these via rel_path(...): {offenders}"


def test_consolidated_status_endpoint(client):
    """One poll returns sync + reconcile + scan + counts, replacing separate polls."""
    r = client.get("/status")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"sync", "reconcile", "scan", "counts", "pending"}
    assert body["sync"]["state"] == "idle"
    assert body["reconcile"]["state"] == "idle"
    assert "state" in body["scan"]
    # Single source of truth for the inbox/library numbers.
    for key in ("inbox", "library", "new", "needs_sync"):
        assert key in body["counts"]


def test_tasks_renders_inbox_count(client, cfg):
    """The inbox count lives inside the polled /tasks fragment so it
    updates without a full-page reload.
    """
    _make_album(cfg, "New Album")
    _make_album(cfg, "Another One")
    r = client.get("/tasks")
    assert r.status_code == 200
    assert "2" in r.text
    assert "need attention" in r.text


def test_tasks_does_not_render_library_total(client, cfg):
    """The Inbox header is for inbox concerns; library-wide totals belong
    to a separate Library box (TBD). Regression on the user's UX feedback.
    """
    _make_album(cfg, "X")
    r = client.get("/tasks")
    assert "total in library" not in r.text


def test_tasks_groups_albums_by_state_with_headers_and_instructions(client, cfg):
    """Each state appears as its own <section> with a heading + instruction line."""
    # NEEDS_MBID with store_url
    d1 = _make_album(cfg, "WithURL")
    sc.write(
        d1,
        Sidecar(
            store_url="https://x.bandcamp.com/album/y",
            bandcamp=BandcampInfo(item_id=1),
        ),
    )
    # NEEDS_MBID without store_url
    d2 = _make_album(cfg, "Manual")
    sc.write(d2, Sidecar())

    r = client.get("/tasks")
    # Both are NEEDS_MBID — single section heading
    assert ">MBID</abbr>" in r.text  # the "Needs MBID" header (MBID is an <abbr>)
    # Open-in-Harmony link appears for the store_url card
    assert "Open in Harmony" in r.text
    # Manual MBID form appears too
    assert 'name="mbid"' in r.text


def test_tasks_state_group_omitted_when_empty(client, cfg):
    """No section header rendered for states without any albums."""
    _make_album(cfg, "Only New")
    r = client.get("/tasks")
    assert "New" in r.text
    # No NEEDS_MBID / NEEDS_REVIEW / NEEDS_SYNC headers. Match a fragment unique
    # to the NEEDS_MBID header (the New header now also mentions MBID).
    assert "No confirmed MusicBrainz release" not in r.text
    assert "Needs Review" not in r.text
    assert "Needs Link" not in r.text


def test_tasks_needs_sync_section_advises_sync(client, cfg):
    """The Needs Link group instructions point the user to click Sync."""
    d = _make_album(cfg, "UB")
    audio = MP4(d / "01 Track.m4a")
    audio[ATOM_MB_ALBUM_ID] = [b"rel-a"]
    audio.save()
    sc.write(
        d,
        Sidecar(
            store_url="https://x.bandcamp.com/album/y",
            bandcamp=BandcampInfo(item_id=None),
            mb_release_id="rel-a",
            tagged_at=datetime.now(UTC),
        ),
    )
    r = client.get("/tasks")
    assert "Needs Link" in r.text
    # Instruction points at the (header) Sync button — there's no inline
    # bulk-sync button; the header Sync popover is the single entry point.
    assert "Click Sync" in r.text
    assert "Sync to link" not in r.text


def test_large_inbox_group_collapses_into_details(client, cfg):
    """A group past the collapse threshold (12) folds into a <details> so a
    big library import doesn't render as an endless wall of cards."""
    for i in range(13):
        _make_album(cfg, f"New {i:02d}")
    r = client.get("/tasks")
    assert "Show all 13 albums" in r.text


def test_small_inbox_group_not_collapsed(client, cfg):
    """Below the threshold the cards render inline — no 'Show all' disclosure.
    (Cards carry their own <details> for tools, so 'Show all N' is the marker.)"""
    _make_album(cfg, "Solo New")
    r = client.get("/tasks")
    assert "Show all" not in r.text


def test_tasks_empty_state_message_distinguishes_zero_vs_all_done(client, cfg):
    # Empty library
    r = client.get("/tasks")
    assert "Drop some albums" in r.text or "Inbox is empty" in r.text


# ---------- state dispatch — each card type renders ----------


def test_new_card_rendered(client, cfg):
    _make_album(cfg, "New Album")  # untagged
    r = client.get("/tasks")
    assert "New" in r.text
    assert "need a MusicBrainz release" in r.text  # group header states what's needed
    assert 'id="newtools-' in r.text  # search/paste tools present


def test_needs_mbid_card_with_store_url_rendered(client, cfg):
    d = _make_album(cfg, "HasURL")
    sc.write(
        d,
        Sidecar(
            store_url="https://x.bandcamp.com/album/y",
            bandcamp=BandcampInfo(item_id=1),
        ),
    )
    r = client.get("/tasks")
    assert ">MBID</abbr>" in r.text  # the "Needs MBID" header (MBID is an <abbr>)
    assert "Open in Harmony" in r.text
    assert "harmony.pulsewidth.org.uk" in r.text


def test_needs_mbid_private_release_suppresses_harmony_and_recheck(client, cfg):
    """A private Bandcamp release (public URL 404s) must not offer Harmony
    seeding or Recheck — its URL can't be added to MusicBrainz. The manual
    MBID form stays, and the badge reads 'Bandcamp (private)'."""
    d = _make_album(cfg, "Private")
    sc.write(
        d,
        Sidecar(
            store_url="https://x.bandcamp.com/album/secret",
            bandcamp=BandcampInfo(item_id=1, is_private=True),
        ),
    )
    r = client.get("/tasks")
    assert ">MBID</abbr>" in r.text  # still a Needs MBID card
    assert "Open in Harmony" not in r.text
    assert "/recheck/" not in r.text
    assert "won't resolve on MusicBrainz" in r.text
    assert "Bandcamp (private)" in r.text
    # No store-URL search mode for a private release — only name search.
    assert "Look up releases at this URL" not in r.text
    assert "Search MusicBrainz by name" in r.text
    # The manual resolution path remains.
    assert 'name="mbid"' in r.text


def test_needs_mbid_card_without_store_url_rendered(client, cfg):
    d = _make_album(cfg, "NoURL")
    sc.write(d, Sidecar())
    r = client.get("/tasks")
    assert ">MBID</abbr>" in r.text  # the "Needs MBID" header (MBID is an <abbr>)
    # Manual MBID form is included
    assert 'name="mbid"' in r.text
    assert "/manual/" in r.text


def test_new_card_untagged_offers_search_no_reconcile(client, cfg):
    _make_album(cfg, "New Album")  # untagged: no MB Album Id atom
    r = client.get("/tasks")
    # The group header states what's needed; the card is just the search/paste
    # tools — no per-card blurb, and no (useless here) "Reconcile from tags".
    assert "need a MusicBrainz release" in r.text
    assert "Reconcile from tags" not in r.text
    assert "Assign &amp; Tag" in r.text or "Assign & Tag" in r.text


def test_new_card_tagged_orphan_offers_reconcile(client, cfg):
    _make_album(cfg, "Orphan", mbid="rel-xyz")  # MB Album Id atom, no sidecar
    r = client.get("/tasks")
    assert "Reconcile from tags" in r.text


def test_needs_review_card_renders_side_by_side(client, cfg):
    d = _make_album(cfg, "NR Album")
    sc.write(
        d,
        Sidecar(
            store_url="https://x.bandcamp.com/album/y",
            bandcamp=BandcampInfo(item_id=1),
            mb_match_candidate=MatchCandidate(
                mb_release_id="rel-aaa",
                confidence="approximate",
                file_count=2,
                track_count=2,
                track_comparisons=[
                    TrackComparison(
                        file_name="01.m4a",
                        file_duration_ms=180000,
                        file_title="Side A",
                        mb_track_title="Side A",
                        mb_track_length_ms=185000,
                        delta_ms=5000,
                    ),
                    TrackComparison(
                        file_name="02.m4a",
                        file_duration_ms=200000,
                        file_title="Side B",
                        mb_track_title="Side B",
                        mb_track_length_ms=200500,
                        delta_ms=500,
                    ),
                ],
            ),
        ),
    )
    r = client.get("/tasks")
    # Merged into NEEDS_MBID: the card shows the suggestion side-by-side inline.
    assert ">MBID</abbr>" in r.text  # the "Needs MBID" header (MBID is an <abbr>)
    assert "MusicBrainz suggests" in r.text
    assert "approximate" in r.text
    assert "Side A" in r.text
    assert "Confirm" in r.text
    assert "Dismiss suggestion" in r.text
    # Re-query MB for the same release (after fixing it upstream)
    assert "Refresh from MB" in r.text


def test_mistag_card_renders_open_panel_with_sibling_explanation(client, cfg):
    """A mis-tag candidate renders the 'Wrong release?' panel OPEN with a
    store-URL-based sibling explanation, kept separate from the track-count
    match-quality note."""
    d = _make_album(cfg, "Mistag Card")
    sc.write(
        d,
        Sidecar(
            store_url="https://ultimae.bandcamp.com/album/life",
            mb_match_candidate=MatchCandidate(
                mb_release_id="rel-correct",
                confidence="no_match",
                file_count=10,
                track_count=11,
                notes=["file count 10 does not match MB track count 11"],
                mistag_owned_url="https://ultimae.bandcamp.com/album/life",
                mistag_owned_label="ASURA / Life²",
                mistag_owned_disambig="24-bit",
                mistag_tagged_mbid="rel-wrong",
                mistag_tagged_label="ASURA / Life²",
                mistag_tagged_disambig="",
                mistag_release_group_mbid="rg-life",
            ),
        ),
    )
    r = client.get("/tasks")
    # The wrong-release panel is rendered OPEN and names both releases.
    assert "<details open" in r.text
    assert "Wrong release?" in r.text
    assert "ASURA / Life²" in r.text
    # Both releases link to MusicBrainz (tagged + owned/suggested), and the
    # shared release group links too.
    assert "https://musicbrainz.org/release/rel-wrong" in r.text
    assert "https://musicbrainz.org/release/rel-correct" in r.text
    assert "https://musicbrainz.org/release-group/rg-life" in r.text
    # The store URL is hidden behind the word "Bandcamp", not shown raw.
    assert ">Bandcamp</a>" in r.text
    # Disambiguation is rendered in parentheses, visually distinct (muted span).
    assert ">(24-bit)</span>" in r.text
    # The track-count note stays in the match-quality box, NOT in the mis-tag
    # prose, and is flagged with a "Note:" prefix.
    assert "Note:" in r.text
    assert "file count 10 does not match MB track count 11" in r.text
    # File count (10) < track count (11): the only valid tag is incomplete, so
    # that's the primary action — no misleading highlighted "Confirm & Tag".
    assert "Confirm as Incomplete (10 of 11)" in r.text
    assert "Confirm &amp; Tag" not in r.text


def test_mistag_renders_in_own_top_level_section(client, cfg):
    """A mis-tag (candidate carries mistag_owned_url) is lifted into its own
    top-level 'Likely mis-tagged' section ABOVE Needs MBID, while a plain
    NEEDS_MBID album stays under the Needs MBID header. Both remain
    AlbumState.NEEDS_MBID — this is a rendering split, not a new state."""
    mt = _make_album(cfg, "Mistagged One")
    sc.write(
        mt,
        Sidecar(
            store_url="https://ultimae.bandcamp.com/album/life",
            mb_match_candidate=MatchCandidate(
                mb_release_id="rel-correct",
                confidence="no_match",
                file_count=10,
                track_count=10,
                mistag_owned_url="https://ultimae.bandcamp.com/album/life",
                mistag_owned_label="ASURA / Life²",
                mistag_tagged_mbid="rel-wrong",
                mistag_tagged_label="ASURA / Life²",
                mistag_release_group_mbid="rg-life",
            ),
        ),
    )
    # A plain NEEDS_MBID album (sidecar, no MBID, no candidate).
    plain = _make_album(cfg, "Plain Needs Mbid")
    sc.write(plain, Sidecar())

    r = client.get("/tasks")
    body = r.text
    assert "Possibly mis-tagged" in body
    header = body.index("Needs <abbr")  # the Needs MBID group header
    # The mis-tag section is above the Needs MBID group.
    assert body.index("Possibly mis-tagged") < header
    # The mis-tagged album sits in the mis-tag section (before Needs MBID); the
    # plain one sits under Needs MBID (after it).
    assert body.index("Mistagged One") < header
    assert body.index("Plain Needs Mbid") > header


def test_surrender_card_renders_readonly_with_tools(client, cfg):
    """A surrender candidate (unmatched_purchase, no matching download) leads with
    'Move to Library' + the withdrawn-release explanation, and offers the 'wrong
    release?' escape hatch — but NO Confirm & Tag (that would loop to Needs Link)."""
    d = _make_album(cfg, "Surrendered")
    sc.write(
        d,
        Sidecar(
            store_url="https://x.bandcamp.com/album/surrendered",
            mb_match_candidate=MatchCandidate(
                mb_release_id="rel-surr",
                confidence="exact",
                file_count=10,
                track_count=10,
                unmatched_purchase=True,
            ),
        ),
    )
    from harmonist import scanner

    a = next(x for x in scanner.scan(cfg.paths.music_dir) if x.path == d)
    r = client.get("/tasks")
    assert "No matching Bandcamp purchase" in r.text
    assert "withdrawn from Bandcamp" in r.text  # names the common cause
    # Primary action is Move to Library; no Confirm-and-tag loop.
    assert f"/surrender/{a.id}/keep" in r.text
    assert "Move to Library" in r.text
    assert "Confirm &amp; Tag" not in r.text
    assert "/confirm/" not in r.text
    # The redundant store-URL lookup is gone; the name/MBID escape hatch remains.
    assert "Look up releases at this URL" not in r.text


def test_surrender_is_audited(client, cfg):
    """#88: a surrender is named in the review gate. Setting the MBID from the
    candidate was already audited as an identity change, but
    `purchase_unavailable` — the defining, PERMANENT part of the decision — moved
    silently. The scanner then treats the album as terminal forever."""
    from harmonist import activity_store, scanner
    from harmonist.activity_store import Source

    d = _make_album(cfg, "Surrendered", mbid="rel-sur")
    sc.write(
        d,
        Sidecar(
            store_url="https://x.bandcamp.com/album/sur",
            mb_release_id=None,
            mb_match_candidate=MatchCandidate(
                mb_release_id="rel-sur",
                confidence="exact",
                file_count=1,
                track_count=1,
                unmatched_purchase=True,
            ),
        ),
    )
    a = next(x for x in scanner.scan(cfg.paths.music_dir) if x.path == d)
    activity_store.clear()

    assert client.post(f"/surrender/{a.id}/keep").status_code == 200

    rows = [e.message for e in activity_store.recent(50, source=Source.AUDIT)]
    upd = next(m for m in rows if m.startswith("sidecar.update"))
    assert "purchase_unavailable=False->True" in upd


def test_surrender_keep_marks_purchase_unavailable_and_completes(client, cfg):
    """'Move to Library' restores the release id from the surrender candidate, flags
    the purchase unavailable, and the album classifies COMPLETE — and STAYS complete
    (a re-scan won't drag it back to NEEDS_SYNC despite the Bandcamp store_url)."""
    from harmonist import scanner
    from harmonist.models import AlbumState

    d = _make_album(cfg, "Withdrawn", mbid="rel-wd")  # files tagged with rel-wd
    sc.write(
        d,
        Sidecar(
            store_url="https://x.bandcamp.com/album/withdrawn",
            mb_release_id=None,
            mb_match_candidate=MatchCandidate(
                mb_release_id="rel-wd",
                confidence="exact",
                file_count=1,
                track_count=1,
                unmatched_purchase=True,
            ),
        ),
    )
    a = next(x for x in scanner.scan(cfg.paths.music_dir) if x.path == d)
    assert a.state == AlbumState.NEEDS_MBID

    r = client.post(f"/surrender/{a.id}/keep")
    assert r.status_code == 200, r.text

    loaded = sc.read(d)
    assert loaded.purchase_unavailable is True
    assert loaded.mb_release_id == "rel-wd"
    assert loaded.mb_match_candidate is None
    after = next(x for x in scanner.scan(cfg.paths.music_dir) if x.path == d)
    assert after.state == AlbumState.COMPLETE  # terminal, despite the bandcamp store_url


def test_inconsistent_card_renders(client, cfg):
    """A dir with conflicting album tags surfaces an INCONSISTENT card,
    showing the per-file table and the Picard nudge."""
    d = _make_album(cfg, "Mixed")
    # _make_album wrote one track; add a second with a conflicting album tag.
    second = d / "02 Another.m4a"
    shutil.copy(SINE_M4A, second)
    a1 = MP4(d / "01 Track.m4a")
    a1[ATOM_ALBUM] = ["Album A"]
    a1.save()
    a2 = MP4(second)
    a2[ATOM_ALBUM] = ["Album B"]
    a2.save()

    r = client.get("/tasks")
    assert r.status_code == 200
    assert "Inconsistent" in r.text
    assert "Picard" in r.text
    # The per-file table should list both conflicting titles
    assert "Album A" in r.text
    assert "Album B" in r.text


def test_needs_sync_card_renders(client, cfg):
    d = _make_album(cfg, "UB Album")
    # Tag the file so scanner sees it as DONE-style (mb_release_id matches)
    audio = MP4(d / "01 Track.m4a")
    audio[ATOM_MB_ALBUM_ID] = [b"rel-aaa"]
    audio.save()
    sc.write(
        d,
        Sidecar(
            store_url="https://x.bandcamp.com/album/y",
            bandcamp=BandcampInfo(item_id=None),
            mb_release_id="rel-aaa",
            tagged_at=datetime.now(UTC),
        ),
    )
    r = client.get("/tasks")
    assert "Needs Link" in r.text
    assert "Mark purchased elsewhere" in r.text
    assert 'hx-post="/unconfirmed/' in r.text
    # URL input is pre-filled with the existing URL (not just a placeholder)
    assert 'value="https://x.bandcamp.com/album/y"' in r.text


# ---------- action endpoints ----------


def test_post_sync_starts_runner(client):
    # Replace the runner_fn to a no-op so we don't try to hit Bandcamp
    runner = client.app.state.sync_runner
    runner._runner_fn = lambda: None
    r = client.post("/sync")
    assert r.status_code == 200
    assert "Sync started" in r.text


def test_sync_route_does_not_duplicate_the_start_entry(client):
    """#101: starting a sync wrote TWO feed entries — the route's flash and the
    runner's generic line — and neither said whether it was link-only. The route
    keeps its status-bar flash but no longer writes an entry; the runner writes
    the single authoritative one once it knows the mode."""
    import time

    from harmonist import activity

    runner = client.app.state.sync_runner
    runner._runner_fn = lambda: None
    activity.clear()

    r = client.post("/sync")
    assert r.status_code == 200
    assert "Sync started" in r.text  # the status-bar flash still fires

    for _ in range(50):  # let the runner thread finish
        if runner.status()["state"] == "idle":
            break
        time.sleep(0.02)

    starts = [e.message for e in activity.recent(20) if "ync started" in e.message]
    assert starts == [], f"the route should write no start entry, got {starts}"


def test_post_sync_409_when_already_running(client):
    runner = client.app.state.sync_runner
    # Manually flag as running so the second POST hits the 409 branch
    runner._status.state = "running"
    r = client.post("/sync")
    assert r.status_code == 409
    assert "already running" in r.text


def test_post_sync_409_while_reconciling(client):
    # A sync must not start while a reconcile pass is in flight — it mutates
    # sidecars and the inbox. The endpoint backstops the disabled button.
    client.app.state.sync_runner._runner_fn = lambda: None
    client.app.state.reconcile_runner._status.state = "running"
    r = client.post("/sync")
    assert r.status_code == 409
    assert "reconciling" in r.text.lower()


def test_post_sync_409_during_the_cold_start_scan(client):
    # Until the first scan lands there's no snapshot, so a sync would run
    # against a library it can't see. The button is disabled client-side; this
    # is the backstop for a stale page or the window before the next poll.
    client.app.state.sync_runner._runner_fn = lambda: None
    client.app.state.scan_runner._status.state = "scanning"
    client.app.state.scan_runner._status.seq = 0
    r = client.post("/sync")
    assert r.status_code == 409
    assert "scanning" in r.text.lower()


def test_post_sync_allowed_during_a_later_rescan(client):
    # Only the FIRST scan blocks: a later rescan (seq > 0) has a usable
    # snapshot behind it, so it must not lock the user out of syncing.
    client.app.state.sync_runner._runner_fn = lambda: None
    client.app.state.scan_runner._status.state = "scanning"
    client.app.state.scan_runner._status.seq = 3
    assert client.post("/sync").status_code == 200


def test_needs_sync_bulk_button_absent_while_reconciling(client, cfg):
    # During reconcile the inbox renders the live-count panel, not the groups —
    # so the bulk-sync button isn't offered at all (can't race the pass).
    d = _make_album(cfg, "BulkSync")
    audio = MP4(d / "01 Track.m4a")
    audio[ATOM_MB_ALBUM_ID] = [b"rel-bulk-1"]
    audio.save()
    sc.write(
        d,
        Sidecar(
            store_url="https://x.bandcamp.com/album/y",
            bandcamp=BandcampInfo(item_id=None),
            mb_release_id="rel-bulk-1",
            tagged_at=datetime.now(UTC),
        ),
    )
    client.app.state.reconcile_runner._status.state = "running"
    r = client.get("/tasks")
    assert r.status_code == 200
    assert "Sorting your library" in r.text  # the reconcile panel
    assert "sync-trigger" not in r.text  # ...no bulk-sync button


def test_needs_sync_group_collapses_to_progress_note_while_syncing(client, cfg):
    # While a sync is in flight the NEEDS_SYNC group hides its bulk button and
    # its "Show all N albums" card list, showing a progress note instead — the
    # albums are being linked right now and clear when the sync finishes.
    d = _make_album(cfg, "BulkSyncBusy")
    audio = MP4(d / "01 Track.m4a")
    audio[ATOM_MB_ALBUM_ID] = [b"rel-bulk-3"]
    audio.save()
    sc.write(
        d,
        Sidecar(
            store_url="https://x.bandcamp.com/album/y",
            bandcamp=BandcampInfo(item_id=None),
            mb_release_id="rel-bulk-3",
            tagged_at=datetime.now(UTC),
        ),
    )
    client.app.state.sync_runner._status.state = "running"
    r = client.get("/tasks")
    assert r.status_code == 200
    assert "Linking these to your Bandcamp purchases" in r.text
    assert "sync-trigger" not in r.text  # bulk button gone while syncing
    assert "Show all" not in r.text  # no expandable card list either


def test_tasks_shows_live_reconcile_counts(client, cfg):
    """During reconcile the inbox shows the live base+tally counts (need
    attention = reconcile.inbox, and a New/Needs-Sync/Library split building
    up) rather than the frozen snapshot — and NOT the status bar's X/Y."""
    _make_album(cfg, "Fresh")  # a NEW orphan (frozen snapshot would say 1)
    runner = client.app.state.reconcile_runner
    runner._status.state = "running"
    runner._status.completed = 143
    runner._status.total = 346
    runner._status.inbox = 60
    runner._status.new = 5
    runner._status.needs_sync = 55
    runner._status.library = 78
    r = client.get("/tasks")
    assert r.status_code == 200
    assert "Sorting your library" in r.text  # the live-count panel
    # In-page "need attention" tracks reconcile.inbox (the tab badge itself now
    # comes from /status `counts`, tested separately).
    assert ">60</span> need attention" in r.text
    assert ">5</span> New" in r.text  # the building split
    assert ">55</span> Needs Link" in r.text
    assert ">78</span> to Library" in r.text
    assert "143 / 346" not in r.text  # progress lives in the status bar
    assert "Fresh" not in r.text  # frozen card list not rendered


def test_tasks_kicks_reconcile_only_for_reconcilable_orphan(client, cfg):
    """A NEW album with an MBID atom (reconcilable) kicks reconcile; an
    untagged orphan does not — so incidental inbox refreshes don't churn."""
    started: list[bool] = []

    def fake_start() -> bool:
        started.append(True)
        return True

    client.app.state.reconcile_runner.start = fake_start

    _make_album(cfg, "Untagged")  # NEW, no MBID atom → not reconcilable
    client.get("/tasks")
    assert started == []  # untagged orphan must NOT kick reconcile

    _make_album(cfg, "Tagged", mbid="rel-z")  # NEW + MBID atom → reconcilable
    client.get("/tasks")
    assert started == [True]  # now there's something reconcile can resolve


def test_tasks_skips_reconcile_for_forgotten_orphan(client, cfg):
    """A Forgotten (exempt) MBID-tagged orphan must not kick reconcile — the
    runner would skip it anyway, so kicking would loop forever."""
    started: list[bool] = []

    def fake_start() -> bool:
        started.append(True)
        return True

    client.app.state.reconcile_runner.start = fake_start

    d = _make_album(cfg, "Forgotten", mbid="rel-f")  # NEW + MBID atom
    client.app.state.forgotten_paths.add(d)
    client.get("/tasks")
    assert started == []


def test_tasks_hides_groups_and_shows_panel_while_reconciling(client, cfg):
    """While reconcile runs, the inbox shows the live-count panel instead of the
    (frozen, re-polled) group card lists."""
    _make_album(cfg, "FreshOne")  # NEW orphan; frozen snapshot would render it
    runner = client.app.state.reconcile_runner
    runner._status.state = "running"
    runner._status.new = 3
    r = client.get("/tasks")
    assert r.status_code == 200
    assert "Sorting your library" in r.text  # the live-count panel
    assert ">3</span> New" in r.text  # the building split
    assert "FreshOne" not in r.text  # no card rendered
    assert "Show all" not in r.text  # ...and no disclosure


def test_tasks_new_group_shows_cards_when_idle(client, cfg):
    """Sanity counterpart: with no reconcile running the NEW card renders."""
    _make_album(cfg, "FreshTwo")
    r = client.get("/tasks")
    assert r.status_code == 200
    assert "FreshTwo" in r.text


def test_tasks_no_group_cards_while_reconciling(client, cfg):
    """No group cards render during reconcile (the panel replaces them) — e.g.
    the Needs Link card's actions are gone."""
    _needs_sync_album(cfg, "Linkme", "rel-q")
    client.app.state.reconcile_runner._status.state = "running"
    r = client.get("/tasks")
    assert r.status_code == 200
    assert "Sorting your library" in r.text  # the panel
    assert "Mark purchased elsewhere" not in r.text  # ...not the needs-sync card


def test_tasks_needs_sync_card_shown_when_idle(client, cfg):
    """Sanity counterpart: idle → the Needs Link card renders normally."""
    _needs_sync_album(cfg, "Linkme2", "rel-r")
    r = client.get("/tasks")
    assert r.status_code == 200
    assert "Mark purchased elsewhere" in r.text


def test_tasks_in_page_attention_line_counts_inbox(client, cfg):
    """The in-page 'N need attention' line reflects the snapshot when idle.
    (The tab badges themselves come from /status `counts` — tested separately.)"""
    _make_album(cfg, "InboxA")
    _make_album(cfg, "InboxB")
    r = client.get("/tasks")
    assert "need attention" in r.text
    assert "2" in r.text

    runner = client.app.state.reconcile_runner
    runner._status.state = "running"
    runner._status.inbox = 7  # base + tallies so far
    r = client.get("/tasks")
    assert "7" in r.text  # running → reconcile.inbox
    assert "InboxA" not in r.text  # ...and no cards


def _needs_sync_album(cfg, name: str, mbid: str) -> Path:
    """A tagged, on-disk album with a Bandcamp store_url but no item_id →
    scans as NEEDS_SYNC (the post-sync audit's input set)."""
    d = _make_album(cfg, name, mbid=mbid)
    sc.write(
        d,
        Sidecar(
            store_url=f"https://label.bandcamp.com/album/{name.lower()}",
            bandcamp=BandcampInfo(item_id=None),
            mb_release_id=mbid,
            tagged_at=datetime.now(UTC),
        ),
    )
    return d


def test_report_unmatched_partial_sync_warns_but_does_not_demote(cfg):
    """On a PARTIAL sync (checkpoint-limited) we only warn — the purchase may be
    below the checkpoint. The album stays NEEDS_SYNC; nothing is demoted."""
    from harmonist import activity, scanner
    from harmonist.models import AlbumState
    from harmonist.web.main import _report_unmatched_after_sync

    cfg.paths.music_dir.mkdir(parents=True, exist_ok=True)
    d1 = _needs_sync_album(cfg, "Stranded", "rel-Stranded")
    _needs_sync_album(cfg, "Adrift", "rel-Adrift")
    activity.clear()
    _report_unmatched_after_sync(cfg, full_sync=False)
    warnings = [e for e in activity.recent(10) if e.level == "warning"]
    assert len(warnings) == 2
    assert all("Not linked to a Bandcamp purchase" in w.message for w in warnings)
    assert all("Try a different URL" in w.message for w in warnings)
    msgs = " ".join(w.message for w in warnings)
    assert "https://label.bandcamp.com/album/stranded" in msgs
    # Still NEEDS_SYNC — not demoted.
    album = next(a for a in scanner.scan(cfg.paths.music_dir) if a.path == d1)
    assert album.state == AlbumState.NEEDS_SYNC


def test_report_unmatched_full_sync_surrenders_to_needs_mbid(cfg):
    """On a FULL sync, an album with no matching purchase is dropped back to
    NEEDS_MBID with its current release kept as a read-only suggestion."""
    from harmonist import activity, scanner
    from harmonist.models import AlbumState
    from harmonist.web.main import _report_unmatched_after_sync

    cfg.paths.music_dir.mkdir(parents=True, exist_ok=True)
    d = _needs_sync_album(cfg, "Orphan", "rel-orphan")
    activity.clear()
    _report_unmatched_after_sync(cfg, full_sync=True)
    loaded = sc.read(d)
    assert loaded.mb_release_id is None  # demoted out of NEEDS_SYNC
    cand = loaded.mb_match_candidate
    assert cand is not None
    assert cand.mb_release_id == "rel-orphan"  # current release kept as suggestion
    assert cand.unmatched_purchase is True
    album = next(a for a in scanner.scan(cfg.paths.music_dir) if a.path == d)
    assert album.state == AlbumState.NEEDS_MBID
    msgs = [e.message for e in activity.recent(10)]
    assert any(
        "No Bandcamp purchase matched" in m and "Needs MBID" in m and "duplicate" in m for m in msgs
    )


def test_surrender_leaves_on_disk_file_tags_intact(cfg):
    """Surrender only rewrites the sidecar — it must NOT untag the audio files.
    This is what makes it non-destructive: the release is still written on disk
    and is offered as a one-click re-confirm suggestion. (Pins the deferred
    user-assigned-MBID behavior: a manually-assigned tag is re-inboxed, never
    erased — see design.md §3.)"""
    from harmonist import formats
    from harmonist.web.main import _report_unmatched_after_sync

    cfg.paths.music_dir.mkdir(parents=True, exist_ok=True)
    d = _needs_sync_album(cfg, "Orphan", "rel-orphan")
    track = d / "01 Track.m4a"
    assert formats.read_album_id(track) == "rel-orphan"  # tagged before surrender

    _report_unmatched_after_sync(cfg, full_sync=True)

    # Sidecar demoted, but the file's MB Album Id atom is untouched.
    assert sc.read(d).mb_release_id is None
    assert formats.read_album_id(track) == "rel-orphan"


def test_surrender_flags_possible_duplicate_of_linked_album(cfg):
    """When a surrendered album is tagged as the SAME release as one already
    linked to a purchase, log a non-committal 'possible duplicate / split'
    warning (we don't auto-resolve — a release can legitimately span dirs)."""
    from harmonist import activity
    from harmonist.web.main import _report_unmatched_after_sync

    cfg.paths.music_dir.mkdir(parents=True, exist_ok=True)
    # The linked copy (has an item_id) — COMPLETE.
    linked = _make_album(cfg, "LinkedCopy", mbid="rel-dup")
    sc.write(
        linked,
        Sidecar(
            store_url="https://x.bandcamp.com/album/dup",
            bandcamp=BandcampInfo(item_id=999),
            mb_release_id="rel-dup",
            tagged_at=datetime.now(UTC),
        ),
    )
    # The unlinked twin tagged as the same release → NEEDS_SYNC → surrenders.
    _needs_sync_album(cfg, "DupCopy", "rel-dup")
    activity.clear()
    _report_unmatched_after_sync(cfg, full_sync=True)
    msgs = [e.message for e in activity.recent(10)]
    assert any("possibly a duplicate copy" in m or "duplicate copy" in m for m in msgs)


def test_report_unmatched_after_sync_quiet_when_all_linked(cfg):
    """A linked album (item_id set) is COMPLETE, not NEEDS_SYNC — no warning."""
    from harmonist import activity
    from harmonist.web.main import _report_unmatched_after_sync

    cfg.paths.music_dir.mkdir(parents=True, exist_ok=True)
    d = _make_album(cfg, "Linked", mbid="rel-y")
    sc.write(
        d,
        Sidecar(
            store_url="https://label.bandcamp.com/album/linked",
            bandcamp=BandcampInfo(item_id=123),
            mb_release_id="rel-y",
            tagged_at=datetime.now(UTC),
        ),
    )
    activity.clear()
    _report_unmatched_after_sync(cfg, full_sync=True)
    assert [e for e in activity.recent(10) if e.level == "warning"] == []


def test_force_full_sync_clears_checkpoint_when_pending_links(cfg, client):
    """A NEEDS_SYNC album means a purchase still needs linking — clear the
    checkpoint so the next sync re-pages the whole collection."""
    from harmonist.web.main import _BANDCAMPSYNC_STATE_FILE, _force_full_sync_if_pending_links

    cfg.paths.music_dir.mkdir(parents=True, exist_ok=True)
    _needs_sync_album(cfg, "Pending", "rel-pending")
    state = cfg.paths.music_dir / _BANDCAMPSYNC_STATE_FILE
    state.write_text("{}")
    pending = _force_full_sync_if_pending_links(cfg, client.app.state.scan_runner)
    assert not state.exists()  # cleared → next sync is full
    assert pending == 1  # the gate signal: >0 → run the sync link-only


def test_configure_logging_quiets_noisy_bandcampsync_loggers(cfg):
    """bandcampsync floods every sync with per-item lines on its own loggers —
    'ignores' (WARNING, normal already-downloaded) and 'sync' (INFO, progress).
    At INFO we raise their thresholds; genuine third-party errors still surface."""
    import logging

    from harmonist.web.main import _configure_logging

    for name in ("ignores", "sync"):
        logging.getLogger(name).setLevel(logging.NOTSET)
    _configure_logging(cfg)  # default log_level == "info"
    assert logging.getLogger("ignores").level == logging.ERROR
    assert logging.getLogger("sync").level == logging.WARNING


def test_configure_logging_leaves_thirdparty_verbose_at_debug(cfg):
    """At DEBUG we leave the third-party loggers verbose for deep debugging."""
    import logging

    from harmonist.web.main import _configure_logging

    logging.getLogger("ignores").setLevel(logging.NOTSET)
    cfg.log_level = "debug"
    _configure_logging(cfg)
    assert logging.getLogger("ignores").level != logging.ERROR


def test_force_full_sync_keeps_checkpoint_when_nothing_pending(cfg, client):
    """No NEEDS_SYNC albums → the checkpoint is left intact (fast incremental)."""
    from harmonist.web.main import _BANDCAMPSYNC_STATE_FILE, _force_full_sync_if_pending_links

    cfg.paths.music_dir.mkdir(parents=True, exist_ok=True)  # empty library
    state = cfg.paths.music_dir / _BANDCAMPSYNC_STATE_FILE
    state.write_text("{}")
    pending = _force_full_sync_if_pending_links(cfg, client.app.state.scan_runner)
    assert state.exists()  # untouched
    assert pending == 0  # nothing pending → downloads enabled (not link-only)


# ---------- mis-tag detection via release-group join ----------


def _release_with_rg(
    mbid: str,
    rg: str,
    n_tracks: int = 1,
    *,
    artist: str = "",
    title: str = "Album",
    disambiguation: str = "",
) -> dict:
    tracks = [
        {
            "id": f"rt-{i}",
            "position": str(i),
            "title": f"T{i}",
            "recording": {"id": f"rec-{i}", "title": f"T{i}", "length": "1000"},
        }
        for i in range(1, n_tracks + 1)
    ]
    rel: dict = {
        "id": mbid,
        "title": title,
        "release-group": {"id": rg, "title": "RG"},
        "medium-list": [{"position": "1", "track-list": tracks}],
    }
    if artist:
        rel["artist-credit-phrase"] = artist
    if disambiguation:
        rel["disambiguation"] = disambiguation
    return rel


class _FakeSyncer:
    def __init__(self, unmatched):
        self._unmatched = unmatched

    def unmatched_purchases(self):
        return self._unmatched


def test_detect_mistag_by_release_group_demotes_with_suggestion(cfg):
    """An unmatched-after-sync album whose release group contains a *different*
    edition the user OWNS (a purchase that linked to no album) → mis-tag: demote
    to NEEDS_MBID with that owned edition suggested, naming the purchase.

    Critically: the 270-style noise (purchases NOT in the album's release group)
    costs nothing — detection is driven by the album + one release-group browse."""
    from harmonist import activity, scanner
    from harmonist.models import AlbumState
    from harmonist.web.main import _detect_mistags_after_sync

    cfg.paths.music_dir.mkdir(parents=True, exist_ok=True)
    d = _needs_sync_album(cfg, "Mistag", "rel-wrong")
    # One relevant purchase (the owned 24-bit edition) buried in lots of noise.
    owned = [(i, f"https://noise.bandcamp.com/album/n{i}", f"Noise / {i}") for i in range(250)]
    owned.append(
        (9999, "https://ultimae.bandcamp.com/album/live-in-corfu-24bit", "Cell / Live in Corfu")
    )
    syncer = _FakeSyncer(owned)

    # rel-wrong's release group rg-1 contains the tagged edition and the owned one.
    def browse_rg(rg):
        assert rg == "rg-1"
        return [
            ("rel-wrong", ["https://ultimae.bandcamp.com/album/live-in-corfu"]),
            ("rel-correct", ["https://ultimae.bandcamp.com/album/live-in-corfu-24bit"]),
        ]

    # Distinct MB releases for the tagged (standard) vs owned (24-bit) editions
    # so we can assert both names AND the disambiguation are captured.
    def fetch_release(m):
        disambig = "24-bit" if m == "rel-correct" else ""
        return _release_with_rg(
            m, "rg-1", artist="Cell", title="Live in Corfu", disambiguation=disambig
        )

    # The sync already recorded these as potential downloads (replace_all runs
    # during the sync, before detection): the owned edition + unrelated noise.
    from harmonist import pending_downloads as pd

    pd.replace_all(
        [
            _pp(9999, url="https://ultimae.bandcamp.com/album/live-in-corfu-24bit"),
            _pp(9998, url="https://noise.bandcamp.com/album/n1"),
        ]
    )

    activity.clear()
    _detect_mistags_after_sync(
        cfg,
        syncer,
        browse_rg=browse_rg,
        fetch_release=fetch_release,
    )
    # The mis-tag claims its purchase out of the potential-downloads list (it's
    # now represented by the mis-tag card); unrelated pending is untouched.
    assert pd.get(9999) is None
    assert pd.get(9998) is not None
    loaded = sc.read(d)
    assert loaded.mb_release_id is None  # wrong tag cleared
    cand = loaded.mb_match_candidate
    assert cand.mb_release_id == "rel-correct"  # the suggestion
    # Mis-tag provenance is structured, NOT crammed into the matcher's notes —
    # both releases named, with disambiguation kept separate from the title.
    assert cand.mistag_tagged_mbid == "rel-wrong"
    assert cand.mistag_tagged_label == "Cell / Live in Corfu"
    assert not cand.mistag_tagged_disambig  # standard edition has none
    assert cand.mistag_owned_label == "Cell / Live in Corfu"
    assert cand.mistag_owned_disambig == "24-bit"
    assert cand.mistag_owned_url == "https://ultimae.bandcamp.com/album/live-in-corfu-24bit"
    assert cand.mistag_release_group_mbid == "rg-1"
    assert not any("release group" in n for n in cand.notes)
    album = next(a for a in scanner.scan(cfg.paths.music_dir) if a.path == d)
    assert album.state == AlbumState.NEEDS_MBID
    msgs = [e.message for e in activity.recent(10)]
    assert any("Possible mis-tag" in m and "Cell / Live in Corfu" in m for m in msgs)
    # #97: the warning names and links its album. The id is read back AFTER the
    # demote rewrote the sidecar (which clears the MBID), so it still resolves.
    entry = next(e for e in activity.recent(10) if "Possible mis-tag" in e.message)
    assert entry.album_id == _id_for(cfg, d)
    # Named from the album ON DISK, not the MusicBrainz release — the feed should
    # call it what the user's library calls it.
    assert entry.album_label == album.title


def test_detect_mistag_no_action_when_no_owned_sibling(cfg):
    """No edition in the album's release group is owned → not a mis-tag."""
    from harmonist.web.main import _detect_mistags_after_sync

    cfg.paths.music_dir.mkdir(parents=True, exist_ok=True)
    d = _needs_sync_album(cfg, "Solo", "rel-a")
    syncer = _FakeSyncer([(1, "https://x.bandcamp.com/album/unrelated", "X / Other")])
    _detect_mistags_after_sync(
        cfg,
        syncer,
        # The group's only other edition isn't among the owned purchases.
        browse_rg=lambda rg: [("rel-b", ["https://x.bandcamp.com/album/different"])],
        fetch_release=lambda m: _release_with_rg(m, "rg-a"),
    )
    assert sc.read(d).mb_release_id == "rel-a"  # untouched


def test_detect_mistag_skips_when_multiple_owned_editions(cfg):
    """You own two editions in the album's release group → ambiguous → skip."""
    from harmonist.web.main import _detect_mistags_after_sync

    cfg.paths.music_dir.mkdir(parents=True, exist_ok=True)
    d = _needs_sync_album(cfg, "Amb", "rel-wrong")
    syncer = _FakeSyncer(
        [
            (1, "https://x.bandcamp.com/album/a", "X / A"),
            (2, "https://x.bandcamp.com/album/b", "X / B"),
        ]
    )
    _detect_mistags_after_sync(
        cfg,
        syncer,
        browse_rg=lambda rg: [
            ("rel-a", ["https://x.bandcamp.com/album/a"]),
            ("rel-b", ["https://x.bandcamp.com/album/b"]),  # both owned → ambiguous
        ],
        fetch_release=lambda m: _release_with_rg(m, "rg-1"),
    )
    assert sc.read(d).mb_release_id == "rel-wrong"  # ambiguous → untouched


def test_detect_mistag_bails_over_album_cap(cfg, monkeypatch):
    """If the unmatched-album set exceeds the cap, bail with an Activity warning
    (the cap is on Set A now, not on the owned-purchase count)."""
    from harmonist import activity
    from harmonist.web import main as main_mod

    monkeypatch.setattr(main_mod, "_MISTAG_DETECTION_MAX_ALBUMS", 1)
    cfg.paths.music_dir.mkdir(parents=True, exist_ok=True)
    _needs_sync_album(cfg, "A1", "rel-1")
    _needs_sync_album(cfg, "A2", "rel-2")  # 2 albums > cap of 1
    syncer = _FakeSyncer([(1, "https://x.bandcamp.com/album/owned", "X / Owned")])
    called: list[str] = []

    def browse_rg(rg: str) -> list[tuple[str, list[str]]]:
        called.append(rg)
        return []

    activity.clear()
    main_mod._detect_mistags_after_sync(
        cfg,
        syncer,
        browse_rg=browse_rg,
        fetch_release=lambda m: _release_with_rg(m, "rg"),
    )
    assert called == []  # bailed before any browse
    assert any(
        "Mis-tag detection skipped" in e.message and e.level == "warning"
        for e in activity.recent(10)
    )


def test_link_unmatched_by_release_urls_links_via_alternate_slug(cfg):
    """A release with two Bandcamp URLs (…/idleness and …/idleness-2): the album
    is tagged with one slug but the purchase used the other → the plain slug
    match misses it. Linking via the release's *full* URL set fills item_id."""
    from harmonist.web.main import _link_unmatched_by_release_urls

    cfg.paths.music_dir.mkdir(parents=True, exist_ok=True)
    d = _needs_sync_album(cfg, "Idleness", "rel-x")  # store_url slug 'idleness'
    syncer = _FakeSyncer([(555, "https://yann.bandcamp.com/album/idleness-2", "Yann / Idleness")])
    _link_unmatched_by_release_urls(
        cfg,
        syncer,
        fetch_urls=lambda m: [
            "https://yann.bandcamp.com/album/idleness",
            "https://yann.bandcamp.com/album/idleness-2",  # the purchase's slug
        ],
    )
    linked = sc.read(d)
    assert linked.bandcamp.item_id == 555  # Needs Link → Library
    assert linked.mb_release_id == "rel-x"  # tag preserved
    assert linked.store_url == "https://yann.bandcamp.com/album/idleness-2"  # purchase URL adopted


def test_link_unmatched_by_release_urls_no_match_leaves_album(cfg):
    """No owned purchase among the release's URLs → album untouched (it goes on
    to mis-tag detection / surrender)."""
    from harmonist.web.main import _link_unmatched_by_release_urls

    cfg.paths.music_dir.mkdir(parents=True, exist_ok=True)
    d = _needs_sync_album(cfg, "Solo", "rel-y")
    syncer = _FakeSyncer([(7, "https://x.bandcamp.com/album/unrelated", "X / Other")])
    _link_unmatched_by_release_urls(
        cfg,
        syncer,
        fetch_urls=lambda m: [
            "https://x.bandcamp.com/album/solo",
            "https://x.bandcamp.com/album/solo-2",
        ],
    )
    r = sc.read(d)  # still Needs Link: no item_id, store_url not adopted
    assert r.bandcamp is None or r.bandcamp.item_id is None
    assert r.store_url == "https://label.bandcamp.com/album/solo"


def test_reject_clears_candidate(client, cfg):
    d = _make_album(cfg, "RC")
    sc.write(
        d,
        Sidecar(
            store_url="https://x.bandcamp.com/album/y",
            bandcamp=BandcampInfo(item_id=1),
            mb_match_candidate=MatchCandidate(
                mb_release_id="rel-zzz",
                confidence="approximate",
                file_count=1,
                track_count=1,
            ),
        ),
    )
    aid = _id_for(cfg, d)
    r = client.post(f"/reject/{aid}")
    assert r.status_code == 200
    loaded = sc.read(d)
    assert loaded.mb_match_candidate is None


def test_unconfirmed_url_update(client, cfg):
    d = _make_album(cfg, "UB")
    audio = MP4(d / "01 Track.m4a")
    audio[ATOM_MB_ALBUM_ID] = [b"rel-aaa"]
    audio.save()
    sc.write(
        d,
        Sidecar(
            store_url="https://x.bandcamp.com/album/old",
            bandcamp=BandcampInfo(item_id=None),
            mb_release_id="rel-aaa",
            tagged_at=datetime.now(UTC),
        ),
    )
    aid = _id_for(cfg, d)
    r = client.post(f"/unconfirmed/{aid}/url", data={"url": "https://x.bandcamp.com/album/new"})
    assert r.status_code == 200
    loaded = sc.read(d)
    assert loaded.store_url == "https://x.bandcamp.com/album/new"
    assert loaded.bandcamp is None or loaded.bandcamp.item_id is None


def test_unconfirmed_mark_manual(client, cfg):
    d = _make_album(cfg, "UB2")
    audio = MP4(d / "01 Track.m4a")
    audio[ATOM_MB_ALBUM_ID] = [b"rel-aaa"]
    audio.save()
    sc.write(
        d,
        Sidecar(
            store_url="https://x.bandcamp.com/album/y",
            bandcamp=BandcampInfo(item_id=None),
            mb_release_id="rel-aaa",
            tagged_at=datetime.now(UTC),
        ),
    )
    aid = _id_for(cfg, d)
    r = client.post(f"/unconfirmed/{aid}/manual")
    assert r.status_code == 200
    loaded = sc.read(d)
    assert loaded.store_url is None
    assert loaded.bandcamp is None
    assert loaded.mb_release_id == "rel-aaa"  # preserved


def test_404_for_missing_album(client):
    r = client.post("/recheck/nonexistent")
    assert r.status_code == 404


# ---------- manual ingest flow ----------


def test_manual_search_returns_results(client, cfg, monkeypatch):
    d = _make_album(cfg, "Search Album")
    aid = _id_for(cfg, d)
    monkeypatch.setattr(
        "harmonist.mb_search.search_releases",
        lambda artist, title, limit=10: [
            {
                "id": "rel-1",
                "title": "Result A",
                "artist": "X",
                "date": "2020",
                "country": "GB",
                "status": "Official",
                "track_count": 10,
                "label": "Label",
                "catalog_number": "CAT1",
            },
            {
                "id": "rel-2",
                "title": "Result B",
                "artist": "X",
                "date": "2021",
                "country": "US",
                "status": "Official",
                "track_count": 12,
                "label": None,
                "catalog_number": None,
            },
        ],
    )
    r = client.post(f"/manual/{aid}/search", data={"artist": "X", "title": "Y"})
    assert r.status_code == 200
    assert "Result A" in r.text
    assert "Result B" in r.text
    assert "rel-1" in r.text
    assert 'name="mbid"' in r.text  # hidden mbid input on each "Use" button
    # Each row links out to the release on MusicBrainz for closer inspection.
    assert "musicbrainz.org/release/rel-1" in r.text
    assert "musicbrainz.org/release/rel-2" in r.text
    # The local album has 1 file; both results (10/12 tracks) mismatch and are
    # struck through with an explanatory tooltip.
    assert "line-through" in r.text
    assert "your album has 1 file" in r.text


def test_manual_search_no_strikethrough_on_track_count_match(client, cfg, monkeypatch):
    """A candidate whose track count matches the local file count isn't flagged."""
    d = _make_album(cfg, "MatchCount")  # 1 file
    aid = _id_for(cfg, d)
    monkeypatch.setattr(
        "harmonist.mb_search.search_releases",
        lambda artist, title, limit=10: [
            {"id": "rel-9", "title": "Exactly One", "artist": "X", "track_count": 1}
        ],
    )
    r = client.post(f"/manual/{aid}/search", data={"artist": "X", "title": "Y"})
    assert "rel-9" in r.text
    assert "line-through" not in r.text


def test_manual_search_caps_results_at_five(client, cfg, monkeypatch):
    """The route requests at most 5 from MB — beyond that, MB's own search."""
    seen = {}

    def fake_search(artist, title, limit=10):
        seen["limit"] = limit
        return []

    monkeypatch.setattr("harmonist.mb_search.search_releases", fake_search)
    d = _make_album(cfg, "CapSearch")
    aid = _id_for(cfg, d)
    client.post(f"/manual/{aid}/search", data={"artist": "X", "title": "Y"})
    assert seen["limit"] == 5


def test_manual_search_empty_results(client, cfg, monkeypatch):
    d = _make_album(cfg, "NoResults")
    aid = _id_for(cfg, d)
    monkeypatch.setattr(
        "harmonist.mb_search.search_releases",
        lambda artist, title, limit=10: [],
    )
    r = client.post(f"/manual/{aid}/search", data={"artist": "X", "title": "Y"})
    assert r.status_code == 200
    assert "No matches" in r.text


def test_manual_search_handles_mb_error(client, cfg, monkeypatch):
    d = _make_album(cfg, "Errsearch")
    aid = _id_for(cfg, d)
    from harmonist.mb_search import MBSearchError

    def explode(artist, title, limit=10):
        raise MBSearchError("MB down")

    monkeypatch.setattr("harmonist.mb_search.search_releases", explode)
    r = client.post(f"/manual/{aid}/search", data={"artist": "X", "title": "Y"})
    assert r.status_code == 200
    assert "MB search failed" in r.text


def _needs_mbid_with_store_url(cfg, name: str, store_url: str):
    """A NEEDS_MBID album (store URL on file, no MBID yet) for picker tests."""
    d = _make_album(cfg, name)
    sc.write(
        d,
        Sidecar(store_url=store_url),
    )
    return d


def test_needs_mbid_store_url_offers_both_search_modes(client, cfg):
    """A Needs-MBID card with a store URL renders ONE find-a-release area with
    a radio to pick the search method (store URL vs name) — both controls
    present, mutually exclusive via the .find-mode :has() rule."""
    d = _needs_mbid_with_store_url(cfg, "Modes", "https://x.bandcamp.com/album/y")
    aid = _id_for(cfg, d)
    r = client.get("/tasks")
    assert "Find a different release" in r.text
    # Both radio options + their controls render (one results box for both).
    assert 'value="url"' in r.text
    assert 'value="name"' in r.text
    assert "Look up releases at this URL" in r.text  # store-URL mode action
    assert f"/manual/{aid}/search" in r.text  # name-mode form
    assert r.text.count('id="mbid-results-') == 1  # single shared results box


def test_manual_search_results_carry_heading(client, cfg, monkeypatch):
    """Name-search results are headed so it's clear which search ran."""
    d = _make_album(cfg, "Headed")
    aid = _id_for(cfg, d)
    monkeypatch.setattr(
        "harmonist.mb_search.search_releases",
        lambda artist, title, limit=10: [
            {"id": "rel-h", "title": "H", "artist": "X", "track_count": 1}
        ],
    )
    r = client.post(f"/manual/{aid}/search", data={"artist": "X", "title": "Y"})
    assert "MusicBrainz search results" in r.text


def test_manual_candidates_lists_store_url_releases(client, cfg, monkeypatch):
    """The Choose-release picker lists the MB releases linked to the store URL,
    with disambiguation + media, each with a Use button and MB link."""
    cross = "\N{MULTIPLICATION SIGN}"
    d = _needs_mbid_with_store_url(cfg, "PickMe", "https://x.bandcamp.com/album/y")
    aid = _id_for(cfg, d)
    monkeypatch.setattr(
        "harmonist.mb_lookup.candidate_summaries_for_url",
        lambda url: (
            [
                {
                    "id": "rel-std",
                    "title": "Life²",
                    "disambiguation": "",
                    "artist": "Asura",
                    "track_count": 11,
                    "media": "CD",
                    "date": "2010",
                    "country": "FR",
                    "status": "Official",
                    "label": "Ultimae",
                    "catalog_number": "ULT-1",
                },
                {
                    "id": "rel-24",
                    "title": "Life²",
                    "disambiguation": "24-bit",
                    "artist": "Asura",
                    "track_count": 11,
                    "media": f"2{cross}CD",
                    "date": "2010",
                    "country": "FR",
                    "status": "Official",
                    "label": "Ultimae",
                    "catalog_number": "ULT-2",
                },
            ],
            2,
        ),
    )
    r = client.post(f"/manual/{aid}/candidates")
    assert r.status_code == 200
    assert "Releases linked to this store URL" in r.text
    assert "musicbrainz.org/release/rel-std" in r.text
    assert "musicbrainz.org/release/rel-24" in r.text
    assert "(24-bit)" in r.text  # disambiguation rendered distinctly
    assert f"2{cross}CD" in r.text  # media summary
    assert r.text.count('name="mbid"') == 2  # a Use button per release


def test_recheck_escapes_mb_error_text_in_the_flash(client, cfg, monkeypatch):
    """#142: an MBError message reaches the flash fragment as text, not markup.

    The message wraps upstream musicbrainzngs output, so it originates off-box.
    The diagnostic still has to be readable — this pins escaped, not dropped.
    """
    d = _needs_mbid_with_store_url(cfg, "Hostile", "https://x.bandcamp.com/album/y")
    aid = _id_for(cfg, d)

    def boom(url):
        raise mb_lookup.MBError('MB request failed: <script>alert(1)</script> & "quoted"')

    monkeypatch.setattr("harmonist.mb_lookup.lookup_by_bandcamp_url", boom)

    r = client.post(f"/recheck/{aid}")
    assert r.status_code == 200
    assert "<script>" not in r.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in r.text
    assert "&amp;" in r.text and "&quot;quoted&quot;" in r.text
    assert "MB lookup failed" in r.text  # the diagnostic survives, escaped


def test_recheck_multiple_matches_shows_picker_not_autopick(client, cfg, monkeypatch):
    """When a store URL maps to several MB releases, Recheck stops guessing —
    it retargets the picker into the card's results box and leaves the album
    untagged for the user to choose."""
    d = _needs_mbid_with_store_url(cfg, "Ambiguous", "https://x.bandcamp.com/album/y")
    aid = _id_for(cfg, d)
    monkeypatch.setattr(
        "harmonist.mb_lookup.lookup_by_bandcamp_url",
        lambda url: ["rel-a", "rel-b"],
    )
    monkeypatch.setattr(
        "harmonist.mb_lookup.candidate_summaries_for_url",
        lambda url: (
            [
                {
                    "id": "rel-a",
                    "title": "A",
                    "disambiguation": "",
                    "artist": "X",
                    "track_count": 9,
                    "media": "CD",
                    "date": None,
                    "country": None,
                    "status": None,
                    "label": None,
                    "catalog_number": None,
                },
                {
                    "id": "rel-b",
                    "title": "B",
                    "disambiguation": "",
                    "artist": "X",
                    "track_count": 10,
                    "media": "Digital Media",
                    "date": None,
                    "country": None,
                    "status": None,
                    "label": None,
                    "catalog_number": None,
                },
            ],
            2,
        ),
    )
    r = client.post(f"/recheck/{aid}")
    assert r.status_code == 200
    # The button posts with hx-swap=none, so the response retargets the picker.
    assert r.headers.get("HX-Retarget") == f"#mbid-results-{aid}"
    assert r.headers.get("HX-Reswap") == "innerHTML"
    assert "pick the right one" in r.text
    assert "musicbrainz.org/release/rel-a" in r.text
    # No silent auto-pick: the album stays untagged.
    loaded = sc.read(d)
    assert loaded.mb_release_id is None
    assert loaded.mb_match_candidate is None


def test_manual_assign_with_full_url(client, cfg, monkeypatch):
    """Pasting an MB release URL should extract the MBID."""
    d = _make_album(cfg, "AssignURL")
    aid = _id_for(cfg, d)
    captured = {}

    def fake_fetch(mbid):
        captured["mbid"] = mbid
        return _release_for_match(mbid, n_tracks=1)

    monkeypatch.setattr("harmonist.mb_lookup.fetch_release", fake_fetch)
    monkeypatch.setattr("harmonist.cover_art.ensure_cover", lambda *a, **kw: None)

    r = client.post(
        f"/manual/{aid}/assign",
        data={"mbid": "https://musicbrainz.org/release/abc12345-1234-1234-1234-1234567890ab"},
    )
    assert r.status_code == 200
    assert captured["mbid"] == "abc12345-1234-1234-1234-1234567890ab"
    loaded = sc.read(d)
    assert loaded.store_url is None
    assert loaded.mb_release_id == "abc12345-1234-1234-1234-1234567890ab"


def test_manual_assign_with_bare_mbid(client, cfg, monkeypatch):
    d = _make_album(cfg, "AssignMBID")
    aid = _id_for(cfg, d)

    monkeypatch.setattr(
        "harmonist.mb_lookup.fetch_release",
        lambda mbid: _release_for_match(mbid, n_tracks=1),
    )
    monkeypatch.setattr("harmonist.cover_art.ensure_cover", lambda *a, **kw: None)

    r = client.post(
        f"/manual/{aid}/assign",
        data={"mbid": "abc12345-1234-1234-1234-1234567890ab"},
    )
    assert r.status_code == 200
    loaded = sc.read(d)
    assert loaded.mb_release_id == "abc12345-1234-1234-1234-1234567890ab"


_ASSIGN_MBID = "abc12345-1234-1234-1234-1234567890ab"


def _state_of(cfg, album_dir):
    from harmonist import scanner

    for a in scanner.scan(cfg.paths.music_dir):
        if a.path == album_dir:
            return a.state
    raise AssertionError(f"no album at {album_dir}")


def test_manual_assign_derives_store_url_from_embedded_comment(client, cfg, monkeypatch):
    """A manual download with a precise /album/ URL in ©cmt → store_url recorded
    from the comment (no MB url-rel lookup) → album lands in Needs Link."""
    from harmonist.models import AlbumState

    d = _make_album(cfg, "EmbeddedURL", comment="https://artist.bandcamp.com/album/x")
    aid = _id_for(cfg, d)
    monkeypatch.setattr(
        "harmonist.mb_lookup.fetch_release", lambda m: _release_for_match(m, n_tracks=1)
    )
    monkeypatch.setattr("harmonist.cover_art.ensure_cover", lambda *a, **kw: None)

    def no_urls(_m):
        raise AssertionError("MB url-rels must not be queried when ©cmt has a precise URL")

    monkeypatch.setattr("harmonist.mb_lookup.fetch_release_urls", no_urls)

    r = client.post(f"/manual/{aid}/assign", data={"mbid": _ASSIGN_MBID})
    assert r.status_code == 200
    assert sc.read(d).store_url == "https://artist.bandcamp.com/album/x"
    assert _state_of(cfg, d) == AlbumState.NEEDS_SYNC


def test_manual_assign_falls_back_to_mb_url_when_comment_is_root(client, cfg, monkeypatch):
    """Artist-root ©cmt + MB has a Bandcamp url-rel → MB's canonical URL recorded."""
    from harmonist.models import AlbumState

    d = _make_album(cfg, "RootURL", comment="Visit https://artist.bandcamp.com")
    aid = _id_for(cfg, d)
    monkeypatch.setattr(
        "harmonist.mb_lookup.fetch_release", lambda m: _release_for_match(m, n_tracks=1)
    )
    monkeypatch.setattr("harmonist.cover_art.ensure_cover", lambda *a, **kw: None)
    monkeypatch.setattr(
        "harmonist.mb_lookup.fetch_release_urls",
        lambda _m: ["https://artist.bandcamp.com/album/canonical"],
    )

    r = client.post(f"/manual/{aid}/assign", data={"mbid": _ASSIGN_MBID})
    assert r.status_code == 200
    assert sc.read(d).store_url == "https://artist.bandcamp.com/album/canonical"
    assert _state_of(cfg, d) == AlbumState.NEEDS_SYNC


def test_manual_assign_uses_artist_root_placeholder_when_no_mb_url(client, cfg, monkeypatch):
    """Artist-root ©cmt + MB has no Bandcamp url-rel → keep the artist-root as a
    placeholder store_url so it's still recognised as a Bandcamp purchase."""
    from harmonist.models import AlbumState

    d = _make_album(cfg, "Placeholder", comment="Visit https://artist.bandcamp.com")
    aid = _id_for(cfg, d)
    monkeypatch.setattr(
        "harmonist.mb_lookup.fetch_release", lambda m: _release_for_match(m, n_tracks=1)
    )
    monkeypatch.setattr("harmonist.cover_art.ensure_cover", lambda *a, **kw: None)
    monkeypatch.setattr("harmonist.mb_lookup.fetch_release_urls", lambda _m: [])

    r = client.post(f"/manual/{aid}/assign", data={"mbid": _ASSIGN_MBID})
    assert r.status_code == 200
    assert sc.read(d).store_url == "https://artist.bandcamp.com"
    assert _state_of(cfg, d) == AlbumState.NEEDS_SYNC


def test_manual_assign_store_url_derivation_error_is_swallowed(client, cfg, monkeypatch):
    """If store_url derivation raises, tagging still succeeds (store_url stays None)."""
    d = _make_album(cfg, "BoomURL")
    aid = _id_for(cfg, d)
    monkeypatch.setattr(
        "harmonist.mb_lookup.fetch_release", lambda m: _release_for_match(m, n_tracks=1)
    )
    monkeypatch.setattr("harmonist.cover_art.ensure_cover", lambda *a, **kw: None)

    def boom(*a, **kw):
        raise RuntimeError("derivation failed")

    monkeypatch.setattr("harmonist.reconcile.store_url_for_tagging", boom)

    r = client.post(f"/manual/{aid}/assign", data={"mbid": _ASSIGN_MBID})
    assert r.status_code == 200
    loaded = sc.read(d)
    assert loaded.mb_release_id == _ASSIGN_MBID  # tagged despite the derivation error
    assert loaded.store_url is None


def test_manual_assign_invalid_input(client, cfg):
    """Input that's neither an MBID-shaped token nor an MB URL is rejected."""
    d = _make_album(cfg, "Bad")
    aid = _id_for(cfg, d)
    # Whitespace + punctuation that can't be an MBID
    r = client.post(f"/manual/{aid}/assign", data={"mbid": "this has spaces!"})
    assert r.status_code == 200
    assert "Could not parse" in r.text
    assert not sc.has_sidecar(d)


def test_manual_assign_with_approximate_match_stores_candidate(client, cfg, monkeypatch):
    d = _make_album(cfg, "Approximate")
    aid = _id_for(cfg, d)

    # Release with one track, but a length way off → "approximate"
    monkeypatch.setattr(
        "harmonist.mb_lookup.fetch_release",
        lambda mbid: _release_for_match(mbid, n_tracks=1, length_ms=99999),
    )

    r = client.post(
        f"/manual/{aid}/assign",
        data={"mbid": "abc12345-1234-1234-1234-1234567890ab"},
    )
    assert r.status_code == 200
    assert "review and confirm" in r.text
    loaded = sc.read(d)
    assert loaded.mb_release_id is None
    assert loaded.mb_match_candidate is not None
    assert loaded.mb_match_candidate.confidence == "approximate"


# ---------- URL recovery ----------


def _release_for_match(mbid: str, *, n_tracks: int, length_ms: int = 1000) -> dict:
    return {
        "id": mbid,
        "title": "Title",
        "release-group": {"id": "rg-1", "primary-type": "Album"},
        "artist-credit": [{"artist": {"id": "art-1", "name": "Artist"}, "name": "Artist"}],
        "medium-list": [
            {
                "position": "1",
                "track-list": [
                    {
                        "id": f"rt-{i}",
                        "position": str(i),
                        "title": f"Track {i}",
                        "recording": {
                            "id": f"rec-{i}",
                            "title": f"Track {i}",
                            "length": str(length_ms),
                        },
                    }
                    for i in range(1, n_tracks + 1)
                ],
            }
        ],
    }


# ---------- cover route ----------


def test_cover_returns_404_when_absent(client, cfg):
    d = _make_album(cfg, "NoCover")
    aid = _id_for(cfg, d)
    r = client.get(f"/cover/{aid}")
    assert r.status_code == 404


# ---------- library ----------


def _make_tagged_album(cfg, name: str, *, mbid: str, tagged_at, item_id: int | None = None) -> Path:
    """Create a Done-state album with sidecar + matching MBID tag on the file."""
    d = _make_album(cfg, name)
    audio = MP4(d / "01 Track.m4a")
    audio[ATOM_MB_ALBUM_ID] = [mbid.encode("utf-8")]
    audio.save()
    from harmonist.models import BandcampInfo, Sidecar

    sc.write(
        d,
        Sidecar(
            store_url=(
                f"https://x.bandcamp.com/album/{name.lower().replace(' ', '-')}"
                if item_id
                else None
            ),
            bandcamp=BandcampInfo(item_id=item_id) if item_id else None,
            mb_release_id=mbid,
            tagged_at=tagged_at,
        ),
    )
    return d


def test_library_renders_only_done_albums(client, cfg):
    from datetime import datetime

    _make_album(cfg, "NewAlbum")
    _make_tagged_album(cfg, "DoneOne", mbid="rel-1", tagged_at=datetime.now(UTC), item_id=100)
    r = client.get("/library")
    assert r.status_code == 200
    assert "DoneOne" in r.text
    assert "NewAlbum" not in r.text


def test_library_sorted_by_tagged_at_desc(client, cfg):
    from datetime import datetime, timedelta

    base = datetime.now(UTC)
    _make_tagged_album(cfg, "Old", mbid="rel-old", tagged_at=base - timedelta(days=5), item_id=1)
    _make_tagged_album(cfg, "Mid", mbid="rel-mid", tagged_at=base - timedelta(days=2), item_id=2)
    _make_tagged_album(cfg, "Recent", mbid="rel-recent", tagged_at=base, item_id=3)
    r = client.get("/library")
    text = r.text
    # Recent appears before Mid which appears before Old
    assert text.index("Recent") < text.index("Mid") < text.index("Old")


def _make_library(cfg, count):
    """`count` tagged albums, newest first: Album0 is the most recently tagged,
    so page 1 of the grid holds Album0…Album{limit-1} in order."""
    from datetime import datetime, timedelta

    base = datetime.now(UTC)
    for i in range(count):
        _make_tagged_album(
            cfg, f"Album{i}", mbid=f"rel-{i}", tagged_at=base - timedelta(days=i), item_id=i + 1
        )


def test_library_first_page_holds_one_limit_of_rows(client, cfg):
    _make_library(cfg, 5)
    r = client.get("/library?page=1&limit=2")
    assert r.status_code == 200
    assert r.text.count('id="lib-') == 2
    assert "Album0" in r.text and "Album1" in r.text
    assert "Album2" not in r.text
    # The tab-badge total reflects ALL done albums (5), not the 2 rendered —
    # this is the attribute the Library tab count reads.
    assert 'data-total-done="5"' in r.text


def test_library_page_two_holds_the_next_slice(client, cfg):
    _make_library(cfg, 5)
    r = client.get("/library?page=2&limit=2")
    assert r.text.count('id="lib-') == 2
    assert "Album2" in r.text and "Album3" in r.text
    assert "Album0" not in r.text
    # Every page is a whole render now, search and filters included — it replaces
    # the grid rather than appending to it, so page 2 must stand on its own.
    assert 'id="library-search"' in r.text
    assert 'aria-label="Library filters"' in r.text
    assert 'data-page="2"' in r.text


def test_library_pager_links_carry_prev_and_next(client, cfg):
    _make_library(cfg, 5)
    body = client.get("/library?page=2&limit=2").text
    assert 'hx-get="/library?page=1&limit=2"' in body
    assert 'hx-get="/library?page=3&limit=2"' in body
    # The pushed URL and the href agree, so the address bar names what's on
    # screen and a middle-click opens the same page. Both carry the page SIZE as
    # well as the number (#144) — without it the same link resolves to different
    # albums once the reader picks a different size.
    assert 'href="/?tab=library&page=3&limit=2"' in body
    assert 'hx-push-url="/?tab=library&page=3&limit=2"' in body
    assert "3–4 of 5" in body  # row range readout
    assert "page 2 of 3" in body


def test_library_pager_has_no_link_past_either_end(client, cfg):
    _make_library(cfg, 5)
    first = client.get("/library?page=1&limit=2").text
    assert 'hx-get="/library?page=0' not in first
    assert 'aria-disabled="true"' in first  # Previous is inert, not a link

    last = client.get("/library?page=3&limit=2").text
    assert 'hx-get="/library?page=4' not in last
    assert 'aria-disabled="true"' in last


def test_library_pager_absent_when_everything_fits_on_one_page(client, cfg):
    _make_library(cfg, 2)
    body = client.get("/library?page=1&limit=30").text
    assert "Previous" not in body
    assert "Next" not in body


def test_library_page_beyond_the_end_clamps_to_the_last_page(client, cfg):
    """A page number outlives the albums that filled it — a bookmark, or Back
    after a sync removed some. Clamping keeps a saved link pointing at albums
    instead of resolving to a blank grid that reads as "my library is gone"."""
    _make_library(cfg, 5)
    r = client.get("/library?page=99&limit=2")
    assert r.status_code == 200
    assert 'data-page="3"' in r.text
    assert "Album4" in r.text


def test_library_page_below_one_clamps_up(client, cfg):
    _make_library(cfg, 5)
    r = client.get("/library?page=-3&limit=2")
    assert 'data-page="1"' in r.text
    assert "Album0" in r.text


def test_library_page_size_control_offers_the_sizes_and_marks_the_current_one(client, cfg):
    _make_library(cfg, 3)
    body = client.get("/library?limit=40").text
    for size in (20, 40, 60):
        assert f'<option value="{size}"' in body
    assert '<option value="40" selected>' in body
    assert '<option value="20" selected>' not in body
    # A real form, so it still works with JS off; and the size control must not
    # carry an onclick beside its hx-* (template-lint's rule, #40).
    assert 'method="get" action="/"' in body
    assert "onclick" not in body
    # `submit` must stay in the trigger list beside `change`. Naming `change`
    # alone REPLACES a form's default `submit` trigger, and every submit event
    # then escapes HTMX into a full page navigation. The browser half of this is
    # in test/e2e/test_library_page_size.py, which most runs skip — this is the
    # cheap guard against the attribute being tidied by someone who never sees it.
    assert 'hx-trigger="change, submit"' in body


def test_library_page_size_control_sits_below_the_grid(client, cfg):
    """It is a control you reach for after reading a page, so it belongs at the
    bottom with the pager rather than above the search box, competing for the top
    of the view (#217). Rendering order is the only handle the server has on that."""
    _make_library(cfg, 3)
    body = client.get("/library").text
    assert body.index('id="library-grid"') < body.index('id="library-limit"')
    assert body.index('id="library-search"') < body.index('id="library-limit"')


def test_library_page_size_control_survives_a_single_page(client, cfg):
    """The pager disappears when everything fits on one page; the size control must
    not go with it. Dropping to 20 is how a reader *makes* a second page."""
    _make_library(cfg, 3)
    body = client.get("/library?limit=40").text
    assert 'aria-label="Library pages"' not in body
    assert 'id="library-limit"' in body


def test_library_page_size_control_is_absent_when_there_is_nothing_to_size(client, cfg):
    """An empty Library gets the empty state, not an offer to show 40 of them."""
    body = client.get("/library").text
    assert "No fully-tagged albums yet." in body
    assert 'id="library-limit"' not in body


def test_library_page_size_control_shows_an_off_menu_size_rather_than_lying(client, cfg):
    """A hand-typed `?limit=7` is honoured, so the control has to be able to say
    so. Snapping the <select> to a neighbouring option would make it report a page
    size that isn't the one on screen."""
    _make_library(cfg, 3)
    body = client.get("/library?limit=7").text
    assert "<option selected>7</option>" in body
    assert '<option value="20" selected>' not in body


def test_library_default_page_size_is_twenty(client, cfg):
    _make_library(cfg, 25)
    r = client.get("/library")
    assert r.text.count('id="lib-') == 20
    assert "1–20 of 25" in r.text


def test_library_remembers_a_chosen_page_size_for_the_next_bare_visit(client, cfg):
    """The size the reader picked has to survive the trip to an album page and
    back, and the URL there carries only `?page=`. The cookie is what makes a
    later bare request render their size in the FIRST paint, rather than painting
    the default and correcting it (#144)."""
    _make_library(cfg, 25)
    chosen = client.get("/library?limit=40")
    assert chosen.cookies["harmonist-library-limit"] == "40"

    # TestClient carries the cookie forward, exactly as a browser would.
    assert client.get("/library").text.count('id="lib-') == 25


def test_library_does_not_remember_a_size_the_reader_never_chose(client, cfg):
    """Only an explicit `?limit=` is a choice. Writing the cookie on every render
    would pin readers to a default they never picked."""
    _make_library(cfg, 3)
    assert "harmonist-library-limit" not in client.get("/library").cookies


def test_library_survives_an_unreadable_remembered_size(client, cfg):
    """The cookie outlives the code that wrote it and anyone can edit it. A bad
    value must degrade to the default page, never to a 500."""
    _make_library(cfg, 25)
    client.cookies.set("harmonist-library-limit", "not-a-number")
    r = client.get("/library")
    assert r.status_code == 200
    assert r.text.count('id="lib-') == 20


def test_library_page_size_change_keeps_the_album_you_were_looking_at_on_screen(client, cfg):
    """Carrying the page NUMBER across a size change teleports the reader: page 3
    is rows 41–60 at 20 per page and rows 81–120 at 40. The anchor resolves against
    the new size instead, so the row at the top of the screen stays on screen."""
    _make_library(cfg, 100)
    # Reader is on page 3 of 20 — rows 41–60, i.e. Album40 at the top.
    assert "41–60 of 100" in client.get("/library?page=3&limit=20").text
    # Switching to 40 per page must land on the page holding row 41, not page 3.
    r = client.get("/library?limit=40&anchor=41")
    assert 'data-page="2"' in r.text
    assert "41–80 of 100" in r.text
    assert "Album40" in r.text
    # Only the server knows where the anchor landed, so it corrects the address
    # bar itself rather than leaving a guessed hx-push-url on the control.
    assert r.headers["HX-Push-Url"] == "/?tab=library&page=2&limit=40"


def test_library_refresh_does_not_push_a_history_entry(client, cfg):
    """The grid re-requests itself on every `tasks-changed`. Pushing a URL per
    background refresh would bury the Back button under identical entries — so the
    header is confined to the anchor case."""
    _make_library(cfg, 25)
    assert "HX-Push-Url" not in client.get("/library?page=2&limit=20").headers


# ---------- library filters (#174) ----------
#
# Every album these helpers build is terminal — it lives in the Library, the state
# machine considers it finished — and every one is nonetheless wrong in a way the
# grid does not show. That is the whole premise of the filters.


def _make_incomplete_album(cfg, name, *, mbid, tagged_at, expected=4) -> Path:
    """Tagged against an MB release of `expected` tracks with one file on disk.

    The count lives in the file's own `trkn` total (#195), which is where a real
    tagging leaves it — not in the sidecar."""
    from test.helpers import write_track_totals

    d = _make_tagged_album(cfg, name, mbid=mbid, tagged_at=tagged_at)
    write_track_totals(d, track_total=expected)
    return d


def _make_partially_tagged_album(cfg, name, *, mbid, tagged_at) -> Path:
    """Two files, one carrying the MB Album Id atom and one bare.

    This derives **COMPLETE**, not INCONSISTENT: `_files_tagged_with` is an
    `any()`, and a file missing the field doesn't vote on consistency. So it sits
    in the grid looking finished, which is exactly why it needs a filter.
    """
    d = _make_tagged_album(cfg, name, mbid=mbid, tagged_at=tagged_at)
    shutil.copy(SINE_M4A, d / "02 Track.m4a")
    return d


def _give_cover(album_dir: Path) -> Path:
    """A folder cover, so the album stops matching the No-artwork filter. The
    fixture audio carries no embedded art, so every test album lacks one until
    this is called."""
    (album_dir / "cover.jpg").write_bytes(b"\xff\xd8\xff\xdb not really a jpeg")
    return album_dir


def test_library_filter_narrows_the_grid_to_incomplete_albums(client, cfg):
    from datetime import datetime

    base = datetime.now(UTC)
    _make_tagged_album(cfg, "Whole", mbid="rel-whole", tagged_at=base)
    _make_incomplete_album(cfg, "Short", mbid="rel-short", tagged_at=base)
    body = client.get("/library?filter=incomplete").text
    assert "Short" in body
    assert "Whole" not in body


def test_library_filter_finds_partially_tagged_albums(client, cfg):
    """The filter that earns its place: these albums are COMPLETE, so nothing but
    a 10-pixel "1/2 tagged" line distinguishes them from a finished one."""
    from datetime import datetime

    from harmonist import scanner

    base = datetime.now(UTC)
    _make_tagged_album(cfg, "Clean", mbid="rel-clean", tagged_at=base)
    d = _make_partially_tagged_album(cfg, "Half", mbid="rel-half", tagged_at=base)
    # Precondition: the album really is terminal, or the filter is picking over
    # the inbox's population rather than the Library's.
    assert next(a.state for a in scanner.scan(cfg.paths.music_dir) if a.path == d) == "complete"
    body = client.get("/library?filter=partial").text
    assert "Half" in body
    assert "Clean" not in body


def test_library_filter_finds_albums_with_no_artwork(client, cfg):
    from datetime import datetime

    base = datetime.now(UTC)
    _give_cover(_make_tagged_album(cfg, "Pictured", mbid="rel-pic", tagged_at=base))
    _make_tagged_album(cfg, "Bare", mbid="rel-bare", tagged_at=base)
    body = client.get("/library?filter=no-artwork").text
    assert "Bare" in body
    assert "Pictured" not in body


def test_library_filter_counts_come_from_the_whole_library_not_the_page(client, cfg):
    """A count is a promise about what selecting the option would show. Counting
    only the current page would break that promise on every page but the first."""
    from datetime import datetime, timedelta

    base = datetime.now(UTC)
    for i in range(5):
        _make_incomplete_album(
            cfg, f"Short{i}", mbid=f"rel-short-{i}", tagged_at=base - timedelta(days=i)
        )
    body = client.get("/library?limit=2").text
    assert body.count('id="lib-') == 2  # page 1 holds two albums
    assert re.search(r"Incomplete\s*<span[^>]*>5</span>", body)  # all five, not the two shown


def test_library_total_done_ignores_the_filter(client, cfg):
    """#140's constraint: `data-total-done` reports the whole library, however the
    grid below it is narrowed. A filtered grid must not let it start reporting the
    rows it happens to be rendering — the "show all N" ways out read it, and so do
    the browser tests that wait for a particular render."""
    from datetime import datetime

    base = datetime.now(UTC)
    _make_tagged_album(cfg, "Whole", mbid="rel-whole", tagged_at=base)
    _make_incomplete_album(cfg, "Short", mbid="rel-short", tagged_at=base)
    body = client.get("/library?filter=incomplete").text
    assert 'data-total-done="2"' in body
    assert body.count('id="lib-') == 1  # one incomplete album on screen, of the two


def test_library_filtered_pager_reports_the_filtered_total(client, cfg):
    """The other half of the same split: the row-range readout describes the list
    being paged, which under a filter is a subset."""
    from datetime import datetime, timedelta

    base = datetime.now(UTC)
    for i in range(3):
        _make_tagged_album(cfg, f"Whole{i}", mbid=f"rel-w-{i}", tagged_at=base - timedelta(days=i))
    for i in range(3):
        _make_incomplete_album(
            cfg, f"Short{i}", mbid=f"rel-s-{i}", tagged_at=base - timedelta(days=10 + i)
        )
    body = client.get("/library?filter=incomplete&limit=2").text
    assert "1–2 of 3" in body  # three incomplete albums, not the six on disk
    assert 'data-total-done="6"' in body


def test_library_filter_rides_in_every_pager_url(client, cfg):
    """A pager arrow that dropped the filter would widen the view mid-browse —
    silently, since the grid still renders albums."""
    from datetime import datetime, timedelta

    base = datetime.now(UTC)
    for i in range(5):
        _make_incomplete_album(
            cfg, f"Short{i}", mbid=f"rel-short-{i}", tagged_at=base - timedelta(days=i)
        )
    body = client.get("/library?page=2&limit=2&filter=incomplete").text
    assert 'hx-get="/library?page=1&limit=2&filter=incomplete"' in body
    assert 'hx-get="/library?page=3&limit=2&filter=incomplete"' in body
    assert 'href="/?tab=library&page=3&limit=2&filter=incomplete"' in body
    assert 'hx-push-url="/?tab=library&page=3&limit=2&filter=incomplete"' in body
    # The container's own refresh wiring too — every `tasks-changed` re-requests
    # this view, and without the filter the grid would quietly widen on its own.
    assert 'hx-get="/library?page=2&limit=2&filter=incomplete"' in body
    # And the page-size form, or picking "40 per page" answers a different
    # question than the one on screen.
    assert '<input type="hidden" name="filter" value="incomplete">' in body


def test_library_filter_links_start_at_the_first_page(client, cfg):
    """Filtering asks a new question, so it starts at the first page of the answer.
    Carrying `page` across would ask for page 7 of a 3-album result."""
    from datetime import datetime, timedelta

    base = datetime.now(UTC)
    for i in range(5):
        _make_incomplete_album(
            cfg, f"Short{i}", mbid=f"rel-short-{i}", tagged_at=base - timedelta(days=i)
        )
    body = client.get("/library?page=3&limit=2").text
    assert 'hx-get="/library?limit=2&filter=incomplete"' in body
    assert "filter=incomplete&page=" not in body


def test_library_filter_offering_nothing_is_inert_rather_than_a_link(client, cfg):
    """An option worth zero albums says so instead of promising an empty grid."""
    from datetime import datetime

    _give_cover(_make_tagged_album(cfg, "Fine", mbid="rel-fine", tagged_at=datetime.now(UTC)))
    body = client.get("/library").text
    assert "filter=incomplete" not in body  # nothing incomplete → no link to it
    assert 'aria-disabled="true"' in body
    assert "Incomplete" in body  # still named, still counted


def test_library_filter_matching_nothing_says_so_and_offers_the_way_back(client, cfg):
    """A filter must never be mistakable for data loss. The empty-library wording
    would be a lie here — the library is fine, this question's answer is empty."""
    from datetime import datetime

    _make_tagged_album(cfg, "Whole", mbid="rel-whole", tagged_at=datetime.now(UTC))
    body = client.get("/library?filter=incomplete").text
    assert "No albums match this filter." in body
    assert "No fully-tagged albums yet." not in body
    assert "Show all 1 albums" in body


def test_library_ignores_an_unknown_filter(client, cfg):
    """`?filter=` is untrusted and reflected into the page. An unrecognised slug —
    a mangled link, or one from an older build — shows the library, never an empty
    grid and never its own text back."""
    from datetime import datetime

    _make_tagged_album(cfg, "Whole", mbid="rel-whole", tagged_at=datetime.now(UTC))
    body = client.get("/library?filter=<script>alert(1)</script>").text
    assert "Whole" in body
    assert "alert(1)" not in body
    # Fell through to All, which is what the control reports.
    assert 'aria-current="true">All' in body


def test_library_does_not_remember_a_filter(client, cfg):
    """Page size is a standing preference and gets a cookie (#144). A filter is a
    question asked once — restoring it weeks later reads as "my library shrank"."""
    from datetime import datetime

    base = datetime.now(UTC)
    _make_tagged_album(cfg, "Whole", mbid="rel-whole", tagged_at=base)
    _make_incomplete_album(cfg, "Short", mbid="rel-short", tagged_at=base)
    filtered = client.get("/library?filter=incomplete")
    assert not any("filter" in name for name in filtered.cookies)
    # The next bare visit is the whole library again.
    assert "Whole" in client.get("/library").text


def test_library_filter_rides_in_the_index_url(client, cfg):
    """The inline first paint honours `?filter=`, so a shared link resolves to the
    same view rather than painting the whole library and correcting it."""
    from datetime import datetime

    base = datetime.now(UTC)
    _make_tagged_album(cfg, "Whole", mbid="rel-whole", tagged_at=base)
    _make_incomplete_album(cfg, "Short", mbid="rel-short", tagged_at=base)
    body = client.get("/?tab=library&filter=incomplete").text
    assert "Short" in body
    assert "Whole" not in body


def test_library_filter_survives_the_trip_out_to_an_album_and_back(client, cfg):
    """The round trip #140 exists for. The page size survives it in a cookie; the
    filter has nothing but these two links, so both have to carry it."""
    from datetime import datetime

    d = _make_incomplete_album(cfg, "Short", mbid="rel-short", tagged_at=datetime.now(UTC))
    album_id = _id_for(cfg, d)
    grid = client.get("/library?filter=incomplete").text
    assert f'href="/album/{album_id}?from_filter=incomplete"' in grid

    back = client.get(f"/album/{album_id}?from_filter=incomplete").text
    assert 'href="/?tab=library&filter=incomplete"' in back


def test_album_page_ignores_an_unknown_back_filter(client, cfg):
    """`?from_filter=` reaches an href, so it is validated like every other
    reflected parameter rather than trusted because it looks internal."""
    from datetime import datetime

    d = _make_tagged_album(cfg, "Whole", mbid="rel-whole", tagged_at=datetime.now(UTC))
    body = client.get(f"/album/{_id_for(cfg, d)}?from_filter=nonsense").text
    assert 'href="/?tab=library"' in body
    assert "nonsense" not in body


def test_library_filter_anchor_push_url_keeps_the_filter(client, cfg):
    """Changing the page size inside a filtered view corrects the address bar from
    the server (#144). That correction has to name the filtered view, not drop back
    to the whole library."""
    from datetime import datetime, timedelta

    base = datetime.now(UTC)
    for i in range(5):
        _make_incomplete_album(
            cfg, f"Short{i}", mbid=f"rel-short-{i}", tagged_at=base - timedelta(days=i)
        )
    r = client.get("/library?limit=2&anchor=3&filter=incomplete")
    assert r.headers["HX-Push-Url"] == "/?tab=library&page=2&limit=2&filter=incomplete"


def test_library_filter_row_is_absent_when_the_library_is_empty(client, cfg):
    """Nothing to filter, so no filters — the empty state stands alone."""
    body = client.get("/library").text
    assert "No fully-tagged albums yet." in body
    assert 'aria-label="Library filters"' not in body


# ---------- library search (#180) ----------
#
# The filters above find albums that are *wrong*; search finds the album you have
# in mind. Both narrow the same list, and they compose — so most of what is worth
# testing here is that neither silently drops the other.


def _make_searchable_album(cfg, artist: str, title: str, *, mbid: str, tagged_at) -> Path:
    """A terminal album with a real artist tag, so search has both fields to match.

    `_make_tagged_album` files everything under one hardcoded "Artist" parent and
    tags no artist at all, which is fine for paging but makes every album
    indistinguishable to a search over artist names.
    """
    d = cfg.paths.music_dir / artist / title
    d.mkdir(parents=True)
    shutil.copy(SINE_M4A, d / "01 Track.m4a")
    audio = MP4(d / "01 Track.m4a")
    audio[ATOM_MB_ALBUM_ID] = [mbid.encode("utf-8")]
    audio["aART"] = [artist]
    audio.save()
    from harmonist.models import Sidecar

    sc.write(d, Sidecar(mb_release_id=mbid, tagged_at=tagged_at))
    return d


def _make_two_albums(cfg):
    """One Aphex Twin album and one Björk album — different artists, different
    titles, so any test can tell which one a query found."""
    from datetime import datetime, timedelta

    base = datetime.now(UTC)
    _make_searchable_album(
        cfg, "Aphex Twin", "Selected Ambient Works 85-92", mbid="rel-saw", tagged_at=base
    )
    _make_searchable_album(
        cfg, "Björk", "Post", mbid="rel-post", tagged_at=base - timedelta(days=1)
    )


def test_library_search_narrows_by_album_title(client, cfg):
    _make_two_albums(cfg)
    body = client.get("/library?q=ambient").text
    assert "Selected Ambient Works" in body
    assert "Post" not in body


def test_library_search_narrows_by_artist(client, cfg):
    """The other half of the promise — and the half a title-only search would fail
    silently, since it still returns albums."""
    _make_two_albums(cfg)
    body = client.get("/library?q=aphex").text
    assert "Selected Ambient Works" in body
    assert ">Björk<" not in body


def test_library_search_terms_may_span_both_fields_in_any_order(client, cfg):
    """`aphex ambient` is an artist word and a title word, and nobody types them
    in tag order. Every term must match; where it matches is not the reader's
    problem."""
    _make_two_albums(cfg)
    assert "Selected Ambient Works" in client.get("/library?q=ambient+aphex").text
    assert "Selected Ambient Works" in client.get("/library?q=aphex+ambient").text
    # Both terms have to land, though, or the search would just be an OR.
    assert "Selected Ambient Works" not in client.get("/library?q=aphex+post").text


def test_library_search_ignores_case_accents_and_punctuation(client, cfg):
    """Nobody types the umlaut, and nobody types the hyphen in `85-92`."""
    _make_two_albums(cfg)
    assert ">Post<" in client.get("/library?q=bjork").text
    assert ">Post<" in client.get("/library?q=BJORK").text
    assert "Selected Ambient Works" in client.get("/library?q=85+92").text


def test_library_search_handles_a_name_made_only_of_punctuation(client, cfg):
    """Bands called `!!!` and `†††` are real. Folding a query to nothing and then
    matching everything would quietly ignore a query still visible in the box."""
    from datetime import datetime

    base = datetime.now(UTC)
    _make_searchable_album(cfg, "!!!", "Louden Up Now", mbid="rel-chk", tagged_at=base)
    _make_searchable_album(cfg, "Aphex Twin", "Drukqs", mbid="rel-drukqs", tagged_at=base)
    body = client.get("/library?q=%21%21%21").text
    assert "Louden Up Now" in body
    assert "Drukqs" not in body


def test_library_search_matching_nothing_names_the_query(client, cfg):
    """An empty answer must never read as an empty library, and must say which
    question emptied it."""
    _make_two_albums(cfg)
    body = client.get("/library?q=zzzz").text
    assert "No albums match “zzzz”." in body
    assert "No fully-tagged albums yet." not in body
    assert "Clear the search" in body


def test_library_total_done_ignores_the_search(client, cfg):
    """The other narrowing, same rule: `data-total-done` reports the whole library,
    so a searched grid must not let it start reporting its own rows (#140)."""
    _make_two_albums(cfg)
    body = client.get("/library?q=aphex").text
    assert 'data-total-done="2"' in body
    # The All chip, though, reports what the search left — it is the number the
    # other chips are subsets of, and it is not the library's size any more.
    assert re.search(r'aria-current="true">All\s*<span[^>]*>1</span>', body)


def test_library_filter_counts_are_taken_within_the_search(client, cfg):
    """A chip promising 2 and delivering 1 describes a population the reader
    cannot see. Search narrows first; the counts follow it."""
    from datetime import datetime

    base = datetime.now(UTC)
    # Two coverless albums, only one of them an Aphex one.
    _make_searchable_album(cfg, "Aphex Twin", "Drukqs", mbid="rel-drukqs", tagged_at=base)
    _make_searchable_album(cfg, "Björk", "Post", mbid="rel-post", tagged_at=base)
    assert re.search(r"No artwork\s*<span[^>]*>2</span>", client.get("/library").text)
    assert re.search(r"No artwork\s*<span[^>]*>1</span>", client.get("/library?q=aphex").text)


def test_library_search_composes_with_a_filter(client, cfg):
    """They ask different questions — "what is broken" and "where is this record" —
    so one must not silently cancel the other."""
    from datetime import datetime

    base = datetime.now(UTC)
    _give_cover(_make_searchable_album(cfg, "Aphex Twin", "Drukqs", mbid="rel-d", tagged_at=base))
    _make_searchable_album(cfg, "Aphex Twin", "Syro", mbid="rel-syro", tagged_at=base)
    body = client.get("/library?q=aphex&filter=no-artwork").text
    assert "Syro" in body  # matches the search AND the filter
    assert "Drukqs" not in body  # matches the search, has a cover


def test_library_search_inside_a_filter_says_so(client, cfg):
    """The compound state the reader assembled in two moves, named in words at the
    place they are typing — a chip count changing is too quiet a signal for it."""
    from datetime import datetime

    _make_searchable_album(cfg, "Aphex Twin", "Syro", mbid="rel-syro", tagged_at=datetime.now(UTC))
    body = client.get("/library?q=aphex&filter=no-artwork").text
    assert "Searching only the “No artwork” albums." in body
    # And with no filter there is nothing to disambiguate, so no caption.
    assert "Searching only the" not in client.get("/library?q=aphex").text


def test_library_search_inside_a_filter_offers_both_ways_out(client, cfg):
    """Two narrowings, two escape hatches. A reader who undoes the wrong one is
    still staring at an empty grid and no wiser about why."""
    from datetime import datetime

    _make_searchable_album(cfg, "Björk", "Post", mbid="rel-post", tagged_at=datetime.now(UTC))
    body = client.get("/library?q=zzzz&filter=no-artwork").text
    assert "No “No artwork” albums match “zzzz”." in body
    assert "Search all 1 albums" in body  # drops the filter, keeps the search
    assert "Clear the search" in body  # drops the search, keeps the filter


def test_library_search_rides_in_every_library_url(client, cfg):
    """A control that dropped `q` would widen the view mid-browse, silently, since
    the grid still renders albums. Seventeen call sites; this is the net under
    them (`make template-lint` is the other half)."""
    from datetime import datetime, timedelta

    base = datetime.now(UTC)
    for i in range(5):
        _make_searchable_album(
            cfg, "Aphex Twin", f"Album{i}", mbid=f"rel-{i}", tagged_at=base - timedelta(days=i)
        )
    body = client.get("/library?page=2&limit=2&q=aphex").text
    # Both pager arrows, as href and as hx-push-url.
    assert 'hx-get="/library?page=1&limit=2&q=aphex"' in body
    assert 'href="/?tab=library&page=3&limit=2&q=aphex"' in body
    assert 'hx-push-url="/?tab=library&page=3&limit=2&q=aphex"' in body
    # The container's own refresh wiring — every `tasks-changed` re-requests this
    # view, and without `q` the grid would quietly widen on its own.
    assert 'hx-get="/library?page=2&limit=2&q=aphex"' in body
    # The page-size form, or picking "40 per page" answers a different question.
    assert '<input type="hidden" name="q" value="aphex">' in body
    # And the filter chips, which are supposed to narrow the search, not replace it.
    assert 'hx-get="/library?limit=2&filter=no-artwork&q=aphex"' in body


def test_library_search_links_start_at_the_first_page(client, cfg):
    """Searching asks a new question, so the chips start at the first page of the
    answer rather than asking for page 3 of a one-album result."""
    from datetime import datetime, timedelta

    base = datetime.now(UTC)
    for i in range(5):
        _make_searchable_album(
            cfg, "Aphex Twin", f"Album{i}", mbid=f"rel-{i}", tagged_at=base - timedelta(days=i)
        )
    body = client.get("/library?page=3&limit=2&q=aphex").text
    assert 'hx-get="/library?limit=2&filter=no-artwork&q=aphex"' in body


def test_library_search_is_urlencoded_in_every_url(client, cfg):
    """`q` is free text, unlike the filter's whitelisted slug. A bare `&` would end
    every URL on this page early and silently drop the parameters after it."""
    from datetime import datetime

    _make_searchable_album(
        cfg, "Belle & Sebastian", "Tigermilk", mbid="rel-tiger", tagged_at=datetime.now(UTC)
    )
    body = client.get("/library?q=belle+%26+seb").text
    assert "Tigermilk" in body
    assert "q=belle%20%26%20seb" in body or "q=belle+%26+seb" in body
    assert "q=belle & seb" not in body


def test_library_search_does_not_reflect_markup(client, cfg):
    """`q` is untrusted and, unlike a rejected filter slug, it is echoed back into
    the box and into the empty state. It has to arrive as text."""
    _make_two_albums(cfg)
    body = client.get("/library?q=%3Cscript%3Ealert(1)%3C/script%3E").text
    assert "<script>alert(1)" not in body
    assert "&lt;script&gt;" in body


def test_library_blank_search_is_indistinguishable_from_no_search(client, cfg):
    """`?q=` with nothing in it must not render as a search that found everything —
    no caption, no `q=` trailing through the page's URLs."""
    _make_two_albums(cfg)
    body = client.get("/library?q=+++").text
    assert "Selected Ambient Works" in body and ">Post<" in body
    assert "q=" not in body


def test_library_search_is_length_clamped(client, cfg):
    """`q` is reflected into seventeen URLs, so an unbounded one bloats the whole
    render. Past a hundred characters it is a paste accident, not a search."""
    _make_two_albums(cfg)
    r = client.get("/library?q=" + "z" * 500)
    assert r.status_code == 200
    assert "z" * 200 not in r.text


def test_library_does_not_remember_a_search(client, cfg):
    """Same reasoning as the filter (#174): a page size is a standing preference, a
    search is a question asked once."""
    _make_two_albums(cfg)
    searched = client.get("/library?q=aphex")
    assert not any("q" == name for name in searched.cookies)
    assert ">Post<" in client.get("/library").text


def test_library_search_rides_in_the_index_url(client, cfg):
    """The grid paints inline, so a shared link resolves to the same albums rather
    than painting the whole library and correcting it."""
    _make_two_albums(cfg)
    body = client.get("/?tab=library&q=aphex").text
    assert "Selected Ambient Works" in body
    assert ">Post<" not in body
    # And the box shows what is being searched for, rather than looking unsearched.
    assert 'name="q" value="aphex"' in body


def test_library_search_survives_the_trip_out_to_an_album_and_back(client, cfg):
    """The round trip #140 exists for. Nothing remembers a search, so these two
    links are the only things that can carry it."""
    from datetime import datetime

    d = _make_searchable_album(
        cfg, "Aphex Twin", "Syro", mbid="rel-syro", tagged_at=datetime.now(UTC)
    )
    album_id = _id_for(cfg, d)
    assert f'href="/album/{album_id}?from_q=aphex"' in client.get("/library?q=aphex").text
    back = client.get(f"/album/{album_id}?from_q=aphex").text
    assert 'href="/?tab=library&q=aphex"' in back


def test_library_search_pushes_the_resolved_url_only_when_asked(client, cfg):
    """The search form cannot spell its own `hx-push-url` — it knows neither the
    page size nor the page its query will land on — so it asks the server for one
    with a header. Every other control here is a link that already names its URL,
    and this grid re-requests itself on every `tasks-changed`: pushing on those
    would bury the Back button under identical entries."""
    _make_two_albums(cfg)
    asked = client.get("/library?q=aphex", headers={"X-Harmonist-Push-Url": "1"})
    assert asked.headers["HX-Push-Url"] == "/?tab=library&page=1&limit=20&q=aphex"
    assert "HX-Push-Url" not in client.get("/library?q=aphex").headers


def test_library_search_push_url_encodes_the_query(client, cfg):
    """The server builds this one itself rather than through the template macro, so
    it needs its own encoding — an unencoded `&` would truncate the address bar."""
    _make_two_albums(cfg)
    r = client.get("/library?q=belle+%26+seb", headers={"X-Harmonist-Push-Url": "1"})
    assert r.headers["HX-Push-Url"] == "/?tab=library&page=1&limit=20&q=belle%20%26%20seb"


def test_library_search_box_is_absent_when_the_library_is_empty(client, cfg):
    """Nothing to search, so no box — the empty state stands alone, as it does for
    the filters."""
    body = client.get("/library").text
    assert "No fully-tagged albums yet." in body
    assert 'id="library-search"' not in body


def test_library_page_size_rides_in_the_index_url(client, cfg):
    """`?limit=` belongs to the same addressable view as `?tab=` and `?page=`, so
    a shared link resolves to exactly the albums the sender was looking at."""
    _make_library(cfg, 100)
    body = client.get("/?tab=library&page=2&limit=40").text
    assert body.count('id="lib-') == 40
    assert "41–80 of 100" in body


def test_library_page_size_is_bounded(client, cfg):
    """An unbounded `?limit=` would put a whole library through the template on
    one request."""
    _make_library(cfg, 3)
    r = client.get("/library?limit=100000")
    assert r.status_code == 200
    assert 'data-limit="200"' in r.text


def test_library_empty_still_describes_itself(client, cfg):
    """An empty Library is still a render that says what it is: the e2e harness
    polls `data-total-done` to know when the seeded albums have landed, so the
    attribute has to be there before there is anything to count."""
    r = client.get("/library")
    assert "No fully-tagged albums yet." in r.text
    assert 'data-total-done="0"' in r.text


def test_library_tile_is_a_link_to_the_album_page(client, cfg):
    """#129: a tile is an ordinary link, not a dialog trigger — so every click
    has a real URL that can be shared, bookmarked and gone back from."""
    from datetime import datetime

    d = _make_tagged_album(cfg, "Linked", mbid="abc-123", tagged_at=datetime.now(UTC), item_id=42)
    r = client.get("/library")
    assert f'href="/album/{_id_for(cfg, d)}"' in r.text
    # The heavy detail markup is still not inlined in the grid response.
    assert "musicbrainz.org/release/abc-123" not in r.text


def test_library_detail_links_to_musicbrainz(client, cfg):
    from datetime import datetime

    d = _make_tagged_album(cfg, "Linked", mbid="abc-123", tagged_at=datetime.now(UTC), item_id=42)
    r = client.get(f"/album/{_id_for(cfg, d)}")
    assert "musicbrainz.org/release/abc-123" in r.text


def test_library_detail_shows_store_url_and_item_id(client, cfg):
    from datetime import datetime

    d = _make_tagged_album(cfg, "Linked", mbid="abc-123", tagged_at=datetime.now(UTC), item_id=42)
    r = client.get(f"/album/{_id_for(cfg, d)}")
    assert "x.bandcamp.com" in r.text
    assert "42" in r.text  # item_id


def test_album_page_format_row_says_what_the_format_actually_is(client, cfg):
    """#130: "ALAC" alone doesn't say whether the download is the quality the
    user paid for. The Format row carries the stream detail beside the codec."""
    import re
    from datetime import datetime

    d = _make_tagged_album(cfg, "Detailed", mbid="abc-123", tagged_at=datetime.now(UTC))
    r = client.get(f"/album/{_id_for(cfg, d)}")
    # The whole row, so the two halves are asserted together rather than as
    # substrings that could each be somewhere else on the page.
    row = re.search(r"<dt>Format</dt>\s*<dd>(.*?)</dd>", r.text, re.DOTALL)
    assert row is not None
    assert "ALAC" in row.group(1)  # the fixture is ALAC-encoded
    assert "44.1 kHz · 16 bit" in row.group(1)


def test_album_page_says_when_only_some_files_carry_the_mb_id(client, cfg):
    """#175: the Library tile has said "1/3 tagged" since #139, but the album page
    said nothing — and its Tracks section reports "All N tracks match MusicBrainz",
    because the MB Album Id is identity rather than a compared field, so a file
    missing it produces no finding there. The album is legitimately COMPLETE; that
    is exactly why it has to be said out loud."""
    from datetime import datetime

    d = _make_tagged_album(cfg, "Half", mbid="abc-123", tagged_at=datetime.now(UTC))
    shutil.copy(SINE_M4A, d / "02 Track.m4a")  # second file, no MB Album Id atom
    body = client.get(f"/album/{_id_for(cfg, d)}").text
    assert "MusicBrainz id on 1 of 2 tracks" in body


def test_album_page_stays_quiet_when_every_file_is_tagged(client, cfg):
    """The normal case must not grow a warning — `partial_tag_count` is None both
    when every file carries the id and when none does."""
    from datetime import datetime

    d = _make_tagged_album(cfg, "Whole", mbid="abc-123", tagged_at=datetime.now(UTC))
    assert "MusicBrainz id on" not in client.get(f"/album/{_id_for(cfg, d)}").text


def test_library_detail_omits_bandcamp_removal_controls(client, cfg):
    """#42 interim: the Bandcamp link-removal controls (× forget-URL and Unlink) are
    temporarily pulled — with no way to re-link a Library album, removing a link is a
    dead end. Even a linked album shows no removal control. (The /unlink endpoint
    still exists; only the UI is removed.)"""
    linked = _make_tagged_album(
        cfg, "IsLinked", mbid="rel-l", tagged_at=datetime.now(UTC), item_id=7
    )
    html = client.get(f"/album/{_id_for(cfg, linked)}").text
    assert "/unlink" not in html
    assert "forget_url" not in html


def test_unlink_reverts_complete_album_to_needs_sync(client, cfg):
    """Unlinking clears the purchase item_id (keeps tags + store_url), so the album
    reverts from Library/COMPLETE to NEEDS_SYNC and can be re-linked."""
    from datetime import datetime

    from harmonist import activity
    from harmonist.models import AlbumState
    from harmonist.scanner import scan

    d = _make_tagged_album(cfg, "Undo Me", mbid="rel-u", tagged_at=datetime.now(UTC), item_id=99)
    assert next(a for a in scan(cfg.paths.music_dir) if a.path == d).state == AlbumState.COMPLETE

    activity.clear()
    r = client.post(f"/library/{_id_for(cfg, d)}/unlink")
    assert r.status_code == 200
    loaded = sc.read(d)
    # Link cleared (an all-None bandcamp block is dropped on write).
    assert loaded.bandcamp is None or loaded.bandcamp.item_id is None
    assert loaded.store_url == "https://x.bandcamp.com/album/undo-me"  # kept
    assert next(a for a in scan(cfg.paths.music_dir) if a.path == d).state == AlbumState.NEEDS_SYNC
    # The outcome is in the feed; the purchase id it dropped is in the audit row
    # beneath it, where a structured value belongs (#342).
    assert any("Unlinked" in e.message for e in activity.recent(5))
    assert any(
        "item_id=99->None" in e.message for e in activity_store.recent(5, source=Source.AUDIT)
    )


def test_unlink_wrong_match_forgets_url_and_stays_in_library(client, cfg):
    """'Wrong match' unlink also drops the store_url, so the album stays a
    correctly-tagged Library album (COMPLETE) and no sync can re-link it — unlike a
    plain unlink, which keeps the (wrong) URL and would re-link on the next sync."""
    from datetime import datetime

    from harmonist.bandcamp_hook import survey_album_links
    from harmonist.models import AlbumState
    from harmonist.scanner import scan

    d = _make_tagged_album(cfg, "Wrong Link", mbid="rel-w", tagged_at=datetime.now(UTC), item_id=42)
    assert next(a for a in scan(cfg.paths.music_dir) if a.path == d).state == AlbumState.COMPLETE

    r = client.post(f"/library/{_id_for(cfg, d)}/unlink", data={"forget_url": "true"})
    assert r.status_code == 200
    loaded = sc.read(d)
    assert loaded.store_url is None  # URL forgotten
    assert loaded.bandcamp is None
    assert loaded.mb_release_id == "rel-w"  # still correctly tagged
    # Stays in the Library (not Needs Link), and no sync will re-link it: with no
    # store_url it's invisible to both the slug matcher and adoption.
    assert next(a for a in scan(cfg.paths.music_dir) if a.path == d).state == AlbumState.COMPLETE
    by_slug, slugless, _ = survey_album_links(cfg.paths.music_dir)
    assert d not in slugless
    assert all(d not in paths for paths in by_slug.values())


def test_rematch_clears_mbid_and_demotes_to_needs_mbid(client, cfg):
    """Wrong MusicBrainz match → /rematch clears mb_release_id so the album
    derives back to Needs MBID, keeping the Bandcamp link and the on-disk tags
    intact for the user to re-pick + re-tag (#38)."""
    from harmonist.models import AlbumState
    from harmonist.scanner import scan

    d = _make_tagged_album(
        cfg, "Wrong MB", mbid="rel-wrong", tagged_at=datetime.now(UTC), item_id=7
    )
    assert next(a for a in scan(cfg.paths.music_dir) if a.path == d).state == AlbumState.COMPLETE
    # File carries the (wrong) MB Album Id before re-match.
    assert MP4(d / "01 Track.m4a")[ATOM_MB_ALBUM_ID][0].decode() == "rel-wrong"

    r = client.post(f"/library/{_id_for(cfg, d)}/rematch")
    assert r.status_code == 200
    assert "Needs MBID" in r.text

    loaded = sc.read(d)
    assert loaded.mb_release_id is None  # MB match cleared
    assert loaded.temp_uid is not None  # write() minted a local id (identity invariant)
    assert loaded.store_url is not None  # Bandcamp link kept — only MB was wrong
    assert loaded.bandcamp.item_id == 7
    # Non-destructive: the on-disk tag is untouched until the user re-tags.
    assert MP4(d / "01 Track.m4a")[ATOM_MB_ALBUM_ID][0].decode() == "rel-wrong"
    assert next(a for a in scan(cfg.paths.music_dir) if a.path == d).state == AlbumState.NEEDS_MBID


def test_rematch_drops_everything_describing_the_release(client, cfg):
    """A re-match un-names the release, so everything describing it goes (#166).

    There is no longer a track count among them: since #195 it is read from the
    files' own tags, so there is no stored copy that could outlive the release
    it described — the hazard this test was originally written about cannot
    recur, by construction.
    """
    d = _make_tagged_album(
        cfg, "Counted", mbid="rel-counted", tagged_at=datetime.now(UTC), item_id=7
    )

    client.post(f"/library/{_id_for(cfg, d)}/rematch")

    loaded = sc.read(d)
    assert loaded is not None
    assert loaded.mb_release_id is None
    assert loaded.tagged_at is None
    assert loaded.store_url is not None, "the store link is not a claim about the release"


def test_rematch_offers_no_suggestion_but_an_undo_does(client, cfg):
    """The one thing the two unlink paths must NOT share (#166).

    A re-match means the release was wrong, so putting it straight back as a
    suggestion would undo the user's own judgement. An undo means "put it back",
    so keeping it is the one-click way home.
    """
    d = _make_tagged_album(cfg, "NoSuggestion", mbid="rel-nosug", tagged_at=datetime.now(UTC))

    client.post(f"/library/{_id_for(cfg, d)}/rematch")

    loaded = sc.read(d)
    assert loaded is not None
    assert loaded.mb_match_candidate is None

    # The undo's side of the contract is covered by
    # test_undo_unlinks_the_album_and_keeps_its_release_as_a_suggestion.


def test_rematch_warns_when_no_mb_release(client, cfg):
    """/rematch on an album with no MB release is a no-op warning, not an error."""
    d = _make_album(cfg, "NoMB")  # NEW, no sidecar mb_release_id
    r = client.post(f"/library/{_id_for(cfg, d)}/rematch")
    assert r.status_code == 200
    assert "Nothing to re-match" in r.text


@pytest.mark.parametrize(
    ("endpoint", "verb"),
    [("rematch", "MB match cleared"), ("unlink", "Unlinked")],
)
def test_one_action_writes_one_activity_entry(client, cfg, endpoint, verb):
    """#342: an action that both records and flashes wrote the feed twice.

    `_flash_response` is the funnel for user actions — it writes the activity
    entry itself — so a handler calling `activity.record()` as well produced two
    entries for one press. The duplicate was also the worse of the pair: no
    `album_id`, so it linked to nothing and never joined the album's history,
    with the album's name inlined into the message instead (the #65 mistake).

    The old identifier the duplicate carried is not lost: `_audit_sidecar_change`
    diffs `mbid`/`item_id` as `old->new`, under the same `action_id`, which is
    what the last two assertions hold in place.
    """
    d = _make_tagged_album(
        cfg, f"Dup {endpoint}", mbid="rel-dup", tagged_at=datetime.now(UTC), item_id=7
    )
    aid = _id_for(cfg, d)
    activity_store.clear()

    r = client.post(f"/library/{aid}/{endpoint}")
    assert r.status_code == 200

    feed = activity_store.recent(50, source=Source.ACTIVITY)
    assert len(feed) == 1, f"one press, one entry — got {[e.message for e in feed]}"
    assert feed[0].message.startswith(verb)
    # It is the well-formed one: linked to the album, with the name in its own
    # column rather than inlined into the message.
    assert feed[0].album_id is not None
    assert "Dup" not in feed[0].message

    # The old identifier still survives, in the audit row beneath the same action.
    audit_rows = activity_store.recent(50, source=Source.AUDIT)
    dropped = "mbid=rel-dup->None" if endpoint == "rematch" else "item_id=7->None"
    assert any(dropped in e.message for e in audit_rows), [e.message for e in audit_rows]
    assert {e.action_id for e in audit_rows} == {feed[0].action_id}

    # Pressing it again is a no-op warning, not a second outcome: there is
    # nothing left to clear, and the guard is what keeps the action idempotent.
    r2 = client.post(f"/library/{aid}/{endpoint}")
    assert r2.status_code == 200
    assert "Nothing to" in r2.text
    again = activity_store.recent(50, source=Source.ACTIVITY)
    assert [e.message for e in again if e.message.startswith(verb)] == [feed[0].message]


def test_library_detail_wrong_match_controls_beside_badges(client, cfg):
    """The MB 'wrong match' re-pick (pencil → /rematch) sits beside the MusicBrainz
    badge (#38), where its placement says which badge it acts on."""
    d = _make_tagged_album(cfg, "Badges", mbid="rel-b", tagged_at=datetime.now(UTC), item_id=9)
    aid = _id_for(cfg, d)
    html = client.get(f"/album/{aid}").text
    assert f"/library/{aid}/rematch" in html  # MB re-pick control exists
    assert "Wrong MusicBrainz match" in html  # explanatory tooltip


def test_album_page_actions_navigate_after_success_not_onclick(client, cfg):
    """Regression for #40, now on the page instead of the dialog (#129): an
    action button whose side effect is to leave the page must run that side
    effect from hx-on::after-request — after htmx has done its confirm and its
    request — never from onclick. An onclick that navigates detaches the button
    before htmx's delegated handler runs, so the confirm never shows and the
    request never fires; the control silently does nothing but move you."""
    import re

    d = _make_tagged_album(cfg, "PageActs", mbid="rel-ma", tagged_at=datetime.now(UTC), item_id=5)
    html = client.get(f"/album/{_id_for(cfg, d)}").text
    assert "/rematch" in html  # the pencil sends the album out of the Library
    # No hx-post *action* button may use onclick to navigate.
    action_buttons = re.findall(r"<button[^>]*\shx-post[^>]*>", html)
    assert action_buttons
    assert all("onclick=" not in b for b in action_buttons)
    # The ones that navigate after acting use hx-on::after-request instead.
    assert any("hx-on::after-request" in b for b in action_buttons)


def test_the_album_page_offers_the_inbox_decisions_for_an_untagged_album(client, cfg):
    """#150's second half. An album still in the inbox has a page, and following
    a card's link to it used to strand the reader: the page could only look.

    Asserted through the assign form, which is what actually gets the album out
    of Needs MBID — its presence is the difference between a page you can act on
    and one you can only read.
    """
    d = _make_album(cfg, "Undecided")
    sc.write(d, Sidecar(store_url="https://x.bandcamp.com/album/undecided"))
    aid = _id_for(cfg, d)

    html = client.get(f"/album/{aid}").text

    assert 'id="album-inbox-actions"' in html
    assert f'hx-post="/manual/{aid}/assign"' in html


def test_a_page_that_cannot_re_tag_does_not_offer_to(client, cfg):
    """The dead control #150 found. Re-tag posts to a route that needs a
    MusicBrainz release id, and an album in Needs MBID has none by definition —
    but the panel offered it anyway, styled as the primary action, on a page
    reachable from every inbox card.

    Both halves asserted together, because an absence is only worth checking
    where a live path produces the thing: the tagged album below is that path.
    """
    untagged = _make_album(cfg, "NoRelease")
    sc.write(untagged, Sidecar(store_url="https://x.bandcamp.com/album/norelease"))
    untagged_id = _id_for(cfg, untagged)
    tagged = _make_tagged_album(cfg, "HasRelease", mbid="rel-ok", tagged_at=datetime.now(UTC))
    tagged_id = _id_for(cfg, tagged)

    assert f'hx-post="/retag/{untagged_id}"' not in client.get(f"/album/{untagged_id}").text
    assert f'hx-post="/retag/{tagged_id}"' in client.get(f"/album/{tagged_id}").text


def test_an_inbox_card_links_to_the_album_it_is_about(client, cfg):
    """#150's first half. The cards linked out to MusicBrainz, to Picard and to
    Bandcamp — everywhere except the album they concern.

    Against /tasks, not /: the index ships a shell and pulls the cards into it,
    so the card markup is never in the page's own response."""
    d = _make_album(cfg, "Linkable")
    aid = _id_for(cfg, d)

    assert f'href="/album/{aid}"' in client.get("/tasks").text


def test_an_action_rendered_on_the_album_page_re_renders_it(client, cfg):
    """The same block serves two hosts, and only one of them has an inbox to
    re-render around it (#150). On the album page every one of these decisions
    changes what the page is describing, so the page has to come back.

    Asserted on the inbox copy too: the flag must change the answer, not merely
    be present. Without that half this passes on a template that reloads
    everywhere, which would reload the inbox out from under a half-worked list.

    Matched on the whole attribute rather than on `window.location.reload()`,
    which the page already contains for an unrelated reason — the re-tag
    listener at the foot of album.html. Asserting the bare call passes with the
    flag forced off, which is how this test failed its own mutation check first
    time round.
    """
    reload_attr = 'hx-on::after-request="if (event.detail.successful) window.location.reload()"'
    d = _make_album(cfg, "Reloader")
    sc.write(d, Sidecar(store_url="https://x.bandcamp.com/album/reloader"))
    aid = _id_for(cfg, d)

    assert reload_attr in client.get(f"/album/{aid}").text
    assert reload_attr not in client.get("/tasks").text


# ---------- retag / forget ----------


def test_retag_re_runs_tagger(client, cfg, monkeypatch):
    from datetime import datetime

    d = _make_tagged_album(
        cfg, "ToRetag", mbid="rel-1", tagged_at=datetime(2026, 1, 1, tzinfo=UTC), item_id=1
    )
    monkeypatch.setattr(
        "harmonist.mb_lookup.fetch_release",
        lambda mbid: _release_for_match(mbid, n_tracks=1),
    )
    monkeypatch.setattr("harmonist.cover_art.ensure_cover", lambda *a, **kw: None)

    aid = _id_for(cfg, d)
    r = client.post(f"/retag/{aid}")
    assert r.status_code == 200
    assert "Re-tagged" in r.text
    # tagged_at should be refreshed
    loaded = sc.read(d)
    assert loaded is not None
    assert loaded.tagged_at is not None
    assert loaded.tagged_at > datetime(2026, 1, 1, tzinfo=UTC)


def test_retag_works_on_an_album_confirmed_as_incomplete(client, cfg, monkeypatch):
    """#133: the tagger refuses file_count < track_count unless told the album is
    knowingly incomplete. /retag never told it, so re-tagging failed for exactly
    the albums a MusicBrainz correction is most likely to affect."""
    # One file on disk, and the file's own tags say the release has four tracks.
    d = _make_incomplete_album(
        cfg, "Partial", mbid="rel-part", tagged_at=datetime.now(UTC), expected=4
    )
    monkeypatch.setattr(
        "harmonist.mb_lookup.fetch_release", lambda mbid: _release_for_match(mbid, n_tracks=4)
    )
    monkeypatch.setattr("harmonist.cover_art.ensure_cover", lambda *a, **kw: None)

    r = client.post(f"/retag/{_id_for(cfg, d)}")
    assert r.status_code == 200
    assert "Re-tagged" in r.text
    assert "Re-tag failed" not in r.text


def test_retag_still_refuses_an_unconfirmed_short_album(client, cfg, monkeypatch):
    """The control: an album not already known to be short must still not be
    tagged short, or the guard against tagging a partial download by accident is
    gone (design §15.3). Its files carry no totals, so it derives COMPLETE and
    /retag passes `incomplete=False`.

    Since #252 the refusal is a question rather than an error, so what's asserted
    is that nothing was written — the flash wording is the next test's subject."""
    tagged = datetime(2026, 1, 1, tzinfo=UTC)
    d = _make_tagged_album(cfg, "Unconfirmed", mbid="rel-unc", tagged_at=tagged)
    sc.write(d, Sidecar(mb_release_id="rel-unc", tagged_at=tagged))  # no expectation
    monkeypatch.setattr(
        "harmonist.mb_lookup.fetch_release", lambda mbid: _release_for_match(mbid, n_tracks=4)
    )
    monkeypatch.setattr("harmonist.cover_art.ensure_cover", lambda *a, **kw: None)

    r = client.post(f"/retag/{_id_for(cfg, d)}")
    assert "Re-tagged" not in r.text
    after = sc.read(d)
    assert after is not None and after.tagged_at == tagged, "the files were left alone"


def test_retag_offers_incomplete_when_mb_has_grown_tracks(client, cfg, monkeypatch):
    """#252: the album's files agree among themselves that it is complete — one
    of one — so it derives COMPLETE and `incomplete=False` goes to the tagger.
    MusicBrainz has since gained tracks, so the guard refuses against the *new*
    count and re-tagging was impossible for exactly the album a MusicBrainz
    correction has touched.

    The answer is the two counts and a control that resolves them, not a stack
    trace. The control rides back out of band into the album page's alert slot."""
    import json

    from test.helpers import write_track_totals

    tagged = datetime(2026, 1, 1, tzinfo=UTC)
    d = _make_tagged_album(cfg, "Grown", mbid="rel-grown", tagged_at=tagged)
    write_track_totals(d, track_total=1)  # one file, and the file says 1 of 1
    monkeypatch.setattr(
        "harmonist.mb_lookup.fetch_release", lambda mbid: _release_for_match(mbid, n_tracks=3)
    )
    monkeypatch.setattr("harmonist.cover_art.ensure_cover", lambda *a, **kw: None)
    aid = _id_for(cfg, d)

    r = client.post(f"/retag/{aid}")
    assert r.status_code == 200
    assert "Re-tag failed" not in r.text
    # Both counts, said in the flash rather than left to the panel.
    payload = json.loads(r.headers["HX-Trigger"])["harmonist-status"]
    assert payload["level"] == "warning"
    assert "3 there, 1 here" in payload["details"]
    # The way out is one click from where the problem appeared.
    assert f'id="album-alert-{aid}"' in r.text
    assert 'hx-swap-oob="true"' in r.text
    assert f'hx-post="/retag/{aid}"' in r.text
    assert "accept_short" in r.text
    # Nothing was written: the guard still holds until the user says so.
    after = sc.read(d)
    assert after is not None and after.tagged_at == tagged


def test_retag_as_incomplete_takes_the_grown_releases_tags(client, cfg, monkeypatch):
    """The other half of #252: pressing the offered control re-runs the same
    re-tag with the shortfall accepted. The files take the release's current
    tags — including its higher total — so the album then derives INCOMPLETE
    (design §13.3), which is the true thing to say about it."""
    from harmonist import scanner
    from harmonist.models import AlbumState
    from test.helpers import write_track_totals

    d = _make_tagged_album(cfg, "Grown", mbid="rel-grown", tagged_at=datetime.now(UTC))
    write_track_totals(d, track_total=1)
    monkeypatch.setattr(
        "harmonist.mb_lookup.fetch_release", lambda mbid: _release_for_match(mbid, n_tracks=3)
    )
    monkeypatch.setattr("harmonist.cover_art.ensure_cover", lambda *a, **kw: None)

    r = client.post(f"/retag/{_id_for(cfg, d)}", data={"accept_short": "true"})
    assert r.status_code == 200
    assert "Re-tagged" in r.text
    album = next(a for a in scanner.scan(cfg.paths.music_dir) if a.path == d)
    assert album.state == AlbumState.INCOMPLETE
    assert album.expected_track_count == 3
    # ...and the entry says so, rather than leaving the new badge to be found.
    assert "now listed as incomplete" in r.text

    # Idempotent, and nothing had to be persisted to make it so: the decision
    # lives in the totals the re-tag wrote, so the album now derives INCOMPLETE
    # and a PLAIN re-tag (no `accept_short`) goes through on its own.
    again = client.post(f"/retag/{_id_for(cfg, d)}")
    assert "Re-tagged" in again.text
    assert "Re-tag failed" not in again.text
    still = next(a for a in scanner.scan(cfg.paths.music_dir) if a.path == d)
    assert (still.state, still.expected_track_count) == (AlbumState.INCOMPLETE, 3)


def test_retag_reports_extra_files_as_a_failure_with_no_way_out(client, cfg, monkeypatch):
    """The mismatch #252 does NOT turn into a decision: more files on disk than
    the release has tracks is out of scope for the tagger in both modes (§15.3),
    so incomplete mode would not help and must not be offered."""
    d = _make_tagged_album(cfg, "Extra", mbid="rel-extra", tagged_at=datetime.now(UTC))
    shutil.copy(SINE_M4A, d / "02 Track.m4a")
    monkeypatch.setattr(
        "harmonist.mb_lookup.fetch_release", lambda mbid: _release_for_match(mbid, n_tracks=1)
    )
    monkeypatch.setattr("harmonist.cover_art.ensure_cover", lambda *a, **kw: None)

    r = client.post(f"/retag/{_id_for(cfg, d)}")
    assert "Re-tag failed" in r.text
    assert "accept_short" not in r.text


def test_album_action_records_activity_against_that_album(client, cfg, monkeypatch):
    """End-to-end for #33: a per-album action tags its activity entry with the
    album's id, so the album's history is queryable (and other albums' events
    aren't mixed in)."""
    from harmonist import activity_store

    d = _make_tagged_album(
        cfg, "Historied", mbid="rel-hist", tagged_at=datetime.now(UTC), item_id=3
    )
    aid = _id_for(cfg, d)
    monkeypatch.setattr(
        "harmonist.mb_lookup.fetch_release", lambda mbid: _release_for_match(mbid, n_tracks=1)
    )
    monkeypatch.setattr("harmonist.cover_art.ensure_cover", lambda *a, **kw: None)
    activity_store.clear()

    r = client.post(f"/retag/{aid}")
    assert r.status_code == 200

    mine = activity_store.recent(album_id=aid)
    assert any("Re-tagged" in e.message for e in mine)
    # Nothing recorded against this album belongs to another one.
    assert all(e.album_id == aid for e in mine)


def test_activity_recent_carries_album_id(cfg):
    """#65: the stored album_id must survive the store → Event conversion, or the
    feed template can't link the entry no matter what the DB holds."""
    from harmonist import activity

    activity.clear()
    activity.record("Tagged something", album_id="rel-xyz", album_label="BoC — Geogaddi")
    activity.record("Something general")

    events = {e.message: (e.album_id, e.album_label) for e in activity.recent(10)}
    assert events["Tagged something"] == ("rel-xyz", "BoC — Geogaddi")
    assert events["Something general"] == (None, None)


def test_activity_feed_links_entries_with_an_album(client, cfg):
    """An entry tied to an album renders a real deep link; one without stays
    plain text (nothing to point at)."""
    from harmonist import activity

    activity.clear()
    activity.record("Unlinked", album_id="rel-geo", album_label="BoC — Geogaddi")
    activity.record("Sync finished")

    body = client.get("/activity").text
    # Only the album NAME is the link — the message stays plain text beside it.
    assert ">BoC — Geogaddi</a>" in body
    assert 'href="/album/rel-geo"' in body
    assert "Unlinked" in body
    # The untied entry is present but carries no album column at all.
    assert "Sync finished" in body
    assert 'href="/album/None"' not in body


def test_status_bar_payload_carries_the_album_name(client, cfg, monkeypatch):
    """#68: the status bar has no album column, so the album name must ride in
    the harmonist-status payload — otherwise it just says "Tagged" with no clue
    which album. Carried as its own field, NOT folded into `details` (that would
    duplicate it in the feed, which is what #65 got wrong)."""
    import json as _json

    d = _make_tagged_album(cfg, "BarNamed", mbid="rel-bar", tagged_at=datetime.now(UTC), item_id=11)
    aid = _id_for(cfg, d)
    monkeypatch.setattr(
        "harmonist.mb_lookup.fetch_release", lambda mbid: _release_for_match(mbid, n_tracks=1)
    )
    monkeypatch.setattr("harmonist.cover_art.ensure_cover", lambda *a, **kw: None)

    r = client.post(f"/retag/{aid}")
    payload = _json.loads(r.headers["HX-Trigger"])["harmonist-status"]
    assert "BarNamed" in payload["album"]
    # ...and it is NOT smuggled into details (the feed renders that verbatim).
    assert "BarNamed" not in (payload["details"] or "")


def test_activity_feed_puts_album_before_message(client, cfg):
    """#68: album name leads, message follows — one left-aligned run. The old
    right-aligned column flung them to opposite edges on a wide window."""
    from harmonist import activity

    activity.clear()
    activity.record("Unlinked now", album_id="rel-x", album_label="The Thamesmen — Gimme")

    body = client.get("/activity").text
    # Single entry, so these are unambiguous positions within the one row.
    assert body.index("The Thamesmen — Gimme") < body.index("Unlinked now")


# ---------- The feed poll must not re-send an unchanged feed (#118) ----------


def _feed_version(html: str) -> str:
    m = re.search(r'id="feed-version" data-version="([^"]*)"', html)
    assert m is not None, "no feed-version element — the poll can never match"
    return m.group(1)


def test_an_unchanged_feed_answers_204_and_sends_no_body(client, cfg):
    """The feed polls every 2s. Re-sending every entry each time put 154 KB on
    the wire for a panel that usually hasn't changed, and kept it dimmed by the
    in-flight style for most of the interval (#118). 204 is htmx's documented
    "do not swap"."""
    from harmonist import activity

    activity.clear()
    activity.record("something happened")

    first = client.get("/activity")
    assert first.status_code == 200
    have = _feed_version(first.text)

    again = client.get("/activity", params={"have": have})
    assert again.status_code == 204
    assert again.content == b""


def test_a_new_event_makes_the_feed_answer_200_again(client, cfg):
    """The control: 204 must mean "nothing changed", not "always 204". Without
    this the feed would freeze on whatever it last showed."""
    from harmonist import activity

    activity.clear()
    activity.record("first")
    have = _feed_version(client.get("/activity").text)
    assert client.get("/activity", params={"have": have}).status_code == 204

    activity.record("second")
    r = client.get("/activity", params={"have": have})
    assert r.status_code == 200
    assert "second" in r.text


def test_toggling_technical_detail_is_not_mistaken_for_no_change(client, cfg):
    """The params are part of the version, so a client holding the plain-feed
    token can't be answered 204 when it asks for the audit view."""
    from harmonist import activity, audit

    activity.clear()
    activity.record("visible")
    audit.record("sidecar.update", mbid="a->b")

    have = _feed_version(client.get("/activity").text)
    r = client.get("/activity", params={"have": have, "audit": 1})
    assert r.status_code == 200
    assert "sidecar.update" in r.text


def test_an_unreadable_store_still_carries_a_version(client, broken_store):
    """Otherwise the poll sends an empty `have`, never matches, and re-renders
    the unavailable notice every 2s forever."""
    r = client.get("/activity")
    assert r.status_code == 200
    assert 'id="feed-version"' in r.text


def test_audit_detail_is_capped_but_reports_the_true_total(client, cfg):
    """One action can produce hundreds of rows — the first scan of a large
    library records every album. Rendering them all is what made the payload
    150 KB; the summary still shows the real count (#118)."""
    from harmonist import activity, activity_store, audit
    from harmonist.web.main import AUDIT_DETAIL_LIMIT

    activity.clear()
    n = AUDIT_DETAIL_LIMIT + 15
    with activity_store.action():
        activity.record("Started tracking lots")
        for i in range(n):
            audit.record("album.discovered", album=f"Artist {i}/Album {i}")

    body = client.get("/activity", params={"audit": 1}).text
    assert body.count("album.discovered") == AUDIT_DETAIL_LIMIT
    # The overflow line names the true total, not just what was shown.
    assert f"…and {n - AUDIT_DETAIL_LIMIT} more of {n}" in body


def test_activity_shows_album_label_even_when_link_is_dead(client, cfg):
    """The label is stored with the event, so an entry stays readable after its
    album is gone — the reason we persist it rather than resolving live."""
    from harmonist import activity

    activity.clear()
    activity.record("Tagged", album_id=None, album_label="Departed — Album")

    body = client.get("/activity").text
    assert "Departed — Album" in body  # still named...
    assert 'href="/album/' not in body  # ...but not a link


def test_album_page_shows_history_from_before_the_album_was_re_identified(client, cfg):
    """#103's reason for existing, and the consumer the alias table (#73) was
    built for: an album's records are spread across every id it has ever had.
    Tagging replaces the sidecar's temp_uid with the MBID, so querying the
    current id alone silently drops everything from before — the records most
    likely to explain how the album got into its current state."""
    from harmonist import activity, activity_store

    d = _make_album(cfg, "Historied")
    sc.write(d, Sidecar(store_url="https://x.bandcamp.com/album/h"))
    old_id = _id_for(cfg, d)
    activity_store.clear()
    activity.record("Discovered", album_id=old_id, album_label="Artist — Historied")

    # Tag it: identity moves to the MBID, and the alias is recorded.
    sc.write(d, Sidecar(store_url="https://x.bandcamp.com/album/h", mb_release_id="rel-hist"))
    new_id = _id_for(cfg, d)
    assert new_id != old_id
    activity.record("Tagged", album_id=new_id, album_label="Artist — Historied")

    body = client.get(f"/album/{new_id}").text
    assert "Tagged" in body
    assert "Discovered" in body, "history stopped at the last re-identification"

    # The naive query — current id only — would have missed it entirely.
    naive = [e.message for e in activity_store.recent(50, album_id=new_id)]
    assert "Discovered" not in naive
    assert "Discovered" in [e.message for e in activity_store.album_history(new_id)]


def test_album_page_shows_what_a_tagging_changed_under_its_activity_entry(client, cfg):
    """#86: the per-field record, rendered field-first under the tagging's
    user-facing entry rather than under any one file's audit line."""
    from harmonist import activity, activity_store, audit

    d = _make_album(cfg, "Changed")
    sc.write(d, Sidecar(store_url="https://x.bandcamp.com/album/c", mb_release_id="rel-chg"))
    album_id = _id_for(cfg, d)
    activity_store.clear()

    with activity_store.action():
        activity.record("Re-tagged", album_id=album_id, album_label="Artist — Changed")
        for i, name in enumerate(("01 a.m4a", "02 b.m4a"), start=1):
            event_id = audit.record("tag.track", album_id=album_id, file=name)
            assert event_id is not None
            activity_store.record_tag_changes(
                event_id,
                file=name,
                changes={
                    "label": [None, "Warp Records"],
                    "artist": ["Boards Of Canada", "Boards of Canada"],
                    "mb_track_id": [None, f"rec-{i}"],
                },
                position=str(i),
            )

    body = client.get(f"/album/{album_id}").text

    assert "Warp Records" in body
    # The capitalisation fix is marked in place, so the value is SPLIT around
    # the changed character — `Boards <em class="diff-run">o</em>f Canada`. The
    # whole string deliberately doesn't appear; that's the emphasis working.
    assert "diff-run" in body
    assert "f Canada" in body
    # Album-scoped and track-scoped changes are annotated differently — the
    # #149 split made visible.
    assert "album" in body and "all tracks" in body
    # A field that differs per track gets the expansion; one that doesn't, doesn't.
    assert "Show which tracks" in body
    assert "rec-1" in body and "rec-2" in body
    # ...and its own row states how many answers there are rather than passing
    # track 1's off as the album's (#185). The values live in the expansion.
    assert "2 different values" in body
    assert body.index("2 different values") < body.index("rec-1")

    # Anchored to the activity entry, so it renders ONCE, not once per file.
    assert body.count('class="tag-changes"') == 1


def test_added_tags_render_without_a_before_arrow(client, cfg):
    """#159: a field with nothing before it shows only what was written.

    The fixture changes three fields — two additions (`label`, `mb_track_id`)
    and one replacement (`artist`) — so exactly one arrow should survive. On a
    FIRST tagging every row is an addition, which is what made the old "— →"
    prefix two columns of chrome on the whole list.
    """
    from harmonist import activity, activity_store, audit

    d = _make_album(cfg, "Added")
    sc.write(d, Sidecar(store_url="https://x.bandcamp.com/album/a", mb_release_id="rel-add"))
    album_id = _id_for(cfg, d)
    activity_store.clear()

    with activity_store.action():
        activity.record("Re-tagged", album_id=album_id, album_label="Artist — Added")
        event_id = audit.record("tag.track", album_id=album_id, file="01 a.m4a")
        assert event_id is not None
        activity_store.record_tag_changes(
            event_id,
            file="01 a.m4a",
            changes={
                "label": [None, "Warp Records"],
                "artist": ["Boards Of Canada", "Boards of Canada"],
                "mb_track_id": [None, "rec-1"],
            },
            position="1",
        )

    body = client.get(f"/album/{album_id}").text

    assert body.count("tag-changes__arrow") == 1, "only the replacement keeps its arrow"
    assert "Warp Records" in body, "the added value is still shown"
    # The em-dash standing in for "absent" is gone from the change rows. It
    # remains legitimate in the per-track table, which has a Before column.
    assert 'class="tag-changes__absent" title="Not present before this tagging"' not in body


def test_removed_tags_keep_their_arrow(client, cfg):
    """A removal has to say what went, so it keeps both sides (#159)."""
    from harmonist import activity, activity_store, audit

    d = _make_album(cfg, "Removed")
    sc.write(d, Sidecar(store_url="https://x.bandcamp.com/album/r", mb_release_id="rel-rem"))
    album_id = _id_for(cfg, d)
    activity_store.clear()

    with activity_store.action():
        activity.record("Re-tagged", album_id=album_id, album_label="Artist — Removed")
        event_id = audit.record("tag.track", album_id=album_id, file="01 a.m4a")
        assert event_id is not None
        activity_store.record_tag_changes(
            event_id, file="01 a.m4a", changes={"label": ["Warp Records", None]}, position="1"
        )

    body = client.get(f"/album/{album_id}").text

    assert body.count("tag-changes__arrow") == 1
    assert "Warp Records" in body
    assert "removed" in body


def test_album_page_shows_no_change_detail_when_a_tagging_changed_nothing(client, cfg):
    """A re-tag that found MusicBrainz unchanged writes no detail, so the page
    shows the audit line and nothing else. #32 will run one of these nightly."""
    from harmonist import activity, activity_store, audit

    d = _make_album(cfg, "Unchanged")
    sc.write(d, Sidecar(store_url="https://x.bandcamp.com/album/u", mb_release_id="rel-unc"))
    album_id = _id_for(cfg, d)
    activity_store.clear()
    with activity_store.action():
        activity.record("Re-tagged", album_id=album_id, album_label="Artist — Unchanged")
        audit.record("tag.track", album_id=album_id, file="01 a.m4a")

    body = client.get(f"/album/{album_id}").text

    assert "Re-tagged" in body
    assert 'class="tag-changes"' not in body


def _tagging_with_artwork_change(cfg, name, *, keep_image=True):
    """An album with one recorded tagging that replaced its artwork, and the
    old image in the store (unless `keep_image` says otherwise)."""
    from harmonist import activity_store, artwork_store, audit, formats
    from harmonist.formats.types import TagSet

    d = _make_album(cfg, name)
    # A release id per album: the album id derives from it, so sharing one would
    # make two albums the same album and their histories indistinguishable.
    sc.write(
        d, Sidecar(store_url=f"https://x.bandcamp.com/album/{name}", mb_release_id=f"rel-{name}")
    )
    album_id = _id_for(cfg, d)

    # Unique per album: the store is content-addressed, so an identical image
    # from another album's setup would satisfy this one's lookup and the
    # "evicted" case could never be reproduced.
    old = b"\xff\xd8\xff" + f"OLD-{name}".encode() * 20
    files = sorted(p for p in d.iterdir() if formats.is_supported(p))
    for i, path in enumerate(files):
        formats.write_tags(
            path,
            TagSet(
                mb_album_id=f"rel-{name}",
                album=name,
                album_artist="A",
                title="T",
                artist="A",
                track_num=i + 1,
                track_total=len(files),
            ),
            b"\xff\xd8\xff" + b"NEW" * 60,
        )
    was = artwork_store.digest(old)
    if keep_image:
        artwork_store.keep(old, mime="image/jpeg")

    with activity_store.action():
        from harmonist import activity

        activity.record("Re-tagged", album_id=album_id, album_label=f"Artist — {name}")
        for path in files:
            event_id = audit.record("tag.track", album_id=album_id, file=path.name)
            assert event_id is not None
            activity_store.record_tag_changes(
                event_id,
                file=path.name,
                changes={"artwork": [was, artwork_store.digest(b"\xff\xd8\xff" + b"NEW" * 60)]},
            )
    anchor = next(e.id for e in activity_store.album_history(album_id) if e.message == "Re-tagged")
    return album_id, d, anchor, old


def test_undo_restores_the_artwork_a_tagging_replaced(client, cfg, tmp_path):
    from harmonist import artwork_store, formats

    artwork_store.configure(tmp_path / "artwork")
    album_id, d, anchor, old = _tagging_with_artwork_change(cfg, "ArtUndo")

    r = client.post(f"/artwork/restore/{album_id}", data={"event_id": anchor})

    assert r.status_code == 200
    for path in sorted(p for p in d.iterdir() if formats.is_supported(p)):
        current = formats.read_cover(path)
        assert current is not None and current[0] == old


def test_undo_is_offered_only_while_the_image_is_still_kept(client, cfg, tmp_path):
    """The store evicts oldest-first, so an old enough change is genuinely
    unrevertable. Offering a button that would fail is worse than none."""
    from harmonist import artwork_store

    artwork_store.configure(tmp_path / "artwork")
    kept_id, _, _, _ = _tagging_with_artwork_change(cfg, "ArtKept")
    assert "tag-changes__undo" in client.get(f"/album/{kept_id}").text

    gone_id, _, _, _ = _tagging_with_artwork_change(cfg, "ArtGone", keep_image=False)
    assert "tag-changes__undo" not in client.get(f"/album/{gone_id}").text


def test_undo_says_so_plainly_when_the_image_has_been_evicted(client, cfg, tmp_path):
    from harmonist import artwork_store

    artwork_store.configure(tmp_path / "artwork")
    album_id, _, anchor, _ = _tagging_with_artwork_change(cfg, "ArtEvicted", keep_image=False)

    r = client.post(f"/artwork/restore/{album_id}", data={"event_id": anchor})

    assert r.status_code == 200  # a flash, not a stack trace
    assert "no longer kept" in r.text


def test_undo_rebuilds_its_plan_from_the_albums_own_history(client, cfg, tmp_path):
    """The form names a history row, never files or digests. A row belonging to
    a different album — or to nothing — restores nothing rather than reaching
    for whatever the caller asked for."""
    from harmonist import artwork_store

    artwork_store.configure(tmp_path / "artwork")
    mine, _, _, _ = _tagging_with_artwork_change(cfg, "ArtMine")
    _theirs_id, _, theirs_anchor, _ = _tagging_with_artwork_change(cfg, "ArtTheirs")

    r = client.post(f"/artwork/restore/{mine}", data={"event_id": theirs_anchor})
    assert r.status_code == 404

    r = client.post(f"/artwork/restore/{mine}", data={"event_id": 999999})
    assert r.status_code == 404


# --- undoing a tagging's tags (#157) -----------------------------------------


def _tagging_with_tag_changes(cfg, name, *, release_id=None):
    """An album whose files are really tagged, with one recorded tagging saying
    so — the shape an Undo acts on. Returns (album_id, dir, anchor)."""
    from harmonist import activity, activity_store, audit, formats
    from harmonist.formats.types import TagSet

    d = _make_album(cfg, name)
    sc.write(
        d,
        Sidecar(
            store_url=f"https://x.bandcamp.com/album/{name}",
            mb_release_id=release_id or f"rel-{name}",
        ),
    )
    album_id = _id_for(cfg, d)
    files = sorted(p for p in d.iterdir() if formats.is_supported(p))

    with activity_store.action():
        activity.record("Re-tagged", album_id=album_id, album_label=f"Artist — {name}")
        for i, path in enumerate(files):
            before = formats.write_tags(
                path,
                TagSet(
                    mb_album_id=release_id or f"rel-{name}",
                    album=name,
                    album_artist="A",
                    title=f"T{i + 1}",
                    artist="A",
                    track_num=i + 1,
                    track_total=len(files),
                    label=["Warp Records"],
                ),
                None,
            )
            event_id = audit.record("tag.track", album_id=album_id, file=path.name)
            assert event_id is not None
            activity_store.record_tag_changes(
                event_id,
                file=path.name,
                changes=formats.owned.diff(before, formats.read_owned(path)),
            )
    anchor = next(e.id for e in activity_store.album_history(album_id) if e.message == "Re-tagged")
    return album_id, d, anchor


def test_undo_puts_back_the_tags_a_tagging_changed(client, cfg):
    from harmonist import formats

    album_id, d, anchor = _tagging_with_tag_changes(cfg, "TagUndo")
    path = min(p for p in d.iterdir() if formats.is_supported(p))
    assert formats.read_owned(path)["label"] == ["Warp Records"]

    r = client.post(f"/tags/restore/{album_id}", data={"event_id": anchor})

    assert r.status_code == 200
    assert "Tags put back" in r.text
    after = formats.read_owned(path)
    assert after["label"] in (None, []), "the added tag was removed, not blanked"
    assert after["album"] is None


def test_undo_unlinks_the_album_and_keeps_its_release_as_a_suggestion(client, cfg):
    """#158: the sidecar follows the files. Leaving `mb_release_id` set while
    the files carry no id derives as TAGGING — a spinner with no way out — so
    the link goes too, and the release stays on the card so Confirm puts it
    back in one click."""
    from harmonist import formats

    album_id, d, anchor = _tagging_with_tag_changes(cfg, "TagUnlink")
    path = min(p for p in d.iterdir() if formats.is_supported(p))

    r = client.post(f"/tags/restore/{album_id}", data={"event_id": anchor})

    assert "now Needs MBID" in r.text
    assert formats.read_owned(path)["mb_album_id"] is None

    after = sc.read(d)
    assert after is not None
    assert after.mb_release_id is None, "unlinked"
    assert after.tagged_at is None
    assert after.mb_match_candidate is not None
    assert after.mb_match_candidate.mb_release_id == "rel-TagUnlink", "kept as a suggestion"
    assert after.mb_match_candidate.notes, "and says why the album is back here"
    # Confirmable, unlike a surrender — the whole point is a one-click way back.
    assert after.mb_match_candidate.unmatched_purchase is False


def test_undo_unlink_derives_the_album_back_to_needs_mbid(client, cfg):
    """The transition the sidecar change exists to produce, asserted on the
    state itself rather than on the fields behind it."""
    from harmonist import scanner
    from harmonist.models import AlbumState

    album_id, d, anchor = _tagging_with_tag_changes(cfg, "TagState")
    client.post(f"/tags/restore/{album_id}", data={"event_id": anchor})

    # NEEDS_MBID, specifically not TAGGING — which is what a sidecar still
    # naming a release the files no longer carry would derive as, and which has
    # no action on it and no way out.
    album = next(a for a in scanner.scan(cfg.paths.music_dir) if a.path == d)
    assert album.state is AlbumState.NEEDS_MBID


def test_undo_unlink_after_a_directory_rename_mints_the_new_paths_id(client, cfg):
    """A tagged album's id is its release, so a rename doesn't move it — but an
    unlink has to mint one from the path, and the path has changed.

    The id it mints must be the one the SCANNER will derive for the directory as
    it now stands. Minting from a remembered path would hand the album an id
    nothing on disk agrees with, and its history would split at the rename.
    """
    from harmonist import activity_store, id_registry

    album_id, d, anchor = _tagging_with_tag_changes(cfg, "TagRenamed")
    moved = d.parent / "Renamed After Tagging"
    d.rename(moved)

    r = client.post(f"/tags/restore/{album_id}", data={"event_id": anchor})
    assert "now Needs MBID" in r.text

    after = sc.read(moved)
    assert after is not None
    assert after.mb_release_id is None
    assert after.temp_uid == id_registry.get_or_mint(moved), "the id the scanner will derive"

    # And the history came with it, across both identity changes.
    messages = [e.message for e in activity_store.album_history(after.temp_uid)]
    assert any("Re-tagged" in m for m in messages)


def test_undo_refuses_when_a_file_has_been_renamed(client, cfg):
    """Records name their file, so renaming one puts it out of the undo's reach.

    It refuses rather than doing part of the job: the album is left exactly as
    it was, tags and link both. The record carries three other ways to name the
    track (`track_ref`, `rec_ref`, `position`) that would find it anyway — that
    lookup isn't built yet (#167), and until it is, a clear refusal is the right
    failure.
    """
    from harmonist import formats

    album_id, d, anchor = _tagging_with_tag_changes(cfg, "TagFileRenamed")
    path = min(p for p in d.iterdir() if formats.is_supported(p))
    before = formats.read_owned(path)
    path.rename(path.with_name("99 Renamed.m4a"))

    r = client.post(f"/tags/restore/{album_id}", data={"event_id": anchor})

    # The apostrophe is escaped in the rendered flash (#142).
    assert "Couldn&#x27;t undo" in r.text
    assert "no longer in this album" in r.text
    # Nothing was written, and the album is still linked.
    assert formats.read_owned(d / "99 Renamed.m4a") == before
    after = sc.read(d)
    assert after is not None and after.mb_release_id == "rel-TagFileRenamed"


def test_undo_unlink_keeps_the_albums_history_reachable(client, cfg):
    """Unlinking changes the album's id back to its path-derived temp_uid, so
    the alias row is what stops its whole tagged-era history disappearing from
    the page (#33)."""
    from harmonist import activity_store

    album_id, d, anchor = _tagging_with_tag_changes(cfg, "TagAlias")
    client.post(f"/tags/restore/{album_id}", data={"event_id": anchor})

    new_id = _id_for(cfg, d)
    assert new_id != album_id, "identity moved"
    messages = [e.message for e in activity_store.album_history(new_id)]
    assert any("Re-tagged" in m for m in messages), "the pre-unlink history came with it"


def test_undo_names_the_fields_it_left_alone(client, cfg):
    """The count is for what moved; the names are for what didn't. A field
    changed since is the surprise, and a bare count would leave the reader
    guessing which one."""
    from harmonist import formats

    album_id, d, anchor = _tagging_with_tag_changes(cfg, "TagStale")
    for path in sorted(p for p in d.iterdir() if formats.is_supported(p)):
        # A LIST, as `read_owned` returns for a multi-value field (#334) —
        # handing `write_owned` a bare string writes it one character per value.
        formats.write_owned(path, {**formats.read_owned(path), "label": ["Changed Since"]})

    r = client.post(f"/tags/restore/{album_id}", data={"event_id": anchor})

    assert "Label left alone (changed since)" in r.text
    path = min(p for p in d.iterdir() if formats.is_supported(p))
    assert formats.read_owned(path)["label"] == ["Changed Since"]


def test_undo_tags_button_appears_on_a_tagging_that_changed_tags(client, cfg):
    album_id, _, _ = _tagging_with_tag_changes(cfg, "TagButton")
    assert "tag-changes__undo--tags" in client.get(f"/album/{album_id}").text


def test_undo_tags_button_appears_when_only_the_release_id_changed(client, cfg):
    """A tagging whose only change was the MusicBrainz id is now undoable like
    any other — that used to be the one case with nothing to offer, because the
    id was the one field the revert refused (#158)."""
    from harmonist import activity, activity_store, audit

    d = _make_album(cfg, "OnlyId")
    sc.write(d, Sidecar(store_url="https://x.bandcamp.com/album/o", mb_release_id="rel-onlyid"))
    album_id = _id_for(cfg, d)
    with activity_store.action():
        activity.record("Re-tagged", album_id=album_id, album_label="Artist — OnlyId")
        event_id = audit.record("tag.track", album_id=album_id, file="01 a.m4a")
        assert event_id is not None
        activity_store.record_tag_changes(
            event_id, file="01 a.m4a", changes={"mb_album_id": [None, "rel-onlyid"]}
        )

    assert "tag-changes__undo--tags" in client.get(f"/album/{album_id}").text


def test_undo_tags_rebuilds_its_plan_from_the_albums_own_history(client, cfg):
    """The form names a history row, never files or values. A row belonging to
    another album — or to nothing — is a 404, not a reach for whatever the
    caller asked for."""
    mine, _, _ = _tagging_with_tag_changes(cfg, "TagMine")
    _theirs, _, theirs_anchor = _tagging_with_tag_changes(cfg, "TagTheirs")

    assert client.post(f"/tags/restore/{mine}", data={"event_id": theirs_anchor}).status_code == 404
    assert client.post(f"/tags/restore/{mine}", data={"event_id": 999999}).status_code == 404


def test_undo_tags_twice_reports_nothing_left_to_do(client, cfg):
    """And writes no history line for the second click. A no-op that recorded
    itself would leave a permanent entry whose whole content is that nothing
    happened — the rule a re-tag finding MusicBrainz unchanged already follows
    (#86) — and it would state it by reciting every field in the album."""
    from harmonist import activity_store

    album_id, _, anchor = _tagging_with_tag_changes(cfg, "TagTwice")

    first = client.post(f"/tags/restore/{album_id}", data={"event_id": anchor})
    entries = len(activity_store.album_history(album_id))
    second = client.post(f"/tags/restore/{album_id}", data={"event_id": anchor})

    assert "Tags put back" in first.text
    assert "Nothing to undo" in second.text
    assert "these tags have changed since" in second.text
    assert len(activity_store.album_history(album_id)) == entries, "the no-op stayed quiet"


def test_undo_caps_the_fields_it_names(client, cfg):
    """Naming the outliers is the point; reciting the whole owned set is not.
    An undo whose fields have all moved on would otherwise put twenty labels in
    the status bar."""
    from harmonist import formats

    album_id, d, anchor = _tagging_with_tag_changes(cfg, "TagMany")
    for path in sorted(p for p in d.iterdir() if formats.is_supported(p)):
        current = formats.read_owned(path)
        # Five fields the tagging actually wrote, so all five read as stale —
        # one past the cap.
        formats.write_owned(
            path,
            {
                **current,
                "label": "X",
                "album": "Y",
                "title": "Z",
                "artist": "W",
                "album_artist": "V",
            },
        )

    r = client.post(f"/tags/restore/{album_id}", data={"event_id": anchor})

    assert " and 1 more" in r.text, "named up to the cap, then counted"


def test_album_page_resolves_a_superseded_id(client, cfg):
    """A link written before the album was re-identified must still land on the
    page rather than 404 — the same forward walk `_find_album` already does."""
    d = _make_album(cfg, "Moved")
    sc.write(d, Sidecar(store_url="https://x.bandcamp.com/album/m"))
    old_id = _id_for(cfg, d)
    sc.write(d, Sidecar(store_url="https://x.bandcamp.com/album/m", mb_release_id="rel-moved"))

    r = client.get(f"/album/{old_id}")
    assert r.status_code == 200
    assert "Moved" in r.text


def test_album_page_404s_for_an_unknown_album(client, cfg):
    _make_album(cfg, "Present")
    assert client.get("/album/no-such-id").status_code == 404


# --- Library → album → Library round-trip (#139) -----------------------------
#
# The three links that have to agree for the trip to land where it started: the
# tile hands the album page its page number, the album page hands it back to the
# index, and the index starts the grid there.


def test_library_tile_tells_the_album_page_which_page_it_came_from(client, cfg):
    _make_library(cfg, 5)
    body = client.get("/library?page=2&limit=2").text
    assert "?from_page=2" in body

    # Page 1 is what Back means with no hint at all, so the common URL stays bare.
    assert "from_page" not in client.get("/library?page=1&limit=2").text


def test_album_page_back_link_returns_to_the_page_it_came_from(client, cfg):
    from datetime import datetime

    d = _make_tagged_album(cfg, "Deep", mbid="rel-deep", tagged_at=datetime.now(UTC), item_id=1)
    aid = _id_for(cfg, d)

    body = client.get(f"/album/{aid}?from_page=4").text
    assert 'href="/?tab=library&page=4"' in body

    # No hint (a bookmark, or a link out of the Activity feed) → the top of the
    # grid, but still the Library tab: arriving from Activity leaves the
    # remembered tab on Inbox, and "Back to library" must not land there.
    plain = client.get(f"/album/{aid}").text
    assert 'href="/?tab=library"' in plain
    assert "page=" not in plain.split("Back to library")[0].rsplit("<a ", 1)[1]


def test_index_renders_the_grid_inline_on_the_page_the_url_names(client, cfg, monkeypatch):
    """The index renders the grid itself rather than fetching it on load, so a
    `?page=` link is answered by the HTML — no follow-up request that would have
    to be told which page it is on, and nothing to drift on a reload or a Back."""
    from harmonist.web import main as web_main

    monkeypatch.setattr(web_main, "_LIBRARY_PAGE_SIZE", 2)
    _make_library(cfg, 5)

    body = client.get("/?tab=library&page=2").text
    assert 'data-page="2"' in body
    assert "Album2" in body and "Album0" not in body
    assert 'harmonistTab("library")' in body


def test_index_ignores_an_unknown_tab_rather_than_reflecting_it(client, cfg):
    """`?tab=` is interpolated into a `panel-<name>` lookup in the tab script."""
    body = client.get("/?tab=</script><script>alert(1)</script>").text
    assert "alert(1)" not in body
    assert "harmonistTab(" in body  # falls back to the remembered tab
    assert "localStorage.getItem('harmonist-tab')" in body


def test_index_defaults_to_the_remembered_tab_and_page_one(client, cfg):
    body = client.get("/").text
    assert "localStorage.getItem('harmonist-tab')" in body
    assert 'data-page="1"' in body


def test_library_pager_links_do_not_inherit_a_competing_page_value(client, cfg):
    """Regression guard for the bug that made every pager click a no-op.

    The refresh wiring used to sit on #library-rows as an `hx-vals`, which htmx
    INHERITS: each pager link inside then sent its own `?page=2` *plus* the
    container's `?page=1`, and the last value won. Keeping the page in each
    fragment's own `hx-get` is what removes the collision, so no ancestor of the
    pager may reintroduce an `hx-vals`."""
    _make_library(cfg, 5)
    index = client.get("/?tab=library").text
    assert "hx-vals" not in index.split('id="library-rows"')[1].split("</div>")[0]

    fragment = client.get("/library?page=2&limit=2").text
    assert "hx-vals" not in fragment
    # Each render re-requests the page it is showing, at the size it is showing,
    # so a refresh mid-browse (every `tasks-changed`) can't yank the user back to
    # the newest page or resize the one they're on. Since #217 this wiring is the
    # WHOLE of the Library's refreshing — the manual button it used to sit beside
    # is gone — so losing it would leave the grid stale with nothing to press.
    assert 'hx-get="/library?page=2&limit=2"' in fragment
    assert "tasks-changed from:body" in fragment


def test_old_deep_link_redirects_to_the_album_page(client, cfg):
    """`?album=<id>` was the deep link before there was an album page (#65). Now
    there is one (#103), so it redirects — activity entries written with the old
    URL are durable, so this will keep arriving indefinitely and must keep
    working."""
    d = _make_album(cfg, "DeepLinked")
    aid = _id_for(cfg, d)

    r = client.get(f"/?album={aid}", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == f"/album/{aid}"


def test_deep_link_to_unknown_album_still_renders_page(client, cfg):
    """A stale id (reconcile moved the album's identity, or it left the library)
    must degrade to the normal page plus a notice — never a whole-page 404, and
    never a silent no-op that looks like a broken link."""
    _make_album(cfg, "Present")

    r = client.get("/?album=no-such-album-id")
    assert r.status_code == 200
    assert "isn't in your library any more" in r.text
    # And it stayed put rather than redirecting to a page for an id that
    # resolves to nothing (TestClient follows redirects, so the final URL is
    # the honest check that the #103 redirect didn't fire).
    assert not str(r.url).endswith("/album/no-such-album-id")


def test_action_records_an_id_that_still_resolves_after_tagging(client, cfg, monkeypatch):
    """Regression: the link in the Activity feed must actually work.

    An album that already has a sidecar `temp_uid` — demo's seeded library, or
    anything at all after a restart — is NOT in the in-memory id registry. Tagging
    then DROPS temp_uid in favour of the MBID (models.py), so the pre-mutation id
    the handler was holding is erased from disk and unrecoverable: `_find_album`'s
    registry fallback can't help. Recording that id produced a permanently dead
    link. `_flash_response` now re-derives the id from disk AFTER the mutation.
    """
    from harmonist import activity

    # A sidecar'd but untagged album: its id is the sidecar's temp_uid.
    d = _make_album(cfg, "Relinked")
    sc.write(
        d,
        Sidecar(
            store_url="https://x.bandcamp.com/album/relinked",
            mb_match_candidate=MatchCandidate(
                mb_release_id="rel-relink", confidence="exact", file_count=1, track_count=1
            ),
        ),
    )
    aid = _id_for(cfg, d)
    monkeypatch.setattr(
        "harmonist.mb_lookup.fetch_release", lambda mbid: _release_for_match(mbid, n_tracks=1)
    )
    monkeypatch.setattr("harmonist.cover_art.ensure_cover", lambda *a, **kw: None)
    activity.clear()

    # Confirming TAGS it: temp_uid is dropped, the id becomes the MBID.
    assert client.post(f"/confirm/{aid}").status_code == 200
    assert _id_for(cfg, d) != aid  # the identity really did move
    assert sc.read(d).temp_uid is None  # ...and the old id is gone from disk

    entry = next(e for e in activity.recent(10) if "Tagged" in e.message)
    assert entry.album_id is not None
    assert entry.album_label  # the feed can name it
    # The recorded id resolves — i.e. the rendered link is live, not a 404.
    r = client.get(f"/?album={entry.album_id}")
    assert r.status_code == 200
    assert "isn't in your library any more" not in r.text
    # Anchored on the album page's own MB-comparison fetch, which carries the
    # resolved id. (It used to check the retag-refresh span, but that was a
    # modal-only element that leaked onto the page — see the album-retagged
    # gating in library_detail.html.)
    assert f'hx-get="/library/{entry.album_id}/compare"' in r.text


def test_missing_album_notice_is_dismissible_and_clears_the_url(client, cfg):
    """#71: the notice described a navigation that already happened, but sat
    there through tab switches and survived refreshes (?album= stayed in the
    URL and re-rendered it). It now has a dismiss control, and the failure path
    strips the parameter."""
    _make_album(cfg, "Present")

    body = client.get("/?album=no-such-album-id").text
    assert "isn't in your library any more" in body
    assert 'id="deep-link-notice"' in body
    assert 'aria-label="Dismiss"' in body
    assert "searchParams.delete('album')" in body


def test_successful_deep_link_keeps_the_url_parameter(client, cfg):
    """The counterpart to #71: a WORKING deep link must resolve to its album
    rather than being stripped. Only the broken one is.

    Anchored on the page's alert slot, which is keyed by the album id and drawn
    for every state — so it holds whatever this album turns out to be. It used
    to anchor on the re-tag control for that reason, which stopped being true in
    #150: an album with no MusicBrainz release has nothing to re-tag from, so
    that button is no longer offered on its page."""
    d = _make_album(cfg, "Keeper")
    aid = _id_for(cfg, d)

    body = client.get(f"/?album={aid}").text
    assert f'id="album-alert-{aid}"' in body
    assert "searchParams.delete('album')" not in body
    assert 'id="deep-link-notice"' not in body


def test_deep_link_resolves_through_the_alias_chain_after_a_restart(client, cfg):
    """#33's payoff, and the case the path-derived id CANNOT cover.

    id_registry only answers for an album with no sidecar. Once tagging moves an
    album's identity (temp_uid dropped for the MBID), a deep link written under
    the old id has no way home — the old id is erased from disk and isn't
    derivable from the path either. The durable alias chain, recorded at the
    moment of the change, is what rescues it.
    """
    d = _make_album(cfg, "Aliased")
    sc.write(d, Sidecar(store_url="https://x.bandcamp.com/album/aliased"))
    old_id = _id_for(cfg, d)

    # Tag it: identity moves to the MBID and temp_uid is erased from disk.
    sc.write(d, Sidecar(store_url="https://x.bandcamp.com/album/aliased", mb_release_id="rel-al"))
    assert sc.read(d).temp_uid is None
    assert _id_for(cfg, d) == "rel-al"

    r = client.get(f"/?album={old_id}")
    assert r.status_code == 200
    assert "isn't in your library any more" not in r.text
    # Resolved forward to the album's CURRENT id.
    assert 'hx-get="/library/rel-al/compare"' in r.text


def test_post_sync_auto_tag_entry_links_to_the_album(cfg, monkeypatch):
    """#75: the entry a user is most likely to want to click — Harmonist tagged
    something on its own initiative after a sync — was plain text. The id must be
    read back AFTER tagging, since that write moves the album's identity."""
    from harmonist import activity
    from harmonist.tagger import PicardCompatibleTagger
    from harmonist.web.main import _resolve_by_store_url

    d = _make_album(cfg, "AutoTagged")
    sc.write(d, Sidecar(store_url="https://x.bandcamp.com/album/auto"))
    monkeypatch.setattr("harmonist.mb_lookup.lookup_by_bandcamp_url", lambda url: ["rel-auto"])
    monkeypatch.setattr(
        "harmonist.mb_lookup.fetch_release", lambda mbid: _release_for_match(mbid, n_tracks=1)
    )
    monkeypatch.setattr("harmonist.cover_art.ensure_cover", lambda *a, **kw: None)
    activity.clear()

    assert _resolve_by_store_url(d, cfg, PicardCompatibleTagger()) == "tagged"

    entry = next(e for e in activity.recent(10) if "Auto-tagged" in e.message)
    assert entry.album_label == "AutoTagged"  # the on-disk name, as shown before
    # Resolves to the id the album has NOW (the MBID), not its pre-tag temp_uid.
    assert entry.album_id == "rel-auto"
    assert _id_for(cfg, d) == entry.album_id
    # The name is no longer duplicated inside the message.
    assert "AutoTagged" not in entry.message


def test_reconcile_with_nothing_to_do_is_silent_in_the_feed(cfg, caplog):
    """#101: reconcile runs on startup and after every sync, and used to report a
    no-op with three entries — making "nothing happened" the feed's most frequent
    content. It stays in the server log so it's still greppable."""
    import logging

    from harmonist import activity
    from harmonist.web.reconcile_runner import reconcile_pending_orphans

    cfg.paths.music_dir.mkdir(parents=True, exist_ok=True)
    activity.clear()

    with caplog.at_level(logging.INFO):
        reconcile_pending_orphans(cfg.paths.music_dir, fetch_urls=lambda m: [], albums=[])

    assert activity.recent(10) == []
    assert any("nothing to reconcile" in r.message for r in caplog.records)


def test_reconcile_with_work_announces_once(cfg):
    """One start line naming the count, then the summary — not a separate
    "started" and "N to check"."""
    from harmonist import activity
    from harmonist.web.reconcile_runner import reconcile_pending_orphans

    _make_album(cfg, "NeedsReconcile", mbid="rel-nr")
    activity.clear()

    reconcile_pending_orphans(cfg.paths.music_dir, fetch_urls=lambda m: [])

    msgs = [e.message for e in activity.recent(10)]
    starts = [m for m in msgs if m.startswith("Reconcile started")]
    assert len(starts) == 1, f"expected exactly one start line, got {starts}"
    assert "1 album(s) to check" in starts[0]
    assert any(m.startswith("Reconcile done") for m in msgs)


def test_reconcile_entries_link_to_their_album(cfg):
    """#75: reconcile's 'New -> ...' entries carry the album too, with the same
    'Artist — Title' column the rest of the feed uses."""
    from harmonist import activity
    from harmonist.web.reconcile_runner import reconcile_pending_orphans

    d = _make_album(cfg, "Reconciled", mbid="rel-rec")
    activity.clear()

    reconcile_pending_orphans(cfg.paths.music_dir, fetch_urls=lambda mbid: [])

    entry = next(e for e in activity.recent(20) if "New →" in e.message)
    # The recorded id is the one the album actually has after reconcile wrote its
    # sidecar — i.e. the link resolves, rather than merely being non-empty.
    assert entry.album_id == _id_for(cfg, d)
    assert entry.album_label and "Reconciled" in entry.album_label
    assert "Reconciled" not in entry.message  # not duplicated in the text


def test_deep_link_follows_moved_album_id(client, cfg):
    """The id in an old activity row is a snapshot. When a NEW album's registry
    UUID is superseded (a sidecar write giving it an MBID identity), the deep
    link must still resolve via the registry fallback rather than 404."""
    d = _make_album(cfg, "Moved")
    original_id = _id_for(cfg, d)  # registry UUID, minted for the NEW album
    sc.write(d, Sidecar(mb_release_id="rel-moved"))
    current_id = _id_for(cfg, d)
    assert current_id != original_id  # identity moved to the MBID

    r = client.get(f"/?album={original_id}")
    assert r.status_code == 200
    # Resolved forward to the album's CURRENT id, so the fragment fetch works.
    assert f'hx-get="/library/{current_id}/compare"' in r.text


def test_retag_emits_album_retagged_trigger(client, cfg, monkeypatch):
    """On success /retag fires an `album-retagged` HX-Trigger so the open detail
    modal reloads itself — otherwise its disk-vs-MB comparison stays stale (#34)."""
    d = _make_tagged_album(
        cfg, "TriggerRetag", mbid="rel-1", tagged_at=datetime.now(UTC), item_id=1
    )
    monkeypatch.setattr(
        "harmonist.mb_lookup.fetch_release", lambda mbid: _release_for_match(mbid, n_tracks=1)
    )
    monkeypatch.setattr("harmonist.cover_art.ensure_cover", lambda *a, **kw: None)
    r = client.post(f"/retag/{_id_for(cfg, d)}")
    assert r.status_code == 200
    assert "album-retagged" in r.headers.get("HX-Trigger", "")


def test_a_failed_action_reaches_the_feed_once_and_names_its_album(
    client, cfg, monkeypatch, caplog
):
    """#258: a failure produces ONE feed entry, and it is the one that says which
    album failed and why.

    The `log.exception` beside each of these `_flash_response(..., album=...)`
    calls is there for the server log's traceback. It mirrored into the feed too
    (activity._ActivityLogHandler), so every failed action wrote a SECOND error
    row — "retag failed", no album, no detail — beneath the real one. Two rows
    for one failure, the useless copy last, and only the useless copy reached
    nobody's album history.
    """
    caplog.set_level("ERROR")
    d = _make_tagged_album(cfg, "FailRetag", mbid="rel-1", tagged_at=datetime.now(UTC), item_id=1)

    def boom(mbid):
        raise RuntimeError("mb down")

    monkeypatch.setattr("harmonist.mb_lookup.fetch_release", boom)
    album_id = _id_for(cfg, d)
    assert "Re-tag failed" in client.post(f"/retag/{album_id}").text

    errors = [e for e in activity.recent() if e.level == "error"]
    assert [e.message for e in errors] == ["Re-tag failed — mb down"]
    assert errors[0].album_id == album_id
    # The traceback still reaches the server log — suppressing the MIRROR must
    # not suppress the thing you actually want three weeks later.
    assert any(r.exc_info for r in caplog.records if r.name == "harmonist.web.main")


def test_retag_failure_does_not_emit_refresh_trigger(client, cfg, monkeypatch):
    """A failed re-tag must not fire album-retagged (nothing changed to refresh)."""
    d = _make_tagged_album(cfg, "FailRetag", mbid="rel-1", tagged_at=datetime.now(UTC), item_id=1)

    def boom(mbid):
        raise RuntimeError("mb down")

    monkeypatch.setattr("harmonist.mb_lookup.fetch_release", boom)
    r = client.post(f"/retag/{_id_for(cfg, d)}")
    assert r.status_code == 200  # rendered as a flash, not an HTTP error
    assert "Re-tag failed" in r.text
    assert "album-retagged" not in r.headers.get("HX-Trigger", "")


def test_album_page_wires_retag_progress_and_refresh(client, cfg):
    """The re-tag button shows a tagging indicator, disables the whole action
    group while it runs, and refreshes the page when it lands.

    The page must NOT carry the modal's `album-retagged` listener: that fetches
    the detail fragment into #modal, which base.html opens as a dialog — so
    re-tagging from the page popped up the dialog the page replaced."""
    d = _make_tagged_album(cfg, "DetailWiring", mbid="rel-1", tagged_at=datetime.now(UTC))
    aid = _id_for(cfg, d)
    r = client.get(f"/album/{aid}")
    assert r.status_code == 200
    assert f'hx-indicator="#tagging-{aid}"' in r.text  # progress spinner target
    assert f'id="tagging-{aid}"' in r.text  # the overlay itself
    # `this`, not a group selector: htmx disables matching elements before it
    # issues the request, so a selector covering the trigger stops the request
    # firing at all. The overlay is what blocks the other buttons.
    assert 'hx-disabled-elt="this"' in r.text
    # The page reloads itself once the re-tag lands.
    assert "window.location.reload()" in r.text


def test_album_page_dates_are_relative_with_the_exact_value_on_hover(client, cfg):
    """Dates read as elapsed time, since what they answer is "how recently?",
    with the exact timestamp on hover so nothing is lost.

    (The title used to be a link through to this page; on the page itself
    there's nowhere to go, and since #129 there is no other view.)"""
    d = _make_tagged_album(cfg, "Linked", mbid="rel-l", tagged_at=datetime.now(UTC))
    body = client.get(f"/album/{_id_for(cfg, d)}").text

    assert "just now" in body  # relative, not a bare date
    assert datetime.now(UTC).strftime("%Y-%m-%d") in body  # exact value on hover


def test_leaving_the_library_returns_you_to_it(client, cfg):
    """#129: rematch and Forget both send the album OUT of the Library — to
    Needs MBID and to NEW. The dialog used to close on success, which took you
    back by construction; a page has to navigate, or it sits there describing an
    album that moved."""
    d = _make_tagged_album(cfg, "Departing", mbid="rel-s", tagged_at=datetime.now(UTC))
    body = client.get(f"/album/{_id_for(cfg, d)}").text

    assert body.count("window.location.href = '/'") == 2  # rematch and Forget
    # …and both controls are actually there to navigate from.
    aid = _id_for(cfg, d)
    assert f"/library/{aid}/rematch" in body
    assert f"/forget/{aid}" in body


def test_retag_recomputes_count_and_promotes_incomplete_to_complete(client, cfg, monkeypatch):
    """When MB over-counts (e.g. a phantom album-mix track) an album lands as a
    spurious INCOMPLETE. After the user fixes MB upstream, Re-tag from MB writes
    the corrected release's totals into the files and the state self-corrects to
    COMPLETE — no explicit override needed."""
    from harmonist import scanner
    from harmonist.models import AlbumState
    from test.helpers import write_track_totals

    d = _make_album(cfg, "OverCounted", mbid="rel-1")  # 1 file, tagged rel-1
    write_track_totals(d, track_total=2)  # MB wrongly said 2 → 1 file < 2 → INCOMPLETE
    sc.write(d, Sidecar(mb_release_id="rel-1", tagged_at=datetime.now(UTC)))
    before = next(a for a in scanner.scan(cfg.paths.music_dir) if a.path == d)
    assert before.state == AlbumState.INCOMPLETE  # precondition

    # MB corrected upstream: the release now has 1 track, matching the 1 file.
    monkeypatch.setattr(
        "harmonist.mb_lookup.fetch_release",
        lambda mbid: _release_for_match(mbid, n_tracks=1),
    )
    monkeypatch.setattr("harmonist.cover_art.ensure_cover", lambda *a, **kw: None)

    r = client.post(f"/retag/{_id_for(cfg, d)}")
    assert r.status_code == 200

    after = next(a for a in scanner.scan(cfg.paths.music_dir) if a.path == d)
    assert after.expected_track_count == 1, "the re-tag rewrote the total into the file"
    assert after.state == AlbumState.COMPLETE  # self-corrected


def test_retag_400_when_no_mbid_on_sidecar(client, cfg):
    d = _make_album(cfg, "NoMBID")
    sc.write(d, Sidecar())
    aid = _id_for(cfg, d)
    r = client.post(f"/retag/{aid}")
    assert r.status_code == 400


def test_reconcile_is_idempotent_on_already_reconciled_album(client, cfg, monkeypatch):
    """After Forget → auto-reconcile, a stale 'Reconcile from tags' click
    used to 400. It should now return a calm 'already reconciled' message.
    """
    from harmonist.models import BandcampInfo, Sidecar

    d = _make_album(cfg, "ToReconcile")
    sc.write(
        d,
        Sidecar(
            store_url="https://x.bandcamp.com/album/y",
            bandcamp=BandcampInfo(item_id=None),
            mb_release_id="rel-x",
        ),
    )
    aid = _id_for(cfg, d)
    r = client.post(f"/reconcile/{aid}")
    assert r.status_code == 200, r.text
    assert "already reconciled" in r.text.lower() or "reconcile" in r.text.lower()


def test_reconcile_returns_warning_when_no_mbid_atom(client, cfg):
    """Untagged new album should get a clear no-MBID message, not a 400."""
    d = _make_album(cfg, "Untagged")  # no MBID atom on file
    aid = _id_for(cfg, d)
    r = client.post(f"/reconcile/{aid}")
    assert r.status_code == 200
    assert "MusicBrainz Album Id" in r.text or "no" in r.text.lower()


def test_forget_deletes_sidecar(client, cfg):
    from datetime import datetime

    d = _make_tagged_album(cfg, "Forgetme", mbid="rel-1", tagged_at=datetime.now(UTC), item_id=1)
    assert sc.has_sidecar(d)
    aid = _id_for(cfg, d)
    r = client.post(f"/forget/{aid}")
    assert r.status_code == 200
    assert "Forgotten" in r.text
    # Sidecar gone; album files untouched
    assert not sc.has_sidecar(d)
    assert (d / "01 Track.m4a").exists()


def test_forget_adds_path_to_exemption_set(client, cfg):
    """Forget must add the album's path to forgotten_paths so the
    auto-reconciler won't undo the user's intent on the next /tasks tick.
    """
    from datetime import datetime

    d = _make_tagged_album(cfg, "Exempt", mbid="rel-1", tagged_at=datetime.now(UTC), item_id=1)
    aid = _id_for(cfg, d)
    client.post(f"/forget/{aid}")
    assert d in client.app.state.forgotten_paths


def test_explicit_reconcile_clears_exemption(client, cfg):
    """Explicit /reconcile/{id} should discard any prior Forget exemption —
    the user's most-recent intent wins.
    """
    d = _make_album(cfg, "Cleared")
    # Pre-seed the album in the exemption set
    client.app.state.forgotten_paths.add(d)
    # The album has no sidecar (new) and no MBID atom — reconcile is a
    # no-op but the route still discards the exemption.
    aid = _id_for(cfg, d)
    r = client.post(f"/reconcile/{aid}")
    assert r.status_code == 200
    assert d not in client.app.state.forgotten_paths


def test_cover_serves_when_present(client, cfg):
    d = _make_album(cfg, "WithCover")
    (d / "cover.jpg").write_bytes(b"\xff\xd8\xff\xe0FAKE")
    aid = _id_for(cfg, d)
    r = client.get(f"/cover/{aid}")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg"
    assert r.content.startswith(b"\xff\xd8")


# ---------- album id stability ----------


def test_a_new_album_gets_an_id_without_a_sidecar_to_hold_it(client, cfg):
    """An album with no sidecar still needs an id for its URLs and its activity
    records. `id_registry.get_or_mint` hashes the path, so the same folder
    answers the same id on every scan with nothing persisted anywhere (#114 —
    it was a minted UUID in a dict once, which the name still remembers).
    """
    d = _make_album(cfg, "NewOne")
    aid1 = _id_for(cfg, d)
    aid2 = _id_for(cfg, d)
    assert aid1 == aid2
    # No sidecar exists, so the id can't have come from one
    assert not sc.has_sidecar(d)


def test_sidecar_album_id_matches_temp_uid(client, cfg):
    """A sidecar'd album's id is the sidecar's temp_uid."""
    d = _make_album(cfg, "WithSidecar")
    sc.write(
        d,
        Sidecar(
            store_url="https://x.bandcamp.com/album/y",
        ),
    )
    aid = _id_for(cfg, d)
    assert aid == sc.read(d).temp_uid


def test_sidecar_album_id_matches_mbid_when_tagged(client, cfg):
    """A tagged album's id is its MBID."""
    from datetime import datetime

    d = _make_tagged_album(
        cfg, "Tagged", mbid="abc-mbid-1234", tagged_at=datetime.now(UTC), item_id=42
    )
    aid = _id_for(cfg, d)
    assert aid == "abc-mbid-1234"


def test_new_album_id_survives_first_sidecar_write(client, cfg):
    """The registry UUID minted for a NEW album is reused when the first
    sidecar is written — so the inbox URL the user interacted with stays
    valid across the NEW → sidecar'd transition.
    """
    d = _make_album(cfg, "Surviving")
    registry_uid = _id_for(cfg, d)  # mints into registry as a side effect

    # Now write a sidecar. The temp_uid should be the registry value.
    sc.write(
        d,
        Sidecar(
            store_url="https://x.bandcamp.com/album/y",
        ),
    )
    assert sc.read(d).temp_uid == registry_uid
    # And the album.id from a fresh scan still matches
    assert _id_for(cfg, d) == registry_uid


def test_sidecar_album_id_survives_rename(client, cfg):
    """The UUID lives in the sidecar JSON, which moves with the directory
    on rename. So album.id is stable across renames for sidecar'd albums.
    """
    d = _make_album(cfg, "RenameMe")
    sc.write(
        d,
        Sidecar(
            store_url="https://x.bandcamp.com/album/y",
        ),
    )
    aid_before = _id_for(cfg, d)

    new_d = d.parent / "Renamed"
    d.rename(new_d)
    aid_after = _id_for(cfg, new_d)
    assert aid_before == aid_after


# ---------- Confirm as Incomplete (§15.3) ----------


def test_confirm_incomplete_tags_and_persists_expected_count(client, cfg, monkeypatch):
    """POST /confirm/{id}/incomplete tags via the incomplete-mode tagger, sets
    mb_release_id, and leaves the album INCOMPLETE on the next scan.

    The expected count is not persisted anywhere (#195) — the tagging writes the
    release's own totals into the file, and that is what the scan reads back.
    """
    d = _make_album(cfg, "ShortAlbum")  # 1 file from the fixture
    # Seed a candidate with file_count=1, track_count=3 (MB says 3, disk has 1)
    sc.write(
        d,
        Sidecar(
            store_url="https://x.bandcamp.com/album/y",
            mb_match_candidate=MatchCandidate(
                mb_release_id="rel-mbid",
                confidence="approximate",
                file_count=1,
                track_count=3,
            ),
        ),
    )

    def fake_release(mbid):
        return {
            "id": mbid,
            "title": "Three-track Album",
            "release-group": {"id": "rg-1"},
            "medium-list": [
                {
                    "position": "1",
                    "track-list": [
                        {
                            "id": "rt-1",
                            "position": "1",
                            "title": "T1",
                            "recording": {"id": "rec-1", "title": "T1"},
                        },
                        {
                            "id": "rt-2",
                            "position": "2",
                            "title": "T2",
                            "recording": {"id": "rec-2", "title": "T2"},
                        },
                        {
                            "id": "rt-3",
                            "position": "3",
                            "title": "T3",
                            "recording": {"id": "rec-3", "title": "T3"},
                        },
                    ],
                }
            ],
        }

    monkeypatch.setattr("harmonist.mb_lookup.fetch_release", fake_release)
    monkeypatch.setattr("harmonist.cover_art.ensure_cover", lambda *a, **kw: None)

    aid = _id_for(cfg, d)
    r = client.post(f"/confirm/{aid}/incomplete")
    assert r.status_code == 200
    assert "incomplete" in r.text.lower()

    loaded = sc.read(d)
    assert loaded.mb_release_id == "rel-mbid"
    assert loaded.mb_match_candidate is None
    assert loaded.tagged_at is not None

    # Scanner now reports INCOMPLETE
    from harmonist import scanner

    a = next(a for a in scanner.scan(cfg.paths.music_dir) if a.path == d)
    from harmonist.models import AlbumState

    assert a.state == AlbumState.INCOMPLETE
    assert a.expected_track_count == 3, "read back from the tags the tagging wrote"


def test_confirm_mistag_adopts_owned_store_url(client, cfg, monkeypatch):
    """Confirming a mis-tag re-tags the MBID AND adopts the owned edition's
    purchase URL as store_url — otherwise the album keeps the wrong-edition URL,
    matches no purchase on the next sync, and falls through to surrender."""
    d = _make_album(cfg, "Mistagged")
    sc.write(
        d,
        Sidecar(
            # The WRONG edition's URL the album currently carries.
            store_url="https://ultimae.bandcamp.com/album/life-24bits-2",
            mb_match_candidate=MatchCandidate(
                mb_release_id="rel-standard",
                confidence="exact",
                file_count=1,
                track_count=1,
                # Mis-tag provenance: the owned edition + the URL it was bought at.
                mistag_owned_url="https://ultimae.bandcamp.com/album/life",
                mistag_owned_label="ASURA / Life²",
            ),
        ),
    )
    monkeypatch.setattr(
        "harmonist.mb_lookup.fetch_release",
        lambda mbid: _release_for_match(mbid, n_tracks=1),
    )
    monkeypatch.setattr("harmonist.cover_art.ensure_cover", lambda *a, **kw: None)

    aid = _id_for(cfg, d)
    r = client.post(f"/confirm/{aid}")
    assert r.status_code == 200
    loaded = sc.read(d)
    assert loaded.mb_release_id == "rel-standard"
    # store_url now points at the edition the user actually purchased.
    assert loaded.store_url == "https://ultimae.bandcamp.com/album/life"


def test_confirm_normal_candidate_keeps_store_url(client, cfg, monkeypatch):
    """A non-mis-tag candidate has no owned URL, so confirming keeps the
    existing store_url unchanged."""
    d = _make_album(cfg, "NormalConfirm")
    sc.write(
        d,
        Sidecar(
            store_url="https://x.bandcamp.com/album/keep-me",
            mb_match_candidate=MatchCandidate(
                mb_release_id="rel-keep",
                confidence="exact",
                file_count=1,
                track_count=1,
            ),
        ),
    )
    monkeypatch.setattr(
        "harmonist.mb_lookup.fetch_release",
        lambda mbid: _release_for_match(mbid, n_tracks=1),
    )
    monkeypatch.setattr("harmonist.cover_art.ensure_cover", lambda *a, **kw: None)
    aid = _id_for(cfg, d)
    client.post(f"/confirm/{aid}")
    assert sc.read(d).store_url == "https://x.bandcamp.com/album/keep-me"


def test_confirm_incomplete_400_without_candidate(client, cfg):
    d = _make_album(cfg, "NoCandidate")
    sc.write(d, Sidecar())
    aid = _id_for(cfg, d)
    r = client.post(f"/confirm/{aid}/incomplete")
    assert r.status_code == 400


def test_needs_review_card_offers_incomplete_when_file_count_short(client, cfg):
    """The Confirm as Incomplete button appears on a suggestion card
    where file_count < track_count — and not when they're equal.
    """
    d_short = _make_album(cfg, "ShortMatch")
    sc.write(
        d_short,
        Sidecar(
            store_url="https://x.bandcamp.com/album/short",
            mb_match_candidate=MatchCandidate(
                mb_release_id="rel-short",
                confidence="approximate",
                file_count=1,
                track_count=3,
            ),
        ),
    )
    d_exact = _make_album(cfg, "ExactMatch")
    sc.write(
        d_exact,
        Sidecar(
            store_url="https://x.bandcamp.com/album/exact",
            mb_match_candidate=MatchCandidate(
                mb_release_id="rel-exact",
                confidence="approximate",
                file_count=1,
                track_count=1,
            ),
        ),
    )
    r = client.get("/tasks")
    # Button text appears only when there's at least one short album
    assert "Confirm as Incomplete" in r.text
    # Count occurrences — exactly one (only on the short card, not the exact one)
    assert r.text.count("Confirm as Incomplete") == 1


def test_library_shows_partial_tag_badge(client, cfg):
    """An album with some files missing the MBID atom surfaces a
    '{N}/{M} tagged' badge alongside the title in the library row.
    """
    from datetime import datetime

    d = _make_album(cfg, "PartiallyTagged")
    # Add a second file
    second = d / "02 Other.m4a"
    shutil.copy(SINE_M4A, second)
    # Tag only the first
    audio = MP4(d / "01 Track.m4a")
    audio[ATOM_MB_ALBUM_ID] = [b"rel-aaa"]
    audio.save()
    sc.write(
        d,
        Sidecar(
            mb_release_id="rel-aaa",
            tagged_at=datetime.now(UTC),
        ),
    )
    r = client.get("/library")
    assert r.status_code == 200
    assert "PartiallyTagged" in r.text
    assert "1/2 tagged" in r.text


def test_library_includes_incomplete_albums(client, cfg):
    """Library shows both COMPLETE and INCOMPLETE — both are terminal."""
    from datetime import datetime

    _make_tagged_album(
        cfg,
        "Whole",
        mbid="rel-c",
        tagged_at=datetime.now(UTC),
        item_id=1,
    )
    _make_incomplete_album(cfg, "Partial", mbid="rel-i", tagged_at=datetime.now(UTC), expected=5)
    r = client.get("/library")
    assert r.status_code == 200
    assert "Whole" in r.text
    assert "Partial" in r.text
    # INCOMPLETE row shows the "N of M" badge
    assert "1 of 5" in r.text


def test_canonical_id_change_mid_transaction(client, cfg, monkeypatch):
    """A handler that mutates an album to assign an MBID changes the
    canonical id as a side effect. The action response itself is 200 (the
    handler ran at the URL that was canonical at request time); the new
    canonical id takes effect on the *next* request via the inbox refresh.

    Design: we do NOT redirect mid-transaction. The HX-Trigger='tasks-changed'
    header propagates the new id to the UI.
    """
    d = _make_album(cfg, "ToAssign")
    sc.write(
        d,
        Sidecar(
            store_url="https://x.bandcamp.com/album/y",
        ),
    )
    temp_uid = sc.read(d).temp_uid
    mbid = "abc12345-1234-1234-1234-1234567890ab"

    monkeypatch.setattr(
        "harmonist.mb_lookup.fetch_release",
        lambda mbid: _release_for_match(mbid, n_tracks=1),
    )
    monkeypatch.setattr("harmonist.cover_art.ensure_cover", lambda *a, **kw: None)

    # POST at the temp_uid URL — runs the handler, returns 200
    r = client.post(f"/manual/{temp_uid}/assign", data={"mbid": mbid})
    assert r.status_code == 200
    assert "tasks-changed" in r.headers.get("hx-trigger", "")

    # Sidecar identity flipped: MBID set, temp_uid dropped
    loaded = sc.read(d)
    assert loaded.mb_release_id == mbid
    assert loaded.temp_uid is None

    # Album's canonical id is now the MBID
    assert _id_for(cfg, d) == mbid

    # The old temp_uid URL STILL resolves — to the same album. This assertion was
    # inverted by #33: it previously expected 404, which encoded the limitation of
    # the in-memory registry (empty for anything already sidecar'd, and after any
    # restart) rather than a design goal. `_find_album` has always intended the
    # opposite — "404 only when we can't resolve the id any way" — and the durable
    # alias chain now makes that achievable. The id refers to the same album, so
    # acting on it is correct; a stale page's buttons keep working instead of
    # dead-ending.
    r_stale = client.post(f"/recheck/{temp_uid}")
    assert r_stale.status_code == 200


# ---------- activity feed ----------


def test_activity_pages_backwards_through_history(client, cfg):
    """#14: the feed was capped at 100 with no way to reach anything older, so
    the durable history was accumulating unreachably."""
    from harmonist import activity

    activity.clear()
    for i in range(120):
        activity.record(f"event {i:03d}")

    # Newest first, so page 1 is 119 down to 070.
    first = client.get("/activity?limit=50").text
    assert "event 119" in first
    assert "event 070" in first  # 50th and last on the page
    assert "event 069" not in first  # page boundary
    assert "Load older activity" in first

    second = client.get("/activity?offset=50&limit=50").text
    assert "event 070" not in second  # no overlap with page 1
    assert "event 069" in second  # continues exactly where page 1 stopped
    assert "event 020" in second
    # A later page is an append-only fragment: no list wrapper, no controls.
    assert "<ul id=" not in second
    assert "Technical detail" not in second


def test_activity_last_page_has_no_load_more(client, cfg):
    """has_more comes from asking for limit+1, so the final page must not offer
    another — a COUNT would be wasted work on an unbounded table."""
    from harmonist import activity

    activity.clear()
    for i in range(10):
        activity.record(f"event {i}")

    body = client.get("/activity?limit=50").text
    assert "Load older activity" not in body


def test_activity_audit_toggle_reveals_unscoped_records(client, cfg):
    """The point of the toggle: audit rows written OUTSIDE an action scope have
    no action_id, so no entry's "what changed" disclosure can show them. Without
    this they're reachable from nowhere in the UI."""
    from harmonist import activity, activity_store, audit

    activity_store.clear()
    activity.record("a user-facing outcome")
    audit.record("unscoped.bookkeeping", detail="no action scope")

    plain = client.get("/activity").text
    assert "a user-facing outcome" in plain
    assert "unscoped.bookkeeping" not in plain  # invisible by default

    with_audit = client.get("/activity?audit=1").text
    assert "a user-facing outcome" in with_audit
    assert "unscoped.bookkeeping" in with_audit


def test_audit_detail_hangs_off_its_entry_and_only_when_asked(client, cfg):
    """Detail belongs to the entry that caused it, and appears only with "Show
    details" on. It used to be an always-present <details> disclosure as well as
    an inline interleave — two routes to the same records, and the interleave is
    what let one big action swamp the page (#123)."""
    from harmonist import activity, activity_store, audit

    activity_store.clear()
    with activity_store.action():
        activity.record("Tagged", album_id="rel-1", album_label="An Album")
        audit.record("sidecar.update", album_id="rel-1", mbid="a->b")

    off = client.get("/activity").text
    assert "Tagged" in off
    assert "sidecar.update" not in off
    assert "what changed" not in off  # the disclosure is gone entirely

    on = client.get("/activity?audit=1").text
    assert "Tagged" in on
    assert on.count("sidecar.update") == 1  # once, under its entry — not twice


def test_audit_rows_outside_an_action_still_appear(client, cfg):
    """They have no entry to hang off, so grouping alone would hide them.

    Most writes are scoped (the HTTP middleware wraps every mutating request;
    reconcile scopes per album), but the post-sync surrender / unmatched-purchase
    pass runs on the sync thread outside any request — and those rows are the
    only record that a surrender happened (#123)."""
    from harmonist import activity, activity_store, audit

    activity_store.clear()
    activity.record("Reconcile done")
    audit.record("sidecar.create", album_id="rel-1", mbid="x")  # no action scope

    assert "sidecar.create" not in client.get("/activity").text
    on = client.get("/activity?audit=1").text
    assert "sidecar.create" in on
    assert "Reconcile done" in on


def test_a_big_action_cannot_swamp_the_feed(client, cfg):
    """The bug behind #123: audit rows were interleaved by id, so one action that
    wrote 970 of them filled page 1 with identical lines and pushed every outcome
    off it. Detail hanging off its entry is what bounds that."""
    from harmonist import activity, activity_store, audit
    from harmonist.web.main import AUDIT_DETAIL_LIMIT

    activity_store.clear()
    for i in range(5):
        activity.record(f"Bandcamp sync finished — {i} new items")
    with activity_store.action():
        activity.record("Started tracking 300 albums")
        for i in range(300):
            audit.record("album.discovered", album=f"Artist {i}/Album {i}")

    body = client.get("/activity?audit=1").text
    # Every ordinary outcome is still on the page…
    for i in range(5):
        assert f"Bandcamp sync finished — {i} new items" in body
    # …and the 300 are capped rather than filling it.
    assert body.count("album.discovered") <= AUDIT_DETAIL_LIMIT


def test_activity_empty_state(client):
    r = client.get("/activity")
    assert r.status_code == 200
    assert "No activity yet" in r.text


def test_activity_lists_recorded_events(client):
    from harmonist import activity

    activity.record("Tagged — Some Album", Level.INFO)
    activity.record("Sync failed — boom", Level.ERROR)
    r = client.get("/activity")
    assert r.status_code == 200
    assert "Tagged — Some Album" in r.text
    assert "Sync failed — boom" in r.text
    # No empty-state copy when there are events
    assert "No activity yet" not in r.text


def test_action_outcome_recorded_to_activity(client, cfg):
    """A flash-producing action also lands in the activity feed."""
    d = _make_album(cfg, "ActivityAlbum")
    client.post(f"/reconcile/{_id_for(cfg, d)}")
    r = client.get("/activity")
    assert "ActivityAlbum" in r.text or "Reconcile" in r.text or "reconcile" in r.text


def test_index_has_activity_tab(client):
    r = client.get("/")
    assert 'data-tab="activity"' in r.text
    assert "Activity" in r.text


# ---------- settings ----------


def test_settings_page_renders(client, cfg):
    r = client.get("/settings")
    assert r.status_code == 200
    assert "Settings" in r.text
    # read-only paths + editable fields present
    assert "Music library" in r.text
    assert 'name="download_format"' in r.text
    assert 'name="max_downloads_per_sync"' in r.text
    assert 'name="user_agent"' in r.text
    # The preferences form must submit via HTMX (hx-boost) so the CSRF
    # middleware's required HX-Request header is sent — a plain POST 403s.
    assert 'action="/settings" hx-boost="true"' in r.text


def _button_tag(html: str, button_id: str) -> str:
    """The opening <button> tag with this id, so a test can assert on the
    attributes of THAT button rather than anywhere in the page."""
    m = re.search(rf'<button id="{button_id}"[^>]*>', html)
    assert m is not None, f"no <button id={button_id}> in page"
    return m.group(0)


def _button_is_disabled(html: str, button_id: str) -> bool:
    """Whether THAT button carries the bare `disabled` attribute.

    A plain `"disabled" in tag` would be true for free on any button carrying
    `hx-disabled-elt` or a `disabled:` Tailwind variant — the update-check
    button has both — so it would pass with the attribute never rendered.
    Values are blanked first, then the attribute names are matched whole.
    """
    return "disabled" in re.sub(r'"[^"]*"', '""', _button_tag(html, button_id)).split()


def test_settings_page_disables_the_global_sync_actions(client, cfg):
    """Sync is inert on Settings: it would read configuration the user is
    partway through changing (#108)."""
    cfg.cookies_file.write_text(
        "# Netscape HTTP Cookie File\n.bandcamp.com\tTRUE\t/\tFALSE\t0\tident\tabc\n"
    )
    settings = client.get("/settings")
    assert "disabled" in _button_tag(settings.text, "sync-button")
    # Control: the same header on the index page is live, or this test would
    # pass against a sync button that is disabled everywhere.
    assert "disabled" not in _button_tag(client.get("/").text, "sync-button")


def test_sync_popover_is_bound_to_the_sync_button(client, cfg):
    """The popover's submit is a second sync trigger, so it must not open over
    a disabled button. That binding is a CSS rule keyed on #sync-popover /
    #sync-button, so all pytest can check is that the two hooks it needs are
    present — whether the popover actually stays shut is an e2e test (#110)."""
    cfg.cookies_file.write_text(
        "# Netscape HTTP Cookie File\n.bandcamp.com\tTRUE\t/\tFALSE\t0\tident\tabc\n"
    )
    home = client.get("/").text
    assert 'id="sync-popover"' in home
    assert 'id="sync-button"' in home
    css = Path(__file__).resolve().parents[1] / "static" / "harmonist.css"
    assert "#sync-control:has(#sync-button:disabled) #sync-popover" in css.read_text()


def test_settings_page_disables_the_header_setup_button(client, cfg):
    """The header's Set-up duplicate goes inert too — Settings has its own in
    the Bandcamp sync section, so this is no dead end (#108)."""
    settings = client.get("/settings")  # no cookies → header offers setup
    assert "disabled" in _button_tag(settings.text, "bandcamp-setup-button")
    # The escape hatch: Settings' own control, still live.
    assert 'hx-get="/bandcamp/setup"' in settings.text
    assert "disabled" not in _button_tag(client.get("/").text, "bandcamp-setup-button")


def test_debug_memory_endpoint(client):
    r = client.get("/debug/memory")
    assert r.status_code == 200
    data = r.json()
    assert "rss_mb" in data
    assert "albums_in_snapshot" in data
    assert "scan_cache_entries" in data
    assert "gc_counts" in data
    # tracemalloc off by default → null payload + the enable hint.
    assert data["tracemalloc"] is None
    assert "tracemalloc_hint" in data


def test_settings_save_persists_and_applies_live(client, cfg):
    r = client.post(
        "/settings",
        data={
            "download_format": "alac",
            "max_downloads_per_sync": "12",
            "user_agent": "Harmonist/9.9 ( me@example.com )",
            "cover_art_size": "500",
            "gardener_level": "review",
            "log_level": "warning",
        },
    )
    assert r.status_code == 200
    assert "Settings saved" in r.text
    # Applied live on the running app
    live = client.app.state.cfg
    assert live.bandcamp.download_format == "alac"
    assert live.bandcamp.max_downloads_per_sync == 12
    assert live.cover_art.size == "500"
    assert live.gardener.level == "review"
    assert live.log_level == "warning"
    # Persisted to harmonist.toml (round-trips on next load)
    toml = (cfg.paths.config_dir / "harmonist.toml").read_text()
    assert "alac" in toml
    assert "max_downloads_per_sync = 12" in toml
    assert 'level = "review"' in toml


def test_settings_save_rejects_invalid_cover_size(client, cfg):
    r = client.post(
        "/settings",
        data={
            "download_format": "flac",
            "max_downloads_per_sync": "5",
            "user_agent": "Harmonist/0.1 ( x@y.z )",
            "cover_art_size": "999",  # not a valid Literal
            "gardener_level": "off",
            "log_level": "info",
        },
    )
    assert r.status_code == 200
    assert "Couldn't save" in r.text
    # nothing persisted
    assert not (cfg.paths.config_dir / "harmonist.toml").exists()


def test_settings_save_rejects_an_unknown_gardener_level(client, cfg):
    """The level is a Literal, not free text, and it decides what Harmonist does
    to a library unattended — so a value nobody defined is a rejection, never a
    quiet fall back to `off` (which would look identical to a saved setting) or
    to `review` (which would start spending the MusicBrainz budget)."""
    r = client.post(
        "/settings",
        data={
            "download_format": "flac",
            "max_downloads_per_sync": "5",
            "user_agent": "Harmonist/0.1 ( x@y.z )",
            "cover_art_size": "original",
            "gardener_level": "enrich",  # #273's level, not implemented yet
            "log_level": "info",
        },
    )
    assert r.status_code == 200
    assert "Couldn't save" in r.text
    assert client.app.state.cfg.gardener.level == "off"
    assert not (cfg.paths.config_dir / "harmonist.toml").exists()


def test_settings_offers_check_now_only_once_the_check_is_on(client, cfg):
    """The control's escape hatch from the empty hour after enabling it (#312).
    Inert while the level is `off`: there is no background pass to pre-empt, and
    a button that spends a hundred MusicBrainz requests must not be the way round
    a setting that says not to."""
    from harmonist.config import GardenerConfig

    assert cfg.gardener.level == "off"
    assert _button_is_disabled(client.get("/settings").text, "update-check-now") is True

    client.app.state.cfg = cfg.model_copy(update={"gardener": GardenerConfig(level="review")})

    assert _button_is_disabled(client.get("/settings").text, "update-check-now") is False


def test_check_now_starts_a_pass(client, cfg, monkeypatch):
    """`run_periodically` fires one full interval after startup, so enabling the
    check at 10:00 does nothing until 11:00. This is what stops that hour
    reading as a setting that didn't take."""
    from harmonist import gardener
    from harmonist.config import GardenerConfig

    swept = threading.Event()

    def _sweep(albums, **kw):
        swept.set()
        return gardener.PassResult(asked=0, examined=0, flagged=0, gone=0, failed=0)

    monkeypatch.setattr(client.app.state.scan_runner, "has_completed", lambda: True, raising=False)
    monkeypatch.setattr(gardener, "sweep", _sweep)
    client.app.state.cfg = cfg.model_copy(update={"gardener": GardenerConfig(level="review")})

    r = client.post("/settings/update-check")

    assert r.status_code == 200
    assert "Update check started" in r.text
    assert swept.wait(5) is True
    # And the page says so where the button is. The flash alone is silence on
    # /settings — the status bar's JS is defined in index.html — so the outcome
    # rides back as an out-of-band swap of the control's own row.
    assert 'id="update-check-control"' in r.text
    assert 'hx-swap-oob="true"' in r.text
    assert "Checking now" in r.text


def test_check_now_says_why_when_it_declines(client, cfg, monkeypatch):
    """A press answered with silence reads as broken, so the tick's reason goes
    back to whoever pressed — here the level, which the button is disabled for
    but a stale page or a direct POST can still reach."""
    from harmonist import gardener

    swept = threading.Event()

    def _sweep(albums, **kw):
        swept.set()
        return gardener.PassResult(asked=0, examined=0, flagged=0, gone=0, failed=0)

    monkeypatch.setattr(gardener, "sweep", _sweep)

    r = client.post("/settings/update-check")  # cfg ships `off`

    assert r.status_code == 200
    assert "Update check not started" in r.text
    assert "background update checks are off" in r.text
    assert swept.wait(0.25) is False
    # Refusals get the same out-of-band answer as a start, for the same reason.
    assert 'hx-swap-oob="true"' in r.text
    assert "Not started — background update checks are off." in r.text


def test_tasks_shows_scanning_placeholder_while_scanning(client, cfg, monkeypatch):
    """While the background scan is in progress and the snapshot is empty, the
    inbox shows a 'Scanning…' placeholder with live counts (not 'Inbox empty')."""
    runner = client.app.state.scan_runner
    monkeypatch.setattr(
        runner,
        "status",
        lambda: {
            "state": "scanning",
            "dirs_scanned": 42,
            "albums_found": 10,
            "started_at": None,
            "finished_at": None,
            "last_error": None,
        },
    )
    r = client.get("/tasks")  # no albums on disk → empty snapshot
    assert "Scanning your library" in r.text
    assert "42" in r.text
    assert "directories" in r.text
    assert "Inbox is empty" not in r.text


def test_engaged_lifespan_serves_background_scan_snapshot(cfg):
    """With the lifespan running (TestClient as a context manager), the
    background scanner engages and routes serve its snapshot — not a
    request-time scan. The /scan/status endpoint reports progress.

    Startup also auto-kicks a reconcile pass the moment the first scan
    completes, and /tasks deliberately replaces the album cards with a
    "Sorting your library…" progress panel while reconcile is running — so
    asserting on /tasks right after the scan reports "done" raced that pass
    (the issue #52 flake: near-instant locally, but a late-scheduled thread
    on a loaded CI runner left it mid-pass). Wait for full startup
    quiescence — scan done AND the reconcile pass finished — first."""
    import time as _time

    _make_album(cfg, "BgAlbum")  # present before startup → initial scan finds it
    with TestClient(create_app(cfg), headers={"HX-Request": "true"}) as client:
        status: dict = {}
        reconcile: dict = {}
        for _ in range(200):
            status = client.get("/scan/status").json()
            reconcile = client.get("/reconcile/status").json()
            # finished_at set ⇒ the startup reconcile ran to completion (its
            # initial state is also "idle", so "idle" alone can't tell
            # not-yet-started from done).
            if (
                status["state"] == "done"
                and reconcile["state"] == "idle"
                and (reconcile["finished_at"] is not None)
            ):
                break
            _time.sleep(0.02)
        assert status["state"] == "done"
        assert status["albums_found"] == 1
        assert reconcile["state"] == "idle" and reconcile["finished_at"] is not None
        # /tasks serves the snapshot the background scan produced.
        assert "BgAlbum" in client.get("/tasks").text


def test_a_periodic_rescan_is_skipped_while_a_sync_or_reconcile_is_running():
    """The watcher waits for the music dir to go quiet so it can never scan
    mid-copy; a timer has no such instinct. A rescan landing halfway through a
    sync would read a part-written album directory and record Harmonist as
    having "started tracking" a folder that was still being written — a
    permanent Activity entry about a transient."""
    from harmonist.web.main import _periodic_rescan_if_idle

    class _Runner:
        def __init__(self, running: bool) -> None:
            self.is_running = running

    class _Scan:
        def __init__(self) -> None:
            self.rescans = 0

        def request_quiet_rescan(self) -> None:
            self.rescans += 1

    idle, busy = _Runner(False), _Runner(True)
    scan = _Scan()

    def tick(sync, reconcile) -> None:
        _periodic_rescan_if_idle(cast("Any", sync), cast("Any", reconcile), cast("Any", scan))

    tick(idle, idle)
    assert scan.rescans == 1  # the ordinary case: nothing else is happening

    tick(busy, idle)  # sync running
    tick(idle, busy)  # reconcile running
    tick(busy, busy)
    assert scan.rescans == 1


def test_an_album_that_appears_on_disk_is_picked_up_without_being_announced(cfg, monkeypatch):
    """An album dropped into the library with nothing in the app asking for a
    scan (#151) — no click, no mutation, no watcher event — turns up in the
    inbox anyway. On a network mount, where inotify delivers nothing at all
    (#152), the timer is the only thing that ever will.

    And it does so INVISIBLY: the rescan never reports "scanning", because that
    status dims the inbox and turns off pointer events until the scan finishes.
    A lock the user set in motion is protection; one that arrives hourly on a
    timer, under their cursor, is not.

    The settle delay is absurdly high so that even if the watcher did see the
    new folder, its rescan could not land inside the test — what happens here
    can only be the periodic rescan's doing."""
    import time as _time

    import harmonist.web.main as main_mod

    # The real interval is an hour and deliberately not configurable, so the
    # test shortens the constant rather than a setting.
    monkeypatch.setattr(main_mod, "_RESCAN_INTERVAL", timedelta(seconds=0.05))
    cfg.library = LibraryConfig(watch_settle_seconds=1000)
    _make_album(cfg, "Present")

    with TestClient(create_app(cfg), headers={"HX-Request": "true"}) as client:
        # Wait for startup to go quiet first — the reconcile pass kicked at the
        # end of the first scan requests a scan of its own, and mistaking that
        # for the periodic one would pass this test with none running at all.
        for _ in range(200):
            scan = client.get("/scan/status").json()
            reconcile = client.get("/reconcile/status").json()
            if scan["state"] == "done" and reconcile["finished_at"] is not None:
                break
            _time.sleep(0.02)
        first = client.get("/scan/status").json()["seq"]
        assert first >= 1
        assert "Arrived" not in client.get("/tasks").text

        before = client.get("/scan/status").json()

        _make_album(cfg, "Arrived")

        for _ in range(250):
            if client.get("/scan/status").json()["seq"] > first:
                break
            _time.sleep(0.02)
        after = client.get("/scan/status").json()
        assert after["seq"] > first
        assert "Arrived" in client.get("/tasks").text

        # Nothing about the rescan reached the status EXCEPT the counter saying a
        # new snapshot exists. Asserted as "the whole status is otherwise
        # untouched" rather than "state was never 'scanning'": a rescan that took
        # the status would hold it for milliseconds here, so polling for the
        # word could miss it and the assertion would be unable to fail. A
        # published status leaves its marks — a fresh started_at, its own
        # progress counters — and this compares all of them at once.
        assert {k: v for k, v in after.items() if k != "seq"} == {
            k: v for k, v in before.items() if k != "seq"
        }


def test_album_page_re_reads_the_album_from_disk(cfg, monkeypatch):
    """The album page shows what is on disk NOW, not what the last scan saw
    (#151). This is the page where the user decides whether to re-tag, and on a
    network mount the watcher never fires — so without the per-album refresh
    that decision can be made against a reading taken at startup.

    The watcher is muzzled for the duration (`request_scan` no-ops), so nothing
    but the refresh itself can explain the new reading — and the scan sequence
    is asserted unchanged to say so a second way."""
    import time as _time

    from harmonist.web.scan_runner import ScanRunner

    d = _make_album(cfg, "Refreshed")
    monkeypatch.setattr(ScanRunner, "request_scan", lambda self: None)

    with TestClient(create_app(cfg), headers={"HX-Request": "true"}) as client:
        for _ in range(200):
            if client.get("/scan/status").json()["state"] == "done":
                break
            _time.sleep(0.02)
        album_id = _id_for(cfg, d)  # after create_app: it sets the registry root
        seq = client.get("/scan/status").json()["seq"]
        assert "Refreshed" in client.get(f"/album/{album_id}").text

        # The album title is corrected in Picard — the exact case #151 names.
        audio = MP4(d / "01 Track.m4a")
        audio["\xa9alb"] = ["Corrected In Picard"]
        audio.save()

        page = client.get(f"/album/{album_id}").text
        assert "Corrected In Picard" in page
        assert client.get("/scan/status").json()["seq"] == seq  # no scan ran


def test_app_attribute_is_memoized(monkeypatch):
    """`harmonist.web.main.app` builds the app once and caches it — repeated
    access (uvicorn does this at startup) must not run create_app() twice."""
    import harmonist.web.main as m

    calls: list = []

    def fake_create_app():
        calls.append(1)
        return object()

    monkeypatch.setattr(m, "create_app", fake_create_app)
    monkeypatch.setattr(m, "_app_singleton", None)
    first = m.app
    second = m.app
    assert first is second
    assert len(calls) == 1


def test_erase_sidecars_is_audited_per_album(client, cfg):
    """Erasing sidecars is the most destructive thing Harmonist does — every
    album's identity, match candidate and purchase link at once — and it had no
    audit record. One line per sidecar names which albums lost one; a bare count
    would say a nuke happened but not what it took."""
    from harmonist import activity_store
    from harmonist import sidecar as scmod
    from harmonist.activity_store import Source

    a = _make_album(cfg, "NukedOne")
    b = _make_album(cfg, "NukedTwo")
    scmod.write(a, Sidecar(mb_release_id="rel-a"))
    scmod.write(b, Sidecar(mb_release_id="rel-b"))
    activity_store.clear()

    assert client.post("/settings/erase-sidecars").status_code == 200

    rows = [e.message for e in activity_store.recent(50, source=Source.AUDIT)]
    deletes = [m for m in rows if m.startswith("sidecar.delete ")]
    assert len(deletes) == 2
    assert any("rel-a" in m for m in deletes) and any("rel-b" in m for m in deletes)
    assert any(m.startswith("sidecar.delete_all") and "removed=2" in m for m in rows)


def test_erase_sidecars_counts_and_reports_ones_it_could_not_delete(cfg, caplog):
    """The "Erased N sidecar(s)" count must not over-report. A sidecar that fails to
    unlink was skipped silently and left out of the count, so the user believed
    the nuke was complete while that album kept its identity (#104)."""
    from harmonist import sidecar as scmod

    a = _make_album(cfg, "Deletable")
    b = _make_album(cfg, "Stubborn")
    scmod.write(a, Sidecar(mb_release_id="rel-a"))
    scmod.write(b, Sidecar(mb_release_id="rel-b"))

    stubborn = scmod.sidecar_path(b)
    real_unlink = Path.unlink

    def selective_unlink(self, *args, **kwargs):
        if self == stubborn:
            raise OSError("permission denied")
        return real_unlink(self, *args, **kwargs)

    with mock.patch.object(Path, "unlink", selective_unlink), caplog.at_level("ERROR"):
        removed = scmod.delete_all(cfg.paths.music_dir)

    assert removed == 1, "the undeletable sidecar must not be counted as erased"
    assert scmod.sidecar_path(b).exists()
    assert any("could not be deleted" in r.message for r in caplog.records)


def test_erase_sidecars_removes_only_sidecars(client, cfg):
    from harmonist import sidecar as scmod

    d = _make_album(cfg, "Erasable")
    scmod.write(d, Sidecar(mb_release_id="rel-x"))
    assert scmod.has_sidecar(d)
    audio = d / "01 Track.m4a"
    assert audio.exists()

    r = client.post("/settings/erase-sidecars")
    assert r.status_code == 200
    assert "erased" in r.text.lower()
    # sidecar gone, audio untouched
    assert not scmod.has_sidecar(d)
    assert audio.exists()


def test_erase_sidecars_clears_bandcampsync_checkpoint(client, cfg):
    """Nuke also forgets the sync checkpoint so the next sync re-pages the
    whole collection. ignores.txt is left alone (not a re-download)."""
    state_file = cfg.paths.music_dir / ".bandcampsync-state.json"
    state_file.write_text('{"last_seen_token": "x"}')
    ignores = cfg.paths.config_dir / "ignores.txt"
    ignores.write_text("123  # keep me\n")

    r = client.post("/settings/erase-sidecars")
    assert r.status_code == 200
    assert not state_file.exists()  # checkpoint cleared
    assert "checkpoint reset" in r.text
    assert ignores.read_text() == "123  # keep me\n"  # ignores untouched


def test_settings_shows_sidecar_count(client, cfg):
    from harmonist import sidecar as scmod

    d = _make_album(cfg, "Counted")
    scmod.write(d, Sidecar(mb_release_id="rel-y"))
    r = client.get("/settings")
    assert "Erase sidecars" in r.text
    assert "Maintenance" in r.text


def test_library_new_ribbon_is_time_based(client, cfg):
    """A 'New' ribbon shows on albums downloaded within the last week, not older ones."""
    from datetime import timedelta

    now = datetime.now(UTC)
    fresh = _make_tagged_album(cfg, "FreshDrop", mbid="rel-fresh", tagged_at=now)
    sc.write(
        fresh,
        Sidecar(
            mb_release_id="rel-fresh",
            tagged_at=now,
            downloaded_at=now,
        ),
    )
    stale = _make_tagged_album(cfg, "OldDrop", mbid="rel-stale", tagged_at=now)
    sc.write(
        stale,
        Sidecar(
            mb_release_id="rel-stale",
            tagged_at=now,
            downloaded_at=now - timedelta(days=30),
        ),
    )
    r = client.get("/library")
    assert r.text.count(">New</span>") == 1  # only the fresh download


def test_library_compare_renders_the_tracklist(client, cfg, monkeypatch):
    """The on-demand comparison fetches MB and renders the per-track table."""
    d = _make_tagged_album(cfg, "Verifyme", mbid="rel-verify", tagged_at=datetime.now(UTC))

    def fake_release(mbid):
        return {
            "id": mbid,
            "title": "Verifyme",
            "medium-list": [
                {
                    "position": "1",
                    "track-list": [{"id": "t1", "title": "Track 1", "length": "1000"}],
                }
            ],
        }

    monkeypatch.setattr("harmonist.web.main.mb_lookup.fetch_release", fake_release)
    r = client.get(f"/library/{_id_for(cfg, d)}/compare")
    assert r.status_code == 200
    assert "track-diff" in r.text
    assert "Track 1" in r.text  # MusicBrainz's title for the one track


def test_library_compare_escapes_mb_error_text(client, cfg, monkeypatch):
    """#142: the compare panel's fetch-failure fragment escapes the MB message."""
    d = _make_tagged_album(cfg, "Hostile", mbid="rel-hostile", tagged_at=datetime.now(UTC))

    def boom(mbid):
        raise mb_lookup.MBError("MB ResponseError: <img src=x onerror=alert(1)>")

    monkeypatch.setattr("harmonist.web.main.mb_lookup.fetch_release", boom)

    r = client.get(f"/library/{_id_for(cfg, d)}/compare")
    assert r.status_code == 200
    assert "<img" not in r.text
    assert "&lt;img src=x onerror=alert(1)&gt;" in r.text
    assert "Couldn't fetch from MusicBrainz" in r.text


# ---------- the per-field tag comparison on the album page (#106) ----------


def _release_with_metadata(mbid="rel-cmp"):
    """A release carrying the album-level fields the panel shows."""
    return {
        "id": mbid,
        "title": "Obreel",
        "date": "2019-03-15",
        "artist-credit": [{"artist": {"id": "a1", "name": "Galán, Spieth & Guentner"}}],
        # `title` so the release-group row has a name to render instead of its
        # MBID (#298); MusicBrainz always sends one with the `release-groups`
        # include, and a fixture without it exercises only the fallback.
        "release-group": {"id": "rg-1", "title": "Obreel", "primary-type": "Album"},
        "label-info-list": [{"label": {"name": "Dial Records"}, "catalog-number": "DIAL 042"}],
        "medium-list": [
            {
                "position": "1",
                "format": "Digital Media",
                "track-list": [{"id": "t1", "title": "Kaskade", "length": "1000"}],
            }
        ],
    }


def test_album_page_compares_tags_field_by_field(client, cfg, monkeypatch):
    """#106: the album panel shows each field against what tagging would write.
    Absent-on-disk fields render as MusicBrainz values, not as conflicts."""
    d = _make_tagged_album(cfg, "Obreel", mbid="rel-cmp", tagged_at=datetime.now(UTC))
    monkeypatch.setattr(
        "harmonist.web.main.mb_lookup.fetch_release", lambda mbid: _release_with_metadata(mbid)
    )

    body = client.get(f"/library/{_id_for(cfg, d)}/compare").text

    assert "tags differ" in body  # the note's count, now in the album panel
    assert "Dial Records" in body and "DIAL 042" in body
    # A MusicBrainz-only value says so without relying on its colour — someone
    # who can't distinguish the purple still gets the mark and its tooltip.
    assert "Not in your files" in body
    for label in ("Album", "Album artist", "Date", "Label", "Cat. no.", "Comment"):
        assert f"<dt>{label}</dt>" in body, f"missing field row: {label}"


def test_musicbrainz_ids_read_as_names_and_link_to_musicbrainz(client, cfg, monkeypatch):
    """#298: the id rows carried three lines of raw hex in a panel whose whole
    job is to be scannable. They now say what the id stands for and link to it.

    The id itself is still what the row is about, so it stays reachable in the
    link's tooltip — and it must never be the visible text of a row MusicBrainz
    has named, which is what the negative assertion pins.
    """
    d = _make_tagged_album(cfg, "Obreel", mbid="rel-cmp", tagged_at=datetime.now(UTC))
    monkeypatch.setattr(
        "harmonist.web.main.mb_lookup.fetch_release", lambda mbid: _release_with_metadata(mbid)
    )

    body = client.get(f"/library/{_id_for(cfg, d)}/compare").text

    assert "<dt>Album artist IDs</dt>" in body
    assert "<dt>Release group</dt>" in body
    assert 'href="https://musicbrainz.org/artist/a1"' in body
    assert 'href="https://musicbrainz.org/release-group/rg-1"' in body
    # The name is the link text, and the id is only in the tooltip.
    assert ">Galán, Spieth &amp; Guentner</a>" in body
    assert ">a1</a>" not in body and ">rg-1</a>" not in body


def test_an_id_musicbrainz_cannot_name_stays_raw(client, cfg, monkeypatch):
    """The guard that keeps a difference from reading as a match.

    Names come from the release just fetched, so only MusicBrainz's side of a
    differing row has one. Rendering the name on both sides would put the same
    text above and below itself on the one row where the ids genuinely disagree
    — a merge, or a re-credited release — and hide the finding completely.
    """
    from harmonist import tagger as tagger_mod

    d = _make_tagged_album(cfg, "Obreel", mbid="rel-cmp", tagged_at=datetime.now(UTC))
    tagger_mod.tag_album(d, _release_with_metadata())  # files now carry artist a1
    recredited = _release_with_metadata() | {
        "artist-credit": [{"artist": {"id": "a2", "name": "Someone Else"}}]
    }
    monkeypatch.setattr("harmonist.web.main.mb_lookup.fetch_release", lambda mbid: recredited)

    body = client.get(f"/library/{_id_for(cfg, d)}/compare").text

    assert ">Someone Else</a>" in body  # MusicBrainz's side is named
    assert ">a1</a>" in body  # ours is the hex, because that is all we know
    assert 'class="mbid mbid--raw"' in body


def _set_owned_tag(album_dir: Path, atom: str, value: str) -> None:
    """Put `value` in every track's `atom` — a tag written by something other
    than Harmonist, which is what Picard with a non-default setting is."""
    for f in sorted(album_dir.glob("*.m4a")):
        audio = MP4(f)
        audio[atom] = [value.encode("utf-8")]
        audio.save()


def _three_country_release(mbid="rel-cmp"):
    """The release #329 was raised about, trimmed: issued in Germany, then the
    UK, then the US. `country` and `date` carry the German event alone."""
    return _release_with_metadata(mbid) | {
        "country": "DE",
        "date": "2013-06-07",
        "release-event-list": [
            {"date": "2013-06-07", "area": {"name": "Germany", "iso-3166-1-code-list": ["DE"]}},
            {
                "date": "2013-06-10",
                "area": {"name": "United Kingdom", "iso-3166-1-code-list": ["GB"]},
            },
            {
                "date": "2013-06-11",
                "area": {"name": "United States", "iso-3166-1-code-list": ["US"]},
            },
        ],
    }


def test_the_country_row_names_every_country_the_release_came_out_in(client, cfg, monkeypatch):
    """#329: a release issued in three countries read as though MusicBrainz knew
    of one, because the tag holds one and the row showed only the tag.

    The tag is still one code, and must be — Picard's `releasecountry` is a
    scalar too. What is added sits BESIDE it: the other countries, and the dates
    that go with them, which is also the answer to why the Date row says June 7.
    """
    d = _make_tagged_album(cfg, "Obreel", mbid="rel-cmp", tagged_at=datetime.now(UTC))
    monkeypatch.setattr(
        "harmonist.web.main.mb_lookup.fetch_release", lambda mbid: _three_country_release(mbid)
    )

    body = client.get(f"/library/{_id_for(cfg, d)}/compare").text

    assert "DE, GB, US" in body
    assert "MusicBrainz lists 3 release events" in body
    for area, date in (
        ("Germany", "2013-06-07"),
        ("United Kingdom", "2013-06-10"),
        ("United States", "2013-06-11"),
    ):
        assert area in body and date in body
    # The value the tagger will write is untouched by any of it: the row's own
    # value is still the bare code, and the list is a sibling of it rather than
    # something the tag grew into.
    cell = re.search(r"<dt>Country</dt>\s*<dd>(.*?)</dd>", body, re.DOTALL)
    assert cell, "no Country row"
    assert re.search(r'class="tag-fields__mb">DE<', cell.group(1))
    assert 'class="tag-fields__more"' in cell.group(1)


def test_one_release_country_gets_no_annotation(client, cfg, monkeypatch):
    """The everyday album. One event says nothing the Country row didn't, and a
    pill on every album in the library would be noise rather than information.

    A live path could produce this markup — the test above asserts it present on
    the same row of the same page — so its absence here is a real check.
    """
    d = _make_tagged_album(cfg, "Obreel", mbid="rel-cmp", tagged_at=datetime.now(UTC))
    one_event = _release_with_metadata() | {
        "country": "DE",
        "release-event-list": [
            {"date": "2013-06-07", "area": {"name": "Germany", "iso-3166-1-code-list": ["DE"]}}
        ],
    }
    monkeypatch.setattr("harmonist.web.main.mb_lookup.fetch_release", lambda mbid: one_event)

    body = client.get(f"/library/{_id_for(cfg, d)}/compare").text

    assert "<dt>Country</dt>" in body
    assert "release events" not in body


def test_a_second_release_country_is_not_reported_as_a_difference(client, cfg, monkeypatch):
    """#346, on the page: a file carrying "GB" for a release MusicBrainz issued
    in DE, GB and US is not out of date — Picard writes whichever country
    `preferred_release_countries` names, and all three are true of the release.

    The web rung rather than the tagger's, because the failure this guards is
    the two surfaces disagreeing: `plan_album` accepting the value while the
    panel still drew an arrow through it would leave the page reporting a
    difference the Library had already decided was not one.
    """
    from harmonist import tagger as tagger_mod

    d = _make_tagged_album(cfg, "Obreel", mbid="rel-cmp", tagged_at=datetime.now(UTC))
    release = _three_country_release()
    tagger_mod.tag_album(d, release)  # files now carry DE, MusicBrainz's first
    _set_owned_tag(d, ATOM_MB_ALBUM_COUNTRY, "GB")  # …as Picard would have
    monkeypatch.setattr("harmonist.web.main.mb_lookup.fetch_release", lambda mbid: release)

    body = client.get(f"/library/{_id_for(cfg, d)}/compare").text

    cell = re.search(r"<dt>Country</dt>\s*<dd>(.*?)</dd>", body, re.DOTALL)
    assert cell, "no Country row"
    assert "tag-fields__arrow" not in cell.group(1), "GB vs DE drawn as a difference"
    assert tagger_mod.plan_album(d, release).empty  # …and the two agree


def test_a_country_the_release_never_names_is_still_a_difference(client, cfg, monkeypatch):
    """The limit of that tolerance, on the page. "JP" is not one of this
    release's countries, so the row draws the arrow it should."""
    from harmonist import tagger as tagger_mod

    d = _make_tagged_album(cfg, "Obreel", mbid="rel-cmp", tagged_at=datetime.now(UTC))
    release = _three_country_release()
    tagger_mod.tag_album(d, release)
    _set_owned_tag(d, ATOM_MB_ALBUM_COUNTRY, "JP")
    monkeypatch.setattr("harmonist.web.main.mb_lookup.fetch_release", lambda mbid: release)

    body = client.get(f"/library/{_id_for(cfg, d)}/compare").text

    cell = re.search(r"<dt>Country</dt>\s*<dd>(.*?)</dd>", body, re.DOTALL)
    assert cell and "tag-fields__arrow" in cell.group(1)


def test_the_release_row_is_gone_from_the_comparison(client, cfg, monkeypatch):
    """#298 drops it: the comparison is fetched BY the release id the sidecar
    names, and the album header already shows and links that release, so the row
    matched on every album in the library bar one case.

    That case is a MusicBrainz merge, where the fetch redirects and returns a
    different id than the one asked for — and it is not lost, because dropping
    the row takes `mb_album_id` out of `compare.SHOWN_FIELDS` and the re-tag box
    below picks it up as the change it is.
    """
    d = _make_tagged_album(cfg, "Obreel", mbid="rel-cmp", tagged_at=datetime.now(UTC))
    monkeypatch.setattr(
        "harmonist.web.main.mb_lookup.fetch_release", lambda mbid: _release_with_metadata(mbid)
    )

    body = client.get(f"/library/{_id_for(cfg, d)}/compare").text
    assert "<dt>MusicBrainz release</dt>" not in body

    # The same label, from the live path that still emits it: a second album
    # whose release comes back under a different id than its sidecar asked for.
    # Its own album, not a re-fetch of the one above — that one's release is in
    # the cache now, and the second read would be served from it (#127).
    merged = _make_tagged_album(cfg, "Merged", mbid="rel-old", tagged_at=datetime.now(UTC))
    monkeypatch.setattr(
        "harmonist.web.main.mb_lookup.fetch_release",
        lambda mbid: _release_with_metadata("rel-survivor"),
    )

    body = client.get(f"/library/{_id_for(cfg, merged)}/compare").text
    assert "<dt>MusicBrainz release</dt>" in body
    # The in-value emphasis splits the id across <em> runs, so the string is
    # matched against the text rather than the markup.
    assert "rel-old" in body and "rel-survivor" in re.sub(r"</?em[^>]*>", "", body)


def test_a_small_difference_is_marked_inside_the_value(client, cfg, monkeypatch):
    """The reason in-value emphasis exists: a bare year against a full date
    differs by six characters that are invisible without marking."""
    d = _make_tagged_album(cfg, "Obreel", mbid="rel-cmp", tagged_at=datetime.now(UTC))
    from mutagen.mp4 import MP4

    audio = MP4(min(d.glob("*.m4a")))
    audio["\xa9day"] = ["2019"]  # MB says 2019-03-15
    audio.save()
    monkeypatch.setattr(
        "harmonist.web.main.mb_lookup.fetch_release", lambda mbid: _release_with_metadata(mbid)
    )

    body = client.get(f"/library/{_id_for(cfg, d)}/compare").text
    assert '<em class="diff-run">-03-15</em>' in body


def test_tracks_disagreeing_about_a_field_are_counted_not_hidden(client, cfg, monkeypatch):
    """An album tagged unevenly still gets a comparison — annotated with how
    many tracks back the value shown, and the outliers named on hover."""
    import shutil

    from mutagen.mp4 import MP4

    d = _make_tagged_album(cfg, "Uneven", mbid="rel-cmp", tagged_at=datetime.now(UTC))
    first = d / "01 Track.m4a"
    for n in (2, 3):
        shutil.copy(first, d / f"0{n} Track.m4a")
    # Two tracks agree with MusicBrainz, the third doesn't.
    for f in (first, d / "02 Track.m4a"):
        audio = MP4(f)
        audio["aART"] = ["Galán, Spieth & Guentner"]
        audio.save()
    odd = d / "03 Track.m4a"
    audio = MP4(odd)
    audio["aART"] = ["Someone Else"]
    audio.save()
    monkeypatch.setattr(
        "harmonist.web.main.mb_lookup.fetch_release", lambda mbid: _release_with_metadata(mbid)
    )

    body = client.get(f"/library/{_id_for(cfg, d)}/compare").text
    # The pill names the finding rather than reciting a ratio (#164) — a ratio
    # under "All N fields match MusicBrainz" reads as a second opinion on that
    # comparison, when it is a different one entirely.
    assert "1 track differs" in body
    assert "2 of your 3 tracks agree" in body  # the popover still has the count
    assert odd.name in body  # the outlier is named
    assert "Someone Else" in body  # and what it carries instead
    # Verb agrees with the count — one outlier is "The other differs", not
    # "The other differ". Easy to get wrong in a template, which is why it's
    # asserted rather than eyeballed.
    assert "The other differs." in body
    assert "The others differ." not in body


def test_the_columns_a_tracklist_drops_are_named_under_it(client, cfg, monkeypatch):
    """The #112 half of #309: a collapsed column must not be indistinguishable
    from a tag nobody looked at.

    So the dropped set is named, with its values a press away — and behind a
    native <details>, which brings its own keyboard handling and open state
    rather than a scripted toggle beside the hx-* attributes this page runs on.
    """
    d = _make_tagged_album(cfg, "Quiet", mbid="rel-quiet", tagged_at=datetime.now(UTC))
    audio = MP4(d / "01 Track.m4a")
    audio[ATOM_TITLE] = ["Ground Glass"]
    audio[ATOM_ARTIST] = ["Benoît Pioulard"]
    audio["aART"] = ["Benoît Pioulard"]
    audio.save()

    def fake_release(mbid):
        return {
            "id": mbid,
            "title": "Quiet",
            "artist-credit": [{"artist": {"id": "art-1", "name": "Benoît Pioulard"}}],
            "medium-list": [
                {
                    "position": "1",
                    "track-list": [{"id": "t1", "title": "Ground Glass", "length": "1000"}],
                }
            ],
        }

    monkeypatch.setattr("harmonist.web.main.mb_lookup.fetch_release", fake_release)
    body = client.get(f"/library/{_id_for(cfg, d)}/compare").text

    # Nothing about this track differs from its album or from MusicBrainz, so the
    # Artist column is a name repeated down a column and is dropped.
    assert not re.search(r"<th [^>]*>\s*Artist\s*</th>", body)
    # Shown in the open since #328, in the footer band under the table — the
    # values themselves are what prove the tag was checked rather than skipped
    # (#112), which a sentence naming them over a closed <details> only claimed.
    assert '<div class="track-foot">' in body
    assert "The same on every track" in body
    assert "<dt>Artist</dt>" in body
    assert "Benoît Pioulard" in body


def test_a_disc_subtitle_change_is_drawn_on_the_disc_heading(client, cfg, monkeypatch):
    """#320, end to end.

    MusicBrainz names Hybrid's second medium *Live Angle*; the files carry no
    disc subtitle at all. That is a real change a re-tag would make, and as a
    column it was one value repeated down a whole disc — so it is drawn on the
    heading that already names the disc, with MusicBrainz's reading stacked
    beneath the files'.

    The web rung, not the pure one: `compare` can only say which slots differ.
    Whether the template draws the second line, and draws it in the slot that
    changed, is decided by the markup — and the model tests would stay green
    against a heading that never rendered its MusicBrainz half at all.
    """
    d = _make_tagged_album(cfg, "Hybrid", mbid="rel-hybrid", tagged_at=datetime.now(UTC))
    shutil.copy(SINE_M4A, d / "02 Track.m4a")
    for name, disc, title in (("01 Track.m4a", 1, "Higher"), ("02 Track.m4a", 2, "Finished")):
        audio = MP4(d / name)
        audio[ATOM_TITLE] = [title]
        audio[ATOM_MB_ALBUM_ID] = [b"rel-hybrid"]
        # One track per disc, stated on the file, so the heading's track count
        # agrees and the subtitle is the only thing left to report.
        audio["trkn"] = [(1, 1)]
        audio["disk"] = [(disc, 2)]
        audio.save()

    def fake_release(mbid):
        return {
            "id": mbid,
            "title": "Hybrid",
            "medium-list": [
                {
                    "position": "1",
                    "track-list": [{"id": "t1", "title": "Higher", "length": "1000"}],
                },
                {
                    "position": "2",
                    "title": "Live Angle",
                    "track-list": [{"id": "t2", "title": "Finished", "length": "1000"}],
                },
            ],
        }

    monkeypatch.setattr("harmonist.web.main.mb_lookup.fetch_release", fake_release)
    body = client.get(f"/library/{_id_for(cfg, d)}/compare").text

    # No column for it, and no entry in the collapsed set either — the heading
    # accounts for it, so it is on the page in exactly one place.
    assert not re.search(r"<th [^>]*>\s*Disc subtitle\s*</th>", body)
    assert "<dt>Disc subtitle</dt>" not in body

    # The heading is a comparison: what the files say, then what MusicBrainz
    # says, in the slot that changed.
    assert '<span class="disc-head">' in body
    assert '<span class="disc-head__cell">Disc 2</span>' in body
    assert '<span class="disc-head__cell disc-head__mb">Disc 2 — Live Angle' in body
    # Disc 1 has nothing to report, so it draws one line and no hexagon.
    assert body.count("MusicBrainz's description of this disc") == 1

    # The slots that agree are EMPTY on the MusicBrainz line — held open so the
    # changed one stays under its counterpart, but saying nothing. Empty rather
    # than absent because `:not(:empty)` is what suppresses their "·" separator,
    # so a cell that renders a stray space renders a bullet before nothing.
    assert body.count('<span class="disc-head__cell disc-head__mb disc-head__meta"></span>') == 2


def test_one_musicbrainz_note_lands_in_the_album_panel(client, cfg, monkeypatch):
    """#328. The hexagon band was drawn twice — over Tags and over the tracklist
    — saying the same thing about the same fetch. One note now, in the panel
    beside Re-tag from MB, carrying both summaries.

    Two halves worth asserting together, because either alone is satisfied by a
    broken page: the panel has to render an empty slot at page load, and the
    compare response has to swap into THAT id. A typo in either produces a
    perfectly correct-looking response that lands nowhere (#210).
    """
    d = _make_tagged_album(cfg, "Noted", mbid="rel-noted", tagged_at=datetime.now(UTC))
    audio = MP4(d / "01 Track.m4a")
    audio[ATOM_TITLE] = ["Ground Glass"]
    audio.save()

    def fake_release(mbid):
        return {
            "id": mbid,
            "title": "Noted",
            "medium-list": [
                {"position": "1", "track-list": [{"id": "t1", "title": "Ground Glass"}]}
            ],
        }

    monkeypatch.setattr("harmonist.web.main.mb_lookup.fetch_release", fake_release)
    album_id = _id_for(cfg, d)

    page = client.get(f"/album/{album_id}").text
    assert f'<div id="album-mb-note-{album_id}"></div>' in page, "the panel's empty slot"

    body = client.get(f"/library/{album_id}/compare").text
    assert f'<div id="album-mb-note-{album_id}" hx-swap-oob="true">' in body

    # Both summaries, in one legend, said once each. The count of the legend's
    # own wrapper rather than of any phrase inside it: "tags" and "tracks" occur
    # all over this response, and the thing being asserted is that there is ONE
    # note, which only the wrapper can say.
    assert body.count('class="mb-note mb-note--panel"') == 1
    assert body.count('<span class="mb-note__legend">') == 1
    legend = re.search(r'class="mb-note__legend">(.*?)</span>', body, re.DOTALL)
    assert legend and " · " in legend.group(1), "both summaries, joined into one line"
    # This album has findings ("2 of 18 tags differ"), so the note keeps its
    # colour — the class above is the whole of it, with no `--quiet` (#352).
    assert "mb-note--quiet" not in body


def test_an_album_with_nothing_to_act_on_gets_no_note_at_all(client, cfg, monkeypatch):
    """#358, finishing what #352 started. That issue dropped the band's tint on
    an album whose tags and tracks all match; the band still had to be drawn
    because it also carried the read timestamp, which #355 has since moved to
    the panel's dates. With nothing else to say, the quietest form of "there is
    nothing to do" is no band.

    The wrapper still comes back, and that half matters as much as the absence:
    it is the out-of-band swap target, so a later /compare that DOES find
    something has somewhere to put it — and an empty one is what clears a note
    that no longer applies.

    Driven through the template global rather than by building an album whose
    eighteen tags and every track match MusicBrainz: that fixture would assert
    `advisory`'s own logic a second time, and go stale the next time a tag joins
    the comparison. The note-carrying path is covered by the test above, which
    is what makes this absence worth asserting.
    """
    d = _make_tagged_album(cfg, "Quiet", mbid="rel-quiet", tagged_at=datetime.now(UTC))
    monkeypatch.setattr("harmonist.web.main.mb_lookup.fetch_release", lambda mbid: {"id": mbid})
    album_id = _id_for(cfg, d)
    env = client.app.state.templates.env
    monkeypatch.setitem(env.globals, "advisory", lambda album, tracks: True)

    body = client.get(f"/library/{album_id}/compare").text

    assert f'<div id="album-mb-note-{album_id}" hx-swap-oob="true">' in body
    assert "mb-note__legend" not in body


def test_forget_sits_at_the_far_end_of_the_actions_row(client, cfg):
    """#328, and web-ui rule 4: rare and hard to undo belongs out of the path.

    Forget deletes the sidecar and reverts the album to NEW, and it sat one
    button away from Re-tag — the thing you came to press. Asserted as the ORDER
    of the row, not just the class, because `ml-auto` on a button that is not
    last pushes the ones after it right along with it.
    """
    d = _make_tagged_album(cfg, "Ordered", mbid="rel-ordered", tagged_at=datetime.now(UTC))
    page = client.get(f"/album/{_id_for(cfg, d)}").text

    row = re.search(r'<div id="album-actions-[^"]+".*?\n            </div>', page, re.DOTALL)
    assert row, "the actions row"
    labels = [t.strip() for t in re.findall(r">\s*([A-Z][A-Za-z- ]+)\s*</button>", row.group(0))]
    assert labels[0] == "Re-tag from MB"
    assert labels[-1] == "Forget", "last in the DOM, so it is last in the tab order too"
    assert re.search(r'class="ml-auto [^"]*"[^>]*>\s*Forget', row.group(0), re.DOTALL) or re.search(
        r'class="ml-auto[^"]*"', row.group(0)
    ), "and pushed to the far end"


def test_a_missing_track_and_a_missing_disc_are_both_marked(client, cfg, monkeypatch):
    """#326. Both placements of the mark, on one page.

    On a track row it sits in the NUMBER cell — the slot the video mark uses, so
    a half-ripped disc reads as a column of them rather than as nine notices —
    and the words are beside it, so the glyph itself is `aria-hidden`.

    On the heading of a disc nobody ripped it REPLACES the words, so there the
    accessible name is load-bearing and is asserted as such: the text beside it
    is the disc's name and says nothing about absence.
    """
    d = _make_tagged_album(cfg, "Halfrip", mbid="rel-halfrip", tagged_at=datetime.now(UTC))
    audio = MP4(d / "01 Track.m4a")
    audio[ATOM_TITLE] = ["Song 1"]
    audio["trkn"] = [(1, 2)]
    audio["disk"] = [(1, 2)]
    audio.save()

    def fake_release(mbid):
        def medium(pos, titles):
            return {
                "position": str(pos),
                "track-list": [
                    {"id": f"t{pos}{i}", "title": t, "length": "1000"}
                    for i, t in enumerate(titles, 1)
                ],
            }

        return {
            "id": mbid,
            "title": "Halfrip",
            "medium-list": [medium(1, ["Song 1", "Song 2"]), medium(2, ["Bonus 1", "Bonus 2"])],
        }

    monkeypatch.setattr("harmonist.web.main.mb_lookup.fetch_release", fake_release)
    body = client.get(f"/library/{_id_for(cfg, d)}/compare").text

    # The missing track: mark in the number cell, words in the cell beside it.
    assert re.search(r'class="track-diff__num">\s*<span class="track-diff__missing">', body), (
        "the mark goes in the number cell, where the video mark goes"
    )
    assert re.search(r'class="track-diff__absent">\s*Not in your files\s*<', body)

    # The absent disc: the mark carries the meaning on its own, so it has a name.
    assert (
        '<span class="track-diff__disc-missing" role="img" aria-label="Not in your files"' in body
    )
    assert "None of this disc's 2 tracks are in your files" in body
    # And the headline says it in words, for the reader who never reaches either.
    assert "Disc 2 not in your files" in body


def test_identifiers_start_revealed_when_they_are_the_only_difference(client, cfg, monkeypatch):
    """#339, end to end and in both halves.

    The page said "1 of 1 tracks differ" over a table where nothing was marked.
    Asserted through the rendered response because the two halves of the initial
    state live in different partials since #328 — the checkbox in the section
    header, the class on the wrapper inside the tracklist — and a model test
    cannot see them disagree.
    """
    # TWO tracks, with a different ISRC each: rule 1 only earns a column when the
    # readings differ BETWEEN tracks, so a one-track album can never reach the
    # state this is about.
    d = _make_tagged_album(cfg, "Isrc", mbid="rel-isrc", tagged_at=datetime.now(UTC))
    shutil.copy(SINE_M4A, d / "02 Track.m4a")
    for name, title, isrc in (
        ("01 Track.m4a", "Quoth", b"GBAAA0000001"),
        ("02 Track.m4a", "Audax", b"GBAAA0000003"),
    ):
        audio = MP4(d / name)
        audio[ATOM_TITLE] = [title]
        audio[ATOM_MB_ALBUM_ID] = [b"rel-isrc"]
        audio["trkn"] = [(1 if "01" in name else 2, 2)]
        audio["----:com.apple.iTunes:ISRC"] = [isrc]
        audio.save()

    def fake_release(mbid):
        return {
            "id": mbid,
            "title": "Isrc",
            "medium-list": [
                {
                    "position": "1",
                    "track-list": [
                        {
                            "id": "t1",
                            "title": "Quoth",
                            "length": "1000",
                            "recording": {"id": "r1", "isrc-list": ["GBAAA0000002"]},
                        },
                        {
                            "id": "t2",
                            "title": "Audax",
                            "length": "1000",
                            "recording": {"id": "r2", "isrc-list": ["GBAAA0000004"]},
                        },
                    ],
                }
            ],
        }

    monkeypatch.setattr("harmonist.web.main.mb_lookup.fetch_release", fake_release)
    body = client.get(f"/library/{_id_for(cfg, d)}/compare").text

    assert re.search(r'<div class="tracklist tracklist--ids">', body), "the wrapper"
    assert re.search(r'<input type="checkbox" class="cursor-pointer" checked', body), "the box"


def test_a_featured_credit_reads_as_the_artists_it_names(client, cfg, monkeypatch):
    """#309's second half, end to end.

    MusicBrainz assembles a credit from named artists and the words between them,
    and Picard flattens it into one string on the way to the file. The page has
    the release in hand, so it can put the structure back: each artist a link,
    joined by MusicBrainz's own " feat. ".

    The web rung, not the pure one. `compare` deliberately never sees the release
    payload — it is told a value is a credit and nothing more — so whether the
    parts actually reach the markup is decided by the template and the context,
    which only a rendered response can show.
    """
    d = _make_tagged_album(cfg, "Featured", mbid="rel-feat", tagged_at=datetime.now(UTC))
    audio = MP4(d / "01 Track.m4a")
    audio[ATOM_TITLE] = ["Empire Systems"]
    audio[ATOM_ARTIST] = ["Rafael Anton Irisarri feat. Julia Kent"]
    audio["aART"] = ["Rafael Anton Irisarri"]
    audio.save()

    def fake_release(mbid):
        return {
            "id": mbid,
            "title": "A Fragile Geography",
            "artist-credit": [{"artist": {"id": "art-1", "name": "Rafael Anton Irisarri"}}],
            "medium-list": [
                {
                    "position": "1",
                    "track-list": [
                        {
                            "id": "rt-1",
                            "title": "Empire Systems",
                            "length": "1000",
                            "artist-credit": [
                                {"artist": {"id": "art-1", "name": "Rafael Anton Irisarri"}},
                                " feat. ",
                                {"artist": {"id": "art-2", "name": "Julia Kent"}},
                            ],
                        }
                    ],
                }
            ],
        }

    monkeypatch.setattr("harmonist.web.main.mb_lookup.fetch_release", fake_release)
    body = client.get(f"/library/{_id_for(cfg, d)}/compare").text

    # The track is credited to two artists where the album is credited to one, so
    # the Artist column is earned rather than collapsed.
    assert re.search(r"<th [^>]*>\s*Artist\s*</th>", body)
    # Both artists linked, and the join phrase between them as MusicBrainz spells
    # it — not the "; " a list of ids would have been joined with.
    assert 'href="https://musicbrainz.org/artist/art-1"' in body
    assert 'href="https://musicbrainz.org/artist/art-2"' in body
    assert re.search(r">Rafael Anton Irisarri</a> feat\. <a [^>]*>Julia Kent</a>", body)


def test_library_compare_flags_title_discrepancy(client, cfg, monkeypatch):
    """When on-disk track titles differ from MB (e.g. a featured-artist tidy-up
    on MB after tagging), the tracklist reports the metadata difference even
    though the lengths line up perfectly (issue #29)."""
    d = _make_tagged_album(cfg, "Mismatch", mbid="rel-mm", tagged_at=datetime.now(UTC))
    audio = MP4(d / "01 Track.m4a")
    audio[ATOM_TITLE] = ["Ground Glass [w/ Foxes in Fiction]"]
    audio.save()

    def fake_release(mbid):
        return {
            "id": mbid,
            "title": "Mismatch",
            "medium-list": [
                {
                    "position": "1",
                    # MB has the cleaned-up title; the file still carries the old one.
                    "track-list": [{"id": "t1", "title": "Ground Glass", "length": "1000"}],
                }
            ],
        }

    monkeypatch.setattr("harmonist.web.main.mb_lookup.fetch_release", fake_release)
    r = client.get(f"/library/{_id_for(cfg, d)}/compare")
    assert r.status_code == 200
    # Both titles on the page: the file's, and MusicBrainz's beneath it. The
    # file's arrives split across diff runs — the featured-artist suffix is
    # marked in place — so the assertion is on the marked run itself.
    assert '<em class="diff-run"> [w/ Foxes in Fiction]</em>' in r.text
    assert "1 of 1 tracks differs" in r.text
    # Neither wording the tracklist's headline uses when nothing differs. Named
    # exactly rather than as a bare "match MusicBrainz", which the collapsed
    # columns note under the table now says truthfully about fields that DO
    # agree (#309) — a substring that broad stopped being about the headline.
    assert "tracks match MusicBrainz" not in r.text
    assert "Matches MusicBrainz" not in r.text


def test_album_page_loads_the_comparison_lazily(client, cfg, monkeypatch):
    """The MusicBrainz comparison is fetched on load rather than rendered with
    the page: it costs an MB request, and the budget is one per second
    (review-gate item 6). Lazy keeps that cost to opening the page.

    MusicBrainz is stubbed to answer in full, so the empty `#compare-<id>` is
    the render's own doing and not the absence of anything to put in it."""
    d = _make_tagged_album(cfg, "HasVerify", mbid="rel-v2", tagged_at=datetime.now(UTC))
    aid = _id_for(cfg, d)
    monkeypatch.setattr(
        "harmonist.web.main.mb_lookup.fetch_release", lambda mbid: _release_with_metadata(mbid)
    )

    page = client.get(f"/album/{aid}").text
    assert f"/library/{aid}/compare" in page
    assert 'hx-trigger="load"' in page
    assert "<dt>Cat. no.</dt>" not in page, "the comparison waits for the fetch"


def test_library_detail_shows_ambiguous_bandcamp_ids(client, cfg):
    """An ambiguously-linked album (COMPLETE, candidate ids but no single id)
    shows the candidate item ids in the store badge tooltip."""
    d = _make_album(cfg, "Ambi")
    audio = MP4(d / "01 Track.m4a")
    audio[ATOM_MB_ALBUM_ID] = [b"rel-ambi"]
    audio.save()
    sc.write(
        d,
        Sidecar(
            store_url="https://x.bandcamp.com/album/ambi",
            bandcamp=BandcampInfo(item_id=None, candidate_item_ids=[111, 222]),
            mb_release_id="rel-ambi",
            tagged_at=datetime.now(UTC),
        ),
    )
    r = client.get(f"/album/{_id_for(cfg, d)}")
    assert "ambiguous: item #111 or #222" in r.text


def test_about_page_renders(client):
    r = client.get("/about")
    assert r.status_code == 200
    assert "About Harmonist" in r.text
    assert "GPL-3.0-or-later" in r.text
    assert "mutagen" in r.text
    assert "MusicBrainz" in r.text
    assert "github.com/randomphrase/harmonist" in r.text


def test_git_sha_prefers_baked_env(monkeypatch):
    """The build SHA is taken from HARMONIST_GIT_SHA (baked at Docker build) when
    set — so the container reports its commit even with no .git present."""
    from harmonist.web.main import _git_sha

    monkeypatch.setenv("HARMONIST_GIT_SHA", "deadbeefcafebabe1234")
    assert _git_sha() == "deadbeefcafe"  # truncated to 12


def test_container_detected_from_env_selects_no_timestamp_format(monkeypatch):
    """HARMONIST_IN_CONTAINER=1 marks a container → log format omits the timestamp
    (the driver stamps at capture); the dev format keeps one."""
    from harmonist.web import main as m

    monkeypatch.setenv("HARMONIST_IN_CONTAINER", "1")
    assert m._in_container() is True
    # The two formats differ exactly by the timestamp field.
    assert "asctime" not in m._LOG_FORMAT_NO_TS
    assert "asctime" in m._LOG_FORMAT_WITH_TS


# ---------- startup permission gate ----------


def test_validate_runtime_paths_ok_and_creates_dirs(cfg):
    from harmonist.web.main import _validate_runtime_paths

    _validate_runtime_paths(cfg)  # must not raise
    assert cfg.paths.music_dir.is_dir()
    assert cfg.paths.config_dir.is_dir()
    assert not list(cfg.paths.music_dir.glob(".harmonist-write-test-*"))  # probe cleaned up


def test_validate_runtime_paths_raises_when_unwritable(tmp_path):
    from harmonist.web.main import _validate_runtime_paths

    # A file where the music dir should be → mkdir/touch fails → fail fast.
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file, not a directory")
    bad = Config(
        paths=PathsConfig(config_dir=tmp_path / "config", music_dir=blocker / "music"),
        bandcamp=BandcampConfig(),
        server=ServerConfig(),
        test=TestConfig(mode="fixture"),
    )
    with pytest.raises(RuntimeError, match="not writable"):
        _validate_runtime_paths(bad)


def test_run_bandcamp_sync_precreates_ignores_file(cfg, monkeypatch):
    """bandcampsync seeds a missing ignores file from a template that only
    exists in its own Docker image (/ignores.template.txt). We pre-create the
    file so its copy is skipped — no 'No such file' on first sync."""
    from harmonist.web import main as main_mod

    cfg.paths.config_dir.mkdir(parents=True, exist_ok=True)
    cfg.cookies_file.write_text("cookie", encoding="utf-8")  # get past the cookies check
    assert not cfg.ignores_file.exists()

    called: dict[str, bool] = {}
    monkeypatch.setattr(main_mod, "HarmonistSyncer", lambda *a, **k: called.setdefault("yes", True))

    main_mod._run_bandcamp_sync(cfg)

    assert cfg.ignores_file.exists()  # pre-created → bandcampsync won't copy the template
    # Seeded from the vendored template, not left blank.
    assert "exclude releases from downloads" in cfg.ignores_file.read_text()
    assert called.get("yes")


def test_run_bandcamp_sync_keeps_existing_ignores_file(cfg, monkeypatch):
    """An existing ignores file (with the user's ids) is never overwritten."""
    from harmonist.web import main as main_mod

    cfg.paths.config_dir.mkdir(parents=True, exist_ok=True)
    cfg.cookies_file.write_text("cookie", encoding="utf-8")
    cfg.ignores_file.write_text("12345  # keep me\n", encoding="utf-8")
    monkeypatch.setattr(main_mod, "HarmonistSyncer", lambda *a, **k: None)

    main_mod._run_bandcamp_sync(cfg)

    assert cfg.ignores_file.read_text() == "12345  # keep me\n"


def _sync_finished_message(monkeypatch, **result_fields) -> str:
    """Run SyncRunner's body against a stub syncer result and return the single
    Activity line it wrote."""
    from types import SimpleNamespace

    from harmonist.web.sync_runner import SyncRunner

    said: list[str] = []
    monkeypatch.setattr("harmonist.activity.info", lambda msg, **kw: said.append(msg))
    runner = SyncRunner(runner_fn=lambda: SimpleNamespace(**result_fields))
    runner._run()
    assert len(said) == 1
    return said[0]


def test_sync_finish_reports_pre_orders_it_could_not_download(monkeypatch):
    """A pre-order is skipped inside bandcampsync, on a logger we pin to
    WARNING — so a sync that downloads nothing else reports "0 new items" and
    the purchase looks lost. Say it is owed and being retried (#351)."""
    msg = _sync_finished_message(
        monkeypatch, new_items=0, skipped_for_limit=0, deferred_preorders=1
    )
    assert msg == (
        "Bandcamp sync finished — 0 new items; 1 pre-order not released yet — "
        "each sync retries them"
    )


def test_sync_finish_says_nothing_about_pre_orders_when_there_are_none(monkeypatch):
    """The clause only appears when a pre-order was actually deferred — the
    steady-state line stays as it was."""
    msg = _sync_finished_message(
        monkeypatch, new_items=2, skipped_for_limit=0, deferred_preorders=0
    )
    assert msg == "Bandcamp sync finished — 2 new items"


# ---------- Potential-download actions ----------


def _pp(item_id, *, band="B", title="T", url="https://x.bandcamp.com/album/y", fmt="alac"):
    from harmonist.pending_downloads import PendingPurchase

    return PendingPurchase(item_id=item_id, band=band, title=title, url=url, fmt=fmt)


def test_pending_skip_ignores_and_removes(client, cfg):
    from harmonist import pending_downloads as pd

    pd.replace_all([_pp(42)])
    r = client.post("/pending/42/skip")
    assert r.status_code == 200
    assert pd.get(42) is None  # dropped from the store
    assert "42" in cfg.ignores_file.read_text()  # appended to ignores.txt


def test_pending_download_approves_and_removes(client):
    from harmonist import pending_downloads as pd

    pd.replace_all([_pp(43)])
    r = client.post("/pending/43/download")
    assert r.status_code == 200
    assert pd.get(43) is None
    assert pd.is_approved(43)  # the next sync will fetch it


def test_pending_card_has_inline_library_search(client):
    """The card carries the unified 'already in your library?' live search inline
    (no separate Match toggle) — the search box + results container are always
    present, so the auto-match and a manual search share one coherent surface."""
    from harmonist import pending_downloads as pd

    pd.replace_all([_pp(44, band="Variant", title="Sequential Sleep")])
    body = client.get("/tasks").text
    assert "Already in your library?" in body
    assert 'name="q"' in body  # the inline library search input
    assert 'id="match-results-44"' in body  # the results container the search targets


def test_pending_match_results_endpoint_searches_library(client, cfg):
    """The inline search hits /pending/{id}/match/results, returning library rows."""
    from harmonist import pending_downloads as pd

    _make_album(cfg, "Sequential Sleep")
    pd.replace_all([_pp(44, band="Variant", title="Sequential Sleep")])
    r = client.get("/pending/44/match/results", params={"q": "Sequential"})
    assert r.status_code == 200
    assert "Sequential Sleep" in r.text
    assert "/pending/44/match" in r.text  # each row Links via the match POST


def test_pending_result_opens_the_album_page_to_verify(client, cfg):
    """#129: verifying a match opens the album's page in a NEW TAB rather than a
    dialog. You're mid-task in the pending list, and navigating away from it is
    the one thing the dialog was protecting against — a new tab protects it just
    as well, without a dialog lifecycle, and the Link button is still right
    there in the row."""
    from harmonist import pending_downloads as pd
    from harmonist import scanner
    from harmonist import sidecar as scmod

    d = _make_album(cfg, "Black Soma", mbid="rel-bs")
    scmod.write(d, Sidecar(mb_release_id="rel-bs"))
    album_id = _id_for(cfg, d)
    a = next(x for x in scanner.scan(cfg.paths.music_dir) if x.path == d)
    # Pending band/title match the on-disk album → it's seeded as the suggestion,
    # whose row deep-links into the verify modal.
    pd.replace_all([_pp(77, band=a.artist, title=a.title, url="https://3six.net/album/black-soma")])

    body = client.get("/tasks").text
    # The result row links to the album's page, in a new tab, with noopener —
    # target=_blank without it hands the opened page a reference back.
    assert f'href="/album/{album_id}" target="_blank" rel="noopener"' in body
    # …and Link is still on the row itself, carrying the pending item and album.
    assert "/pending/77/match" in body
    assert f'"album_id": "{album_id}"' in body


def test_pending_match_link_fills_item_id(client, cfg):
    """Linking a potential download to an on-disk album writes the purchase's
    item_id + store_url onto that album's sidecar, and drops it from pending."""
    from harmonist import pending_downloads as pd
    from harmonist import sidecar as scmod

    d = _make_album(cfg, "Existing", mbid="rel-xyz")
    scmod.write(d, Sidecar(mb_release_id="rel-xyz"))
    album_id = _id_for(cfg, d)

    pd.replace_all([_pp(45, url="https://x.bandcamp.com/album/y")])
    from harmonist import activity

    activity.clear()
    r = client.post("/pending/45/match", data={"album_id": album_id})
    assert r.status_code == 200
    assert pd.get(45) is None
    sc = scmod.read(d)
    assert sc.bandcamp is not None
    assert sc.bandcamp.item_id == 45
    assert sc.store_url == "https://x.bandcamp.com/album/y"
    # A manual link is logged like an auto-link: "Linked <purchase> → <album>".
    assert any(m.message.startswith("Linked ") for m in activity.recent(5))


def test_pending_match_to_library_album_skips_rescan(client, cfg, monkeypatch):
    """Matching a potential download to a Library (COMPLETE) album leaves it
    complete — no inbox change — so it must NOT trigger a full rescan (the flicker
    the user hit while working through the adoption residue)."""
    from datetime import datetime

    from harmonist import pending_downloads as pd

    d = _make_tagged_album(cfg, "Lib Match", mbid="rel-lm", tagged_at=datetime.now(UTC))
    calls: list[int] = []
    monkeypatch.setattr(
        client.app.state.scan_runner, "request_scan", lambda *a, **k: calls.append(1)
    )
    pd.replace_all([_pp(46, url="https://x.bandcamp.com/album/z")])
    r = client.post("/pending/46/match", data={"album_id": _id_for(cfg, d)})
    assert r.status_code == 200
    assert calls == []  # no rescan → no inbox flicker


def test_pending_match_to_surrender_triggers_rescan(client, cfg, monkeypatch):
    """Matching a Needs-Sync (surrender) album moves it out of the inbox → a
    rescan IS needed so its card clears."""
    from harmonist import pending_downloads as pd

    d = _needs_sync_album(cfg, "Surr Match", "rel-sm")
    calls: list[int] = []
    monkeypatch.setattr(
        client.app.state.scan_runner, "request_scan", lambda *a, **k: calls.append(1)
    )
    pd.replace_all([_pp(47, url="https://x.bandcamp.com/album/w")])
    r = client.post("/pending/47/match", data={"album_id": _id_for(cfg, d)})
    assert r.status_code == 200
    assert calls == [1]  # rescanned


def test_claim_pending_by_store_url_is_subdomain_insensitive(client):
    """Tagging an album whose store_url matches a pending purchase claims it out of
    the list (the purchase is now on disk) — matched by slug, so a label vs artist
    subdomain for the same edition still resolves. Unrelated pending is untouched.
    This is the belt-and-braces behind confirming a mis-tag."""
    from harmonist import pending_downloads as pd
    from harmonist.web.main import _claim_pending_by_store_url

    pd.replace_all(
        [
            _pp(60, url="https://label.bandcamp.com/album/x"),
            _pp(61, url="https://other.bandcamp.com/album/z"),
        ]
    )
    # Same slug, DIFFERENT subdomain (artist page vs label page).
    _claim_pending_by_store_url("https://artist.bandcamp.com/album/x")
    assert pd.get(60) is None
    assert pd.get(61) is not None
    # No-op on empty / non-matching input.
    _claim_pending_by_store_url(None)
    _claim_pending_by_store_url("https://nope.bandcamp.com/album/nomatch")
    assert pd.get(61) is not None


def test_reconcile_suggestions_unambiguous_both_directions(cfg):
    """Case-B pairing by approximate artist+title (store_url join failed): the
    potential download points at the on-disk album, and the surrender album points
    back at the potential download — both unambiguous."""
    from harmonist import scanner
    from harmonist.web.main import _reconcile_suggestions

    cfg.paths.music_dir.mkdir(parents=True, exist_ok=True)
    d = _needs_sync_album(cfg, "Reunion", "rel-r")
    albums = scanner.scan(cfg.paths.music_dir)
    a = next(x for x in albums if x.path == d)
    # Same name, DIFFERENT slug (the realistic case B — a re-slug / rip).
    pend = [_pp(70, band=a.artist, title=a.title, url="https://x.bandcamp.com/album/reunited")]
    ps, ss = _reconcile_suggestions(albums, pend, cfg.paths.music_dir)
    assert ps[70]["id"] == a.id  # P→A
    assert ss[a.id].item_id == 70  # A→P


def test_reconcile_suggestions_ambiguous_is_skipped(cfg):
    """Two potential downloads with the same normalized name → the album can't tell
    which it is → no surrender suggestion (safety via uniqueness). Each pending
    still uniquely points at the one album."""
    from harmonist import scanner
    from harmonist.web.main import _reconcile_suggestions

    cfg.paths.music_dir.mkdir(parents=True, exist_ok=True)
    d = _needs_sync_album(cfg, "Reunion", "rel-r")
    albums = scanner.scan(cfg.paths.music_dir)
    a = next(x for x in albums if x.path == d)
    pend = [
        _pp(70, band=a.artist, title=a.title, url="https://x.bandcamp.com/album/a"),
        _pp(71, band=a.artist, title=a.title, url="https://y.bandcamp.com/album/b"),
    ]
    ps, ss = _reconcile_suggestions(albums, pend, cfg.paths.music_dir)
    assert a.id not in ss  # ambiguous on the album side
    assert ps[70]["id"] == a.id  # each pending still uniquely points at the album
    assert ps[71]["id"] == a.id


def test_reconcile_suggestions_skips_already_linked_album(cfg):
    """An album already linked to a purchase (item_id set) is not a case-B
    candidate — it's done, so no suggestion points at it."""
    from harmonist import scanner
    from harmonist.web.main import _reconcile_suggestions

    cfg.paths.music_dir.mkdir(parents=True, exist_ok=True)
    d = _make_album(cfg, "Linked")
    sc.write(
        d,
        Sidecar(
            mb_release_id="rel-l",
            store_url="https://x.bandcamp.com/album/linked",
            bandcamp=BandcampInfo(item_id=123),
        ),
    )
    albums = scanner.scan(cfg.paths.music_dir)
    a = next(x for x in albums if x.path == d)
    pend = [_pp(80, band=a.artist, title=a.title, url="https://x.bandcamp.com/album/other")]
    ps, _ = _reconcile_suggestions(albums, pend, cfg.paths.music_dir)
    assert 80 not in ps


def test_reconcile_suggestions_includes_library_albums(cfg):
    """A COMPLETE (Library) album that's unlinked (no item_id) is still a match
    candidate — the "36" case: a real Bandcamp purchase whose files lost the
    Bandcamp comment, so it landed in Library. The potential-download match must
    reach into the library, not just the inbox, to recover it."""
    from harmonist import scanner
    from harmonist.models import AlbumState
    from harmonist.web.main import _reconcile_suggestions

    cfg.paths.music_dir.mkdir(parents=True, exist_ok=True)
    # Tagged (files carry the MBID), no Bandcamp store_url, no item_id → COMPLETE.
    d = _make_album(cfg, "Black Soma", mbid="rel-bs")
    sc.write(d, Sidecar(mb_release_id="rel-bs"))
    albums = scanner.scan(cfg.paths.music_dir)
    a = next(x for x in albums if x.path == d)
    assert a.state == AlbumState.COMPLETE  # it's in the Library, not the inbox
    pend = [_pp(81, band=a.artist, title=a.title, url="https://3six.bandcamp.com/album/black-soma")]
    ps, _ = _reconcile_suggestions(albums, pend, cfg.paths.music_dir)
    assert ps[81]["id"] == a.id  # the library album is offered as the match


def _surrendered_album(cfg, name: str, mbid: str) -> Path:
    """An album that surrendered: files tagged with `mbid`, but the sidecar's
    mb_release_id was cleared and an `unmatched_purchase` candidate stashed →
    NEEDS_MBID (the 'No Bandcamp purchase found' card)."""
    d = _make_album(cfg, name, mbid=mbid)
    sc.write(
        d,
        Sidecar(
            store_url=f"https://x.bandcamp.com/album/{name.lower().replace(' ', '-')}",
            mb_release_id=None,
            mb_match_candidate=MatchCandidate(
                mb_release_id=mbid,
                confidence="exact",
                file_count=1,
                track_count=1,
                unmatched_purchase=True,
            ),
        ),
    )
    return d


def test_reconcile_suggestions_includes_surrendered_albums(cfg):
    """A surrendered album (NEEDS_MBID + unmatched_purchase) gets the reverse
    suggestion too, not just NEEDS_SYNC — that's where unmatched albums live."""
    from harmonist import scanner
    from harmonist.models import AlbumState
    from harmonist.web.main import _reconcile_suggestions

    cfg.paths.music_dir.mkdir(parents=True, exist_ok=True)
    d = _surrendered_album(cfg, "Surrendered One", "rel-s1")
    albums = scanner.scan(cfg.paths.music_dir)
    a = next(x for x in albums if x.path == d)
    assert a.state == AlbumState.NEEDS_MBID
    pend = [_pp(60, band=a.artist, title=a.title, url="https://x.bandcamp.com/album/other")]
    _, ss = _reconcile_suggestions(albums, pend, cfg.paths.music_dir)
    assert ss[a.id].item_id == 60


def test_norm_artist_collapses_collab_and_feat_variants():
    """A collaboration written differently across sources normalizes the same:
    Bandcamp 'A / B' vs tagged 'A and B'/'A & B', and 'A feat. B' vs 'A'."""
    from harmonist.web.main import _norm_artist

    assert _norm_artist("Jah Wobble and Bill Laswell") == _norm_artist("Jah Wobble / Bill Laswell")
    assert _norm_artist("A & B") == _norm_artist("A / B")
    assert _norm_artist("Bonobo feat. Andreya Triana") == _norm_artist("Bonobo")
    assert _norm_artist("Above and Beyond") != _norm_artist("Tycho")  # not everything collapses


def test_reconcile_suggestions_matches_collab_artist_separator(cfg):
    """The Radioaxiom case: on-disk artist 'A and B' (tags) vs purchase band 'A / B'
    (Bandcamp) still matches — the artist normalization collapses the separator."""
    from harmonist import scanner
    from harmonist.web.main import _reconcile_suggestions

    cfg.paths.music_dir.mkdir(parents=True, exist_ok=True)
    d = _make_album(cfg, "Radioaxiom", mbid="rel-ra")
    f = next(d.glob("*.m4a"))
    audio = MP4(f)
    audio[ATOM_ARTIST] = ["Jah Wobble and Bill Laswell"]
    audio[ATOM_ALBUM] = ["Radioaxiom"]
    audio.save()
    sc.write(
        d,
        Sidecar(
            store_url="http://billlaswell.bandcamp.com",
            mb_release_id=None,
            mb_match_candidate=MatchCandidate(
                mb_release_id="rel-ra",
                confidence="exact",
                file_count=1,
                track_count=1,
                unmatched_purchase=True,
            ),
        ),
    )
    albums = scanner.scan(cfg.paths.music_dir)
    a = next(x for x in albums if x.path == d)
    assert a.artist == "Jah Wobble and Bill Laswell"
    pend = [
        _pp(70, band="Jah Wobble / Bill Laswell", title="Radioaxiom", url="https://x.net/album/ra")
    ]
    ps, ss = _reconcile_suggestions(albums, pend, cfg.paths.music_dir)
    assert 70 in ps  # forward: pending → album
    assert a.id in ss  # reverse: surrendered album → pending


def test_link_surrendered_album_un_surrenders_to_complete(client, cfg):
    """Linking a potential download to a surrendered album restores its release id
    from the candidate (files are still tagged) → Library, and links the item_id."""
    from harmonist import pending_downloads as pd
    from harmonist import sidecar as scmod
    from harmonist.models import AlbumState
    from harmonist.scanner import scan

    d = _surrendered_album(cfg, "Undo Surrender", "rel-us")
    pd.replace_all([_pp(61, url="https://x.bandcamp.com/album/undo")])
    r = client.post("/pending/61/match", data={"album_id": _id_for(cfg, d)})
    assert r.status_code == 200
    loaded = scmod.read(d)
    assert loaded.bandcamp.item_id == 61
    assert loaded.mb_release_id == "rel-us"  # restored from the candidate
    assert loaded.mb_match_candidate is None  # surrender cleared
    assert next(x for x in scan(cfg.paths.music_dir) if x.path == d).state == AlbumState.COMPLETE


def test_surrender_card_shows_reverse_suggestion(client, cfg):
    """A surrendered album with a matching potential download LEADS with the Link
    (positive framing), not the contradictory 'No Bandcamp purchase found' panel."""
    from harmonist import pending_downloads as pd
    from harmonist import scanner

    d = _surrendered_album(cfg, "Reverse Vis", "rel-rv")
    a = next(x for x in scanner.scan(cfg.paths.music_dir) if x.path == d)
    pd.replace_all([_pp(62, band=a.artist, title=a.title, url="https://x.bandcamp.com/album/rv2")])
    body = client.get("/tasks").text
    assert "Looks like a purchase you own" in body
    assert "/pending/62/match" in body
    # With a match, the contradictory "No Bandcamp purchase found" panel is dropped.
    card = body[body.index('id="task-' + a.id) :]
    assert "No Bandcamp purchase found" not in card[: card.index("</section>")]


def test_case_b_suggestions_render_on_both_cards(client, cfg):
    """The potential-download card shows 'Already in your library?' with a Link-these
    button to the matched album, and the surrender card shows the reverse — both
    posting to the same /pending/{id}/match link."""
    from harmonist import pending_downloads as pd
    from harmonist import scanner

    d = _needs_sync_album(cfg, "Reunion", "rel-r")
    a = next(x for x in scanner.scan(cfg.paths.music_dir) if x.path == d)
    pd.replace_all(
        [_pp(90, band=a.artist, title=a.title, url="https://x.bandcamp.com/album/reunited")]
    )
    body = client.get("/tasks").text
    assert "Already in your library?" in body  # potential-download card suggestion
    assert "You may have already downloaded this purchase:" in body  # surrender card
    assert f'"album_id": "{a.id}"' in body  # both Links target this album
    assert "/pending/90/match" in body


def test_pending_section_open_by_default_with_summary(client):
    """The potential-downloads list is an OPEN <details> with a one-line summary —
    these purchases need reviewing, so we don't hide them behind an expander. The
    user can still collapse it (open state is preserved across swaps)."""
    from harmonist import pending_downloads as pd

    pd.replace_all([_pp(50), _pp(51)])
    body = client.get("/tasks").text
    assert 'id="group-PENDING"' in body
    # Open by default.
    assert '<details id="group-PENDING" open' in body
    assert "you own but" in body  # the summary line
    assert "· 2" in body  # the count


# ---------- Sync popover (link-only override + max-downloads) ----------


def test_sync_popover_forces_link_only_and_persists_cap(client, monkeypatch):
    """The popover posts from_popover + link_only + max_downloads → the runner gets
    an explicit link-only override, and the per-sync cap persists to config."""
    runner = client.app.state.sync_runner
    monkeypatch.setattr(runner, "start", lambda: None)  # don't spawn a real sync
    r = client.post(
        "/sync", data={"from_popover": "true", "link_only": "true", "max_downloads": "0"}
    )
    assert r.status_code == 200
    assert runner.link_only_override is True
    assert client.app.state.cfg.bandcamp.max_downloads_per_sync == 0


def test_sync_popover_unchecked_forces_download_mode(client, monkeypatch):
    """from_popover with the link-only checkbox absent → explicit False (not auto)."""
    runner = client.app.state.sync_runner
    monkeypatch.setattr(runner, "start", lambda: None)
    r = client.post("/sync", data={"from_popover": "true", "max_downloads": "5"})
    assert r.status_code == 200
    assert runner.link_only_override is False


def test_plain_sync_button_leaves_link_only_auto(client, monkeypatch):
    """The plain Sync button (no popover fields) → None → auto-detect link-only."""
    runner = client.app.state.sync_runner
    monkeypatch.setattr(runner, "start", lambda: None)
    r = client.post("/sync")
    assert r.status_code == 200
    assert runner.link_only_override is None


def test_sync_popover_exposes_ids_for_live_default_js(client, cfg):
    """The popover renders the ids the /status poll binds to — the JS derives the
    link-only default from the live count and greys max-downloads. Guards the
    contract so a template rename can't silently break the wiring."""
    body = "# Netscape HTTP Cookie File\n.bandcamp.com\tTRUE\t/\tFALSE\t0\tident\tabc\n"
    client.post("/bandcamp/cookies", data={"cookies_text": body})
    home = client.get("/").text
    assert 'id="sync-control"' in home
    assert 'id="sync-link-only"' in home
    assert 'id="sync-max-row"' in home
    assert 'id="sync-max-downloads"' in home


# ---------- ignores.txt: write region + the "Won't download" surface (#19/#77) ----------

_IGNORES_WITH_SEPARATOR = """\
# This file allows you to exclude releases from downloads.
1111  # Manually / Added
# =========================================================
2222  # Already / Downloaded
3333  # Also Already / Downloaded
"""


def test_ignore_is_written_above_the_separator(client, cfg):
    """#77: a declined purchase belongs in the USER region. Appending put it in
    bandcampsync's auto-managed region, where it's indistinguishable from
    'already downloaded' and gets clobbered when bandcampsync rewrites the file."""
    from harmonist.web.main import _append_ignore

    cfg.ignores_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.ignores_file.write_text(_IGNORES_WITH_SEPARATOR)

    _append_ignore(cfg.ignores_file, 4444, "New / Ignore")

    lines = cfg.ignores_file.read_text().splitlines()
    sep = next(i for i, ln in enumerate(lines) if "=========" in ln)
    ours = next(i for i, ln in enumerate(lines) if ln.startswith("4444"))
    assert ours < sep, "user ignore must land above the separator"
    # The auto-managed region is untouched.
    assert lines[sep + 1 :] == ["2222  # Already / Downloaded", "3333  # Also Already / Downloaded"]


def test_ignore_with_no_separator_still_appends(client, cfg):
    """A fresh ignores.txt has no separator yet (bandcampsync adds it at the end
    on first run), so everything present is user data — append is correct."""
    from harmonist.web.main import _append_ignore

    cfg.ignores_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.ignores_file.write_text("# header only\n")
    _append_ignore(cfg.ignores_file, 55, "Solo / Ignore")
    assert "55  # Solo / Ignore" in cfg.ignores_file.read_text()


def test_ignored_surface_lists_only_user_entries(client, cfg):
    """#19: the surface must NOT list the auto-managed region — that's every
    album already downloaded, i.e. the user's whole collection."""
    from harmonist.web.main import _read_user_ignores

    cfg.ignores_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.ignores_file.write_text(_IGNORES_WITH_SEPARATOR)

    got = _read_user_ignores(cfg.ignores_file)
    assert [i["item_id"] for i in got] == [1111]
    assert got[0]["label"] == "Manually / Added"

    # Lives on Settings, not the Inbox: a declined purchase is resolved, not
    # pending, so the Inbox stays a pure work queue.
    settings = client.get("/settings").text
    assert "Manually / Added" in settings
    assert "Already / Downloaded" not in settings
    assert "Manually / Added" not in client.get("/tasks").text


def test_restore_removes_only_from_the_user_region(client, cfg):
    """Restoring must never drop an id from the auto-managed region — that would
    make the next sync re-download an album the user already has."""
    from harmonist.web.main import _read_user_ignores

    cfg.ignores_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.ignores_file.write_text(_IGNORES_WITH_SEPARATOR)

    r = client.post("/ignored/1111/restore")
    assert r.status_code == 200
    assert _read_user_ignores(cfg.ignores_file) == []
    # ...and the downloaded ids survive.
    text = cfg.ignores_file.read_text()
    assert "2222" in text and "3333" in text

    # Restoring an id that only exists in the auto region is a no-op.
    client.post("/ignored/2222/restore")
    assert "2222" in cfg.ignores_file.read_text()


def test_demo_mode_sandboxes_the_ignores_file(monkeypatch, tmp_path):
    """#77: demo shares the real CONFIG dir, so 'Don't download' on a demo
    purchase was appending fixture item_ids to the user's genuine ignores.txt."""
    from harmonist import config as config_mod

    monkeypatch.setenv("HARMONIST_DEMO_MODE", "1")
    monkeypatch.setenv("HARMONIST_CONFIG_DIR", str(tmp_path / "config"))
    cfg = config_mod.load()

    assert cfg.demo_mode
    assert cfg.paths.config_dir not in cfg.ignores_file.parents
    assert cfg.ignores_file.parent == cfg.paths.music_dir  # the demo sandbox


def test_ignore_is_not_written_twice(client, cfg):
    """#79: re-deciding the same purchase must not append a duplicate. Ours was
    the only unguarded writer — bandcampsync checks is_ignored() before its own
    adds — which is how a real file grew 55 lines for 31 unique ids."""
    from harmonist.web.main import _append_ignore, _read_user_ignores

    cfg.ignores_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.ignores_file.write_text("# header\n")

    _append_ignore(cfg.ignores_file, 4242, "Band — Album")
    _append_ignore(cfg.ignores_file, 4242, "Band — Album")
    _append_ignore(cfg.ignores_file, 4242, "Band — Album (different label)")

    assert cfg.ignores_file.read_text().count("4242") == 1
    assert len(_read_user_ignores(cfg.ignores_file)) == 1


def test_ignore_already_in_the_auto_region_is_a_noop(client, cfg):
    """An id bandcampsync already recorded is honoured wherever it sits, so
    re-adding it to the user region would change nothing and just inflate the
    file — and would wrongly offer an album you HAVE for un-ignoring."""
    from harmonist.web.main import _append_ignore, _read_user_ignores

    cfg.ignores_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.ignores_file.write_text(_IGNORES_WITH_SEPARATOR)

    _append_ignore(cfg.ignores_file, 2222, "Already / Downloaded")

    assert cfg.ignores_file.read_text().count("2222") == 1
    # Not promoted into the user region either.
    assert [i["item_id"] for i in _read_user_ignores(cfg.ignores_file)] == [1111]


# ---------- action correlation coverage (#84) ----------
#
# The mechanism is unit-tested in test_activity_store.py. The risk here is
# COVERAGE: an audit.record() outside any scope silently gets NULL, so each
# entry point that opens a scope needs one test proving it actually does.


def test_request_scope_correlates_the_action_with_its_audit_records(client, cfg, monkeypatch):
    """A state-changing request opens one scope, so the activity entry the
    handler writes and the sidecar audit beneath it share an action_id — even
    though audit.record() is called from deep inside sidecar.write(), which knows
    nothing about requests. This is what the contextvar buys."""
    from harmonist import activity, activity_store

    # Confirm, not re-tag: _audit_sidecar_change only records when a load-bearing
    # actually moves, so re-tagging to the SAME mbid audits nothing and would
    # make this pass vacuously. Confirming moves temp_uid -> mbid.
    d = _make_album(cfg, "Correlated")
    sc.write(
        d,
        Sidecar(
            store_url="https://x.bandcamp.com/album/corr",
            mb_match_candidate=MatchCandidate(
                mb_release_id="rel-corr", confidence="exact", file_count=1, track_count=1
            ),
        ),
    )
    aid = _id_for(cfg, d)
    monkeypatch.setattr(
        "harmonist.mb_lookup.fetch_release", lambda mbid: _release_for_match(mbid, n_tracks=1)
    )
    monkeypatch.setattr("harmonist.cover_art.ensure_cover", lambda *a, **kw: None)
    activity.clear()

    assert client.post(f"/confirm/{aid}").status_code == 200

    entry = next(e for e in activity.recent(10) if "Tagged" in e.message)
    assert entry.action_id, "the request opened no action scope"
    detail = activity_store.audit_by_action([entry.action_id])
    assert detail.get(entry.action_id), "no audit records correlated to the action"


def test_get_requests_open_no_action_scope(client, cfg):
    """GETs mutate nothing, so they shouldn't mint an id — an action id that
    correlates nothing is noise in the column."""
    from harmonist import activity, activity_store

    _make_album(cfg, "Idle")
    activity.clear()
    client.get("/tasks")
    activity.record("written during no action")

    assert activity_store.recent()[0].action_id is None


def test_reconcile_gives_each_album_its_own_action(cfg):
    """Per album, not per run: reconcile writes a sidecar and an activity entry
    for each, so each must be separately revertible."""
    from harmonist import activity
    from harmonist.web.reconcile_runner import reconcile_pending_orphans

    _make_album(cfg, "OrphanOne", mbid="rel-one")
    _make_album(cfg, "OrphanTwo", mbid="rel-two")
    activity.clear()

    reconcile_pending_orphans(cfg.paths.music_dir, fetch_urls=lambda mbid: [])

    ids = {e.action_id for e in activity.recent(20) if "New →" in e.message}
    assert len(ids) == 2, "the two albums should not share one action id"
    assert None not in ids


# ---------- A broken history store must not render as an empty one (#104) ----------


@pytest.fixture
def broken_store(monkeypatch):
    """Every activity_store operation fails, as a locked or corrupt DB would."""
    import sqlite3

    from harmonist import activity_store

    def boom():
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(activity_store, "_ensure", boom)


def test_activity_feed_says_unavailable_rather_than_empty(client, broken_store):
    """The feed is re-polled every 2s, so rendering a store failure as "No
    activity yet" is how a broken store goes unnoticed for weeks on a NAS."""
    r = client.get("/activity")
    assert r.status_code == 200  # a polling panel must not 500 on a loop
    assert "Activity unavailable" in r.text
    assert "No activity yet" not in r.text


def test_album_page_says_history_unavailable_rather_than_nothing_recorded(client, cfg):
    """The sharpest form of the lie: the page would state as fact that Harmonist
    has never touched the album, at the moment the user asks what it did."""
    d = _make_album(cfg, "BrokenHistory")
    sc.write(d, sc.Sidecar(store_url="https://x.bandcamp.com/album/y"))
    album_id = sc.album_id_for(d)
    assert album_id

    # Control first: with a working store the page renders real history (the
    # sidecar write above audited itself) and no unavailable notice.
    ok = client.get(f"/album/{album_id}")
    assert ok.status_code == 200
    assert "sidecar.create" in ok.text
    assert "History unavailable" not in ok.text

    from harmonist import activity_store

    def boom(*a, **k):
        raise activity_store.StoreUnavailableError("nope")

    with mock.patch.object(activity_store, "album_history", boom):
        r = client.get(f"/album/{album_id}")
    assert r.status_code == 200  # tracklist and actions don't need the store
    assert "History unavailable" in r.text
    assert "Nothing recorded for this album yet" not in r.text
    assert "sidecar.create" not in r.text  # no half-history presented as whole


def test_album_page_history_offers_show_details_checked_by_default(client, cfg):
    """Checked by default, unlike the feed: a single album's history is often
    mostly audit rows, and an adopted album may have nothing else at all — so
    defaulting it off would show an empty history for exactly the albums this
    page exists to explain (#123)."""
    d = _make_album(cfg, "HistoryToggle")
    sc.write(d, sc.Sidecar(store_url="https://x.bandcamp.com/album/y"))
    album_id = sc.album_id_for(d)

    body = client.get(f"/album/{album_id}").text
    assert 'type="checkbox" class="cursor-pointer" checked' in body
    assert "Show details" in body
    # Audit rows are marked for the CSS toggle, and both counts are rendered so
    # the heading can follow it without JS.
    assert "album-history__audit" in body
    assert "album-history__count-full" in body
    assert "album-history__count-brief" in body


def test_unresolvable_link_is_503_not_404_when_the_store_is_broken(client, cfg, broken_store):
    """resolve_alias returning None means "never superseded", which sends
    _find_album to a 404. One DB hiccup must not tell the user an album that is
    sitting on disk does not exist."""
    r = client.get("/album/some-id-that-is-not-in-the-snapshot")
    assert r.status_code == 503
    assert "history store is unavailable" in r.text


def test_a_genuine_miss_is_still_404(client, cfg):
    """Control for the test above — a working store must still 404 an id that
    really isn't there, or the 503 would just be masking every miss."""
    r = client.get("/album/some-id-that-is-not-in-the-snapshot")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Accepting an incomplete album as finished (#196)
# ---------------------------------------------------------------------------


def _accept(client, album_id, accept=True):
    """POST the acceptance the way the checkbox does (#227): a ticked box sends
    `accept=true`, and an unticked one sends nothing at all — that absence is
    what takes the acceptance back."""
    return client.post(
        f"/library/{album_id}/tracks-unavailable", data={"accept": "true"} if accept else {}
    )


def _is_checked(page, marker):
    """Whether the one `<input type=checkbox …>` on `page` whose attributes
    mention `marker` is ticked — which is what says how the setting stands, where
    the label beside it reads the same either way.

    Attribute VALUES are blanked before looking for `checked`: this box carries an
    `hx-on::after-request` handler that mentions `this.checked`, and matching that
    would make every such assertion pass for free.
    """
    tags = [t for t in re.findall(r"<input\b[^>]*>", page) if marker in t]
    assert len(tags) == 1, f"expected one checkbox mentioning {marker!r}, found {len(tags)}"
    return bool(re.search(r"\bchecked\b", re.sub(r'"[^"]*"', '""', tags[0])))


def test_accepting_an_incomplete_album_takes_it_out_of_the_filter(client, cfg):
    """The Pink Floyd case: only the stereo mixes were ever ripped off the
    Blu-ray, so the missing tracks are not a defect anyone can act on."""
    d = _make_incomplete_album(cfg, "Blu", mbid="rel-blu", tagged_at=datetime.now(UTC))
    assert "Blu" in client.get("/library?filter=incomplete").text

    r = _accept(client, _id_for(cfg, d))

    assert r.status_code == 200
    assert sc.read(d).tracks_unavailable is True
    assert "Blu" not in client.get("/library?filter=incomplete").text


def test_an_accepted_album_stays_incomplete_and_still_says_so(client, cfg):
    """It is not a lie about the files — the album really is short. Only the
    "what should I fix" list stops carrying it."""
    from harmonist import scanner
    from harmonist.models import AlbumState

    d = _make_incomplete_album(cfg, "Blu", mbid="rel-blu", tagged_at=datetime.now(UTC))
    _accept(client, _id_for(cfg, d))

    album = next(a for a in scanner.scan(cfg.paths.music_dir) if a.path == d)
    assert album.state == AlbumState.INCOMPLETE
    assert "Blu" in client.get("/library").text, "still in the Library"


def test_accepting_is_reversible_from_the_ui(client, cfg):
    d = _make_incomplete_album(cfg, "Blu", mbid="rel-blu", tagged_at=datetime.now(UTC))
    aid = _id_for(cfg, d)
    _accept(client, aid)

    r = _accept(client, aid, accept=False)

    assert r.status_code == 200
    assert sc.read(d).tracks_unavailable is False
    assert "Blu" in client.get("/library?filter=incomplete").text


def test_accepting_is_idempotent(client, cfg):
    """Reconcile-style double submits must not stack audit rows."""
    d = _make_incomplete_album(cfg, "Blu", mbid="rel-blu", tagged_at=datetime.now(UTC))
    aid = _id_for(cfg, d)

    first = _accept(client, aid)
    second = _accept(client, aid)

    assert (first.status_code, second.status_code) == (200, 200)
    assert "No change" in second.text
    assert sc.read(d).tracks_unavailable is True


def test_a_complete_album_cannot_be_accepted_as_complete(client, cfg):
    """The claim is about missing tracks. There are none, so there is nothing to
    record — and allowing it would turn the field into a general "ignore this"."""
    d = _make_tagged_album(cfg, "Whole", mbid="rel-whole", tagged_at=datetime.now(UTC))

    r = _accept(client, _id_for(cfg, d))

    assert r.status_code == 400
    assert sc.read(d).tracks_unavailable is False


def test_accepting_touches_no_tags(client, cfg):
    """It records a decision, not a change to the album."""
    from harmonist import formats

    d = _make_incomplete_album(cfg, "Blu", mbid="rel-blu", tagged_at=datetime.now(UTC))
    path = next(p for p in d.iterdir() if formats.is_supported(p))
    before = formats.read_owned(path)

    _accept(client, _id_for(cfg, d))

    assert formats.read_owned(path) == before


def test_accepting_is_audited(client, cfg, tmp_path):
    """It reclassifies what the user is shown as needing attention, so it is a
    load-bearing sidecar change and has to be reconstructable."""
    from harmonist import activity_store

    d = _make_incomplete_album(cfg, "Blu", mbid="rel-blu", tagged_at=datetime.now(UTC))
    _accept(client, _id_for(cfg, d))

    rows = [e.message for e in activity_store.recent(50)]
    assert any("tracks_unavailable=False->True" in m for m in rows)


def test_the_album_page_offers_the_acceptance_only_when_incomplete(client, cfg):
    incomplete = _make_incomplete_album(cfg, "Blu", mbid="rel-blu", tagged_at=datetime.now(UTC))
    whole = _make_tagged_album(cfg, "Whole", mbid="rel-whole", tagged_at=datetime.now(UTC))

    page = client.get(f"/album/{_id_for(cfg, incomplete)}").text
    assert "Don't warn me about this" in page
    assert not _is_checked(page, "tracks-unavailable"), "not accepted yet"

    assert "warn me about this" not in client.get(f"/album/{_id_for(cfg, whole)}").text


def test_the_album_page_offers_the_way_back_once_accepted(client, cfg):
    """The escape hatch (review gate item 5) is now unticking the box, so what
    has to be on the page is a CHECKED checkbox — the label stays put either
    way, and by itself proves nothing."""
    d = _make_incomplete_album(cfg, "Blu", mbid="rel-blu", tagged_at=datetime.now(UTC))
    aid = _id_for(cfg, d)
    _accept(client, aid)

    page = client.get(f"/album/{aid}").text
    assert _is_checked(page, "tracks-unavailable"), "escape hatch, per review gate item 5"


# ---------------------------------------------------------------------------
# The completeness badge on the album's own page (#227)
# ---------------------------------------------------------------------------


def test_the_album_page_states_how_much_of_the_release_is_on_disk(client, cfg):
    """The tile carried this and the page didn't, so the one view that had room
    to explain the badge was the one that omitted it."""
    d = _make_incomplete_album(cfg, "Blu", mbid="rel-blu", tagged_at=datetime.now(UTC))

    page = client.get(f"/album/{_id_for(cfg, d)}").text

    assert "1 of 4 tracks on disk" in page


def test_a_complete_album_gets_no_completeness_badge(client, cfg):
    """Nothing to explain — and the tile says nothing about one either, so a
    blob here would be a second vocabulary for the same fact."""
    d = _make_tagged_album(cfg, "Whole", mbid="rel-whole", tagged_at=datetime.now(UTC))

    page = client.get(f"/album/{_id_for(cfg, d)}").text

    assert "tracks on disk" not in page
    assert "album-completeness" not in page


@pytest.mark.parametrize(
    "absent,disc_total,expected",
    [
        ({2}, 2, "Disc 2 of 2 is missing"),
        ({2}, None, "Disc 2 is missing"),  # the files don't agree on a disc count
        ({2, 3}, 3, "Discs 2 and 3 are missing"),  # no "of 3": a fraction of a fraction
        # Disc numbers whose SET iteration order isn't ascending — {9, 2} yields
        # 9 first — so this fails if the sort is dropped. {2, 3, 4} would not:
        # small ints hash to themselves, so that set already iterates in order
        # and would pass either way.
        ({9, 2, 5}, 10, "Discs 2, 5 and 9 are missing"),
    ],
)
def test_the_missing_disc_phrase(absent, disc_total, expected):
    """The badge's text for an album short by whole media (#245). Unit-tested
    because the plural forms are fiddly and each would otherwise need its own
    multi-disc fixture to reach through a page."""
    from harmonist.web.main import _missing_discs

    assert _missing_discs(frozenset(absent), disc_total) == expected


def test_an_album_missing_a_whole_disc_names_the_disc(client, cfg):
    """`expected_track_count` is None when a medium has no files at all — nothing
    on disk records how long it was — so the badge can't count. It says which
    disc is missing instead, which is the question "a disc of this release" left
    the reader to take to the tracklist (#245).

    Also the only spelling of the shortfall that avoids putting "disc" and "on
    disk" in one sentence, four words apart."""
    from test.helpers import write_track_totals

    d = _make_tagged_album(cfg, "Boxed", mbid="rel-boxed", tagged_at=datetime.now(UTC))
    write_track_totals(d, track_total=1, disc_num=1, disc_total=2)

    page = client.get(f"/album/{_id_for(cfg, d)}").text

    assert "Disc 2 of 2 is missing" in page
    assert "of 1 tracks on disk" not in page, "no denominator invented from the disc that IS here"


def test_accepting_demotes_the_badge_instead_of_removing_it(client, cfg):
    """The album really is short — that stays true once accepted. It stops being
    a warning (amber) and becomes a statement (neutral), the same distinction the
    tile makes."""
    d = _make_incomplete_album(cfg, "Blu", mbid="rel-blu", tagged_at=datetime.now(UTC))
    aid = _id_for(cfg, d)
    assert "amber" in _badge(client.get(f"/album/{aid}").text)

    _accept(client, aid)

    badge = _badge(client.get(f"/album/{aid}").text)
    assert "1 of 4 tracks on disk" in badge, "still says what is missing"
    assert "amber" not in badge, "no longer a defect to fix"


def _badge(page):
    """The completeness blob and everything up to the end of its badge text."""
    start = page.index("album-completeness")
    return page[start : page.index("</span>", start)]


def test_ticking_the_box_re_states_the_badge_in_the_same_response(client, cfg):
    """The control sits beside the statement it changes, so the response has to
    carry that statement back — otherwise the page keeps showing a warning about
    an album the user just accepted, until they reload it."""
    d = _make_incomplete_album(cfg, "Blu", mbid="rel-blu", tagged_at=datetime.now(UTC))
    aid = _id_for(cfg, d)

    body = _accept(client, aid).text

    assert 'hx-swap-oob="true"' in body
    assert f'id="album-completeness-{aid}"' in body
    assert "amber" not in body, "swapped back demoted, matching what was just written"


def test_taking_the_acceptance_back_re_states_the_badge_too(client, cfg):
    d = _make_incomplete_album(cfg, "Blu", mbid="rel-blu", tagged_at=datetime.now(UTC))
    aid = _id_for(cfg, d)
    _accept(client, aid)

    body = _accept(client, aid, accept=False).text

    assert 'hx-swap-oob="true"' in body
    assert "amber" in body, "back to being something to fix"


def test_a_double_submit_still_re_states_the_badge(client, cfg):
    """The no-op path returns early to keep the audit log clean, and the badge
    has to come back with it — a checkbox that raced itself must end up showing
    what is on disk, not what the last click typed."""
    d = _make_incomplete_album(cfg, "Blu", mbid="rel-blu", tagged_at=datetime.now(UTC))
    aid = _id_for(cfg, d)
    _accept(client, aid)

    body = _accept(client, aid).text

    assert "No change" in body
    assert f'id="album-completeness-{aid}"' in body
    assert "amber" not in body


# ---------------------------------------------------------------------------
# A release MusicBrainz has deleted (#194)
# ---------------------------------------------------------------------------


def _gone(mbid):
    from harmonist import mb_lookup

    raise mb_lookup.ReleaseGoneError(f"MusicBrainz no longer has release {mbid}")


def test_album_page_explains_a_deleted_release_instead_of_showing_a_404(client, cfg, monkeypatch):
    """Real case: Voices From the Lake — II names `7b598009-…`, which MB has
    deleted. The page used to render "Couldn't fetch from MusicBrainz: HTTP
    Error 404: Not Found", which reads as something to retry forever."""
    d = _make_tagged_album(cfg, "Gone", mbid="rel-gone", tagged_at=datetime.now(UTC))
    monkeypatch.setattr("harmonist.mb_lookup.fetch_release", _gone)

    r = client.get(f"/library/{_id_for(cfg, d)}/compare")

    assert r.status_code == 200
    assert "It was deleted there" in r.text, "the page says what happened"
    assert "No comparison" in r.text, "and each panel says it wasn't able to compare"
    assert "404" not in r.text


def test_a_deleted_release_raises_a_banner_with_a_way_out(client, cfg, monkeypatch):
    """A sentence pointing at another control is not a remedy. The banner asks,
    with the button that acts, and it is swapped OUT OF BAND to the top of the
    page — the page could not know at render time (#210)."""
    d = _make_tagged_album(cfg, "Gone", mbid="rel-gone", tagged_at=datetime.now(UTC))
    monkeypatch.setattr("harmonist.mb_lookup.fetch_release", _gone)
    aid = _id_for(cfg, d)

    r = client.get(f"/library/{aid}/compare")

    assert f'id="album-alert-{aid}" hx-swap-oob="true"' in r.text
    assert "This release is gone from MusicBrainz" in r.text
    assert f'hx-post="/library/{aid}/rematch"' in r.text, "the button acts"
    assert "hx-confirm" in r.text, "moving it out of the Library is not for a mis-click"


def test_a_deleted_release_disables_retag(client, cfg, monkeypatch):
    """Nothing behind Re-tag can succeed, so it must not look available."""
    d = _make_tagged_album(cfg, "Gone", mbid="rel-gone", tagged_at=datetime.now(UTC))
    monkeypatch.setattr("harmonist.mb_lookup.fetch_release", _gone)
    aid = _id_for(cfg, d)

    r = client.get(f"/library/{aid}/compare")

    swapped = r.text.split(f'id="retag-btn-{aid}"')[1].split(">")[0]
    assert "hx-swap-oob" in swapped and "disabled" in swapped
    assert "hx-post" not in swapped, "cannot fire at all, rather than firing and failing"


def _gone_album_with_tags(cfg, name="Gone", *, mbid="rel-gone"):
    """A deleted-release album whose files carry tags worth showing."""
    d = _make_tagged_album(cfg, name, mbid=mbid, tagged_at=datetime.now(UTC))
    audio = MP4(d / "01 Track.m4a")
    audio[ATOM_ALBUM] = ["Silent Season Sampler"]
    audio[ATOM_TITLE] = ["Drifting Under Ice"]
    audio.save()
    return d


def test_a_deleted_release_still_shows_the_files_own_tags(client, cfg, monkeypatch):
    """#228: the banner says the files "still carry its tags", and the page then
    declined to show them. Those tags are the evidence for finding the
    replacement release — dropping them sent the user to Picard."""
    d = _gone_album_with_tags(cfg)
    monkeypatch.setattr("harmonist.mb_lookup.fetch_release", _gone)

    r = client.get(f"/library/{_id_for(cfg, d)}/compare")

    assert "Silent Season Sampler" in r.text, "the album's own tags, not an empty note"
    assert "No comparison — showing your own tags and" in r.text
    assert "read " not in r.text, "nothing was read from MusicBrainz — don't claim a timestamp"


def test_a_deleted_release_settles_the_tracks_placeholder(client, cfg, monkeypatch):
    """The original #228 symptom: Tracks sat on "Checking tracks against
    MusicBrainz…" forever, so the one panel still claiming to be working
    contradicted the three that had already answered."""
    d = _gone_album_with_tags(cfg)
    monkeypatch.setattr("harmonist.mb_lookup.fetch_release", _gone)

    r = client.get(f"/library/{_id_for(cfg, d)}/compare")

    assert 'id="album-tracks" hx-swap-oob="true"' in r.text, "the fourth destination"
    assert "Drifting Under Ice" in r.text, "the file-derived tracklist"
    assert "Not in MusicBrainz" not in r.text, "MB was never asked — that's not a finding"


def test_a_failed_mb_fetch_settles_the_tracks_placeholder_too(client, cfg, monkeypatch):
    """Same stuck placeholder, different cause. It must NOT degrade to the
    disk-only view: a fetch that failed may succeed on a reload, and a view that
    quietly drops MusicBrainz would look authoritative."""

    def _boom(mbid):
        raise mb_lookup.MBError("upstream said no")

    d = _gone_album_with_tags(cfg)
    monkeypatch.setattr("harmonist.mb_lookup.fetch_release", _boom)

    r = client.get(f"/library/{_id_for(cfg, d)}/compare")

    assert 'id="album-tracks" hx-swap-oob="true"' in r.text
    assert r.text.count("Couldn't fetch from MusicBrainz") == 2, "both halves settle"
    assert "reload" in r.text, "retryable, unlike a deleted release"
    assert "album-alert-" not in r.text, "no deleted-release banner for a transient failure"


def test_a_failed_mb_fetch_escapes_the_upstream_message(client, cfg, monkeypatch):
    """An MBError wraps musicbrainzngs text, which originates off-box and must
    not reach the DOM as markup."""

    def _boom(mbid):
        raise mb_lookup.MBError("<script>alert(1)</script>")

    d = _make_tagged_album(cfg, "Boom", mbid="rel-boom", tagged_at=datetime.now(UTC))
    monkeypatch.setattr("harmonist.mb_lookup.fetch_release", _boom)

    r = client.get(f"/library/{_id_for(cfg, d)}/compare")

    assert "<script>" not in r.text
    assert "&lt;script&gt;" in r.text


def test_a_healthy_album_gets_no_banner_and_a_live_retag(client, cfg, monkeypatch):
    """The control: the OOB swaps must not fire for an album that is fine."""
    d = _make_tagged_album(cfg, "Fine", mbid="rel-fine", tagged_at=datetime.now(UTC))
    monkeypatch.setattr(
        "harmonist.mb_lookup.fetch_release", lambda m: _release_for_match(m, n_tracks=1)
    )
    aid = _id_for(cfg, d)

    r = client.get(f"/library/{aid}/compare")

    assert "album-alert-" not in r.text
    assert "gone from MusicBrainz" not in r.text
    assert 'hx-post="/retag/' in client.get(f"/album/{aid}").text, "still live on the page"


def test_album_page_still_says_try_again_for_a_transient_failure(client, cfg, monkeypatch):
    """The control. An outage must not tell the user their release was deleted."""
    from harmonist import mb_lookup

    d = _make_tagged_album(cfg, "Flaky", mbid="rel-flaky", tagged_at=datetime.now(UTC))

    def unwell(mbid):
        raise mb_lookup.MBError("MB request failed: HTTP Error 503")

    monkeypatch.setattr("harmonist.mb_lookup.fetch_release", unwell)

    r = client.get(f"/library/{_id_for(cfg, d)}/compare")

    assert "Couldn't fetch from MusicBrainz" in r.text
    assert "no longer has this release" not in r.text


def test_retag_reports_a_deleted_release_as_gone_not_as_a_failure(client, cfg, monkeypatch):
    d = _make_tagged_album(cfg, "Gone", mbid="rel-gone", tagged_at=datetime.now(UTC))
    monkeypatch.setattr("harmonist.mb_lookup.fetch_release", _gone)

    r = client.post(f"/retag/{_id_for(cfg, d)}")

    assert r.status_code == 200
    assert "gone from MusicBrainz" in r.text
    assert "Re-tag failed" not in r.text
    assert "Wrong MusicBrainz match" in r.text


def test_retag_still_reports_a_real_failure_as_a_failure(client, cfg, monkeypatch):
    d = _make_tagged_album(cfg, "Boom", mbid="rel-boom", tagged_at=datetime.now(UTC))

    def boom(mbid):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr("harmonist.mb_lookup.fetch_release", boom)

    r = client.post(f"/retag/{_id_for(cfg, d)}")

    assert "Re-tag failed" in r.text


def test_a_deleted_release_leaves_the_album_alone(client, cfg, monkeypatch):
    """No automatic demotion. MusicBrainz being briefly odd must not dump albums
    out of the Library, and the user's tags are not rewritten behind them —
    the remedy is offered, not applied."""
    d = _make_tagged_album(cfg, "Gone", mbid="rel-gone", tagged_at=datetime.now(UTC))
    monkeypatch.setattr("harmonist.mb_lookup.fetch_release", _gone)
    before = sc.read(d)

    client.get(f"/library/{_id_for(cfg, d)}/compare")
    client.post(f"/retag/{_id_for(cfg, d)}")

    assert sc.read(d) == before


# ---------- Re-download from Bandcamp (#132) ----------


@pytest.fixture
def quiet_sync(client):
    """Make the sync a no-op that still moves the runner through its states, so a
    re-download can start one without reaching the network (or the missing
    cookies file). Returns the runner for assertions about whether it ran."""
    runner = client.app.state.sync_runner
    runner._runner_fn = lambda: None
    return runner


def _ignores_with(cfg, *, user: Sequence[int] = (), auto: Sequence[int] = ()) -> Path:
    """An ignores.txt with ids in each of its two regions. bandcampsync appends
    a downloaded album's id BELOW the separator, which is the half the ordinary
    Restore refuses to touch — so a re-download has to reach into it."""
    cfg.paths.config_dir.mkdir(parents=True, exist_ok=True)
    lines = [f"{i}  # declined\n" for i in user]
    lines.append("# ==========================================================\n")
    lines += [f"{i}  # downloaded\n" for i in auto]
    cfg.ignores_file.write_text("".join(lines), encoding="utf-8")
    return cfg.ignores_file


def test_redownload_archives_the_album_and_takes_it_off_disk(client, cfg, quiet_sync):
    d = _make_tagged_album(
        cfg, "Upgrade Me", mbid="rel-rd-1", tagged_at=datetime.now(UTC), item_id=5150
    )
    album_id = _id_for(cfg, d)

    r = client.post(f"/library/{album_id}/redownload")

    assert r.status_code == 200
    assert not d.exists()
    zips = list(cfg.paths.music_dir.glob("*.zip"))
    assert len(zips) == 1
    with zipfile.ZipFile(zips[0]) as zf:
        assert any(n.endswith("01 Track.m4a") for n in zf.namelist())
        assert any(n.endswith(".harmonist.json") for n in zf.namelist())


def test_redownload_unignores_the_purchase_in_bandcampsyncs_own_region(client, cfg, quiet_sync):
    """The one thing that actually makes the next sync re-fetch it. The id sits
    below the separator because bandcampsync put it there on the first download,
    and `_remove_user_ignore` deliberately won't go there."""
    d = _make_tagged_album(cfg, "Reget", mbid="rel-rd-2", tagged_at=datetime.now(UTC), item_id=777)
    ign = _ignores_with(cfg, user=[111], auto=[777, 888])

    client.post(f"/library/{_id_for(cfg, d)}/redownload")

    remaining = ign.read_text()
    assert "777" not in remaining
    assert "888" in remaining  # somebody else's download, left alone
    assert "111" in remaining  # a genuine "don't download", left alone


def test_redownload_approves_the_download_so_a_link_only_sync_still_fetches_it(
    client, cfg, quiet_sync
):
    """While any album is Needs Link the sync runs link-only and downloads
    nothing — except items the user explicitly asked for, which is this."""
    from harmonist import pending_downloads

    d = _make_tagged_album(
        cfg, "Approved", mbid="rel-rd-3", tagged_at=datetime.now(UTC), item_id=4242
    )
    try:
        client.post(f"/library/{_id_for(cfg, d)}/redownload")
        assert pending_downloads.is_approved(4242)
    finally:
        pending_downloads.reset()


def test_redownload_clears_the_collection_checkpoint(client, cfg, quiet_sync):
    """An incremental sync starts at the checkpoint, so an old purchase would
    never be re-paged and the replacement would never arrive."""
    d = _make_tagged_album(
        cfg, "Old Purchase", mbid="rel-rd-4", tagged_at=datetime.now(UTC), item_id=99
    )
    state_file = cfg.paths.music_dir / ".bandcampsync-state.json"
    state_file.write_text('{"token": "2020-01-01"}')

    client.post(f"/library/{_id_for(cfg, d)}/redownload")

    assert not state_file.exists()


def test_redownload_starts_a_sync(client, cfg, quiet_sync):
    started = []
    quiet_sync._runner_fn = lambda: started.append(True)
    d = _make_tagged_album(
        cfg, "Fetch Me", mbid="rel-rd-5", tagged_at=datetime.now(UTC), item_id=31337
    )

    client.post(f"/library/{_id_for(cfg, d)}/redownload")

    quiet_sync._thread.join(timeout=5)
    assert started == [True]


def test_redownload_refuses_while_a_sync_is_running_and_keeps_the_album(client, cfg):
    """bandcampsync snapshots ignores.txt at startup and rewrites it wholesale,
    so an edit made mid-sync is silently discarded — the album would be deleted
    and then skipped."""
    d = _make_tagged_album(cfg, "Busy", mbid="rel-rd-6", tagged_at=datetime.now(UTC), item_id=606)
    client.app.state.sync_runner._status.state = "running"

    r = client.post(f"/library/{_id_for(cfg, d)}/redownload")

    assert r.status_code == 409
    assert d.exists()
    assert not list(cfg.paths.music_dir.glob("*.zip"))


def test_redownload_refuses_an_album_with_no_single_purchase_id(client, cfg, quiet_sync):
    """`candidate_item_ids` means several purchases share this store URL and a
    tiebreak couldn't pin one. Fetching a guess is exactly what Harmonist
    doesn't do — so the album is not deleted either."""
    d = _make_tagged_album(cfg, "Ambiguous", mbid="rel-rd-7", tagged_at=datetime.now(UTC))
    sc.write(
        d,
        Sidecar(
            store_url="https://x.bandcamp.com/album/ambiguous",
            bandcamp=BandcampInfo(item_id=None, candidate_item_ids=[1, 2]),
            mb_release_id="rel-rd-7",
            tagged_at=datetime.now(UTC),
        ),
    )

    r = client.post(f"/library/{_id_for(cfg, d)}/redownload")

    assert r.status_code == 400
    assert d.exists()


def test_redownload_records_the_archive_against_the_albums_history(client, cfg, quiet_sync):
    """The album's id is its MBID, and the replacement gets tagged with the same
    one — so the archive, the delete and the eventual re-tag all land on one
    history, which is the point of auditing this at all."""
    from harmonist import activity_store

    d = _make_tagged_album(
        cfg, "Traceable", mbid="rel-rd-8", tagged_at=datetime.now(UTC), item_id=8080
    )

    client.post(f"/library/{_id_for(cfg, d)}/redownload")

    messages = [e.message for e in activity_store.album_history("rel-rd-8")]
    assert any(m.startswith("redownload.archive") for m in messages)
    assert any(m.startswith("redownload.delete") for m in messages)
    assert any("Archived to" in m for m in messages)
    # Recorded relative to the library root (#98), so the log names the album
    # rather than repeating a container path on every line. The pruned parent is
    # the one that can get this wrong — see test_archive.py, where a symlinked
    # root can actually tell the two apart.
    prune = next(m for m in messages if m.startswith("redownload.prune"))
    assert prune == "redownload.prune dir=Artist"


def test_redownload_of_an_adopted_album_does_not_cry_wolf_about_the_ignores(
    client, cfg, quiet_sync
):
    """An album adopted from an existing library was never downloaded by
    bandcampsync, so it has no line in ignores.txt and nothing was blocking the
    re-download. Reporting that as a problem would fire on the common case."""
    from harmonist import activity

    d = _make_tagged_album(
        cfg, "Adopted", mbid="rel-rd-12", tagged_at=datetime.now(UTC), item_id=1212
    )
    assert not cfg.ignores_file.exists()

    client.post(f"/library/{_id_for(cfg, d)}/redownload")

    assert not [e for e in activity.recent() if e.level == Level.WARNING]


def test_redownload_warns_when_the_ignore_could_not_be_removed(
    client, cfg, quiet_sync, no_cover_fetch, monkeypatch
):
    """The opposite case, and the one worth interrupting for: the id is still
    ignored, so bandcampsync skips the purchase and the replacement the user was
    promised never arrives."""
    from harmonist import activity

    d = _make_tagged_album(
        cfg, "Stuck", mbid="rel-rd-13", tagged_at=datetime.now(UTC), item_id=1313
    )
    _ignores_with(cfg, auto=[1313])
    monkeypatch.setattr(Path, "write_text", mock.Mock(side_effect=OSError("read-only filesystem")))

    client.post(f"/library/{_id_for(cfg, d)}/redownload")

    warnings = [e.message for e in activity.recent() if e.level == Level.WARNING]
    assert any("ignores" in m for m in warnings)


def test_an_awaited_redownload_is_shown_in_the_inbox_until_it_lands(client, cfg, quiet_sync):
    """Between the archive and the download the album has no directory, so it is
    in no state and no group — without this card it just vanishes moments after
    the user clicked something that deleted their files."""
    from harmonist import library_index, redownloads

    d = _make_tagged_album(
        cfg, "Interim", mbid="rel-rd-9", tagged_at=datetime.now(UTC), item_id=9090
    )
    try:
        client.post(f"/library/{_id_for(cfg, d)}/redownload")

        r = client.get("/tasks")
        assert "Re-downloading" in r.text
        assert "Interim" in r.text
        # The zip is the escape hatch out of this state, so the card has to name it.
        assert "(archived " in r.text

        # ...and it clears by derivation once the purchase is back on disk — no
        # completion callback anything could forget to fire.
        library_index.upsert(
            cfg.paths.music_dir / "Artist" / "Interim",
            Sidecar(mb_release_id="rel-rd-9", bandcamp=BandcampInfo(item_id=9090)),
        )
        assert "Interim" not in client.get("/tasks").text
    finally:
        redownloads.reset()
        library_index.clear()


def test_album_page_offers_redownload_only_for_a_linked_bandcamp_purchase(client, cfg):
    linked = _make_tagged_album(
        cfg, "Linked", mbid="rel-rd-10", tagged_at=datetime.now(UTC), item_id=1010
    )
    manual = _make_tagged_album(cfg, "Manual", mbid="rel-rd-11", tagged_at=datetime.now(UTC))

    assert "/redownload" in client.get(f"/album/{_id_for(cfg, linked)}").text
    # A CD rip has no purchase to re-download, so the control isn't offered —
    # the same string IS present on the album above, under different conditions.
    assert "/redownload" not in client.get(f"/album/{_id_for(cfg, manual)}").text


def test_redownloading_twice_archives_once(client, cfg, quiet_sync):
    """Transitions are idempotent (§3). The album is off disk after the first
    call, so the second finds nothing to act on rather than writing a second
    archive of an album that no longer exists — which would be an empty zip and
    a second sync for a purchase already queued."""
    d = _make_tagged_album(
        cfg, "Twice", mbid="rel-rd-14", tagged_at=datetime.now(UTC), item_id=1414
    )
    album_id = _id_for(cfg, d)

    first = client.post(f"/library/{album_id}/redownload")
    second = client.post(f"/library/{album_id}/redownload")

    assert first.status_code == 200
    assert second.status_code == 404
    assert len(list(cfg.paths.music_dir.glob("*.zip"))) == 1


def _replay_the_download(
    cfg, name: str, *, item_id: int, mbid: str | None, tracks: int = 1
) -> Path:
    """What the sync does when the replacement lands: recreate the album dir and
    write a sidecar for the purchase (no MBID — a fresh download isn't tagged
    yet), then, if MusicBrainz resolved it, tag it. Both go through
    `sidecar.write`, which is what records the identity alias."""
    from harmonist.bandcamp_hook import write_sidecar_for_item

    d = _make_album(cfg, name)
    for i in range(2, tracks + 1):  # _make_album writes track 1
        shutil.copy(SINE_M4A, d / f"{i:02d} Track.m4a")
    slug = name.lower().replace(" ", "-")
    item = mock.Mock(
        item_id=item_id,
        band_name="Artist",
        item_title=name,
        _data={"band_id": 1, "item_url": f"https://x.bandcamp.com/album/{slug}"},
    )
    write_sidecar_for_item(item, d)
    activity.record("Downloaded", album_id=sc.album_id_for(d), album_label=name)
    if mbid:
        existing = sc.read(d)
        assert existing is not None
        sc.write(d, replace(existing, mb_release_id=mbid, tagged_at=datetime.now(UTC)))
        activity.record("Auto-tagged from MusicBrainz after sync", album_id=sc.album_id_for(d))
    return d


def test_history_survives_the_round_trip_when_the_replacement_tags_the_same_release(
    client, cfg, quiet_sync
):
    """The claim the audit trail rests on: the archive is recorded under the
    album's MBID, the fresh download starts life under a path-derived temp_uid,
    and tagging it back to the same release aliases the two — so the album's page
    shows what happened to it, not just what happened since."""
    d = _make_tagged_album(
        cfg, "Round Trip", mbid="rel-rt-1", tagged_at=datetime.now(UTC), item_id=7001
    )
    client.post(f"/library/{_id_for(cfg, d)}/redownload")
    shutil.rmtree(d, ignore_errors=True)

    _replay_the_download(cfg, "Round Trip", item_id=7001, mbid="rel-rt-1")

    messages = [e.message for e in activity_store.album_history("rel-rt-1")]
    assert any(m.startswith("redownload.archive") for m in messages)  # before
    assert "Downloaded" in messages  # during, under the temp_uid
    assert "Auto-tagged from MusicBrainz after sync" in messages  # after


def test_history_splits_when_the_replacement_tags_a_different_release(client, cfg, quiet_sync):
    """The limit of the above, stated rather than discovered. If the replacement
    ends up on a DIFFERENT release, its id is not the archived album's id and no
    alias joins them — the archive stays on the old id's history, reachable from
    the Activity feed but not from the new album's page.

    Carrying the release through the round trip is what makes this rare rather
    than routine: it is now only reachable when the carried release was lost (a
    restart) or refused (the files don't fit it), and in the second case the user
    is looking at the suggestion card when it happens. So this pins a residual
    case, not the common path — which is the test above."""
    d = _make_tagged_album(
        cfg, "Split", mbid="rel-split-old", tagged_at=datetime.now(UTC), item_id=7002
    )
    client.post(f"/library/{_id_for(cfg, d)}/redownload")
    shutil.rmtree(d, ignore_errors=True)

    _replay_the_download(cfg, "Split", item_id=7002, mbid="rel-split-new")

    new_side = [e.message for e in activity_store.album_history("rel-split-new")]
    assert "Downloaded" in new_side
    assert not any(m.startswith("redownload.archive") for m in new_side)
    # The record is not lost, just filed under the album it was made about.
    old_side = [e.message for e in activity_store.album_history("rel-split-old")]
    assert any(m.startswith("redownload.archive") for m in old_side)


def test_a_replacement_musicbrainz_cannot_match_lands_in_the_inbox_not_the_library(
    client, cfg, quiet_sync
):
    """A re-download can end up UNTAGGED. The album was COMPLETE before; if the
    store URL no longer resolves, the replacement has a sidecar and a store_url
    but no release, which is NEEDS_MBID — in the inbox with the usual tools, not
    silently absent and not silently wrong."""
    from harmonist import scanner
    from harmonist.models import AlbumState

    d = _make_tagged_album(
        cfg, "Unmatched", mbid="rel-um-1", tagged_at=datetime.now(UTC), item_id=7003
    )
    client.post(f"/library/{_id_for(cfg, d)}/redownload")
    shutil.rmtree(d, ignore_errors=True)

    back = _replay_the_download(cfg, "Unmatched", item_id=7003, mbid=None)

    states = {a.path: a.state for a in scanner.scan(cfg.paths.music_dir)}
    assert states[back] == AlbumState.NEEDS_MBID


@pytest.fixture
def no_cover_fetch(monkeypatch):
    """Tagging embeds cover art, which would otherwise reach the real Cover Art
    Archive over the network for every release id these tests invent."""
    monkeypatch.setattr("harmonist.cover_art.ensure_cover", lambda *a, **kw: None)


def _resolve_the_download(cfg, album_dir: Path) -> str:
    """Run the post-download hook the sync runs, against a just-landed album."""
    from harmonist.tagger import PicardCompatibleTagger
    from harmonist.web.main import _resolve_by_store_url

    return _resolve_by_store_url(album_dir, cfg, PicardCompatibleTagger())


def test_the_replacement_is_tagged_as_the_release_it_was_archived_as(
    client, cfg, quiet_sync, no_cover_fetch, monkeypatch
):
    """Re-downloading says the FILES are wrong, not the match — so the release
    the user already accepted is carried through rather than re-resolved from the
    store URL, which is a question they didn't ask."""
    d = _make_tagged_album(
        cfg, "Carried", mbid="rel-carry", tagged_at=datetime.now(UTC), item_id=8001
    )
    client.post(f"/library/{_id_for(cfg, d)}/redownload")
    shutil.rmtree(d, ignore_errors=True)

    monkeypatch.setattr(
        mb_lookup, "fetch_release", lambda m: _release_with_rg(m, "rg-carry", n_tracks=1)
    )
    # The store URL now resolves somewhere ELSE — a re-match would take it, and
    # carrying the release is exactly what stops that.
    monkeypatch.setattr(mb_lookup, "lookup_by_bandcamp_url", lambda _u: ["rel-something-else"])
    back = _replay_the_download(cfg, "Carried", item_id=8001, mbid=None)

    assert _resolve_the_download(cfg, back) == "tagged"
    assert sc.read(back).mb_release_id == "rel-carry"


def test_carrying_the_release_keeps_the_album_out_of_the_inbox(
    client, cfg, quiet_sync, no_cover_fetch, monkeypatch
):
    """The point of it, in the terms the user sees: an album that was in the
    Library before the re-download is in the Library after it."""
    from harmonist import scanner
    from harmonist.models import AlbumState

    d = _make_tagged_album(
        cfg, "Stays Done", mbid="rel-stays", tagged_at=datetime.now(UTC), item_id=8002
    )
    client.post(f"/library/{_id_for(cfg, d)}/redownload")
    shutil.rmtree(d, ignore_errors=True)

    monkeypatch.setattr(
        mb_lookup, "fetch_release", lambda m: _release_with_rg(m, "rg-stays", n_tracks=1)
    )
    back = _replay_the_download(cfg, "Stays Done", item_id=8002, mbid=None)
    _resolve_the_download(cfg, back)

    states = {a.path: a.state for a in scanner.scan(cfg.paths.music_dir)}
    assert states[back] == AlbumState.COMPLETE


def test_a_replacement_that_does_not_fit_the_release_falls_back_to_a_suggestion(
    client, cfg, quiet_sync, no_cover_fetch, monkeypatch
):
    """The tagger's count guard is not skipped, and must not be: whether a
    tagging is representable is not something the user's assertion can settle.
    MusicBrainz may not have caught up with a release the artist has grown, so
    the new files can outnumber its tracklist — which is an error in both tagger
    modes (§13.3). The user meets the ordinary side-by-side instead."""
    d = _make_tagged_album(
        cfg, "Outgrown", mbid="rel-outgrown", tagged_at=datetime.now(UTC), item_id=8003
    )
    client.post(f"/library/{_id_for(cfg, d)}/redownload")
    shutil.rmtree(d, ignore_errors=True)

    # MB still lists one track; the replacement arrives with three.
    monkeypatch.setattr(
        mb_lookup, "fetch_release", lambda m: _release_with_rg(m, "rg-out", n_tracks=1)
    )
    monkeypatch.setattr(mb_lookup, "lookup_by_bandcamp_url", lambda _u: ["rel-outgrown"])
    back = _replay_the_download(cfg, "Outgrown", item_id=8003, mbid=None, tracks=3)

    assert _resolve_the_download(cfg, back) != "tagged"
    after = sc.read(back)
    assert after.mb_release_id is None  # not tagged behind the user's back
    assert after.mb_match_candidate is not None  # ...but the suggestion is there


def test_a_release_deleted_from_musicbrainz_falls_back_rather_than_failing(
    client, cfg, quiet_sync, no_cover_fetch, monkeypatch
):
    """The carried release can go away between the archive and the download —
    #194's case. Falling through to the store-URL path is how the album still
    gets a chance at a release that exists."""
    d = _make_tagged_album(
        cfg, "Vanished", mbid="rel-vanished", tagged_at=datetime.now(UTC), item_id=8004
    )
    client.post(f"/library/{_id_for(cfg, d)}/redownload")
    shutil.rmtree(d, ignore_errors=True)

    monkeypatch.setattr(mb_lookup, "fetch_release", _gone)
    monkeypatch.setattr(mb_lookup, "lookup_by_bandcamp_url", lambda _u: [])
    back = _replay_the_download(cfg, "Vanished", item_id=8004, mbid=None)

    assert _resolve_the_download(cfg, back) == "no_match"
    assert sc.read(back).mb_release_id is None


def test_the_carried_release_survives_an_inbox_poll_between_download_and_tagging(
    client, cfg, quiet_sync, no_cover_fetch, monkeypatch
):
    """The inbox polls every couple of seconds during a sync, and the download
    writes the sidecar — putting the item_id into library_index — BEFORE the
    tagging runs. A poll landing in that gap must not take the carried release
    away with the card."""
    from harmonist import redownloads

    d = _make_tagged_album(
        cfg, "Polled", mbid="rel-polled", tagged_at=datetime.now(UTC), item_id=8005
    )
    client.post(f"/library/{_id_for(cfg, d)}/redownload")
    shutil.rmtree(d, ignore_errors=True)

    monkeypatch.setattr(
        mb_lookup, "fetch_release", lambda m: _release_with_rg(m, "rg-polled", n_tracks=1)
    )
    monkeypatch.setattr(mb_lookup, "lookup_by_bandcamp_url", lambda _u: ["rel-elsewhere"])
    back = _replay_the_download(cfg, "Polled", item_id=8005, mbid=None)

    client.get("/tasks")  # the poll: the album is back, so the card goes
    assert redownloads.count() == 0
    _resolve_the_download(cfg, back)

    assert sc.read(back).mb_release_id == "rel-polled"


def test_a_replacement_that_arrives_short_is_not_quietly_tagged_incomplete(
    client, cfg, quiet_sync, no_cover_fetch, monkeypatch
):
    """The other side of the count guard. A download that arrives SHORT could be
    forced through in the tagger's incomplete mode, and that would be the wrong
    favour: the album was complete before, so a short replacement is a bad
    download, not a newly-incomplete album. Carrying the release must not turn
    into carrying an excuse for it."""
    d = _make_tagged_album(
        cfg, "Short", mbid="rel-short", tagged_at=datetime.now(UTC), item_id=8006
    )
    client.post(f"/library/{_id_for(cfg, d)}/redownload")
    shutil.rmtree(d, ignore_errors=True)

    # MB says three tracks; only one file comes back.
    monkeypatch.setattr(
        mb_lookup, "fetch_release", lambda m: _release_with_rg(m, "rg-short", n_tracks=3)
    )
    monkeypatch.setattr(mb_lookup, "lookup_by_bandcamp_url", lambda _u: ["rel-short"])
    back = _replay_the_download(cfg, "Short", item_id=8006, mbid=None, tracks=1)

    assert _resolve_the_download(cfg, back) != "tagged"
    assert sc.read(back).mb_release_id is None


def test_the_carried_release_is_let_go_of_once_it_has_been_used(
    client, cfg, quiet_sync, no_cover_fetch, monkeypatch
):
    """`prune` deliberately leaves the carried release alone, so consuming it at
    the tagging is the ONLY thing that clears it on the happy path. Without that
    every successful re-download would leak one until restart."""
    from harmonist import redownloads

    d = _make_tagged_album(
        cfg, "Let Go", mbid="rel-letgo", tagged_at=datetime.now(UTC), item_id=8007
    )
    client.post(f"/library/{_id_for(cfg, d)}/redownload")
    shutil.rmtree(d, ignore_errors=True)

    monkeypatch.setattr(
        mb_lookup, "fetch_release", lambda m: _release_with_rg(m, "rg-letgo", n_tracks=1)
    )
    back = _replay_the_download(cfg, "Let Go", item_id=8007, mbid=None)
    assert redownloads.take_match(8007) is not None  # still held before the tagging
    redownloads.add(
        redownloads.PendingRedownload(
            item_id=8007,
            artist="Artist",
            title="Let Go",
            url="",
            archive_name="x.zip",
            requested_at=datetime.now(UTC),
        ),
        match=redownloads.CarriedMatch(mb_release_id="rel-letgo"),
    )

    _resolve_the_download(cfg, back)

    assert redownloads.take_match(8007) is None


def test_an_incomplete_album_that_comes_back_just_as_short_keeps_its_tags(
    client, cfg, quiet_sync, no_cover_fetch, monkeypatch
):
    """The issue's own case: an album short of its release because the artist
    added tracks after the purchase, re-downloaded to go and get them. Bandcamp
    may not have them either — and then the replacement arrives just as short.

    Being short is the state it was ALREADY in, and the user accepted this
    release knowing that, so it stays tagged and INCOMPLETE. Refusing here would
    charge them their tags for a shortfall that predates the button, which is
    what the demo library did before the incompleteness was carried too."""
    from harmonist import scanner
    from harmonist.models import AlbumState
    from test.helpers import write_track_totals

    d = _make_incomplete_album(
        cfg, "Still Short", mbid="rel-short-still", tagged_at=datetime.now(UTC), expected=4
    )
    tagged = sc.read(d)
    assert tagged is not None
    sc.write(
        d,
        replace(
            tagged,
            bandcamp=BandcampInfo(item_id=8008),
            store_url="https://x.bandcamp.com/album/still-short",
        ),
    )
    assert _state_of(cfg, d) == AlbumState.INCOMPLETE

    client.post(f"/library/{_id_for(cfg, d)}/redownload")
    shutil.rmtree(d, ignore_errors=True)

    monkeypatch.setattr(
        mb_lookup, "fetch_release", lambda m: _release_with_rg(m, "rg-still", n_tracks=4)
    )
    back = _replay_the_download(cfg, "Still Short", item_id=8008, mbid=None, tracks=1)

    assert _resolve_the_download(cfg, back) == "tagged"
    assert sc.read(back).mb_release_id == "rel-short-still"
    write_track_totals(back, track_total=4)
    states = {a.path: a.state for a in scanner.scan(cfg.paths.music_dir)}
    assert states[back] == AlbumState.INCOMPLETE  # short, and honest about it


def test_the_album_page_does_not_report_a_disambiguated_title_as_a_difference(cfg):
    """The wiring, which is the half a unit test can't see.

    `compare.album_fields` takes the accepted spelling; something has to hand it
    one, and forgetting to would leave the whole of #283 inert with every unit
    test still green. This drives the real call the album page makes.
    """
    from harmonist.tagger import tag_album
    from harmonist.web.main import _album_comparison

    release = {
        "id": "rel-283",
        "title": "Obreel",
        "disambiguation": "expanded edition",
        "artist-credit": [{"artist": {"id": "a1", "name": "Artist"}}],
        "release-group": {"id": "rg-1", "primary-type": "Album"},
        "medium-list": [
            {
                "position": "1",
                "format": "Digital Media",
                "track-list": [
                    {"id": "t1", "title": "Track", "recording": {"id": "r1", "title": "Track"}}
                ],
            }
        ],
    }
    d = _make_album(cfg, "Obreel")
    tag_album(d, release)
    audio = MP4(d / "01 Track.m4a")
    audio[ATOM_ALBUM] = ["Obreel (expanded edition)"]  # what Picard's option writes
    audio.save()

    comparison, _ = _album_comparison(d, release)

    assert [f.label for f in comparison.differing] == []
    assert {f.label: f.disk for f in comparison.fields}["Album"] == "Obreel (expanded edition)"
