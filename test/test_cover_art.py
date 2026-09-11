"""Tests for cover_art module — uses httpx MockTransport, no real network."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from harmonist import cover_art
from harmonist.cover_art import CoverArtError, Front, cached_cover, front_image

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _client(handler) -> httpx.Client:
    return httpx.Client(
        transport=httpx.MockTransport(handler),
        follow_redirects=True,
        timeout=10,
    )


@pytest.fixture
def caa_cache(tmp_path, monkeypatch) -> Path:
    """The candidate cache, configured for this test and put back afterwards —
    `configure_cache` sets a module global, and a test that leaves it pointing
    at its own tmp_path hands the next test a cache it did not ask for."""
    root = tmp_path / "caa"
    monkeypatch.setattr(cover_art, "_caa_root", root)
    return root


# ---------- folder covers already on disk ----------


def test_cached_cover_finds_jpg(tmp_path):
    (tmp_path / "cover.jpg").write_bytes(b"jpegbytes")
    assert cached_cover(tmp_path) == tmp_path / "cover.jpg"


def test_cached_cover_finds_png(tmp_path):
    (tmp_path / "cover.png").write_bytes(b"pngbytes")
    assert cached_cover(tmp_path) == tmp_path / "cover.png"


def test_cached_cover_returns_none_when_absent(tmp_path):
    assert cached_cover(tmp_path) is None


# ---------- the archive's image for a tagging: front_image (#469) ----------
#
# A CANDIDATE, not a folder cover. Whether it is written anywhere is the artwork
# plan's decision — see test_tagger's creation tests — so nothing here can touch
# an album directory: `front_image` is not given one.


def test_front_image_fetches_the_releases_original_and_keeps_it(caa_cache):
    """The ORIGINAL, whatever size a deployment once configured: the largest
    image available is the one Harmonist prefers, and it is the one the album
    page's check measures — the preview and the tagging compare one picture."""
    seen_urls = []

    def handler(req):
        seen_urls.append(str(req.url))
        return httpx.Response(200, content=b"REAL_JPEG", headers={"content-type": "image/jpeg"})

    assert front_image("rel-123", client=_client(handler)) == Front(b"REAL_JPEG", "image/jpeg")
    assert seen_urls == ["https://coverartarchive.org/release/rel-123/front"]
    # Kept, so the album page afterwards shows the candidate a tagging weighed.
    kept = cover_art.cached_image("rel-123")
    assert kept is not None
    assert kept.read_bytes() == b"REAL_JPEG"


def test_front_image_reports_a_png_as_one(caa_cache):
    """The mime decides the name a created folder cover takes."""

    def handler(req):
        return httpx.Response(200, content=b"REAL_PNG", headers={"content-type": "image/png"})

    assert front_image("rel-123", client=_client(handler)) == Front(b"REAL_PNG", "image/png")


def test_front_image_serves_the_cache_without_asking(caa_cache):
    cover_art.cache_image("rel-123", b"CACHED", "image/jpeg")

    def boom(req):
        raise AssertionError(f"network should not be hit: {req.url}")

    assert front_image("rel-123", "rg-456", client=_client(boom)) == Front(b"CACHED", "image/jpeg")


def test_front_image_still_answers_with_the_cache_switched_off(monkeypatch):
    """Bytes in hand rather than a path: a tagging needs the image it just
    fetched whether or not there was anywhere to keep it."""
    monkeypatch.setattr(cover_art, "_caa_root", None)

    def handler(req):
        return httpx.Response(200, content=b"REAL_JPEG", headers={"content-type": "image/jpeg"})

    assert front_image("rel-123", client=_client(handler)) == Front(b"REAL_JPEG", "image/jpeg")


# ---------- fallback to release-group ----------


def test_front_image_falls_back_to_release_group_on_404(caa_cache):
    seen_urls = []

    def handler(req):
        seen_urls.append(str(req.url))
        if "release-group" in str(req.url):
            return httpx.Response(200, content=b"RG_JPEG", headers={"content-type": "image/jpeg"})
        return httpx.Response(404)

    assert front_image("rel-123", "rg-456", client=_client(handler)) == Front(
        b"RG_JPEG", "image/jpeg"
    )
    assert seen_urls == [
        "https://coverartarchive.org/release/rel-123/front",
        "https://coverartarchive.org/release-group/rg-456/front",
    ]
    # Kept under the RELEASE, which is what the cache is keyed by.
    assert cover_art.cached_image("rel-123") is not None


def test_front_image_is_none_when_both_endpoints_404(caa_cache):
    def handler(req):
        return httpx.Response(404)

    assert front_image("rel-123", "rg-456", client=_client(handler)) is None


def test_front_image_is_none_when_release_404_and_no_release_group(caa_cache):
    def handler(req):
        return httpx.Response(404)

    assert front_image("rel-123", client=_client(handler)) is None


def test_front_image_falls_back_to_release_group_when_the_release_endpoint_is_down(caa_cache):
    """#458: a 503 says nothing about whether this release has a cover, so it
    must not end the search the way a 404's definitive "there is none" does."""
    seen_urls = []

    def handler(req):
        seen_urls.append(str(req.url))
        if "release-group" in str(req.url):
            return httpx.Response(200, content=b"RG_JPEG", headers={"content-type": "image/jpeg"})
        return httpx.Response(503)

    assert front_image("rel-123", "rg-456", client=_client(handler)) == Front(
        b"RG_JPEG", "image/jpeg"
    )
    assert seen_urls == [
        "https://coverartarchive.org/release/rel-123/front",
        "https://coverartarchive.org/release-group/rg-456/front",
    ]


def test_front_image_falls_back_to_release_group_after_a_transport_failure(caa_cache):
    """#458, the other way the first endpoint fails: an httpx error rather than a
    status."""

    def handler(req):
        if "release-group" in str(req.url):
            return httpx.Response(200, content=b"RG_JPEG", headers={"content-type": "image/jpeg"})
        raise httpx.ConnectError("connection refused")

    assert front_image("rel-123", "rg-456", client=_client(handler)) == Front(
        b"RG_JPEG", "image/jpeg"
    )


# ---------- error path ----------


def test_front_image_raises_on_non_404_failure(caa_cache):
    """Could-not-ask and there-is-nothing-there must not arrive as the same
    answer (#458). The 404 tests above are the other half of that pair: they
    return None, because a 404 IS an answer."""

    def handler(req):
        return httpx.Response(500, content=b"server explosion")

    with pytest.raises(CoverArtError):
        front_image("rel-123", client=_client(handler))


def test_front_image_raises_on_network_error(caa_cache):
    def handler(req):
        raise httpx.ConnectError("connection refused")

    with pytest.raises(CoverArtError):
        front_image("rel-123", client=_client(handler))


def test_a_later_404_does_not_erase_an_earlier_could_not_ask(caa_cache):
    """The release endpoint was unreachable and the release group answered "no
    art here". The archive was never properly asked about this release, so this
    must raise rather than return None: None would report a definitive "there is
    nothing for this release" that only one of the two answers is entitled to
    say (#458)."""

    def handler(req):
        return httpx.Response(404 if "release-group" in str(req.url) else 503)

    with pytest.raises(CoverArtError):
        front_image("rel-123", "rg-456", client=_client(handler))


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
