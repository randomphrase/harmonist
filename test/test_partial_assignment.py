"""Release confirmation may leave local files without published MB tracks."""

import pytest

from harmonist import activity_store, album_files, formats, tag_history, tagger
from test import test_web
from test.helpers import write_ripper_tags, write_track_totals
from test.test_tagger import _detail, _release_2_tracks

cfg = test_web.cfg
client = test_web.client


@pytest.mark.parametrize("release_only", [False, True])
def test_partial_assignment_preserves_unassigned_metadata_and_repeats_as_noop(
    album_with_tracks, tmp_path, release_only
):
    activity_store.init(tmp_path / "history.db")
    root = album_with_tracks(6)
    files = album_files.audio_files(root)
    release = test_web._release_for_match("rel-five-tracks", n_tracks=5)
    before = [formats.read_owned(p) for p in files]
    assignment = {} if release_only else {**{files[i]: i for i in range(4)}, files[5]: 4}
    preview = tagger.plan_album(root, release, artwork=False, assignment=assignment)
    assert preview.changes[files[4]] == {"mb_album_id": [None, release["id"]]}
    tagger.tag_and_artwork(root, release, artwork_included=False, assignment=assignment)
    for i, path in enumerate(files):
        after = formats.read_owned(path)
        if path not in assignment:
            assert after == before[i] | {"mb_album_id": release["id"]}
        else:
            assert after["title"] == f"Track {assignment[path] + 1}"
            assert after["track_num"] == assignment[path] + 1
            assert after["track_total"] == 5
    records = len(_detail())
    written = [p.read_bytes() for p in files]
    tagger.tag_and_artwork(root, release, artwork_included=False, assignment=assignment)
    assert [p.read_bytes() for p in files] == written
    assert len(_detail()) == records
    # Known track IDs let Undo follow the paired file even after it is renamed.
    if not release_only:
        files[5] = files[5].rename(root / "Sparrows Flight.m4a")
    outcome = tagger.revert_tags(root, tag_history.revert_plan(_detail()))
    assert outcome.release_id_reverted
    assert [formats.read_owned(p) for p in files] == before


def test_unassigned_conflicting_track_ids_block_before_writing(album_with_tracks):
    root = album_with_tracks(3)
    files = album_files.audio_files(root)
    tags = formats.read_owned(files[1])
    tags["mb_release_track_id"] = "old-release-track"
    formats.write_owned(files[1], tags)
    before = [p.read_bytes() for p in files]
    with pytest.raises(ValueError, match="unassigned.*MusicBrainz"):
        tagger.tag_and_artwork(
            root,
            _release_2_tracks(),
            artwork_included=False,
            assignment={files[0]: 0, files[2]: 1},
        )
    assert [p.read_bytes() for p in files] == before


def test_partial_confirmation_and_review_after_mb_adds_track(client, cfg, monkeypatch):
    from harmonist import scanner

    root, release, _, _, calls = test_web._confirmation_setup(cfg, monkeypatch, old_mbid=None)
    later = release["medium-list"][0]["track-list"].pop()
    aid = test_web._id_for(cfg, root)
    files = album_files.audio_files(root)
    untouched = formats.read_owned(files[1])
    editor = client.get(f"/assignments/{aid}")
    result = client.post(f"/confirm/{aid}/accept", data=test_web._confirmation_fields(editor.text))
    assert "confirmation-applied" in result.headers.get("HX-Trigger", "")
    assert [call for call in calls if call[0] == "mb"] == [("mb", release["id"])]
    assert formats.read_owned(files[1]) == untouched | {"mb_album_id": release["id"]}
    album = next(a for a in scanner.scan(cfg.paths.music_dir) if a.path == root)
    assert album.unassigned_track_count == 1
    page = client.get(f"/album/{album.id}")
    assert "Tracks unassigned" in page.text
    from bs4 import BeautifulSoup

    comparison = client.get(f"/library/{album.id}/compare")
    tracks = BeautifulSoup(comparison.text, "html.parser").select_one("#album-track-view")
    assert tracks.select_one(
        f'button[hx-get="/assignments/{album.id}?album_tracks=true&on_album_page=true"]'
    )

    release["medium-list"][0]["track-list"].append(later)
    editor = client.get(f"/assignments/{album.id}?reread=true&album_tracks=true&on_album_page=true")
    fields = test_web._confirmation_fields(editor.text)
    assert fields["disk_order"] == "0,1", "the sole remaining file is proposed for review"
    assert fields["mb_order"] == "0,1"
    assert [call for call in calls if call[0] == "mb"] == [("mb", release["id"])] * 2
    from harmonist import compare
    from harmonist.web.main import _album_comparison

    _, comparison = _album_comparison(root, release)
    assert [track.state for track in comparison.tracks].count(compare.TrackState.MISSING) == 1
    assert [track.state for track in comparison.tracks].count(compare.TrackState.EXTRA) == 1
    assert formats.read_owned(files[1]) == untouched | {"mb_album_id": release["id"]}
    assert "Proposed assignment" in editor.text
    result = client.post(f"/confirm/{album.id}/accept", data=fields)
    assert "confirmation-applied" in result.headers.get("HX-Trigger", "")
    assert formats.read_owned(files[1])["mb_release_track_id"] == later["id"]
    album = next(a for a in scanner.scan(cfg.paths.music_dir) if a.path == root)
    assert album.unassigned_track_count == 0


def test_partial_album_can_have_missing_and_unassigned_tracks(album_with_tracks, tmp_path):
    from harmonist import scanner, sidecar
    from harmonist.models import AlbumState, Sidecar

    root = album_with_tracks(2)
    files = album_files.audio_files(root)
    release = _release_2_tracks()
    tagger.tag_and_artwork(root, release, artwork_included=False, assignment={files[0]: 0})
    sidecar.write(root, Sidecar(mb_release_id=release["id"]))
    album = next(a for a in scanner.scan(tmp_path) if a.path == root)
    assert album.unassigned_track_count == 1
    assert album.state == AlbumState.INCOMPLETE


@pytest.mark.parametrize("changed_id", [False, True])
def test_upstream_update_cannot_assign_gap_automatically(album_with_tracks, changed_id):
    root = album_with_tracks(2)
    files = album_files.audio_files(root)
    release = _release_2_tracks()
    assignment = {files[0]: 0, files[1]: 1} if changed_id else {files[0]: 0}
    tagger.tag_and_artwork(root, release, artwork_included=False, assignment=assignment)
    if changed_id:
        release["medium-list"][0]["track-list"][0]["id"] = "changed-upstream"
    before = [p.read_bytes() for p in files]
    with pytest.raises(tagger.TrackAssignmentRequired):
        tagger.tag_album(root, release)
    assert [p.read_bytes() for p in files] == before


def test_ripper_tags_without_release_track_ids_pair_instead_of_reading_unassigned(
    album_with_tracks, tmp_path
):
    """A CD ripped with the MBID chosen in the ripper (#538).

    XLD writes the album MBID and the recording MBID and stops there. Every file
    names the track it is, so none of them is unassigned and a re-tag needs no
    review — the recording id is the recorded decision.
    """
    from harmonist import scanner, sidecar
    from harmonist.models import Sidecar

    root = album_with_tracks(3)
    files = album_files.audio_files(root)
    release = test_web._release_for_match("rel-ripped", n_tracks=3)
    write_track_totals(root, track_total=3)
    write_ripper_tags(root, album_id=release["id"])
    sidecar.write(root, Sidecar(mb_release_id=release["id"]))

    album = next(a for a in scanner.scan(tmp_path) if a.path == root)
    assert album.unassigned_track_count == 0

    plan = tagger.plan_album(root, release, artwork=False)
    assert [plan.changes[f]["mb_release_track_id"] for f in files] == [
        [None, "rt-1"],
        [None, "rt-2"],
        [None, "rt-3"],
    ]


def test_editor_offers_a_pairing_for_ripper_tagged_files(album_with_tracks):
    """The escape hatch must not be the only way through: a confirmed album whose
    files carry recording ids opens already paired, not as two disjoint columns."""
    from harmonist import track_assignment

    root = album_with_tracks(3)
    files = album_files.audio_files(root)
    release = test_web._release_for_match("rel-ripped", n_tracks=3)
    write_track_totals(root, track_total=3)
    write_ripper_tags(root, album_id=release["id"])

    panel = track_assignment.panel(files, release, confirmed=True)
    assert panel.paired
    assert panel.mapping() == {f: i for i, f in enumerate(files)}
