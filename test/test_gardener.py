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
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from harmonist import activity_store, gardener, mb_cache, mb_lookup, scanner, tagger
from harmonist import sidecar as sc
from harmonist.models import Album, Sidecar
from harmonist.web.scan_runner import ScanRunner

SINE_M4A = Path(__file__).parent / "fixtures" / "sine.m4a"


def _release(title: str = "Test Album", *, tracks: int = 1) -> dict:
    """A minimal release the tagger can write from, in musicbrainzngs' shape."""
    return {
        "id": "rel-aaa",
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


def _album_dir(root: Path, *, tracks: int = 1) -> Path:
    d = root / "Test Artist" / "Test Album"
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


def _tagged(root: Path, release: dict, *, tracks: int = 1) -> Album:
    """An album tagged from `release`, as the scanner sees it afterwards."""
    d = _album_dir(root, tracks=tracks)
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
    for. An ISRC is the everyday one — MusicBrainz fills them in constantly, and
    the tracklist's four columns (#, Title, Artist, Length) have nowhere to put
    it — so without this box the album sits under Update available and reads as
    matching on every row shown."""
    cfg, engage = engaged
    _tagged(cfg.paths.music_dir, _release())
    filled_in = copy.deepcopy(_release())
    track = filled_in["medium-list"][0]["track-list"][0]
    track["recording"]["isrc-list"] = ["GBAYE0000123"]
    monkeypatch.setattr(mb_lookup, "fetch_release", lambda *a, **k: filled_in)
    client, runner = engage()

    body = client.get(f"/library/{runner.albums()[0].id}/compare").text

    assert "Track tags a re-tag would change" in body
    assert "ISRC" in body
    assert "GBAYE0000123" in body


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
    assert "Track tags a re-tag would change" not in body


def test_an_album_with_nothing_waiting_says_nothing(engaged, monkeypatch):
    """No empty heading on an album that is up to date — the section has to be
    absent, not present-and-blank, or every album grows a box telling the reader
    there is nothing to tell them."""
    cfg, engage = engaged
    _tagged(cfg.paths.music_dir, _release())
    monkeypatch.setattr(mb_lookup, "fetch_release", lambda *a, **k: _release())
    client, runner = engage()

    body = client.get(f"/library/{runner.albums()[0].id}/compare").text

    assert "Track tags a re-tag would change" not in body


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
