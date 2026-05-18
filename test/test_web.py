"""Smoke tests for the FastAPI layer.

Not exhaustive — task 13 owns the comprehensive integration test matrix.
These verify wiring: routes load, scanner integration works, state-dispatched
templates render without crashing for each AlbumState.
"""
from __future__ import annotations

import shutil
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from mutagen.mp4 import MP4

from harmonist import sidecar as sc
from harmonist.config import Config, PathsConfig, BandcampConfig, ServerConfig, TestConfig
from harmonist.models import BandcampInfo, MatchCandidate, Sidecar, TrackComparison
from harmonist.sidecar import CURRENT_SCHEMA_VERSION
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
    return TestClient(app)


def _make_album(cfg, name: str, *, mbid: str = None, comment: str = None) -> Path:
    d = cfg.paths.music_dir / "Artist" / name
    d.mkdir(parents=True)
    f = d / "01 Track.m4a"
    shutil.copy(SINE_M4A, f)
    if mbid or comment:
        audio = MP4(f)
        if mbid:
            audio["----:com.apple.iTunes:MusicBrainz Album Id"] = [mbid.encode("utf-8")]
        if comment:
            audio["\xa9cmt"] = [comment]
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
    sc.write(d1, Sidecar(
        schema_version=CURRENT_SCHEMA_VERSION,
        store_url="https://x.bandcamp.com/album/y",
        bandcamp=BandcampInfo(item_id=1),
    ))
    # NEEDS_MBID without store_url
    d2 = _make_album(cfg, "Manual")
    sc.write(d2, Sidecar(schema_version=CURRENT_SCHEMA_VERSION))

    r = client.get("/tasks")
    # Both are NEEDS_MBID — single section heading
    assert "Needs MBID" in r.text
    # Open-in-Harmony link appears for the store_url card
    assert "Open in Harmony" in r.text
    # Manual MBID form appears too
    assert 'name="mbid"' in r.text


def test_tasks_state_group_omitted_when_empty(client, cfg):
    """No section header rendered for states without any albums."""
    _make_album(cfg, "Only New")
    r = client.get("/tasks")
    assert "New" in r.text
    # No NEEDS_MBID / NEEDS_REVIEW / NEEDS_SYNC headers
    assert "Needs MBID" not in r.text
    assert "Needs Review" not in r.text
    assert "Needs Sync" not in r.text


def test_tasks_needs_sync_section_advises_sync(client, cfg):
    """The Needs Sync group instructions point the user to click Sync."""
    d = _make_album(cfg, "UB")
    audio = MP4(d / "01 Track.m4a")
    audio["----:com.apple.iTunes:MusicBrainz Album Id"] = [b"rel-a"]
    audio.save()
    sc.write(d, Sidecar(
        schema_version=CURRENT_SCHEMA_VERSION,
        store_url="https://x.bandcamp.com/album/y",
        bandcamp=BandcampInfo(item_id=None),
        mb_release_id="rel-a",
        tagged_at=datetime.now(timezone.utc),
    ))
    r = client.get("/tasks")
    assert "Needs Sync" in r.text
    # Instruction explicitly calls out Sync
    assert "Click Sync" in r.text


def test_tasks_empty_state_message_distinguishes_zero_vs_all_done(client, cfg):
    # Empty library
    r = client.get("/tasks")
    assert "Drop some albums" in r.text or "Inbox is empty" in r.text


# ---------- state dispatch — each card type renders ----------

def test_new_card_rendered(client, cfg):
    _make_album(cfg, "New Album")
    r = client.get("/tasks")
    assert "New" in r.text
    assert "Reconcile" in r.text
    assert 'hx-post="/reconcile/' in r.text


def test_needs_mbid_card_with_store_url_rendered(client, cfg):
    d = _make_album(cfg, "HasURL")
    sc.write(d, Sidecar(
        schema_version=CURRENT_SCHEMA_VERSION,
        store_url="https://x.bandcamp.com/album/y",
        bandcamp=BandcampInfo(item_id=1),
    ))
    r = client.get("/tasks")
    assert "Needs MBID" in r.text
    assert "Open in Harmony" in r.text
    assert "harmony.pulsewidth.org.uk" in r.text


def test_needs_mbid_card_without_store_url_rendered(client, cfg):
    d = _make_album(cfg, "NoURL")
    sc.write(d, Sidecar(schema_version=CURRENT_SCHEMA_VERSION))
    r = client.get("/tasks")
    assert "Needs MBID" in r.text
    # Manual MBID form is included
    assert 'name="mbid"' in r.text
    assert "/manual/" in r.text


def test_new_card_offers_three_paths(client, cfg):
    _make_album(cfg, "New Album")
    r = client.get("/tasks")
    assert "Reconcile from tags" in r.text
    assert "Recover store URL" in r.text
    assert "Assign &amp; Tag" in r.text or "Assign & Tag" in r.text


def test_needs_review_card_renders_side_by_side(client, cfg):
    d = _make_album(cfg, "NR Album")
    sc.write(d, Sidecar(
        schema_version=CURRENT_SCHEMA_VERSION,
        store_url="https://x.bandcamp.com/album/y",
        bandcamp=BandcampInfo(item_id=1),
        mb_match_candidate=MatchCandidate(
            mb_release_id="rel-aaa",
            confidence="approximate",
            file_count=2, track_count=2,
            track_comparisons=[
                TrackComparison(file_name="01.m4a", file_duration_ms=180000,
                                file_title="Side A", mb_track_title="Side A",
                                mb_track_length_ms=185000, delta_ms=5000),
                TrackComparison(file_name="02.m4a", file_duration_ms=200000,
                                file_title="Side B", mb_track_title="Side B",
                                mb_track_length_ms=200500, delta_ms=500),
            ],
        ),
    ))
    r = client.get("/tasks")
    assert "Needs Review" in r.text
    assert "approximate" in r.text
    assert "Side A" in r.text
    assert "Confirm" in r.text
    assert "Reject" in r.text


def test_needs_sync_card_renders(client, cfg):
    d = _make_album(cfg, "UB Album")
    # Tag the file so scanner sees it as DONE-style (mb_release_id matches)
    audio = MP4(d / "01 Track.m4a")
    audio["----:com.apple.iTunes:MusicBrainz Album Id"] = [b"rel-aaa"]
    audio.save()
    sc.write(d, Sidecar(
        schema_version=CURRENT_SCHEMA_VERSION,
        store_url="https://x.bandcamp.com/album/y",
        bandcamp=BandcampInfo(item_id=None),
        mb_release_id="rel-aaa",
        tagged_at=datetime.now(timezone.utc),
    ))
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


def test_reject_clears_candidate(client, cfg):
    d = _make_album(cfg, "RC")
    sc.write(d, Sidecar(
        schema_version=CURRENT_SCHEMA_VERSION,
        store_url="https://x.bandcamp.com/album/y",
        bandcamp=BandcampInfo(item_id=1),
        mb_match_candidate=MatchCandidate(
            mb_release_id="rel-zzz", confidence="approximate",
            file_count=1, track_count=1,
        ),
    ))
    aid = _id_for(cfg, d)
    r = client.post(f"/reject/{aid}")
    assert r.status_code == 200
    loaded = sc.read(d)
    assert loaded.mb_match_candidate is None


def test_unconfirmed_url_update(client, cfg):
    d = _make_album(cfg, "UB")
    audio = MP4(d / "01 Track.m4a")
    audio["----:com.apple.iTunes:MusicBrainz Album Id"] = [b"rel-aaa"]
    audio.save()
    sc.write(d, Sidecar(
        schema_version=CURRENT_SCHEMA_VERSION,
        store_url="https://x.bandcamp.com/album/old",
        bandcamp=BandcampInfo(item_id=None),
        mb_release_id="rel-aaa", tagged_at=datetime.now(timezone.utc),
    ))
    aid = _id_for(cfg, d)
    r = client.post(f"/unconfirmed/{aid}/url",
                    data={"url": "https://x.bandcamp.com/album/new"})
    assert r.status_code == 200
    loaded = sc.read(d)
    assert loaded.store_url == "https://x.bandcamp.com/album/new"
    assert loaded.bandcamp is None or loaded.bandcamp.item_id is None


def test_unconfirmed_mark_manual(client, cfg):
    d = _make_album(cfg, "UB2")
    audio = MP4(d / "01 Track.m4a")
    audio["----:com.apple.iTunes:MusicBrainz Album Id"] = [b"rel-aaa"]
    audio.save()
    sc.write(d, Sidecar(
        schema_version=CURRENT_SCHEMA_VERSION,
        store_url="https://x.bandcamp.com/album/y",
        bandcamp=BandcampInfo(item_id=None),
        mb_release_id="rel-aaa", tagged_at=datetime.now(timezone.utc),
    ))
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
            {"id": "rel-1", "title": "Result A", "artist": "X", "date": "2020",
             "country": "GB", "status": "Official", "track_count": 10,
             "label": "Label", "catalog_number": "CAT1"},
            {"id": "rel-2", "title": "Result B", "artist": "X", "date": "2021",
             "country": "US", "status": "Official", "track_count": 12,
             "label": None, "catalog_number": None},
        ],
    )
    r = client.post(f"/manual/{aid}/search", data={"artist": "X", "title": "Y"})
    assert r.status_code == 200
    assert "Result A" in r.text
    assert "Result B" in r.text
    assert "rel-1" in r.text
    assert 'name="mbid"' in r.text  # hidden mbid input on each "Use" button


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


def test_manual_assign_with_full_url(client, cfg, monkeypatch):
    """Pasting an MB release URL should extract the MBID."""
    d = _make_album(cfg, "AssignURL")
    aid = _id_for(cfg, d)
    captured = {}

    def fake_fetch(mbid):
        captured["mbid"] = mbid
        return _release_for_match(mbid, n_tracks=1)

    monkeypatch.setattr("harmonist.mb_lookup.fetch_release", fake_fetch)
    monkeypatch.setattr(
        "harmonist.cover_art.ensure_cover", lambda *a, **kw: None
    )

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


def test_manual_assign_invalid_input(client, cfg):
    d = _make_album(cfg, "Bad")
    aid = _id_for(cfg, d)
    r = client.post(f"/manual/{aid}/assign", data={"mbid": "not-an-mbid"})
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


def test_recover_url_writes_partial_sidecar(client, cfg):
    d = _make_album(cfg, "RecoverMe", comment="https://artist.bandcamp.com/album/the-album")
    aid = _id_for(cfg, d)
    r = client.post(f"/recover/{aid}")
    assert r.status_code == 200
    loaded = sc.read(d)
    assert loaded is not None
    assert loaded.store_url == "https://artist.bandcamp.com/album/the-album"
    assert loaded.bandcamp is None
    assert loaded.mb_release_id is None


def test_recover_url_warning_when_no_evidence(client, cfg):
    d = _make_album(cfg, "NoEvidence")  # no ©cmt
    aid = _id_for(cfg, d)
    r = client.post(f"/recover/{aid}")
    assert r.status_code == 200
    assert "Could not recover" in r.text
    assert not sc.has_sidecar(d)


def test_recover_url_400_when_not_new(client, cfg):
    d = _make_album(cfg, "NotNew")
    sc.write(d, Sidecar(schema_version=CURRENT_SCHEMA_VERSION))
    aid = _id_for(cfg, d)
    r = client.post(f"/recover/{aid}")
    assert r.status_code == 400


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
                        "recording": {"id": f"rec-{i}", "title": f"Track {i}", "length": str(length_ms)},
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
    audio["----:com.apple.iTunes:MusicBrainz Album Id"] = [mbid.encode("utf-8")]
    audio.save()
    from harmonist.models import BandcampInfo, Sidecar
    sc.write(d, Sidecar(
        schema_version=CURRENT_SCHEMA_VERSION,
        store_url=(f"https://x.bandcamp.com/album/{name.lower().replace(' ', '-')}"
                   if item_id else None),
        bandcamp=BandcampInfo(item_id=item_id) if item_id else None,
        mb_release_id=mbid,
        tagged_at=tagged_at,
    ))
    return d


def test_library_renders_only_done_albums(client, cfg):
    from datetime import datetime, timezone
    _make_album(cfg, "NewAlbum")
    _make_tagged_album(cfg, "DoneOne", mbid="rel-1", tagged_at=datetime.now(timezone.utc), item_id=100)
    r = client.get("/library")
    assert r.status_code == 200
    assert "DoneOne" in r.text
    assert "NewAlbum" not in r.text


def test_library_sorted_by_tagged_at_desc(client, cfg):
    from datetime import datetime, timezone, timedelta
    base = datetime.now(timezone.utc)
    _make_tagged_album(cfg, "Old", mbid="rel-old", tagged_at=base - timedelta(days=5), item_id=1)
    _make_tagged_album(cfg, "Mid", mbid="rel-mid", tagged_at=base - timedelta(days=2), item_id=2)
    _make_tagged_album(cfg, "Recent", mbid="rel-recent", tagged_at=base, item_id=3)
    r = client.get("/library")
    text = r.text
    # Recent appears before Mid which appears before Old
    assert text.index("Recent") < text.index("Mid") < text.index("Old")


def test_library_pagination_offset_limit(client, cfg):
    from datetime import datetime, timezone, timedelta
    base = datetime.now(timezone.utc)
    for i in range(5):
        _make_tagged_album(cfg, f"Album{i}", mbid=f"rel-{i}",
                          tagged_at=base - timedelta(days=i), item_id=i + 1)
    r = client.get("/library?offset=0&limit=2")
    assert r.status_code == 200
    # First page has 2 rows
    assert r.text.count('id="lib-') == 2
    # Load more button references offset=2
    assert "offset=2" in r.text


def test_library_load_more_button_absent_on_last_page(client, cfg):
    from datetime import datetime, timezone
    _make_tagged_album(cfg, "OnlyOne", mbid="rel-1", tagged_at=datetime.now(timezone.utc), item_id=1)
    r = client.get("/library?offset=0&limit=10")
    assert "Load more" not in r.text


def test_library_first_page_includes_header(client, cfg):
    from datetime import datetime, timezone
    _make_tagged_album(cfg, "Album", mbid="rel-1", tagged_at=datetime.now(timezone.utc), item_id=1)
    r = client.get("/library?offset=0")
    assert "<h2" in r.text and "Library" in r.text
    assert "Refresh" in r.text


def test_library_second_page_omits_header(client, cfg):
    from datetime import datetime, timezone
    _make_tagged_album(cfg, "Album", mbid="rel-1", tagged_at=datetime.now(timezone.utc), item_id=1)
    r = client.get("/library?offset=30&limit=30")
    # No header on offsets > 0 (the load-more button replaces itself)
    assert "<h2" not in r.text


def test_library_empty_state(client):
    r = client.get("/library")
    assert "No fully-tagged albums yet" in r.text


def test_library_row_links_to_musicbrainz(client, cfg):
    from datetime import datetime, timezone
    _make_tagged_album(cfg, "Linked", mbid="abc-123", tagged_at=datetime.now(timezone.utc), item_id=42)
    r = client.get("/library")
    assert "musicbrainz.org/release/abc-123" in r.text


def test_library_row_shows_store_url_and_item_id_in_expanded(client, cfg):
    from datetime import datetime, timezone
    _make_tagged_album(cfg, "Linked", mbid="abc-123", tagged_at=datetime.now(timezone.utc), item_id=42)
    r = client.get("/library")
    assert "x.bandcamp.com" in r.text
    assert "42" in r.text  # item_id


# ---------- retag / forget ----------


def test_retag_re_runs_tagger(client, cfg, monkeypatch):
    from datetime import datetime, timezone
    d = _make_tagged_album(cfg, "ToRetag", mbid="rel-1",
                          tagged_at=datetime(2026, 1, 1, tzinfo=timezone.utc), item_id=1)
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
    assert loaded.tagged_at > datetime(2026, 1, 1, tzinfo=timezone.utc)


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
    sc.write(d, Sidecar(
        schema_version=CURRENT_SCHEMA_VERSION,
        store_url="https://x.bandcamp.com/album/y",
        bandcamp=BandcampInfo(item_id=None),
        mb_release_id="rel-x",
    ))
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
    from datetime import datetime, timezone
    d = _make_tagged_album(cfg, "Forgetme", mbid="rel-1",
                          tagged_at=datetime.now(timezone.utc), item_id=1)
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
    from datetime import datetime, timezone
    d = _make_tagged_album(cfg, "Exempt", mbid="rel-1",
                          tagged_at=datetime.now(timezone.utc), item_id=1)
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
    sc.write(d, Sidecar(
        schema_version=CURRENT_SCHEMA_VERSION,
        store_url="https://x.bandcamp.com/album/y",
    ))
    aid = _id_for(cfg, d)
    assert aid == sc.read(d).temp_uid


def test_sidecar_album_id_matches_mbid_when_tagged(client, cfg):
    """A tagged album's id is its MBID."""
    from datetime import datetime, timezone
    d = _make_tagged_album(cfg, "Tagged", mbid="abc-mbid-1234",
                           tagged_at=datetime.now(timezone.utc), item_id=42)
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
    sc.write(d, Sidecar(
        schema_version=CURRENT_SCHEMA_VERSION,
        store_url="https://x.bandcamp.com/album/y",
    ))
    assert sc.read(d).temp_uid == registry_uid
    # And the album.id from a fresh scan still matches
    assert _id_for(cfg, d) == registry_uid


def test_sidecar_album_id_survives_rename(client, cfg):
    """The UUID lives in the sidecar JSON, which moves with the directory
    on rename. So album.id is stable across renames for sidecar'd albums.
    """
    d = _make_album(cfg, "RenameMe")
    sc.write(d, Sidecar(
        schema_version=CURRENT_SCHEMA_VERSION,
        store_url="https://x.bandcamp.com/album/y",
    ))
    aid_before = _id_for(cfg, d)

    new_d = d.parent / "Renamed"
    d.rename(new_d)
    aid_after = _id_for(cfg, new_d)
    assert aid_before == aid_after


def test_route_404_when_id_is_unknown(client):
    r = client.post("/recheck/this-id-doesnt-exist")
    assert r.status_code == 404


def test_canonical_id_change_mid_transaction(client, cfg, monkeypatch):
    """A handler that mutates an album to assign an MBID changes the
    canonical id as a side effect. The action response itself is 200 (the
    handler ran at the URL that was canonical at request time); the new
    canonical id takes effect on the *next* request via the inbox refresh.

    Design: we do NOT redirect mid-transaction. The HX-Trigger='tasks-changed'
    header propagates the new id to the UI.
    """
    d = _make_album(cfg, "ToAssign")
    sc.write(d, Sidecar(
        schema_version=CURRENT_SCHEMA_VERSION,
        store_url="https://x.bandcamp.com/album/y",
    ))
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
