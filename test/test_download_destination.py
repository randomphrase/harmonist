"""Destination safety against the real bandcampsync downloader (#703)."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from zipfile import ZipFile

import pytest
from bandcampsync.bandcamp import BandcampItem

from harmonist import activity, library_index, pending_downloads, sidecar
from harmonist.bandcamp_hook import HarmonistSyncer
from harmonist.models import BandcampInfo, Sidecar


def _purchase(item_id=202, title="Favourite"):
    return BandcampItem(
        {
            "item_id": item_id,
            "item_type": "album",
            "band_name": "Artist",
            "item_title": title,
            "item_url": f"https://artist.bandcamp.com/album/edition-{item_id}",
            "is_preorder": False,
            "token": f"token-{item_id}",
            "purchased": None,
        }
    )


@pytest.fixture
def downloads(tmp_path, monkeypatch):
    """Only the network is fake; destination selection, skip and writes are real."""
    library_index.clear()
    pending_downloads.reset()
    music = tmp_path / "music"
    music.mkdir()
    ignores = tmp_path / "ignores.txt"
    ignores.write_text("# empty\n")
    bandcamp = MagicMock(purchases=[], collection_items=[])
    bandcamp.check_download_stat.return_value = "https://bandcamp.invalid/download"
    monkeypatch.setattr("bandcampsync.sync.Bandcamp", lambda cookies, impersonate: bandcamp)

    def download_file(url, target, impersonate):
        with ZipFile(target, "w") as archive:
            archive.write(Path(__file__).parent / "fixtures" / "sine.m4a", "01 Track.m4a")
        return "application/zip"

    network = MagicMock(side_effect=download_file)
    monkeypatch.setattr("bandcampsync.sync.download_file", network)
    syncer = HarmonistSyncer(
        cookies="fake",
        dir_path=music,
        media_format="alac",
        max_downloads_per_sync=5,
        ign_file_path=ignores,
        auto_run=False,
    )
    activity.install_log_handler()
    yield syncer, network
    library_index.clear()
    pending_downloads.reset()


def _snapshot(directory):
    return {p.name: p.read_bytes() for p in directory.iterdir()}


@pytest.mark.parametrize("approved", [False, True])
@pytest.mark.parametrize("limit", [0, 5])
def test_different_purchase_marker_blocks_before_upstream_skip(downloads, approved, limit):
    syncer, network = downloads
    purchase = _purchase()
    destination = syncer.local_media.get_path_for_purchase(purchase)
    destination.mkdir(parents=True)
    (destination / "bandcamp_item_id.txt").write_text("101\n")
    (destination / "01 Track.m4a").write_bytes(b"original edition")
    before = _snapshot(destination)
    syncer._max_downloads_per_sync = limit
    if approved:
        pending_downloads.approve(purchase.item_id)

    assert syncer.sync_item(purchase) is False

    assert syncer._unfinished == {202}
    assert [p.item_id for p in syncer._pending_this_run] == [202]
    assert not syncer.ignores.is_ignored(purchase)
    network.assert_not_called()
    assert _snapshot(destination) == before
    errors = [e.message for e in activity.recent() if "Download blocked" in e.message]
    assert len(errors) == 1
    assert str(destination) in errors[0]
    assert "202" in errors[0]


@pytest.mark.parametrize(
    "ownership",
    [
        "unknown",
        "different_sidecar",
        "corrupt_marker",
        "corrupt_sidecar",
        "unsupported_sidecar",
        "conflicting",
        "conflicting_marker",
    ],
)
def test_occupied_destination_never_receives_files(downloads, ownership):
    syncer, network = downloads
    purchase = _purchase()
    destination = syncer.local_media.get_path_for_purchase(purchase)
    destination.mkdir(parents=True)
    (destination / "01 Track.m4a").write_bytes(b"original edition")
    if ownership in {"different_sidecar", "conflicting"}:
        sidecar.write(destination, Sidecar(bandcamp=BandcampInfo(item_id=101)))
    if ownership == "conflicting":
        (destination / "bandcamp_item_id.txt").write_text("202\n")
    if ownership == "corrupt_marker":
        (destination / "bandcamp_item_id.txt").write_text("not an ID\n")
    if ownership == "corrupt_sidecar":
        (destination / ".harmonist.json").write_text("not JSON")
    if ownership == "unsupported_sidecar":
        (destination / ".harmonist.json").write_text(json.dumps({"schema_version": 999}))
    if ownership == "conflicting_marker":
        sidecar.write(destination, Sidecar(bandcamp=BandcampInfo(item_id=202)))
        (destination / "bandcamp_item_id.txt").write_text("101\n")
    before = _snapshot(destination)

    assert syncer.sync_item(purchase) is False

    network.assert_not_called()
    assert _snapshot(destination) == before
    assert syncer._unfinished == {202}
    assert [p.item_id for p in syncer._pending_this_run] == [202]


def test_unreadable_destination_is_blocked_and_reported(downloads, monkeypatch):
    syncer, network = downloads
    purchase = _purchase()
    destination = syncer.local_media.get_path_for_purchase(purchase)
    destination.mkdir(parents=True)
    original_iterdir = Path.iterdir

    def unreadable(path):
        if path == destination:
            raise PermissionError("cannot list destination")
        return original_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", unreadable)
    assert syncer.sync_item(purchase) is False
    network.assert_not_called()
    assert syncer._unfinished == {202}
    assert any("cannot list destination" in e.message for e in activity.recent())


@pytest.mark.parametrize("ownership", ["marker", "sidecar", "both"])
def test_exact_purchase_is_skipped_without_rewriting(downloads, ownership):
    syncer, network = downloads
    purchase = _purchase()
    destination = syncer.local_media.get_path_for_purchase(purchase)
    destination.mkdir(parents=True)
    (destination / "01 Track.m4a").write_bytes(b"already downloaded")
    if ownership in {"marker", "both"}:
        (destination / "bandcamp_item_id.txt").write_text("202\n")
    if ownership in {"sidecar", "both"}:
        sidecar.write(destination, Sidecar(bandcamp=BandcampInfo(item_id=202)))
    before = _snapshot(destination)

    assert syncer.sync_item(purchase) is False
    assert syncer.sync_item(purchase) is False

    network.assert_not_called()
    assert _snapshot(destination) == before
    assert syncer._unfinished == set()
    assert syncer._pending_this_run == []


@pytest.mark.parametrize("empty_folder", [False, True])
def test_free_destination_downloads_once(downloads, empty_folder):
    syncer, network = downloads
    purchase = _purchase()
    destination = syncer.local_media.get_path_for_purchase(purchase)
    if empty_folder:
        destination.mkdir(parents=True)

    assert syncer.sync_item(purchase) is True
    before = _snapshot(destination)
    assert syncer.sync_item(purchase) is False

    network.assert_called_once()
    assert _snapshot(destination) == before
    assert sidecar.read(destination).bandcamp.item_id == 202


def test_download_moved_aside_by_its_name_is_recorded_where_it_landed(downloads, monkeypatch):
    """bandcampsync picks a "Title [id]" folder during sync_item when the ignore
    file lists another purchase under this name."""
    syncer, _ = downloads
    recorded = []
    monkeypatch.setattr(
        "harmonist.bandcamp_hook.audit.record",
        lambda event, **fields: recorded.append((event, fields)),
    )
    syncer.ignores.add(_purchase(101, "Favourite"))
    purchase = _purchase(202, "Favourite")
    plain = syncer.local_media.get_path_for_purchase(purchase)

    assert syncer.sync_item(purchase) is True

    landed = plain.with_name("Favourite [202]")
    assert (landed / "01 Track.m4a").is_file()
    written = sidecar.read(landed)
    assert written is not None and written.bandcamp.item_id == 202
    assert [f["path"] for event, f in recorded if event == "download"] == [landed]
    assert not plain.exists()


def test_undecided_lookalike_stays_reachable_by_later_syncs(downloads, monkeypatch):
    """A purchase held for the user's decision must not fall behind the checkpoint,
    or the next sync stops before it and Download can never be acted on."""
    syncer, network = downloads
    lookalike, older = _purchase(202, "Tides"), _purchase(101, "Older")
    other_page = SimpleNamespace(album_dir=Path("Tides"), linked=True)
    monkeypatch.setattr(
        syncer,
        "_copies_of_release",
        lambda url: ([], [other_page] if url.endswith("edition-202") else []),
    )
    syncer.bandcamp.purchases = [lookalike]
    syncer.bandcamp.collection_items = [lookalike, older]

    asyncio.run(syncer.sync_items())
    asyncio.run(syncer.sync_items())

    network.assert_not_called()
    assert [p.item_id for p in pending_downloads.all_pending()] == [202]
    syncer.collection_checkpoint_token = syncer._load_collection_checkpoint()
    assert syncer._should_stop_loading_purchase(lookalike) is False
    assert syncer._should_stop_loading_purchase(older) is True

    pending_downloads.approve(202)
    assert syncer.sync_item(lookalike) is True
    network.assert_called_once()


def test_cleaned_filename_collision_stays_pending_across_syncs(downloads):
    syncer, network = downloads
    older, newer = _purchase(101, "Favourite?"), _purchase(202, "Favourite")
    destination = syncer.local_media.get_path_for_purchase(older)
    assert destination == syncer.local_media.get_path_for_purchase(newer)
    destination.mkdir(parents=True)
    (destination / "01 Track.m4a").write_bytes(b"original edition")
    (destination / "bandcamp_item_id.txt").write_text("101\n")
    before = _snapshot(destination)
    syncer.bandcamp.purchases = [newer]
    syncer.bandcamp.collection_items = [newer, older]

    asyncio.run(syncer.sync_items())
    asyncio.run(syncer.sync_items())

    network.assert_not_called()
    assert _snapshot(destination) == before
    assert [p.item_id for p in pending_downloads.all_pending()] == [202]
    checkpoint = json.loads(syncer.state_file_path.read_text())
    assert checkpoint["last_seen_item_id"] == 101
    assert not syncer.ignores.is_ignored(newer)

    # Moving the old edition intact frees the destination; the approved retry
    # then downloads normally without changing the archived ownership evidence.
    moved = destination.with_name("Favourite (original)")
    destination.rename(moved)
    pending_downloads.approve(202)
    assert syncer.sync_item(newer) is True
    network.assert_called_once()
    assert _snapshot(moved) == before
