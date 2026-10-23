"""Local artist identity and metadata browsing (#813)."""

import shutil
from pathlib import Path

import pytest

from harmonist import formats, scanner
from harmonist.formats.types import TagSet

ALICE = "11111111-1111-4111-8111-111111111111"
BOB = "22222222-2222-4222-8222-222222222222"
RELEASE = "33333333-3333-4333-8333-333333333333"
FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.parametrize("filename", ["sine.m4a", "sine.mp3", "sine.flac", "sine.opus"])
def test_scan_keeps_separate_album_artists_in_one_read(tmp_path, filename):
    path = tmp_path / filename
    shutil.copy(FIXTURES / filename, path)
    formats.write_tags(
        path,
        TagSet(
            mb_album_id=RELEASE,
            album="Together",
            album_artist="Alice & Bob",
            album_artists=["Alice", "Bob"],
            mb_album_artist_ids=[ALICE, BOB],
            title="Song",
            artist="Guest",
            track_num=1,
            track_total=1,
        ),
        None,
    )
    fields = formats.read_scan_fields(path)
    assert getattr(fields, "album_artist_names", ()) == ("Alice", "Bob")
    assert getattr(fields, "album_artist_ids", ()) == (ALICE, BOB)
    album = scanner.build_album(tmp_path, [path], scanner.read_album_io(tmp_path, [path]))
    assert [(c.name, c.mbid) for c in album.artist_credits] == [("Alice", ALICE), ("Bob", BOB)]


def test_index_distinguishes_homonyms_and_keeps_collaborations(tmp_path):
    from harmonist.artists import build_index
    from harmonist.models import Album, AlbumArtist, AlbumState

    def album(key, credits):
        return Album(
            id=key,
            path=tmp_path / key,
            title=key,
            artist="display",
            track_count=1,
            state=AlbumState.COMPLETE,
            artist_credits=credits,
        )

    together = album("together", (AlbumArtist("Éclair", ALICE), AlbumArtist("Bob", BOB)))
    solo = album("solo", (AlbumArtist("Éclair", ALICE),))
    homonym = album("other", (AlbumArtist("Éclair", RELEASE),))
    unverified = album("unverified", (AlbumArtist("Éclair"),))
    result = build_index([together, solo, homonym, unverified])
    assert [a.name for a in result] == ["Bob", "Éclair", "Éclair", "Éclair"]
    by_id = {a.key: a for a in result}
    assert {a.id for a in by_id[ALICE].albums} == {"together", "solo"}
    assert {a.id for a in by_id[BOB].albums} == {"together"}
    assert {a.id for a in by_id[RELEASE].albums} == {"other"}
    assert len([a for a in result if a.mbid is None]) == 1


def test_credits_need_consistent_aligned_ids_and_never_split_names(tmp_path):
    from harmonist.artists import build_index

    def build(fields):
        return scanner.build_album(
            tmp_path,
            [tmp_path / f"{i}.flac" for i in range(len(fields))],
            scanner.AlbumIO(sidecar=None, fields=fields, cover_path=None, artwork_local=None),
        )

    a = formats.ScanFields("Title", None, "Guest", "FLAC", album_artist="Earth, Wind & Fire")
    assert [a.name for a in build_index([build([a])])] == ["Earth, Wind & Fire"]
    # Album-artist IDs cannot identify per-track guests or be zipped against a
    # differently-sized name list. Disagreement across tracks also invalidates it.
    paired = a._replace(album_artist_names=("Alice", "Bob"), album_artist_ids=(ALICE, BOB))
    mismatch = paired._replace(album_artist_ids=(ALICE,))
    assert all(a.mbid is None for a in build_index([build([mismatch])]))
    assert all(a.mbid is None for a in build_index([build([paired, mismatch])]))


def test_missing_artist_ids_on_two_tracks_keep_the_album_artist_identified():
    from harmonist.artists import album_credits
    from harmonist.models import AlbumArtist

    tagged = formats.ScanFields(
        "Meteora",
        RELEASE,
        "Linkin Park",
        "ALAC",
        album_artist="Linkin Park",
        album_artist_names=("Linkin Park",),
        album_artist_ids=(ALICE,),
    )
    unassigned = tagged._replace(album_artist_names=(), album_artist_ids=())
    assert album_credits([tagged] * 13 + [unassigned] * 2) == (
        AlbumArtist("Linkin Park", ALICE, missing_id_count=2),
    )


@pytest.mark.parametrize(
    "change",
    [
        {"album_id": None},
        {"album_id": BOB},
        {"album_artist": "Someone else"},
        {"album_artist_names": ("Someone else",)},
        {"album_artist_names": ("Linkin Park",), "album_artist_ids": (BOB,)},
        {"album_artist_names": ("Linkin Park",), "album_artist_ids": ("invalid",)},
    ],
)
def test_partial_artist_identification_requires_one_album_and_no_conflicts(change):
    from harmonist.artists import album_credits

    tagged = formats.ScanFields(
        "Meteora",
        RELEASE,
        "Linkin Park",
        "ALAC",
        album_artist="Linkin Park",
        album_artist_names=("Linkin Park",),
        album_artist_ids=(ALICE,),
    )
    other = tagged._replace(album_artist_names=(), album_artist_ids=())._replace(**change)
    assert not any(credit.mbid for credit in album_credits([tagged, other]))
