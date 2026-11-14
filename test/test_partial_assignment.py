"""Release confirmation may leave local files without published MB tracks."""

import shutil

import pytest

from harmonist import activity_store, album_files, formats, tag_history, tagger
from harmonist.formats import owned
from harmonist.transforms import TaggingChoices
from test import test_formats, test_web
from test.helpers import write_ripper_tags, write_track_totals
from test.test_tagger import _detail, _release_2_tracks

cfg = test_web.cfg
client = test_web.client

#: What a file left unassigned takes from the release: every album-scope field
#: but the disc total, which is stored packed with the file's own disc number.
#: Spelled out from the scope rather than read from `owned.ALBUM_WIDE`, so a
#: field dropped from that by mistake fails here.
_TAKEN = [f.value for f in owned.ALBUM_FIELDS if f is not owned.Owned.DISC_TOTAL]


def _album_values(release):
    """The release's values for `_TAKEN`, which are the same on every track."""
    tagset = tagger.tagsets_for(release, TaggingChoices())[0]
    return {field: getattr(tagset, field) for field in _TAKEN}


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
    # The file belongs to the release, so it takes what is true of the album
    # (#1004); which track it is stays unsaid, so nothing of a track's arrives.
    assert preview.changes[files[4]]["mb_album_id"] == [None, release["id"]]
    assert preview.changes[files[4]]["album"] == [before[4]["album"], "Title"]
    assert set(preview.changes[files[4]]) <= set(_TAKEN)
    tagger.tag_and_artwork(root, release, artwork_included=False, assignment=assignment)
    for i, path in enumerate(files):
        after = formats.read_owned(path)
        if path not in assignment:
            assert not owned.diff(after, before[i] | _album_values(release))
            assert after["title"] == before[i]["title"]
            assert after["track_num"] == before[i]["track_num"]
            assert not after.get("mb_release_track_id")
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


def _confirmed_with_a_leftover(album_with_tracks):
    """Six files confirmed against a five-track release: every track paired and
    the fifth file left unassigned, as a track added on Bandcamp is while its
    MusicBrainz edit waits (#517)."""
    root = album_with_tracks(6)
    files = album_files.audio_files(root)
    release = test_web._release_for_match("rel-five-tracks", n_tracks=5)
    assignment = {**{files[i]: i for i in range(4)}, files[5]: 4}
    tagger.tag_and_artwork(root, release, artwork_included=False, assignment=assignment)
    return root, files, release


def test_a_file_left_unassigned_asks_for_no_review_while_every_track_is_paired(album_with_tracks):
    """#1004: the confirmation was the review. With no track left to give the
    file, asking again offers the user nothing to decide."""
    _, files, release = _confirmed_with_a_leftover(album_with_tracks)
    review = tagger.assignment_review(release, [formats.read_tags(f) for f in files])
    assert not review.required
    assert review.unassigned == 1


def test_an_unreviewed_tagging_keeps_a_file_left_unassigned(album_with_tracks):
    """#1004: the file's tags record that it was left unassigned, so a tagging
    nobody reviewed leaves it out of the count and out of the pairing, and
    still brings it what changes about the album."""
    root, files, release = _confirmed_with_a_leftover(album_with_tracks)
    leftover = formats.read_owned(files[4])
    written = [p.read_bytes() for p in files]
    assert tagger.plan_album(root, release, artwork=False).changes == {}
    tagger.tag_album(root, release)
    assert [p.read_bytes() for p in files] == written

    release["title"] = "Retitled"
    plan = tagger.plan_album(root, release, artwork=False)
    assert set(plan.changes) == set(files)
    assert plan.changes[files[4]] == {"album": ["Title", "Retitled"]}
    tagger.tag_album(root, release)
    assert formats.read_owned(files[4]) == leftover | {"album": "Retitled"}
    assert formats.read_owned(files[5])["mb_release_track_id"] == "rt-5"
    assert tagger.plan_album(root, release, artwork=False).changes == {}


@pytest.mark.parametrize(("ext", "fixture"), test_formats.FIXTURES)
def test_a_file_left_unassigned_settles_in_every_format(tmp_path, ext, fixture):
    """The album's tags reach the file through `write_owned`, and each format
    stores them its own way. What is written has to read back as written, or
    the album has an update waiting that no tagging takes: MP4 can't hold a
    disc total without a disc number, which is why that one is left out."""
    root = tmp_path / "Format Artist" / "Format Album"
    root.mkdir(parents=True)
    files = [root / f"{n:02d} Track {n}{ext}" for n in (1, 2)]
    for path in files:
        shutil.copy(test_formats.FIXTURES_DIR / fixture, path)
    release = test_formats._release_one_track()
    tagger.tag_and_artwork(root, release, artwork_included=False, assignment={files[0]: 0})
    left, paired = formats.read_owned(files[1]), formats.read_owned(files[0])
    assert {f: left[f] for f in _TAKEN} == {f: paired[f] for f in _TAKEN}
    assert left["label"] == ["Test Label"]
    assert tagger.plan_album(root, release, artwork=False).changes == {}


def test_an_album_with_a_file_left_unassigned_has_no_update_waiting(album_with_tracks, tmp_path):
    """#1004: what puts an album in the Inbox is the gardener's flag. Nothing
    is waiting on this one, and the count behind **Tracks unassigned** has to
    outlive the look that says so."""
    from harmonist import gardener, scanner, sidecar
    from harmonist.models import Sidecar

    activity_store.init(tmp_path / "history.db")
    root, _, release = _confirmed_with_a_leftover(album_with_tracks)
    sidecar.write(root, Sidecar(mb_release_id=release["id"]))
    album = next(a for a in scanner.scan(tmp_path) if a.path == root)
    assert album.unassigned_track_count == 1
    gardener.refresh_flag(album, release)
    assert not album.update_available
    assert album.update_significance is None
    assert album.unassigned_track_count == 1


def test_the_album_panel_holds_a_file_left_unassigned_to_what_it_is_given(album_with_tracks):
    """The file is never written a disc total, which is stored with a disc
    number it hasn't got. The panel reporting one missing would be a finding
    no re-tag can clear."""
    from harmonist import compare

    _, files, release = _confirmed_with_a_leftover(album_with_tracks)
    tagset = tagger.tagsets_for(release, TaggingChoices())[0]
    rows = compare.album_fields([(f.name, formats.read_tags(f)) for f in files], tagset)
    uneven = [row.label for row in rows if row.consensus and row.consensus.outliers]
    assert uneven == []


def test_a_stray_file_beside_one_left_unassigned_is_still_refused(album_with_tracks):
    """A file nobody confirmed for this release isn't covered by the decision
    about its neighbour: it carries no release ID, so the count still refuses
    it rather than claim it for the album unreviewed."""
    root, _, release = _confirmed_with_a_leftover(album_with_tracks)
    stray = root / "07 Stray.m4a"
    stray.write_bytes(test_web.SINE_M4A.read_bytes())
    before = stray.read_bytes()
    with pytest.raises(tagger.TagMismatchError) as refused:
        tagger.tag_album(root, release)
    assert (refused.value.files, refused.value.tracks) == (7, 5)
    assert stray.read_bytes() == before


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
    left_alone = untouched | _album_values(release)
    assert not owned.diff(formats.read_owned(files[1]), left_alone)
    album = next(a for a in scanner.scan(cfg.paths.music_dir) if a.path == root)
    assert album.unassigned_track_count == 1
    page = client.get(f"/album/{album.id}")
    assert "Tracks unassigned" in page.text
    from bs4 import BeautifulSoup

    def edit_button():
        """The album page's update, and the control that opens its editor."""
        comparison = client.get(f"/library/{album.id}/compare")
        tracks = BeautifulSoup(comparison.text, "html.parser").select_one("#album-track-view")
        return comparison.text, tracks.select_one(
            f'button[hx-get="/assignments/{album.id}?album_tracks=true&on_album_page=true"]'
        )

    # The release's one track is paired, so the confirmation settled it
    # (#1004): no review is asked for, and the editor opens only on a click.
    update, edit = edit_button()
    assert "Track assignments need review" not in update
    assert edit.get("hx-trigger") is None

    release["medium-list"][0]["track-list"].append(later)
    editor = client.get(f"/assignments/{album.id}?reread=true&album_tracks=true&on_album_page=true")
    # MusicBrainz now has a track for the file, and that is a review.
    update, edit = edit_button()
    assert "Track assignments need review" in update
    assert edit.get("hx-trigger") == "click, load"
    fields = test_web._confirmation_fields(editor.text)
    assert fields["disk_order"] == "0,1", "the sole remaining file is proposed for review"
    assert fields["mb_order"] == "0,1"
    assert [call for call in calls if call[0] == "mb"] == [("mb", release["id"])] * 2
    from harmonist import compare
    from harmonist.web.main import _album_comparison

    _, comparison = _album_comparison(root, release)
    assert [track.state for track in comparison.tracks].count(compare.TrackState.MISSING) == 1
    assert [track.state for track in comparison.tracks].count(compare.TrackState.EXTRA) == 1
    assert not owned.diff(formats.read_owned(files[1]), left_alone)
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
