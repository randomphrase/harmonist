"""What an album's History says about finding and confirming its release (#639).

A suggestion is not an event: it is rewritten by every lookup and changes
nothing about the album. The event is the user confirming one, and that record
names the release and says how it was found. Clearing a match with the "wrong
match" pencil is an event too, and gets an Undo like the others.
"""

from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import Mock

from mutagen.mp4 import MP4

from harmonist import activity_store, scanner, sidecar
from harmonist.activity_store import Source
from harmonist.models import AlbumState, FoundBy, MatchCandidate, Sidecar
from test import test_barcode_discovery as barcode_album
from test import test_web
from test.test_web import (
    ATOM_MB_ALBUM_ID,
    _confirmation_fields,
    _confirmation_setup,
    _id_for,
    _make_tagged_album,
)

cfg = test_web.cfg
client = test_web.client


def _activity(album_dir) -> list[str]:
    """This album's activity entries, newest first."""
    album_id = sidecar.album_id_for(album_dir)
    assert album_id is not None
    return [
        e.message for e in activity_store.album_history(album_id) if e.source == Source.ACTIVITY
    ]


# ---------- suggestions ----------


def test_a_barcode_suggestion_is_flashed_but_not_recorded(client, cfg, monkeypatch):
    path, _ = barcode_album.album(cfg.paths.music_dir)
    sidecar.write(path, Sidecar())
    aid = scanner.scan(cfg.paths.music_dir)[0].id
    barcode_album.services(monkeypatch, [barcode_album.release()])

    response = client.post(f"/manual/{aid}/barcode")

    assert "Barcode match found" in response.headers["HX-Trigger"]
    candidate = sidecar.read(path).mb_match_candidate
    assert candidate is not None and candidate.found_by is FoundBy.BARCODE
    assert not any("match found" in m for m in _activity(path))


def test_a_store_url_suggestion_is_flashed_but_not_recorded(client, cfg, monkeypatch):
    path, _ = barcode_album.album(cfg.paths.music_dir)
    sidecar.write(path, Sidecar(store_url="https://lfo.bandcamp.com/album/frequencies"))
    aid = scanner.scan(cfg.paths.music_dir)[0].id
    barcode_album.services(monkeypatch, [barcode_album.release()])
    lookup = Mock(return_value=([{"id": barcode_album.MBID}], 1))
    monkeypatch.setattr("harmonist.mb_lookup.candidate_summaries_for_url", lookup)

    response = client.post(f"/manual/{aid}/candidates", data={"suggest": "true"})

    assert "Store URL match found" in response.headers["HX-Trigger"]
    candidate = sidecar.read(path).mb_match_candidate
    assert candidate is not None and candidate.found_by is FoundBy.STORE_URL
    assert not any("match found" in m for m in _activity(path))


def test_a_release_picked_from_a_name_search_remembers_how(client, cfg, monkeypatch):
    """A picker's Use carries how its rows were found, so the suggestion it
    stashes can say so at confirm — the one route where the lookup and the
    choice are separate requests."""
    path, _ = barcode_album.album(cfg.paths.music_dir)
    sidecar.write(path, Sidecar())
    aid = scanner.scan(cfg.paths.music_dir)[0].id
    barcode_album.services(monkeypatch, [barcode_album.release()])
    monkeypatch.setattr(
        "harmonist.mb_search.search_releases",
        lambda artist, title, limit=10: [
            {"id": barcode_album.MBID, "title": "Frequencies", "artist": "LFO"}
        ],
    )

    results = client.post(f"/manual/{aid}/search", data={"artist": "LFO", "title": "Frequencies"})
    use = _confirmation_fields(results.text)
    assert use["found_by"] == FoundBy.NAME_SEARCH
    client.post(f"/manual/{aid}/assign", data=use | {"review_only": "true"})

    candidate = sidecar.read(path).mb_match_candidate
    assert candidate is not None and candidate.found_by is FoundBy.NAME_SEARCH


def test_found_by_survives_the_sidecar(tmp_path):
    """The only place the fact outlives the lookup: a candidate read back from
    disk still says how it was found, so the confirm can."""
    candidate = MatchCandidate(
        mb_release_id="rel-x",
        confidence="exact",
        file_count=1,
        track_count=1,
        found_by=FoundBy.BARCODE,
    )
    sidecar.write(tmp_path, Sidecar(mb_match_candidate=candidate))
    loaded = sidecar.read(tmp_path)
    assert loaded is not None and loaded.mb_match_candidate is not None
    assert loaded.mb_match_candidate.found_by is FoundBy.BARCODE


# ---------- confirming ----------


def _confirm(client, cfg, root) -> None:
    aid = _id_for(cfg, root)
    fields = _confirmation_fields(client.get(f"/assignments/{aid}").text)
    response = client.post(f"/confirm/{aid}", data=fields)
    assert "confirmation-applied" in response.headers.get("HX-Trigger", "")


def test_confirming_records_the_release_and_how_it_was_found(client, cfg, monkeypatch):
    root, release, *_ = _confirmation_setup(cfg, monkeypatch, old_mbid=None)
    release["disambiguation"] = "24bits"
    current = sidecar.read(root)
    assert current is not None and current.mb_match_candidate is not None
    sidecar.write(
        root,
        replace(
            current,
            mb_match_candidate=replace(current.mb_match_candidate, found_by=FoundBy.STORE_URL),
        ),
    )

    _confirm(client, cfg, root)

    latest = _activity(root)[0]
    assert latest.startswith("Matched — Title (24bits), found by store URL")
    # The files carried no release before, so the tags did change.
    assert "tags already matched" not in latest


def test_confirming_the_release_the_files_already_carry_says_so(client, cfg, monkeypatch):
    """The re-match that ends where it started: nothing about the tags changed,
    and a bare "Tagged" read as though that half had gone unrecorded."""
    root, *_ = _confirmation_setup(cfg, monkeypatch, old_mbid="rel-new-confirm")

    _confirm(client, cfg, root)

    latest = _activity(root)[0]
    assert latest.startswith("Matched — Title")
    assert "tags already matched" in latest
    # A suggestion written before `found_by` existed says nothing it can't know.
    assert "found by" not in latest


# ---------- undoing the pencil ----------


def _cleared(client, cfg, mbid="rel-kept"):
    d = _make_tagged_album(cfg, "Cleared", mbid=mbid, tagged_at=datetime.now(UTC), item_id=7)
    client.post(f"/library/{_id_for(cfg, d)}/rematch")
    return d


def test_a_cleared_match_can_be_undone_from_history(client, cfg):
    d = _cleared(client, cfg)
    aid = _id_for(cfg, d)
    assert f'hx-post="/library/{aid}/rematch/undo"' in client.get(f"/album/{aid}").text

    response = client.post(f"/library/{aid}/rematch/undo")

    assert "album-retagged" in response.headers["HX-Trigger"]
    loaded = sidecar.read(d)
    assert loaded is not None
    assert loaded.mb_release_id == "rel-kept"  # the release the files still carry
    assert loaded.tagged_at is not None
    assert loaded.mb_match_candidate is None
    assert loaded.bandcamp is not None and loaded.bandcamp.item_id == 7
    album = next(a for a in scanner.scan(cfg.paths.music_dir) if a.path == d)
    assert album.state == AlbumState.COMPLETE
    assert _activity(d)[0].startswith("MB match restored")
    # Relinked, so there is nothing left to undo and the button goes.
    assert "/rematch/undo" not in client.get(f"/album/{album.id}").text


def test_undoing_a_cleared_match_twice_changes_nothing(client, cfg):
    d = _cleared(client, cfg)
    aid = _id_for(cfg, d)
    client.post(f"/library/{aid}/rematch/undo")
    before = (d / ".harmonist.json").read_bytes()
    entries = len(_activity(d))

    response = client.post(f"/library/{sidecar.album_id_for(d)}/rematch/undo")

    assert response.status_code == 200
    assert (d / ".harmonist.json").read_bytes() == before
    assert len(_activity(d)) == entries


def test_no_undo_when_the_files_no_longer_name_one_release(client, cfg):
    """The files are the evidence: the pencil leaves them alone, so they still
    name the cleared release — unless something changed them since, in which
    case there is nothing to restore from and no button to offer."""
    d = _cleared(client, cfg)
    for track in sorted(d.glob("*.m4a")):
        audio = MP4(track)
        del audio[ATOM_MB_ALBUM_ID]
        audio.save()
    aid = _id_for(cfg, d)

    assert "/rematch/undo" not in client.get(f"/album/{aid}").text
    client.post(f"/library/{aid}/rematch/undo")
    loaded = sidecar.read(d)
    assert loaded is not None and loaded.mb_release_id is None
