"""Picard 3.0's artist-name standardization (#678), as Harmonist writes it.

Picard offers three choices — keep names as credited, standardize variations
only, standardize variations and name changes — plus a checkbox that always
standardizes the multi-value artist lists. The rule underneath is
`picard.mbjson.should_standardize_artist_name`: under "variations", a credited
name is kept only when it is a FORMER name of the artist, which Picard reads as
an ended alias of type Artist name (or untyped).

Harmonist reads MusicBrainz as XML, which has no `ended` flag for an alias, so
"ended" means "has an end date" here (#690 is the way to exact parity).

Mouse Rat is the fixture: credited once as "MouseRat", a variation, and once as
"Scarecrow Boat", a name the band dropped.
"""

from __future__ import annotations

import copy
import shutil
from pathlib import Path
from typing import Any

import pytest

from harmonist import formats as formats_mod
from harmonist import gardener, tagger
from harmonist.formats import owned
from harmonist.formats.types import TagSet
from harmonist.transforms import ArtistNames, TaggingChoices

from .test_tagger import _release_2_tracks

MOUSE_RAT: dict[str, Any] = {
    "id": "art-mouse-rat",
    "name": "Mouse Rat",
    "sort-name": "Mouse Rat",
    "alias-list": [
        # A former name: an Artist name alias that ended.
        {
            "alias": "Scarecrow Boat",
            "sort-name": "Boat, Scarecrow",
            "type": "Artist name",
            "begin-date": "2009",
            "end-date": "2010",
        },
        # A current alias: an Artist name that has not ended.
        {"alias": "Andy Dwyer and Mouse Rat", "sort-name": "Dwyer, Andy", "type": "Artist name"},
        # A search hint with dates is not a name the band went by.
        {
            "alias": "Mouse Rat Live",
            "sort-name": "Mouse Rat Live",
            "type": "Search hint",
            "end-date": "2012",
        },
    ],
}
DUKE_SILVER: dict[str, Any] = {"id": "art-duke", "name": "Duke Silver", "sort-name": "Silver, Duke"}


def _credit(name: str, artist: dict[str, Any] = MOUSE_RAT) -> dict[str, Any]:
    return {"artist": artist, "name": name}


def _release(*credit: Any) -> dict[str, Any]:
    release = copy.deepcopy(_release_2_tracks())
    release["artist-credit"] = list(credit)
    return release


def _tags(release: dict[str, Any], **choices: Any) -> TagSet:
    return tagger.tagsets_for(release, TaggingChoices(**choices))[0]


@pytest.mark.parametrize(
    ("names", "variation", "former"),
    [
        (ArtistNames.NONE, "MouseRat", "Scarecrow Boat"),
        (ArtistNames.VARIATIONS, "Mouse Rat", "Scarecrow Boat"),
        (ArtistNames.ALL, "Mouse Rat", "Mouse Rat"),
    ],
)
def test_each_choice_writes_picards_name(names, variation, former):
    """The three choices, side by side on the two kinds of credit they treat
    differently — the whole of Picard's rule in one table."""
    for credited, expected in (("MouseRat", variation), ("Scarecrow Boat", former)):
        tags = _tags(_release(_credit(credited)), standardize_artist_names=names)
        assert (tags.album_artist, tags.artist) == (expected, expected), credited


def test_a_current_alias_is_a_variation():
    """Only an ENDED name is kept: a credit under a name the artist still goes
    by is a variation of their name, and "variations" standardizes it."""
    tags = _tags(
        _release(_credit("Andy Dwyer and Mouse Rat")),
        standardize_artist_names=ArtistNames.VARIATIONS,
    )
    assert tags.artist == "Mouse Rat"


def test_a_dated_search_hint_is_not_a_former_name():
    """Picard counts only aliases of type Artist name, or untyped. A search hint
    is a spelling people type, not a name the band used."""
    tags = _tags(
        _release(_credit("Mouse Rat Live")), standardize_artist_names=ArtistNames.VARIATIONS
    )
    assert tags.artist == "Mouse Rat"


def test_an_ended_alias_without_a_date_reads_as_current():
    """The XML's limit, pinned so it is a decision rather than an accident: with
    no end date there is nothing to say the alias ended, so the credit is
    standardized where Picard, reading the JSON's `ended` flag, might keep it."""
    artist = copy.deepcopy(MOUSE_RAT)
    del artist["alias-list"][0]["end-date"]
    tags = _tags(
        _release(_credit("Scarecrow Boat", artist)),
        standardize_artist_names=ArtistNames.VARIATIONS,
    )
    assert tags.artist == "Mouse Rat"


def test_a_kept_alias_takes_the_aliases_sort_name():
    """Picard's `_select_sort_name_from_aliases`: a credited name that is an
    alias sorts by that alias's sort name, not the artist's."""
    tags = _tags(_release(_credit("Scarecrow Boat")), standardize_artist_names=ArtistNames.NONE)
    assert tags.artist_sort == "Boat, Scarecrow"


def test_a_standardized_name_takes_the_artists_sort_name():
    tags = _tags(_release(_credit("Scarecrow Boat")), standardize_artist_names=ArtistNames.ALL)
    assert tags.artist_sort == "Mouse Rat"


def test_a_credit_under_no_alias_sorts_by_the_artist():
    """Nothing to select from, so the artist's own sort name — the fallback
    Picard takes, and what Harmonist always wrote."""
    tags = _tags(_release(_credit("MouseRat")), standardize_artist_names=ArtistNames.NONE)
    assert tags.artist_sort == "Mouse Rat"


def test_join_phrases_and_order_survive_standardization():
    """Each artist is spelled on its own and the credit is rebuilt around them,
    so the words MusicBrainz joins them with are untouched."""
    release = _release(_credit("MouseRat"), " & ", _credit("Duke Silver", DUKE_SILVER))
    tags = _tags(release, standardize_artist_names=ArtistNames.ALL)
    assert tags.artist == "Mouse Rat & Duke Silver"
    assert tags.artist_sort == "Mouse Rat & Silver, Duke"
    assert tags.artists == ["Mouse Rat", "Duke Silver"]


def test_the_lists_follow_the_names_by_default():
    tags = _tags(_release(_credit("MouseRat")), standardize_artist_names=ArtistNames.NONE)
    assert tags.artists == ["MouseRat"]
    assert tags.album_artists == ["MouseRat"]


def test_the_list_checkbox_standardizes_the_lists_alone():
    """ "Always standardize multi-valued artist tags": the lists a player groups
    by name the artist, while the display credit keeps the credited name."""
    tags = _tags(
        _release(_credit("Scarecrow Boat")),
        standardize_artist_names=ArtistNames.NONE,
        always_standardize_multivalue_artist=True,
    )
    assert (tags.artist, tags.artists, tags.album_artists) == (
        "Scarecrow Boat",
        ["Mouse Rat"],
        ["Mouse Rat"],
    )


def test_by_default_a_credit_is_written_as_credited():
    """`TaggingChoices()` is MusicBrainz's own spelling — what every tagging
    wrote before these settings existed. The CONFIG defaults to "variations";
    the value object does not, so code with no setting to read stays put."""
    assert _tags(_release(_credit("MouseRat"))).artist == "MouseRat"


def test_the_album_page_links_every_spelling_of_a_credit():
    """#309 links an artist credit on the album page by the phrase it renders
    as, so the links appear only on a value that IS that phrase. A library may
    carry any setting's spelling (#678), so every one of them is keyed — each
    linking to the same artists — or a standardized file loses its links."""
    release = _release(_credit("Scarecrow Boat"), " & ", _credit("Duke", DUKE_SILVER))

    credits = tagger.artist_credits(release)

    for phrase in (
        "Scarecrow Boat & Duke",
        "Scarecrow Boat & Duke Silver",
        "Mouse Rat & Duke Silver",
    ):
        assert [part.mbid for part in credits[phrase]] == ["art-mouse-rat", "art-duke"], phrase


# ---------- an existing library converges, at the Settings level ----------

SINE_M4A = Path(__file__).parent / "fixtures" / "sine.m4a"

AS_CREDITED = TaggingChoices(standardize_artist_names=ArtistNames.NONE)
VARIATIONS = TaggingChoices(standardize_artist_names=ArtistNames.VARIATIONS)
ALL_NAMES = TaggingChoices(standardize_artist_names=ArtistNames.ALL)


def _album(tmp_path: Path, release: dict[str, Any], tagging: TaggingChoices) -> Path:
    album_dir = tmp_path / "Mouse Rat" / "Test Album"
    album_dir.mkdir(parents=True)
    for i in (1, 2):
        shutil.copy(SINE_M4A, album_dir / f"{i:02d} Track {i}.m4a")
    tagger.tag_album(album_dir, release, tagging=tagging)
    return album_dir


def _overwrite(album_dir: Path, **values: Any) -> None:
    for f in sorted(album_dir.iterdir()):
        formats_mod.write_owned(f, {**formats_mod.read_owned(f), **values})


def _verdict(album_dir: Path, release: dict[str, Any], tagging: TaggingChoices):
    return gardener.verdict_for(
        tagger.plan_album(album_dir, release, artwork=False, tagging=tagging)
    )


def test_a_credited_variation_converges_on_the_default(tmp_path):
    """The bvdub case (#678): an album tagged with the credited name has a
    Settings update under "variations" — the display credit, the sort credit
    and the lists all move, and each is a spelling this release has."""
    release = _release(_credit("MouseRat"))
    album_dir = _album(tmp_path, release, AS_CREDITED)

    assert _verdict(album_dir, release, VARIATIONS) == owned.Significance.SETTINGS


def test_and_back_again(tmp_path):
    """Symmetric, as #685's title setting is: whichever way the setting points,
    the other supported spelling is a Settings change."""
    release = _release(_credit("Scarecrow Boat"))
    album_dir = _album(tmp_path, release, ALL_NAMES)

    assert _verdict(album_dir, release, AS_CREDITED) == owned.Significance.SETTINGS


def test_harmonists_old_sort_name_is_a_supported_spelling(tmp_path):
    """Before #678 a kept alias sorted under the artist ("Mouse Rat"); now it
    sorts under its own alias ("Boat, Scarecrow"). The old value is what "all"
    writes, so the change is the setting's, not a fact MusicBrainz moved."""
    release = _release(_credit("Scarecrow Boat"))
    album_dir = _album(tmp_path, release, AS_CREDITED)
    # The album credit only: the fixture's second track is credited to another
    # artist, so its own sort name is not Mouse Rat's under any setting.
    _overwrite(album_dir, album_artist_sort="Mouse Rat")

    assert _verdict(album_dir, release, AS_CREDITED) == owned.Significance.SETTINGS


def test_a_name_no_setting_writes_keeps_its_own_significance(tmp_path):
    """What stops the tests above passing for the wrong reason: a credit that
    is not one of the release's spellings is a change of artist, whatever the
    setting."""
    release = _release(_credit("MouseRat"))
    album_dir = _album(tmp_path, release, AS_CREDITED)
    _overwrite(album_dir, album_artist="Mouse Rats")

    assert _verdict(album_dir, release, VARIATIONS) == owned.Significance.IDENTITY


def test_a_credit_mixing_two_choices_is_no_choices_spelling(tmp_path):
    """A supported spelling is what ONE setting writes for the whole credit.
    Standardizing one artist and keeping the other's credited name is neither
    choice's output, so it keeps its ordinary significance."""
    duke = {**DUKE_SILVER}
    release = _release(_credit("MouseRat"), " & ", _credit("Duke", duke))
    album_dir = _album(tmp_path, release, AS_CREDITED)
    _overwrite(album_dir, album_artist="Mouse Rat & Duke")

    assert _verdict(album_dir, release, ALL_NAMES) == owned.Significance.IDENTITY


def test_the_list_checkbox_converges_the_lists(tmp_path):
    release = _release(_credit("Scarecrow Boat"))
    album_dir = _album(tmp_path, release, AS_CREDITED)
    lists = TaggingChoices(
        standardize_artist_names=ArtistNames.NONE, always_standardize_multivalue_artist=True
    )

    assert _verdict(album_dir, release, lists) == owned.Significance.SETTINGS


def test_settings_alone_have_nothing_to_take_once_applied(tmp_path):
    """Idempotent: a library tagged under the setting has nothing outstanding
    under it."""
    release = _release(_credit("Scarecrow Boat"), " & ", _credit("Duke", DUKE_SILVER))
    album_dir = _album(tmp_path, release, VARIATIONS)

    assert _verdict(album_dir, release, VARIATIONS) is None
