"""Work budgets must preserve image identity, invalidation and conditional HTTP."""

import shutil
from unittest.mock import Mock

import pytest
from mutagen.mp4 import MP4

from harmonist import artist_nfo, artists, scanner
from harmonist.formats.m4a import ATOM_ALBUM_ARTISTS, ATOM_MB_ALBUM_ARTIST_ID
from harmonist.web.scan_runner import ScanRunner
from test.test_artist_image_selection import picture
from test.test_artist_web import ALICE, BOB, SOLO, artist_client, make_album
from test.test_nfo_inventory import write_nfo

__all__ = ["artist_client"]


def thumb(root, directory, identity, reference):
    path = write_nfo(root, directory, identity)
    path.write_text(path.read_text().replace("</artist>", f"<thumb>{reference}</thumb></artist>"))
    return path


def test_image_requests_do_not_rebuild_artist_groups(artist_client, monkeypatch):
    client, cfg, _ = artist_client
    image = picture(cfg.paths.music_dir / "ArtistInfo" / "Different directory name", "artist.png")
    runner = client.app.state.scan_runner
    runner.refresh_now()
    monkeypatch.setattr(runner, "is_engaged", lambda: True)
    build = Mock(wraps=artists.build_index)
    monkeypatch.setattr(artists, "build_index", build)
    response = client.get(f"/artist/{ALICE}/image")
    assert response.content == image.read_bytes()
    assert (
        client.get(
            f"/artist/{ALICE}/image", headers={"If-None-Match": response.headers["etag"]}
        ).status_code
        == 304
    )
    assert client.get(f"/artist/{BOB}/image").status_code == 404
    assert client.get(f"/artist/{SOLO}/image").status_code == 404
    assert build.call_count == 0


def test_image_membership_follows_credit_changes_removal_and_other_instances(
    artist_client, monkeypatch, tmp_path
):
    client, cfg, _ = artist_client
    root = cfg.paths.music_dir
    nfo = write_nfo(root, "metadata/bob", BOB)
    picture(nfo.parent, "artist.png")
    runner = client.app.state.scan_runner
    runner.refresh_now()
    monkeypatch.setattr(runner, "is_engaged", lambda: True)
    assert client.get(f"/artist/{BOB}/image").status_code == 200
    path = root / "Together" / "01.m4a"
    audio = MP4(path)
    audio[ATOM_ALBUM_ARTISTS] = [b"Alice"]
    audio[ATOM_MB_ALBUM_ARTIST_ID] = [ALICE.encode()]
    audio["aART"] = ["Alice"]
    audio.save()
    runner.refresh_album([path.parent])
    assert client.get(f"/artist/{BOB}/image").json()["detail"] == "artist not found in the library"
    make_album(root, "Bob returns", "66666666-6666-4666-8666-666666666666", ["Bob"], [BOB])
    runner.refresh_now()
    assert client.get(f"/artist/{BOB}/image").status_code == 200
    shutil.rmtree(root / "Bob returns")
    runner.refresh_now()
    assert client.get(f"/artist/{BOB}/image").status_code == 404
    other = ScanRunner(tmp_path / "another-library")
    other.refresh_now()
    monkeypatch.setattr(client.app.state, "scan_runner", other)
    monkeypatch.setattr(other, "is_engaged", lambda: True)
    assert client.get(f"/artist/{ALICE}/image").status_code == 404


def test_unrelated_album_cover_does_not_revisit_nfos(artist_client, monkeypatch):
    client, cfg, _ = artist_client
    runner = client.app.state.scan_runner
    runner.refresh_now()
    read = Mock(wraps=artist_nfo.read)
    monkeypatch.setattr(artist_nfo, "read", read)
    cover = picture(cfg.paths.music_dir / "Together", "cover.jpg")
    assert runner._refresh_changed({cover})
    assert read.call_count == 0


@pytest.mark.parametrize(
    "reference", ["portrait.png", "../shared/cover.jpg", "absolute", "symlink"]
)
def test_artist_image_events_resolve_only_affected_images_without_xml_reads(
    tmp_path, monkeypatch, reference
):
    nfo = write_nfo(tmp_path, "metadata/alice", ALICE)
    destination = picture(tmp_path / "shared", "cover.jpg")
    if reference == "absolute":
        reference = str(destination)
    elif reference == "symlink":
        (nfo.parent / "portrait.png").symlink_to(destination)
        reference = "portrait.png"
    elif reference == "portrait.png":
        destination = picture(nfo.parent, reference)
    else:
        destination = picture(nfo.parent.parent / "shared", "cover.jpg")
    thumb(tmp_path, "metadata/alice", ALICE, reference)
    other = write_nfo(tmp_path, "metadata/bob", BOB)
    picture(other.parent, "artist.png")
    runner = ScanRunner(tmp_path)
    runner.refresh_now()
    before = runner.nfo_inventory().image_url(ALICE)
    destination.write_bytes(b"changed picture")
    read = Mock(side_effect=AssertionError("an image event must not re-read NFO XML"))
    resolve = Mock(wraps=artist_nfo.local_image)
    monkeypatch.setattr(artist_nfo, "read", read)
    monkeypatch.setattr(artist_nfo, "local_image", resolve)
    assert runner._refresh_changed({destination})
    assert runner.nfo_inventory().image_for(ALICE) == destination
    assert runner.nfo_inventory().image_url(ALICE) != before
    read.assert_not_called()
    assert resolve.call_count == 1


def test_image_creation_removal_priority_and_nfo_source_changes(tmp_path, monkeypatch):
    nfo = thumb(tmp_path, "metadata", ALICE, "preferred.png")
    fallback = picture(nfo.parent, "artist.png")
    runner = ScanRunner(tmp_path)
    runner.refresh_now()
    assert runner.nfo_inventory().image_for(ALICE) == fallback
    preferred = picture(nfo.parent, "preferred.png")
    assert runner._refresh_changed({preferred})
    assert runner.nfo_inventory().image_for(ALICE) == preferred
    preferred.unlink()
    assert runner._refresh_changed({preferred})
    assert runner.nfo_inventory().image_for(ALICE) == fallback
    higher = picture(nfo.parent, "artist.jpg")
    assert runner._refresh_changed({higher})
    assert runner.nfo_inventory().image_for(ALICE) == higher
    replacement = picture(nfo.parent, "other.png")
    thumb(tmp_path, "metadata", ALICE, "other.png")
    assert runner._refresh_changed({nfo})
    read = Mock(side_effect=AssertionError("picture events must use the current NFO snapshot"))
    monkeypatch.setattr(artist_nfo, "read", read)
    assert runner._refresh_changed({higher})
    assert runner.nfo_inventory().image_for(ALICE) == replacement
    replacement.write_bytes(b"new portrait")
    assert runner._refresh_changed({replacement})
    assert runner.nfo_inventory().image_for(ALICE) == replacement


def test_album_cover_referenced_by_thumb_updates_conditional_image(artist_client, monkeypatch):
    client, cfg, _ = artist_client
    root = cfg.paths.music_dir
    cover = picture(root / "Together", "cover.jpg")
    thumb(root, "ArtistInfo/Different directory name", ALICE, str(cover))
    runner = client.app.state.scan_runner
    runner.refresh_now()
    monkeypatch.setattr(runner, "is_engaged", lambda: True)
    first = client.get(f"/artist/{ALICE}/image")
    assert first.content == cover.read_bytes()
    cover.write_bytes(b"updated referenced album cover")
    read = Mock(side_effect=AssertionError("cover.jpg may update art without re-reading NFOs"))
    monkeypatch.setattr(artist_nfo, "read", read)
    assert runner._refresh_changed({cover})
    second = client.get(f"/artist/{ALICE}/image", headers={"If-None-Match": first.headers["etag"]})
    assert second.status_code == 200 and second.content == cover.read_bytes()
    assert second.headers["etag"] != first.headers["etag"]


def test_image_refresh_preserves_conflicts_and_does_not_walk(tmp_path, monkeypatch):
    one = write_nfo(tmp_path, "one", ALICE)
    two = write_nfo(tmp_path, "two", ALICE)
    image = picture(one.parent, "artist.png")
    runner = ScanRunner(tmp_path)
    runner.refresh_now()
    monkeypatch.setattr(
        scanner,
        "iter_album_dirs",
        Mock(side_effect=AssertionError("image-only refresh must not walk")),
    )
    image.write_bytes(b"new picture")
    assert runner._refresh_changed({image})
    result = runner.nfo_inventory().for_artist(ALICE)
    assert result.profile is None and set(result.candidates) == {one, two}


def test_selective_image_refresh_cannot_overwrite_newer_folder_evidence(tmp_path, monkeypatch):
    nfo = write_nfo(tmp_path, "Artist", ALICE)
    make_album(tmp_path, "Artist/Album", SOLO, ["Alice"], [ALICE])
    image = picture(nfo.parent, "folder.jpg")
    runner = ScanRunner(tmp_path)
    runner.refresh_now()
    assert runner.nfo_inventory().image_for(ALICE) == image
    original = artist_nfo.Inventory.refresh_images
    interleaved = False

    def refresh(inventory, root, changed):
        nonlocal interleaved
        result = original(inventory, root, changed)
        if not interleaved:
            interleaved = True
            make_album(
                tmp_path, "Artist/Other", "66666666-6666-4666-8666-666666666666", ["Bob"], [BOB]
            )
            runner.refresh_now()
        return result

    monkeypatch.setattr(artist_nfo.Inventory, "refresh_images", refresh)
    runner._refresh_changed({image})
    assert runner.nfo_inventory().image_for(ALICE) is None


def test_picture_permission_error_and_recovery_preserve_text_and_diagnostics(tmp_path, monkeypatch):
    nfo = thumb(tmp_path, "metadata", ALICE, "photo.png")
    image = picture(nfo.parent, "photo.png")
    runner = ScanRunner(tmp_path)
    runner.refresh_now()
    original = artist_nfo._present

    def denied(path):
        if path == image:
            raise PermissionError("picture cannot be read")
        return original(path)

    with monkeypatch.context() as patch:
        patch.setattr(artist_nfo, "_present", denied)
        assert runner._refresh_changed({image})
    profile = runner.nfo_inventory().for_artist(ALICE).profile
    assert profile is not None and profile.mbid == ALICE
    assert runner.nfo_inventory().image_errors
    assert runner._refresh_changed({image})
    assert runner.nfo_inventory().image_for(ALICE) == image
    assert not runner.nfo_inventory().image_errors


def test_symlink_retarget_and_library_root_alias_are_invalidated(tmp_path):
    root = tmp_path / "real"
    alias = tmp_path / "alias"
    root.mkdir()
    alias.symlink_to(root, target_is_directory=True)
    nfo = thumb(alias, "metadata", ALICE, "../pictures/link.png")
    first = picture(root / "pictures", "first.png")
    second = picture(root / "pictures", "second.png")
    link = first.parent / "link.png"
    link.symlink_to(first)
    runner = ScanRunner(alias)
    runner.refresh_now()
    assert runner.nfo_inventory().image_for(ALICE) == first
    link.unlink()
    link.symlink_to(second)
    assert runner._refresh_changed({alias / "pictures" / "link.png"})
    assert runner.nfo_inventory().image_for(ALICE) == second
    assert nfo.exists()


def test_image_in_an_unseen_directory_still_requests_a_full_scan(tmp_path):
    runner = ScanRunner(tmp_path)
    runner.refresh_now()
    image = picture(tmp_path / "new-directory", "cover.jpg")
    assert not runner._refresh_changed({image})
