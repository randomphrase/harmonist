"""Explicit pairing must survive the preview-to-write boundary."""

import pytest

from harmonist import album_files, formats, tagger
from test.test_tagger import _release_2_tracks


def test_explicit_pairing_overrules_existing_ids_and_is_idempotent(album_with_tracks, tmp_path):
    from harmonist import activity_store
    from test.test_tagger import _detail

    activity_store.init(tmp_path / "history.db")
    album_with_tracks = album_with_tracks(2)
    release = _release_2_tracks()
    files = album_files.audio_files(album_with_tracks)
    tagger.tag_album(album_with_tracks, release)
    assignment = {files[0]: 1, files[1]: 0}
    plan = tagger.plan_album(album_with_tracks, release, artwork=False, assignment=assignment)
    assert plan.changes[files[0]]["title"][1] == "Track 2"
    tagger.tag_and_artwork(
        album_with_tracks, release, artwork_included=False, assignment=assignment
    )
    assert formats.read_tags(files[0]).title == "Track 2"
    assert formats.read_tags(files[0]).track_num == 2
    assert formats.read_tags(files[1]).title == "Track 1"
    before_repeat = len(_detail())
    tagged_bytes = [f.read_bytes() for f in files]
    tagger.tag_and_artwork(
        album_with_tracks, release, artwork_included=False, assignment=assignment
    )
    assert len(_detail()) == before_repeat
    assert [f.read_bytes() for f in files] == tagged_bytes
    assert not tagger.plan_album(
        album_with_tracks, release, artwork=False, assignment=assignment
    ).changes
    # The IDs written by the correction also make the automatic pairing stable.
    assert not tagger.plan_album(album_with_tracks, release, artwork=False).changes


@pytest.mark.parametrize("slots", [[0, 0], [0, 9], [0]])
def test_invalid_explicit_pairing_writes_nothing(album_with_tracks, slots):
    album_with_tracks = album_with_tracks(2)
    files = album_files.audio_files(album_with_tracks)
    before = [f.read_bytes() for f in files]
    with pytest.raises(ValueError, match="assignment"):
        tagger.tag_and_artwork(
            album_with_tracks,
            _release_2_tracks(),
            artwork_included=False,
            assignment=dict(zip(files, slots)),
        )
    assert [f.read_bytes() for f in files] == before


def test_split_discs_with_duplicate_filenames_keep_their_own_identity(album_with_tracks):
    from harmonist import track_assignment

    root = album_with_tracks(2)
    files = album_files.audio_files(root)
    for i, path in enumerate(files):
        disc = root / f"CD{i + 1}"
        disc.mkdir()
        files[i] = path.rename(disc / "track.m4a")
    release = _release_2_tracks()
    first, second = release["medium-list"][0]["track-list"]
    release["medium-list"] = [
        {"position": "1", "track-list": [first]},
        {"position": "2", "track-list": [second]},
    ]
    second["recording"]["length"] = "5500"
    panel = track_assignment.panel(files, release)
    assert [e.path for e in panel.disk] == ["CD1/track.m4a", "CD2/track.m4a"]
    assert [e.number for e in panel.mb] == ["1.1", "2.1"]
    assert panel.mb[1].length == 5500
    panel.move("disk:0:down")
    tagger.tag_and_artwork(
        root, release, files=files, artwork_included=False, assignment=panel.mapping()
    )
    assert formats.read_tags(files[0]).disc_num == 2
    assert formats.read_tags(files[1]).disc_num == 1
