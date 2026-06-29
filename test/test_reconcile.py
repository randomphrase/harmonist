"""Tests for reconcile.reconcile_album."""

from __future__ import annotations

import shutil
from pathlib import Path

from mutagen.mp4 import MP4

from harmonist import sidecar as sc
from harmonist.models import BandcampInfo, Sidecar
from harmonist.reconcile import reconcile_album, reconcile_pending
from harmonist.sidecar import CURRENT_SCHEMA_VERSION
from harmonist.tagger import ATOM_COMMENT, ATOM_MB_ALBUM_ID

FIXTURES_DIR = Path(__file__).parent / "fixtures"
SINE_M4A = FIXTURES_DIR / "sine.m4a"


def _make_album(root: Path, *, mbid: str | None = None, comment: str | None = None) -> Path:
    d = root / "Artist" / "Album"
    d.mkdir(parents=True)
    f = d / "01 Track.m4a"
    shutil.copy(SINE_M4A, f)
    if mbid or comment:
        audio = MP4(f)
        if mbid:
            audio[ATOM_MB_ALBUM_ID] = [mbid.encode("utf-8")]
        if comment:
            audio[ATOM_COMMENT] = [comment]
        audio.save()
    return d


def _no_urls(_mbid):
    return []


def _bandcamp_urls(*urls):
    return lambda _mbid: list(urls)


# ---------- skip cases ----------


def test_skips_album_with_consistent_existing_sidecar(tmp_path):
    """Sidecar already agrees with the file tags → no-op, and no MB query."""
    album_dir = _make_album(tmp_path, mbid="rel-aaa", comment="Visit https://x.bandcamp.com")
    sc.write(album_dir, Sidecar(schema_version=CURRENT_SCHEMA_VERSION, mb_release_id="rel-aaa"))

    def boom(_mbid):
        raise AssertionError("should not query MB when sidecar already matches the files")

    result = reconcile_album(album_dir, fetch_urls=boom)
    assert result is None
    loaded = sc.read(album_dir)
    assert loaded.mb_release_id == "rel-aaa"  # untouched


def test_skips_album_without_m4a_files(tmp_path):
    album_dir = tmp_path / "Empty"
    album_dir.mkdir()
    assert reconcile_album(album_dir, fetch_urls=_no_urls) is None


def test_adopts_external_file_retag(tmp_path):
    """Sidecar MBID disagrees with a consistent file re-tag (Picard) → adopt the
    file's MBID, keeping store_url + item_id (same purchase)."""
    album_dir = _make_album(tmp_path, mbid="rel-NEW")  # files now tagged rel-NEW
    sc.write(
        album_dir,
        Sidecar(
            schema_version=CURRENT_SCHEMA_VERSION,
            store_url="https://x.bandcamp.com/album/a",
            mb_release_id="rel-OLD",  # stale
            bandcamp=BandcampInfo(item_id=99),
        ),
    )
    result = reconcile_album(album_dir, fetch_urls=_no_urls)
    assert result is not None
    assert result.mb_release_id == "rel-NEW"  # adopted the file tag
    assert result.store_url == "https://x.bandcamp.com/album/a"  # kept
    assert result.bandcamp is not None
    assert result.bandcamp.item_id == 99  # kept (same purchase)


def test_leaves_consistent_sidecar_untouched(tmp_path):
    """Sidecar MBID == file MBID → idempotent no-op."""
    album_dir = _make_album(tmp_path, mbid="rel-1")
    sc.write(album_dir, Sidecar(schema_version=CURRENT_SCHEMA_VERSION, mb_release_id="rel-1"))
    assert reconcile_album(album_dir, fetch_urls=_no_urls) is None


def test_does_not_re_promote_surrendered_album(tmp_path):
    """A surrendered album (sidecar MBID None) whose files still carry the old
    MBID must NOT be re-promoted to it."""
    album_dir = _make_album(tmp_path, mbid="rel-OLD")  # files still tagged rel-OLD
    sc.write(
        album_dir,
        Sidecar(
            schema_version=CURRENT_SCHEMA_VERSION,
            store_url="https://x.bandcamp.com/album/a",
            mb_release_id=None,  # surrendered → Needs MBID
        ),
    )
    assert reconcile_album(album_dir, fetch_urls=_no_urls) is None  # untouched


def test_skips_album_without_mbid_tag(tmp_path):
    album_dir = _make_album(tmp_path)  # no MBID, no recoverable URL
    result = reconcile_album(album_dir, fetch_urls=_no_urls, recover_url=lambda _p: None)
    assert result is None
    assert not sc.has_sidecar(album_dir)


# ---------- URL recovery for untagged (no-MBID) downloads ----------


def test_recovers_store_url_when_no_mbid(tmp_path):
    """A manually-added Bandcamp download (no MBID atom) gets a sidecar with the
    recovered store_url and no MBID → NEEDS_MBID, not stuck in NEW."""
    album_dir = _make_album(tmp_path, comment="Visit https://myartist.bandcamp.com")
    url = "https://myartist.bandcamp.com/album/manual-add"
    result = reconcile_album(album_dir, fetch_urls=_no_urls, recover_url=lambda _p: url)
    assert result is not None
    assert result.store_url == url
    assert result.mb_release_id is None  # untagged → NEEDS_MBID
    assert result.tagged_at is None
    loaded = sc.read(album_dir)
    assert loaded.store_url == url


def test_reconcile_records_artist_root_url_no_mbid(tmp_path):
    """The Asura case: an untagged download whose ©cmt has only an ARTIST-ROOT
    Bandcamp URL is still recorded (store_url, no MBID) → NEEDS_MBID. Uses the
    real default recovery (no injected stub)."""
    album_dir = _make_album(tmp_path, comment="Visit https://asura.bandcamp.com")
    result = reconcile_album(album_dir, fetch_urls=_no_urls)  # default recover_store_url
    assert result is not None
    assert result.store_url == "https://asura.bandcamp.com"
    assert result.mb_release_id is None  # untagged → NEEDS_MBID
    assert result.tagged_at is None


def test_no_sidecar_when_no_mbid_and_no_recoverable_url(tmp_path):
    album_dir = _make_album(tmp_path)
    result = reconcile_album(album_dir, fetch_urls=_no_urls, recover_url=lambda _p: None)
    assert result is None
    assert not sc.has_sidecar(album_dir)


def test_recovery_failure_leaves_orphan(tmp_path):
    """A recover_url that raises is swallowed; the album stays an Orphan."""
    album_dir = _make_album(tmp_path, comment="Visit https://myartist.bandcamp.com")

    def boom(_path):
        raise RuntimeError("scrape failed")

    result = reconcile_album(album_dir, fetch_urls=_no_urls, recover_url=boom)
    assert result is None
    assert not sc.has_sidecar(album_dir)


# ---------- bandcamp store_url ----------


def test_writes_bandcamp_sidecar_when_comment_and_mb_match(tmp_path):
    album_dir = _make_album(
        tmp_path,
        mbid="rel-aaa",
        comment="Visit https://myartist.bandcamp.com",
    )
    fetch = _bandcamp_urls("https://myartist.bandcamp.com/album/my-album")
    result = reconcile_album(album_dir, fetch_urls=fetch)
    assert result is not None
    assert result.store_url == "https://myartist.bandcamp.com/album/my-album"
    assert result.bandcamp is None or result.bandcamp.item_id is None  # unconfirmed until sync
    assert result.mb_release_id == "rel-aaa"
    assert result.tagged_at is not None

    loaded = sc.read(album_dir)
    assert loaded.store_url == "https://myartist.bandcamp.com/album/my-album"


def test_uses_canonical_mb_url_not_comment_url(tmp_path):
    """Sidecar records MB's URL even if ©cmt has a different (e.g. artist-page) URL."""
    album_dir = _make_album(
        tmp_path,
        mbid="rel-aaa",
        comment="Visit https://myartist.bandcamp.com",  # artist page
    )
    fetch = _bandcamp_urls("https://myartist.bandcamp.com/album/canonical-album")
    result = reconcile_album(album_dir, fetch_urls=fetch)
    assert result.store_url == "https://myartist.bandcamp.com/album/canonical-album"


def test_picks_first_bandcamp_url_when_mb_has_multiple(tmp_path):
    album_dir = _make_album(tmp_path, mbid="rel-aaa", comment="https://myartist.bandcamp.com")
    fetch = _bandcamp_urls(
        "https://artist.notbandcamp.com/x",
        "https://primary.bandcamp.com/album/y",
        "https://other.bandcamp.com/album/z",
    )
    result = reconcile_album(album_dir, fetch_urls=fetch)
    assert result.store_url == "https://primary.bandcamp.com/album/y"


# ---------- no store_url (manual) ----------


def test_writes_no_store_url_when_no_bandcamp_comment(tmp_path):
    album_dir = _make_album(tmp_path, mbid="rel-aaa", comment="ripped from CD")
    fetch = _bandcamp_urls("https://x.bandcamp.com/album/y")  # MB knows of bandcamp
    result = reconcile_album(album_dir, fetch_urls=fetch)
    assert result is not None
    assert result.store_url is None
    assert result.bandcamp is None
    assert result.mb_release_id == "rel-aaa"


def test_writes_no_store_url_when_no_comment(tmp_path):
    album_dir = _make_album(tmp_path, mbid="rel-aaa")  # default sine.m4a, no ©cmt
    fetch = _bandcamp_urls("https://x.bandcamp.com/album/y")
    result = reconcile_album(album_dir, fetch_urls=fetch)
    assert result.store_url is None


def test_writes_no_store_url_when_mb_has_no_bandcamp_url(tmp_path):
    """User has Bandcamp ©cmt but MB doesn't actually link this release to Bandcamp."""
    album_dir = _make_album(tmp_path, mbid="rel-aaa", comment="https://x.bandcamp.com")
    fetch = _bandcamp_urls("https://example.com/somewhere-else")  # no bandcamp URL
    result = reconcile_album(album_dir, fetch_urls=fetch)
    assert result.store_url is None


def test_writes_no_store_url_when_mb_has_no_url_relationships(tmp_path):
    album_dir = _make_album(tmp_path, mbid="rel-aaa", comment="https://x.bandcamp.com")
    result = reconcile_album(album_dir, fetch_urls=_no_urls)
    assert result.store_url is None


def test_writes_no_store_url_when_mb_lookup_fails(tmp_path):
    """If MB lookup throws, fall back to no store_url rather than abort the whole scan."""
    album_dir = _make_album(tmp_path, mbid="rel-aaa", comment="https://x.bandcamp.com")

    def explode(_mbid):
        raise RuntimeError("MB down")

    result = reconcile_album(album_dir, fetch_urls=explode)
    assert result.store_url is None


# ---------- comment matching ----------


def test_matches_any_bandcamp_url_in_comment(tmp_path):
    """The comment doesn't have to be a clean URL — 'Visit X' counts."""
    album_dir = _make_album(
        tmp_path,
        mbid="rel-aaa",
        comment="Visit X at https://artist.bandcamp.com — brought to you by friends",
    )
    fetch = _bandcamp_urls("https://artist.bandcamp.com/album/y")
    result = reconcile_album(album_dir, fetch_urls=fetch)
    assert result.store_url == "https://artist.bandcamp.com/album/y"


def test_case_insensitive_bandcamp_match(tmp_path):
    album_dir = _make_album(tmp_path, mbid="rel-aaa", comment="https://X.BANDCAMP.COM")
    fetch = _bandcamp_urls("https://x.bandcamp.com/album/y")
    result = reconcile_album(album_dir, fetch_urls=fetch)
    assert result.store_url == "https://x.bandcamp.com/album/y"


# ---------- batch ----------


def test_reconcile_pending_classifies_each_album(tmp_path):
    a = _make_album(tmp_path / "a", mbid="rel-1", comment="https://artist.bandcamp.com")
    b = tmp_path / "b" / "Artist" / "Album"
    b.mkdir(parents=True)
    shutil.copy(SINE_M4A, b / "01.m4a")
    audio = MP4(b / "01.m4a")
    audio[ATOM_MB_ALBUM_ID] = [b"rel-2"]
    audio.save()
    c = tmp_path / "c" / "Artist" / "Album"
    c.mkdir(parents=True)
    shutil.copy(SINE_M4A, c / "01.m4a")  # untagged

    fetch = _bandcamp_urls("https://artist.bandcamp.com/album/y")
    stats = reconcile_pending([a, b, c], fetch_urls=fetch)
    assert stats == {
        "reconciled_bandcamp": 1,
        "reconciled_manual": 1,
        "skipped": 1,
        "errors": 0,
    }


def test_reconcile_pending_counts_errors(tmp_path):
    """A dir that makes reconcile_album raise (here: it doesn't exist, so
    iterdir fails) is counted as an error, not a crash."""
    missing = tmp_path / "does-not-exist"
    stats = reconcile_pending([missing], fetch_urls=_no_urls)
    assert stats["errors"] == 1
