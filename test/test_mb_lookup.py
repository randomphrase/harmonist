"""Tests for mb_lookup — uses monkeypatch to stub musicbrainzngs."""

from __future__ import annotations

from unittest.mock import MagicMock

import musicbrainzngs
import pytest

from harmonist import mb_lookup
from harmonist.mb_lookup import (
    MBError,
    _media_summary,
    browse_release_group_releases,
    candidate_summaries_for_url,
    fetch_release,
    fetch_release_urls,
    lookup_by_bandcamp_url,
    release_summary,
)

# ---------- configure ----------


def test_configure_parses_user_agent(monkeypatch):
    seen = {}

    def fake_set_useragent(name, version, contact):
        seen["name"] = name
        seen["version"] = version
        seen["contact"] = contact

    monkeypatch.setattr(musicbrainzngs, "set_useragent", fake_set_useragent)
    mb_lookup.configure("Harmonist/0.1 ( harmonist@girtby.net )")
    assert seen == {
        "name": "Harmonist",
        "version": "0.1",
        "contact": "harmonist@girtby.net",
    }


def test_configure_rejects_malformed_user_agent():
    with pytest.raises(ValueError, match="user_agent must look like"):
        mb_lookup.configure("not a valid user agent")


# ---------- lookup_by_bandcamp_url ----------


def test_lookup_returns_mbid_when_url_linked(monkeypatch):
    response = {
        "url": {
            "id": "url-aaa",
            "resource": "https://x.bandcamp.com/album/y",
            "release-relation-list": [
                {"release": {"id": "rel-aaa", "title": "Album"}},
            ],
        }
    }
    monkeypatch.setattr(musicbrainzngs, "browse_urls", lambda **kw: response)

    mbids = lookup_by_bandcamp_url("https://x.bandcamp.com/album/y")
    assert mbids == ["rel-aaa"]


def test_lookup_returns_all_releases_when_multiple_linked(monkeypatch):
    """One Bandcamp URL can be attached to several MB releases (e.g. a long
    digital edition + a shorter CD mix). All linked MBIDs must come back so
    the caller can disambiguate by tracklist."""
    response = {
        "url": {
            "release-relation-list": [
                {"release": {"id": "rel-aaa"}},
                {"release": {"id": "rel-bbb"}},
            ],
        }
    }
    monkeypatch.setattr(musicbrainzngs, "browse_urls", lambda **kw: response)
    assert lookup_by_bandcamp_url("https://x.bandcamp.com/album/y") == ["rel-aaa", "rel-bbb"]


def test_lookup_dedupes_release_listed_via_multiple_relationships(monkeypatch):
    """A release linked to the URL via more than one relationship comes back
    repeatedly — dedupe (order-preserving) so the picker doesn't show dupes."""
    response = {
        "url": {
            "release-relation-list": [
                {"release": {"id": "rel-aaa"}},
                {"release": {"id": "rel-bbb"}},
                {"release": {"id": "rel-aaa"}},  # same release, second relationship
            ],
        }
    }
    monkeypatch.setattr(musicbrainzngs, "browse_urls", lambda **kw: response)
    assert lookup_by_bandcamp_url("https://x.bandcamp.com/album/y") == ["rel-aaa", "rel-bbb"]


def test_browse_release_group_releases(monkeypatch):
    """Returns each sibling release in the group as (mbid, [url targets])."""
    response = {
        "release-list": [
            {
                "id": "rel-standard",
                "url-relation-list": [{"target": "https://x.bandcamp.com/album/y"}],
            },
            {
                "id": "rel-24bit",
                "url-relation-list": [{"target": "https://x.bandcamp.com/album/y-24bit"}],
            },
            {"id": "rel-no-urls"},  # tolerated: no url-relation-list
        ]
    }
    captured = {}

    def fake_browse(**kw):
        captured.update(kw)
        return response

    monkeypatch.setattr(musicbrainzngs, "browse_releases", fake_browse)

    out = browse_release_group_releases("rg-1")
    assert captured["release_group"] == "rg-1"
    assert "url-rels" in captured["includes"]
    assert out == [
        ("rel-standard", ["https://x.bandcamp.com/album/y"]),
        ("rel-24bit", ["https://x.bandcamp.com/album/y-24bit"]),
        ("rel-no-urls", []),
    ]


def test_browse_release_group_releases_raises_on_error(monkeypatch):
    def boom(**kw):
        raise musicbrainzngs.NetworkError("down")

    monkeypatch.setattr(musicbrainzngs, "browse_releases", boom)
    with pytest.raises(MBError):
        browse_release_group_releases("rg-1")


# ---------- release summaries / store-URL picker ----------


def test_media_summary_groups_and_counts_formats():
    cross = "\N{MULTIPLICATION SIGN}"
    assert _media_summary([]) == ""
    assert _media_summary([{"format": "CD"}]) == "CD"
    assert _media_summary([{"format": "CD"}, {"format": "CD"}]) == f"2{cross}CD"
    assert _media_summary([{"format": "CD"}, {"format": "Digital Media"}]) == "CD + Digital Media"
    assert _media_summary([{}]) == "Media"  # missing format tolerated


def test_release_summary_shape():
    release = {
        "id": "rel-1",
        "title": "Life²",
        "disambiguation": "24-bit",
        "artist-credit-phrase": "Asura",
        "status": "Official",
        "date": "2010",
        "country": "FR",
        "medium-list": [
            {"format": "CD", "track-count": 6},
            {"format": "CD", "track-list": [{}, {}, {}, {}, {}]},
        ],
        "label-info-list": [{"label": {"name": "Ultimae"}, "catalog-number": "ULT-1"}],
    }
    s = release_summary(release)
    cross = "\N{MULTIPLICATION SIGN}"
    assert s["id"] == "rel-1"
    assert s["title"] == "Life²"
    assert s["disambiguation"] == "24-bit"
    assert s["artist"] == "Asura"
    assert s["track_count"] == 11  # 6 + 5
    assert s["media"] == f"2{cross}CD"
    assert s["label"] == "Ultimae"
    assert s["catalog_number"] == "ULT-1"


def test_candidate_summaries_for_url_caps_and_reports_total(monkeypatch):
    # 7 linked releases; the picker caps at MAX_URL_CANDIDATES (5) but reports
    # the true total so the UI can say "showing 5 of 7".
    mbids = [f"rel-{i}" for i in range(7)]
    monkeypatch.setattr(mb_lookup, "lookup_by_bandcamp_url", lambda url: mbids)
    monkeypatch.setattr(
        mb_lookup,
        "fetch_release",
        lambda m: {"id": m, "title": "T", "medium-list": [{"format": "CD"}]},
    )
    summaries, total = candidate_summaries_for_url("https://x.bandcamp.com/album/y")
    assert total == 7
    assert len(summaries) == mb_lookup.MAX_URL_CANDIDATES
    assert [s["id"] for s in summaries] == mbids[:5]


def test_lookup_returns_empty_when_no_relations(monkeypatch):
    response = {"url": {"id": "url-aaa", "release-relation-list": []}}
    monkeypatch.setattr(musicbrainzngs, "browse_urls", lambda **kw: response)
    assert lookup_by_bandcamp_url("https://x.bandcamp.com/album/y") == []


def test_lookup_returns_empty_when_url_unknown_404(monkeypatch):
    cause = MagicMock()
    cause.code = 404

    def raise_404(**kw):
        err = musicbrainzngs.ResponseError(cause=cause)
        raise err

    monkeypatch.setattr(musicbrainzngs, "browse_urls", raise_404)
    assert lookup_by_bandcamp_url("https://x.bandcamp.com/album/y") == []


class _Cause:
    """Deterministic stand-in for a urllib HTTPError cause (a MagicMock's str
    contains its object id, which can incidentally include '404' — flaky)."""

    def __init__(self, code: int | None, text: str):
        self.code = code
        self._text = text

    def __str__(self) -> str:
        return self._text


def _raise_response_error(cause: _Cause):
    def _raise(**kw):
        raise musicbrainzngs.ResponseError(cause=cause)

    return _raise


def test_lookup_raises_on_non_404_response_error(monkeypatch):
    monkeypatch.setattr(
        musicbrainzngs, "browse_urls", _raise_response_error(_Cause(500, "Server Error"))
    )
    with pytest.raises(MBError):
        lookup_by_bandcamp_url("https://x.bandcamp.com/album/y")


def test_lookup_buried_404_in_number_is_not_treated_as_not_found(monkeypatch):
    """A 404 embedded in a longer number (object id, MBID, …) must NOT look
    like an HTTP 404 — it should still raise."""
    monkeypatch.setattr(
        musicbrainzngs, "browse_urls", _raise_response_error(_Cause(500, "id 12340456 failed"))
    )
    with pytest.raises(MBError):
        lookup_by_bandcamp_url("https://x.bandcamp.com/album/y")


def test_lookup_standalone_404_message_is_not_found(monkeypatch):
    """No structured code, but a standalone 404 in the message → treat as a
    'not found' (return None), not an error."""
    monkeypatch.setattr(
        musicbrainzngs,
        "browse_urls",
        _raise_response_error(_Cause(None, "HTTP Error 404: Not Found")),
    )
    assert lookup_by_bandcamp_url("https://x.bandcamp.com/album/y") == []


def test_lookup_raises_on_network_error(monkeypatch):
    def raise_net(**kw):
        raise musicbrainzngs.NetworkError(cause=Exception("connection refused"))

    monkeypatch.setattr(musicbrainzngs, "browse_urls", raise_net)
    with pytest.raises(MBError):
        lookup_by_bandcamp_url("https://x.bandcamp.com/album/y")


def test_lookup_passes_correct_args(monkeypatch):
    seen = {}

    def fake_browse(**kw):
        seen.update(kw)
        return {"url": {}}

    monkeypatch.setattr(musicbrainzngs, "browse_urls", fake_browse)
    lookup_by_bandcamp_url("https://x.bandcamp.com/album/y")
    assert seen["resource"] == "https://x.bandcamp.com/album/y"
    assert "release-rels" in seen["includes"]


# ---------- fetch_release ----------


def test_fetch_release_unwraps_response(monkeypatch):
    response = {
        "release": {
            "id": "rel-aaa",
            "title": "Test Album",
            "medium-list": [],
        }
    }
    monkeypatch.setattr(musicbrainzngs, "get_release_by_id", lambda mbid, **kw: response)
    release = fetch_release("rel-aaa")
    assert release["id"] == "rel-aaa"
    assert release["title"] == "Test Album"


def test_fetch_release_passes_correct_includes(monkeypatch):
    seen = {}

    def fake_get(mbid, **kw):
        seen["mbid"] = mbid
        seen["includes"] = kw.get("includes", [])
        return {"release": {"id": mbid}}

    monkeypatch.setattr(musicbrainzngs, "get_release_by_id", fake_get)
    fetch_release("rel-aaa")
    assert seen["mbid"] == "rel-aaa"
    for inc in ("artist-credits", "recordings", "release-groups", "labels", "media"):
        assert inc in seen["includes"]


def test_fetch_release_raises_on_error(monkeypatch):
    def raise_err(mbid, **kw):
        raise musicbrainzngs.NetworkError(cause=Exception("boom"))

    monkeypatch.setattr(musicbrainzngs, "get_release_by_id", raise_err)
    with pytest.raises(MBError):
        fetch_release("rel-aaa")


# ---------- fetch_release_urls ----------


def test_fetch_release_urls_extracts_targets(monkeypatch):
    response = {
        "release": {
            "id": "rel-aaa",
            "url-relation-list": [
                {"type": "purchase for download", "target": "https://x.bandcamp.com/album/y"},
                {"type": "discogs", "target": "https://www.discogs.com/release/123"},
                {"type": "stream for free", "target": "https://soundcloud.com/x/y"},
            ],
        }
    }
    monkeypatch.setattr(musicbrainzngs, "get_release_by_id", lambda mbid, **kw: response)
    urls = fetch_release_urls("rel-aaa")
    assert urls == [
        "https://x.bandcamp.com/album/y",
        "https://www.discogs.com/release/123",
        "https://soundcloud.com/x/y",
    ]


def test_fetch_release_urls_empty_when_no_relations(monkeypatch):
    monkeypatch.setattr(
        musicbrainzngs,
        "get_release_by_id",
        lambda mbid, **kw: {"release": {"id": mbid}},
    )
    assert fetch_release_urls("rel-aaa") == []


def test_fetch_release_urls_passes_url_rels_include(monkeypatch):
    seen = {}

    def fake(mbid, **kw):
        seen["includes"] = kw.get("includes", [])
        return {"release": {"id": mbid}}

    monkeypatch.setattr(musicbrainzngs, "get_release_by_id", fake)
    fetch_release_urls("rel-aaa")
    assert seen["includes"] == ["url-rels"]


def test_fetch_release_urls_raises_on_error(monkeypatch):
    def explode(mbid, **kw):
        raise musicbrainzngs.NetworkError(cause=Exception("boom"))

    monkeypatch.setattr(musicbrainzngs, "get_release_by_id", explode)
    with pytest.raises(MBError):
        fetch_release_urls("rel-aaa")
