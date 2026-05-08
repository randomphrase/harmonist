"""Tests for mb_search.search_releases."""
from __future__ import annotations

import musicbrainzngs
import pytest

from harmonist import mb_search
from harmonist.mb_search import MBSearchError, search_releases


def test_returns_results_with_expected_fields(monkeypatch):
    response = {
        "release-list": [
            {
                "id": "rel-aaa",
                "title": "Abbey Road",
                "artist-credit-phrase": "The Beatles",
                "date": "1969-09-26",
                "country": "GB",
                "status": "Official",
                "medium-track-count": 17,
                "label-info-list": [
                    {"label": {"name": "Apple Records"}, "catalog-number": "PCS 7088"},
                ],
            },
        ]
    }
    monkeypatch.setattr(musicbrainzngs, "search_releases", lambda **kw: response)
    results = search_releases("The Beatles", "Abbey Road")
    assert len(results) == 1
    r = results[0]
    assert r["id"] == "rel-aaa"
    assert r["title"] == "Abbey Road"
    assert r["artist"] == "The Beatles"
    assert r["date"] == "1969-09-26"
    assert r["country"] == "GB"
    assert r["track_count"] == 17
    assert r["label"] == "Apple Records"
    assert r["catalog_number"] == "PCS 7088"


def test_returns_empty_when_both_inputs_empty():
    assert search_releases("", "") == []
    assert search_releases("   ", "  ") == []


def test_query_uses_lucene_field_syntax(monkeypatch):
    seen = {}

    def fake_search(**kw):
        seen.update(kw)
        return {"release-list": []}

    monkeypatch.setattr(musicbrainzngs, "search_releases", fake_search)
    search_releases("The Beatles", "Abbey Road")
    assert 'artist:"The Beatles"' in seen["query"]
    assert 'release:"Abbey Road"' in seen["query"]
    assert " AND " in seen["query"]


def test_query_escapes_quotes_and_backslashes(monkeypatch):
    seen = {}

    def fake_search(**kw):
        seen.update(kw)
        return {"release-list": []}

    monkeypatch.setattr(musicbrainzngs, "search_releases", fake_search)
    search_releases('art"ist', "ti\\tle")
    # Embedded quote → escaped
    assert r'artist:"art\"ist"' in seen["query"]
    # Embedded backslash → escaped
    assert r'release:"ti\\tle"' in seen["query"]


def test_artist_only_search(monkeypatch):
    seen = {}

    def fake_search(**kw):
        seen.update(kw)
        return {"release-list": []}

    monkeypatch.setattr(musicbrainzngs, "search_releases", fake_search)
    search_releases("Solo Artist", "")
    assert seen["query"] == 'artist:"Solo Artist"'


def test_title_only_search(monkeypatch):
    seen = {}

    def fake_search(**kw):
        seen.update(kw)
        return {"release-list": []}

    monkeypatch.setattr(musicbrainzngs, "search_releases", fake_search)
    search_releases("", "Just A Title")
    assert seen["query"] == 'release:"Just A Title"'


def test_falls_back_to_artist_credit_when_phrase_absent(monkeypatch):
    response = {
        "release-list": [
            {
                "id": "rel-aaa",
                "title": "X",
                "artist-credit": [
                    {"name": "A", "joinphrase": " feat. "},
                    {"name": "B"},
                ],
            }
        ]
    }
    monkeypatch.setattr(musicbrainzngs, "search_releases", lambda **kw: response)
    results = search_releases("A", "X")
    assert results[0]["artist"] == "AB"


def test_handles_missing_optional_fields(monkeypatch):
    response = {"release-list": [{"id": "r1", "title": "T"}]}
    monkeypatch.setattr(musicbrainzngs, "search_releases", lambda **kw: response)
    r = search_releases("a", "t")[0]
    assert r["date"] is None
    assert r["country"] is None
    assert r["track_count"] is None
    assert r["label"] is None
    assert r["catalog_number"] is None


def test_passes_limit(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        musicbrainzngs, "search_releases",
        lambda **kw: (seen.update(kw), {"release-list": []})[1],
    )
    search_releases("a", "t", limit=5)
    assert seen["limit"] == 5


def test_raises_on_network_error(monkeypatch):
    def explode(**kw):
        raise musicbrainzngs.NetworkError(cause=Exception("boom"))

    monkeypatch.setattr(musicbrainzngs, "search_releases", explode)
    with pytest.raises(MBSearchError):
        search_releases("a", "t")


def test_returns_empty_when_no_release_list(monkeypatch):
    monkeypatch.setattr(musicbrainzngs, "search_releases", lambda **kw: {})
    assert search_releases("a", "t") == []
