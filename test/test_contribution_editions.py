"""Scoped digital-edition discovery, bounded requests and honest uncertainty."""

from unittest.mock import Mock

import musicbrainzngs
import pytest
from fastapi.testclient import TestClient

from harmonist import activity_store, mb_cache, mb_lookup
from harmonist.config import Config, PathsConfig
from harmonist.web.main import create_app
from test.test_contributions import MBID, URL, make_files, release
from test.test_gardener import _release
from test.test_web import _confirmation_fields


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


def test_contributions_resolve_media_before_link_and_explain_unknown_privacy(library):
    from bs4 import BeautifulSoup

    client, _ = library
    for fmt, expected in [
        ("CD", "Possible media mismatch"),
        ("Digital Media", "Store URL missing"),
    ]:
        activity_store.store_release(
            MBID, mb_cache._key(mb_lookup.RELEASE_INCLUDES), release((fmt,))
        )
        page = BeautifulSoup(client.get(f"/album/{MBID}").text, "html.parser")
        panel = page.select_one(f"#album-contributions-{MBID}")
        assert expected in panel.text
        if fmt == "CD":
            assert "Store URL missing" not in panel.text
        else:
            assert "public release page" in panel.text
            assert panel.select_one(f'a[href="https://musicbrainz.org/release/{MBID}/edit"]')
            url_input = panel.select_one("input[readonly]")
            assert url_input is not None and url_input["value"] == URL
            assert panel.select_one(f"#contribution-editions-{MBID}") is None


@pytest.mark.parametrize(
    "kind", ["unique", "ambiguous", "count", "unlinked", "unknown", "truncated"]
)
def test_suggestion_requires_one_exact_url_and_matching_count(library, monkeypatch, kind):
    from bs4 import BeautifulSoup

    client, _ = library
    first = release(urls=() if kind == "unlinked" else (URL,), mbid="first")
    first["medium-list"][0]["track-count"] = 2 if kind == "count" else 1
    second = release(urls=(URL,) if kind == "ambiguous" else (), mbid="second")
    second["medium-list"][0]["track-count"] = 1
    releases = [second, first]
    if kind == "unknown":
        releases.append(release(("",), mbid="unknown"))
    browse = Mock(
        return_value={
            "release-list": releases,
            "release-count": 101 if kind == "truncated" else len(releases),
        }
    )
    monkeypatch.setattr(musicbrainzngs, "browse_releases", browse)
    page = BeautifulSoup(client.get(f"/library/{MBID}/contributions/editions").text, "html.parser")
    suggestion = page.select_one('tbody tr[aria-label="Suggested digital release"]')
    assert bool(suggestion) is (kind == "unique")
    if suggestion:
        assert "Suggested" in suggestion.text
        assert "bg-amber-50/40" in suggestion["class"]
    assert len(page.select("tbody tr")) == 2
    assert len(page.select("tbody button[hx-get]")) == 2
    assert "Digital Media" in page.text
    assert browse.call_count == 1


@pytest.mark.parametrize("outcome", ["apply", "cancel", "failure", "changed"])
def test_replacement_review_keeps_original_until_confirmed(library, monkeypatch, outcome):
    from dataclasses import replace

    from harmonist import sidecar

    client, root = library
    if outcome != "changed":
        make_files(root, "Another copy", mbid=MBID)
    aid = next(a.id for a in client.app.state.scan_runner.scan_now() if a.path == root / "Download")
    replacement = _release(mbid="digital")
    fetch = Mock(return_value=replacement)
    monkeypatch.setattr(mb_lookup, "fetch_release", fetch)
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    query = f"replacement={MBID}:digital"
    editor = client.get(f"/assignments/{aid}?{query}&cancel=true&on_album_page=true")
    assert editor.status_code == 200
    fields = _confirmation_fields(editor.text)
    assert fields["candidate_mbid"] == "digital"
    assert "Confirm suggestion" in editor.text
    assert before == {p: p.read_bytes() for p in before}
    assert fetch.call_count == 1
    fetch.side_effect = AssertionError("continuing a reviewed replacement must not fetch")
    if outcome == "cancel":
        return  # closing this read-only editor discards its page-local choice
    if outcome == "changed":
        sc = sidecar.read(root / "Download")
        assert sc is not None
        sidecar.write(root / "Download", replace(sc, mb_release_id="other"))
    if outcome == "failure":
        monkeypatch.setattr(
            "harmonist.web.main._tag_with_release", Mock(side_effect=OSError("disk full"))
        )
    response = client.post(f"/confirm/{aid}/accept?{query}", data=fields)
    if outcome == "apply":
        assert "confirmation-applied" in response.headers.get("HX-Trigger", "")
        assert sidecar.read(root / "Download").mb_release_id == "digital"
        assert "Store URL missing" in client.get("/album/digital").text
        after = {p: p.read_bytes() for p in before}
        repeated = client.post(f"/confirm/{aid}/accept?{query}", data=fields)
        assert "confirmation-applied" not in repeated.headers.get("HX-Trigger", "")
        assert after == {p: p.read_bytes() for p in after}
    elif outcome == "failure":
        assert "disk full" in response.text
        assert before == {p: p.read_bytes() for p in before}
    else:
        assert response.status_code == 200 and "album&#39;s release changed" in response.text
        assert sidecar.read(root / "Download").mb_release_id == "other"
    assert all(p.read_bytes() == data for p, data in before.items() if "Download" not in p.parts)
    assert fetch.call_count == 1


@pytest.mark.parametrize("kind", ["network", "group", "physical", "merged", "missing-cache"])
def test_unavailable_replacement_is_visible_without_mutation(library, monkeypatch, kind):
    client, root = library
    replacement = _release(mbid="digital")
    if kind == "group":
        replacement["release-group"]["id"] = "different-group"
    elif kind == "physical":
        replacement["medium-list"][0]["format"] = "CD"
    elif kind == "merged":
        replacement["id"] = "merged-digital"
    fetch = Mock(return_value=replacement)
    if kind == "network":
        fetch.side_effect = mb_lookup.MBError("offline")
    monkeypatch.setattr(mb_lookup, "fetch_release", fetch)
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    query = f"replacement={MBID}:digital"
    if kind == "missing-cache":
        # Continuation cannot repopulate a vanished review snapshot.
        response = client.post(
            f"/confirm/{MBID}/accept?{query}",
            data={
                "disk_order": "0",
                "mb_order": "0",
                "disk_fingerprint": "old",
                "release_fingerprint": "old",
            },
        )
        assert fetch.call_count == 0
    else:
        response = client.get(f"/assignments/{MBID}?{query}")
        assert fetch.call_count == 1
    assert "Review unavailable" in response.text
    assert response.headers["HX-Retarget"] == "#confirmation-modal"
    assert "confirmation-applied" not in response.headers.get("HX-Trigger", "")
    assert before == {p: p.read_bytes() for p in before}


def test_replacement_refresh_spends_one_request_even_when_uncached(library, monkeypatch):
    client, _ = library
    fetch = Mock(return_value=_release(mbid="digital"))
    monkeypatch.setattr(mb_lookup, "fetch_release", fetch)
    for count in (1, 2):
        response = client.get(f"/assignments/{MBID}?replacement={MBID}:digital&reread=true")
        assert response.status_code == 200
        assert _confirmation_fields(response.text)["candidate_mbid"] == "digital"
        assert fetch.call_count == count


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
        assert "Same URL" in r.text and "Not linked" in r.text
        assert f'release/{MBID}"' not in r.text
        assert "Add digital release with Harmony" in r.text
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
    assert ("Add digital release with Harmony" in r.text) is (kind == "absent")
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
    assert response.status_code == 200 and "Add digital release with Harmony" in response.text
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
