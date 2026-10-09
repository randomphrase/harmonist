"""The user's enabled tag transforms (#544), and the invariant they rest on.

`transforms.py` states the rule these tests exist to hold in place: *a transform
may only choose among spellings the comparison already accepts unconditionally*.
The write half is the visible feature; the accepted half is what keeps turning
one on from rewriting a library or filling the Inbox, and it is the half a
plausible-looking change would break silently.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from mutagen.mp4 import MP4

from harmonist import formats as formats_mod
from harmonist import gardener, tagger, transforms
from harmonist.formats import owned
from harmonist.tagger import ATOM_ALBUM
from harmonist.transforms import TaggingChoices, TagTransform

from .test_tagger import _release_2_tracks

SINE_M4A = Path(__file__).parent / "fixtures" / "sine.m4a"

DISAMBIG = frozenset({TagTransform.ALBUM_DISAMBIGUATION})
CHOSEN = TaggingChoices(transforms=DISAMBIG)


def _release(disambiguation: str | None = None) -> dict:
    release = _release_2_tracks()
    if disambiguation is not None:
        release["disambiguation"] = disambiguation
    return release


# ---------- the write half ----------


def test_the_transform_appends_the_disambiguation_the_release_states():
    assert transforms.album_title(_release("expanded edition"), DISAMBIG) == (
        "Test Album (expanded edition)"
    )


def test_without_the_transform_the_title_is_musicbrainzs_own():
    """The default, and what every install that says nothing keeps doing."""
    assert transforms.album_title(_release("expanded edition"), frozenset()) == "Test Album"


def test_a_release_with_no_disambiguation_has_nothing_to_append():
    """Not an empty parenthetical, and not a trailing space — the transform has
    no second spelling to choose here, so it must produce the only one there is.
    """
    assert transforms.album_title(_release(), DISAMBIG) == "Test Album"
    assert transforms.album_title(_release(""), DISAMBIG) == "Test Album"


def test_applying_the_transform_twice_is_applying_it_once():
    """Idempotence, which is structural rather than lucky: the transform reads
    the RELEASE, never the tag already on the file. A rule that appended to what
    it found would grow the title a little on every one of #32's nightly passes.
    """
    release = _release("expanded edition")
    once = transforms.album_title(release, DISAMBIG)

    release_as_if_retagged = {**release, "title": once}
    # The release is what it was; MusicBrainz's title has not become the
    # disambiguated one, so the second pass produces exactly the first's answer.
    assert transforms.album_title(release, DISAMBIG) == once
    # And even handed a release whose title ALREADY reads that way — the shape a
    # confused caller would produce — it appends once, not twice.
    assert transforms.album_title(release_as_if_retagged, DISAMBIG) == (
        "Test Album (expanded edition) (expanded edition)"
    )


# ---------- the supported half: the invariant ----------
#
# Which spellings a release supports is now derived by running the write under
# every combination of settings (#678), so the title's two spellings are tested
# where they are used — at the plan, below and in test_tagger: both are Settings
# changes, a title that is neither keeps IDENTITY, and a parenthetical that is
# not the release's disambiguation is a retitle.


def test_every_choice_is_every_combination_of_the_settings():
    """The whole set, as a contract: it is what "a spelling the release
    supports" means, so a setting value missing from it would quietly turn a
    library written under that value into a library of retitles."""
    combos = {
        (c.transforms, c.standardize_artist_names, c.always_standardize_multivalue_artist)
        for c in transforms.every_choice()
    }

    assert combos == {
        (t, names, lists)
        for t in (frozenset(), DISAMBIG)
        for names in transforms.ArtistNames
        for lists in (False, True)
    }


# ---------- the two halves, joined ----------


def test_the_tagset_carries_the_spelling_the_transform_chose():
    """Through `tagsets_for`, which is what the album page's MusicBrainz column
    is built from — so the page shows what a tagging would really write."""
    plain = tagger.tagsets_for(_release("expanded edition"), TaggingChoices())
    transformed = tagger.tagsets_for(_release("expanded edition"), CHOSEN)

    assert {t.album for t in plain} == {"Test Album"}
    assert {t.album for t in transformed} == {"Test Album (expanded edition)"}


def test_conditional_disambiguation_follows_other_releases_in_the_group(tmp_path):
    release = _release("expanded edition")
    release["release-group"] = {"id": "group"}
    album_dir = _album(tmp_path)
    conditional = TaggingChoices(
        transforms=frozenset({TagTransform.ALBUM_DISAMBIGUATION_IF_NEEDED}),
    )
    from dataclasses import replace

    # A duplicate encoding of this release does not distinguish anything.
    single = replace(conditional, library_releases=frozenset({(release["id"], "group")}))
    paired = replace(single, library_releases=single.library_releases | {("other", "group")})
    unrelated = replace(single, library_releases=single.library_releases | {("other", "elsewhere")})
    assert {t.album for t in tagger.tagsets_for(release, single)} == {"Test Album"}
    assert {t.album for t in tagger.tagsets_for(release, unrelated)} == {"Test Album"}
    tagger.tag_album(album_dir, release, tagging=single)
    plan = tagger.plan_album(album_dir, release, artwork=False, tagging=paired)
    assert gardener.verdict_for(plan) == owned.Significance.SETTINGS
    tagger.tag_album(album_dir, release, tagging=paired)
    assert _album_tags(album_dir) == {"Test Album (expanded edition)"}
    assert not tagger.plan_album(album_dir, release, artwork=False, tagging=paired).changes
    assert (
        gardener.verdict_for(tagger.plan_album(album_dir, release, artwork=False, tagging=single))
        == owned.Significance.SETTINGS
    )
    tagger.tag_album(album_dir, release, tagging=single)
    assert _album_tags(album_dir) == {"Test Album"}
    assert not tagger.plan_album(album_dir, release, artwork=False, tagging=single).changes


@pytest.mark.parametrize("comment", [None, "", "shared comment"])
def test_conditional_title_needs_no_unique_comment(comment):
    release = _release(comment)
    release["release-group"] = {"id": "group"}
    choices = TaggingChoices(
        transforms=frozenset({TagTransform.ALBUM_DISAMBIGUATION_IF_NEEDED}),
        library_releases=frozenset({("other", "group")}),
    )
    expected = "Test Album (shared comment)" if comment else "Test Album"
    assert {t.album for t in tagger.tagsets_for(release, choices)} == {expected}


def _album(tmp_path, n: int = 2) -> Path:
    album_dir = tmp_path / "Test Artist" / "Test Album"
    album_dir.mkdir(parents=True)
    for i in range(1, n + 1):
        shutil.copy(SINE_M4A, album_dir / f"{i:02d} Track {i}.m4a")
    return album_dir


def _album_tags(album_dir: Path) -> set[str]:
    """The album atom as it really sits in each file, read with mutagen rather
    than through Harmonist's own reader — so a writer and a reader that agreed
    with each other and disagreed with Picard could not both be wrong here."""
    return {MP4(f)[ATOM_ALBUM][0] for f in sorted(album_dir.iterdir())}


def test_a_tagging_writes_the_transformed_title_to_every_file(tmp_path):
    """The load-bearing one. The tagset being right proves nothing about the
    file: `write_tags` is what puts a value on disk, and the transform has to
    survive the whole path from the setting to the atom.
    """
    album_dir = _album(tmp_path)

    tagger.tag_album(album_dir, _release("expanded edition"), tagging=CHOSEN)

    assert _album_tags(album_dir) == {"Test Album (expanded edition)"}


def test_a_tagging_without_the_transform_writes_musicbrainzs_title(tmp_path):
    album_dir = _album(tmp_path)

    tagger.tag_album(album_dir, _release("expanded edition"))

    assert _album_tags(album_dir) == {"Test Album"}


def _album_fields(plan) -> set[str]:
    return {f for c in plan.changes.values() for f in c}


def _overwrite(album_dir: Path, field: owned.Owned, value: str) -> None:
    """Put `value` in `field` on every file, as another tool would have left it."""
    for f in sorted(album_dir.iterdir()):
        formats_mod.write_owned(f, {**formats_mod.read_owned(f), field.value: value})


def test_turning_the_transform_on_is_a_settings_update(tmp_path):
    """The library converges on the setting (#685). An album tagged with
    MusicBrainz's plain title has an update once the transform is on, and it is
    classified SETTINGS: the title on disk is one this release legitimately has,
    and the new one is what the user chose.
    """
    album_dir = _album(tmp_path)
    release = _release("expanded edition")
    tagger.tag_album(album_dir, release)  # the plain title, as before

    plan = tagger.plan_album(album_dir, release, artwork=False, tagging=CHOSEN)

    assert gardener.verdict_for(plan) == owned.Significance.SETTINGS


def test_and_so_is_turning_it_off(tmp_path):
    """The other direction, symmetric on purpose: off is a choice like any other,
    so a library carrying Picard's disambiguated title converges on the plain one
    — an update that would remove the disambiguation from files, and is why the Settings page
    warns before it happens."""
    album_dir = _album(tmp_path)
    release = _release("expanded edition")
    tagger.tag_album(album_dir, release, tagging=CHOSEN)

    plan = tagger.plan_album(album_dir, release, artwork=False)

    assert gardener.verdict_for(plan) == owned.Significance.SETTINGS


def test_a_title_that_is_neither_accepted_spelling_keeps_its_own_significance(tmp_path):
    """What stops the two tests above from passing for the wrong reason. Only
    two exact strings can be a Settings change, so a genuinely wrong album title
    is a retitle under either setting — one that took this for a Settings change
    would hand #273 a retitle disguised as something the user asked for.
    """
    album_dir = _album(tmp_path)
    release = _release("expanded edition")
    tagger.tag_album(album_dir, release)
    _overwrite(album_dir, owned.Owned.ALBUM, "Something Else")

    plan = tagger.plan_album(album_dir, release, artwork=False, tagging=CHOSEN)

    assert gardener.verdict_for(plan) == owned.Significance.IDENTITY


def test_a_retitle_on_musicbrainz_is_not_lowered_by_the_setting(tmp_path):
    """MusicBrainz and the setting moving the same field at once. The disk holds
    the old release title disambiguated; the new title is not one the release
    has any more, so the change is a retitle and nothing about the setting can
    explain it away."""
    album_dir = _album(tmp_path)
    release = _release("expanded edition")
    tagger.tag_album(album_dir, release, tagging=CHOSEN)
    retitled = {**release, "title": "Renamed Album"}

    plan = tagger.plan_album(album_dir, retitled, artwork=False)

    assert gardener.verdict_for(plan) == owned.Significance.IDENTITY


def test_a_settings_change_beside_an_enrichment_is_an_enrichment(tmp_path):
    """The album's verdict is still its furthest-reaching change, so a Settings
    change cannot carry anything bigger along under its own name."""
    album_dir = _album(tmp_path)
    release = _release("expanded edition")
    tagger.tag_album(album_dir, release)
    _overwrite(album_dir, owned.Owned.DATE, "2021")

    plan = tagger.plan_album(album_dir, release, artwork=False, tagging=CHOSEN)

    assert gardener.verdict_for(plan) == owned.Significance.ENRICHMENT
    assert gardener.follows_settings(plan)


def test_an_album_already_on_the_setting_has_nothing_to_take(tmp_path):
    """And the converse of the first test, so `follows_settings` is not simply
    true of every plan with an album title in it."""
    album_dir = _album(tmp_path)
    release = _release("expanded edition")
    tagger.tag_album(album_dir, release, tagging=CHOSEN)
    _overwrite(album_dir, owned.Owned.DATE, "2021")

    plan = tagger.plan_album(album_dir, release, artwork=False, tagging=CHOSEN)

    assert gardener.verdict_for(plan) == owned.Significance.ENRICHMENT
    assert not gardener.follows_settings(plan)


@pytest.mark.parametrize("enabled", [TaggingChoices(), CHOSEN])
def test_a_release_with_no_disambiguation_is_untouched_either_way(tmp_path, enabled):
    """The transform is opt-in AND inert where the release says nothing, so the
    overwhelming majority of a library is unaffected by ticking the box."""
    album_dir = _album(tmp_path)

    tagger.tag_album(album_dir, _release(), tagging=enabled)

    assert _album_tags(album_dir) == {"Test Album"}


def test_tagging_twice_under_a_transform_is_a_no_op_the_second_time(tmp_path):
    """Review-gate item 7, on the transition this change touches.

    The assertion is on **mtime**, not on the tags, because the tags being right
    twice would also be true of a tagger that rewrote both files identically on
    every pass. mtime is what `reconcile.looks_externally_retagged` reads as
    "somebody tagged this in Picard" (#220), so a write that is not a real
    change is not a harmless one — it is #266's whole point, and #283 is on
    record as the bug that made it unreachable for a whole class of album.
    """
    album_dir = _album(tmp_path)
    release = _release("expanded edition")
    tagger.tag_album(album_dir, release, tagging=CHOSEN)
    before = {f: f.stat().st_mtime_ns for f in sorted(album_dir.iterdir())}

    tagger.tag_album(album_dir, release, tagging=CHOSEN)

    assert {f: f.stat().st_mtime_ns for f in sorted(album_dir.iterdir())} == before
    assert _album_tags(album_dir) == {"Test Album (expanded edition)"}
