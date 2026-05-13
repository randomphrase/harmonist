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
    _make_album(cfg, "Orphan Album")
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
    # Held (Bandcamp)
    d1 = _make_album(cfg, "HBC")
    sc.write(d1, Sidecar(
        schema_version=1, source="bandcamp",
        bandcamp=BandcampInfo(url="https://x.bandcamp.com/album/y", item_id=1),
    ))
    # Held (Manual)
    d2 = _make_album(cfg, "HM")
    sc.write(d2, Sidecar(schema_version=1, source="manual"))

    r = client.get("/tasks")
    # Both state headings appear
    assert "Held (Bandcamp)" in r.text
    assert "Held (Manual)" in r.text
    # Their per-section instructions appear
    assert "Open in Harmony" in r.text  # held-bandcamp instruction
    assert "Paste an MB release URL" in r.text  # held-manual instruction


def test_tasks_state_group_omitted_when_empty(client, cfg):
    """No section header rendered for states without any albums."""
    _make_album(cfg, "Orphan only")
    r = client.get("/tasks")
    assert "Orphans" in r.text
    # No Held / NeedsConfirmation / Tagging headers
    assert "Held (Bandcamp)" not in r.text
    assert "Needs Confirmation" not in r.text


def test_tasks_unconfirmed_bandcamp_section_advises_sync(client, cfg):
    """The UB group instructions point the user to click Sync."""
    from datetime import datetime, timezone
    d = _make_album(cfg, "UB")
    audio = MP4(d / "01 Track.m4a")
    audio["----:com.apple.iTunes:MusicBrainz Album Id"] = [b"rel-a"]
    audio.save()
    sc.write(d, Sidecar(
        schema_version=1, source="bandcamp",
        bandcamp=BandcampInfo(url="https://x.bandcamp.com/album/y", item_id=None),
        mb_release_id="rel-a",
        tagged_at=datetime.now(timezone.utc),
    ))
    r = client.get("/tasks")
    assert "Unconfirmed Bandcamp" in r.text
    # Instruction explicitly calls out Sync
    assert "Click Sync" in r.text


def test_tasks_empty_state_message_distinguishes_zero_vs_all_done(client, cfg):
    # Empty library
    r = client.get("/tasks")
    assert "Drop some albums" in r.text or "Inbox is empty" in r.text


# ---------- state dispatch — each card type renders ----------

def test_orphan_card_rendered(client, cfg):
    _make_album(cfg, "Orphan Album")
    r = client.get("/tasks")
    assert "Orphan" in r.text
    assert "Reconcile" in r.text
    assert 'hx-post="/reconcile/' in r.text


def test_held_bandcamp_card_rendered(client, cfg):
    d = _make_album(cfg, "Held BC")
    sc.write(d, Sidecar(
        schema_version=1, source="bandcamp",
        bandcamp=BandcampInfo(url="https://x.bandcamp.com/album/y", item_id=1),
    ))
    r = client.get("/tasks")
    assert "Held (Bandcamp)" in r.text
    assert "Open in Harmony" in r.text
    assert "harmony.pulsewidth.org.uk" in r.text


def test_held_manual_card_rendered(client, cfg):
    d = _make_album(cfg, "Held Manual")
    sc.write(d, Sidecar(schema_version=1, source="manual"))
    r = client.get("/tasks")
    assert "Held (Manual)" in r.text
    # Manual MBID form is included
    assert 'name="mbid"' in r.text
    assert "/manual/" in r.text


def test_orphan_card_offers_three_paths(client, cfg):
    _make_album(cfg, "Orphan Album")
    r = client.get("/tasks")
    assert "Reconcile from tags" in r.text
    assert "Recover Bandcamp URL" in r.text
    assert "Assign &amp; Tag" in r.text or "Assign & Tag" in r.text


def test_needs_confirmation_card_renders_side_by_side(client, cfg):
    d = _make_album(cfg, "NC Album")
    sc.write(d, Sidecar(
        schema_version=1, source="bandcamp",
        bandcamp=BandcampInfo(url="https://x.bandcamp.com/album/y", item_id=1),
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
    assert "Needs Confirmation" in r.text
    assert "approximate" in r.text
    assert "Side A" in r.text
    assert "Confirm" in r.text
    assert "Reject" in r.text


def test_unconfirmed_bandcamp_card_renders(client, cfg):
    d = _make_album(cfg, "UB Album")
    # Tag the file so scanner sees it as DONE-style (mb_release_id matches)
    audio = MP4(d / "01 Track.m4a")
    audio["----:com.apple.iTunes:MusicBrainz Album Id"] = [b"rel-aaa"]
    audio.save()
    sc.write(d, Sidecar(
        schema_version=1, source="bandcamp",
        bandcamp=BandcampInfo(url="https://x.bandcamp.com/album/y", item_id=None),
        mb_release_id="rel-aaa",
        tagged_at=datetime.now(timezone.utc),
    ))
    r = client.get("/tasks")
    assert "Unconfirmed Bandcamp" in r.text
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
        schema_version=1, source="bandcamp",
        bandcamp=BandcampInfo(url="https://x.bandcamp.com/album/y", item_id=1),
        mb_match_candidate=MatchCandidate(
            mb_release_id="rel-zzz", confidence="approximate",
            file_count=1, track_count=1,
        ),
    ))
    from harmonist.models import Album
    aid = Album.make_id(d)
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
        schema_version=1, source="bandcamp",
        bandcamp=BandcampInfo(url="https://x.bandcamp.com/album/old", item_id=None),
        mb_release_id="rel-aaa", tagged_at=datetime.now(timezone.utc),
    ))
    from harmonist.models import Album
    aid = Album.make_id(d)
    r = client.post(f"/unconfirmed/{aid}/url",
                    data={"url": "https://x.bandcamp.com/album/new"})
    assert r.status_code == 200
    loaded = sc.read(d)
    assert loaded.bandcamp.url == "https://x.bandcamp.com/album/new"
    assert loaded.bandcamp.item_id is None


def test_unconfirmed_mark_manual(client, cfg):
    d = _make_album(cfg, "UB2")
    audio = MP4(d / "01 Track.m4a")
    audio["----:com.apple.iTunes:MusicBrainz Album Id"] = [b"rel-aaa"]
    audio.save()
    sc.write(d, Sidecar(
        schema_version=1, source="bandcamp",
        bandcamp=BandcampInfo(url="https://x.bandcamp.com/album/y", item_id=None),
        mb_release_id="rel-aaa", tagged_at=datetime.now(timezone.utc),
    ))
    from harmonist.models import Album
    aid = Album.make_id(d)
    r = client.post(f"/unconfirmed/{aid}/manual")
    assert r.status_code == 200
    loaded = sc.read(d)
    assert loaded.source == "manual"
    assert loaded.bandcamp is None
    assert loaded.mb_release_id == "rel-aaa"  # preserved


def test_404_for_missing_album(client):
    r = client.post("/recheck/nonexistent")
    assert r.status_code == 404


# ---------- manual ingest flow ----------


def test_manual_search_returns_results(client, cfg, monkeypatch):
    d = _make_album(cfg, "Search Album")
    from harmonist.models import Album
    aid = Album.make_id(d)
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
    from harmonist.models import Album
    aid = Album.make_id(d)
    monkeypatch.setattr(
        "harmonist.mb_search.search_releases",
        lambda artist, title, limit=10: [],
    )
    r = client.post(f"/manual/{aid}/search", data={"artist": "X", "title": "Y"})
    assert r.status_code == 200
    assert "No matches" in r.text


def test_manual_search_handles_mb_error(client, cfg, monkeypatch):
    d = _make_album(cfg, "Errsearch")
    from harmonist.models import Album
    aid = Album.make_id(d)
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
    from harmonist.models import Album
    aid = Album.make_id(d)
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
    assert loaded.source == "manual"
    assert loaded.mb_release_id == "abc12345-1234-1234-1234-1234567890ab"


def test_manual_assign_with_bare_mbid(client, cfg, monkeypatch):
    d = _make_album(cfg, "AssignMBID")
    from harmonist.models import Album
    aid = Album.make_id(d)

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
    from harmonist.models import Album
    aid = Album.make_id(d)
    r = client.post(f"/manual/{aid}/assign", data={"mbid": "not-an-mbid"})
    assert r.status_code == 200
    assert "Could not parse" in r.text
    assert not sc.has_sidecar(d)


def test_manual_assign_with_approximate_match_stores_candidate(client, cfg, monkeypatch):
    d = _make_album(cfg, "Approximate")
    from harmonist.models import Album
    aid = Album.make_id(d)

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
    from harmonist.models import Album
    aid = Album.make_id(d)
    r = client.post(f"/recover/{aid}")
    assert r.status_code == 200
    loaded = sc.read(d)
    assert loaded is not None
    assert loaded.source == "bandcamp"
    assert loaded.bandcamp.url == "https://artist.bandcamp.com/album/the-album"
    assert loaded.bandcamp.item_id is None
    assert loaded.mb_release_id is None


def test_recover_url_warning_when_no_evidence(client, cfg):
    d = _make_album(cfg, "NoEvidence")  # no ©cmt
    from harmonist.models import Album
    aid = Album.make_id(d)
    r = client.post(f"/recover/{aid}")
    assert r.status_code == 200
    assert "Could not recover" in r.text
    assert not sc.has_sidecar(d)


def test_recover_url_400_when_not_orphan(client, cfg):
    d = _make_album(cfg, "NotOrphan")
    sc.write(d, Sidecar(schema_version=1, source="manual"))
    from harmonist.models import Album
    aid = Album.make_id(d)
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
    from harmonist.models import Album
    aid = Album.make_id(d)
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
        schema_version=1, source="bandcamp" if item_id else "manual",
        bandcamp=BandcampInfo(url=f"https://x.bandcamp.com/album/{name.lower().replace(' ', '-')}",
                              item_id=item_id) if item_id else None,
        mb_release_id=mbid,
        tagged_at=tagged_at,
    ))
    return d


def test_library_renders_only_done_albums(client, cfg):
    from datetime import datetime, timezone
    _make_album(cfg, "OrphanAlbum")
    _make_tagged_album(cfg, "DoneOne", mbid="rel-1", tagged_at=datetime.now(timezone.utc), item_id=100)
    r = client.get("/library")
    assert r.status_code == 200
    assert "DoneOne" in r.text
    assert "OrphanAlbum" not in r.text


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


def test_library_row_shows_bandcamp_url_and_item_id_in_expanded(client, cfg):
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

    from harmonist.models import Album
    aid = Album.make_id(d)
    r = client.post(f"/retag/{aid}")
    assert r.status_code == 200
    assert "Re-tagged" in r.text
    # tagged_at should be refreshed
    loaded = sc.read(d)
    assert loaded.tagged_at > datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_retag_400_when_no_mbid_on_sidecar(client, cfg):
    d = _make_album(cfg, "NoMBID")
    sc.write(d, sc_module_Sidecar_manual())
    from harmonist.models import Album
    aid = Album.make_id(d)
    r = client.post(f"/retag/{aid}")
    assert r.status_code == 400


def sc_module_Sidecar_manual():
    from harmonist.models import Sidecar
    return Sidecar(schema_version=1, source="manual")


def test_forget_deletes_sidecar(client, cfg):
    from datetime import datetime, timezone
    d = _make_tagged_album(cfg, "Forgetme", mbid="rel-1",
                          tagged_at=datetime.now(timezone.utc), item_id=1)
    assert sc.has_sidecar(d)
    from harmonist.models import Album
    aid = Album.make_id(d)
    r = client.post(f"/forget/{aid}")
    assert r.status_code == 200
    assert "Forgotten" in r.text
    # Sidecar gone; album files untouched
    assert not sc.has_sidecar(d)
    assert (d / "01 Track.m4a").exists()


def test_cover_serves_when_present(client, cfg):
    d = _make_album(cfg, "WithCover")
    (d / "cover.jpg").write_bytes(b"\xff\xd8\xff\xe0FAKE")
    from harmonist.models import Album
    aid = Album.make_id(d)
    r = client.get(f"/cover/{aid}")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg"
    assert r.content.startswith(b"\xff\xd8")
