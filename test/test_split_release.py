"""Split releases: one MB release filed as per-disc directories (#16).

Two halves, tested separately because they fail differently: the SCANNER
grouping (a sidecar'd parent owns the audio beneath it) and the DETECTION that
writes that sidecar in the first place.
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest
from mutagen.mp4 import MP4

from harmonist import album_files, reconcile
from harmonist import sidecar as sidecar_mod
from harmonist.models import AlbumState, Sidecar
from harmonist.scanner import scan

FIXTURES_DIR = Path(__file__).parent / "fixtures"
SINE_M4A = FIXTURES_DIR / "sine.m4a"

MBID = "11111111-2222-3333-4444-555555555555"
OTHER_MBID = "99999999-8888-7777-6666-555555555555"


def _disc_dir(
    parent: Path,
    name: str,
    *,
    mbid: str | None = MBID,
    disc: int | None = 1,
    tracks: int = 2,
    sidecar: bool = True,
) -> Path:
    """One disc's directory: `tracks` tagged files, plus its own sidecar."""
    d = parent / name
    d.mkdir(parents=True)
    for i in range(1, tracks + 1):
        f = d / f"{i:02d} Track {i}.m4a"
        shutil.copy(SINE_M4A, f)
        audio = MP4(f)
        audio["\xa9alb"] = ["Vapourized Volume One"]
        audio["\xa9ART"] = ["Kasey Taylor"]
        if disc is not None:
            audio["disk"] = [(disc, 2)]
        if mbid is not None:
            audio["----:com.apple.iTunes:MusicBrainz Album Id"] = [mbid.encode()]
        audio.save()
    if sidecar:
        sidecar_mod.write(
            d,
            Sidecar(
                mb_release_id=mbid,
                added_at=datetime(2026, 1, 1, tzinfo=UTC),
                tagged_at=datetime(2026, 1, 1, tzinfo=UTC),
            ),
        )
    return d


def _edit_sidecar(album_dir: Path, **fields: object) -> None:
    """Adjust an already-written sidecar in place."""
    sc = sidecar_mod.read(album_dir)
    assert sc is not None
    for key, value in fields.items():
        setattr(sc, key, value)
    sidecar_mod.write(album_dir, sc)


def _split_album(root: Path, name: str = "Vapourized Volume One") -> Path:
    parent = root / "Various Artists" / name
    _disc_dir(parent, "CD1", disc=1)
    _disc_dir(parent, "CD2", disc=2)
    return parent


# ---------------------------------------------------------------------------
# Scanner grouping
# ---------------------------------------------------------------------------


def test_disc_dirs_are_separate_albums_until_the_parent_is_promoted(tmp_path):
    """The pre-#16 shape, and the shape a library is in before reconcile runs:
    two directories, two tiles."""
    _split_album(tmp_path)
    albums = scan(tmp_path)
    assert len(albums) == 2
    assert {a.path.name for a in albums} == {"CD1", "CD2"}


def test_a_sidecared_parent_owns_the_audio_beneath_it(tmp_path):
    parent = _split_album(tmp_path)
    sidecar_mod.write(parent, Sidecar(mb_release_id=MBID, tagged_at=datetime.now(UTC)))

    albums = scan(tmp_path)

    assert [a.path for a in albums] == [parent]
    assert albums[0].track_count == 4


def test_grouped_album_orders_its_files_by_disc(tmp_path):
    parent = _split_album(tmp_path)
    sidecar_mod.write(parent, Sidecar(mb_release_id=MBID))

    files = album_files.audio_files(parent)

    assert [str(f.relative_to(parent)) for f in files] == [
        "CD1/01 Track 1.m4a",
        "CD1/02 Track 2.m4a",
        "CD2/01 Track 1.m4a",
        "CD2/02 Track 2.m4a",
    ]


def test_disc_directories_sort_numerically_not_lexically(tmp_path):
    """ "CD10" after "CD2". Full tagging zips this list against the release's
    tracks positionally, so lexical order would tag every disc as the wrong one."""
    parent = tmp_path / "Box Set"
    for n in (1, 2, 10):
        _disc_dir(parent, f"CD{n}", disc=n, tracks=1, sidecar=False)
    sidecar_mod.write(parent, Sidecar(mb_release_id=MBID))

    files = album_files.audio_files(parent)

    assert [f.parent.name for f in files] == ["CD1", "CD2", "CD10"]


def test_a_directory_with_its_own_audio_is_never_grouped(tmp_path):
    """The safety property: no album that exists today can change shape. An
    album dir has audio of its own, so a bonus subfolder is not absorbed."""
    album = tmp_path / "Artist" / "Album"
    album.mkdir(parents=True)
    shutil.copy(SINE_M4A, album / "01 Track.m4a")
    sidecar_mod.write(album, Sidecar(mb_release_id=MBID))
    _disc_dir(album, "bonus", disc=None, tracks=1, sidecar=False)

    albums = scan(tmp_path)

    assert len(albums) == 2
    assert {a.path.name for a in albums} == {"Album", "bonus"}
    by_name = {a.path.name: a for a in albums}
    assert by_name["Album"].track_count == 1


def test_a_parent_without_a_sidecar_is_not_an_album(tmp_path):
    """An artist directory holds album directories and is not itself an album."""
    _split_album(tmp_path)
    albums = scan(tmp_path)
    assert all(a.path.name != "Various Artists" for a in albums)


def test_grouped_album_rescans_from_cache_when_nothing_changed(tmp_path):
    parent = _split_album(tmp_path)
    sidecar_mod.write(parent, Sidecar(mb_release_id=MBID))
    cache: dict = {}

    first = scan(tmp_path, album_cache=cache)
    second = scan(tmp_path, album_cache=cache)

    assert [a.path for a in first] == [a.path for a in second]
    assert list(cache) == [parent]


def test_grouped_album_notices_a_track_added_in_a_disc_dir(tmp_path):
    """The signature must span the subdirectories, or a change inside one would
    be invisible to a cached rescan."""
    parent = _split_album(tmp_path)
    sidecar_mod.write(parent, Sidecar(mb_release_id=MBID))
    cache: dict = {}
    assert scan(tmp_path, album_cache=cache)[0].track_count == 4

    shutil.copy(SINE_M4A, parent / "CD2" / "03 Track 3.m4a")

    assert scan(tmp_path, album_cache=cache)[0].track_count == 5


def test_removing_the_parent_sidecar_ungroups_the_album(tmp_path):
    """The escape hatch — no hand-editing of JSON, and nothing on disk to undo."""
    parent = _split_album(tmp_path)
    sidecar_mod.write(parent, Sidecar(mb_release_id=MBID))
    assert len(scan(tmp_path)) == 1

    sidecar_mod.sidecar_path(parent).unlink()

    assert {a.path.name for a in scan(tmp_path)} == {"CD1", "CD2"}


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def _find(root: Path):
    return reconcile.find_split_releases(scan(root), root)


def test_finds_a_release_split_across_two_disc_dirs(tmp_path):
    parent = _split_album(tmp_path)

    found = _find(tmp_path)

    assert len(found) == 1
    assert found[0].parent == parent
    assert found[0].mb_release_id == MBID
    assert [p.name for p in found[0].parts] == ["CD1", "CD2"]


def test_parts_are_ordered_by_disc_number_not_by_name(tmp_path):
    parent = tmp_path / "Artist" / "Album"
    _disc_dir(parent, "second-half", disc=2)
    _disc_dir(parent, "first-half", disc=1)

    found = _find(tmp_path)

    assert [p.name for p in found[0].parts] == ["first-half", "second-half"]


def test_duplicate_copies_of_one_disc_are_not_a_split_release(tmp_path):
    """The case the disc number exists to reject: same release, same disc, two
    folders. Merging those would fabricate a 2-disc album out of a duplicate."""
    parent = tmp_path / "Artist" / "Album"
    _disc_dir(parent, "copy-a", disc=1)
    _disc_dir(parent, "copy-b", disc=1)

    assert _find(tmp_path) == []


def test_untagged_disc_numbers_are_not_a_split_release(tmp_path):
    """No disc number is no evidence, and no evidence is not a merge."""
    parent = tmp_path / "Artist" / "Album"
    _disc_dir(parent, "one", disc=None)
    _disc_dir(parent, "two", disc=None)

    assert _find(tmp_path) == []


def test_different_releases_are_not_a_split_release(tmp_path):
    parent = tmp_path / "Artist" / "Box"
    _disc_dir(parent, "CD1", mbid=MBID, disc=1)
    _disc_dir(parent, "CD2", mbid=OTHER_MBID, disc=2)

    assert _find(tmp_path) == []


def test_a_part_without_a_sidecar_is_not_a_split_release(tmp_path):
    parent = tmp_path / "Artist" / "Box"
    _disc_dir(parent, "CD1", disc=1)
    _disc_dir(parent, "CD2", disc=2, sidecar=False)

    assert _find(tmp_path) == []


def test_a_lone_subdirectory_is_not_a_split_release(tmp_path):
    parent = tmp_path / "Artist" / "Album"
    _disc_dir(parent, "CD1", disc=1)

    assert _find(tmp_path) == []


def test_audio_loose_in_the_parent_blocks_the_merge(tmp_path):
    """A container directory that happens to hold two discs — absorbing the
    loose file too would be a guess."""
    parent = _split_album(tmp_path)
    shutil.copy(SINE_M4A, parent / "bonus.m4a")

    assert _find(tmp_path) == []


def test_the_library_root_is_never_a_split_release(tmp_path):
    _disc_dir(tmp_path, "CD1", disc=1)
    _disc_dir(tmp_path, "CD2", disc=2)

    assert _find(tmp_path) == []


def test_an_already_grouped_parent_is_not_found_again(tmp_path):
    parent = _split_album(tmp_path)
    sidecar_mod.write(parent, Sidecar(mb_release_id=MBID))

    assert _find(tmp_path) == []


# ---------------------------------------------------------------------------
# Promotion
# ---------------------------------------------------------------------------


def test_promotion_writes_the_parent_sidecar_and_moves_no_files(tmp_path):
    parent = _split_album(tmp_path)
    before = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*.m4a"))

    reconcile.promote_split_release(_find(tmp_path)[0])

    assert sidecar_mod.read(parent).mb_release_id == MBID
    assert sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*.m4a")) == before


def test_promotion_leaves_the_parts_own_sidecars_alone(tmp_path):
    """Deleting them would be a destructive write on a derived rule. The user
    removes them by hand if they want to."""
    parent = _split_album(tmp_path)

    reconcile.promote_split_release(_find(tmp_path)[0])

    assert sidecar_mod.has_sidecar(parent / "CD1")
    assert sidecar_mod.has_sidecar(parent / "CD2")


def test_promoted_album_scans_as_one_complete_album(tmp_path):
    parent = _split_album(tmp_path)

    reconcile.promote_split_release(_find(tmp_path)[0])
    albums = scan(tmp_path)

    assert [a.path for a in albums] == [parent]
    assert albums[0].track_count == 4
    assert albums[0].state == AlbumState.COMPLETE


def test_promotion_is_idempotent(tmp_path):
    """Reconcile runs on startup and after every sync; a second pass must find
    nothing and write nothing."""
    parent = _split_album(tmp_path)
    reconcile.promote_split_release(_find(tmp_path)[0])
    written = sidecar_mod.sidecar_path(parent).read_text()

    assert _find(tmp_path) == []
    assert sidecar_mod.sidecar_path(parent).read_text() == written


def test_promotion_inherits_the_widest_expected_track_count(tmp_path):
    """Each part Harmonist tagged recorded the WHOLE release's count, so the
    maximum is that count — never the sum."""
    parent = tmp_path / "Artist" / "Album"
    d1 = _disc_dir(parent, "CD1", disc=1, tracks=2)
    d2 = _disc_dir(parent, "CD2", disc=2, tracks=2)
    for d in (d1, d2):
        _edit_sidecar(d, track_count_expected=4)

    reconcile.promote_split_release(_find(tmp_path)[0])

    assert sidecar_mod.read(parent).track_count_expected == 4


def test_promotion_keeps_the_earliest_added_and_latest_tagged(tmp_path):
    parent = tmp_path / "Artist" / "Album"
    d1 = _disc_dir(parent, "CD1", disc=1)
    d2 = _disc_dir(parent, "CD2", disc=2)
    early, late = datetime(2020, 1, 1, tzinfo=UTC), datetime(2026, 6, 1, tzinfo=UTC)
    for d, when in ((d1, early), (d2, late)):
        _edit_sidecar(d, added_at=when, tagged_at=when)

    reconcile.promote_split_release(_find(tmp_path)[0])

    merged = sidecar_mod.read(parent)
    assert merged.added_at == early
    assert merged.tagged_at == late


def test_promotion_carries_a_store_url_from_whichever_part_has_one(tmp_path):
    parent = tmp_path / "Artist" / "Album"
    _disc_dir(parent, "CD1", disc=1)
    d2 = _disc_dir(parent, "CD2", disc=2)
    _edit_sidecar(d2, store_url="https://kaseytaylor.bandcamp.com/album/vapourized")

    reconcile.promote_split_release(_find(tmp_path)[0])

    assert sidecar_mod.read(parent).store_url == (
        "https://kaseytaylor.bandcamp.com/album/vapourized"
    )


@pytest.mark.parametrize("part", ["CD1", "CD2"])
def test_promotion_preserves_a_surrender(tmp_path, part):
    """`purchase_unavailable` is permanent — losing it on one disc would put the
    album back through surrender on the next full sync."""
    parent = tmp_path / "Artist" / "Album"
    _disc_dir(parent, "CD1", disc=1)
    _disc_dir(parent, "CD2", disc=2)
    _edit_sidecar(parent / part, purchase_unavailable=True)

    reconcile.promote_split_release(_find(tmp_path)[0])

    assert sidecar_mod.read(parent).purchase_unavailable is True


# ---------------------------------------------------------------------------
# Tagging and undo across disc directories
# ---------------------------------------------------------------------------


def _two_disc_release() -> dict:
    """A 2-disc, 4-track MB release — the shape a split album matches."""

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
        "id": MBID,
        "title": "Vapourized Volume One",
        "artist-credit": [
            {"artist": {"id": "art-a", "name": "Kasey Taylor", "sort-name": "Taylor, Kasey"}}
        ],
        "release-group": {"id": "rg-a", "primary-type": "Album"},
        "medium-list": [medium(1, 1), medium(2, 3)],
    }


def _grouped(tmp_path: Path) -> Path:
    parent = _split_album(tmp_path)
    reconcile.promote_split_release(_find(tmp_path)[0])
    return parent


def test_a_grouped_album_can_be_re_tagged(tmp_path):
    """The album page's Re-tag button. Before #16 the tagger listed only the
    parent's own files, found none, and raised."""
    from harmonist import activity_store, tagger

    activity_store.init(tmp_path / "audit.db")
    parent = _grouped(tmp_path)

    assert tagger.tag_album(parent, _two_disc_release()) == 4


def test_re_tagging_a_grouped_album_numbers_the_discs_from_their_folders(tmp_path):
    """Disc 2's files must take disc 2's tracks. This is the ordering the
    natural sort exists for, checked end to end."""
    from harmonist import activity_store, tagger
    from harmonist.tagger import ATOM_DISC_NUM, ATOM_TITLE

    activity_store.init(tmp_path / "audit.db")
    parent = _grouped(tmp_path)

    tagger.tag_album(parent, _two_disc_release())

    disc2 = MP4(parent / "CD2" / "01 Track 1.m4a")
    assert disc2[ATOM_DISC_NUM] == [(2, 2)]
    assert disc2[ATOM_TITLE] == ["Track 3"]


def test_records_name_a_grouped_albums_files_by_disc(tmp_path):
    """Both discs have an "01 Track 1.m4a". A bare filename in the records would
    not merely display wrong — it would restore one disc's tags onto the other."""
    from harmonist import activity_store, tagger

    activity_store.init(tmp_path / "audit.db")
    parent = _grouped(tmp_path)

    tagger.tag_album(parent, _two_disc_release())

    assert {r.file for r in _records()} == {
        "CD1/01 Track 1.m4a",
        "CD1/02 Track 2.m4a",
        "CD2/01 Track 1.m4a",
        "CD2/02 Track 2.m4a",
    }


ESCAPES = ["../../secrets.m4a", "/etc/passwd", "CD1/../../../x.m4a", ".", "", "CD1/./../.."]


@pytest.mark.parametrize("name", ESCAPES)
def test_undo_refuses_a_record_naming_a_file_outside_the_album(tmp_path, name):
    """Records are permanent and unversioned, so a malformed one turns up
    eventually. Allowing a disc directory must not allow an escape from the
    album."""
    from harmonist import tag_history, tagger

    parent = _grouped(tmp_path)

    plan = [tag_history.FileRevert(file=name, fields={})]
    with pytest.raises(tagger.RevertUnavailableError):
        tagger.revert_tags(parent, plan)


@pytest.mark.parametrize("name", ESCAPES)
def test_artwork_restore_refuses_a_record_naming_a_file_outside_the_album(tmp_path, name):
    """The same guard, on the other undo — it takes file names from the same
    records, which now carry a disc directory."""
    from harmonist import tagger

    parent = _grouped(tmp_path)

    with pytest.raises(tagger.ArtworkUnavailableError):
        tagger.restore_artwork(parent, {name: "deadbeef"})


def test_undo_accepts_a_name_naming_a_real_file_in_a_disc_dir(tmp_path):
    """The guard must still let the legitimate case through — the shape it was
    relaxed for."""
    from harmonist import activity_store, tag_history, tagger

    activity_store.init(tmp_path / "audit.db")
    parent = _grouped(tmp_path)
    tagger.tag_album(parent, _two_disc_release())

    plan = [r for r in tag_history.revert_plan(_records()) if r.file == "CD2/02 Track 2.m4a"]

    assert plan, "the disc-qualified record is what we mean to act on"
    assert tagger.revert_tags(parent, plan).files == 1


def test_undo_puts_back_each_discs_own_tags(tmp_path):
    """End to end: the disc-qualified names in the records resolve back to the
    right files, so an undo of a grouped album's tagging really does undo it."""
    from harmonist import activity_store, formats, tag_history, tagger

    activity_store.init(tmp_path / "audit.db")
    parent = _grouped(tmp_path)
    before = {
        str(f.relative_to(parent)): formats.read_owned(f) for f in album_files.audio_files(parent)
    }

    tagger.tag_album(parent, _two_disc_release())
    tagger.revert_tags(parent, tag_history.revert_plan(_records()))

    after = {
        str(f.relative_to(parent)): formats.read_owned(f) for f in album_files.audio_files(parent)
    }
    assert after == before


def _records():
    """Every stored tag-change record, in the order it was written."""
    from harmonist import activity_store

    conn = activity_store._ensure()
    ids = [r[0] for r in conn.execute("SELECT event_id FROM tag_changes ORDER BY event_id")]
    detail = activity_store.tag_changes_for(ids)
    return [detail[i] for i in ids]


# ---------------------------------------------------------------------------
# Through the reconcile runner — how promotion actually reaches a library
# ---------------------------------------------------------------------------


def test_the_reconcile_pass_groups_split_releases(tmp_path):
    """Reconcile is where this runs, and it must run even on a library with no
    orphans in it: split releases are made of COMPLETE albums, so gating on the
    orphan count would mean it only fired by coincidence."""
    from harmonist import activity_store
    from harmonist.web.reconcile_runner import reconcile_pending_orphans

    activity_store.init(tmp_path / "audit.db")
    parent = _split_album(tmp_path)

    stats = reconcile_pending_orphans(tmp_path, fetch_urls=lambda mbid: [], rate_limit_seconds=0)

    assert stats["total"] == 0, "no orphans — the early return path"
    assert stats["grouped"] == 1
    assert sidecar_mod.read(parent).mb_release_id == MBID
    assert [a.path for a in scan(tmp_path)] == [parent]


def test_a_second_reconcile_pass_groups_nothing(tmp_path):
    from harmonist import activity_store
    from harmonist.web.reconcile_runner import reconcile_pending_orphans

    activity_store.init(tmp_path / "audit.db")
    _split_album(tmp_path)

    first = reconcile_pending_orphans(tmp_path, fetch_urls=lambda m: [], rate_limit_seconds=0)
    second = reconcile_pending_orphans(tmp_path, fetch_urls=lambda m: [], rate_limit_seconds=0)

    assert (first["grouped"], second["grouped"]) == (1, 0)


def test_grouping_is_recorded_in_the_activity_feed(tmp_path):
    from harmonist import activity, activity_store
    from harmonist.web.reconcile_runner import reconcile_pending_orphans

    activity_store.init(tmp_path / "audit.db")
    activity.clear()
    _split_album(tmp_path)

    reconcile_pending_orphans(tmp_path, fetch_urls=lambda m: [], rate_limit_seconds=0)

    messages = [e.message for e in activity_store.recent(50)]
    assert any("Grouped 2 disc folder(s) into one album (4 tracks)" in m for m in messages)
    assert any(m.startswith("album.group") and f"release={MBID}" in m for m in messages)


def test_forgetting_a_grouped_album_is_not_undone_by_the_next_reconcile(tmp_path):
    """Forget deletes the parent's sidecar — the UI way to un-group an album.
    Re-grouping it on the next pass would make the button do nothing."""
    from harmonist import activity_store
    from harmonist.web.reconcile_runner import reconcile_pending_orphans

    activity_store.init(tmp_path / "audit.db")
    parent = _split_album(tmp_path)
    reconcile_pending_orphans(tmp_path, fetch_urls=lambda m: [], rate_limit_seconds=0)
    sidecar_mod.sidecar_path(parent).unlink()  # what Forget does

    stats = reconcile_pending_orphans(
        tmp_path, fetch_urls=lambda m: [], rate_limit_seconds=0, exempt_paths={parent}
    )

    assert stats["grouped"] == 0
    assert not sidecar_mod.has_sidecar(parent)
    assert {a.path.name for a in scan(tmp_path)} == {"CD1", "CD2"}
