"""Smoke tests for the FastAPI layer.

Not exhaustive — task 13 owns the comprehensive integration test matrix.
These verify wiring: routes load, scanner integration works, state-dispatched
templates render without crashing for each AlbumState.
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from mutagen.mp4 import MP4

from harmonist import sidecar as sc
from harmonist.config import BandcampConfig, Config, PathsConfig, ServerConfig, TestConfig
from harmonist.models import BandcampInfo, MatchCandidate, Sidecar, TrackComparison
from harmonist.sidecar import CURRENT_SCHEMA_VERSION
from harmonist.tagger import ATOM_ALBUM, ATOM_COMMENT, ATOM_MB_ALBUM_ID
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
            schema_version=CURRENT_SCHEMA_VERSION,
            store_url="https://x.bandcamp.com/album/y",
            bandcamp=BandcampInfo(item_id=1),
        ),
    )
    # NEEDS_MBID without store_url
    d2 = _make_album(cfg, "Manual")
    sc.write(d2, Sidecar(schema_version=CURRENT_SCHEMA_VERSION))

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
    assert "Needs Sync" not in r.text


def test_tasks_needs_sync_section_advises_sync(client, cfg):
    """The Needs Sync group instructions point the user to click Sync."""
    d = _make_album(cfg, "UB")
    audio = MP4(d / "01 Track.m4a")
    audio[ATOM_MB_ALBUM_ID] = [b"rel-a"]
    audio.save()
    sc.write(
        d,
        Sidecar(
            schema_version=CURRENT_SCHEMA_VERSION,
            store_url="https://x.bandcamp.com/album/y",
            bandcamp=BandcampInfo(item_id=None),
            mb_release_id="rel-a",
            tagged_at=datetime.now(UTC),
        ),
    )
    r = client.get("/tasks")
    assert "Needs Sync" in r.text
    # Instruction explicitly calls out Sync
    assert "Click Sync" in r.text
    # Inline bulk-Sync affordance (the whole group is fixed by one Sync).
    # /tasks is a fragment with no header, so hx-post="/sync" is this button.
    assert 'hx-post="/sync"' in r.text
    assert "Sync to link" in r.text


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
            schema_version=CURRENT_SCHEMA_VERSION,
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
            schema_version=CURRENT_SCHEMA_VERSION,
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
    sc.write(d, Sidecar(schema_version=CURRENT_SCHEMA_VERSION))
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
    assert "Recover store URL" not in r.text  # URL recovery is automatic now


def test_new_card_tagged_orphan_offers_reconcile(client, cfg):
    _make_album(cfg, "Orphan", mbid="rel-xyz")  # MB Album Id atom, no sidecar
    r = client.get("/tasks")
    assert "Reconcile from tags" in r.text


def test_needs_review_card_renders_side_by_side(client, cfg):
    d = _make_album(cfg, "NR Album")
    sc.write(
        d,
        Sidecar(
            schema_version=CURRENT_SCHEMA_VERSION,
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
            schema_version=CURRENT_SCHEMA_VERSION,
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


def test_surrender_card_renders_readonly_with_tools(client, cfg):
    """A surrender candidate (unmatched_purchase) renders read-only: the
    'No Bandcamp purchase found' note + the seed/fix tools, and NO Confirm."""
    d = _make_album(cfg, "Surrendered")
    sc.write(
        d,
        Sidecar(
            schema_version=CURRENT_SCHEMA_VERSION,
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
    r = client.get("/tasks")
    assert "No Bandcamp purchase found" in r.text
    assert "https://musicbrainz.org/release/rel-surr" in r.text
    # Read-only: no Confirm action at all.
    assert "Confirm &amp; Tag" not in r.text
    assert "/confirm/" not in r.text
    # The resolution tools are present (Harmony seed + recheck/fix).
    assert "Open in Harmony" in r.text


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
            schema_version=CURRENT_SCHEMA_VERSION,
            store_url="https://x.bandcamp.com/album/y",
            bandcamp=BandcampInfo(item_id=None),
            mb_release_id="rel-aaa",
            tagged_at=datetime.now(UTC),
        ),
    )
    r = client.get("/tasks")
    assert "Needs Sync" in r.text
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
            schema_version=CURRENT_SCHEMA_VERSION,
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


def test_needs_sync_bulk_button_enabled_when_idle(client, cfg):
    # Sanity counterpart: with no reconcile running the button is not disabled.
    d = _make_album(cfg, "BulkSyncIdle")
    audio = MP4(d / "01 Track.m4a")
    audio[ATOM_MB_ALBUM_ID] = [b"rel-bulk-2"]
    audio.save()
    sc.write(
        d,
        Sidecar(
            schema_version=CURRENT_SCHEMA_VERSION,
            store_url="https://x.bandcamp.com/album/y",
            bandcamp=BandcampInfo(item_id=None),
            mb_release_id="rel-bulk-2",
            tagged_at=datetime.now(UTC),
        ),
    )
    r = client.get("/tasks")
    assert r.status_code == 200
    button = r.text[r.text.index("sync-trigger") : r.text.index("sync-trigger") + 400]
    assert "disabled title=" not in button


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
            schema_version=CURRENT_SCHEMA_VERSION,
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
    assert ">55</span> Needs Sync" in r.text
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
    the Needs Sync card's actions are gone."""
    _needs_sync_album(cfg, "Linkme", "rel-q")
    client.app.state.reconcile_runner._status.state = "running"
    r = client.get("/tasks")
    assert r.status_code == 200
    assert "Sorting your library" in r.text  # the panel
    assert "Mark purchased elsewhere" not in r.text  # ...not the needs-sync card


def test_tasks_needs_sync_card_shown_when_idle(client, cfg):
    """Sanity counterpart: idle → the Needs Sync card renders normally."""
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
            schema_version=CURRENT_SCHEMA_VERSION,
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
        "No Bandcamp purchase matched" in m and "store URL" in m and "next full sync" in m
        for m in msgs
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
            schema_version=CURRENT_SCHEMA_VERSION,
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
            schema_version=CURRENT_SCHEMA_VERSION,
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

    activity.clear()
    _detect_mistags_after_sync(
        cfg,
        syncer,
        browse_rg=browse_rg,
        fetch_release=fetch_release,
    )
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
    assert linked.bandcamp.item_id == 555  # Needs Sync → Library
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
    r = sc.read(d)  # still Needs Sync: no item_id, store_url not adopted
    assert r.bandcamp is None or r.bandcamp.item_id is None
    assert r.store_url == "https://label.bandcamp.com/album/solo"


def test_reject_clears_candidate(client, cfg):
    d = _make_album(cfg, "RC")
    sc.write(
        d,
        Sidecar(
            schema_version=CURRENT_SCHEMA_VERSION,
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
            schema_version=CURRENT_SCHEMA_VERSION,
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
            schema_version=CURRENT_SCHEMA_VERSION,
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
        Sidecar(schema_version=CURRENT_SCHEMA_VERSION, store_url=store_url),
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
    # No nested "search by name" disclosure anymore.
    assert "Or search MusicBrainz by name" not in r.text


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
    from the comment (no MB url-rel lookup) → album lands in Needs Sync."""
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
            schema_version=CURRENT_SCHEMA_VERSION,
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


def test_library_pagination_offset_limit(client, cfg):
    from datetime import datetime, timedelta

    base = datetime.now(UTC)
    for i in range(5):
        _make_tagged_album(
            cfg, f"Album{i}", mbid=f"rel-{i}", tagged_at=base - timedelta(days=i), item_id=i + 1
        )
    r = client.get("/library?offset=0&limit=2")
    assert r.status_code == 200
    # First page has 2 rows
    assert r.text.count('id="lib-') == 2
    # Load more button references offset=2
    assert "offset=2" in r.text
    # The tab-badge total reflects ALL done albums (5), not the 2 rendered —
    # this is the attribute the Library tab count reads.
    assert 'data-total-done="5"' in r.text


def test_library_load_more_button_absent_on_last_page(client, cfg):
    from datetime import datetime

    _make_tagged_album(cfg, "OnlyOne", mbid="rel-1", tagged_at=datetime.now(UTC), item_id=1)
    r = client.get("/library?offset=0&limit=10")
    assert "Load more" not in r.text


def test_library_first_page_includes_header(client, cfg):
    from datetime import datetime

    _make_tagged_album(cfg, "Album", mbid="rel-1", tagged_at=datetime.now(UTC), item_id=1)
    r = client.get("/library?offset=0")
    assert "<h2" in r.text
    assert "Library" in r.text
    assert "Refresh" in r.text


def test_library_second_page_omits_header(client, cfg):
    from datetime import datetime

    _make_tagged_album(cfg, "Album", mbid="rel-1", tagged_at=datetime.now(UTC), item_id=1)
    r = client.get("/library?offset=30&limit=30")
    # No header on offsets > 0 (the load-more button replaces itself)
    assert "<h2" not in r.text


def test_library_empty_state(client):
    r = client.get("/library")
    assert "No fully-tagged albums yet" in r.text


def test_library_row_links_to_musicbrainz(client, cfg):
    from datetime import datetime

    _make_tagged_album(cfg, "Linked", mbid="abc-123", tagged_at=datetime.now(UTC), item_id=42)
    r = client.get("/library")
    assert "musicbrainz.org/release/abc-123" in r.text


def test_library_row_shows_store_url_and_item_id_in_expanded(client, cfg):
    from datetime import datetime

    _make_tagged_album(cfg, "Linked", mbid="abc-123", tagged_at=datetime.now(UTC), item_id=42)
    r = client.get("/library")
    assert "x.bandcamp.com" in r.text
    assert "42" in r.text  # item_id


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


def test_retag_recomputes_count_and_promotes_incomplete_to_complete(client, cfg, monkeypatch):
    """When MB over-counts (e.g. a phantom album-mix track) an album lands as a
    spurious INCOMPLETE. After the user fixes MB upstream, Re-tag from MB
    recomputes track_count_expected from the corrected release and the state
    self-corrects to COMPLETE — no explicit override needed."""
    from harmonist import scanner
    from harmonist.models import AlbumState

    d = _make_album(cfg, "OverCounted", mbid="rel-1")  # 1 file, tagged rel-1
    sc.write(
        d,
        Sidecar(
            schema_version=CURRENT_SCHEMA_VERSION,
            mb_release_id="rel-1",
            track_count_expected=2,  # MB wrongly said 2 → 1 file < 2 → INCOMPLETE
            tagged_at=datetime.now(UTC),
        ),
    )
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

    assert sc.read(d).track_count_expected == 1  # recomputed from corrected MB
    after = next(a for a in scanner.scan(cfg.paths.music_dir) if a.path == d)
    assert after.state == AlbumState.COMPLETE  # self-corrected


def test_retag_400_when_no_mbid_on_sidecar(client, cfg):
    d = _make_album(cfg, "NoMBID")
    sc.write(d, Sidecar(schema_version=CURRENT_SCHEMA_VERSION))
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
            schema_version=CURRENT_SCHEMA_VERSION,
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


def test_new_album_id_is_minted_from_registry(client, cfg):
    """A NEW album (no sidecar) gets a UUID from the in-process registry;
    the same path gets the same id on repeat scans.
    """
    d = _make_album(cfg, "NewOne")
    aid1 = _id_for(cfg, d)
    aid2 = _id_for(cfg, d)
    assert aid1 == aid2
    # No sidecar exists, so the id can't have come from one
    assert not sc.has_sidecar(d)
    # 32 hex chars = uuid4().hex
    assert len(aid1) == 32


def test_sidecar_album_id_matches_temp_uid(client, cfg):
    """A sidecar'd album's id is the sidecar's temp_uid."""
    d = _make_album(cfg, "WithSidecar")
    sc.write(
        d,
        Sidecar(
            schema_version=CURRENT_SCHEMA_VERSION,
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
            schema_version=CURRENT_SCHEMA_VERSION,
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
            schema_version=CURRENT_SCHEMA_VERSION,
            store_url="https://x.bandcamp.com/album/y",
        ),
    )
    aid_before = _id_for(cfg, d)

    new_d = d.parent / "Renamed"
    d.rename(new_d)
    aid_after = _id_for(cfg, new_d)
    assert aid_before == aid_after


def test_route_404_when_id_is_unknown(client):
    r = client.post("/recheck/this-id-doesnt-exist")
    assert r.status_code == 404


# ---------- Confirm as Incomplete (§15.3) ----------


def test_confirm_incomplete_tags_and_persists_expected_count(client, cfg, monkeypatch):
    """POST /confirm/{id}/incomplete tags via the incomplete-mode tagger,
    sets mb_release_id and track_count_expected, and leaves the album in
    INCOMPLETE on next scan.
    """
    d = _make_album(cfg, "ShortAlbum")  # 1 file from the fixture
    # Seed a candidate with file_count=1, track_count=3 (MB says 3, disk has 1)
    sc.write(
        d,
        Sidecar(
            schema_version=CURRENT_SCHEMA_VERSION,
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
    assert loaded.track_count_expected == 3
    assert loaded.mb_match_candidate is None
    assert loaded.tagged_at is not None

    # Scanner now reports INCOMPLETE
    from harmonist import scanner

    a = next(a for a in scanner.scan(cfg.paths.music_dir) if a.path == d)
    from harmonist.models import AlbumState

    assert a.state == AlbumState.INCOMPLETE


def test_confirm_mistag_adopts_owned_store_url(client, cfg, monkeypatch):
    """Confirming a mis-tag re-tags the MBID AND adopts the owned edition's
    purchase URL as store_url — otherwise the album keeps the wrong-edition URL,
    matches no purchase on the next sync, and falls through to surrender."""
    d = _make_album(cfg, "Mistagged")
    sc.write(
        d,
        Sidecar(
            schema_version=CURRENT_SCHEMA_VERSION,
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
            schema_version=CURRENT_SCHEMA_VERSION,
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
    sc.write(d, Sidecar(schema_version=CURRENT_SCHEMA_VERSION))
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
            schema_version=CURRENT_SCHEMA_VERSION,
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
            schema_version=CURRENT_SCHEMA_VERSION,
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
            schema_version=CURRENT_SCHEMA_VERSION,
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
    d_partial = _make_album(cfg, "Partial")
    audio = MP4(d_partial / "01 Track.m4a")
    audio[ATOM_MB_ALBUM_ID] = [b"rel-i"]
    audio.save()
    sc.write(
        d_partial,
        Sidecar(
            schema_version=CURRENT_SCHEMA_VERSION,
            mb_release_id="rel-i",
            tagged_at=datetime.now(UTC),
            track_count_expected=5,
        ),
    )
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
            schema_version=CURRENT_SCHEMA_VERSION,
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

    # The old temp_uid URL no longer resolves
    r_stale = client.post(f"/recheck/{temp_uid}")
    assert r_stale.status_code == 404


# ---------- activity feed ----------


def test_activity_empty_state(client):
    r = client.get("/activity")
    assert r.status_code == 200
    assert "No activity yet" in r.text


def test_activity_lists_recorded_events(client):
    from harmonist import activity

    activity.record("Tagged — Some Album", "info")
    activity.record("Sync failed — boom", "error")
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
    assert live.log_level == "warning"
    # Persisted to harmonist.toml (round-trips on next load)
    toml = (cfg.paths.config_dir / "harmonist.toml").read_text()
    assert "alac" in toml
    assert "max_downloads_per_sync = 12" in toml


def test_settings_save_rejects_invalid_cover_size(client, cfg):
    r = client.post(
        "/settings",
        data={
            "download_format": "flac",
            "max_downloads_per_sync": "5",
            "user_agent": "Harmonist/0.1 ( x@y.z )",
            "cover_art_size": "999",  # not a valid Literal
            "log_level": "info",
        },
    )
    assert r.status_code == 200
    assert "Couldn't save" in r.text
    # nothing persisted
    assert not (cfg.paths.config_dir / "harmonist.toml").exists()


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
    request-time scan. The /scan/status endpoint reports progress."""
    import time as _time

    _make_album(cfg, "BgAlbum")  # present before startup → initial scan finds it
    with TestClient(create_app(cfg), headers={"HX-Request": "true"}) as client:
        status = {}
        for _ in range(200):
            status = client.get("/scan/status").json()
            if status["state"] == "done":
                break
            _time.sleep(0.02)
        assert status["state"] == "done"
        assert status["albums_found"] == 1
        # /tasks serves the snapshot the background scan produced.
        assert "BgAlbum" in client.get("/tasks").text


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


def test_erase_sidecars_removes_only_sidecars(client, cfg):
    from harmonist import sidecar as scmod

    d = _make_album(cfg, "Erasable")
    scmod.write(d, Sidecar(schema_version=CURRENT_SCHEMA_VERSION, mb_release_id="rel-x"))
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
    scmod.write(d, Sidecar(schema_version=CURRENT_SCHEMA_VERSION, mb_release_id="rel-y"))
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
            schema_version=CURRENT_SCHEMA_VERSION,
            mb_release_id="rel-fresh",
            tagged_at=now,
            downloaded_at=now,
        ),
    )
    stale = _make_tagged_album(cfg, "OldDrop", mbid="rel-stale", tagged_at=now)
    sc.write(
        stale,
        Sidecar(
            schema_version=CURRENT_SCHEMA_VERSION,
            mb_release_id="rel-stale",
            tagged_at=now,
            downloaded_at=now - timedelta(days=30),
        ),
    )
    r = client.get("/library")
    assert r.text.count(">New</span>") == 1  # only the fresh download


def test_library_compare_renders_side_by_side(client, cfg, monkeypatch):
    """The on-demand 'verify tagging' view fetches MB + shows a disk-vs-MB table."""
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
    assert "On disk vs MusicBrainz" in r.text
    assert "Track 1" in r.text


def test_library_detail_offers_verify_tagging(client, cfg):
    _make_tagged_album(cfg, "HasVerify", mbid="rel-v2", tagged_at=datetime.now(UTC))
    r = client.get("/library")
    assert "Verify tagging vs MusicBrainz" in r.text


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
            schema_version=CURRENT_SCHEMA_VERSION,
            store_url="https://x.bandcamp.com/album/ambi",
            bandcamp=BandcampInfo(item_id=None, candidate_item_ids=[111, 222]),
            mb_release_id="rel-ambi",
            tagged_at=datetime.now(UTC),
        ),
    )
    r = client.get("/library")
    assert "ambiguous: item #111 or #222" in r.text


def test_about_page_renders(client):
    r = client.get("/about")
    assert r.status_code == 200
    assert "About Harmonist" in r.text
    assert "GPL-3.0-or-later" in r.text
    assert "mutagen" in r.text
    assert "MusicBrainz" in r.text
    assert "github.com/randomphrase/harmonist" in r.text


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


def test_pending_match_panel_renders_search(client):
    from harmonist import pending_downloads as pd

    pd.replace_all([_pp(44, band="Variant", title="Sequential Sleep")])
    r = client.get("/pending/44/match")
    assert r.status_code == 200
    assert "Variant" in r.text
    assert 'name="q"' in r.text  # the library search input


def test_pending_match_link_fills_item_id(client, cfg):
    """Linking a potential download to an on-disk album writes the purchase's
    item_id + store_url onto that album's sidecar, and drops it from pending."""
    from harmonist import pending_downloads as pd
    from harmonist import sidecar as scmod

    d = _make_album(cfg, "Existing", mbid="rel-xyz")
    scmod.write(d, Sidecar(schema_version=CURRENT_SCHEMA_VERSION, mb_release_id="rel-xyz"))
    album_id = _id_for(cfg, d)

    pd.replace_all([_pp(45, url="https://x.bandcamp.com/album/y")])
    r = client.post("/pending/45/match", data={"album_id": album_id})
    assert r.status_code == 200
    assert pd.get(45) is None
    sc = scmod.read(d)
    assert sc.bandcamp is not None
    assert sc.bandcamp.item_id == 45
    assert sc.store_url == "https://x.bandcamp.com/album/y"
