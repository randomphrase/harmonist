"""What a re-tag does to the sidecar beside the files (#239, #240).

`_tag_with_release` used to build a **new** `Sidecar(...)` field by field, which
meant every field it did not name was reset to its default — silently, and with
nothing in the suite to notice. Three of them mattered:

* `video_media` (#206), so an album with a bonus DVD went Incomplete after every
  re-tag and stayed there until a reconcile pass spent a MusicBrainz request
  re-learning what Harmonist had already written down;
* `purchase_unavailable` and `tracks_unavailable`, the surrender and
  accept-as-done flags, whose entire purpose is to be permanent.

The shape is the bug, not the three fields: a field added to the model later
would have joined them. So the test below asserts the WHOLE record, not the
fields that were wrong this time.
"""

from __future__ import annotations

import dataclasses
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from mutagen.mp4 import MP4

from harmonist import activity_store, mb_lookup
from harmonist import sidecar as sidecar_mod
from harmonist.config import BandcampConfig, Config, PathsConfig, ServerConfig, TestConfig
from harmonist.models import BandcampInfo, MatchCandidate, Sidecar
from harmonist.web.main import create_app

FIXTURES_DIR = Path(__file__).parent / "fixtures"
SINE_M4A = FIXTURES_DIR / "sine.m4a"
MBID = "33333333-4444-5555-6666-777777777777"
TAGGED_AT = datetime(2026, 1, 1, tzinfo=UTC)


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
    """No cover-art requests: this module is about the sidecar, and CAA is off-box."""
    monkeypatch.setattr("harmonist.cover_art.ensure_cover", lambda *a, **kw: None)


@pytest.fixture
def client(cfg):
    cfg.paths.music_dir.mkdir(parents=True, exist_ok=True)
    cfg.paths.config_dir.mkdir(parents=True, exist_ok=True)
    # HX-Request: the CSRF middleware requires it on every state-changing call.
    return TestClient(create_app(cfg), headers={"HX-Request": "true"})


def _release(*, video_disc: bool) -> dict:
    """A 2-track CD, optionally followed by a 2-track DVD of videos."""
    release = {
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
    if video_disc:
        release["medium-list"].append(
            {
                "position": "2",
                "format": "DVD-Video",
                "track-list": [
                    {
                        "id": f"rt-v{i}",
                        "position": str(i),
                        "title": f"Video {i}",
                        "recording": {
                            "id": f"rec-v{i}",
                            "title": f"Video {i}",
                            "length": "300000",
                            "video": "true",
                        },
                    }
                    for i in range(1, 3)
                ],
            }
        )
    return release


def _album(cfg, sidecar: Sidecar) -> Path:
    """A 2-track album carrying `sidecar`, tagged and sitting in the Library."""
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
    sidecar_mod.write(d, sidecar)
    return d


def _album_id(cfg, album_dir: Path) -> str:
    from harmonist import scanner

    for a in scanner.scan(cfg.paths.music_dir):
        if a.path == album_dir:
            return a.id
    raise AssertionError(f"no album at {album_dir}")


def _fully_populated() -> Sidecar:
    """Every field a sidecar can carry, set to something that is not its default
    — so anything the re-tag drops shows up as a difference."""
    return Sidecar(
        store_url="https://artist.bandcamp.com/album/test-album",
        bandcamp=BandcampInfo(item_id=4242, band_id=99, is_private=True),
        downloaded_at=datetime(2025, 6, 1, tzinfo=UTC),
        added_at=datetime(2025, 5, 1, tzinfo=UTC),
        mb_release_id=MBID,
        mb_match_candidate=MatchCandidate(
            mb_release_id=MBID, confidence="exact", file_count=2, track_count=2
        ),
        tagged_at=TAGGED_AT,
        notes="bought at a gig",
        purchase_unavailable=True,
        tracks_unavailable=True,
    )


def test_a_retag_changes_only_what_it_means_to_change(client, cfg, monkeypatch):
    """The whole record, field by field.

    Deliberately not a list of the three fields that were being lost: the defect
    was that the sidecar got REBUILT, so the next field added to the model would
    have been lost too. This fails if that happens again.
    """
    d = _album(cfg, _fully_populated())
    monkeypatch.setattr(mb_lookup, "fetch_release", lambda mbid: _release(video_disc=False))

    r = client.post(f"/retag/{_album_id(cfg, d)}")
    assert r.status_code == 200

    after = sidecar_mod.read(d)
    assert after is not None
    before = dataclasses.asdict(_fully_populated())
    changed = {k: v for k, v in dataclasses.asdict(after).items() if before[k] != v}

    assert set(changed) == {"mb_match_candidate", "tagged_at", "video_media"}
    assert changed["mb_match_candidate"] is None, "a suggestion, now acted on"
    assert after.tagged_at is not None and after.tagged_at > TAGGED_AT
    assert changed["video_media"] == (), "asked, and this release has no video media"


def test_a_retag_keeps_a_surrender(client, cfg, monkeypatch):
    """`purchase_unavailable` is the user's decision that an album with no
    findable purchase is finished. A re-tag changes nothing about Bandcamp, and
    dropping it would send the album back round the sync-and-surrender loop."""
    d = _album(cfg, _fully_populated())
    monkeypatch.setattr(mb_lookup, "fetch_release", lambda mbid: _release(video_disc=False))

    client.post(f"/retag/{_album_id(cfg, d)}")

    after = sidecar_mod.read(d)
    assert after is not None
    assert after.purchase_unavailable is True
    assert after.tracks_unavailable is True


def test_a_retag_records_which_media_are_video(client, cfg, monkeypatch):
    """The release is in hand and carries the per-track video flag, so the fact
    is written at the moment it is known (#237) — rather than the album going
    Incomplete and a later reconcile pass spending a request to find it out."""
    d = _album(cfg, Sidecar(mb_release_id=MBID, tagged_at=TAGGED_AT))
    monkeypatch.setattr(mb_lookup, "fetch_release", lambda mbid: _release(video_disc=True))

    def _boom(mbid):
        raise AssertionError("the release already says which media are video")

    monkeypatch.setattr(mb_lookup, "fetch_video_media", _boom)

    client.post(f"/retag/{_album_id(cfg, d)}")

    after = sidecar_mod.read(d)
    assert after is not None
    assert after.video_media == (2,)


def test_the_audit_line_numbers_tracks_from_one(client, cfg, monkeypatch):
    """#240: `tag.track` recorded `_flatten_tracks`'s 0-based index, so every
    line in a production log was off by one against the file it named — and
    against the tracklist on the page right above it."""
    d = _album(cfg, Sidecar(mb_release_id=MBID, tagged_at=TAGGED_AT))
    monkeypatch.setattr(mb_lookup, "fetch_release", lambda mbid: _release(video_disc=False))

    client.post(f"/retag/{_album_id(cfg, d)}")

    rows = [
        e.message
        for e in activity_store.recent(50, source=activity_store.Source.AUDIT)
        if e.message.startswith("tag.track")
    ]
    assert any('file="01 Track 1.m4a" track=1' in m for m in rows), rows
    assert any('file="02 Track 2.m4a" track=2' in m for m in rows), rows
    assert not any("track=0" in m for m in rows)
