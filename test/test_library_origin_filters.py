"""Origin browsing uses the album's resolved provenance, within Library/search."""

from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import Mock

import pytest

from harmonist import mb_lookup, provenance
from harmonist.models import Album, AlbumState, Sidecar
from harmonist.web.main import _library_page_vars
from test.test_web import _make_tagged_album, cfg, client  # noqa: F401


@pytest.mark.parametrize("origin", list(provenance.Origin))
def test_origin_filter_uses_resolved_origin_with_search_and_counts(tmp_path, origin):
    albums = [
        Album(
            id=choice.value,
            path=tmp_path / choice.value,
            state=AlbumState.COMPLETE,
            title="Find me",
            artist="Artist",
            track_count=1,
            sidecar=Sidecar(mb_release_id=choice.value, origin_override=choice),
            # The explicit choice must win over conflicting detected evidence.
            download_stores=frozenset({provenance.Origin.AMAZON}),
        )
        for choice in provenance.Origin
    ]
    albums += [
        replace(
            albums[0],
            id="inbox",
            path=tmp_path / "inbox",
            state=AlbumState.NEEDS_MBID,
            title="Find me",
            sidecar=Sidecar(origin_override=origin),
        ),
        replace(
            albums[0],
            id="other",
            path=tmp_path / "other",
            state=AlbumState.COMPLETE,
            title="Other album",
            sidecar=Sidecar(mb_release_id="other", origin_override=origin),
        ),
    ]
    view = _library_page_vars(albums, 1, 20, filter_=f"origin-{origin.value.lower()}", q="Find")
    assert [a.sidecar.origin_override for a in view["rows"]] == [origin]
    assert {f["label"]: f["count"] for f in view["origin_filters"]} == {
        choice.value: 1 for choice in provenance.Origin
    }
    assert view["total_done"] == 7
    assert view["total_matched"] == 6
    assert view["total_shown"] == 1


def test_origin_filter_detects_sources_without_external_requests(tmp_path, monkeypatch):
    fetch = Mock(side_effect=AssertionError("Origin filtering must not fetch MusicBrainz"))
    monkeypatch.setattr(mb_lookup, "fetch_release", fetch)
    base = Album("album", tmp_path, "Album", "Artist", 1, AlbumState.COMPLETE)
    albums = [
        replace(base, path=tmp_path / "cd", ripped=True),
        replace(
            base,
            path=tmp_path / "download",
            state=AlbumState.COMPLETE,
            sidecar=Sidecar(bandcamp_downloaded=True),
        ),
        replace(base, path=tmp_path / "unknown"),
        replace(
            base,
            path=tmp_path / "conflict",
            state=AlbumState.COMPLETE,
            ripped=True,
            download_stores=frozenset({provenance.Origin.AMAZON}),
        ),
    ]
    for slug, names in [
        ("cd", ["cd"]),
        ("bandcamp", ["download"]),
        ("unknown", ["conflict", "unknown"]),  # untagged ties go by folder (#963)
    ]:
        view = _library_page_vars(albums, 1, 20, filter_=f"origin-{slug}")
        assert [a.path.name for a in view["rows"]] == names
    fetch.assert_not_called()


def test_origin_filter_is_addressable_and_keeps_paging_context(client, cfg):  # noqa: F811
    from harmonist import sidecar

    for i in range(3):
        folder = _make_tagged_album(
            cfg, f"Record {i}", mbid=f"record-{i}", tagged_at=datetime.now(UTC)
        )
        current = sidecar.read(folder)
        assert current is not None
        current.origin_override = provenance.Origin.CD if i < 2 else provenance.Origin.QOBUZ
        sidecar.write(folder, current)
    response = client.get("/?tab=library&filter=origin-cd&limit=1")
    assert response.status_code == 200
    assert 'data-total-done="3"' in response.text
    assert "Origin: CD" in response.text
    assert "filter=origin-cd" in response.text
    response = client.get("/library?filter=origin-cd&limit=1&page=2")
    assert response.status_code == 200
    assert 'data-page="2"' in response.text
    assert "2–2 of 2" in response.text
    assert 'name="filter" value="origin-cd"' in response.text
    assert "filter=origin-cd" in response.text
