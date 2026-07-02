"""Tests for bandcamp_hook — URL construction, cap, sidecar capture."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from mutagen.mp4 import MP4

from harmonist import library_index
from harmonist import sidecar as sc
from harmonist.bandcamp_hook import (
    HarmonistSyncer,
    album_slug,
    construct_bandcamp_url,
    survey_album_links,
    write_sidecar_for_item,
)
from harmonist.models import BandcampInfo, Sidecar
from harmonist.sidecar import CURRENT_SCHEMA_VERSION
from harmonist.tagger import ATOM_ALBUM

FIXTURES_DIR = Path(__file__).parent / "fixtures"
SINE_M4A = FIXTURES_DIR / "sine.m4a"


@pytest.fixture(autouse=True)
def _isolate_library_index():
    """sync_item / sync_items now read the global library_index (populated via the
    sidecar.write hook). Clear it around each test so cases don't bleed into one
    another."""
    library_index.clear()
    yield
    library_index.clear()


def _tag_album_title(album_dir: Path, title: str) -> None:
    """Give the album a tagged ©alb title — the backfill's title-match key
    (folder name is ignored). Drops a sine fixture and sets its album tag."""
    f = album_dir / "01 Track.m4a"
    shutil.copy(SINE_M4A, f)
    audio = MP4(f)
    audio[ATOM_ALBUM] = [title]
    audio.save()


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
    s._link_only = False
    s.new_items = 0
    s.skipped_for_limit = 0
    s._pending_this_run = []
    s._adopt_index = {}
    s._adopt_consumed = set()
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
    # Only item 222 (no sidecar carries it) is returned, as (item_id, url, label).
    assert s.unmatched_purchases() == [(222, "https://x.bandcamp.com/album/orphan", "X / Orphan")]


def test_sync_items_runs_parent_without_aborting(monkeypatch):
    """sync_items no longer aborts on a large would-download set — the per-sync
    download limit is enforced per item in sync_item instead."""
    s = _bare_syncer(max_downloads=2)
    s.bandcamp.purchases = [_StubItem(item_id=i) for i in range(20)]
    s.ignores.is_ignored = lambda item: False
    parent_called = []

    async def parent_sync_items(self):
        parent_called.append(True)

    monkeypatch.setattr("harmonist.bandcamp_hook._BCSyncer.sync_items", parent_sync_items)
    asyncio.run(s.sync_items())  # must not raise
    assert parent_called == [True]


def test_sync_item_caps_downloads_and_defers_rest(tmp_path, monkeypatch):
    """sync_item downloads up to the per-sync limit, then defers genuinely-new
    items to the next sync (counted in skipped_for_limit, not marked ignored)."""
    s = _bare_syncer(max_downloads=2)
    s.local_media = MagicMock()
    s.local_media.get_path_for_purchase = lambda item: tmp_path / str(item.item_id)
    s.local_media.is_locally_downloaded = lambda item, path: False
    s.ignores.is_ignored = lambda item: False
    # Parent "download" always succeeds; skip the post-download sidecar write.
    monkeypatch.setattr(
        "harmonist.bandcamp_hook._BCSyncer.sync_item",
        lambda self, item, encoding=None: True,
    )
    monkeypatch.setattr("harmonist.bandcamp_hook.write_sidecar_for_item", lambda *a, **k: True)

    for i in range(5):  # 5 genuinely-new items, cap 2
        s.sync_item(_StubItem(item_id=i))

    assert s.new_items == 2  # only 2 downloaded this run
    assert s.skipped_for_limit == 3  # the other 3 deferred to the next sync


def test_sync_item_limit_does_not_count_ignored_or_local(tmp_path, monkeypatch):
    """Past the limit, ignored / already-local items aren't counted as deferred
    (they wouldn't download anyway) — only genuinely-new ones inflate the tally."""
    s = _bare_syncer(max_downloads=0)  # download nothing → everything is "past the limit"
    s.local_media = MagicMock()
    s.local_media.get_path_for_purchase = lambda item: tmp_path / str(item.item_id)
    s.local_media.is_locally_downloaded = lambda item, path: item.item_id == 1
    s.ignores.is_ignored = lambda item: item.item_id == 2
    monkeypatch.setattr(
        "harmonist.bandcamp_hook._BCSyncer.sync_item",
        lambda self, item, encoding=None: False,
    )

    for i in range(4):  # 0=new, 1=local, 2=ignored, 3=new
        s.sync_item(_StubItem(item_id=i))

    assert s.skipped_for_limit == 2  # only items 0 and 3


def test_sync_item_link_only_skips_downloads(tmp_path, monkeypatch):
    """Adopt mode: a genuinely-new item does NOT download (the parent's real
    sync_item is never reached). On-disk matches still link via the short-circuit
    (which returns before this gate), so this only blocks new fetches."""
    s = _bare_syncer()
    s._link_only = True
    s.local_media = MagicMock()
    s.local_media.media_dir = str(tmp_path)
    s.ignores.is_ignored = lambda item: False
    downloaded: list[int] = []

    def fake_download(self, item, encoding=None):
        downloaded.append(item.item_id)
        return True

    monkeypatch.setattr("harmonist.bandcamp_hook._BCSyncer.sync_item", fake_download)
    # No url_hints → no on-disk match → would normally download; link-only blocks it.
    assert s.sync_item(_StubItem(item_id=42)) is False
    assert downloaded == []  # the real download was never invoked


def test_link_only_records_unmatched_purchase_as_pending(tmp_path):
    """Adopt mode: an unmatched purchase isn't auto-downloaded — it's recorded as a
    potential download (item_id, band/title, url, format) for the user to resolve."""
    s = _bare_syncer()
    s._link_only = True
    s.local_media = MagicMock()
    s.local_media.media_dir = str(tmp_path)
    s.media_format = "alac"
    s.ignores.is_ignored = lambda item: False
    item = _StubItem(
        item_id=42,
        band_name="Variant",
        item_title="Sequential Sleep",
        url_hints={"subdomain": "echospace313", "slug": "sequential-sleep"},
    )
    assert s.sync_item(item) is False
    assert len(s._pending_this_run) == 1
    p = s._pending_this_run[0]
    assert p.item_id == 42
    assert p.band == "Variant"
    assert p.title == "Sequential Sleep"
    assert p.url == "https://echospace313.bandcamp.com/album/sequential-sleep"
    assert p.fmt == "alac"


def test_sync_item_skips_download_when_release_on_disk_by_slug(tmp_path, monkeypatch):
    """Dedup backstop: a release already on disk under a DIFFERENT subdomain (same
    slug) and linked to a DIFFERENT purchase-id must NOT re-download — by_url is
    exact and by_slug skips linked albums, so only the slug-inclusive check sees it.
    This is the fix for the ~72 spurious re-downloads on a full-library sync."""
    album = tmp_path / "variant" / "sequential sleep"
    album.mkdir(parents=True)
    sc.write(
        album,
        Sidecar(
            schema_version=CURRENT_SCHEMA_VERSION,
            store_url="https://variant.bandcamp.com/album/sequential-sleep",  # artist page
            mb_release_id="rel-1",
            bandcamp=BandcampInfo(item_id=999),  # linked to a DIFFERENT purchase
        ),
    )
    s = _bare_syncer()
    s.local_media = MagicMock()
    s.local_media.media_dir = str(tmp_path)
    s.ignores.is_ignored = lambda item: False
    added: list[object] = []
    s.ignores.add = lambda item: added.append(getattr(item, "item_id", None))

    downloaded: list[int] = []

    def fake_download(self, item, encoding=None):
        downloaded.append(1)
        return True

    monkeypatch.setattr("harmonist.bandcamp_hook._BCSyncer.sync_item", fake_download)
    # Purchase: LABEL subdomain, SAME slug.
    item = _StubItem(
        item_id=4032507453,
        url_hints={"subdomain": "echospace313", "slug": "sequential-sleep"},
    )
    assert s.sync_item(item) is False
    assert downloaded == []  # did NOT re-download the existing release
    assert 4032507453 in added  # ignored so it doesn't churn every sync


def test_link_only_does_not_advance_checkpoint(monkeypatch):
    """A link-only sync downloads nothing, so it must NOT write bandcampsync's
    collection checkpoint — else the next full sync starts past purchases it only
    saw (e.g. a new purchase) and skips them forever."""
    s = _bare_syncer()
    s._link_only = True
    saved: list[int] = []
    monkeypatch.setattr(
        "harmonist.bandcamp_hook._BCSyncer._save_collection_checkpoint",
        lambda self: saved.append(1),
    )
    s._save_collection_checkpoint()
    assert saved == []  # parent never reached → checkpoint left as-is


def test_normal_sync_advances_checkpoint(monkeypatch):
    s = _bare_syncer()
    s._link_only = False
    saved: list[int] = []
    monkeypatch.setattr(
        "harmonist.bandcamp_hook._BCSyncer._save_collection_checkpoint",
        lambda self: saved.append(1),
    )
    s._save_collection_checkpoint()
    assert saved == [1]  # parent called → checkpoint advances normally


def test_sync_item_skips_relink_for_already_linked_album(tmp_path, monkeypatch):
    """The on-disk short-circuit runs every sync (a link-only full re-page pages
    the WHOLE collection), but an album already linked must NOT be re-written —
    else every sidecar's mtime bumps (slow rescan) and the feed is spammed."""
    album = tmp_path / "Artist" / "Album"
    album.mkdir(parents=True)
    sc.write(
        album,
        Sidecar(
            schema_version=CURRENT_SCHEMA_VERSION,
            store_url="https://x.bandcamp.com/album/a",
            mb_release_id="rel-1",
            bandcamp=BandcampInfo(item_id=555),  # already linked
        ),
    )
    s = _bare_syncer()
    s.local_media = MagicMock()
    s.local_media.media_dir = str(tmp_path)
    s.ignores.is_ignored = lambda item: True

    wrote: list[int] = []

    def fake_write(*a, **k):
        wrote.append(1)
        return True

    monkeypatch.setattr("harmonist.bandcamp_hook.write_sidecar_for_item", fake_write)
    # Purchase URL matches the album's store_url → short-circuit finds it.
    item = _StubItem(item_id=555, url_hints={"subdomain": "x", "slug": "a"})
    assert s.sync_item(item) is False
    assert wrote == []  # already linked → no rewrite (no mtime bump, no spam)


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


# (find_existing_album_by_url/by_slug removed — replaced by library_index lookups,
#  covered by test_library_index.)


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


def test_sync_item_short_circuit_records_transition_to_activity(monkeypatch, tmp_path):
    """Linking an on-disk album during the download loop is a Needs Sync →
    Library transition — it should hit the Activity feed (not just the server
    log), like the ignored-purchase backfill."""
    from harmonist import activity

    existing_dir = tmp_path / "Artist" / "Album"
    existing_dir.mkdir(parents=True)
    sc.write(
        existing_dir,
        Sidecar(
            schema_version=CURRENT_SCHEMA_VERSION,
            store_url="https://x.bandcamp.com/album/y",
            mb_release_id="rel-a",
        ),
    )
    s = _bare_syncer()
    s.local_media.media_dir = str(tmp_path)
    s.ignores.is_ignored = MagicMock(return_value=False)
    monkeypatch.setattr(
        "harmonist.bandcamp_hook._BCSyncer.sync_item",
        lambda self, item, encoding=None: True,
    )
    item = _StubItem(
        item_id=12345,
        band_name="Artist",
        item_title="Album",
        url_hints={"subdomain": "x", "slug": "y"},
    )
    activity.clear()
    s.sync_item(item)
    msgs = [e.message for e in activity.recent(5)]
    assert any("Needs Sync → Library" in m and "12345" in m for m in msgs)


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


def test_sync_item_ambiguous_slug_skips_download_no_dupe(monkeypatch, tmp_path):
    """Two unlinked albums share the slug → we can't tell which to LINK, but the
    release is plainly already on disk, so we must NOT download a third copy. (The
    old behaviour fell through and downloaded a duplicate.) Picking which copy to
    link is deferred to the potential-download Match flow."""
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
    parent_called = []

    def parent_sync_item(self, item, encoding=None):
        parent_called.append(True)
        return True

    monkeypatch.setattr("harmonist.bandcamp_hook._BCSyncer.sync_item", parent_sync_item)

    item = _StubItem(item_id=5, url_hints={"subdomain": "artist", "slug": "home"})
    assert s.sync_item(item) is False
    assert parent_called == []  # ambiguous but on-disk → NO download (no duplicate)


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


# ---------- ignored-purchase backfill (#47) ----------


_WUS_URL = "https://quietdetails.bandcamp.com/album/while-the-universe-sleeps"


def _wus_item(item_id: int) -> _StubItem:
    return _StubItem(
        item_id=item_id,
        band_name="Variant",
        item_title="While the Universe Sleeps",
        url_hints={"subdomain": "quietdetails", "slug": "while-the-universe-sleeps"},
    )


def test_survey_album_links_splits_unlinked_and_linked(tmp_path):
    linked = tmp_path / "Linked"
    linked.mkdir()
    sc.write(
        linked,
        Sidecar(
            schema_version=CURRENT_SCHEMA_VERSION,
            store_url=_WUS_URL,
            bandcamp=BandcampInfo(item_id=631669900),
        ),
    )
    unlinked = tmp_path / "Unlinked"
    unlinked.mkdir()
    sc.write(
        unlinked,
        Sidecar(schema_version=CURRENT_SCHEMA_VERSION, store_url=_WUS_URL, mb_release_id="rel-x"),
    )
    # An unlinked album with an artist-root (slug-less) Bandcamp store_url.
    slugless = tmp_path / "Slugless"
    slugless.mkdir()
    sc.write(
        slugless,
        Sidecar(
            schema_version=CURRENT_SCHEMA_VERSION,
            store_url="https://quietdetails.bandcamp.com",
            mb_release_id="rel-y",
        ),
    )
    by_slug, slugless_dirs, linked_ids = survey_album_links(tmp_path)
    assert by_slug == {"album/while-the-universe-sleeps": [unlinked]}
    assert slugless_dirs == [slugless]
    assert linked_ids == {631669900}


def test_backfill_links_ignored_purchase_to_unlinked_album(tmp_path):
    """The #47 fix: a purchase already in ignores.txt whose on-disk album is
    unlinked gets its item_id filled in (never reachable via sync_item, which
    bandcampsync skips for ignored items)."""
    album = tmp_path / "Variant" / "Long-Form"
    album.mkdir(parents=True)
    sc.write(
        album,
        Sidecar(
            schema_version=CURRENT_SCHEMA_VERSION,
            store_url=_WUS_URL,
            mb_release_id="8954fdcc-long-form",
        ),
    )
    s = _bare_syncer()
    s.local_media.media_dir = str(tmp_path)
    s.bandcamp.purchases = [_wus_item(3417563775)]
    s.ignores.is_ignored = lambda item: True  # already downloaded → ignored

    s._backfill_ignored_purchases(survey_album_links(Path(s.local_media.media_dir)))

    loaded = sc.read(album)
    assert loaded.bandcamp.item_id == 3417563775
    assert loaded.mb_release_id == "8954fdcc-long-form"  # MB identity preserved


def test_backfill_links_slugless_album_by_title(tmp_path):
    """A manual download whose only Bandcamp URL is the artist root (no /album/
    slug) links to its purchase via the phase-2 title fallback."""
    album = tmp_path / "anything"  # folder name is ignored; the ©alb tag is the key
    album.mkdir()
    sc.write(
        album,
        Sidecar(
            schema_version=CURRENT_SCHEMA_VERSION,
            store_url="https://quietdetails.bandcamp.com",  # artist-root placeholder
            mb_release_id="rel-manual",
        ),
    )
    _tag_album_title(album, "While the Universe Sleeps")
    s = _bare_syncer()
    s.local_media.media_dir = str(tmp_path)
    s.bandcamp.purchases = [_wus_item(3417563775)]
    s.ignores.is_ignored = lambda item: True

    s._backfill_ignored_purchases(survey_album_links(Path(s.local_media.media_dir)))

    loaded = sc.read(album)
    assert loaded.bandcamp.item_id == 3417563775  # linked by title
    assert loaded.mb_release_id == "rel-manual"


def test_backfill_does_not_touch_linked_sibling_sharing_slug(tmp_path):
    """Regular + long-form editions share the store-URL slug. The regular is
    already linked; the long-form is not. Backfill links ONLY the long-form and
    leaves the linked regular edition untouched."""
    regular = tmp_path / "Variant" / "Regular"
    regular.mkdir(parents=True)
    sc.write(
        regular,
        Sidecar(
            schema_version=CURRENT_SCHEMA_VERSION,
            store_url=_WUS_URL,
            bandcamp=BandcampInfo(item_id=631669900),
        ),
    )
    longform = tmp_path / "Variant" / "Long-Form"
    longform.mkdir(parents=True)
    sc.write(
        longform,
        Sidecar(schema_version=CURRENT_SCHEMA_VERSION, store_url=_WUS_URL, mb_release_id="rel-lf"),
    )
    s = _bare_syncer()
    s.local_media.media_dir = str(tmp_path)
    s.bandcamp.purchases = [_wus_item(3417563775)]
    s.ignores.is_ignored = lambda item: True

    s._backfill_ignored_purchases(survey_album_links(Path(s.local_media.media_dir)))

    assert sc.read(longform).bandcamp.item_id == 3417563775  # linked
    assert sc.read(regular).bandcamp.item_id == 631669900  # untouched


def test_backfill_does_not_mislink_linked_purchase_to_unlinked_sibling(tmp_path):
    """The real "While the Universe Sleeps" trap: a standard + long-form edition
    share ONE store_url slug. The standard is linked (item 631669900); the
    long-form is unlinked but carries the *same* store_url (its MB release only
    has the public page URL). The standard's purchase must NOT be attached to
    the unlinked long-form album just because the slug matches."""
    regular = tmp_path / "Variant" / "Regular"
    regular.mkdir(parents=True)
    sc.write(
        regular,
        Sidecar(
            schema_version=CURRENT_SCHEMA_VERSION,
            store_url=_WUS_URL,
            bandcamp=BandcampInfo(item_id=631669900),
        ),
    )
    longform = tmp_path / "Variant" / "Long-Form"
    longform.mkdir(parents=True)
    sc.write(
        longform,
        Sidecar(schema_version=CURRENT_SCHEMA_VERSION, store_url=_WUS_URL, mb_release_id="rel-lf"),
    )
    s = _bare_syncer()
    s.local_media.media_dir = str(tmp_path)
    # Both purchases are ignored (already downloaded). The standard's URL slug
    # matches the long-form album's store_url slug — the mis-link trap.
    s.bandcamp.purchases = [_wus_item(631669900)]
    s.ignores.is_ignored = lambda item: True

    s._backfill_ignored_purchases(survey_album_links(Path(s.local_media.media_dir)))

    # The long-form stays unlinked — we did NOT attach the standard's item_id.
    assert sc.read(longform).bandcamp is None
    assert sc.read(regular).bandcamp.item_id == 631669900  # untouched


def test_backfill_title_tiebreak_pairs_editions(tmp_path):
    """Two editions share ONE store URL slug, with one purchase each. A title
    match (tagged ©alb vs purchase title) pairs them correctly — the long-form
    album to the long-form purchase, the standard to the standard."""
    standard = tmp_path / "Variant" / "std"
    standard.mkdir(parents=True)
    sc.write(
        standard,
        Sidecar(schema_version=CURRENT_SCHEMA_VERSION, store_url=_WUS_URL, mb_release_id="rel-std"),
    )
    _tag_album_title(standard, "While the Universe Sleeps")
    longform = tmp_path / "Variant" / "lf"
    longform.mkdir(parents=True)
    sc.write(
        longform,
        Sidecar(schema_version=CURRENT_SCHEMA_VERSION, store_url=_WUS_URL, mb_release_id="rel-lf"),
    )
    _tag_album_title(longform, "While the Universe Sleeps (Long-Form Edition)")
    p_std = _StubItem(
        item_id=631669900,
        band_name="Variant",
        item_title="While the Universe Sleeps",
        url_hints={"subdomain": "quietdetails", "slug": "while-the-universe-sleeps"},
    )
    p_lf = _StubItem(
        item_id=3417563775,
        band_name="Variant",
        item_title="While the Universe Sleeps (Long-Form Edition)",
        url_hints={"subdomain": "quietdetails", "slug": "while-the-universe-sleeps"},
    )
    s = _bare_syncer()
    s.local_media.media_dir = str(tmp_path)
    s.bandcamp.purchases = [p_std, p_lf]
    s.ignores.is_ignored = lambda item: True

    s._backfill_ignored_purchases(survey_album_links(Path(s.local_media.media_dir)))

    assert sc.read(standard).bandcamp.item_id == 631669900
    assert sc.read(longform).bandcamp.item_id == 3417563775


def test_backfill_marks_ambiguous_when_title_cannot_separate(tmp_path):
    """Two editions share a slug but neither album's tagged title matches a
    purchase title (can't pick) → each album records the candidate ids and
    leaves NEEDS_SYNC (no nag), rather than guessing."""
    a = tmp_path / "Variant" / "a"
    a.mkdir(parents=True)
    sc.write(
        a, Sidecar(schema_version=CURRENT_SCHEMA_VERSION, store_url=_WUS_URL, mb_release_id="ra")
    )
    _tag_album_title(a, "Edition A")
    b = tmp_path / "Variant" / "b"
    b.mkdir(parents=True)
    sc.write(
        b, Sidecar(schema_version=CURRENT_SCHEMA_VERSION, store_url=_WUS_URL, mb_release_id="rb")
    )
    _tag_album_title(b, "Edition B")
    # Purchase titles match neither folder.
    p1 = _StubItem(
        item_id=111,
        item_title="Totally Different One",
        url_hints={"subdomain": "quietdetails", "slug": "while-the-universe-sleeps"},
    )
    p2 = _StubItem(
        item_id=222,
        item_title="Totally Different Two",
        url_hints={"subdomain": "quietdetails", "slug": "while-the-universe-sleeps"},
    )
    s = _bare_syncer()
    s.local_media.media_dir = str(tmp_path)
    s.bandcamp.purchases = [p1, p2]
    s.ignores.is_ignored = lambda item: True

    s._backfill_ignored_purchases(survey_album_links(Path(s.local_media.media_dir)))

    # Both albums carry the full candidate set, no single item_id.
    for d in (a, b):
        bc = sc.read(d).bandcamp
        assert bc.item_id is None
        assert bc.candidate_item_ids == [111, 222]


def test_backfill_cross_slug_title_fallback_links_longform(tmp_path, caplog):
    """The real WTUS case: the long-form album's store_url is the PUBLIC page
    (shared with the standard), but the long-form's own purchase has a DIFFERENT
    URL. Phase 1 links the standard by URL; phase 2 links the long-form by a
    unique title match across the URL mismatch."""
    standard = tmp_path / "Variant" / "std"
    standard.mkdir(parents=True)
    sc.write(
        standard,
        Sidecar(schema_version=CURRENT_SCHEMA_VERSION, store_url=_WUS_URL, mb_release_id="rel-std"),
    )
    _tag_album_title(standard, "While the Universe Sleeps")
    longform = tmp_path / "Variant" / "lf"
    longform.mkdir(parents=True)
    sc.write(
        longform,
        Sidecar(schema_version=CURRENT_SCHEMA_VERSION, store_url=_WUS_URL, mb_release_id="rel-lf"),
    )
    _tag_album_title(longform, "While the Universe Sleeps (Long-Form Edition)")
    # Standard purchase shares the album's public URL; the long-form purchase
    # has its OWN distinct URL (so slug matching can't tie it to the album).
    p_std = _StubItem(
        item_id=631669900,
        band_name="Variant",
        item_title="While the Universe Sleeps",
        url_hints={"subdomain": "quietdetails", "slug": "while-the-universe-sleeps"},
    )
    p_lf = _StubItem(
        item_id=3417563775,
        band_name="Variant",
        item_title="While the Universe Sleeps (Long-Form Edition)",
        url_hints={
            "subdomain": "quietdetails",
            "slug": "while-the-universe-sleeps-long-form-edition",
        },
    )
    s = _bare_syncer()
    s.local_media.media_dir = str(tmp_path)
    s.bandcamp.purchases = [p_std, p_lf]
    s.ignores.is_ignored = lambda item: True

    import logging

    with caplog.at_level(logging.WARNING, logger="harmonist.bandcamp_hook"):
        s._backfill_ignored_purchases(survey_album_links(Path(s.local_media.media_dir)))

    assert sc.read(standard).bandcamp.item_id == 631669900  # by URL slug
    assert sc.read(longform).bandcamp.item_id == 3417563775  # by title across mismatch
    # A title link warns it could be a mis-tag (URL mismatch is inherent), naming
    # both URLs so the user can see it.
    warn = " ".join(r.message for r in caplog.records if r.levelno >= logging.WARNING)
    assert "Possible mis-tag" in warn
    assert "while-the-universe-sleeps-long-form-edition" in warn  # the purchase URL
    assert "while-the-universe-sleeps" in warn  # the album's store URL


def test_backfill_slug_match_does_not_warn_mistag(tmp_path, caplog):
    """A normal URL-slug link (the album's store URL matches the purchase) is NOT
    a possible mis-tag — only the cross-slug title fallback warns."""
    import logging

    album = tmp_path / "Artist" / "Album"
    album.mkdir(parents=True)
    url = "https://x.bandcamp.com/album/album"
    sc.write(
        album,
        Sidecar(schema_version=CURRENT_SCHEMA_VERSION, store_url=url, mb_release_id="rel-a"),
    )
    item = _StubItem(item_id=42, item_title="Album", url_hints={"subdomain": "x", "slug": "album"})
    s = _bare_syncer()
    s.local_media.media_dir = str(tmp_path)
    s.bandcamp.purchases = [item]
    s.ignores.is_ignored = lambda i: True

    with caplog.at_level(logging.WARNING, logger="harmonist.bandcamp_hook"):
        s._backfill_ignored_purchases(survey_album_links(Path(s.local_media.media_dir)))

    assert sc.read(album).bandcamp.item_id == 42  # linked by URL slug
    assert "Possible mis-tag" not in " ".join(r.message for r in caplog.records)


def test_backfill_skips_non_ignored_items(tmp_path):
    """Non-ignored purchases are left to sync_item's own backfill — the ignored
    pass must not touch them."""
    album = tmp_path / "Unlinked"
    album.mkdir()
    sc.write(
        album,
        Sidecar(schema_version=CURRENT_SCHEMA_VERSION, store_url=_WUS_URL, mb_release_id="rel-x"),
    )
    s = _bare_syncer()
    s.local_media.media_dir = str(tmp_path)
    s.bandcamp.purchases = [_wus_item(3417563775)]
    s.ignores.is_ignored = lambda item: False  # NOT ignored

    s._backfill_ignored_purchases(survey_album_links(Path(s.local_media.media_dir)))

    assert sc.read(album).bandcamp is None  # untouched


# ---------- adoption auto-link (artist-root store_url + title) ----------


def _adopt_syncer(tmp_path) -> HarmonistSyncer:
    """A link-only bare syncer with the adoption index built from disk."""
    s = _bare_syncer()
    s._link_only = True
    s.local_media.media_dir = str(tmp_path)
    s.ignores.is_ignored = lambda item: False
    s._adopt_index = s._build_adopt_index(survey_album_links(Path(tmp_path))[1])
    s._adopt_consumed = set()
    return s


def _artist_root_album(tmp_path, folder: str, *, store_url: str, title: str) -> Path:
    d = tmp_path / folder
    d.mkdir(parents=True)
    sc.write(
        d,
        Sidecar(schema_version=CURRENT_SCHEMA_VERSION, store_url=store_url, mb_release_id="rel-ar"),
    )
    _tag_album_title(d, title)
    return d


def test_adopt_links_artist_root_album_by_subdomain_and_title(tmp_path):
    """A purchase whose Bandcamp subdomain matches an unlinked artist-root album
    AND whose title matches → confidently auto-linked (adopts the full /album/
    URL, leaves Needs Sync), NOT surfaced as a potential download."""
    album = _artist_root_album(
        tmp_path,
        "Banco de Gaia/Live at Glastonbury",
        store_url="https://bancodegaia.bandcamp.com",  # artist root, no /album/ slug
        title="Live at Glastonbury",
    )
    p = _StubItem(
        item_id=555,
        band_name="Banco de Gaia",
        item_title="Live at Glastonbury",
        url_hints={"subdomain": "bancodegaia", "slug": "live-at-glastonbury"},
    )
    s = _adopt_syncer(tmp_path)
    s.bandcamp.purchases = [p]

    assert s.sync_item(p) is False  # linked, no download
    loaded = sc.read(album)
    assert loaded.bandcamp.item_id == 555
    assert loaded.store_url == "https://bancodegaia.bandcamp.com/album/live-at-glastonbury"
    assert s._pending_this_run == []  # NOT a potential download


def test_adopt_skips_album_with_no_store_url(tmp_path):
    """An album with NO Bandcamp store_url (no evidence it came from Bandcamp) is
    NOT auto-linked on a name coincidence — it stays for manual review."""
    d = tmp_path / "Barker" / "Utility"
    d.mkdir(parents=True)
    sc.write(d, Sidecar(schema_version=CURRENT_SCHEMA_VERSION, mb_release_id="rel-b"))
    _tag_album_title(d, "Utility")
    p = _StubItem(
        item_id=556,
        band_name="Barker",
        item_title="Utility",
        url_hints={"subdomain": "sambarker", "slug": "utility"},
    )
    s = _adopt_syncer(tmp_path)
    s.bandcamp.purchases = [p]

    assert s.sync_item(p) is False
    assert sc.read(d).bandcamp is None  # not linked
    assert [x.item_id for x in s._pending_this_run] == [556]  # surfaced for review


def test_adopt_skips_on_subdomain_mismatch(tmp_path):
    """Same title but a DIFFERENT Bandcamp subdomain (e.g. a label page) → not
    confident → manual review, not auto-linked."""
    album = _artist_root_album(
        tmp_path, "Artist/Album", store_url="https://somelabel.bandcamp.com", title="Album"
    )
    p = _StubItem(
        item_id=557,
        band_name="Artist",
        item_title="Album",
        url_hints={"subdomain": "theartist", "slug": "album"},
    )
    s = _adopt_syncer(tmp_path)
    s.bandcamp.purchases = [p]

    assert s.sync_item(p) is False
    assert sc.read(album).bandcamp is None
    assert [x.item_id for x in s._pending_this_run] == [557]


def test_adopt_skips_when_ambiguous(tmp_path):
    """Two unlinked artist-root albums share host + title → can't tell which →
    manual review."""
    a1 = _artist_root_album(
        tmp_path, "Artist/One", store_url="https://artist.bandcamp.com", title="Live"
    )
    a2 = _artist_root_album(
        tmp_path, "Artist/Two", store_url="https://artist.bandcamp.com", title="Live"
    )
    p = _StubItem(
        item_id=558,
        band_name="Artist",
        item_title="Live",
        url_hints={"subdomain": "artist", "slug": "live"},
    )
    s = _adopt_syncer(tmp_path)
    s.bandcamp.purchases = [p]

    assert s.sync_item(p) is False
    assert sc.read(a1).bandcamp is None
    assert sc.read(a2).bandcamp is None
    assert [x.item_id for x in s._pending_this_run] == [558]


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
