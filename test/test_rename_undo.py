"""Undo must find renamed files without guessing between tracks."""

from dataclasses import replace

import pytest

from harmonist import activity_store, album_files, formats, tag_history, tagger
from test import test_web
from test.test_tagger import _detail, _release_2_tracks

cfg = test_web.cfg
client = test_web.client


@pytest.mark.parametrize("identity", ["track", "recording", "position"])
def test_undo_finds_renamed_tracks_and_restores_release_identity(
    album_with_tracks, tmp_path, identity
):
    activity_store.init(tmp_path / "history.db")
    root = album_with_tracks(2)
    files = album_files.audio_files(root)
    before = [formats.read_owned(p) for p in files]
    tagger.tag_album(root, _release_2_tracks())
    plan = tag_history.revert_plan(_detail())
    if identity != "track":
        plan = tuple(replace(p, track_ref=None) for p in plan)
    if identity == "position":
        plan = tuple(replace(p, rec_ref=None) for p in plan)
    renamed = [p.rename(root / f"renamed-{i}.m4a") for i, p in enumerate(files)]

    result = tagger.revert_tags(root, plan)

    assert result.files == 2
    assert result.release_id_reverted
    assert result.release_id_now is None
    assert [formats.read_owned(p) for p in renamed] == before


def test_undo_of_undo_survives_another_rename_and_leaves_later_edits(album_with_tracks, tmp_path):
    activity_store.init(tmp_path / "history.db")
    root = album_with_tracks(2)
    release = _release_2_tracks()
    tagger.tag_album(root, release)
    first_count = len(_detail())
    release["title"] = "Revised album"
    tagger.tag_album(root, release)
    plan = tag_history.revert_plan(_detail()[first_count:])
    files = [
        p.rename(root / f"renamed-{i}.m4a") for i, p in enumerate(album_files.audio_files(root))
    ]
    # A later independent edit must survive both undos.
    tags = formats.read_owned(files[0])
    tags["title"] = "My title"
    formats.write_owned(files[0], tags)
    count = len(_detail())
    assert tagger.revert_tags(root, plan).files == 2
    undo_plan = tag_history.revert_plan(_detail()[count:])
    assert all(p.track_ref and p.rec_ref and p.position for p in undo_plan)
    assert tagger.revert_tags(root, plan).files == 0
    assert len(_detail()) == count + 2
    files = [p.rename(root / f"again-{i}.m4a") for i, p in enumerate(files)]
    assert tagger.revert_tags(root, undo_plan).files == 2
    assert [formats.read_owned(p)["album"] for p in files] == ["Revised album"] * 2
    assert formats.read_owned(files[0])["title"] == "My title"


def test_route_undo_after_rename_unlinks_sidecar(client, cfg, monkeypatch):
    from harmonist import sidecar

    root, release, *_ = test_web._confirmation_setup(cfg, monkeypatch, old_mbid=None)
    aid = test_web._id_for(cfg, root)
    preview = client.get(f"/confirm/{aid}/preview")
    response = client.post(f"/confirm/{aid}", data=test_web._confirmation_fields(preview.text))
    assert response.status_code == 200
    events = activity_store.album_history(release["id"])
    anchor = next(e.id for e in events if e.message == "Tagged")
    files = [
        p.rename(root / f"renamed-{i}.m4a") for i, p in enumerate(album_files.audio_files(root))
    ]

    response = client.post(f"/tags/restore/{release['id']}", data={"event_id": anchor})

    assert "now Needs MBID" in response.text
    assert sidecar.read(root).mb_release_id is None
    assert all(formats.read_owned(p)["mb_album_id"] is None for p in files)


def test_ambiguous_renamed_track_ref_refuses_before_any_write(album_with_tracks, tmp_path):
    activity_store.init(tmp_path / "history.db")
    root = album_with_tracks(2)
    tagger.tag_album(root, _release_2_tracks())
    plan = tag_history.revert_plan(_detail())
    files = album_files.audio_files(root)
    duplicate = formats.read_owned(files[1])
    duplicate["mb_release_track_id"] = plan[0].track_ref
    formats.write_owned(files[1], duplicate)
    files[0] = files[0].rename(root / "renamed.m4a")
    before = [p.read_bytes() for p in files]

    with pytest.raises(tagger.RevertUnavailableError, match="ambiguous"):
        tagger.revert_tags(root, plan)
    assert [p.read_bytes() for p in files] == before


def test_two_records_cannot_resolve_to_one_file(album_with_tracks, tmp_path):
    activity_store.init(tmp_path / "history.db")
    root = album_with_tracks(2)
    tagger.tag_album(root, _release_2_tracks())
    plan = tag_history.revert_plan(_detail())
    plan = (plan[0], replace(plan[0], file="gone.m4a"))
    files = album_files.audio_files(root)
    before = [p.read_bytes() for p in files]
    with pytest.raises(tagger.RevertUnavailableError, match="same file"):
        tagger.revert_tags(root, plan)
    assert [p.read_bytes() for p in files] == before


@pytest.mark.parametrize("disc", [1, 2])
def test_recording_and_position_distinguish_renamed_files(album_with_tracks, tmp_path, disc):
    activity_store.init(tmp_path / "history.db")
    root = album_with_tracks(2)
    tagger.tag_album(root, _release_2_tracks())
    files = album_files.audio_files(root)
    folders = []
    for i, path in enumerate(files):
        folder = root / f"CD{i + 1}"
        folder.mkdir()
        folders.append(folder)
        files[i] = path.rename(folder / "same-name.m4a")
        tags = formats.read_owned(files[i])
        tags["disc_num"] = 1 if i == 0 else disc
        tags["disc_total"] = 2
        tags["track_num"] = 1
        formats.write_owned(files[i], tags)
    # Same position on two files requires the recording ID; on separate discs
    # the position alone suffices. Neither file's basename can disambiguate it.
    plan = [
        tag_history.FileRevert(
            file="old-name.m4a",
            fields={"title": ("Original", "Track 2")},
            rec_ref="rec-002" if disc == 1 else None,
            position=f"{disc}-1",
        )
    ]
    assert tagger.revert_tags(folders[0], plan, paths=folders).files == 1
    assert formats.read_owned(files[0])["title"] == "Track 1"
    assert formats.read_owned(files[1])["title"] == "Original"
    if disc == 1:
        before = [p.read_bytes() for p in files]
        with pytest.raises(tagger.RevertUnavailableError, match="ambiguous"):
            tagger.revert_tags(folders[0], [replace(plan[0], rec_ref=None)], paths=folders)
        assert [p.read_bytes() for p in files] == before
