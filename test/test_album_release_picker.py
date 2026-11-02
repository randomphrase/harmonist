"""Changing a release keeps the album, its files and its review together."""

import json
from dataclasses import replace

import pytest
from bs4 import BeautifulSoup

from harmonist import album_files, formats, mb_cache, mb_lookup, scanner
from harmonist import sidecar as sc
from harmonist.models import AlbumState
from test import test_web
from test.test_identity_grouping import DISC1, DISC2, REL_A, _part

cfg = test_web.cfg
client = test_web.client


def test_rematch_stays_on_album_and_restores_all_folders(client, cfg):
    first = _part(cfg.paths.music_dir / "Disc One", track_ids=DISC1)
    second = _part(cfg.paths.music_dir / "Disc Two", track_ids=DISC2)
    before = {p: p.read_bytes() for p in album_files.for_paths([first, second])}
    response = client.post(
        f"/library/{REL_A}/rematch",
        data={"on_album_page": "true"},
        headers={"HX-Current-URL": f"http://testserver/album/{REL_A}?from_filter=incomplete"},
    )
    albums = scanner.scan(cfg.paths.music_dir)
    assert len(albums) == 1
    album = albums[0]
    assert album.state == AlbumState.NEEDS_MBID
    assert set(album.folders) == {first, second}
    assert response.headers["HX-Redirect"] == f"/album/{album.id}?from_filter=incomplete"
    assert sc.read(first).temp_uid == sc.read(second).temp_uid
    # Repeating the original request must not clear a later suggestion.
    candidate = test_web.MatchCandidate(
        mb_release_id=REL_A, confidence="exact", file_count=4, track_count=4
    )
    current = sc.read(first)
    assert current is not None
    sc.write(first, replace(current, mb_match_candidate=candidate))
    client.post(f"/library/{album.id}/rematch")
    assert sc.read(first).mb_match_candidate == candidate
    assert {p: p.read_bytes() for p in before} == before
    restored = client.post(f"/library/{album.id}/rematch/undo")
    assert "album-retagged" in json.loads(restored.headers["HX-Trigger"])
    assert len(scanner.scan(cfg.paths.music_dir)) == 1
    assert all(sc.read(p).mb_release_id == REL_A for p in (first, second))
    assert all(sc.read(p).mb_match_candidate is None for p in (first, second))
    assert {p: p.read_bytes() for p in before} == before


def test_siblings_use_shared_rows_and_one_browse(client, cfg, monkeypatch):
    root, release, *_ = test_web._confirmation_setup(cfg, monkeypatch, old_mbid=REL_A)
    current = sc.read(root)
    assert current is not None
    sc.write(root, replace(current, mb_release_id=REL_A, mb_match_candidate=None))
    client.post(f"/library/{REL_A}/rematch")
    aid = test_web._id_for(cfg, root)
    calls = []
    original = dict(release, id=REL_A, **{"release-group": {"id": "group"}})
    monkeypatch.setattr(mb_cache, "fetch_release", lambda mbid: original)

    def browse(group):
        calls.append(group)
        return [original, release], 2

    monkeypatch.setattr(mb_cache, "fetch_release_group_editions", browse)
    response = client.get(f"/manual/{aid}/siblings?on_album_page=true")
    assert response.status_code == 200
    assert calls == ["group"]
    soup = BeautifulSoup(response.text, "html.parser")
    assert len(soup.select('[role="listitem"]')) == 2
    assert (
        soup.select_one(f'#release-revert-{aid}[hx-swap-oob="innerHTML"] button').get_text()
        == "Revert to Previous"
    )
    page = client.get(f"/album/{aid}")
    assert f"/manual/{aid}/siblings" in page.text
    assert f"/manual/{aid}/search" in page.text
    assert f'hx-get="/review/{aid}"' in page.text


@pytest.mark.parametrize("missing_id", [False, True])
def test_manual_rematch_reviews_original_release_even_if_exact(
    client, cfg, monkeypatch, missing_id
):
    root, release, _, _, calls = test_web._confirmation_setup(cfg, monkeypatch, old_mbid=REL_A)
    release["id"] = REL_A
    current = sc.read(root)
    assert current is not None
    sc.write(root, replace(current, mb_release_id=REL_A, mb_match_candidate=None))
    client.post(f"/library/{REL_A}/rematch")
    aid = test_web._id_for(cfg, root)
    if missing_id:
        track = album_files.audio_files(root)[0]
        formats.write_owned(track, dict(formats.read_owned(track), mb_album_id=None))
    before = {p: p.read_bytes() for p in album_files.audio_files(root)}
    cleared = sc.read(root)
    # Choosing the album's own release again opens its review, however well it
    # fits, and writes nothing until that is confirmed (#933).
    review = client.get(
        f"/assignments/{aid}",
        params={"release": REL_A, "found_by": "release_group", "cancel": "true"},
    )
    assert review.status_code == 200
    assert "Confirm suggestion" in review.text
    assert sc.read(root) == cleared
    assert sc.read(root).mb_release_id is None
    assert {p: p.read_bytes() for p in before} == before
    # Reading it again uses the release the first read stored.
    lookups = list(calls)
    monkeypatch.setattr(
        mb_cache, "fetch_release", lambda *a, **k: pytest.fail("the review already read it")
    )
    again = client.get(
        f"/assignments/{aid}",
        params={"release": REL_A, "found_by": "release_group", "cancel": "true"},
    )
    assert again.status_code == 200
    assert calls == lookups


@pytest.mark.parametrize("gone", [False, True])
def test_sibling_failure_keeps_restore_search_and_paste(client, cfg, monkeypatch, gone):
    folder = _part(cfg.paths.music_dir / "Album", track_ids=DISC1)
    client.post(f"/library/{REL_A}/rematch")
    aid = test_web._id_for(cfg, folder)

    def fail(*args, **kwargs):
        raise mb_lookup.ReleaseGoneError("deleted") if gone else mb_lookup.MBError("offline")

    monkeypatch.setattr(mb_cache, "fetch_release", fail)
    response = client.get(f"/manual/{aid}/siblings?on_album_page=true")
    soup = BeautifulSoup(response.text, "html.parser")
    assert soup.select_one('[role="alert"]')
    assert soup.select_one(f'#release-revert-{aid} button[hx-post$="/rematch/undo"]')
    assert bool(soup.select_one('[hx-get*="reread=true"]')) is not gone
    assert ("HX-Trigger-After-Swap" in response.headers) is gone
    page = BeautifulSoup(client.get(f"/album/{aid}").text, "html.parser")
    assert page.select_one(f'form[hx-post="/manual/{aid}/search"]')
    assert page.select_one('input[name="release"][type="text"]')
    assert sc.read(folder).mb_release_id is None


@pytest.mark.parametrize("evidence", ["missing-id", "no-group"])
def test_unavailable_group_switches_to_search_without_retry(client, cfg, monkeypatch, evidence):
    folder = _part(cfg.paths.music_dir / "Album", track_ids=DISC1)
    client.post(f"/library/{REL_A}/rematch")
    aid = test_web._id_for(cfg, folder)
    if evidence == "missing-id":
        track = album_files.audio_files(folder)[0]
        formats.write_owned(track, dict(formats.read_owned(track), mb_album_id=None))
    calls = []

    def fetch(mbid):
        calls.append(mbid)
        release = test_web._release_for_match(mbid, n_tracks=2)
        release.pop("release-group", None)
        return release

    monkeypatch.setattr(mb_cache, "fetch_release", fetch)
    monkeypatch.setattr(
        mb_cache, "fetch_release_group_editions", lambda *a: pytest.fail("no group to browse")
    )
    before = {p: p.read_bytes() for p in album_files.audio_files(folder)}
    response = client.get(f"/manual/{aid}/siblings?on_album_page=true")
    assert response.status_code == 200
    assert json.loads(response.headers["HX-Trigger-After-Swap"]) == {
        "release-group-empty": {"target": f"#release-siblings-{aid}"}
    }
    soup = BeautifulSoup(response.text, "html.parser")
    assert soup.select_one('[role="alert"]')
    assert not soup.select_one('[hx-get*="reread=true"]')
    assert bool(soup.select_one('[hx-post$="/rematch/undo"]')) is (evidence == "no-group")
    assert calls == ([] if evidence == "missing-id" else [REL_A])
    assert {p: p.read_bytes() for p in before} == before
    assert sc.read(folder).mb_release_id is None


def test_multi_folder_confirmation_uses_reviewed_release_and_all_files(client, cfg, monkeypatch):
    first = _part(cfg.paths.music_dir / "One", track_ids=DISC1, disc=1, disc_total=2)
    second = _part(cfg.paths.music_dir / "Two", track_ids=DISC2, disc=2, disc_total=2)
    client.post(f"/library/{REL_A}/rematch")
    album = scanner.scan(cfg.paths.music_dir)[0]
    selected = test_web._release_for_match("new-release", n_tracks=4)
    selected["release-group"] = {"id": "another-group"}
    calls = []

    def fetch(mbid):
        calls.append(mbid)
        return selected

    monkeypatch.setattr(mb_lookup, "fetch_release", fetch)
    # A merged MBID is reviewed as the release MusicBrainz now has (#268).
    dialog = client.get(
        f"/review/{album.id}",
        params={"release": "merged-old-id", "found_by": "mbid", "on_album_page": "true"},
    )
    loader = BeautifulSoup(dialog.text, "html.parser").select_one("[hx-trigger='load']")
    assert loader is not None and "release=new-release" in loader["hx-get"]
    review = client.get(loader["hx-get"])
    fields = test_web._confirmation_fields(review.text)
    assert fields["candidate_mbid"] == "new-release"
    confirm = BeautifulSoup(review.text, "html.parser").select_one("button[hx-post*='/confirm/']")
    assert confirm is not None
    numbering = BeautifulSoup(review.text, "html.parser").select(
        '[aria-label^="Track numbering changes"]'
    )
    assert [n["aria-label"] for n in numbering] == [
        "Track numbering changes from 2.1 to 1.3",
        "Track numbering changes from 2.2 to 1.4",
    ]
    monkeypatch.setattr(
        mb_cache, "fetch_release", lambda *a, **k: pytest.fail("review must not fetch")
    )
    result = client.post(confirm["hx-post"], data=fields)
    assert result.headers["HX-Redirect"] == "/album/new-release"
    assert calls == ["merged-old-id"]
    after = scanner.scan(cfg.paths.music_dir)
    assert len(after) == 1 and after[0].track_count == 4
    assert set(after[0].folders) == {first, second}
    assert all(sc.read(p).mb_release_id == "new-release" for p in (first, second))
    assert all(sc.read(p).accepted_release_id == "new-release" for p in (first, second))
    assert all(
        formats.read_owned(p)["mb_album_id"] == "new-release"
        for p in album_files.for_paths([first, second])
    )


def test_restoring_one_duplicate_returns_to_its_own_page(client, cfg):
    first = _part(cfg.paths.music_dir / "First copy", track_ids=DISC1)
    other = _part(cfg.paths.music_dir / "Other copy", track_ids=DISC1)
    albums = scanner.scan(cfg.paths.music_dir)
    selected = next(a for a in albums if a.path == first)
    original = sc.read(other)
    client.post(f"/library/{selected.id}/rematch")
    assert client.get(f"/album/{selected.id}").status_code == 200
    aid = test_web._id_for(cfg, first)
    restored = client.post(f"/library/{aid}/rematch/undo")
    target = json.loads(restored.headers["HX-Trigger"])["album-retagged"]["album_id"]
    assert target == selected.id
    assert sc.read(other) == original
    assert client.get(f"/album/{target}").status_code == 200


def test_cancelling_multi_folder_choice_preserves_each_parts_provenance(client, cfg):
    first = _part(cfg.paths.music_dir / "First", track_ids=DISC1)
    second = _part(cfg.paths.music_dir / "Second", track_ids=DISC2)
    part = sc.read(second)
    assert part is not None
    sc.write(second, replace(part, notes="Only this part's note"))
    client.post(f"/library/{REL_A}/rematch")
    album = scanner.scan(cfg.paths.music_dir)[0]
    part = sc.read(first)
    assert part is not None
    sc.write(
        first,
        replace(
            part,
            mb_match_candidate=test_web.MatchCandidate(
                mb_release_id=REL_A, confidence="exact", file_count=4, track_count=4
            ),
        ),
    )
    response = client.post(f"/reject/{album.id}")
    assert response.status_code == 200
    assert sc.read(first).notes is None
    assert sc.read(second).notes == "Only this part's note"
    assert all(sc.read(p).mb_match_candidate is None for p in (first, second))
    albums = scanner.scan(cfg.paths.music_dir)
    assert len(albums) == 1 and albums[0].state == AlbumState.NEEDS_MBID


def test_replacement_barcode_search_waits_for_selection(client, cfg, monkeypatch):
    from test.test_barcode_discovery import album, release, services

    root, track = album(cfg.paths.music_dir)
    formats.write_owned(track, dict(formats.read_owned(track), mb_album_id=REL_A))
    sc.write(root, test_web.Sidecar())
    aid = test_web._id_for(cfg, root)
    search, fetch = services(monkeypatch, [release()])
    before = track.read_bytes()
    response = client.post(f"/manual/{aid}/barcode")
    assert response.status_code == 200
    assert sc.read(root).mb_match_candidate is None
    soup = BeautifulSoup(response.text, "html.parser")
    assert len(soup.select(f'button[hx-get^="/review/{aid}?release="]')) == 1
    assert search.call_count == 1 and fetch.call_count == 0
    assert track.read_bytes() == before
