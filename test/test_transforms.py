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
from harmonist import tagger, transforms
from harmonist.formats import owned
from harmonist.tagger import ATOM_ALBUM
from harmonist.transforms import TagTransform

from .test_tagger import _release_2_tracks

SINE_M4A = Path(__file__).parent / "fixtures" / "sine.m4a"

DISAMBIG = frozenset({TagTransform.ALBUM_DISAMBIGUATION})


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


# ---------- the accepted half: the invariant ----------


def test_both_spellings_are_accepted_whichever_one_would_be_written():
    """THE test of the module invariant. The accepted set does not depend on the
    enabled set — there is no enabled set in the signature, and this is what
    makes that absence deliberate rather than an oversight.

    Everything safe about the feature follows from it: the tagging diff compares
    against this set, so a file holding either spelling reports no change, so
    turning the transform on does not rewrite a library, does not mark it as
    having updates to take, and does not need the gardener to read a setting it
    has no access to.
    """
    accepted = transforms.accepted_album_titles(_release("expanded edition"))

    assert accepted == frozenset({"Test Album", "Test Album (expanded edition)"})


def test_a_release_with_no_disambiguation_accepts_its_one_title():
    """The half that makes the test above mean something: with nothing to append
    there is one legitimate spelling, and a bracketed suffix on such an album is
    a different title rather than the same one spelled Picard's way."""
    assert transforms.accepted_album_titles(_release()) == frozenset({"Test Album"})


def test_only_the_disambiguation_is_accepted_never_a_parenthetical():
    """Exact strings, never a pattern (review-gate item 2). The release states
    its disambiguation for free, so `(deluxe edition)` is a real difference."""
    accepted = transforms.accepted_album_titles(_release("expanded edition"))

    assert "Test Album (deluxe edition)" not in accepted


# ---------- the two halves, joined ----------


def test_the_tagset_carries_the_spelling_the_transform_chose():
    """Through `tagsets_for`, which is what the album page's MusicBrainz column
    is built from — so the page shows what a tagging would really write."""
    plain = tagger.tagsets_for(_release("expanded edition"), frozenset())
    transformed = tagger.tagsets_for(_release("expanded edition"), DISAMBIG)

    assert {t.album for t in plain} == {"Test Album"}
    assert {t.album for t in transformed} == {"Test Album (expanded edition)"}


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

    tagger.tag_album(album_dir, _release("expanded edition"), transforms=DISAMBIG)

    assert _album_tags(album_dir) == {"Test Album (expanded edition)"}


def test_a_tagging_without_the_transform_writes_musicbrainzs_title(tmp_path):
    album_dir = _album(tmp_path)

    tagger.tag_album(album_dir, _release("expanded edition"))

    assert _album_tags(album_dir) == {"Test Album"}


def test_turning_the_transform_on_does_not_make_an_existing_library_differ(tmp_path):
    """No churn, which is the whole reason this is affordable. A library tagged
    with MusicBrainz's plain title reports NO album change once the transform is
    on — so #266's write-skip still fires, #267's classifier sees nothing, and
    #32's nightly pass does not put the library in the Inbox on its first night.

    That is #283's failure mode in reverse, and it is the one this feature could
    plausibly have shipped.
    """
    album_dir = _album(tmp_path)
    release = _release("expanded edition")
    tagger.tag_album(album_dir, release)  # the plain title, as before

    plan = tagger.plan_album(album_dir, release, artwork=False, transforms=DISAMBIG)

    assert owned.Owned.ALBUM not in {f for c in plan.changes.values() for f in c}


def test_and_nor_does_turning_it_off(tmp_path):
    """The other direction, which is #283's own rule and must survive this
    change: a library carrying Picard's disambiguated title is not a library of
    differences to an install with the transform off."""
    album_dir = _album(tmp_path)
    release = _release("expanded edition")
    tagger.tag_album(album_dir, release, transforms=DISAMBIG)

    plan = tagger.plan_album(album_dir, release, artwork=False)

    assert owned.Owned.ALBUM not in {f for c in plan.changes.values() for f in c}


def test_a_title_that_is_neither_accepted_spelling_is_still_a_change(tmp_path):
    """What stops the two tests above from passing for the wrong reason. The
    tolerance is two exact strings, so a genuinely wrong album title is reported
    under either setting — a tolerance that swallowed this would be a tagger
    that had stopped comparing the field at all.
    """
    album_dir = _album(tmp_path)
    release = _release("expanded edition")
    tagger.tag_album(album_dir, release)
    for f in sorted(album_dir.iterdir()):
        formats_mod.write_owned(
            f, {**formats_mod.read_owned(f), owned.Owned.ALBUM.value: "Something Else"}
        )

    plan = tagger.plan_album(album_dir, release, artwork=False, transforms=DISAMBIG)

    assert owned.Owned.ALBUM in {f for c in plan.changes.values() for f in c}


@pytest.mark.parametrize("enabled", [frozenset(), DISAMBIG])
def test_a_release_with_no_disambiguation_is_untouched_either_way(tmp_path, enabled):
    """The transform is opt-in AND inert where the release says nothing, so the
    overwhelming majority of a library is unaffected by ticking the box."""
    album_dir = _album(tmp_path)

    tagger.tag_album(album_dir, _release(), transforms=enabled)

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
    tagger.tag_album(album_dir, release, transforms=DISAMBIG)
    before = {f: f.stat().st_mtime_ns for f in sorted(album_dir.iterdir())}

    tagger.tag_album(album_dir, release, transforms=DISAMBIG)

    assert {f: f.stat().st_mtime_ns for f in sorted(album_dir.iterdir())} == before
    assert _album_tags(album_dir) == {"Test Album (expanded edition)"}
