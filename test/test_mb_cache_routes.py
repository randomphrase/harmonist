"""Which routes get a cached MusicBrainz answer, and which must not (#127).

The cache is only half the feature; the other half is the routing rule, and it
is the half that can go wrong silently. A route wrongly cached spends nothing
and shows the user stale data; a route wrongly uncached works perfectly and
quietly burns the 1-req/sec budget it was built to protect. Neither shows up as
a failure anywhere else, so the request count is asserted directly.

The rule under test: **reads that display or compare may be cached; writes, and
anything the user pressed to force a re-check, fetch fresh.**
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from mutagen.mp4 import MP4

from harmonist import activity_store, mb_cache, mb_lookup
from harmonist import sidecar as sidecar_mod
from harmonist.config import BandcampConfig, Config, PathsConfig, ServerConfig, TestConfig
from harmonist.models import Sidecar
from harmonist.web.main import create_app

FIXTURES_DIR = Path(__file__).parent / "fixtures"
SINE_M4A = FIXTURES_DIR / "sine.m4a"
MBID = "33333333-4444-5555-6666-777777777777"
STORE_URL = "https://artist.bandcamp.com/album/test-album"


@pytest.fixture
def cfg(tmp_path):
    return Config(
        paths=PathsConfig(config_dir=tmp_path / "config", music_dir=tmp_path / "music"),
        bandcamp=BandcampConfig(),
        server=ServerConfig(),
        test=TestConfig(mode="fixture"),
    )


@pytest.fixture(autouse=True)
def no_cover_fetch(monkeypatch):
    monkeypatch.setattr("harmonist.cover_art.ensure_cover", lambda *a, **kw: None)


@pytest.fixture(autouse=True)
def default_ttl():
    """`mb_cache.configure` is process-level state — put it back, or a test that
    changed it silently changes a later one (tests run in random order)."""
    yield
    mb_cache.configure(timedelta(hours=1))


@pytest.fixture
def client(cfg):
    cfg.paths.music_dir.mkdir(parents=True, exist_ok=True)
    cfg.paths.config_dir.mkdir(parents=True, exist_ok=True)
    return TestClient(create_app(cfg), headers={"HX-Request": "true"})


class _Counter:
    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    def __call__(self, mbid):
        self.calls += 1
        return self.payload


def _release() -> dict:
    return {
        "id": MBID,
        "title": "Test Album",
        "status": "Official",
        "artist-credit": [{"artist": {"id": "art-1", "name": "Artist"}, "name": "Artist"}],
        "release-group": {"id": "rg-1", "primary-type": "Album"},
        "medium-list": [
            {
                "position": "1",
                "format": "CD",
                "track-list": [
                    {
                        "id": f"rt-{i}",
                        "position": str(i),
                        "title": f"Track {i}",
                        "recording": {"id": f"rec-{i}", "title": f"Track {i}", "length": "1000"},
                    }
                    for i in range(1, 3)
                ],
            }
        ],
    }


def _album(cfg, *, store_url: str | None = None) -> Path:
    d = cfg.paths.music_dir / "Artist" / "Album"
    d.mkdir(parents=True)
    for i in (1, 2):
        f = d / f"0{i} Track {i}.m4a"
        shutil.copy(SINE_M4A, f)
        a = MP4(f)
        a["\xa9alb"] = ["Test Album"]
        a["\xa9ART"] = ["Artist"]
        a["trkn"] = [(i, 2)]
        a["----:com.apple.iTunes:MusicBrainz Album Id"] = [MBID.encode()]
        a.save()
    sidecar_mod.write(
        d,
        Sidecar(
            mb_release_id=MBID,
            tagged_at=datetime(2026, 1, 1, tzinfo=UTC),
            store_url=store_url,
        ),
    )
    return d


def _album_id(cfg, album_dir: Path) -> str:
    from harmonist import scanner

    for a in scanner.scan(cfg.paths.music_dir):
        if a.path == album_dir:
            return a.id
    raise AssertionError(f"no album at {album_dir}")


def test_opening_an_album_page_twice_costs_one_musicbrainz_request(client, cfg, monkeypatch):
    """The headline saving. Browsing a library used to spend a rate-limited
    request per album page view, every view."""
    d = _album(cfg)
    fetch = _Counter(_release())
    monkeypatch.setattr(mb_lookup, "fetch_release", fetch)
    album_id = _album_id(cfg, d)

    assert client.get(f"/library/{album_id}/compare").status_code == 200
    assert client.get(f"/library/{album_id}/compare").status_code == 200

    assert fetch.calls == 1


def test_the_compare_panel_says_when_it_last_read_musicbrainz(client, cfg, monkeypatch):
    """The affordance that makes a cached comparison honest. Before the cache
    this said "just now" unconditionally, because it always was.

    The row is aged to three hours under a SIX-hour TTL, so it is stale-looking
    but still servable. Ageing it past the TTL would prove nothing: the next read
    would re-fetch and re-stamp it, and "read just now" would be the truth.
    """
    d = _album(cfg)
    monkeypatch.setattr(mb_lookup, "fetch_release", _Counter(_release()))
    album_id = _album_id(cfg, d)

    client.get(f"/library/{album_id}/compare")
    mb_cache.configure(timedelta(hours=6))
    conn = activity_store._ensure()
    conn.execute(
        "UPDATE mb_release_cache SET fetched_at = ?",
        ((datetime.now(UTC) - timedelta(hours=3)).isoformat(),),
    )
    conn.commit()
    body = client.get(f"/library/{album_id}/compare").text

    assert "read 3 hours ago" in body, body[:400]


def test_read_again_forces_a_live_fetch(client, cfg, monkeypatch):
    """The escape hatch from a stale answer (review-gate item 5). Without it a
    user who has just edited MusicBrainz has no way to see the edit short of
    re-tagging."""
    d = _album(cfg)
    fetch = _Counter(_release())
    monkeypatch.setattr(mb_lookup, "fetch_release", fetch)
    album_id = _album_id(cfg, d)

    client.get(f"/library/{album_id}/compare")
    client.get(f"/library/{album_id}/compare?reread=1")

    assert fetch.calls == 2


def test_the_compare_panel_offers_the_re_read_control(client, cfg, monkeypatch):
    """The control has to be ON the panel for the escape hatch to exist. Asserted
    as an absence elsewhere would be untestable, so this is the positive form."""
    d = _album(cfg)
    monkeypatch.setattr(mb_lookup, "fetch_release", _Counter(_release()))
    album_id = _album_id(cfg, d)

    body = client.get(f"/library/{album_id}/compare").text

    assert f'hx-get="/library/{album_id}/compare?reread=1"' in body


def test_a_retag_never_tags_from_a_cached_release(client, cfg, monkeypatch):
    """It writes tags to the user's files. Doing that from an hour-old payload
    would put metadata on disk Harmonist had already been told was superseded."""
    d = _album(cfg)
    fetch = _Counter(_release())
    monkeypatch.setattr(mb_lookup, "fetch_release", fetch)
    album_id = _album_id(cfg, d)

    client.get(f"/library/{album_id}/compare")  # warms the cache
    assert fetch.calls == 1
    assert client.post(f"/retag/{album_id}").status_code == 200

    assert fetch.calls == 2, "the re-tag must have gone to MusicBrainz itself"


def test_a_recheck_never_matches_against_a_cached_release(client, cfg, monkeypatch):
    """ "Recheck" means "I have just edited MusicBrainz". Serving it a stored
    payload would make the button a silent no-op with nothing on screen to say
    why."""
    d = _album(cfg, store_url=STORE_URL)
    fetch = _Counter(_release())
    monkeypatch.setattr(mb_lookup, "fetch_release", fetch)
    monkeypatch.setattr(mb_lookup, "lookup_by_bandcamp_url", lambda url: [MBID])
    album_id = _album_id(cfg, d)

    client.get(f"/library/{album_id}/compare")  # warms the cache
    assert fetch.calls == 1
    client.post(f"/recheck/{album_id}")

    assert fetch.calls >= 2, "the recheck must have gone to MusicBrainz itself"


def test_a_forced_read_leaves_the_cache_current_for_the_next_reader(client, cfg, monkeypatch):
    """A bypass that read ROUND the cache would leave the stored row stale at
    exactly the moment MusicBrainz is known to have changed — and that row is
    the gardener's baseline (#32)."""
    d = _album(cfg)
    fetch = _Counter(_release())
    monkeypatch.setattr(mb_lookup, "fetch_release", fetch)
    album_id = _album_id(cfg, d)

    client.post(f"/retag/{album_id}")  # a forced, uncached fetch
    calls_after_retag = fetch.calls
    client.get(f"/library/{album_id}/compare")

    assert fetch.calls == calls_after_retag, "the re-tag should have filled the cache"
