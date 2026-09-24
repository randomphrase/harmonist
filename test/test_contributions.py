"""Contribution evidence, edition isolation, and the read-only refresh contract."""

from __future__ import annotations

import json
import shutil
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from mutagen.mp4 import MP4

from harmonist import activity_store, contributions, gardener, mb_cache, mb_lookup, scanner, sidecar
from harmonist.bandcamp_hook import write_sidecar_for_item
from harmonist.config import Config, PathsConfig
from harmonist.models import Album, AlbumState, BandcampInfo, Sidecar
from harmonist.web.main import _library_page_vars, create_app
from harmonist.web.scan_runner import ScanRunner
from test.test_bandcamp_hook import _bare_syncer, _StubItem
from test.test_gardener import _release

URL = "https://artist.bandcamp.com/album/record"
MBID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
NOW = datetime.now(UTC)


@pytest.fixture(autouse=True)
def store(tmp_path):
    activity_store.init(tmp_path / "activity.db")


def album(*, comment=True, downloaded=False, private=False, mbid=MBID):
    return Album(
        id=mbid,
        path=Path("/unused"),
        title="Record",
        artist="Artist",
        track_count=1,
        state=AlbumState.COMPLETE,
        sidecar=Sidecar(
            mb_release_id=mbid,
            store_url=URL,
            bandcamp=BandcampInfo(item_id=123, is_private=private),
            downloaded_at=NOW,
            bandcamp_downloaded=downloaded,
        ),
        bandcamp_comment_urls=(URL,) if comment else (),
    )


def release(formats=("Digital Media",), urls=(), mbid=MBID):
    r = _release(mbid=mbid)
    r["medium-list"] = [{"format": fmt} for fmt in formats]
    r["url-relation-list"] = [{"target": url, "type": "purchase for download"} for url in urls]
    return r


@pytest.mark.parametrize(
    ("formats", "urls", "media", "missing"),
    [
        (("Digital Media",), (URL,), False, False),
        (("Digital Media",), (), False, True),
        (("CD",), (URL,), True, False),
        (("CD",), (), True, True),
        (("CD", "Digital Media"), (URL,), True, False),
        (("",), (), None, True),
        ((), (URL,), None, False),
    ],
)
def test_independent_media_and_url_findings(formats, urls, media, missing):
    a = album()
    contributions.observe(a, release(formats, urls), NOW)
    result = contributions.assess(a)
    assert (result.media_mismatch, result.missing_url) == (media, missing)


def test_purchase_ownership_and_legacy_timestamp_are_not_file_provenance():
    a = album(comment=False)
    contributions.observe(a, release(("CD",)), NOW)
    assert not contributions.assess(a).eligible
    assert a.sidecar is not None
    a.sidecar = replace(
        a.sidecar, bandcamp_downloaded=True, store_url="https://music.example/album/x"
    )
    result = contributions.assess(a)
    assert result.eligible and result.media_mismatch and result.missing_url
    assert result.store_url == "https://music.example/album/x"


def test_private_cd_mix_is_reviewed_without_publishing_its_url():
    a = album(private=True)
    contributions.observe(a, release(("CD",)), NOW)
    result = contributions.assess(a)
    assert result.media_mismatch and result.private
    assert result.missing_url is None


def test_url_identity_is_host_scoped_and_comment_evidence_wins():
    a = album()
    assert a.sidecar is not None
    a.sidecar = replace(a.sidecar, store_url="https://other.bandcamp.com/album/record")
    contributions.observe(a, release(urls=(a.sidecar.store_url,)), NOW)
    assert contributions.assess(a).missing_url
    contributions.observe(
        a, release(urls=("http://ARTIST.bandcamp.com/album/record/?from=fan#x",)), NOW
    )
    assert contributions.assess(a).missing_url is False
    a.bandcamp_comment_urls = (URL, "https://artist.bandcamp.com/album/another")
    assert contributions.assess(a).store_url is None
    a.bandcamp_comment_urls = ("https://artist.bandcamp.com",)
    a.sidecar = replace(a.sidecar, store_url="https://artist.bandcamp.com")
    assert contributions.assess(a).eligible
    assert contributions.assess(a).store_url is None
    assert contributions.assess(a).missing_url is None


def test_identity_change_drops_old_observation_and_unknown_is_not_clean():
    a = album()
    assert contributions.assess(a).observation is None
    contributions.observe(a, release(urls=(URL,)), NOW)
    assert contributions.assess(a).missing_url is False
    a.sidecar = replace(a.sidecar, mb_release_id="new-edition")
    assert contributions.assess(a).observation is None
    assert contributions.assess(a).missing_url is None


def test_only_successful_download_records_provenance(tmp_path, monkeypatch):
    item = _StubItem(item_id=123, item_url=URL)
    d = tmp_path / "album"
    d.mkdir()
    write_sidecar_for_item(item, d)
    assert not sidecar.read(d).bandcamp_downloaded
    syncer = _bare_syncer()
    syncer.ignores.is_ignored.return_value = False
    syncer.local_media.get_path_for_purchase.return_value = d
    # Exercise the successful-download seam, including the caller's flag.
    from harmonist import library_index

    library_index.clear()
    monkeypatch.setattr("harmonist.bandcamp_hook._BCSyncer.sync_item", lambda *args: True)
    syncer.sync_item(item, "flac")
    assert sidecar.read(d).bandcamp_downloaded
    write_sidecar_for_item(item, d)
    assert sidecar.read(d).bandcamp_downloaded  # subsequent linking preserves it
    assert (
        sum(
            "bandcamp_downloaded=False->True" in event.message
            for event in activity_store.recent(50)
        )
        == 1
    )
    saved = json.loads((d / ".harmonist.json").read_text())
    assert saved["schema_version"] == 1 and saved["bandcamp_downloaded"] is True
    d2 = tmp_path / "old"
    d2.mkdir()
    sidecar.write(d2, Sidecar(mb_release_id=MBID))
    assert "bandcamp_downloaded" not in json.loads((d2 / ".harmonist.json").read_text())
    assert not sidecar.read(d2).bandcamp_downloaded


def make_files(root, name, mbid=MBID, *, comment=URL, private=False):
    d = root / name
    d.mkdir(parents=True)
    f = d / "track.m4a"
    shutil.copy(Path(__file__).parent / "fixtures/sine.m4a", f)
    tags = MP4(f)
    tags["----:com.apple.iTunes:MusicBrainz Album Id"] = [mbid.encode()]
    tags["----:com.apple.iTunes:MusicBrainz Release Track Id"] = [b"track-id"]
    tags["\xa9alb"] = [name]
    tags["\xa9cmt"] = [comment]
    tags.save()
    sidecar.write(
        d,
        Sidecar(
            mb_release_id=mbid,
            store_url=URL,
            bandcamp=BandcampInfo(item_id=123, is_private=private),
        ),
    )
    return d


def test_restart_rebuild_and_library_filters_make_no_mb_calls(tmp_path, monkeypatch):
    root = tmp_path / "music"
    make_files(root, "Download")
    make_files(root, "CD rip", comment="")
    make_files(root, "Reissue", mbid="another-release")
    fetch = Mock(return_value=release(("CD",)))
    monkeypatch.setattr(mb_lookup, "fetch_release", fetch)
    mb_cache.fetch_release(MBID)
    activity_store.init(tmp_path / "activity.db")
    albums = scanner.scan(root)
    assert len(albums) == 3
    assert len({a.id for a in albums}) == 3
    view = _library_page_vars(albums, 1, 30, filter_="mb-contributions")
    assert [a.title for a in view["rows"]] == ["Download"]
    assert fetch.call_count == 1


def test_contribution_filter_includes_either_reason_and_counts_each_copy_once():
    albums = []
    for name, formats, urls in (
        ("Media only", ("CD",), (URL,)),
        ("URL only", ("Digital Media",), ()),
        ("Both", ("CD",), ()),
        ("Clean", ("Digital Media",), (URL,)),
        ("Unchecked", None, ()),
    ):
        a = album()
        a.id = a.title = name
        if formats is not None:
            contributions.observe(a, release(formats, urls), NOW)
        albums.append(a)
    view = _library_page_vars(albums, 1, 30, filter_="mb-contributions")
    assert {a.title for a in view["rows"]} == {"Media only", "URL only", "Both"}
    option = next(f for f in view["filters"] if f["slug"] == "mb-contributions")
    assert option["count"] == view["total_shown"] == 3


def test_url_only_edit_does_not_unmute_tag_update():
    before = release()
    after = release(urls=(URL,))
    assert gardener.release_version(before) == gardener.release_version(after)


def test_gardener_updates_contributions_even_when_tags_are_unchanged(monkeypatch):
    a = album()
    before = release()
    activity_store.store_release(MBID, mb_cache._key(mb_lookup.RELEASE_INCLUDES), before)
    contributions.warm(a)
    assert contributions.assess(a).missing_url
    fetch = Mock(return_value=release(urls=(URL,)))
    monkeypatch.setattr(mb_lookup, "fetch_release", fetch)
    monkeypatch.setattr(
        gardener, "plan_for", Mock(side_effect=AssertionError("no tag work needed"))
    )
    for _ in range(2):
        gardener.sweep([a], recheck_after=timedelta(0), limit=1)
        assert contributions.assess(a).missing_url is False
    assert fetch.call_count == 2


def test_old_includes_cache_does_not_claim_urls_were_checked(monkeypatch):
    a = album()
    legacy = tuple(i for i in mb_lookup.RELEASE_INCLUDES if i != "url-rels")
    activity_store.store_release(MBID, mb_cache._key(legacy), release(("CD",)))
    fetch = Mock(side_effect=AssertionError("warming must not fetch"))
    monkeypatch.setattr(mb_lookup, "fetch_release", fetch)
    contributions.warm(a)
    assert contributions.assess(a).observation is None
    assert contributions.assess(a).missing_url is None
    assert fetch.call_count == 0


def test_combined_parts_do_not_spread_download_provenance():
    downloaded = Sidecar(mb_release_id=MBID, bandcamp_downloaded=True)
    adopted = Sidecar(mb_release_id=MBID)
    assert not scanner._merge_sidecars([downloaded, adopted], MBID).bandcamp_downloaded
    assert not scanner._merge_sidecars([downloaded, None], MBID).bandcamp_downloaded
    assert scanner._merge_sidecars([downloaded, downloaded], MBID).bandcamp_downloaded


def test_multi_folder_album_keeps_fresh_observation_after_cached_rescan(tmp_path):
    root = tmp_path / "music"
    make_files(root, "Disc 1")
    second = make_files(root, "Disc 2")
    tags = MP4(second / "track.m4a")
    tags["----:com.apple.iTunes:MusicBrainz Release Track Id"] = [b"other-track"]
    tags.save()
    activity_store.store_release(MBID, mb_cache._key(mb_lookup.RELEASE_INCLUDES), release())
    runner = ScanRunner(root)
    runner.refresh_now()
    assert len(runner.albums()) == 1
    a = runner.albums()[0]
    assert len(a.folders) == 2 and contributions.assess(a).missing_url
    contributions.observe(a, release(urls=(URL,)), datetime.now(UTC) + timedelta(seconds=1))
    runner.refresh_now()
    assert contributions.assess(runner.albums()[0]).missing_url is False


def test_fresh_check_changes_only_observation_and_preserves_failures(tmp_path, monkeypatch):
    cfg = Config(paths=PathsConfig(music_dir=tmp_path / "music", config_dir=tmp_path / "config"))
    cfg.paths.config_dir.mkdir()
    d = make_files(cfg.paths.music_dir, "Download")
    make_files(cfg.paths.music_dir, "CD rip", comment="")
    app = create_app(cfg)
    client = TestClient(app, headers={"HX-Request": "true"})
    a = next(a for a in app.state.scan_runner.scan_now() if a.path == d)
    initial = {p: p.read_bytes() for p in cfg.paths.music_dir.rglob("*") if p.is_file()}
    fetch = Mock(return_value=release(("CD",)))
    monkeypatch.setattr(mb_lookup, "fetch_release", fetch)
    response = client.get(f"/library/{a.id}/compare?reread=1")
    assert response.status_code == 200
    assert "Possible media mismatch" in response.text
    assert f'hx-get="/library/{a.id}/contributions/editions" hx-trigger="load"' in response.text
    fetch.return_value = release(urls=(URL,))
    response = client.get(f"/library/{a.id}/compare?reread=1")
    assert "Digital Media and store URL agree" in response.text
    fetch.side_effect = mb_lookup.MBError("offline")
    response = client.get(f"/library/{a.id}/compare?reread=1")
    assert "Couldn't read MusicBrainz again" in response.text
    assert "Digital Media and store URL agree" in response.text
    assert fetch.call_count == 3  # explicit check bypasses the fresh TTL every time
    assert initial == {p: p.read_bytes() for p in initial}
    assert len(scanner.scan(cfg.paths.music_dir)) == 2


def test_private_refresh_keeps_manual_review_without_harmony(tmp_path, monkeypatch):
    cfg = Config(paths=PathsConfig(music_dir=tmp_path / "music", config_dir=tmp_path / "config"))
    cfg.paths.config_dir.mkdir()
    d = make_files(cfg.paths.music_dir, "Private CD mix", private=True)
    app = create_app(cfg)
    client = TestClient(app, headers={"HX-Request": "true"})
    a = next(a for a in app.state.scan_runner.scan_now() if a.path == d)
    monkeypatch.setattr(mb_lookup, "fetch_release", Mock(return_value=release(("CD",))))
    response = client.get(f"/library/{a.id}/compare?reread=1")
    assert "Private Bandcamp download" in response.text
    assert f'hx-get="/library/{a.id}/contributions/editions" hx-trigger="load"' in response.text
    assert "Store URL missing from MusicBrainz" not in response.text
