"""Tests for the update-available detector (#287).

The verdict is *"would a re-tag change an owned tag?"*, derived from the files on
disk against the release MusicBrainz last gave us. These pin the four answers
that are easy to get subtly wrong — no update, an update, an update that was
taken back upstream, and a structural change — plus the two properties the
feature rests on: the warm-up costs no MusicBrainz requests, and a flag survives
a rescan of an album nothing touched.
"""

from __future__ import annotations

import asyncio
import copy
import re
import shutil
import threading
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from harmonist import activity_store, gardener, mb_cache, mb_lookup, scanner, tagger
from harmonist import sidecar as sc
from harmonist.models import Album, Sidecar
from harmonist.web.scan_runner import ScanRunner

SINE_M4A = Path(__file__).parent / "fixtures" / "sine.m4a"


def _release(title: str = "Test Album", *, tracks: int = 1, mbid: str = "rel-aaa") -> dict:
    """A minimal release the tagger can write from, in musicbrainzngs' shape."""
    return {
        "id": mbid,
        "title": title,
        "status": "Official",
        "artist-credit": [
            {"artist": {"id": "art-aaa", "name": "Test Artist", "sort-name": "Artist, Test"}}
        ],
        "release-group": {"id": "rg-aaa", "primary-type": "Album"},
        "medium-list": [
            {
                "position": "1",
                "format": "Digital Media",
                "track-list": [
                    {
                        "id": f"rt-{i:03d}",
                        "position": str(i),
                        "title": f"Track {i}",
                        "recording": {"id": f"rec-{i:03d}", "title": f"Track {i}"},
                    }
                    for i in range(1, tracks + 1)
                ],
            }
        ],
    }


def _album_dir(root: Path, *, tracks: int = 1, name: str = "Test Album") -> Path:
    d = root / "Test Artist" / name
    d.mkdir(parents=True)
    for i in range(1, tracks + 1):
        shutil.copy(SINE_M4A, d / f"{i:02d} Track {i}.m4a")
    return d


def _flag(album: Album, release: dict) -> bool:
    """Look at `album` against `release` and report the verdict.

    Asserts go through the flag rather than `refresh_flag`'s return value on
    purpose: the return is the *plan*, which is None both for an album that
    could not be read and for one whose tracklist no longer fits — two opposite
    verdicts. `update_available` is the single answer, and it is what the
    Library reads.
    """
    gardener.refresh_flag(album, release)
    return album.update_available


def _tagged(root: Path, release: dict, *, tracks: int = 1, name: str = "Test Album") -> Album:
    """An album tagged from `release`, as the scanner sees it afterwards."""
    d = _album_dir(root, tracks=tracks, name=name)
    tagger.tag_album(d, release)
    sc.write(d, Sidecar(mb_release_id=release["id"], tagged_at=datetime.now(UTC)))
    return next(a for a in scanner.scan(root) if a.path == d)


def test_an_album_carrying_what_musicbrainz_says_has_no_update(tmp_path):
    """The baseline the whole filter depends on. If a freshly tagged album read
    as having an update, every album in the library would, and the filter would
    be a list of everything."""
    release = _release()
    album = _tagged(tmp_path, release)

    assert _flag(album, release) is False


def test_an_edit_upstream_is_an_update(tmp_path):
    """The case the feature exists for: MusicBrainz moved, the files didn't."""
    album = _tagged(tmp_path, _release())

    assert _flag(album, _release("Test Album (remastered)")) is True


def test_an_edit_that_was_taken_back_upstream_leaves_nothing_outstanding(tmp_path):
    """A → B → A. The flag is derived from disk-vs-MusicBrainz, so a reverted
    edit clears itself; a verdict taken from "the payload changed since we last
    looked" would flag this album and keep flagging it, because something did
    change every time we asked."""
    original = _release()
    album = _tagged(tmp_path, original)

    assert _flag(album, _release("Test Album (remastered)")) is True
    assert _flag(album, original) is False
    assert album.update_available is False


def test_the_release_growing_a_track_is_an_update_not_an_error(tmp_path):
    """`plan_album` refuses an album whose file count no longer matches the
    tracklist. That refusal IS the finding — a structural change is the loudest
    thing MusicBrainz can do to an album — and treating it as a failure would
    hide exactly the case the user most needs to see."""
    album = _tagged(tmp_path, _release())

    assert _flag(album, _release(tracks=2)) is True


def test_an_unreadable_file_leaves_the_flag_as_it_was(tmp_path):
    """ "I could not tell" is not "nothing to take". A network mount blinking
    must not quietly empty the filter — the previous answer stands until
    something can read the files again."""
    release = _release()
    album = _tagged(tmp_path, release)
    gardener.refresh_flag(album, _release("Test Album (remastered)"))
    assert album.update_available is True

    (album.path / "01 Track 1.m4a").write_bytes(b"not an m4a at all")

    assert _flag(album, release) is True  # unchanged, not cleared


def test_the_warm_up_rebuilds_flags_without_asking_musicbrainz(tmp_path, monkeypatch):
    """The restart story. Both inputs to the verdict outlive the process — the
    payload in `mb_release_cache`, the tags on disk — so the flags come back for
    free. Any live fetch here would be a rate-limited request spent per album on
    something a restart triggers.

    The stored row is deliberately **stale**, because that is the state a
    restart actually finds it in: the TTL is an hour and the process has been
    down all night. A warm-up built on `fetch_release` would look correct
    against a fresh row and go to the network for every album in the field.
    """
    activity_store.init(tmp_path / "activity.db")
    album = _tagged(tmp_path, _release())
    # What MusicBrainz last told us, stored as `fetch_release` would have.
    activity_store.store_release(
        "rel-aaa",
        "+".join(sorted(mb_lookup.RELEASE_INCLUDES)),
        _release("Test Album (remastered)"),
    )
    conn = activity_store._ensure()
    conn.execute(
        "UPDATE mb_release_cache SET fetched_at = ?",
        ((datetime.now(UTC) - timedelta(days=7)).isoformat(),),
    )
    conn.commit()

    def _no_requests(*a: object, **k: object) -> dict:
        raise AssertionError("the warm-up fetched from MusicBrainz")

    monkeypatch.setattr(mb_lookup, "fetch_release", _no_requests)

    assert gardener.warm_from_cache([album], duty=0) == 1
    assert album.update_available is True


def test_a_second_warm_up_changes_nothing(tmp_path, monkeypatch):
    """Idempotency, which under a background pass is what stops a nightly job
    becoming a nightly stream of noise. Running it twice reaches the same verdict
    and adds nothing — no second flag, no feed entry, no write of any kind, since
    the whole operation is a read and an in-memory assignment."""
    activity_store.init(tmp_path / "activity.db")
    album = _tagged(tmp_path, _release())
    activity_store.store_release(
        "rel-aaa",
        "+".join(sorted(mb_lookup.RELEASE_INCLUDES)),
        _release("Test Album (remastered)"),
    )
    monkeypatch.setattr(
        mb_lookup, "fetch_release", lambda *a, **k: pytest.fail("warm-up went to MusicBrainz")
    )

    first = gardener.warm_from_cache([album], duty=0)
    events_after_first = len(activity_store.recent(50))

    assert gardener.warm_from_cache([album], duty=0) == first == 1
    assert album.update_available is True
    assert len(activity_store.recent(50)) == events_after_first


def test_the_warm_up_leaves_an_album_musicbrainz_was_never_asked_about(tmp_path):
    """No stored payload means no baseline, which is not the same as "no update".
    The album stays unflagged rather than being guessed at either way."""
    activity_store.init(tmp_path / "activity.db")
    album = _tagged(tmp_path, _release())

    assert gardener.warm_from_cache([album], duty=0) == 0
    assert album.update_available is False


def test_the_warm_up_skips_an_album_with_no_release_of_its_own(tmp_path):
    """An unlinked album has nothing to compare against — and `stored_release`
    must not be handed a None mbid to look up."""
    activity_store.init(tmp_path / "activity.db")
    d = _album_dir(tmp_path)
    album = next(a for a in scanner.scan(tmp_path) if a.path == d)
    assert album.sidecar is None

    assert gardener.warm_from_cache([album], duty=0) == 0


async def _scan_via_runner(runner: ScanRunner) -> None:
    runner.attach_loop()
    for _ in range(300):
        if runner.has_completed():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("background scan never completed")


def test_the_flag_survives_a_rescan_of_an_untouched_album(tmp_path):
    """The flag lives on the Album the snapshot holds, and a rescan rebuilds that
    snapshot — so this is what makes it usable at all rather than something that
    evaporates on the next hourly sweep. `resolve_dir` returns the cached Album
    on a signature hit and `merge_by_identity` passes a single-directory album
    straight through, so an album nothing touched is the same object with its
    flag intact.
    """
    _tagged(tmp_path, _release())
    runner = ScanRunner(tmp_path)
    asyncio.run(_scan_via_runner(runner))
    runner.albums()[0].update_available = True

    rescanned = runner.scan_now()  # the same walk the hourly rescan makes

    assert rescanned[0].update_available is True


def test_a_retag_clears_the_flag_by_rebuilding_the_album(tmp_path):
    """The other half of that: an album whose files changed is rebuilt from
    scratch, so taking the update drops the flag with no bookkeeping. This is why
    nothing has to remember to clear it."""
    release = _release()
    album_dir = _tagged(tmp_path, release).path
    runner = ScanRunner(tmp_path)
    asyncio.run(_scan_via_runner(runner))
    runner.albums()[0].update_available = True

    tagger.tag_album(album_dir, _release("Test Album (remastered)"))

    assert runner.scan_now()[0].update_available is False


# ---------------------------------------------------------------------------
# Through the web layer (#287's actual errand)
# ---------------------------------------------------------------------------
#
# These need the ScanRunner engaged, and that is not incidental: the flag lives
# on the Album the snapshot holds, so a route can only record it somewhere
# durable when there IS a snapshot. Without the runner every request rescans
# and builds fresh Albums, which is what `_albums` does in the plain `client`
# fixture — a flag set by one request would be gone by the next, and a test
# there would be asserting about a configuration production never runs in.


@pytest.fixture
def engaged(tmp_path):
    """A TestClient whose app reads a persistent library snapshot, as production
    does. Returns `(client, runner)`."""
    from harmonist.config import BandcampConfig, Config, PathsConfig, ServerConfig, TestConfig

    music = tmp_path / "music"
    music.mkdir(parents=True)
    (tmp_path / "config").mkdir(parents=True)
    cfg = Config(
        paths=PathsConfig(config_dir=tmp_path / "config", music_dir=music),
        bandcamp=BandcampConfig(),
        server=ServerConfig(),
        test=TestConfig(mode="fixture"),
    )
    activity_store.init(tmp_path / "activity.db")
    yield cfg, lambda: _engage(cfg)


def _engage(cfg):
    from fastapi.testclient import TestClient

    from harmonist.web.main import create_app

    app = create_app(cfg)
    runner: ScanRunner = app.state.scan_runner
    asyncio.run(_scan_via_runner(runner))
    # HX-Request for the CSRF middleware, exactly as the other web fixtures do.
    return TestClient(app, headers={"HX-Request": "true"}), runner


def test_opening_an_album_records_that_it_has_an_update(engaged, monkeypatch):
    """The whole of this increment's usefulness before the background pass
    exists (#270): the comparison the album page already runs answers the
    filter's question too, so browsing fills the filter in."""
    cfg, engage = engaged
    _tagged(cfg.paths.music_dir, _release())
    monkeypatch.setattr(
        mb_lookup, "fetch_release", lambda *a, **k: _release("Test Album (remastered)")
    )
    client, runner = engage()
    assert runner.albums()[0].update_available is False

    r = client.get(f"/library/{runner.albums()[0].id}/compare")

    assert r.status_code == 200
    assert runner.albums()[0].update_available is True


def test_the_library_filter_narrows_to_albums_with_an_update(engaged, monkeypatch):
    """And the filter is what makes that reachable without opening fifty pages."""
    cfg, engage = engaged
    _tagged(cfg.paths.music_dir, _release())
    # A second album whose TITLE differs, because a tile renders the album title
    # from the tags — two fixtures tagged from the same release are indis-
    # tinguishable in the HTML, and the "not in" half of this test would then
    # hold no matter what the filter did.
    other = _release("Untouched Album") | {"id": "rel-bbb"}
    d = cfg.paths.music_dir / "Test Artist" / "Untouched"
    d.mkdir(parents=True)
    shutil.copy(SINE_M4A, d / "01 Track 1.m4a")
    tagger.tag_album(d, other)
    sc.write(d, Sidecar(mb_release_id="rel-bbb", tagged_at=datetime.now(UTC)))
    client, runner = engage()
    next(a for a in runner.albums() if a.title == "Test Album").update_available = True

    body = client.get("/library?filter=update-available").text

    assert "Test Album" in body
    assert "Untouched Album" not in body


def test_a_flagged_album_says_so_on_its_tile(engaged):
    """The filter gathers them; the badge means you meet one while browsing
    rather than only when you go looking (#293).

    The absence half is asserted because a live path produces it: the very same
    template renders the badge for the album beside this one, so this is "the
    badge is conditional", not "a string is missing from the page"."""
    cfg, engage = engaged
    _tagged(cfg.paths.music_dir, _release())
    other = _release("Untouched Album") | {"id": "rel-bbb"}
    d = cfg.paths.music_dir / "Test Artist" / "Untouched"
    d.mkdir(parents=True)
    shutil.copy(SINE_M4A, d / "01 Track 1.m4a")
    tagger.tag_album(d, other)
    sc.write(d, Sidecar(mb_release_id="rel-bbb", tagged_at=datetime.now(UTC)))
    client, runner = engage()
    flagged = next(a for a in runner.albums() if a.title == "Test Album")
    flagged.update_available = True

    body = client.get("/library").text

    assert "Update" in _tile_for(body, "Test Album")
    assert "bg-mb-purple-soft" in _tile_for(body, "Test Album")
    assert "bg-mb-purple-soft" not in _tile_for(body, "Untouched Album")


def _tile_for(body: str, title: str) -> str:
    """The one Library tile for `title`, as HTML.

    Asserting against the whole page would let "Update" match the filter chip
    or anything else on it, so the badge would read as present on every album —
    including the one it must not be on, which is the half of the test with
    something to prove.
    """
    tiles = body.split('<a id="lib-')[1:]
    return next(t for t in tiles if f">{title}</div>" in t)


def test_the_filter_chip_is_dead_until_something_has_an_update(engaged):
    """A chip worth 0 says so before it is picked, rather than answering with an
    empty grid — the same promise the other three filters make, and the reason
    the count is computed on every render. So the chip is a disabled span with
    no link until an album is actually flagged, and a link the moment one is."""
    cfg, engage = engaged
    _tagged(cfg.paths.music_dir, _release())
    client, runner = engage()

    body = client.get("/library").text
    assert "Update available" in body  # offered
    assert "filter=update-available" not in body  # but not selectable

    runner.albums()[0].update_available = True

    assert "filter=update-available" in client.get("/library").text


@pytest.mark.parametrize("mbid", ["rel-aaa"])
def test_stored_release_reads_the_store_and_never_the_network(tmp_path, monkeypatch, mbid):
    """`stored_release` is the warm-up's whole budget guarantee, so it is worth
    pinning apart from its caller: a miss returns None rather than fetching."""
    activity_store.init(tmp_path / "activity.db")
    monkeypatch.setattr(
        mb_lookup,
        "fetch_release",
        lambda *a, **k: pytest.fail("stored_release went to MusicBrainz"),
    )

    assert mb_cache.stored_release(mbid) is None

    stored = _release()
    activity_store.store_release(mbid, "+".join(sorted(mb_lookup.RELEASE_INCLUDES)), stored)

    assert mb_cache.stored_release(mbid) == copy.deepcopy(stored)


def test_a_picard_tagged_album_does_not_flag_on_the_release_type(tmp_path):
    """Picard writes `MusicBrainz Album Type` lowercase, as it does the status.
    Harmonist wrote the status the same way and the type verbatim from
    MusicBrainz, so every Picard-tagged album in an adopted library differed on
    that one field forever — ~90% of a real library, and `mb_album_type` is
    classified Identity, so the gardener would have sent all of it to the Inbox
    on its first night (#290).
    """
    from mutagen.mp4 import MP4

    from harmonist.tagger import ATOM_MB_ALBUM_TYPE

    release = _release()
    album = _tagged(tmp_path, release)
    # What Picard leaves on disk, which is what an adopted library carries.
    f = MP4(album.path / "01 Track 1.m4a")
    f[ATOM_MB_ALBUM_TYPE] = [b"album"]
    f.save()

    assert _flag(album, release) is False


# ---------------------------------------------------------------------------
# Explaining the flag (#291)
# ---------------------------------------------------------------------------


def test_the_plan_renders_the_same_rows_as_a_history_entry(tmp_path):
    """One renderer, two questions — *what did that tagging change?* and *what
    would this one change?* — so an update waiting on an album reads exactly like
    the History entry it becomes. Two views of one plan would drift, and the one
    nobody was looking at would be the one that went wrong."""
    from harmonist import tag_history

    album = _tagged(tmp_path, _release())
    plan = gardener.refresh_flag(album, _release("Test Album (remastered)"))
    assert plan is not None

    rows = tag_history.from_plan(plan, album.path, sorted(album.path.glob("*.m4a")))

    assert [(r.label, r.before, r.after) for r in rows] == [
        ("Album", "Test Album", "Test Album (remastered)")
    ]


def test_reach_counts_every_file_not_just_the_changed_ones(tmp_path):
    """`summarise` takes its total from the record count, so handing it only the
    files the plan touches would report "all tracks" for a field that moved on
    one of three."""
    from mutagen.mp4 import MP4

    from harmonist import tag_history
    from harmonist.tagger import ATOM_TITLE

    release = _release(tracks=3)
    album = _tagged(tmp_path, release, tracks=3)
    # One track's title drifts; the other two already match MusicBrainz.
    f = MP4(album.path / "02 Track 2.m4a")
    f[ATOM_TITLE] = ["Trak 2"]
    f.save()

    plan = gardener.refresh_flag(album, release)
    assert plan is not None
    rows = tag_history.from_plan(plan, album.path, sorted(album.path.glob("*.m4a")))

    assert [(r.label, r.reach) for r in rows] == [("Title", "1 of 3 tracks")]


def test_the_album_page_explains_an_update_the_comparison_cannot_show(engaged, monkeypatch):
    """The bug this closes: a re-tag can change tags the page has no other place
    for — so without a surface for them the album sits under Update available and
    reads as matching on every row shown.

    An ISRC MusicBrainz has filled in is the everyday one, and it is still the
    box's after #309: MusicBrainz filled the SAME one in for the whole album, so
    every track reads the same way and "which track" has no answer to give. A
    column would print one position three times over; the box's single line is
    the whole fact. `test_a_per_track_change_lands_on_its_own_row` is the other
    half of that decision, and the two are only worth reading together.
    """
    cfg, engage = engaged
    _tagged(cfg.paths.music_dir, _release(tracks=3), tracks=3)
    filled_in = copy.deepcopy(_release(tracks=3))
    for track in filled_in["medium-list"][0]["track-list"]:
        track["recording"]["isrc-list"] = ["GBAYE0000123"]
    monkeypatch.setattr(mb_lookup, "fetch_release", lambda *a, **k: filled_in)
    client, runner = engage()

    body = client.get(f"/library/{runner.albums()[0].id}/compare").text

    assert runner.albums()[0].update_available is True
    assert "Other tags a re-tag would change" in body
    assert "<dt>ISRC</dt>" in body
    assert not re.search(r"<th [^>]*>\s*ISRC\s*</th>", body), "no column: it says the same thing"


def test_a_per_track_change_lands_on_its_own_row(engaged, monkeypatch):
    """#309's own complaint, end to end.

    The same field as above, differing DIFFERENTLY per track — which is where the
    box's aggregate stopped being enough: "3 different values → 3 different
    values, all tracks" is true and tells the reader nothing. A column puts each
    one beside the track it belongs to, and the box then has nothing to add.
    """
    cfg, engage = engaged
    _tagged(cfg.paths.music_dir, _release(tracks=3), tracks=3)
    filled_in = copy.deepcopy(_release(tracks=3))
    for i, track in enumerate(filled_in["medium-list"][0]["track-list"], start=1):
        track["recording"]["isrc-list"] = [f"GBAYE000012{i}"]
    monkeypatch.setattr(mb_lookup, "fetch_release", lambda *a, **k: filled_in)
    client, runner = engage()

    body = client.get(f"/library/{runner.albums()[0].id}/compare").text

    assert re.search(r"<th [^>]*>\s*ISRC\s*</th>", body)
    # Each on its own row, and each exactly once — not in a column AND the box.
    for i in (1, 2, 3):
        assert body.count(f"GBAYE000012{i}") == 1
    assert "<dt>ISRC</dt>" not in body


def test_identifiers_are_rendered_short_and_start_hidden(engaged, monkeypatch):
    """#319, at the rung that can see it.

    `compare` decides a column is an identifier; whether that actually reaches
    the markup as a hidden column, a named control and a trimmed id is the
    template's business, and only a rendered response shows it.

    The cap is deliberately not asserted here. Since #319 the identifiers are
    exempt from it, and within one medium the readable per-track tags are title
    (pinned), artist, artist sort and artists — three earnable, which IS the cap.
    A single-disc album can no longer exceed it, so the overflow property lives
    in `test_compare`, where a multi-disc release can be built freely.
    """
    cfg, engage = engaged
    _tagged(cfg.paths.music_dir, _release(tracks=3), tracks=3)
    enriched = copy.deepcopy(_release(tracks=3))
    for medium in enriched["medium-list"]:
        for i, track in enumerate(medium["track-list"], start=1):
            track["recording"]["isrc-list"] = [f"GBAYE000012{i}"]
            track["recording"]["id"] = f"rec-moved-{i}"
            track["id"] = f"rt-moved-{i}"
            track["artist-credit"] = [
                {"artist": {"id": f"art-moved-{i}", "name": f"Someone Else {i}"}}
            ]
    monkeypatch.setattr(mb_lookup, "fetch_release", lambda *a, **k: enriched)
    client, runner = engage()

    body = client.get(f"/library/{runner.albums()[0].id}/compare").text

    # On the page, in columns of their own — and every cell of them carrying the
    # class that hides them until asked for.
    assert re.search(r"<th [^>]*track-diff__id[^>]*>\s*Recording\s*</th>", body)
    assert re.search(r"<td [^>]*track-diff__id[^>]*>", body)
    # The control that reveals them, naming what is behind it rather than
    # counting it — a hidden column must not read as one nobody checked (#112).
    assert "Show identifiers" in body
    assert "ISRC, Artist IDs, Recording and Release track differ here." in body
    # Trimmed to eight characters, with the whole id still in the link and the
    # tooltip. `rec-moved-1` is 11 characters, so a full render would show it.
    assert "rec-move…" in body
    assert 'href="https://musicbrainz.org/recording/rec-moved-1"' in body
    assert 'title="rec-moved-1"' in body


def test_an_album_scoped_update_is_not_stated_twice(engaged, monkeypatch):
    """#297. The box was written when the Tags panel compared nine album fields
    out of the thirty a plan covers. #295 widened the panel to all of them, so a
    single album-level difference now renders in the comparison AND again in the
    box directly beneath it — the same row, twice, on a page where one field
    differs.

    The comparison keeps it; the box drops out entirely rather than showing an
    empty heading."""
    cfg, engage = engaged
    _tagged(cfg.paths.music_dir, _release())
    catalogued = copy.deepcopy(_release())
    catalogued["label-info-list"] = [
        {"label": {"name": "Warp"}, "catalog-number": "WARPCD-999"},
    ]
    monkeypatch.setattr(mb_lookup, "fetch_release", lambda *a, **k: catalogued)
    client, runner = engage()

    body = client.get(f"/library/{runner.albums()[0].id}/compare").text

    # The comparison still reports it — this is a real difference, not a
    # suppressed one, and the flag it sets stays explained.
    assert runner.albums()[0].update_available is True
    assert body.count("Cat. no.") == 1
    assert body.count("WARPCD-999") == 1
    assert "Other tags a re-tag would change" not in body


def test_an_album_with_nothing_waiting_says_nothing(engaged, monkeypatch):
    """No empty heading on an album that is up to date — the section has to be
    absent, not present-and-blank, or every album grows a box telling the reader
    there is nothing to tell them."""
    cfg, engage = engaged
    _tagged(cfg.paths.music_dir, _release())
    monkeypatch.setattr(mb_lookup, "fetch_release", lambda *a, **k: _release())
    client, runner = engage()

    body = client.get(f"/library/{runner.albums()[0].id}/compare").text

    assert "Other tags a re-tag would change" not in body


# ---------------------------------------------------------------------------
# Pacing and audibility (#299)
# ---------------------------------------------------------------------------


def test_the_warm_up_says_it_started_and_says_it_finished(tmp_path, caplog):
    """It shipped logging one line, at the end. On a NAS that made a pass taking
    minutes indistinguishable from no pass at all — and from a hang, which is
    exactly the ambiguity that left #299 with two competing explanations and no
    way to choose between them."""
    import logging

    activity_store.init(tmp_path / "activity.db")
    album = _tagged(tmp_path, _release())

    with caplog.at_level(logging.INFO, logger="harmonist.gardener"):
        gardener.warm_from_cache([album], duty=0)

    lines = [r.getMessage() for r in caplog.records]
    assert any("checking 1 albums" in m for m in lines)
    assert any("done in" in m for m in lines)


def test_the_rest_is_proportional_to_the_work(monkeypatch):
    """A fixed pause is the wrong shape: 50 ms is most of the time on a laptop
    and a rounding error on a NAS, which is backwards — the slow machine is the
    one that most needs the pass out of its way. Resting in proportion to what
    the album actually cost self-tunes with nothing to measure."""
    slept: list[float] = []
    monkeypatch.setattr(gardener.time, "sleep", slept.append)

    gardener._rest(worked=0.1, duty=0.25)  # a quarter duty → rest three times as long

    assert slept == [pytest.approx(0.3)]


def test_one_pathological_album_cannot_park_the_pass(monkeypatch):
    """Proportional rest has a tail: a hundred-track album on a failing disk
    would otherwise buy itself a rest measured in minutes, during which the pass
    does nothing at all and its progress lines say nothing new."""
    slept: list[float] = []
    monkeypatch.setattr(gardener.time, "sleep", slept.append)

    gardener._rest(worked=600.0, duty=0.25)

    assert slept == [gardener._MAX_REST.total_seconds()]


def test_pacing_can_be_switched_off_entirely(monkeypatch):
    """`duty=0` is what the tests above run at, so it has to mean *no sleep at
    all* rather than a very short one — a test suite that really slept would be
    paying the pass's politeness on every run."""
    slept: list[float] = []
    monkeypatch.setattr(gardener.time, "sleep", slept.append)

    gardener._rest(worked=1.0, duty=0)

    assert slept == []


# --- The background pass (#270) ---------------------------------------------
#
# The pass is detect-only, so what these have to pin is not what it writes — it
# writes nothing — but what it SPENDS and what it skips. Every assertion about
# the number of MusicBrainz requests is load-bearing for that reason: the flags
# come out identical whether the pass asked once or a hundred times, so nothing
# else in the suite can notice a budget leak.


@pytest.fixture(autouse=True)
def _forget_what_was_asked():
    """`gardener._asked` is module-level pacing state that outlives a test.

    A leak would make a later pass skip an album for reasons that test never set
    up — and it would do it by making the pass *quieter*, which is the direction
    an assertion about call counts reads as success.
    """
    gardener._asked.clear()
    yield
    gardener._asked.clear()


def _inc() -> str:
    return "+".join(sorted(mb_lookup.RELEASE_INCLUDES))


def _store(release: dict, *, age: timedelta = timedelta(0)) -> None:
    """Record `release` as `fetch_release` would have done, `age` ago."""
    activity_store.store_release(str(release["id"]), _inc(), release)
    conn = activity_store._ensure()
    conn.execute(
        "UPDATE mb_release_cache SET fetched_at = ? WHERE mbid = ?",
        ((datetime.now(UTC) - age).isoformat(), str(release["id"])),
    )
    conn.commit()


def _serving(monkeypatch, *releases: dict) -> list[str]:
    """Answer MusicBrainz from `releases`, and return the list of ids asked for.

    Patched on `mb_lookup` rather than on `mb_cache`, so the cache's own
    behaviour — which row it reads, which row it writes back, and under which id
    — is part of what these tests exercise rather than something stubbed past.

    An id nothing answers for raises `ReleaseGoneError`, which is what
    MusicBrainz does for a release that has been deleted.
    """
    by_id = {str(r["id"]): r for r in releases}
    asked: list[str] = []

    def _fetch(mbid: str) -> dict:
        asked.append(mbid)
        if mbid not in by_id:
            raise mb_lookup.ReleaseGoneError(f"MusicBrainz no longer has release {mbid}")
        return copy.deepcopy(by_id[mbid])

    monkeypatch.setattr(mb_lookup, "fetch_release", _fetch)
    return asked


def _stale() -> timedelta:
    """Old enough that the pass considers an album due."""
    return gardener.RECHECK_AFTER + timedelta(days=1)


def test_a_pass_finds_an_update_nobody_went_looking_for(tmp_path, monkeypatch):
    """The reason the pass exists. Every other route to the flag needs a human —
    opening the album, or a payload some earlier human's visit left behind — so
    an album nobody has touched since MusicBrainz edited it stays silent forever
    without this."""
    activity_store.init(tmp_path / "activity.db")
    album = _tagged(tmp_path, _release())
    _store(_release(), age=_stale())
    asked = _serving(monkeypatch, _release("Test Album (remastered)"))

    result = gardener.sweep([album])

    assert asked == ["rel-aaa"]
    assert album.update_available is True
    assert (result.examined, result.flagged) == (1, 1)


def test_an_unchanged_release_never_reaches_the_files(tmp_path, monkeypatch):
    """The early exit, which is what makes a nightly pass over an unchanged
    library cost its requests and nothing else. Asserted as *no plan was built*
    rather than as "the flag came out the same", because the flag comes out the
    same either way — reading every album's files every night would be invisible
    to any assertion about the answer."""
    activity_store.init(tmp_path / "activity.db")
    album = _tagged(tmp_path, _release())
    _store(_release(), age=_stale())
    _serving(monkeypatch, _release())
    monkeypatch.setattr(
        tagger, "plan_album", lambda *a, **k: pytest.fail("the pass read the files")
    )

    assert gardener.sweep([album]).examined == 0


def test_an_unchanged_release_does_not_clear_a_flag_already_raised(tmp_path, monkeypatch):
    """The trap the early exit sets. "MusicBrainz has not moved since we last
    looked" and "the files have nothing outstanding" are different facts, and an
    album whose update was never applied keeps it however long MusicBrainz sits
    still. Skipping the album is right; clearing it would empty the filter of
    exactly the albums the user has yet to act on."""
    activity_store.init(tmp_path / "activity.db")
    album = _tagged(tmp_path, _release())
    moved = _release("Test Album (remastered)")
    _store(moved, age=_stale())
    gardener.refresh_flag(album, moved)
    assert album.update_available is True

    _serving(monkeypatch, moved)
    gardener.sweep([album])

    assert album.update_available is True


def test_the_pass_writes_nothing_to_the_files(tmp_path, monkeypatch):
    """Detect-only, stated as a property rather than as a claim in a docstring.
    `owned.AUTO_APPLY` is empty, so there is nothing the classifier permits the
    pass to apply — and this is the assertion that fails the day someone widens
    it without meaning to."""
    activity_store.init(tmp_path / "activity.db")
    album = _tagged(tmp_path, _release())
    before = {p: p.read_bytes() for p in sorted(album.path.iterdir())}
    _store(_release(), age=_stale())
    _serving(monkeypatch, _release("Test Album (remastered)"))

    gardener.sweep([album])

    assert album.update_available is True  # it found the change ...
    assert {p: p.read_bytes() for p in sorted(album.path.iterdir())} == before  # ... and left it


def test_a_second_pass_over_an_unchanged_library_does_nothing_at_all(tmp_path, monkeypatch):
    """Idempotency, stated end to end: the pass runs twice and the second one
    asks nothing, reads nothing and changes nothing. This is the outer of the
    two mechanisms — the recheck window, which keeps the album out of the queue
    entirely; the early exit behind it has its own test, and never gets a turn
    here. Under a timer that fires every hour forever, this is the property that
    decides whether the feature is quiet or unbearable."""
    activity_store.init(tmp_path / "activity.db")
    album = _tagged(tmp_path, _release())
    _store(_release(), age=_stale())
    asked = _serving(monkeypatch, _release("Test Album (remastered)"))

    first = gardener.sweep([album])
    second = gardener.sweep([album])

    assert (first.asked, first.examined) == (1, 1)
    assert (second.asked, second.examined) == (0, 0)
    assert asked == ["rel-aaa"]
    assert album.update_available is True  # the first pass's answer still stands


def test_an_album_asked_about_recently_is_not_asked_again(tmp_path, monkeypatch):
    """What stops an hourly timer being an hourly request per album. The clock
    is the cache row's own `fetched_at`, so this needs no state of its own — and
    a library the pass has just swept costs nothing until the window is up."""
    activity_store.init(tmp_path / "activity.db")
    album = _tagged(tmp_path, _release())
    _store(_release(), age=gardener.RECHECK_AFTER - timedelta(hours=1))
    asked = _serving(monkeypatch, _release())

    assert gardener.sweep([album]).asked == 0
    assert asked == []


def test_an_album_musicbrainz_was_never_asked_about_goes_first(tmp_path, monkeypatch):
    """Never-fetched sorts before every real timestamp, which puts the albums
    the flag is silent about at the front of the queue. Those are the ones a
    pass is worth most on: the warm-up cannot speak for them, so until the pass
    reaches one, "no update" is a guess rather than an answer."""
    activity_store.init(tmp_path / "activity.db")
    seen = _tagged(tmp_path, _release(mbid="rel-seen"), name="Seen")
    never = _tagged(tmp_path, _release(mbid="rel-never"), name="Never")
    _store(_release(mbid="rel-seen"), age=_stale())
    asked = _serving(monkeypatch, _release(mbid="rel-seen"), _release(mbid="rel-never"))

    gardener.sweep([seen, never], limit=1)

    assert asked == ["rel-never"]


def test_the_stalest_album_goes_first_when_the_cap_bites(tmp_path, monkeypatch):
    """The cap is what bounds one pass; the ordering is what stops the cap
    starving the same albums forever. Least-recently-asked first means every
    album reaches the front eventually, which a stable library order would not
    give — it would sweep the first N albums nightly and never reach the rest."""
    activity_store.init(tmp_path / "activity.db")
    recent = _tagged(tmp_path, _release(mbid="rel-recent"), name="Recent")
    ancient = _tagged(tmp_path, _release(mbid="rel-ancient"), name="Ancient")
    _store(_release(mbid="rel-recent"), age=_stale())
    _store(_release(mbid="rel-ancient"), age=_stale() * 2)
    asked = _serving(monkeypatch, _release(mbid="rel-recent"), _release(mbid="rel-ancient"))

    gardener.sweep([recent, ancient], limit=1)

    assert asked == ["rel-ancient"]


def test_two_albums_on_one_release_cost_one_request(tmp_path, monkeypatch):
    """A duplicate rip in two folders (#243) names one release twice. One fetch
    answers for both, and asking again would spend a second rate-limited request
    on a payload already in hand — invisible in the result, which is correct
    either way."""
    activity_store.init(tmp_path / "activity.db")
    one = _tagged(tmp_path, _release(), name="Test Album")
    two = _tagged(tmp_path, _release(), name="Test Album (copy)")
    _store(_release(), age=_stale())
    asked = _serving(monkeypatch, _release("Test Album (remastered)"))

    result = gardener.sweep([one, two])

    assert asked == ["rel-aaa"]
    assert (one.update_available, two.update_available) == (True, True)
    assert result.examined == 2


def test_a_deleted_release_is_not_asked_about_again_next_pass(tmp_path, monkeypatch):
    """A 404 stores nothing — a negative is never cached — so the cache clock
    says "never asked" forever and the album would come back to the front of the
    queue on every pass. Its remedy is a human decision (#194/#210) that
    re-asking brings no closer, so the pass remembers having asked."""
    activity_store.init(tmp_path / "activity.db")
    album = _tagged(tmp_path, _release())
    asked = _serving(monkeypatch)  # nothing answers: MusicBrainz has deleted it

    first = gardener.sweep([album])
    second = gardener.sweep([album])

    assert first.gone == 1
    assert asked == ["rel-aaa"]  # asked once, across both passes
    assert second.asked == 0


def test_a_merged_release_is_not_asked_about_again_next_pass(tmp_path, monkeypatch):
    """MusicBrainz redirects a merged id, so the row lands under the id it gave
    back (#268) and nothing ever refreshes the one the sidecar names. Same leak
    as the deleted case and a more common one — without the memory, every merged
    album in the library costs a request on every pass, forever."""
    activity_store.init(tmp_path / "activity.db")
    album = _tagged(tmp_path, _release())
    _store(_release(), age=_stale())
    merged = _release(mbid="rel-merged-target")
    asked: list[str] = []

    def _redirects(mbid: str) -> dict:
        """What a merge looks like from here: the id asked for is answered by a
        release carrying a different one."""
        asked.append(mbid)
        return copy.deepcopy(merged)

    monkeypatch.setattr(mb_lookup, "fetch_release", _redirects)

    gardener.sweep([album])
    gardener.sweep([album])

    assert asked == ["rel-aaa"]
    assert album.update_available is True  # the id in the files no longer matches


def test_a_musicbrainz_outage_stops_the_pass_rather_than_grinding_through_it(tmp_path, monkeypatch):
    """When MusicBrainz is down every album fails identically. Continuing spends
    a rate-limited request per album to learn the same thing each time, and
    buries the first failure — the only one that says anything — under the rest.
    The next pass retries from the top."""
    activity_store.init(tmp_path / "activity.db")
    albums = []
    for i in range(gardener._GIVE_UP_AFTER + 3):
        albums.append(_tagged(tmp_path, _release(mbid=f"rel-{i:03d}"), name=f"Album {i}"))
    asked: list[str] = []

    def _down(mbid: str) -> dict:
        asked.append(mbid)
        raise mb_lookup.MBError("MusicBrainz is not answering")

    monkeypatch.setattr(mb_lookup, "fetch_release", _down)

    result = gardener.sweep(albums)

    assert result.gave_up is True
    assert len(asked) == gardener._GIVE_UP_AFTER
    assert result.failed == gardener._GIVE_UP_AFTER


def test_a_deleted_release_does_not_count_towards_giving_up(tmp_path, monkeypatch):
    """A 404 is an answer, not a failure. A library with five albums MusicBrainz
    has deleted — which is a library that has been adopted, not a broken one —
    would otherwise abort every pass at the fifth and never reach the rest."""
    activity_store.init(tmp_path / "activity.db")
    albums = [
        _tagged(tmp_path, _release(mbid=f"rel-{i:03d}"), name=f"Album {i}")
        for i in range(gardener._GIVE_UP_AFTER + 2)
    ]
    asked = _serving(monkeypatch)  # nothing answers for any of them

    result = gardener.sweep(albums)

    assert result.gave_up is False
    assert result.gone == len(albums)
    assert len(asked) == len(albums)


def test_an_album_with_no_release_of_its_own_is_never_asked_about(tmp_path, monkeypatch):
    """An unlinked album has no id to ask with. The pass must skip it rather
    than hand a None to the cache — the same guard the warm-up carries."""
    activity_store.init(tmp_path / "activity.db")
    d = _album_dir(tmp_path)
    album = next(a for a in scanner.scan(tmp_path) if a.path == d)
    assert album.sidecar is None
    asked = _serving(monkeypatch)

    assert gardener.sweep([album]).asked == 0
    assert asked == []


def _stub_runner(*, running: bool = False, scanned: bool = True):
    """Enough of a runner for the tick's guards to read."""

    class _Stub:
        is_running = running

        def has_completed(self) -> bool:
            return scanned

        def albums(self) -> list:
            return []

    return _Stub()


def _sweep_signal(monkeypatch) -> threading.Event:
    """Set when the tick actually starts a pass."""
    done = threading.Event()

    def _sweep(albums, **kwargs):
        done.set()
        return gardener.PassResult(asked=0, examined=0, flagged=0, gone=0, failed=0)

    monkeypatch.setattr(gardener, "sweep", _sweep)
    return done


def _stub_app(level: str = "review"):
    """Enough of the app for the tick to read the live level off (#312)."""
    from harmonist.config import Config, GardenerConfig, PathsConfig

    cfg = Config(
        paths=PathsConfig(config_dir=Path("/nonexistent"), music_dir=Path("/nonexistent")),
        gardener=GardenerConfig.model_validate({"level": level}),
    )
    return SimpleNamespace(state=SimpleNamespace(cfg=cfg))


def test_a_tick_starts_a_pass_when_nothing_else_is_working(monkeypatch):
    """The positive case the four guards below are exceptions to — without it
    they would all pass against a tick that never runs anything at all."""
    from harmonist.web import main as web_main

    done = _sweep_signal(monkeypatch)

    assert (
        web_main._update_check_if_idle(_stub_app(), _stub_runner(), _stub_runner(), _stub_runner())
        is None
    )
    assert done.wait(5) is True


@pytest.mark.parametrize(
    ("sync", "reconcile", "scanned"),
    [(True, False, True), (False, True, True), (False, False, False)],
)
def test_a_tick_stands_aside_for_work_with_a_better_claim(monkeypatch, sync, reconcile, scanned):
    """A sync and a reconcile pass are already spending the one shared
    MusicBrainz rate limit, on something the user set in motion; the check's
    albums have waited a week and can wait another hour. Before the first scan
    there is no library to look at — `albums()` is empty, so a tick then would
    report a library of nothing."""
    from harmonist.web import main as web_main

    done = _sweep_signal(monkeypatch)

    reason = web_main._update_check_if_idle(
        _stub_app(),
        _stub_runner(running=sync),
        _stub_runner(running=reconcile),
        _stub_runner(scanned=scanned),
    )

    assert done.wait(0.25) is False
    # Named rather than merely refused: **Check now** (#312) shows this to
    # whoever pressed it, and a control that declines in silence reads as broken.
    assert reason


def test_a_tick_does_nothing_while_the_level_is_off(monkeypatch):
    """The default, and now a per-tick decision rather than a task that was
    never created (#312): the timer runs from startup either way, so `off` has
    to be checked here or an install that never asked for the pass gets one."""
    from harmonist.web import main as web_main

    done = _sweep_signal(monkeypatch)

    reason = web_main._update_check_if_idle(
        _stub_app(level="off"), _stub_runner(), _stub_runner(), _stub_runner()
    )

    assert done.wait(0.25) is False
    assert reason


def test_a_pass_still_running_is_not_started_on_top_of_itself(monkeypatch):
    """A pass is capped and normally takes two minutes, but a MusicBrainz that
    answers slowly rather than failing can stretch one past the hour. Two at
    once would ask about the same albums twice and pay twice for the answer."""
    from harmonist.web import main as web_main

    done = _sweep_signal(monkeypatch)
    web_main._update_check_lock.acquire()
    try:
        reason = web_main._update_check_if_idle(
            _stub_app(), _stub_runner(), _stub_runner(), _stub_runner()
        )
        assert done.wait(0.25) is False
        assert reason
    finally:
        web_main._update_check_lock.release()


@contextmanager
def _engaged_timers(cfg, monkeypatch):
    """The periodic tasks the lifespan engages, and the app they tick against.

    The actions are captured rather than run, so a test drives a tick when it
    wants one instead of waiting out an hour-long interval.
    """
    from fastapi.testclient import TestClient

    from harmonist.web import main as web_main

    actions: dict[str, object] = {}

    async def _record(interval, action, *, name, stop_event=None):
        actions[name] = action
        if stop_event is not None:
            await stop_event.wait()

    monkeypatch.setattr(web_main.periodic, "run_periodically", _record)
    app = web_main.create_app(cfg)
    with TestClient(app, headers={"HX-Request": "true"}):
        for _ in range(500):  # the first scan, which the tick waits for
            if app.state.scan_runner.has_completed():
                break
            time.sleep(0.01)
        yield app, actions


@pytest.mark.parametrize("level", ["off", "review"])
def test_the_update_check_timer_is_engaged_whatever_the_level(engaged, monkeypatch, level):
    """The timer starts with the process at every level, on the same generic
    timer the hourly rescan runs on rather than a second pattern beside it
    (#151). It used not to when the level was `off`, on the grounds that a
    default install should carry no idle timer — but the level is a Settings
    control now (#312), and a config change cannot retroactively create a task
    that was never started. One sleeping task per process buys the restart."""
    from harmonist.config import GardenerConfig

    cfg, _ = engaged
    cfg = cfg.model_copy(update={"gardener": GardenerConfig.model_validate({"level": level})})

    with _engaged_timers(cfg, monkeypatch) as (_app, actions):
        assert "update check" in actions


def test_turning_the_check_on_takes_effect_without_a_restart(engaged, monkeypatch):
    """What the Settings control (#312) is worth: the tick reads the level from
    `app.state.cfg`, which the save replaces. The lifespan closure holds the
    *startup* config, and a tick reading that would leave the setting saved,
    looking applied, and doing nothing until the container came back — worse
    than having required the restart in the first place."""
    from harmonist.config import GardenerConfig

    cfg, _ = engaged  # ships `off`
    done = _sweep_signal(monkeypatch)

    with _engaged_timers(cfg, monkeypatch) as (app, actions):
        tick = actions["update check"]
        tick()
        assert done.wait(0.25) is False

        app.state.cfg = app.state.cfg.model_copy(
            update={"gardener": GardenerConfig(level="review")}
        )
        tick()

        assert done.wait(5) is True
