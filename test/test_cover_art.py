"""Tests for cover_art module — uses httpx MockTransport, no real network."""
from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from harmonist import cover_art
from harmonist.cover_art import CoverArtError, cached_cover, ensure_cover


def _client(handler) -> httpx.Client:
    return httpx.Client(
        transport=httpx.MockTransport(handler),
        follow_redirects=True,
        timeout=10,
    )


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

    result = ensure_cover(
        tmp_path, "rel-123", release_group_mbid="rg-456", client=_client(boom)
    )
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

    result = ensure_cover(
        tmp_path, "rel-123", release_group_mbid="rg-456", client=_client(handler)
    )
    assert result == tmp_path / "cover.jpg"
    assert result.read_bytes() == b"RG_JPEG"
    assert seen_urls == [
        "https://coverartarchive.org/release/rel-123/front",
        "https://coverartarchive.org/release-group/rg-456/front",
    ]


def test_ensure_cover_returns_none_when_both_endpoints_404(tmp_path):
    def handler(req):
        return httpx.Response(404)

    result = ensure_cover(
        tmp_path, "rel-123", release_group_mbid="rg-456", client=_client(handler)
    )
    assert result is None
    # No cover file written
    assert not list(tmp_path.glob("cover.*"))


def test_ensure_cover_returns_none_when_release_404_and_no_release_group(tmp_path):
    def handler(req):
        return httpx.Response(404)

    result = ensure_cover(tmp_path, "rel-123", client=_client(handler))
    assert result is None


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
