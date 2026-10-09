"""Library changes, not just file changes, decide title updates (#722)."""

import shutil
from dataclasses import replace
from threading import Event
from unittest.mock import Mock

import pytest

from harmonist import (
    activity_store,
    formats,
    gardener,
    library_titles,
    mb_cache,
    mb_lookup,
    scanner,
    tagger,
)
from harmonist.formats.owned import Significance
from harmonist.models import Sidecar
from harmonist.transforms import TaggingChoices, TagTransform
from harmonist.web.scan_runner import ScanRunner
from test import test_web
from test.test_gardener import _release, _tagged

cfg = test_web.cfg
client = test_web.client
CONDITIONAL = TagTransform.ALBUM_DISAMBIGUATION_IF_NEEDED


def _edition(mbid, comment):
    return {**_release(mbid=mbid), "disambiguation": comment}


def _store(release):
    activity_store.store_release(release["id"], mb_cache._key(mb_lookup.RELEASE_INCLUDES), release)


def test_scan_add_remove_and_rematch_rechecks_unchanged_albums(client, cfg, monkeypatch):
    cfg.tagging.transforms = [CONDITIONAL]
    original = _edition("original", "original")
    reissue = _edition("reissue", "reissue")
    _store(original)
    _store(reissue)
    first = _tagged(cfg.paths.music_dir, original, name="Original")
    runner = client.app.state.scan_runner
    runner.refresh_now()
    gardener.warm_from_cache(runner.albums(), duty=0)
    assert not runner.albums()[0].update_available

    done = Event()
    recheck = gardener.recheck_for_settings

    def checked(albums, before, after):
        result = recheck(albums, before, after, duty=0)
        done.set()
        return result

    monkeypatch.setattr(gardener, "recheck_for_settings", checked)
    fetch = Mock(side_effect=AssertionError("a library change must not fetch"))
    monkeypatch.setattr(mb_lookup, "fetch_release", fetch)
    second = _tagged(cfg.paths.music_dir, reissue, name="Reissue")
    runner.refresh_now()
    assert done.wait(5)
    assert [a.update_significance for a in runner.albums()] == [Significance.SETTINGS] * 2
    for album, release in [(first, original), (second, reissue)]:
        tagger.tag_album(album.path, release, tagging=library_titles.choices(cfg.tagging.choices()))
    runner.refresh_now()
    gardener.warm_from_cache(runner.albums(), duty=0)
    assert not any(a.update_available for a in runner.albums())

    # Rematching the other release away affects the original's title too.
    done.clear()
    elsewhere = {**reissue, "id": "elsewhere", "release-group": {"id": "other-group"}}
    _store(elsewhere)
    tagger.tag_album(second.path, elsewhere)
    from harmonist import sidecar

    sidecar.write(second.path, Sidecar(mb_release_id="elsewhere"))
    runner.refresh_now()
    assert done.wait(5)
    assert (
        next(a for a in runner.albums() if a.path == first.path).update_significance
        == Significance.SETTINGS
    )

    # Returning it clears that removal update; removing it offers it again.
    done.clear()
    tagger.tag_album(
        second.path,
        reissue,
        tagging=TaggingChoices(transforms=frozenset({TagTransform.ALBUM_DISAMBIGUATION})),
    )
    sidecar.write(second.path, Sidecar(mb_release_id=reissue["id"]))
    runner.refresh_now()
    assert done.wait(5)
    assert not any(a.update_available for a in runner.albums())
    done.clear()
    shutil.rmtree(second.path)
    runner.refresh_now()
    assert done.wait(5)
    assert runner.albums()[0].update_significance == Significance.SETTINGS
    fetch.assert_not_called()


def test_membership_survives_cold_cache_and_excludes_only_this_copy(tmp_path):
    release = _edition("original", "original")
    first = _tagged(tmp_path, release, name="First")
    second = _tagged(tmp_path, release, name="Other encoding")
    runner = ScanRunner(tmp_path)
    runner.refresh_now()
    settings = TaggingChoices(transforms=frozenset({CONDITIONAL}))
    snapshot = library_titles.choices(settings, excluding=first.folders)
    assert snapshot.library_releases == frozenset({("original", "rg-aaa")})
    # Same release cannot disambiguate itself; a new release being matched can.
    assert tagger.tagsets_for(release, snapshot)[0].album == "Test Album"
    replacement = _edition("replacement", "reissue")
    assert tagger.tagsets_for(replacement, snapshot)[0].album == "Test Album (reissue)"
    shutil.rmtree(second.path)
    runner.refresh_now()
    assert not library_titles.choices(settings, excluding=first.folders).library_releases
    # A frozen preview does not change when the live inventory changes.
    assert tagger.tagsets_for(replacement, snapshot)[0].album == "Test Album (reissue)"


def test_cache_learning_and_group_corrections_recheck_published_albums(tmp_path):
    first = _tagged(tmp_path, _edition("original", "original"), name="First")
    second = _tagged(tmp_path, _edition("reissue", "reissue"), name="Second")
    changes = Mock()
    library_titles.configure(changes)
    # An adopted library may have release IDs without group tags.
    library_titles.reset_from(
        [replace(first, tagged_release_group=None), replace(second, tagged_release_group=None)]
    )
    library_titles.observe(_edition("original", "original"))
    changes.assert_not_called()
    library_titles.observe(_edition("reissue", "reissue"))
    assert changes.call_count == 1
    assert library_titles.choices(TaggingChoices()).library_releases == frozenset(
        {("original", "rg-aaa"), ("reissue", "rg-aaa")}
    )
    library_titles.observe(_edition("reissue", "reissue"))
    assert changes.call_count == 1
    library_titles.observe({**_edition("reissue", "reissue"), "release-group": {"id": "elsewhere"}})
    assert changes.call_count == 2


def test_adding_a_third_release_still_rechecks_the_group(tmp_path):
    first = _tagged(tmp_path, _edition("original", "original"), name="First")
    second = _tagged(tmp_path, _edition("reissue", "reissue"), name="Second")
    changes = Mock()
    library_titles.configure(changes)
    library_titles.reset_from([first, second])
    assert changes.call_count == 1
    third = _tagged(tmp_path, _edition("third", "expanded"), name="Third")
    library_titles.reset_from([first, second, third])
    assert changes.call_count == 2


def test_a_split_album_is_one_release_and_excludes_all_its_parts(tmp_path):
    release = _release(tracks=2)
    album = _tagged(tmp_path, release, tracks=2)
    part = album.path.parent / "Other disc folder"
    part.mkdir()
    shutil.move(str(next(album.path.glob("02*.m4a"))), part)
    from harmonist import sidecar

    sidecar.write(part, Sidecar(mb_release_id=release["id"]))
    albums = scanner.scan(tmp_path)
    assert len(albums) == 1
    assert len(albums[0].folders) == 2
    library_titles.reset_from(albums)
    assert library_titles.choices(TaggingChoices()).library_releases == frozenset(
        {(release["id"], "rg-aaa")}
    )
    assert not library_titles.choices(
        TaggingChoices(), excluding=albums[0].folders
    ).library_releases


def test_disambiguation_options_are_mutually_exclusive():
    from harmonist.config import TaggingConfig

    assert TaggingConfig(transforms=[CONDITIONAL]).choices().transforms == frozenset({CONDITIONAL})
    with pytest.raises(ValueError, match="either always or conditional"):
        TaggingConfig(transforms=[TagTransform.ALBUM_DISAMBIGUATION, CONDITIONAL])


@pytest.mark.parametrize("change", ["add", "remove"])
def test_library_change_invalidates_review_without_writing_or_fetching(
    client, cfg, monkeypatch, change
):
    cfg.tagging.transforms = [CONDITIONAL]
    root, release, *_rest, calls = test_web._confirmation_setup(cfg, monkeypatch, old_mbid=None)
    release["release-group"] = {"id": "rg-aaa"}
    release["disambiguation"] = "reissue"
    sibling = None
    if change == "remove":
        sibling = _tagged(cfg.paths.music_dir, _edition("original", "original"), name="Original")
    aid = test_web._id_for(cfg, root)
    review = client.get(f"/assignments/{aid}")
    fields = test_web._confirmation_fields(review.text)
    before = {p: p.read_bytes() for p in root.iterdir() if p.is_file()}
    if sibling:
        shutil.rmtree(sibling.path)
    else:
        _tagged(cfg.paths.music_dir, _edition("original", "original"), name="Original")
    calls.clear()
    response = client.post(f"/confirm/{aid}/accept", data=fields)
    assert "title choice changed" in response.text
    assert {p: p.read_bytes() for p in root.iterdir() if p.is_file()} == before
    assert calls == []


@pytest.mark.parametrize("sibling", [False, True])
def test_review_and_confirmation_write_the_same_conditional_title(
    client, cfg, monkeypatch, sibling
):
    cfg.tagging.transforms = [CONDITIONAL]
    root, release, *_rest, calls = test_web._confirmation_setup(cfg, monkeypatch)
    release["release-group"] = {"id": "rg-aaa"}
    release["disambiguation"] = "reissue"
    if sibling:
        _tagged(cfg.paths.music_dir, _edition("original", "original"), name="Original")
    aid = test_web._id_for(cfg, root)
    review = client.get(f"/assignments/{aid}")
    expected = release["title"] + (" (reissue)" if sibling else "")
    assert expected in review.text
    calls.clear()
    response = client.post(
        f"/confirm/{aid}/accept", data=test_web._confirmation_fields(review.text)
    )
    assert "confirmation-applied" in response.headers.get("HX-Trigger", "")
    assert {formats.read_album_title(p) for p in root.glob("*.m4a")} == {expected}
    assert calls == []


def test_scanner_requires_consistent_group_identity(tmp_path):
    release = _release(tracks=2)
    album = _tagged(tmp_path, release, tracks=2)
    assert album.tagged_release_group == (release["id"], "rg-aaa")
    file = next(album.path.glob("*.m4a"))
    formats.write_owned(file, {**formats.read_owned(file), "mb_release_group_id": "conflicting"})
    assert scanner.scan(tmp_path)[0].tagged_release_group is None
