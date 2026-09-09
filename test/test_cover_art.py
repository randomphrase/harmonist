"""Tests for cover_art module — uses httpx MockTransport, no real network."""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from harmonist.cover_art import CoverArtError, cached_cover, ensure_cover

FIXTURES_DIR = Path(__file__).parent / "fixtures"
_TINY_JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00" + b"\x00" * 40


def _client(handler) -> httpx.Client:
    return httpx.Client(
        transport=httpx.MockTransport(handler),
        follow_redirects=True,
        timeout=10,
    )


def _flac_with_embedded_art(dirpath: Path, art: bytes) -> Path:
    """Copy the FLAC fixture into dirpath and embed `art` as its front cover."""
    from mutagen.flac import FLAC, Picture

    dst = dirpath / "01 track.flac"
    shutil.copy(FIXTURES_DIR / "sine.flac", dst)
    audio = FLAC(dst)
    pic = Picture()
    pic.type = 3  # front cover
    pic.mime = "image/jpeg"
    pic.data = art
    audio.add_picture(pic)
    audio.save()
    return dst


# ---------- cache hits ----------


def test_cached_cover_finds_jpg(tmp_path):
    (tmp_path / "cover.jpg").write_bytes(b"jpegbytes")
    assert cached_cover(tmp_path) == tmp_path / "cover.jpg"


def test_cached_cover_finds_png(tmp_path):
    (tmp_path / "cover.png").write_bytes(b"pngbytes")
    assert cached_cover(tmp_path) == tmp_path / "cover.png"


def test_cached_cover_returns_none_when_absent(tmp_path):
    assert cached_cover(tmp_path) is None


def test_caa_cover_write_is_audited(tmp_path):
    """#88: fetching cover art writes into the user's album dir, and can land on
    a cover.* they put there themselves — a file write like any other, so it's
    audited. `overwrote` distinguishes creating from replacing."""
    from harmonist import activity_store
    from harmonist.activity_store import Source

    activity_store.init(tmp_path / "audit.db")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"imagebytes", headers={"content-type": "image/jpeg"})

    album = tmp_path / "album"
    album.mkdir()
    assert ensure_cover(album, "rel-1", client=_client(handler)) == album / "cover.jpg"

    rows = [e.message for e in activity_store.recent(10, source=Source.AUDIT)]
    line = next(m for m in rows if m.startswith("cover.write"))
    assert "source=caa" in line
    assert "overwrote=False" in line  # created, not replaced
    assert "bytes=10" in line


def test_ensure_cover_uses_cache_without_network(tmp_path):
    (tmp_path / "cover.jpg").write_bytes(b"existing")

    def boom(req):
        raise AssertionError(f"network should not be hit: {req.url}")

    result = ensure_cover(tmp_path, "rel-123", release_group_mbid="rg-456", client=_client(boom))
    assert result == tmp_path / "cover.jpg"
    assert result.read_bytes() == b"existing"


# ---------- fetch from release endpoint ----------


def test_ensure_cover_fetches_jpeg_from_release(tmp_path):
    seen_urls = []

    def handler(req):
        seen_urls.append(str(req.url))
        return httpx.Response(200, content=b"REAL_JPEG", headers={"content-type": "image/jpeg"})

    result = ensure_cover(tmp_path, "rel-123", client=_client(handler))
    assert result == tmp_path / "cover.jpg"
    assert result.read_bytes() == b"REAL_JPEG"
    assert seen_urls == ["https://coverartarchive.org/release/rel-123/front"]


def test_ensure_cover_writes_png_when_content_type_says_png(tmp_path):
    def handler(req):
        return httpx.Response(200, content=b"REAL_PNG", headers={"content-type": "image/png"})

    result = ensure_cover(tmp_path, "rel-123", client=_client(handler))
    assert result == tmp_path / "cover.png"
    assert result.read_bytes() == b"REAL_PNG"


def test_ensure_cover_with_explicit_size_hits_sized_url(tmp_path):
    seen_urls = []

    def handler(req):
        seen_urls.append(str(req.url))
        return httpx.Response(200, content=b"x", headers={"content-type": "image/jpeg"})

    ensure_cover(tmp_path, "rel-123", size="500", client=_client(handler))
    assert seen_urls == ["https://coverartarchive.org/release/rel-123/front-500"]


# ---------- fallback to release-group ----------


def test_ensure_cover_falls_back_to_release_group_on_404(tmp_path):
    seen_urls = []

    def handler(req):
        seen_urls.append(str(req.url))
        if "release-group" in str(req.url):
            return httpx.Response(200, content=b"RG_JPEG", headers={"content-type": "image/jpeg"})
        return httpx.Response(404)

    result = ensure_cover(tmp_path, "rel-123", release_group_mbid="rg-456", client=_client(handler))
    assert result == tmp_path / "cover.jpg"
    assert result.read_bytes() == b"RG_JPEG"
    assert seen_urls == [
        "https://coverartarchive.org/release/rel-123/front",
        "https://coverartarchive.org/release-group/rg-456/front",
    ]


def test_ensure_cover_returns_none_when_both_endpoints_404(tmp_path):
    def handler(req):
        return httpx.Response(404)

    result = ensure_cover(tmp_path, "rel-123", release_group_mbid="rg-456", client=_client(handler))
    assert result is None
    # No cover file written
    assert not list(tmp_path.glob("cover.*"))


def test_ensure_cover_returns_none_when_release_404_and_no_release_group(tmp_path):
    def handler(req):
        return httpx.Response(404)

    result = ensure_cover(tmp_path, "rel-123", client=_client(handler))
    assert result is None


# ---------- fallback to embedded art ----------


def test_ensure_cover_extracts_embedded_art_when_caa_misses(tmp_path):
    """When CAA has no cover (fresh/private release) but an audio file carries
    embedded art, a folder cover.jpg is written from that art."""
    _flac_with_embedded_art(tmp_path, _TINY_JPEG)

    def handler(req):
        return httpx.Response(404)

    result = ensure_cover(tmp_path, "rel-123", release_group_mbid="rg-456", client=_client(handler))
    assert result == tmp_path / "cover.jpg"
    assert result.read_bytes() == _TINY_JPEG


def test_ensure_cover_prefers_caa_over_embedded(tmp_path):
    """CAA art wins over embedded — it's the authoritative match for the
    tagged release and typically higher resolution."""
    _flac_with_embedded_art(tmp_path, _TINY_JPEG)

    def handler(req):
        return httpx.Response(200, content=b"CAA_JPEG", headers={"content-type": "image/jpeg"})

    result = ensure_cover(tmp_path, "rel-123", client=_client(handler))
    assert result == tmp_path / "cover.jpg"
    assert result.read_bytes() == b"CAA_JPEG"


def test_ensure_cover_none_when_caa_misses_and_audio_has_no_art(tmp_path):
    """An audio file with no embedded art and no CAA match → no cover written."""
    shutil.copy(FIXTURES_DIR / "sine.flac", tmp_path / "01 track.flac")  # plain, no art

    def handler(req):
        return httpx.Response(404)

    result = ensure_cover(tmp_path, "rel-123", client=_client(handler))
    assert result is None
    assert not list(tmp_path.glob("cover.*"))


# ---------- error path ----------


def test_ensure_cover_raises_on_non_404_failure(tmp_path):
    def handler(req):
        return httpx.Response(500, content=b"server explosion")

    with pytest.raises(CoverArtError):
        ensure_cover(tmp_path, "rel-123", client=_client(handler))


def test_ensure_cover_raises_on_network_error(tmp_path):
    def handler(req):
        raise httpx.ConnectError("connection refused")

    with pytest.raises(CoverArtError):
        ensure_cover(tmp_path, "rel-123", client=_client(handler))


# ---------- the candidate cache (#276) ----------


def test_a_traversing_id_never_becomes_a_path(tmp_path):
    """The id comes from a sidecar, so it is untrusted input joined to a path."""
    from harmonist import cover_art

    cover_art.configure_cache(tmp_path / "caa")
    for bad in ("../escape", "a/b", "..", ".hidden/../x", "x" * 100):
        assert cover_art.cache_image(bad, b"data", "image/jpeg") is None
        assert cover_art.cached_image(bad) is None
    assert not list((tmp_path / "caa").glob("**/*")) or all(
        p.parent == tmp_path / "caa" for p in (tmp_path / "caa").glob("**/*")
    )


def test_an_ordinary_id_round_trips(tmp_path):
    from harmonist import cover_art

    cover_art.configure_cache(tmp_path / "caa")
    assert cover_art.cache_image("demo-rel-1", b"\xff\xd8\xffdata", "image/jpeg") is not None

    path = cover_art.cached_image("demo-rel-1")
    assert path is not None
    assert path.read_bytes() == b"\xff\xd8\xffdata"
    assert path.suffix == ".jpg"


def test_a_png_keeps_its_extension(tmp_path):
    from harmonist import cover_art

    cover_art.configure_cache(tmp_path / "caa")
    cover_art.cache_image("demo-rel-2", b"\x89PNG", "image/png")
    path = cover_art.cached_image("demo-rel-2")
    assert path is not None and path.suffix == ".png"


def test_no_cache_configured_is_a_no_op(tmp_path):
    from harmonist import cover_art

    cover_art.configure_cache(None)
    assert cover_art.cache_image("demo-rel-3", b"data", "image/jpeg") is None
    assert cover_art.cached_image("demo-rel-3") is None


# ---------- the release-group fallback (#434) ----------


def _listing(url: str) -> dict:
    return {"images": [{"front": True, "image": url}]}


def _sized_jpeg(width: int) -> bytes:
    from test.test_artwork import jpeg_bytes

    return jpeg_bytes(width, width)


def test_falls_back_to_the_release_group_when_the_release_has_no_cover():
    """The archive very often keeps the artwork against the group, and asking
    only the release reported "no front cover" for albums a tagging would
    happily have fetched one for (#434)."""
    from harmonist import cover_art

    asked: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(request.url.path)
        if request.url.path.startswith("/release/"):
            return httpx.Response(404)
        if request.url.path.startswith("/release-group/"):
            return httpx.Response(200, json=_listing("https://caa.example/rg.jpg"))
        return httpx.Response(200, content=_sized_jpeg(1400))

    answer = cover_art.check_front("rel-1", release_group_mbid="grp-1", client=_client(handler))

    assert answer.has_art is True
    assert answer.from_release_group is True
    assert answer.width == 1400
    assert "/release/rel-1" in asked and "/release-group/grp-1" in asked


def test_the_release_wins_when_it_has_its_own_cover():
    """The group is a fallback, not a preference: this edition's own art is the
    more specific answer."""
    from harmonist import cover_art

    asked: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(request.url.path)
        if request.url.path.startswith("/release/"):
            return httpx.Response(200, json=_listing("https://caa.example/rel.jpg"))
        if request.url.path.startswith("/release-group/"):
            raise AssertionError("asked the group when the release had a cover")
        return httpx.Response(200, content=_sized_jpeg(900))

    answer = cover_art.check_front("rel-1", release_group_mbid="grp-1", client=_client(handler))

    assert answer.from_release_group is False
    assert answer.width == 900


def test_neither_listing_having_one_is_a_stored_answer():
    from harmonist import cover_art

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    answer = cover_art.check_front("rel-1", release_group_mbid="grp-1", client=_client(handler))

    assert answer.has_art is False
    assert answer.source is None


def test_the_etag_is_only_sent_to_the_listing_it_came_from():
    """A 304 from the other listing would answer a question about a resource
    nobody asked after, and keep a stale measurement (#434)."""
    from harmonist import activity_store, cover_art

    sent: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent[request.url.path] = request.headers.get("if-none-match")
        if request.url.path.startswith("/release/"):
            return httpx.Response(404)
        if request.url.path.startswith("/release-group/"):
            return httpx.Response(304)
        return httpx.Response(200, content=_sized_jpeg(1400))

    known = activity_store.CachedCoverArt(
        fetched_at=datetime.now(UTC), etag='"grp"', image_url="x", source="release-group"
    )
    cover_art.check_front("rel-1", release_group_mbid="grp-1", known=known, client=_client(handler))

    assert sent["/release/rel-1"] is None  # not this listing's etag
    assert sent["/release-group/grp-1"] == '"grp"'


# ---------- loading a cover that lost (#448) ----------


def test_fetch_image_keeps_the_picture_where_the_page_can_find_it(tmp_path):
    """The whole point: a losing candidate is never downloaded by the check, so
    this is the only way its picture reaches the candidate cache."""
    from harmonist import cover_art

    cover_art.configure_cache(tmp_path / "caa")
    art = _sized_jpeg(500)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=art, headers={"content-type": "image/jpeg"})

    path = cover_art.fetch_image("rel-1", "https://caa.example/a.jpg", client=_client(handler))

    assert path is not None and path.read_bytes() == art
    assert cover_art.cached_image("rel-1") == path


def test_fetch_image_raises_rather_than_shrugging(tmp_path):
    """Where `_fetch_and_cache` swallows and logs, because losing the picture is
    a footnote to the measurement it was really after. Here the picture IS the
    request, and somebody is waiting to look at it."""
    from harmonist import cover_art

    cover_art.configure_cache(tmp_path / "caa")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    with pytest.raises(cover_art.CoverArtError):
        cover_art.fetch_image("rel-1", "https://caa.example/a.jpg", client=_client(handler))

    assert cover_art.cached_image("rel-1") is None  # nothing half-written
