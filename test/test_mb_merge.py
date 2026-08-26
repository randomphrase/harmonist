"""What happens when MusicBrainz has merged the release an album names (#268).

MusicBrainz *redirects* a merged MBID: `fetch_release(old)` answers with the
**target** release, carrying a different `id`. Two writers then disagree —
`tagger._build_tagset` puts `release["id"]` (the new id) into every file while
`_tag_with_release` wrote the *requested* id to the sidecar. The album derives
TAGGING off that disagreement, the Inbox kicks the reconciler, and reconcile
adopts the file tags: an identity change laundered through machinery built for
"the user re-tagged in Picard", with nothing anywhere saying a merge happened.

Merges are common, and #32's nightly pass would walk into every one that has
happened since an album was tagged — so the album has to come out of a re-tag
settled, and its History has to say why its identity moved.
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from mutagen.mp4 import MP4

from harmonist import activity_store, mb_lookup, scanner
from harmonist import sidecar as sidecar_mod
from harmonist.config import BandcampConfig, Config, PathsConfig, ServerConfig, TestConfig
from harmonist.models import AlbumState, Sidecar
from harmonist.web.main import create_app

FIXTURES_DIR = Path(__file__).parent / "fixtures"
SINE_M4A = FIXTURES_DIR / "sine.m4a"

OLD_MBID = "11111111-2222-3333-4444-555555555555"
NEW_MBID = "99999999-8888-7777-6666-555555555555"
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
    """No cover-art requests: this module is about identity, and CAA is off-box."""
    monkeypatch.setattr("harmonist.cover_art.ensure_cover", lambda *a, **kw: None)


@pytest.fixture
def client(cfg):
    cfg.paths.music_dir.mkdir(parents=True, exist_ok=True)
    cfg.paths.config_dir.mkdir(parents=True, exist_ok=True)
    # HX-Request: the CSRF middleware requires it on every state-changing call.
    return TestClient(create_app(cfg), headers={"HX-Request": "true"})


def _release(mbid: str) -> dict:
    """A 2-track CD carrying `mbid` as its own id."""
    return {
        "id": mbid,
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


def _redirects_to(monkeypatch, mbid: str) -> None:
    """Every fetch answers with the release `mbid` names, whatever was asked for
    — which is exactly what MusicBrainz does for an id that has been merged
    away, and the only signal there is that it happened."""
    monkeypatch.setattr(mb_lookup, "fetch_release", lambda requested: _release(mbid))


def _album(cfg) -> Path:
    """A 2-track album tagged as OLD_MBID, sitting COMPLETE in the Library."""
    d = cfg.paths.music_dir / "Artist" / "Album"
    d.mkdir(parents=True)
    for i in (1, 2):
        f = d / f"0{i} Track {i}.m4a"
        shutil.copy(SINE_M4A, f)
        a = MP4(f)
        a["\xa9alb"] = ["Test Album"]
        a["\xa9ART"] = ["Artist"]
        a["trkn"] = [(i, 2)]
        a["----:com.apple.iTunes:MusicBrainz Album Id"] = [OLD_MBID.encode()]
        a.save()
    sidecar_mod.write(d, Sidecar(mb_release_id=OLD_MBID, tagged_at=TAGGED_AT))
    return d


def _scanned(cfg, album_dir: Path):
    for a in scanner.scan(cfg.paths.music_dir):
        if a.path == album_dir:
            return a
    raise AssertionError(f"no album at {album_dir}")


def _file_mbids(album_dir: Path) -> set[str]:
    key = "----:com.apple.iTunes:MusicBrainz Album Id"
    return {MP4(f)[key][0].decode() for f in sorted(album_dir.glob("*.m4a"))}


def _messages() -> list[str]:
    return [e.message for e in activity_store.recent(50)]


def test_a_merge_leaves_the_sidecar_and_the_files_naming_the_same_release(client, cfg, monkeypatch):
    """The two writers have to agree. The files get `release["id"]` and always
    did; the sidecar used to get the id that was *asked for*, which after a
    redirect names a release that no longer exists."""
    d = _album(cfg)
    _redirects_to(monkeypatch, NEW_MBID)

    r = client.post(f"/retag/{_scanned(cfg, d).id}")
    assert r.status_code == 200

    after = sidecar_mod.read(d)
    assert after is not None
    assert after.mb_release_id == NEW_MBID
    assert _file_mbids(d) == {NEW_MBID}


def test_a_merge_does_not_leave_the_album_looking_externally_retagged(client, cfg, monkeypatch):
    """The disagreement derived TAGGING (`scanner._derive_state`), so the Inbox
    picked the album up and reconcile rewrote its identity from the file tags —
    self-healing, but through a path meant for a re-tag done in Picard."""
    d = _album(cfg)
    _redirects_to(monkeypatch, NEW_MBID)

    client.post(f"/retag/{_scanned(cfg, d).id}")

    assert _scanned(cfg, d).state == AlbumState.COMPLETE


def test_a_merge_says_so_in_the_albums_history_naming_both_releases(client, cfg, monkeypatch):
    """A merge is qualitatively different from a tag change, and usually arrives
    with one. Nothing said it had happened.

    Asserted against `album_history`, which is the surface the album's History
    page renders: it unions activity and audit over the alias chain, so this
    covers the entry being *attributed* to the album as well as written.

    The two conditions are deliberately on ONE message. Split, they both pass
    for free: `sidecar.update` already renders `mbid=old->new` whenever an
    album's identity moves, so "some line names both ids" is true of a user
    re-matching an album by hand — which is the very thing a merge has been
    indistinguishable from. Naming it is the whole point of the record.
    """
    d = _album(cfg)
    _redirects_to(monkeypatch, NEW_MBID)

    client.post(f"/retag/{_scanned(cfg, d).id}")

    history = [e.message for e in activity_store.album_history(NEW_MBID)]
    assert [m for m in history if "merged" in m.lower() and OLD_MBID in m and NEW_MBID in m], (
        history
    )
    # And in the user's own language, in the feed — the ids above are forensics.
    feed = [e.message for e in activity_store.recent(50, source=activity_store.Source.ACTIVITY)]
    assert [m for m in feed if "merged" in m.lower()], feed


def test_a_merge_links_the_old_id_to_the_new_one(client, cfg, monkeypatch):
    """Everything recorded under the old id — the album's whole history, and any
    deep link already written into the feed — is orphaned without the alias."""
    d = _album(cfg)
    _redirects_to(monkeypatch, NEW_MBID)

    client.post(f"/retag/{_scanned(cfg, d).id}")

    assert activity_store.resolve_alias(OLD_MBID) == NEW_MBID


def test_a_second_retag_after_a_merge_reports_nothing(client, cfg, monkeypatch):
    """Idempotency, which is the invariant #32 is built on: the second pass over
    a merged album finds the id it asked for is the id it got, so there is
    nothing to name. Without it a nightly pass would re-announce the same merge
    every night for the rest of the album's life."""
    d = _album(cfg)
    _redirects_to(monkeypatch, NEW_MBID)

    client.post(f"/retag/{_scanned(cfg, d).id}")
    before = len([m for m in _messages() if "merged" in m.lower()])
    client.post(f"/retag/{_scanned(cfg, d).id}")

    assert before == 2, _messages()  # the activity entry and its audit line
    assert len([m for m in _messages() if "merged" in m.lower()]) == before, _messages()
    assert _scanned(cfg, d).state == AlbumState.COMPLETE


def test_an_ordinary_retag_says_nothing_about_a_merge(client, cfg, monkeypatch):
    """The control, and the invariant #32 cares about: don't narrate a no-op.
    A re-tag against a release that has not moved must not mention one."""
    d = _album(cfg)
    _redirects_to(monkeypatch, OLD_MBID)

    client.post(f"/retag/{_scanned(cfg, d).id}")

    assert not [m for m in _messages() if "merged" in m.lower()], _messages()
    assert activity_store.resolve_alias(OLD_MBID) is None
