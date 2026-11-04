"""Library queries intersect findings and replace choices within each facet."""

import shutil
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from harmonist import artwork_updates, formats, provenance, scanner, sidecar
from harmonist.models import Album, AlbumState, Sidecar
from harmonist.web.main import _library_filter, _library_page_vars
from test.test_web import _make_mixed_format_album, cfg, client  # noqa: F401


def test_combinations_and_contextual_counts(tmp_path):
    base = Album(
        "a",
        tmp_path,
        "Find me",
        "Artist",
        1,
        AlbumState.COMPLETE,
        artwork_local=artwork_updates.LocalArtwork(
            ((tmp_path / "track.m4a", None),),
            None,
            unreadable=False,
            lossless=False,
            audio_bytes=None,
        ),
    )
    albums = [
        replace(base, id="cd-bare", sidecar=Sidecar(origin_override=provenance.Origin.CD)),
        replace(
            base,
            id="cd-cover",
            has_cover=True,
            artwork_local=None,
            sidecar=Sidecar(origin_override=provenance.Origin.CD),
        ),
        replace(base, id="qobuz-bare", sidecar=Sidecar(origin_override=provenance.Origin.QOBUZ)),
        replace(
            base, id="other", title="Other", sidecar=Sidecar(origin_override=provenance.Origin.CD)
        ),
    ]
    view = _library_page_vars(albums, 1, 20, filter_="origin-cd,no-artwork", q="Find")
    assert [a.id for a in view["rows"]] == ["cd-bare"]
    origins = {f["slug"]: f for f in view["origin_filters"]}
    assert origins["origin-cd"]["count"] == 1
    assert origins["origin-qobuz"]["count"] == 1
    assert origins["origin-qobuz"]["next_filter"] == "origin-qobuz,no-artwork"
    no_art = next(f for f in view["filters"] if f["slug"] == "no-artwork")
    assert no_art["active"] is True
    assert no_art["next_filter"] == "origin-cd,no-artwork"
    assert view["clear_facets"]["artwork"] == "origin-cd"
    assert view["total_done"] == 4
    assert view["total_matched"] == 3


@pytest.mark.parametrize("scan_order", [(0, 1, 2, 3), (3, 1, 2, 0)])
def test_library_order_ties_follow_folder_path_not_scan_order(tmp_path, scan_order):
    """Albums tagged at the same moment, or never, are ordered by folder path
    whatever order the scan found them in, which is the filesystem's (#963)."""
    when = datetime(2026, 1, 1, tzinfo=UTC)
    base = Album("x", tmp_path, "Record", "Artist", 1, AlbumState.COMPLETE)
    albums = [
        replace(base, id="newest", path=tmp_path / "Z", sidecar=Sidecar(tagged_at=when)),
        replace(base, id="b", path=tmp_path / "B", sidecar=Sidecar(tagged_at=when - timedelta(1))),
        replace(base, id="a", path=tmp_path / "A", sidecar=Sidecar(tagged_at=when - timedelta(1))),
        replace(base, id="untagged", path=tmp_path / "0", sidecar=None),
    ]
    view = _library_page_vars([albums[i] for i in scan_order], 1, 20)
    assert [a.id for a in view["rows"]] == ["newest", "a", "b", "untagged"]


def test_selection_normalization_and_exclusive_facets():
    assert _library_filter("no-artwork,origin-cd,no-artwork,origin-qobuz,invalid") == (
        "origin-qobuz,no-artwork"
    )
    assert _library_filter("update-available,update-structure") == "update-structure"
    assert _library_filter("codec-flac,codec-mp3,mixed-format") == "mixed-format,codec-mp3"
    assert _library_filter("<bad>") is None


def test_new_combines_with_search_and_origin_with_contextual_counts(tmp_path):
    now = datetime.now(UTC)
    recent_cd = Sidecar(origin_override=provenance.Origin.CD, added_at=now - timedelta(days=1))
    base = Album("new-cd", tmp_path, "Find me", "Artist", 1, AlbumState.COMPLETE, sidecar=recent_cd)
    albums = [
        base,
        replace(base, id="old-cd", sidecar=replace(recent_cd, added_at=now - timedelta(days=8))),
        replace(
            base,
            id="new-qobuz",
            sidecar=replace(recent_cd, origin_override=provenance.Origin.QOBUZ),
        ),
        replace(base, id="other-title", title="Other"),
    ]
    view = _library_page_vars(albums, 1, 1, filter_="origin-cd,new", q="Find")
    assert [a.id for a in view["rows"]] == ["new-cd"]
    assert view["filter"] == "origin-cd,new"
    new = next(f for f in view["filters"] if f["slug"] == "new")
    assert new["count"] == 1
    assert new["active"] is True
    assert new["next_filter"] == "origin-cd"
    origins = {f["slug"]: f for f in view["origin_filters"]}
    assert origins["origin-qobuz"]["count"] == 1
    assert origins["origin-qobuz"]["next_filter"] == "origin-qobuz,new"
    assert [
        a.id
        for a in _library_page_vars(albums, 1, 20, filter_=new["next_filter"], q="Find")["rows"]
    ] == ["new-cd", "old-cd"]


def test_codec_matches_mixed_files_and_query_survives_routes(client, cfg):  # noqa: F811
    _make_mixed_format_album(cfg, "Patchwork", mbid="rel-patch", tagged_at=datetime.now(UTC))
    for codec in ("alac", "mp3"):
        query = f"mixed-format,codec-{codec}"
        response = client.get(f"/library?filter={query}&q=Patch&limit=1")
        soup = BeautifulSoup(response.text, "html.parser")
        assert soup.select_one("#lib-rel-patch") is not None
        selection = soup.select_one('input[name="filter"]')
        assert selection is not None and selection["value"] == query
        album_link = soup.select_one("#lib-rel-patch")
        assert album_link is not None
        album = client.get(album_link["href"])
        back = next(
            a
            for a in BeautifulSoup(album.text, "html.parser").find_all("a")
            if a.get_text(strip=True) == "← Back to library"
        )
        assert f"filter={query}" in back["href"]
        assert "q=Patch" in back["href"]
    response = client.get("/library?filter=mixed-format,codec-flac")
    assert 'id="lib-rel-patch"' not in response.text
    soup = BeautifulSoup(response.text, "html.parser")
    mixed = soup.select_one('button[aria-label="Mixed formats only"]')
    assert mixed is not None
    assert mixed["aria-pressed"] == "true"
    assert not mixed.has_attr("disabled")


@pytest.mark.parametrize(
    ("fixtures", "codecs"),
    [
        (("sine.m4a", "sine-aac.m4a"), {"ALAC", "AAC"}),
        (("sine.flac", "sine-hires.flac"), {"FLAC"}),
        (("sine.mp3", "sine-vbr.mp3"), {"MP3"}),
        (("sine.opus", "noise.opus"), {"Opus"}),
    ],
)
def test_codecs_across_disc_folders_come_from_streams_not_extensions(tmp_path, fixtures, codecs):
    for disc, fixture in enumerate(fixtures, 1):
        folder = tmp_path / f"CD{disc}"
        folder.mkdir()
        source = Path(__file__).parent / "fixtures" / fixture
        target = folder / f"01 Track{source.suffix}"
        shutil.copy(source, target)
        formats.write_tags(
            target,
            formats.TagSet(
                mb_album_id="codec-release",
                album="Album",
                album_artist="Artist",
                title=f"Disc {disc}",
                artist="Artist",
                track_num=1,
                track_total=1,
                disc_num=disc,
                disc_total=2,
                mb_release_track_id=f"rt-{disc}",
            ),
            None,
        )
        sidecar.write(folder, Sidecar(mb_release_id="codec-release", tagged_at=datetime.now(UTC)))
    albums = scanner.scan(tmp_path)
    assert len(albums) == 1
    assert albums[0].audio_codecs == codecs
    assert (albums[0].audio_format == scanner.MIXED_FORMAT) == (len(codecs) > 1)
    for codec in codecs:
        assert _library_page_vars(albums, 1, 20, filter_=f"codec-{codec.lower()}")["rows"] == albums
