"""Tests for cover_art module — uses httpx MockTransport, no real network."""

from __future__ import annotations

import shutil
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
