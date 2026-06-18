"""Tests for bandcamp_hook — URL construction, cap, sidecar capture."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from harmonist import sidecar as sc
from harmonist.bandcamp_hook import (
    CapExceededError,
    HarmonistSyncer,
    album_slug,
    check_download_cap,
    construct_bandcamp_url,
    find_existing_album_by_slug,
    find_existing_album_by_url,
    write_sidecar_for_item,
)
from harmonist.models import BandcampInfo, Sidecar
from harmonist.sidecar import CURRENT_SCHEMA_VERSION


class _StubItem:
    """Mimics bandcampsync.bandcamp.BandcampItem (dict-attribute proxy)."""

    def __init__(self, **data):
        self._data = data
        # Some attributes are accessed directly (parent code)
        self.item_id = data.get("item_id", 1)
        self.is_preorder = data.get("is_preorder", False)
        self.band_name = data.get("band_name", "Test Artist")
        self.item_title = data.get("item_title", "Test Album")


# ---------- construct_bandcamp_url ----------


def test_url_from_subdomain_hints():
    item = _StubItem(url_hints={"subdomain": "myartist", "slug": "my-album", "item_type": "album"})
    assert construct_bandcamp_url(item) == "https://myartist.bandcamp.com/album/my-album"


def test_url_from_custom_domain_hints():
    item = _StubItem(
        url_hints={"custom_domain": "music.example.com", "slug": "my-album", "item_type": "album"}
    )
    assert construct_bandcamp_url(item) == "https://music.example.com/album/my-album"


def test_url_track_item_type():
    item = _StubItem(url_hints={"subdomain": "x", "slug": "single", "item_type": "track"})
    assert construct_bandcamp_url(item) == "https://x.bandcamp.com/track/single"


def test_url_default_item_type_album():
    item = _StubItem(url_hints={"subdomain": "x", "slug": "y"})
    assert construct_bandcamp_url(item) == "https://x.bandcamp.com/album/y"


def test_url_direct_item_url_wins():
    """If item_url is present in _data, prefer it over reconstruction."""
    item = _StubItem(
        item_url="https://x.bandcamp.com/album/direct",
        url_hints={"subdomain": "wrong", "slug": "wrong-slug"},
    )
    assert construct_bandcamp_url(item) == "https://x.bandcamp.com/album/direct"


def test_url_returns_none_without_slug():
    item = _StubItem(url_hints={"subdomain": "x"})  # no slug
    assert construct_bandcamp_url(item) is None


def test_url_returns_none_without_hints():
    item = _StubItem()
    assert construct_bandcamp_url(item) is None


def test_url_returns_none_with_garbage_hints():
    item = _StubItem(url_hints="not a dict")
    assert construct_bandcamp_url(item) is None


# ---------- check_download_cap ----------


def test_cap_under():
    check_download_cap(3, 5)  # no raise


def test_cap_equal_ok():
    check_download_cap(5, 5)  # cap is inclusive — 5 is OK


def test_cap_exceeds_raises():
    with pytest.raises(CapExceededError) as exc_info:
        check_download_cap(6, 5)
    assert "6" in str(exc_info.value)
    assert "5" in str(exc_info.value)


def test_cap_zero():
    with pytest.raises(CapExceededError):
        check_download_cap(1, 0)


# ---------- write_sidecar_for_item ----------


def test_write_sidecar_creates_file_with_correct_data(tmp_path):
    album_dir = tmp_path / "Artist" / "Album"
    album_dir.mkdir(parents=True)
    item = _StubItem(
        item_id=12345,
        band_id=678,
        url_hints={"subdomain": "myartist", "slug": "my-album", "item_type": "album"},
    )

    ok = write_sidecar_for_item(item, album_dir)
    assert ok is True

    sidecar = sc.read(album_dir)
    assert sidecar is not None
    assert sidecar.store_url == "https://myartist.bandcamp.com/album/my-album"
    assert sidecar.bandcamp.item_id == 12345
    assert sidecar.bandcamp.band_id == 678
    assert sidecar.downloaded_at is not None
    assert sidecar.mb_release_id is None


def test_write_sidecar_returns_false_when_url_unrecoverable(tmp_path):
    album_dir = tmp_path / "Album"
    album_dir.mkdir()
    item = _StubItem(item_id=1)  # no url_hints, no item_url
    assert write_sidecar_for_item(item, album_dir) is False
    assert not sc.has_sidecar(album_dir)


def test_write_sidecar_handles_missing_band_id(tmp_path):
    album_dir = tmp_path / "Album"
    album_dir.mkdir()
    item = _StubItem(item_id=1, url_hints={"subdomain": "x", "slug": "y"})
    write_sidecar_for_item(item, album_dir)
    sidecar = sc.read(album_dir)
    assert sidecar.bandcamp.band_id is None


def test_write_sidecar_captures_is_private(tmp_path):
    """The Bandcamp `is_private` flag rides into the sidecar (and round-trips
    through disk) so the UI can suppress Harmony/Recheck for private URLs."""
    album_dir = tmp_path / "Album"
    album_dir.mkdir()
    item = _StubItem(item_id=1, is_private=True, url_hints={"subdomain": "x", "slug": "y"})
    write_sidecar_for_item(item, album_dir)
    assert sc.read(album_dir).bandcamp.is_private is True


def test_write_sidecar_is_private_defaults_false(tmp_path):
    album_dir = tmp_path / "Album"
    album_dir.mkdir()
    item = _StubItem(item_id=1, url_hints={"subdomain": "x", "slug": "y"})  # no is_private
    write_sidecar_for_item(item, album_dir)
    assert sc.read(album_dir).bandcamp.is_private is False


# ---------- HarmonistSyncer subclass plumbing ----------
# We can't construct a full HarmonistSyncer (parent __init__ hits Bandcamp).
# Instead: build via __new__, wire up the bits the overrides touch, and
# exercise the behaviour we care about.


def _bare_syncer(max_downloads: int = 5) -> HarmonistSyncer:
    s = HarmonistSyncer.__new__(HarmonistSyncer)
    s._max_downloads_per_sync = max_downloads
    s._progress_callback = None
    s._post_download_callback = None
    s.new_items = 0
    s.bandcamp = MagicMock()
    s.ignores = MagicMock()
    s.local_media = MagicMock()
    return s


def test_unmatched_purchases_returns_only_unlinked(tmp_path):
    """unmatched_purchases() = collection items whose item_id is in NO sidecar
    (the candidates for mis-tag cross-referencing)."""
    linked = tmp_path / "Linked"
    linked.mkdir()
    sc.write(
        linked,
        Sidecar(
            schema_version=CURRENT_SCHEMA_VERSION,
            store_url="https://x.bandcamp.com/album/linked",
            bandcamp=BandcampInfo(item_id=111),
        ),
    )
    s = _bare_syncer()
    s.local_media.media_dir = str(tmp_path)
    s.bandcamp.purchases = [
        _StubItem(
            item_id=111,
            band_name="X",
            item_title="Linked",
            url_hints={"subdomain": "x", "slug": "linked"},
        ),
        _StubItem(
            item_id=222,
            band_name="X",
            item_title="Orphan",
            url_hints={"subdomain": "x", "slug": "orphan"},
        ),
    ]
    # Only item 222 (no sidecar carries it) is returned, as (url, label).
    assert s.unmatched_purchases() == [("https://x.bandcamp.com/album/orphan", "X / Orphan")]


def test_sync_items_raises_when_over_cap(monkeypatch):
    s = _bare_syncer(max_downloads=2)
    s.bandcamp.purchases = [_StubItem(item_id=i) for i in range(5)]
    s.ignores.is_ignored = lambda item: False
    # Stub the parent's sync_items so we can verify it's NOT called when cap raises
    parent_called = []

    async def parent_sync_items(self):
        parent_called.append(True)

    monkeypatch.setattr("harmonist.bandcamp_hook._BCSyncer.sync_items", parent_sync_items)

    with pytest.raises(CapExceededError):
        asyncio.run(s.sync_items())
    assert parent_called == []


def test_sync_items_excludes_ignored_from_cap_count(monkeypatch):
    s = _bare_syncer(max_downloads=2)
    items = [_StubItem(item_id=i) for i in range(5)]
    s.bandcamp.purchases = items
    # 4 of 5 ignored — only 1 candidate, well under cap of 2
    s.ignores.is_ignored = lambda item: item.item_id != 0
    parent_called = []

    async def parent_sync_items(self):
        parent_called.append(True)

    monkeypatch.setattr("harmonist.bandcamp_hook._BCSyncer.sync_items", parent_sync_items)

    asyncio.run(s.sync_items())  # should NOT raise
    assert parent_called == [True]


def test_sync_items_excludes_preorders_from_cap_count(monkeypatch):
    s = _bare_syncer(max_downloads=1)
    items = [
        _StubItem(item_id=0),
        _StubItem(item_id=1, is_preorder=True),
        _StubItem(item_id=2, is_preorder=True),
    ]
    s.bandcamp.purchases = items
    s.ignores.is_ignored = lambda item: False

    async def parent_sync_items(self):
        pass

    monkeypatch.setattr("harmonist.bandcamp_hook._BCSyncer.sync_items", parent_sync_items)
    asyncio.run(s.sync_items())  # 1 real candidate, cap is 1 — OK


def test_sync_item_writes_sidecar_on_successful_download(monkeypatch, tmp_path):
    """When parent sync_item returns True, our override should write a sidecar."""
    album_dir = tmp_path / "Artist" / "Album"
    album_dir.mkdir(parents=True)

    s = _bare_syncer()
    s.local_media.get_path_for_purchase = MagicMock(return_value=album_dir)

    # Patch parent's sync_item to "succeed"
    monkeypatch.setattr(
        "harmonist.bandcamp_hook._BCSyncer.sync_item",
        lambda self, item, encoding=None: True,
    )

    item = _StubItem(item_id=99, url_hints={"subdomain": "x", "slug": "y"})
    result = s.sync_item(item)
    assert result is True
    assert sc.has_sidecar(album_dir)
    sidecar = sc.read(album_dir)
    assert sidecar.bandcamp.item_id == 99


def test_sync_item_invokes_post_download_callback_after_sidecar(monkeypatch, tmp_path):
    """After a successful download + sidecar write, the post-download hook
    fires with the album dir (this is how MB auto-resolve gets triggered)."""
    album_dir = tmp_path / "Artist" / "Album"
    album_dir.mkdir(parents=True)

    s = _bare_syncer()
    s.local_media.get_path_for_purchase = MagicMock(return_value=album_dir)
    seen = []
    s._post_download_callback = lambda d: seen.append(d)

    monkeypatch.setattr(
        "harmonist.bandcamp_hook._BCSyncer.sync_item",
        lambda self, item, encoding=None: True,
    )

    item = _StubItem(item_id=99, url_hints={"subdomain": "x", "slug": "y"})
    s.sync_item(item)
    assert seen == [album_dir]


def test_sync_item_post_download_callback_failure_does_not_break_sync(monkeypatch, tmp_path):
    """A throwing post-download hook must not abort the sync."""
    album_dir = tmp_path / "Artist" / "Album"
    album_dir.mkdir(parents=True)

    s = _bare_syncer()
    s.local_media.get_path_for_purchase = MagicMock(return_value=album_dir)

    def boom(_d):
        raise RuntimeError("resolve blew up")

    s._post_download_callback = boom
    monkeypatch.setattr(
        "harmonist.bandcamp_hook._BCSyncer.sync_item",
        lambda self, item, encoding=None: True,
    )

    item = _StubItem(item_id=99, url_hints={"subdomain": "x", "slug": "y"})
    assert s.sync_item(item) is True  # download still reported as success


def test_sync_item_no_sidecar_on_skipped_download(monkeypatch, tmp_path):
    """When parent sync_item returns falsy (already downloaded / ignored), no sidecar."""
    album_dir = tmp_path / "Album"
    album_dir.mkdir()
    s = _bare_syncer()
    s.local_media.get_path_for_purchase = MagicMock(return_value=album_dir)
    s.local_media.media_dir = str(tmp_path)
    monkeypatch.setattr(
        "harmonist.bandcamp_hook._BCSyncer.sync_item",
        lambda self, item, encoding=None: False,
    )

    item = _StubItem(item_id=1, url_hints={"subdomain": "x", "slug": "y"})
    s.sync_item(item)
    assert not sc.has_sidecar(album_dir)


# ---------- merge into pre-existing sidecar (post-reconciliation) ----------


def test_write_sidecar_fills_in_item_id_on_existing_sidecar(tmp_path):
    """Reconciliation produced a sidecar with item_id=None; sync fills it in."""
    album_dir = tmp_path / "Album"
    album_dir.mkdir()
    sc.write(
        album_dir,
        Sidecar(
            schema_version=CURRENT_SCHEMA_VERSION,
            store_url="https://x.bandcamp.com/album/y",
            mb_release_id="rel-aaa",
        ),
    )
    item = _StubItem(item_id=12345, url_hints={"subdomain": "x", "slug": "y"})
    write_sidecar_for_item(item, album_dir)
    loaded = sc.read(album_dir)
    assert loaded.bandcamp.item_id == 12345
    # MB release ID and other fields preserved
    assert loaded.mb_release_id == "rel-aaa"
    # URL preserved (we don't overwrite the canonical MB URL)
    assert loaded.store_url == "https://x.bandcamp.com/album/y"


def test_write_sidecar_prefer_item_url_adopts_item_url(tmp_path):
    """With prefer_item_url=True (the slug-match case), the item's URL replaces
    the existing (drifted) store_url."""
    album_dir = tmp_path / "Album"
    album_dir.mkdir()
    sc.write(
        album_dir,
        Sidecar(
            schema_version=CURRENT_SCHEMA_VERSION,
            store_url="https://label.bandcamp.com/album/home",  # drifted
            mb_release_id="rel-aaa",
        ),
    )
    item = _StubItem(item_id=42, url_hints={"subdomain": "artist", "slug": "home"})
    write_sidecar_for_item(item, album_dir, prefer_item_url=True)
    loaded = sc.read(album_dir)
    assert loaded.store_url == "https://artist.bandcamp.com/album/home"
    assert loaded.bandcamp.item_id == 42
    assert loaded.mb_release_id == "rel-aaa"


def test_find_existing_album_by_url_returns_match(tmp_path):
    a = tmp_path / "Artist" / "Album"
    a.mkdir(parents=True)
    sc.write(
        a,
        Sidecar(
            schema_version=CURRENT_SCHEMA_VERSION,
            store_url="https://x.bandcamp.com/album/y",
        ),
    )
    other = tmp_path / "Artist2" / "Other"
    other.mkdir(parents=True)
    sc.write(
        other,
        Sidecar(
            schema_version=CURRENT_SCHEMA_VERSION,
            store_url="https://x.bandcamp.com/album/different",
        ),
    )
    found = find_existing_album_by_url(tmp_path, "https://x.bandcamp.com/album/y")
    assert found == a


def test_find_existing_album_by_url_returns_none_if_no_match(tmp_path):
    assert find_existing_album_by_url(tmp_path, "https://x.bandcamp.com/album/y") is None


# ---------- album_slug ----------


def test_album_slug_extracts_album_slug():
    assert (
        album_slug("https://echospace313.bandcamp.com/album/dimensional-space-remastered-by-pole")
        == "album/dimensional-space-remastered-by-pole"
    )


def test_album_slug_ignores_subdomain():
    """Same slug under a label page and an artist page → same key. This is the
    cross-listed-release case ('Home' under echospacedetroit vs the artist)."""
    label = album_slug("https://echospacedetroit.bandcamp.com/album/home")
    artist = album_slug("https://brockvanwey.bandcamp.com/album/home")
    assert label == artist == "album/home"


def test_album_slug_distinguishes_editions():
    """The edition qualifier lives in the slug, so the six 'Dimensional Space'
    editions stay distinct (artist+title matching collapses them)."""
    ep = album_slug("https://cv313.bandcamp.com/album/dimensional-space-ep")
    pole = album_slug("https://cv313.bandcamp.com/album/dimensional-space-remastered-by-pole")
    assert ep != pole


def test_album_slug_keeps_item_type():
    """An album and a track sharing a slug must not collide."""
    assert album_slug("https://x.bandcamp.com/album/y") == "album/y"
    assert album_slug("https://x.bandcamp.com/track/y") == "track/y"
    assert album_slug("https://x.bandcamp.com/album/y") != album_slug(
        "https://x.bandcamp.com/track/y"
    )


def test_album_slug_lowercases_slug():
    assert album_slug("https://x.bandcamp.com/album/Mixed-Case") == "album/mixed-case"


def test_album_slug_none_for_bare_landing_page():
    """A bare subdomain (like the URL embedded in tags) has no release identity
    and must never match anything."""
    assert album_slug("https://echospace313.bandcamp.com") is None
    assert album_slug("https://echospace313.bandcamp.com/") is None


def test_album_slug_none_for_empty():
    assert album_slug(None) is None
    assert album_slug("") is None


# ---------- find_existing_album_by_slug ----------


def _write_sidecar(album_dir: Path, store_url: str, *, item_id: int | None = None) -> None:
    album_dir.mkdir(parents=True, exist_ok=True)
    sc.write(
        album_dir,
        Sidecar(
            schema_version=CURRENT_SCHEMA_VERSION,
            store_url=store_url,
            mb_release_id="rel-x",
            bandcamp=BandcampInfo(item_id=item_id) if item_id is not None else None,
        ),
    )


def test_find_by_slug_matches_across_subdomains(tmp_path):
    """On-disk store_url is the label page; the item URL is the artist page —
    same slug, so it matches (the 'Home' case)."""
    a = tmp_path / "Brock Van Wey" / "Home"
    _write_sidecar(a, "https://echospacedetroit.bandcamp.com/album/home")
    found = find_existing_album_by_slug(tmp_path, "https://brockvanwey.bandcamp.com/album/home")
    assert found == a


def test_find_by_slug_skips_already_linked(tmp_path):
    """An album that already has an item_id is never re-matched."""
    a = tmp_path / "A" / "Album"
    _write_sidecar(a, "https://label.bandcamp.com/album/home", item_id=999)
    assert find_existing_album_by_slug(tmp_path, "https://artist.bandcamp.com/album/home") is None


def test_find_by_slug_ambiguous_returns_none(tmp_path):
    """Two unlinked albums share the slug → refuse to guess."""
    _write_sidecar(tmp_path / "One" / "Home", "https://label.bandcamp.com/album/home")
    _write_sidecar(tmp_path / "Two" / "Home", "https://artist.bandcamp.com/album/home")
    assert find_existing_album_by_slug(tmp_path, "https://x.bandcamp.com/album/home") is None


def test_find_by_slug_distinguishes_editions(tmp_path):
    """A disk album for the pole remaster must not match the EP item, even
    though both 'Dimensional Space' titles normalize the same."""
    ep = tmp_path / "cv313" / "Dimensional Space EP"
    _write_sidecar(ep, "https://cv313.bandcamp.com/album/dimensional-space-ep")
    pole = tmp_path / "cv313" / "Dimensional Space Pole"
    _write_sidecar(pole, "https://cv313.bandcamp.com/album/dimensional-space-remastered-by-pole")
    found = find_existing_album_by_slug(
        tmp_path, "https://cv313.bandcamp.com/album/dimensional-space-remastered-by-pole"
    )
    assert found == pole


def test_find_by_slug_none_for_bare_url(tmp_path):
    _write_sidecar(tmp_path / "A" / "B", "https://x.bandcamp.com/album/home")
    assert find_existing_album_by_slug(tmp_path, "https://x.bandcamp.com") is None


def test_sync_item_short_circuits_on_url_match(monkeypatch, tmp_path):
    """Reconciled album already on disk → sync should NOT call parent download."""
    existing_dir = tmp_path / "Old" / "Path"
    existing_dir.mkdir(parents=True)
    sc.write(
        existing_dir,
        Sidecar(
            schema_version=CURRENT_SCHEMA_VERSION,
            store_url="https://x.bandcamp.com/album/y",
            mb_release_id="rel-aaa",
        ),
    )

    s = _bare_syncer()
    s.local_media.media_dir = str(tmp_path)
    s.ignores.is_ignored = MagicMock(return_value=False)  # not yet marked
    parent_called = []

    def parent_sync_item(self, item, encoding=None):
        parent_called.append(True)
        return True

    monkeypatch.setattr("harmonist.bandcamp_hook._BCSyncer.sync_item", parent_sync_item)

    item = _StubItem(item_id=12345, url_hints={"subdomain": "x", "slug": "y"})
    result = s.sync_item(item)

    assert parent_called == []  # short-circuited
    assert result is False  # didn't download
    s.ignores.add.assert_called_once_with(item)  # marked once
    # Existing sidecar got item_id filled in
    loaded = sc.read(existing_dir)
    assert loaded.bandcamp.item_id == 12345


def test_sync_item_slug_fallback_links_and_adopts_url(monkeypatch, tmp_path):
    """Exact URL misses (different subdomain) but the slug matches → link the
    item_id WITHOUT downloading, and adopt the item's URL as store_url."""
    existing_dir = tmp_path / "Brock Van Wey" / "Home"
    existing_dir.mkdir(parents=True)
    sc.write(
        existing_dir,
        Sidecar(
            schema_version=CURRENT_SCHEMA_VERSION,
            store_url="https://echospacedetroit.bandcamp.com/album/home",  # label page
            mb_release_id="rel-home",
        ),
    )

    s = _bare_syncer()
    s.local_media.media_dir = str(tmp_path)
    s.ignores.is_ignored = MagicMock(return_value=False)
    parent_called = []
    monkeypatch.setattr(
        "harmonist.bandcamp_hook._BCSyncer.sync_item",
        lambda self, item, encoding=None: parent_called.append(True),
    )

    # Collection item lives under the *artist* subdomain — same slug, diff host.
    item = _StubItem(item_id=2043061668, url_hints={"subdomain": "brockvanwey", "slug": "home"})
    result = s.sync_item(item)

    assert parent_called == []  # slug short-circuit — no download
    assert result is False
    loaded = sc.read(existing_dir)
    assert loaded.bandcamp.item_id == 2043061668
    # store_url adopted from the item (where it was actually purchased)
    assert loaded.store_url == "https://brockvanwey.bandcamp.com/album/home"
    assert loaded.mb_release_id == "rel-home"  # MB identity untouched
    s.ignores.add.assert_called_once_with(item)


def test_sync_item_slug_fallback_skips_when_ambiguous(monkeypatch, tmp_path):
    """Two unlinked albums share the slug → don't guess; fall through to the
    normal download path rather than mislink."""
    for sub in ("One", "Two"):
        d = tmp_path / sub / "Home"
        d.mkdir(parents=True)
        sc.write(
            d,
            Sidecar(
                schema_version=CURRENT_SCHEMA_VERSION,
                store_url=f"https://{sub.lower()}.bandcamp.com/album/home",
                mb_release_id=f"rel-{sub}",
            ),
        )

    s = _bare_syncer()
    s.local_media.media_dir = str(tmp_path)
    s.ignores.is_ignored = MagicMock(return_value=False)
    s.local_media.get_path_for_purchase = MagicMock(return_value=tmp_path / "New" / "Dir")
    (tmp_path / "New" / "Dir").mkdir(parents=True)
    parent_called = []

    def parent_sync_item(self, item, encoding=None):
        parent_called.append(True)
        return True

    monkeypatch.setattr("harmonist.bandcamp_hook._BCSyncer.sync_item", parent_sync_item)

    item = _StubItem(item_id=5, url_hints={"subdomain": "artist", "slug": "home"})
    s.sync_item(item)
    assert parent_called == [True]  # ambiguous → fell through, did not short-circuit


def test_sync_item_short_circuit_does_not_re_add_already_ignored(monkeypatch, tmp_path):
    """The short-circuit runs every sync for an on-disk album; if the item is
    already in ignores.txt we must NOT add it again (else duplicates pile up —
    bandcampsync's Ignores.add appends without dedup)."""
    existing_dir = tmp_path / "Old" / "Path"
    existing_dir.mkdir(parents=True)
    sc.write(
        existing_dir,
        Sidecar(
            schema_version=CURRENT_SCHEMA_VERSION,
            store_url="https://x.bandcamp.com/album/y",
            mb_release_id="rel-aaa",
        ),
    )

    s = _bare_syncer()
    s.local_media.media_dir = str(tmp_path)
    s.ignores.is_ignored = MagicMock(return_value=True)  # already marked

    monkeypatch.setattr(
        "harmonist.bandcamp_hook._BCSyncer.sync_item",
        lambda self, item, encoding=None: True,
    )

    item = _StubItem(item_id=12345, url_hints={"subdomain": "x", "slug": "y"})
    assert s.sync_item(item) is False  # still short-circuits
    s.ignores.add.assert_not_called()  # but does NOT re-add


class _StubBandcamp:
    """Lightweight stand-in for bandcampsync.bandcamp.Bandcamp — no network."""

    def __init__(self, cookies):
        self.is_authenticated = True
        self.purchases = []

    def verify_authentication(self):
        return True

    def load_purchases(self, **kwargs):
        # 0.8 calls load_purchases(stop_when=...); accept and ignore kwargs.
        return True


def _patch_bandcamp(monkeypatch):
    import bandcampsync.sync as bc_sync_mod

    monkeypatch.setattr(bc_sync_mod, "Bandcamp", _StubBandcamp)


@pytest.mark.parametrize("dir_path_type", ["str", "Path"])
def test_harmonist_syncer_accepts_str_or_path(tmp_path, monkeypatch, dir_path_type):
    """HarmonistSyncer foolproofs the bandcampsync boundary: dir_path
    accepted as either str or Path, coerced to Path internally.

    Without the coercion, passing str crashes bandcampsync's LocalMedia.index()
    with `'str' object has no attribute 'iterdir'`.
    """

    music_dir = tmp_path / "music"
    music_dir.mkdir()
    ignores_file = tmp_path / "ignores.txt"
    ignores_file.write_text("# empty\n")

    _patch_bandcamp(monkeypatch)

    dir_arg = str(music_dir) if dir_path_type == "str" else music_dir
    syncer = HarmonistSyncer(
        cookies="fake",
        dir_path=dir_arg,
        media_format="alac",
        temp_dir_root=None,
        ign_file_path=str(ignores_file),
        ign_patterns="",
        notify_url=None,
        max_downloads_per_sync=5,
    )
    # bandcampsync's LocalMedia.media_dir should now be a Path regardless
    # of what we passed in.
    assert isinstance(syncer.local_media.media_dir, Path)
    assert syncer.local_media.media_dir == music_dir


def test_run_bandcamp_sync_end_to_end_with_stub(tmp_path, monkeypatch):
    """Drives _run_bandcamp_sync through the real HarmonistSyncer init chain
    with a stubbed Bandcamp, verifying no crashes from the config layer.
    """
    from harmonist.config import (
        BandcampConfig,
        Config,
        MusicBrainzConfig,
        PathsConfig,
        ServerConfig,
        TestConfig,
    )
    from harmonist.web.main import _run_bandcamp_sync

    music_dir = tmp_path / "music"
    music_dir.mkdir()
    config_dir = tmp_path / "cfg"
    config_dir.mkdir()
    (config_dir / "cookies.txt").write_text("fake-cookies")
    (config_dir / "ignores.txt").write_text("# empty\n")

    cfg = Config(
        paths=PathsConfig(config_dir=config_dir, music_dir=music_dir),
        bandcamp=BandcampConfig(),
        musicbrainz=MusicBrainzConfig(),
        server=ServerConfig(),
        test=TestConfig(mode="fixture"),
    )

    _patch_bandcamp(monkeypatch)
    result = _run_bandcamp_sync(cfg)
    assert result is not None


def test_sync_item_invokes_progress_callback(monkeypatch, tmp_path):
    """The runner gets per-item progress so the UI can show 'Syncing: X / Y'."""
    album_dir = tmp_path / "Artist" / "Album"
    album_dir.mkdir(parents=True)
    seen = []
    s = _bare_syncer()
    s._progress_callback = lambda label: seen.append(label)
    s.local_media.media_dir = str(tmp_path)
    s.local_media.get_path_for_purchase = MagicMock(return_value=album_dir)

    monkeypatch.setattr(
        "harmonist.bandcamp_hook._BCSyncer.sync_item",
        lambda self, item, encoding=None: True,
    )

    item = _StubItem(
        item_id=1,
        band_name="My Band",
        item_title="My Album",
        url_hints={"subdomain": "x", "slug": "y"},
    )
    s.sync_item(item)
    assert seen == ["My Band / My Album"]


def test_sync_item_callback_failure_does_not_break_sync(monkeypatch, tmp_path):
    """A buggy progress callback must never abort the actual sync."""
    album_dir = tmp_path / "Artist" / "Album"
    album_dir.mkdir(parents=True)

    def explode(_label):
        raise RuntimeError("callback broken")

    s = _bare_syncer()
    s._progress_callback = explode
    s.local_media.media_dir = str(tmp_path)
    s.local_media.get_path_for_purchase = MagicMock(return_value=album_dir)

    monkeypatch.setattr(
        "harmonist.bandcamp_hook._BCSyncer.sync_item",
        lambda self, item, encoding=None: True,
    )

    item = _StubItem(item_id=1, url_hints={"subdomain": "x", "slug": "y"})
    # Should complete without raising
    s.sync_item(item)


def test_sync_item_does_not_short_circuit_when_no_match(monkeypatch, tmp_path):
    """Genuine new purchase (no existing sidecar) → falls through to download."""
    s = _bare_syncer()
    s.local_media.media_dir = str(tmp_path)
    new_dir = tmp_path / "New" / "Album"
    new_dir.mkdir(parents=True)
    s.local_media.get_path_for_purchase = MagicMock(return_value=new_dir)

    parent_called = []

    def parent_sync_item(self, item, encoding=None):
        parent_called.append(True)
        return True

    monkeypatch.setattr("harmonist.bandcamp_hook._BCSyncer.sync_item", parent_sync_item)

    item = _StubItem(item_id=12345, url_hints={"subdomain": "new", "slug": "alb"})
    result = s.sync_item(item)
    assert result is True
    assert parent_called == [True]
    assert sc.has_sidecar(new_dir)
