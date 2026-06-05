"""Tests for url_recovery — uses MockTransport for httpx, no real network."""

from __future__ import annotations

import shutil
from pathlib import Path

import httpx
from mutagen.mp4 import MP4

from harmonist.tagger import ATOM_ALBUM, ATOM_COMMENT
from harmonist.url_recovery import recover_album_url

FIXTURES_DIR = Path(__file__).parent / "fixtures"
SINE_M4A = FIXTURES_DIR / "sine.m4a"


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True, timeout=10)


def _make_album(tmp_path: Path, *, comment: str = "", album: str = "") -> Path:
    album_dir = tmp_path / "Artist" / "Album"
    album_dir.mkdir(parents=True)
    track = album_dir / "01 Track.m4a"
    shutil.copy(SINE_M4A, track)
    if comment or album:
        audio = MP4(track)
        if comment:
            audio[ATOM_COMMENT] = [comment]
        if album:
            audio[ATOM_ALBUM] = [album]
        audio.save()
    return album_dir


def _artist_html(artist_links: list[tuple[str, str]]) -> str:
    """Build an artist page with the given (album_title, href) links."""
    items = "".join(
        f'<li><a class="title" href="{href}">{text}</a></li>' for text, href in artist_links
    )
    return f"<html><body><ol class='music-grid'>{items}</ol></body></html>"


# ---------- happy path ----------


def test_recover_returns_album_url_directly_if_comment_is_album_url(tmp_path):
    album_dir = _make_album(tmp_path, comment="https://x.bandcamp.com/album/exact-album")

    # Should not even hit the network — return as-is
    def boom(req):
        raise AssertionError(f"unexpected network call: {req.url}")

    url = recover_album_url(album_dir, client=_client(boom))
    assert url == "https://x.bandcamp.com/album/exact-album"


def test_recover_scrapes_artist_page_for_exact_match(tmp_path):
    album_dir = _make_album(
        tmp_path,
        comment="https://myartist.bandcamp.com",
        album="My Album",
    )
    html = _artist_html([("My Album", "/album/my-album"), ("Other", "/album/other")])

    def handler(req):
        if str(req.url) == "https://myartist.bandcamp.com":
            return httpx.Response(
                200, content=html.encode("utf-8"), headers={"content-type": "text/html"}
            )
        return httpx.Response(404)

    url = recover_album_url(album_dir, client=_client(handler))
    assert url == "https://myartist.bandcamp.com/album/my-album"


def test_recover_falls_back_to_substring_match(tmp_path):
    album_dir = _make_album(
        tmp_path,
        comment="https://myartist.bandcamp.com",
        album="Part",
    )
    # No exact "Part" link, but "Part Two" contains "part"
    html = _artist_html([("Other", "/album/other"), ("Part Two", "/album/part-two")])

    def handler(req):
        return httpx.Response(
            200, content=html.encode("utf-8"), headers={"content-type": "text/html"}
        )

    url = recover_album_url(album_dir, client=_client(handler))
    assert url == "https://myartist.bandcamp.com/album/part-two"


def test_recover_uses_dir_name_when_album_tag_absent(tmp_path):
    album_dir = tmp_path / "Artist" / "FallbackName"
    album_dir.mkdir(parents=True)
    shutil.copy(SINE_M4A, album_dir / "01.m4a")
    # Set comment but no ©alb tag
    audio = MP4(album_dir / "01.m4a")
    audio[ATOM_COMMENT] = ["https://myartist.bandcamp.com"]
    audio.save()

    html = _artist_html([("FallbackName", "/album/fallbackname"), ("Other", "/album/other")])

    def handler(req):
        return httpx.Response(200, content=html.encode("utf-8"))

    url = recover_album_url(album_dir, client=_client(handler))
    assert url == "https://myartist.bandcamp.com/album/fallbackname"


# ---------- real Bandcamp comment format ("Visit <url>") ----------


def test_recover_extracts_url_from_visit_prefix_album_comment(tmp_path):
    """Bandcamp embeds prose, e.g. 'Visit https://x.bandcamp.com/album/foo' —
    the URL must be extracted, not used as-is (which would carry 'Visit ')."""
    album_dir = _make_album(
        tmp_path, comment="Visit https://x.bandcamp.com/album/exact-album"
    )

    def boom(req):
        raise AssertionError(f"unexpected network call: {req.url}")

    url = recover_album_url(album_dir, client=_client(boom))
    assert url == "https://x.bandcamp.com/album/exact-album"


def test_recover_extracts_artist_url_from_visit_prefix_comment(tmp_path):
    """The common case: comment is 'Visit https://artist.bandcamp.com' (root,
    no album path). Extract it and scrape for the album by name."""
    album_dir = _make_album(
        tmp_path, comment="Visit https://myartist.bandcamp.com", album="My Album"
    )
    html = _artist_html([("My Album", "/album/my-album")])

    def handler(req):
        assert str(req.url) == "https://myartist.bandcamp.com"  # no 'Visit ' prefix
        return httpx.Response(200, content=html.encode("utf-8"))

    url = recover_album_url(album_dir, client=_client(handler))
    assert url == "https://myartist.bandcamp.com/album/my-album"


# ---------- short-circuit cases ----------


def test_recover_returns_none_when_no_m4a_files(tmp_path):
    album_dir = tmp_path / "Empty"
    album_dir.mkdir()
    assert recover_album_url(album_dir) is None


def test_recover_returns_none_when_no_comment(tmp_path):
    album_dir = _make_album(tmp_path)  # default sine.m4a has no ©cmt

    def boom(req):
        raise AssertionError("should not hit network")

    assert recover_album_url(album_dir, client=_client(boom)) is None


def test_recover_returns_none_when_comment_isnt_bandcamp(tmp_path):
    album_dir = _make_album(tmp_path, comment="https://example.com/something")

    def boom(req):
        raise AssertionError("should not hit network")

    assert recover_album_url(album_dir, client=_client(boom)) is None


# ---------- failure paths ----------


def test_recover_returns_none_when_artist_page_404(tmp_path):
    album_dir = _make_album(tmp_path, comment="https://myartist.bandcamp.com", album="Album")

    def handler(req):
        return httpx.Response(404)

    assert recover_album_url(album_dir, client=_client(handler)) is None


def test_recover_returns_none_when_no_matching_link(tmp_path):
    album_dir = _make_album(
        tmp_path, comment="https://myartist.bandcamp.com", album="Missing Album"
    )
    html = _artist_html([("Different", "/album/different")])

    def handler(req):
        return httpx.Response(200, content=html.encode("utf-8"))

    assert recover_album_url(album_dir, client=_client(handler)) is None


def test_recover_returns_none_on_network_error(tmp_path):
    album_dir = _make_album(tmp_path, comment="https://myartist.bandcamp.com", album="Album")

    def handler(req):
        raise httpx.ConnectError("refused")

    assert recover_album_url(album_dir, client=_client(handler)) is None
