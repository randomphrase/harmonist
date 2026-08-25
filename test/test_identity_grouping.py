"""An album is the files that name its MusicBrainz release, wherever they sit.

#197 replaced #16's directory rule ("a sidecar'd parent owns the audio beneath
it") with identity: the `MusicBrainz Album Id` in the tags says what an album IS,
and the directory is only where its files happen to live.

The two layouts that forced the change, both real and both refused by the
directory rule — because in both the "leftovers" that blocked it were simply
other albums, which is what an artist directory contains:

    Hybrid/Wide Angle          12 files, rel A   <- disc 1
    Hybrid/Live Angle_ Sydney   9 files, rel A   <- disc 2
    Hybrid/I Choose Noise      11 files, rel B   <- a different album
    Hybrid/Morning Sci-Fi      12 files, rel C   <- a different album

and eleven folders of Autechre EPs that are one compilation, alongside unrelated
Autechre albums.
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest
from mutagen.mp4 import MP4

from harmonist import sidecar as sidecar_mod
from harmonist.models import AlbumState, Sidecar
from harmonist.scanner import scan

FIXTURES_DIR = Path(__file__).parent / "fixtures"
SINE_M4A = FIXTURES_DIR / "sine.m4a"

REL_A = "11111111-2222-3333-4444-555555555555"
REL_B = "99999999-8888-7777-6666-555555555555"


def _part(
    where: Path,
    *,
    mbid: str | None = REL_A,
    tracks: int = 2,
    track_ids: list[str] | None = None,
    disc: int | None = None,
    disc_total: int | None = None,
    track_total: int | None = None,
    sidecar: bool = True,
    album: str = "Wide Angle",
) -> Path:
    """One directory of an album: tagged files plus its own standalone sidecar."""
    where.mkdir(parents=True, exist_ok=True)
    for i in range(1, tracks + 1):
        f = where / f"{i:02d} Track {i}.m4a"
        shutil.copy(SINE_M4A, f)
        audio = MP4(f)
        audio["\xa9alb"] = [album]
        audio["\xa9ART"] = ["Hybrid"]
        audio["trkn"] = [(i, track_total or tracks)]
        if disc is not None:
            audio["disk"] = [(disc, disc_total or 1)]
        if mbid is not None:
            audio["----:com.apple.iTunes:MusicBrainz Album Id"] = [mbid.encode()]
        if track_ids is not None and i <= len(track_ids):
            audio["----:com.apple.iTunes:MusicBrainz Release Track Id"] = [
                track_ids[i - 1].encode()
            ]
        audio.save()
    if sidecar and mbid is not None:
        sidecar_mod.write(
            where,
            Sidecar(
                mb_release_id=mbid,
                added_at=datetime(2026, 1, 1, tzinfo=UTC),
                tagged_at=datetime(2026, 1, 1, tzinfo=UTC),
            ),
        )
    return where


def _edit(album_dir: Path, **fields: object) -> None:
    """Adjust a part's already-written sidecar in place."""
    sc = sidecar_mod.read(album_dir)
    assert sc is not None
    for key, value in fields.items():
        setattr(sc, key, value)
    sidecar_mod.write(album_dir, sc)


DISC1 = ["rt-001", "rt-002"]
DISC2 = ["rt-003", "rt-004"]


# ---------------------------------------------------------------------------
# The rule
# ---------------------------------------------------------------------------


def test_two_folders_of_one_release_are_one_album(tmp_path):
    _part(tmp_path / "Hybrid" / "Wide Angle", track_ids=DISC1)
    _part(tmp_path / "Hybrid" / "Live Angle", track_ids=DISC2)

    albums = scan(tmp_path)

    assert len(albums) == 1
    assert albums[0].track_count == 4
    assert {p.name for p in albums[0].paths} == {"Wide Angle", "Live Angle"}


def test_sibling_albums_do_not_block_the_merge(tmp_path):
    """The Hybrid case. The directory rule refused this because the parent held
    "leftovers" — which were two other Hybrid albums."""
    artist = tmp_path / "Hybrid"
    _part(artist / "Wide Angle", track_ids=DISC1)
    _part(artist / "Live Angle", track_ids=DISC2)
    _part(artist / "I Choose Noise", mbid=REL_B, album="I Choose Noise")

    albums = scan(tmp_path)

    assert len(albums) == 2
    by_count = {a.track_count: a for a in albums}
    assert set(by_count) == {4, 2}, "the pair merged; the other album did not join them"


def test_folders_in_unrelated_parts_of_the_tree_still_merge(tmp_path):
    """No containment rule: `Hybrid/Wide Angle` + `Live Albums/Hybrid/Live Angle`
    is a reasonable way to organise a library, and a boundary would rule it out
    to prevent merges that are correct anyway."""
    _part(tmp_path / "Hybrid" / "Wide Angle", track_ids=DISC1)
    _part(tmp_path / "Live Albums" / "Hybrid" / "Live Angle", track_ids=DISC2)

    albums = scan(tmp_path)

    assert len(albums) == 1
    assert albums[0].track_count == 4


def test_eleven_folders_of_one_compilation_become_one_album(tmp_path):
    """The Autechre case, at scale."""
    artist = tmp_path / "Autechre"
    for n in range(11):
        _part(
            artist / f"EP {n}",
            tracks=2,
            track_ids=[f"rt-{n:02d}a", f"rt-{n:02d}b"],
            album="EPs 1991-2002",
        )
    _part(artist / "Amber", mbid=REL_B, album="Amber")

    albums = scan(tmp_path)

    assert len(albums) == 2
    assert max(a.track_count for a in albums) == 22
    assert len(max(albums, key=lambda a: a.track_count).paths) == 11


# ---------------------------------------------------------------------------
# What must NOT merge
# ---------------------------------------------------------------------------


def test_two_copies_of_one_release_stay_separate(tmp_path):
    """The reason dropping the containment rule is safe. Identical release-track
    ids mean these hold the SAME tracks — a backup, a second rip — not two parts
    of one album."""
    _part(tmp_path / "Music" / "Wide Angle", track_ids=DISC1)
    _part(tmp_path / "Backup" / "Wide Angle", track_ids=DISC1)

    albums = scan(tmp_path)

    assert len(albums) == 2
    assert all(a.track_count == 2 for a in albums)


def test_one_shared_track_is_enough_to_refuse(tmp_path):
    _part(tmp_path / "a", track_ids=["rt-001", "rt-002"])
    _part(tmp_path / "b", track_ids=["rt-002", "rt-003"])

    assert len(scan(tmp_path)) == 2


def test_different_releases_stay_separate(tmp_path):
    _part(tmp_path / "a", mbid=REL_A, track_ids=DISC1)
    _part(tmp_path / "b", mbid=REL_B, track_ids=DISC2, album="Other")

    assert len(scan(tmp_path)) == 2


def test_folders_without_track_ids_fall_back_to_disc_numbers(tmp_path):
    """A pre-2011 rip carries no track ids. Distinct disc numbers are weaker —
    what the files claim rather than what they hold — but still exact."""
    _part(tmp_path / "CD1", disc=1, disc_total=2)
    _part(tmp_path / "CD2", disc=2, disc_total=2)

    assert len(scan(tmp_path)) == 1


def test_same_disc_number_twice_is_a_duplicate(tmp_path):
    _part(tmp_path / "rip-a", disc=1, disc_total=2)
    _part(tmp_path / "rip-b", disc=1, disc_total=2)

    assert len(scan(tmp_path)) == 2


def test_no_evidence_either_way_means_no_merge(tmp_path):
    """No track ids and no disc numbers: nothing distinguishes two parts from
    two copies, so nothing is merged."""
    _part(tmp_path / "a")
    _part(tmp_path / "b")

    assert len(scan(tmp_path)) == 2


def test_track_ids_on_only_one_side_is_refused(tmp_path):
    """Nothing to compare. Falling back to disc numbers would answer with the
    weaker evidence a question the better evidence could settle."""
    _part(tmp_path / "a", track_ids=DISC1, disc=1, disc_total=2)
    _part(tmp_path / "b", disc=2, disc_total=2)

    assert len(scan(tmp_path)) == 2


def test_an_untagged_folder_has_no_identity_to_group_on(tmp_path):
    _part(tmp_path / "a", mbid=None, sidecar=False)
    _part(tmp_path / "b", mbid=None, sidecar=False)

    albums = scan(tmp_path)

    assert len(albums) == 2
    assert all(a.state == AlbumState.NEW for a in albums)


def test_three_copies_of_two_discs_pair_off(tmp_path):
    """Two discs, each present twice. The result is two complete albums, not one
    four-folder album and not four fragments."""
    _part(tmp_path / "main" / "CD1", track_ids=DISC1)
    _part(tmp_path / "main" / "CD2", track_ids=DISC2)
    _part(tmp_path / "backup" / "CD1", track_ids=DISC1)
    _part(tmp_path / "backup" / "CD2", track_ids=DISC2)

    albums = scan(tmp_path)

    assert len(albums) == 2
    assert all(a.track_count == 4 for a in albums)


# ---------------------------------------------------------------------------
# What the merged album looks like
# ---------------------------------------------------------------------------


def test_the_merged_album_is_complete_when_its_parts_together_are(tmp_path):
    """Each half alone is 2 of 4 and INCOMPLETE; together they are the album."""
    _part(tmp_path / "CD1", track_ids=DISC1, disc=1, disc_total=2, track_total=2)
    _part(tmp_path / "CD2", track_ids=DISC2, disc=2, disc_total=2, track_total=2)

    album = scan(tmp_path)[0]

    assert album.expected_track_count == 4
    assert album.state == AlbumState.COMPLETE


def test_the_primary_folder_is_the_one_with_the_most_tracks(tmp_path):
    """Something must answer "where is this album" in one line, and the choice
    has to be stable across scans."""
    _part(tmp_path / "few", tracks=2, track_ids=DISC1)
    _part(tmp_path / "many", tracks=5, track_ids=["a", "b", "c", "d", "e"])

    album = scan(tmp_path)[0]

    assert album.path.name == "many"
    assert len(album.paths) == 2


def test_the_merged_sidecar_takes_the_earliest_added_and_latest_tagged(tmp_path):
    a = _part(tmp_path / "a", track_ids=DISC1)
    b = _part(tmp_path / "b", track_ids=DISC2)
    early, late = datetime(2020, 1, 1, tzinfo=UTC), datetime(2026, 6, 1, tzinfo=UTC)
    for d, when in ((a, early), (b, late)):
        _edit(d, added_at=when, tagged_at=when)

    album = scan(tmp_path)[0]

    assert album.sidecar.added_at == early
    assert album.sidecar.tagged_at == late


@pytest.mark.parametrize("part", ["a", "b"])
def test_a_decision_recorded_on_any_part_holds_for_the_album(tmp_path, part):
    """A surrender or an accepted incompleteness is a decision about the ALBUM,
    whichever of its folders happens to carry the file."""
    _part(tmp_path / "a", track_ids=DISC1)
    _part(tmp_path / "b", track_ids=DISC2)
    _edit(tmp_path / part, tracks_unavailable=True, purchase_unavailable=True)

    album = scan(tmp_path)[0]

    assert album.sidecar.tracks_unavailable is True
    assert album.sidecar.purchase_unavailable is True


@pytest.mark.parametrize("part", ["a", "b"])
def test_an_absent_video_medium_stays_forgiven_across_the_merge(tmp_path, part):
    """`video_media` is a fact about the ALBUM, not about a folder (#206).

    A 3-medium release whose DVD was never ripped, with the two CDs in separate
    folders. Only together are they the whole album — each part alone really is
    short of a CD — so the merged view is the only place the DVD can be forgiven.
    Dropping the field there costs the album #206 outright, and costs one
    MusicBrainz call per scan to re-learn what its sidecar already recorded.
    """
    _part(tmp_path / "a", track_ids=DISC1, disc=2, disc_total=3, track_total=2)
    _part(tmp_path / "b", track_ids=DISC2, disc=3, disc_total=3, track_total=2)
    _edit(tmp_path / part, video_media=(1,))

    album = scan(tmp_path)[0]

    assert album.absent_media == frozenset({1})
    assert album.sidecar.video_media == (1,)
    assert album.state == AlbumState.COMPLETE


def test_every_part_keeps_its_own_sidecar_on_disk(tmp_path):
    """No primary, no shards: a folder moved out of the group is still a
    complete album on its own."""
    _part(tmp_path / "a", track_ids=DISC1)
    _part(tmp_path / "b", track_ids=DISC2)

    scan(tmp_path)

    assert sidecar_mod.has_sidecar(tmp_path / "a")
    assert sidecar_mod.has_sidecar(tmp_path / "b")


def test_grouping_writes_nothing(tmp_path):
    """It is a reading of what is already there. Nothing on disk changes."""
    a = _part(tmp_path / "a", track_ids=DISC1)
    b = _part(tmp_path / "b", track_ids=DISC2)
    before = {p: p.read_bytes() for d in (a, b) for p in d.iterdir()}

    scan(tmp_path)

    assert {p: p.read_bytes() for d in (a, b) for p in d.iterdir()} == before


def test_a_part_moved_away_stands_alone_again(tmp_path):
    """The escape hatch: reorganising folders is the thing this tolerates."""
    _part(tmp_path / "a", track_ids=DISC1)
    b = _part(tmp_path / "b", track_ids=DISC2)
    assert len(scan(tmp_path)) == 1

    shutil.rmtree(b)

    albums = scan(tmp_path)
    assert len(albums) == 1
    assert albums[0].track_count == 2


def test_the_scan_cache_survives_grouping(tmp_path):
    """The cache stays per-directory; grouping happens over its output."""
    _part(tmp_path / "a", track_ids=DISC1)
    _part(tmp_path / "b", track_ids=DISC2)
    cache: dict = {}

    first = scan(tmp_path, album_cache=cache)
    second = scan(tmp_path, album_cache=cache)

    assert len(first) == len(second) == 1
    assert first[0].track_count == second[0].track_count == 4
    assert len(cache) == 2, "one entry per directory"


def test_a_track_added_to_one_part_reaches_the_merged_album(tmp_path):
    _part(tmp_path / "a", track_ids=DISC1)
    _part(tmp_path / "b", track_ids=DISC2)
    cache: dict = {}
    assert scan(tmp_path, album_cache=cache)[0].track_count == 4

    _part(tmp_path / "b", tracks=3, track_ids=[*DISC2, "rt-005"], sidecar=False)

    assert scan(tmp_path, album_cache=cache)[0].track_count == 3 + 2


def test_an_untagged_file_in_a_part_suspends_the_merge(tmp_path):
    """Deliberate, and the conservative direction. A file with no release-track
    id makes its folder's set incomplete, and a partial set cannot prove
    disjointness — two copies of a disc with half their files untagged would
    compare as disjoint on the tagged half. So the folders stand apart until the
    stray is tagged, rather than being merged on evidence that does not hold.

    The album is separately flagged as partially tagged, so the cause is
    visible rather than mysterious."""
    _part(tmp_path / "a", track_ids=DISC1)
    _part(tmp_path / "b", track_ids=DISC2)
    assert len(scan(tmp_path)) == 1

    shutil.copy(SINE_M4A, tmp_path / "b" / "99 Untagged.m4a")

    albums = scan(tmp_path)
    assert len(albums) == 2
    assert any(a.partial_tag_count is not None for a in albums)


# ---------------------------------------------------------------------------
# Tagging an album that spans folders
# ---------------------------------------------------------------------------


def _two_disc_release() -> dict:
    def medium(pos: int, first: int) -> dict:
        return {
            "position": str(pos),
            "format": "CD",
            "track-list": [
                {
                    "id": f"rt-{n:03d}",
                    "position": str(i),
                    "title": f"Track {n}",
                    "recording": {"id": f"rec-{n:03d}", "title": f"Track {n}"},
                }
                for i, n in enumerate((first, first + 1), start=1)
            ],
        }

    return {
        "id": REL_A,
        "title": "Wide Angle",
        "artist-credit": [{"artist": {"id": "art-a", "name": "Hybrid", "sort-name": "Hybrid"}}],
        "release-group": {"id": "rg-a", "primary-type": "Album"},
        "medium-list": [medium(1, 1), medium(2, 3)],
    }


def test_retagging_reaches_every_folder_of_the_album(tmp_path):
    """The failure this would otherwise have: `album.path` is only the PRIMARY
    folder, so tagging what is under it alone leaves the rest of the album on
    its old tags — silently, with a success message."""
    from harmonist import activity_store, formats, tagger

    activity_store.init(tmp_path / "audit.db")
    _part(tmp_path / "CD1", track_ids=DISC1, disc=1, disc_total=2)
    _part(tmp_path / "CD2", track_ids=DISC2, disc=2, disc_total=2)
    album = scan(tmp_path)[0]
    assert len(album.folders) == 2

    from harmonist import album_files

    written = tagger.tag_album(
        album.path, _two_disc_release(), files=album_files.for_paths(album.folders)
    )

    assert written == 4, "every file, not just the primary folder's"
    # Disc 2's files really took disc 2's tracks.
    disc2 = MP4(tmp_path / "CD2" / "01 Track 1.m4a")
    assert disc2["disk"] == [(2, 2)]
    assert disc2["\xa9nam"] == ["Track 3"]
    assert all(formats.read_album_id(f) == REL_A for f in album_files.for_paths(album.folders))


def test_tagging_only_the_primary_folder_is_what_files_prevents(tmp_path):
    """The control, stated so the guard cannot be quietly removed: without the
    explicit file list the tagger sees one folder and refuses on the count."""
    from harmonist import activity_store, tagger

    activity_store.init(tmp_path / "audit.db")
    _part(tmp_path / "CD1", track_ids=DISC1, disc=1, disc_total=2)
    _part(tmp_path / "CD2", track_ids=DISC2, disc=2, disc_total=2)
    album = scan(tmp_path)[0]

    with pytest.raises(tagger.TagMismatchError):
        tagger.tag_album(album.path, _two_disc_release())
