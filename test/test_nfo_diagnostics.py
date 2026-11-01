"""Persistent filesystem faults remain visible without repeating Activity rows."""

import asyncio
import errno
import logging
from pathlib import Path

import pytest

from harmonist import activity, activity_store, artist_nfo, scanner
from harmonist.web.scan_runner import ScanRunner
from test.test_artist_web import ALICE, artist_client
from test.test_nfo_inventory import write_nfo

__all__ = ["artist_client"]


def errors(caplog):
    return [
        r for r in caplog.records if r.name == "harmonist.artist_nfo" and r.levelno >= logging.ERROR
    ]


@pytest.fixture(params=["walk", "read", "image"])
def failing_inventory(tmp_path, monkeypatch, request):
    path = write_nfo(tmp_path, "metadata", ALICE)
    image = path.parent / "portrait.png"
    image.write_bytes(b"portrait")
    path.write_text(
        path.read_text().replace(
            "</artist>", "<biography>Local bio</biography><thumb>portrait.png</thumb></artist>"
        )
    )
    blocked = tmp_path / "Unavailable"
    blocked.mkdir()
    target = {"walk": blocked, "read": path, "image": image}[request.param]
    failure = [PermissionError(errno.EACCES, "Permission denied", str(target))]
    if request.param == "walk":
        original = scanner.os.walk

        def walk(root, *args, **kwargs):
            for directory, directories, files in original(root, *args, **kwargs):
                if Path(directory) == tmp_path and failure[0] is not None:
                    directories.remove(blocked.name)
                    kwargs["onerror"](failure[0])
                yield directory, directories, files

        monkeypatch.setattr(scanner.os, "walk", walk)
    else:
        name = "read" if request.param == "read" else "local_image"
        original = getattr(artist_nfo, name)

        def read(*args, **kwargs):
            if failure[0] is not None:
                raise failure[0]
            return original(*args, **kwargs)

        monkeypatch.setattr(artist_nfo, name, read)
    return ScanRunner(tmp_path), failure, target


def test_unchanged_fault_cause_changes_and_recurrence_are_mirrored_once(failing_inventory, caplog):
    runner, failure, target = failing_inventory
    activity_store.init_memory()
    activity.install_log_handler()
    caplog.set_level(logging.ERROR, logger="harmonist.artist_nfo")
    first = failure[0]
    runner.refresh_now()
    seq = runner.status()["seq"]
    runner.refresh_now()
    assert len(errors(caplog)) == 1
    assert not errors(caplog)[0].exc_info
    assert str(target) in errors(caplog)[0].getMessage()
    inventory = runner.nfo_inventory()
    assert inventory.walk_errors or inventory.errors or inventory.image_errors
    assert runner.status()["seq"] == seq
    assert len(activity.recent()) == 1
    failure[0] = OSError(errno.EIO, "Input/output error", str(target))
    runner.refresh_now()
    assert len(errors(caplog)) == 2
    failure[0] = first
    runner.refresh_now()
    assert len(errors(caplog)) == 3
    failure[0] = None
    runner.refresh_now()
    assert not runner.nfo_inventory().walk_errors
    assert not runner.nfo_inventory().errors
    assert not runner.nfo_inventory().image_errors
    failure[0] = first
    runner.refresh_now()
    assert len(errors(caplog)) == 4
    assert len(activity.recent()) == 4


def test_two_runners_do_not_share_suppression(failing_inventory, caplog):
    first, _, _ = failing_inventory
    second = ScanRunner(first._music_dir)
    first.refresh_now()
    second.refresh_now()
    first.refresh_now()
    second.refresh_now()
    assert len(errors(caplog)) == 2


def test_requests_share_their_apps_diagnostics_and_keep_errors_visible(artist_client, caplog):
    client, cfg, _ = artist_client
    path = cfg.paths.music_dir / "ArtistInfo" / "Different directory name" / "artist.nfo"
    original = path.read_bytes()
    path.write_text("<artist>")
    listing = client.get("/artists")
    assert listing.context["artists_view"]["nfo_errors"]
    assert client.get(f"/artist/{ALICE}").context["nfo"].errors
    assert client.get(f"/artist/{ALICE}/image").status_code == 404
    assert len(errors(caplog)) == 1
    path.write_bytes(original)
    assert client.get(f"/artist/{ALICE}").context["nfo"].profile is not None
    path.write_text("<artist>")
    client.get("/artists")
    assert len(errors(caplog)) == 2


def test_targeted_events_and_full_scans_share_failure_and_recovery(tmp_path, caplog):
    path = write_nfo(tmp_path, "metadata", ALICE)
    runner = ScanRunner(tmp_path)
    runner.refresh_now()
    path.write_text("<artist>")
    for _ in range(2):
        assert runner._refresh_changed({path})
    runner.refresh_now()
    assert len(errors(caplog)) == 1
    write_nfo(tmp_path, "metadata", ALICE)
    runner._refresh_changed({path})
    path.write_text("<artist>")
    runner._refresh_changed({path})
    assert len(errors(caplog)) == 2


def test_recovery_of_one_directory_is_observed_while_another_stays_unreadable(
    tmp_path, monkeypatch, caplog
):
    for name in ("one", "two"):
        (tmp_path / name).mkdir()
    denied = {"one", "two"}
    original = scanner.os.walk

    def walk(root, *args, **kwargs):
        for directory, directories, files in original(root, *args, **kwargs):
            if Path(directory) == tmp_path:
                for name in sorted(denied):
                    directories.remove(name)
                    kwargs["onerror"](
                        PermissionError(errno.EACCES, "Permission denied", str(tmp_path / name))
                    )
            yield directory, directories, files

    monkeypatch.setattr(scanner.os, "walk", walk)
    runner = ScanRunner(tmp_path)
    runner.refresh_now()
    denied.remove("one")
    runner.refresh_now()
    denied.add("one")
    runner.refresh_now()
    messages = [r.getMessage() for r in errors(caplog)]
    assert len(messages) == 3
    assert sum(str(tmp_path / "one") in message for message in messages) == 2
    assert sum(str(tmp_path / "two") in message for message in messages) == 1


def test_stale_picture_refresh_cannot_clear_a_newer_failure(failing_inventory, monkeypatch, caplog):
    runner, failure, target = failing_inventory
    if target.name == "Unavailable":
        pytest.skip("targeted refresh is not used during an incomplete walk")
    runner.refresh_now()
    failure[0] = None
    original = artist_nfo.Discovery.finish
    interleaved = False

    def finish(discovery, albums=()):
        nonlocal interleaved
        inventory = original(discovery, albums)
        if not interleaved:
            interleaved = True
            failure[0] = OSError(errno.EIO, "Input/output error", str(target))
            runner.refresh_now()
        return inventory

    monkeypatch.setattr(artist_nfo.Discovery, "finish", finish)
    runner._refresh_nfos(set())
    runner._refresh_nfos(set())
    assert len(errors(caplog)) == 2
    assert "Input/output error" in errors(caplog)[-1].getMessage()


def test_background_passes_share_diagnostics(failing_inventory, caplog):
    runner, _, _ = failing_inventory

    async def scan():
        runner.attach_loop()
        await asyncio.wait_for(runner._task, timeout=5)
        runner._kick()
        await asyncio.wait_for(runner._task, timeout=5)

    asyncio.run(scan())
    runner._executor.shutdown()
    assert len(errors(caplog)) == 1
    inventory = runner.nfo_inventory()
    assert inventory.walk_errors or inventory.errors or inventory.image_errors


def test_an_unvisited_file_does_not_count_as_recovered(tmp_path, monkeypatch, caplog):
    path = write_nfo(tmp_path, "metadata", ALICE)
    path.write_text("<artist>")
    (tmp_path / "other").mkdir()
    denied: set[str] = set()
    original = scanner.os.walk

    def walk(root, *args, **kwargs):
        for directory, directories, files in original(root, *args, **kwargs):
            if Path(directory) == tmp_path:
                for name in sorted(denied):
                    directories.remove(name)
                    kwargs["onerror"](
                        PermissionError(errno.EACCES, "Permission denied", str(tmp_path / name))
                    )
            yield directory, directories, files

    monkeypatch.setattr(scanner.os, "walk", walk)
    runner = ScanRunner(tmp_path)
    runner.refresh_now()
    denied.add("metadata")
    runner.refresh_now()
    denied.remove("metadata")
    denied.add("other")
    runner.refresh_now()
    read_errors = [r for r in errors(caplog) if "invalid artist XML" in r.getMessage()]
    assert len(read_errors) == 1
    assert runner.nfo_inventory().errors


def test_unexpected_parser_failure_keeps_the_background_traceback(tmp_path, monkeypatch, caplog):
    write_nfo(tmp_path, "metadata", ALICE)

    def broken_parser(_path):
        raise RuntimeError("unexpected parser failure")

    monkeypatch.setattr(artist_nfo, "read", broken_parser)
    runner = ScanRunner(tmp_path)

    async def scan():
        runner.attach_loop()
        assert runner._task is not None
        await asyncio.wait_for(runner._task, timeout=5)

    asyncio.run(scan())
    runner._executor.shutdown()
    records = [
        r
        for r in caplog.records
        if r.name == "harmonist.web.scan_runner" and r.levelno >= logging.ERROR
    ]
    assert len(records) == 1
    assert records[0].exc_info and isinstance(records[0].exc_info[1], RuntimeError)
    assert runner.status()["last_error"] == "unexpected parser failure"
