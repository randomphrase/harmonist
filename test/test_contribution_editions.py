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


def test_contributions_resolve_media_before_store_link(library):
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
    if kind == "missing-cache":
        assert 'role="alert"' in response.text
        assert response.headers["HX-Reswap"] == "innerHTML settle:0ms"
    else:
        assert "Review unavailable" in response.text
        assert "Cancel" in response.text
        assert "HX-Retarget" not in response.headers
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


def test_replacement_artwork_serves_reviewed_release_without_fetching(library, monkeypatch):
    from datetime import UTC, datetime

    from bs4 import BeautifulSoup

    from harmonist import cover_art, images
    from harmonist.web.main import _release_fingerprint
    from test.test_web import _png

    client, _ = library
    selected = _release(mbid="digital")
    activity_store.store_release("digital", mb_cache._key(mb_lookup.RELEASE_INCLUDES), selected)
    image = _png(77)
    cover_art.cache_image("digital", image, "image/png")
    # The cached bytes belong to a release-specific observation.
    activity_store.store_cover_art(
        "digital",
        activity_store.CachedCoverArt(
            fetched_at=datetime.now(UTC),
            image_url="https://caa.example/front.png",
            source="release",
        ),
    )
    fetch = Mock(side_effect=AssertionError("image requests must use the review cache"))
    monkeypatch.setattr(mb_lookup, "fetch_release", fetch)
    monkeypatch.setattr(cover_art, "fetch_image", fetch)
    query = f"replacement={MBID}:digital"
    response = client.get(
        f"/assignments/{MBID}/artwork?{query}"
        f"&release_fingerprint={_release_fingerprint(selected, 'digital')}"
    )
    page = BeautifulSoup(response.text, "html.parser")
    preview = page.find("img", alt="Artwork for selected release")
    assert preview is not None
    served = client.get(preview["src"])
    assert served.status_code == 200 and served.content == image
    assert fetch.call_count == 0
    assert client.get(f"/artwork/image/{MBID}/{images.digest(image)}").status_code == 404
    assert client.get(preview["src"].replace("digital", "unknown")).status_code == 404
    assert fetch.call_count == 0


@pytest.mark.parametrize("cached_group", [False, True])
@pytest.mark.parametrize("outcome", ["absent", "failure", "release", "changed"])
def test_sibling_artwork_is_release_specific_through_confirmation(
    library, monkeypatch, cached_group, outcome
):
    from datetime import UTC, datetime

    from bs4 import BeautifulSoup

    from harmonist import cover_art, formats, images, sidecar
    from test.test_web import _png

    client, root = library
    folder = root / "Download"
    track = next(folder.glob("*.m4a"))
    local, group, selected_image = _png(11), _png(22), _png(33)
    (folder / "cover.png").write_bytes(local)
    formats.write_cover(track, local)
    selected = _release(mbid="digital")
    monkeypatch.setattr(mb_lookup, "fetch_release", Mock(return_value=selected))
    query = f"replacement={MBID}:digital"
    editor = client.get(f"/assignments/{MBID}?{query}&cancel=true&on_album_page=true")
    fields = _confirmation_fields(editor.text)
    forbidden = Mock(side_effect=AssertionError("confirmation must not fetch"))
    monkeypatch.setattr(mb_lookup, "fetch_release", forbidden)

    def seed_group():
        cover_art.cache_image("digital", group, "image/png")
        activity_store.store_cover_art(
            "digital",
            activity_store.CachedCoverArt(
                fetched_at=datetime.now(UTC),
                image_url="https://caa.example/group.png",
                source="release-group",
                etag='"group"',
            ),
        )

    if cached_group:
        seed_group()
    calls = []

    def check(mbid, *, release_group_mbid=None, known=None, **kw):
        calls.append((mbid, release_group_mbid, known))
        if outcome == "failure":
            raise cover_art.CoverArtError("CAA unavailable")
        if release_group_mbid:
            seed_group()
            return activity_store.cached_cover_art("digital")
        if outcome in {"release", "changed"}:
            cover_art.cache_image(mbid, selected_image, "image/png")
            return activity_store.CachedCoverArt(
                fetched_at=datetime.now(UTC),
                image_url="https://caa.example/release.png",
                source="release",
            )
        # Leave old bytes present to test readers independently of cache eviction.
        return activity_store.CachedCoverArt(fetched_at=datetime.now(UTC))

    monkeypatch.setattr(cover_art, "check_front", check)
    monkeypatch.setattr(
        cover_art,
        "fetch_image",
        lambda mbid, url: cover_art.cache_image(mbid, selected_image, "image/png"),
    )
    artwork_url = (
        f"/assignments/{MBID}/artwork?{query}&release_fingerprint={fields['release_fingerprint']}"
    )
    for suffix in ("", "&reread=true"):
        response = client.get(artwork_url + suffix)
        assert response.status_code == 200
        page = BeautifulSoup(response.text, "html.parser")
        checkbox = page.select_one('[name="include_artwork"]')
        assert bool(checkbox) is (outcome in {"release", "changed"})
        assert (
            client.get(f"/artwork/image/{MBID}/{images.digest(group)}?{query}").status_code == 404
        )
        if outcome in {"release", "changed"}:
            preview = page.find("img", alt="Artwork for selected release")
            assert preview is not None
            assert client.get(preview["src"]).content == selected_image
            plan = page.select_one('[name="art_plan"]')
            assert plan is not None
            fields.update(include_artwork="true", art_plan=plan["value"])
        elif outcome == "failure":
            assert "Artwork could not be loaded" in page.text
            assert "No front cover" not in page.text
        else:
            assert "No front cover" in page.text
            assert "Existing images will be kept" in page.text
    assert len(calls) == 2
    assert all(mbid == "digital" and rg is None for mbid, rg, _ in calls)
    assert calls[0][2] is None
    if outcome == "changed":
        seed_group()  # Another ordinary artwork check replaced the reviewed cache.
    monkeypatch.setattr(cover_art, "check_front", forbidden)
    monkeypatch.setattr(cover_art, "fetch_image", forbidden)
    response = client.post(f"/confirm/{MBID}/accept?{query}", data=fields)
    if outcome == "changed":
        assert "Artwork changed since the review" in response.text
        assert sidecar.read(folder).mb_release_id == MBID
    else:
        assert "confirmation-applied" in response.headers.get("HX-Trigger", "")
        assert sidecar.read(folder).mb_release_id == "digital"
        assert formats.read_album_id(track) == "digital"
    expected = selected_image if outcome == "release" else local
    assert (folder / "cover.png").read_bytes() == expected
    embedded = formats.read_cover(track)
    assert embedded is not None and embedded[0] == expected
    assert forbidden.call_count == 0


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
        assert "Add Release" in r.text
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
    assert ("Add Release" in r.text) is (kind == "absent")
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
    assert response.status_code == 200 and "Add Release" in response.text
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
