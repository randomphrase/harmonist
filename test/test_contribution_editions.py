"""Scoped digital-edition discovery, bounded requests and honest uncertainty."""

from unittest.mock import Mock

import musicbrainzngs
import pytest
from fastapi.testclient import TestClient

from harmonist import activity_store, mb_cache, mb_lookup
from harmonist.config import Config, PathsConfig
from harmonist.web.main import create_app
from test.test_contributions import MBID, URL, make_files, release


@pytest.fixture
def library(tmp_path):
    cfg = Config(paths=PathsConfig(music_dir=tmp_path / "music", config_dir=tmp_path / "config"))
    cfg.paths.config_dir.mkdir()
    activity_store.init(tmp_path / "activity.db")
    make_files(cfg.paths.music_dir, "Download")
    make_files(cfg.paths.music_dir, "Reissue", mbid="other-edition")
    client = TestClient(create_app(cfg), headers={"HX-Request": "true"})
    activity_store.store_release(MBID, mb_cache._key(mb_lookup.RELEASE_INCLUDES), release(("CD",)))
    return client, cfg.paths.music_dir


def test_discovery_is_scoped_read_only_fresh_and_keeps_multiple_editions(library, monkeypatch):
    client, root = library
    linked = release(urls=(URL,), mbid="digital")
    unlinked = release(mbid="digital-reissue")
    browse = Mock(
        return_value={
            "release-list": [release(("CD", "Digital Media")), linked, unlinked],
            "release-count": 3,
        }
    )
    monkeypatch.setattr(musicbrainzngs, "browse_releases", browse)
    fetch = Mock(side_effect=AssertionError("the stored release already supplies its group"))
    monkeypatch.setattr(mb_lookup, "fetch_release", fetch)
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    for _ in range(2):
        r = client.get(f"/library/{MBID}/contributions/editions")
        assert r.status_code == 200
        assert 'release/digital"' in r.text and 'release/digital-reissue"' in r.text
        assert "Links to this download" in r.text and "Store link missing" in r.text
        assert "Create digital release with Harmony" not in r.text
        assert f'release/{MBID}"' not in r.text
        assert "Import another digital edition with Harmony" in r.text
    assert browse.call_count == 2
    assert browse.call_args.kwargs["release_group"] == "rg-aaa"
    assert browse.call_args.kwargs["limit"] == 100
    assert {"url-rels", "media"} <= set(browse.call_args.kwargs["includes"])
    assert fetch.call_count == 0
    assert before == {p: p.read_bytes() for p in before}


@pytest.mark.parametrize(
    "kind", ["absent", "unknown", "truncated", "failure", "private", "private-digital"]
)
def test_only_complete_public_absence_offers_harmony(library, monkeypatch, kind):
    client, root = library
    if kind.startswith("private"):
        from dataclasses import replace

        from harmonist import sidecar

        folder = root / "Download"
        sc = sidecar.read(folder)
        assert sc is not None and sc.bandcamp is not None
        sidecar.write(folder, replace(sc, bandcamp=replace(sc.bandcamp, is_private=True)))
    responses = {
        "release-list": [
            release(
                ("",)
                if kind == "unknown"
                else ("Digital Media",)
                if kind == "private-digital"
                else ("CD",)
            )
        ],
        "release-count": 101 if kind == "truncated" else 1,
    }
    browse = Mock(return_value=responses)
    if kind == "failure":
        browse.side_effect = musicbrainzngs.NetworkError("offline")
    monkeypatch.setattr(musicbrainzngs, "browse_releases", browse)
    r = client.get(f"/library/{MBID}/contributions/editions")
    assert r.status_code == 200
    assert ("Create digital release with Harmony" in r.text) is (kind == "absent")
    if kind == "absent":
        assert "No confirmed digital editions found" in r.text
    elif kind in {"unknown", "truncated"}:
        assert "search is incomplete" in r.text
    elif kind == "failure":
        assert "Could not check digital editions" in r.text
        assert "No confirmed digital editions found" not in r.text
    else:
        assert URL not in r.text and "harmony.pulsewidth" not in r.text
    assert browse.call_count == 1


def test_no_cached_group_costs_one_release_fetch_and_one_browse(library, monkeypatch):
    client, _root = library
    fetch = Mock(return_value=release(("CD",), mbid="other-edition"))
    browse = Mock(return_value={"release-list": [], "release-count": 0})
    monkeypatch.setattr(mb_lookup, "fetch_release", fetch)
    monkeypatch.setattr(musicbrainzngs, "browse_releases", browse)
    response = client.get("/library/other-edition/contributions/editions")
    assert response.status_code == 200 and "Create digital release with Harmony" in response.text
    assert fetch.call_count == browse.call_count == 1


@pytest.mark.parametrize("missing_group", [True, False])
def test_unknown_or_changed_group_does_not_start_an_unscoped_search(
    library, monkeypatch, missing_group
):
    client, _root = library
    payload = release(mbid="other-edition" if missing_group else "merged-id")
    if missing_group:
        payload.pop("release-group")
    monkeypatch.setattr(mb_lookup, "fetch_release", Mock(return_value=payload))
    browse = Mock(side_effect=AssertionError("no group to search"))
    monkeypatch.setattr(musicbrainzngs, "browse_releases", browse)
    response = client.get("/library/other-edition/contributions/editions")
    assert response.status_code == 200
    assert (
        "not supplied a release group" if missing_group else "merged this release"
    ) in response.text
    assert browse.call_count == 0
