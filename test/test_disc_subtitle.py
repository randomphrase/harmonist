"""Harmonist writes the disc subtitle, as Picard does (#218).

MusicBrainz names a medium where it has a name — Hybrid's two discs are *Wide
Angle* and *Live Angle* — and Picard writes that as `discsubtitle`. So a
Picard-tagged library already carries it, and until now a Harmonist re-tag
STRIPPED it: a field the user's tagger wrote, silently removed by an operation
sold as bringing their tags up to date. That is a review-gate item 4 violation,
not merely a missing feature, which is why the round-trip tests below matter more
than the write ones.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from harmonist import formats
from harmonist.formats import owned
from harmonist.tagger import tag_album

FIXTURES_DIR = Path(__file__).parent / "fixtures"
FIXTURES = ["sine.m4a", "sine.mp3", "sine.flac", "sine.opus"]


def _release(disc_title: str | None) -> dict:
    medium: dict = {
        "position": "1",
        "format": "CD",
        "track-list": [
            {"id": "rt-1", "position": "1", "title": "T1", "recording": {"id": "rec-1"}}
        ],
    }
    if disc_title is not None:
        medium["title"] = disc_title
    return {
        "id": "rel-a",
        "title": "Album",
        "artist-credit": [{"artist": {"id": "a", "name": "Artist", "sort-name": "Artist"}}],
        "release-group": {"id": "rg", "primary-type": "Album"},
        "medium-list": [medium],
    }


def _album(tmp_path: Path, fixture: str) -> Path:
    d = tmp_path / "Artist" / "Album"
    d.mkdir(parents=True, exist_ok=True)
    target = d / f"01 Track{Path(fixture).suffix}"
    shutil.copy(FIXTURES_DIR / fixture, target)
    return d


@pytest.fixture(autouse=True)
def _store(tmp_path):
    from harmonist import activity_store

    activity_store.init(tmp_path / "audit.db")


@pytest.mark.parametrize("fixture", FIXTURES)
def test_the_disc_subtitle_is_written(tmp_path, fixture):
    d = _album(tmp_path, fixture)

    tag_album(d, _release("Live Angle"))

    path = next(p for p in d.iterdir() if formats.is_supported(p))
    assert formats.read_owned(path)[owned.Owned.DISC_SUBTITLE] == "Live Angle"


@pytest.mark.parametrize("fixture", FIXTURES)
def test_a_release_with_no_disc_name_writes_none(tmp_path, fixture):
    """Most releases have no medium title. Writing an empty one would be worse
    than writing nothing."""
    d = _album(tmp_path, fixture)

    tag_album(d, _release(None))

    path = next(p for p in d.iterdir() if formats.is_supported(p))
    assert not formats.read_owned(path).get(owned.Owned.DISC_SUBTITLE)


@pytest.mark.parametrize("fixture", FIXTURES)
def test_it_round_trips_through_write_owned(tmp_path, fixture):
    """It is an owned field, so it rides in the tag-change records and the undo.
    A field that writes but does not read back cannot be reverted."""
    d = _album(tmp_path, fixture)
    tag_album(d, _release("Wide Angle"))
    path = next(p for p in d.iterdir() if formats.is_supported(p))

    values = formats.read_owned(path)
    values[owned.Owned.DISC_SUBTITLE] = "Something Else"
    formats.write_owned(path, values)

    assert formats.read_owned(path)[owned.Owned.DISC_SUBTITLE] == "Something Else"


@pytest.mark.parametrize("fixture", FIXTURES)
def test_removing_it_is_possible(tmp_path, fixture):
    """Undoing a tagging that ADDED the subtitle has to be able to take it off
    again — `write_owned` sets a complete snapshot, absences included."""
    d = _album(tmp_path, fixture)
    tag_album(d, _release("Wide Angle"))
    path = next(p for p in d.iterdir() if formats.is_supported(p))

    values = formats.read_owned(path)
    values[owned.Owned.DISC_SUBTITLE] = None
    formats.write_owned(path, values)

    assert not formats.read_owned(path).get(owned.Owned.DISC_SUBTITLE)


def test_a_retag_no_longer_strips_what_picard_wrote(tmp_path):
    """The actual bug. Picard writes `discsubtitle`; a Harmonist re-tag used to
    remove it, because a field Harmonist does not own is written as absent."""
    d = _album(tmp_path, "sine.m4a")
    path = next(p for p in d.iterdir() if formats.is_supported(p))
    # As Picard left it.
    values = formats.read_owned(path)
    values[owned.Owned.DISC_SUBTITLE] = "Live Angle"
    formats.write_owned(path, values)

    tag_album(d, _release("Live Angle"))

    assert formats.read_owned(path)[owned.Owned.DISC_SUBTITLE] == "Live Angle"


def test_the_field_is_medium_scoped():
    """Track-scoped like `disc_num` and `media`: it describes the MEDIUM, which
    differs between tracks on a multi-disc release even though it reads like an
    album-level fact. Album scope would report one disc's name for the album."""
    assert owned.SCOPE[owned.Owned.DISC_SUBTITLE] is owned.Scope.TRACK
