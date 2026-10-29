"""The scan owns NFO discovery, including directories without audio."""

import os
from unittest.mock import Mock

from harmonist import artist_nfo, formats, scanner
from harmonist.web.scan_runner import ScanRunner
from test.test_artist_web import ALICE, BOB, SOLO, artist_client
from test.test_scan_runner import _album

__all__ = ["artist_client"]


def write_nfo(root, directory, mbid=None, name="Local artist"):
    path = root / directory / "artist.nfo"
    path.parent.mkdir(parents=True, exist_ok=True)
    identity = f"<musicbrainzartistid>{mbid}</musicbrainzartistid>" if mbid else ""
    path.write_text(f"<artist><name>{name}</name>{identity}</artist>")
    return path


def test_global_inventory_finds_nfos_away_from_albums_and_checks_inbox_credits(artist_client):
    client, cfg, network = artist_client
    root = cfg.paths.music_dir
    match = write_nfo(root, "metadata/nested/unrelated name", BOB, "Alice")
    pending = write_nfo(root, "metadata/pending", SOLO)
    missing = write_nfo(root, "metadata/no-id")
    unrelated = write_nfo(root, "metadata/uncredited", "77777777-7777-4777-8777-777777777777")
    detail = client.get(f"/artist/{BOB}")
    assert detail.context["artist"].nfo_paths == (match,)
    assert detail.context["nfo"].profile.path == match
    view = client.get("/artists").context["artists_view"]
    assert {p.path for p in view["nfo_orphans"]} == {missing, unrelated}
    assert pending not in {p.path for p in view["nfo_orphans"]}
    network.assert_not_called()


def test_duplicate_ids_are_conflicts_even_when_a_source_is_requested(artist_client):
    client, cfg, _ = artist_client
    first = write_nfo(cfg.paths.music_dir, "metadata/one", BOB)
    second = write_nfo(cfg.paths.music_dir, "metadata/two", BOB)
    import hashlib

    selection = hashlib.sha256(str(first).encode()).hexdigest()[:24]
    detail = client.get(f"/artist/{BOB}?nfo={selection}")
    assert detail.context["nfo"].profile is None
    assert detail.context["nfo"].candidates == (first, second)
    assert detail.context["nfo"].errors
    assert str(first) in detail.text and str(second) in detail.text
    assert BOB in client.get("/artists").context["artists_view"]["nfo_conflicts"]
    assert [
        a.mbid for a in client.get("/artists?nfo=conflict").context["artists_view"]["rows"]
    ] == [BOB]


def test_inventory_is_served_from_the_scan_without_request_time_discovery(
    artist_client, monkeypatch
):
    client, cfg, _ = artist_client
    path = write_nfo(cfg.paths.music_dir, "deep/metadata", BOB)
    runner = client.app.state.scan_runner
    runner.refresh_now()
    monkeypatch.setattr(runner, "is_engaged", lambda: True)
    read = Mock(side_effect=AssertionError("requests must use the inventory"))
    monkeypatch.setattr(artist_nfo, "read", read)
    monkeypatch.setattr(scanner, "iter_album_dirs", read)
    assert client.get(f"/artist/{BOB}").context["nfo"].profile.path == path
    assert client.get("/artists").status_code == 200
    read.assert_not_called()


def test_targeted_nfo_events_change_the_inventory_without_walking_or_reading_audio(
    tmp_path, monkeypatch
):
    path = write_nfo(tmp_path, "metadata", ALICE, "First")
    _album(tmp_path, "Unchanged audio")
    runner = ScanRunner(tmp_path)
    runner.refresh_now()
    seq = runner.status()["seq"]
    no_walk = Mock(side_effect=AssertionError("a settled NFO path needs no library walk"))
    monkeypatch.setattr(scanner, "iter_album_dirs", no_walk)
    monkeypatch.setattr(formats, "read_scan_fields", no_walk)
    original = path.stat()
    write_nfo(tmp_path, "metadata", ALICE, "Other")
    os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
    assert runner._refresh_changed({path})
    assert runner.nfo_inventory().for_artist(ALICE).profile.name == "Other"
    assert runner.status()["seq"] == seq + 1
    assert runner._refresh_changed({path})
    assert runner.status()["seq"] == seq + 1
    path.write_text("<artist>")
    assert runner._refresh_changed({path})
    assert runner.nfo_inventory().errors
    assert runner.nfo_inventory().for_artist(ALICE).profile is None
    path.unlink()
    assert runner._refresh_changed({path})
    assert runner.nfo_inventory().paths == ()
    assert runner.nfo_inventory().errors == ()
    no_walk.assert_not_called()


def test_full_rescan_discovers_and_removes_nfos_in_non_album_directories(tmp_path):
    runner = ScanRunner(tmp_path)
    runner.refresh_now()
    path = write_nfo(tmp_path, "new/tree", ALICE)
    runner.refresh_now()
    assert runner.nfo_inventory().paths == (path,)
    path.unlink()
    runner.refresh_now()
    assert runner.nfo_inventory().paths == ()
