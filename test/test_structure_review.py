"""Upstream tracklist changes need a reviewed mapping, even when counts fit."""

from copy import deepcopy

import pytest

from harmonist import album_files, formats, tagger, track_assignment
from harmonist.formats import owned
from test import test_web
from test.helpers import write_track_totals
from test.test_tagger import _release_2_tracks


def test_added_track_proposal_keeps_identity_before_duplicate_number(album_with_tracks):
    root = album_with_tracks(6)
    files = album_files.audio_files(root)
    write_track_totals(root, track_total=6)
    release = test_web._release_for_match("rel-five", n_tracks=5)
    tagger.tag_and_artwork(
        root,
        release,
        artwork_included=False,
        assignment={**{files[i]: i for i in range(4)}, files[5]: 4},
    )
    before = [f.read_bytes() for f in files]
    added = deepcopy(release["medium-list"][0]["track-list"][0])
    added["id"] = "rt-julia"
    added["recording"]["id"] = "rec-julia"
    added["title"] = "Julia's Lament"
    tracks = release["medium-list"][0]["track-list"]
    tracks.insert(4, added)
    for i, track in enumerate(tracks, 1):
        track["position"] = str(i)
    panel = track_assignment.panel(files, release, confirmed=True)
    assert panel.mapping() == {f: i for i, f in enumerate(files)}
    assert panel.proposed == frozenset({(4, 4)})
    assert panel.disk[5].number.endswith(".5")
    assert panel.mb[5].number.endswith(".6")
    with pytest.raises(tagger.TrackAssignmentRequired):
        tagger.tag_and_artwork(root, release, artwork_included=False)
    assert [f.read_bytes() for f in files] == before
    tagger.tag_and_artwork(root, release, artwork_included=False, assignment=panel.mapping())
    assert formats.read_tags(files[4]).release_track_id == "rt-julia"
    assert formats.read_tags(files[5]).track_num == 6
    assert not tagger.plan_album(root, release, artwork=False).changes


def test_proposal_labels_follow_draft_moves(album_with_tracks):
    root = album_with_tracks(2)
    release = _release_2_tracks()
    tagger.tag_album(root, release)
    panel = track_assignment.panel(album_files.audio_files(root), release, confirmed=True)
    assert not panel.proposed
    panel.move("disk:0:down")
    assert panel.proposed == frozenset({(0, 1), (1, 0)})
    panel.move("disk:1:up")
    assert not panel.proposed


@pytest.mark.parametrize(
    "change", ["add", "remove", "reorder", "replace", "duplicate", "empty", "disc"]
)
def test_generic_tagging_cannot_apply_structure_without_review(album_with_tracks, change):
    root = album_with_tracks(2)
    release = _release_2_tracks()
    tagger.tag_album(root, release)
    files = album_files.audio_files(root)
    before = [f.read_bytes() for f in files]
    tracks = release["medium-list"][0]["track-list"]
    if change == "add":
        track = deepcopy(tracks[-1])
        track["id"] = "rt-added"
        track["position"] = "3"
        tracks.append(track)
    elif change == "remove":
        tracks.pop()
    elif change == "replace":
        tracks[0]["id"] = "rt-replacement"
    elif change == "duplicate":
        tracks[0]["id"] = tracks[1]["id"]
    elif change == "empty":
        tracks.clear()
    elif change == "reorder":
        tracks.reverse()
        for i, track in enumerate(tracks, 1):
            track["position"] = str(i)
    else:
        second = tracks.pop()
        second["position"] = "1"
        release["medium-list"].append({"position": "2", "track-list": [second]})
    # Neither the incomplete override nor a same-count release bypasses review.
    with pytest.raises(tagger.TrackAssignmentRequired):
        tagger.tag_and_artwork(root, release, artwork_included=False, incomplete=True)
    assert [f.read_bytes() for f in files] == before


def test_existing_incomplete_and_ripper_albums_remain_taggable(album_with_tracks):
    root = album_with_tracks(1)
    release = _release_2_tracks()
    tagger.tag_album(root, release, incomplete=True)
    release["medium-list"][0]["title"] = "Bonus disc subtitle"
    tagger.tag_and_artwork(root, release, incomplete=True, artwork_included=False)
    assert (
        formats.read_tags(album_files.audio_files(root)[0]).owned["disc_subtitle"]
        == "Bonus disc subtitle"
    )


def test_structure_outweighs_identity():
    assert owned.ranked(owned.Significance.STRUCTURE) > owned.ranked(owned.Significance.IDENTITY)


def test_proposals_leave_ambiguity_and_conflicting_ids_unpaired():
    from harmonist import track_structure
    from harmonist.formats import TrackTags

    targets = tagger.tagsets_for(_release_2_tracks(), frozenset())
    eligible = {0, 1}
    assert track_structure.pairs([TrackTags()], targets[:1], {0}, propose=True).slots == (0,)
    ambiguous = [TrackTags(disc_num=1, track_num=1), TrackTags(disc_num=1, track_num=1)]
    assert track_structure.pairs(ambiguous, targets, eligible, propose=True).slots == (None, None)
    for key, value in [("mb_release_track_id", "removed"), ("mb_track_id", "removed")]:
        conflicting = [TrackTags(disc_num=1, track_num=1, owned={key: value})]
        assert track_structure.pairs(conflicting, targets[:1], {0}, propose=True).slots == (None,)
    duplicate_ids = [TrackTags(owned={"mb_release_track_id": "rt-001"})] * 2
    assert track_structure.pairs(duplicate_ids, targets, eligible, propose=True).slots == (
        None,
        None,
    )


def test_recording_id_pairs_remain_available_without_release_track_ids(album_with_tracks):
    from test.helpers import write_ripper_tags

    root = album_with_tracks(2)
    release = _release_2_tracks()
    write_ripper_tags(root, album_id=release["id"], recording_ids=["rec-001", "rec-002"])
    write_track_totals(root, track_total=2)
    files = album_files.audio_files(root)
    panel = track_assignment.panel(files, release, confirmed=True)
    assert panel.mapping() == {files[0]: 0, files[1]: 1}
    assert not panel.proposed
    tagger.tag_and_artwork(root, release, artwork_included=False)


def test_unsupported_bonus_medium_does_not_require_audio_reassignment(album_with_tracks):
    root = album_with_tracks(2)
    release = _release_2_tracks()
    tagger.tag_album(root, release)
    release["medium-list"].append(
        {
            "position": "2",
            "format": "DVD-Video",
            "track-list": [
                {
                    "id": "video",
                    "position": "1",
                    "recording": {"id": "rec-video", "title": "Video", "video": "true"},
                }
            ],
        }
    )
    files = album_files.audio_files(root)
    assert not tagger.assignment_review(release, [formats.read_tags(f) for f in files]).required
    tagger.tag_and_artwork(root, release, artwork_included=False)


def test_restart_and_repeated_checks_rederive_structural_review(
    album_with_tracks, tmp_path, monkeypatch
):
    from harmonist import activity_store, gardener, mb_cache, scanner, sidecar
    from harmonist.models import Sidecar

    activity_store.init(tmp_path / "state.db")
    root = album_with_tracks(2)
    release = _release_2_tracks()
    tagger.tag_album(root, release)
    sidecar.write(root, Sidecar(mb_release_id=release["id"]))
    release["medium-list"][0]["track-list"].reverse()
    release["title"] = "A different title too"
    from harmonist import mb_lookup

    activity_store.store_release(
        release["id"], "+".join(sorted(mb_lookup.RELEASE_INCLUDES)), release
    )

    def no_network(*args, **kwargs):
        raise AssertionError("review spent an MB request")

    monkeypatch.setattr(mb_cache, "fetch_release", no_network)
    before = [f.read_bytes() for f in album_files.audio_files(root)]
    for _ in range(2):
        albums = scanner.scan(tmp_path)
        album = next(a for a in albums if a.path == root)
        gardener.warm_from_cache([album], duty=0)
        assert album.update_available
        assert album.update_significance == owned.Significance.STRUCTURE
    assert [f.read_bytes() for f in album_files.audio_files(root)] == before


def test_repeated_recording_on_a_rip_uses_exact_numbered_occurrences(album_with_tracks):
    from test.helpers import write_ripper_tags

    root = album_with_tracks(2)
    release = _release_2_tracks()
    release["medium-list"][0]["track-list"][1]["recording"]["id"] = "rec-001"
    write_ripper_tags(root, album_id=release["id"], recording_ids=["rec-001", "rec-001"])
    write_track_totals(root, track_total=2)
    files = album_files.audio_files(root)
    assert track_assignment.panel(files, release, confirmed=True).mapping() == {
        files[0]: 0,
        files[1]: 1,
    }
    tagger.tag_and_artwork(root, release, artwork_included=False)


def test_recording_ids_do_not_override_duplicate_release_track_ids():
    from harmonist import track_structure
    from harmonist.formats import TrackTags

    release = _release_2_tracks()
    release["medium-list"][0]["track-list"][1]["id"] = "rt-001"
    tags = [
        TrackTags(
            disc_num=1,
            track_num=i,
            owned={"mb_album_id": release["id"], "mb_track_id": f"rec-00{i}"},
        )
        for i in (1, 2)
    ]
    assert tagger.assignment_review(release, tags).required
    targets = tagger.tagsets_for(release, frozenset())
    assert track_structure.pairs(tags, targets, {0, 1}, propose=True).slots == (None, None)
