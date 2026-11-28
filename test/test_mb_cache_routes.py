"""Which routes get a cached MusicBrainz answer, and which must not (#127).

The cache is only half the feature; the other half is the routing rule, and it
is the half that can go wrong silently. A route wrongly cached spends nothing
and shows the user stale data; a route wrongly uncached works perfectly and
quietly burns the 1-req/sec budget it was built to protect. Neither shows up as
a failure anywhere else, so the request count is asserted directly.

The rule under test: **reads that display or compare may be cached; writes
nobody reviewed, and anything the user pressed to force a re-check, fetch
fresh; a write of what the page showed uses exactly that stored release** (#532,
#1060).
"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
from mutagen.mp4 import MP4

from harmonist import activity, activity_store, mb_cache, mb_lookup
from harmonist import sidecar as sidecar_mod
from harmonist.config import BandcampConfig, Config, PathsConfig, ServerConfig, TestConfig
from harmonist.models import Sidecar
from harmonist.web.main import create_app
from test.test_gardener import _release as tagged_release
from test.test_gardener import _tagged as tagged_album

FIXTURES_DIR = Path(__file__).parent / "fixtures"
SINE_M4A = FIXTURES_DIR / "sine.m4a"
MBID = "33333333-4444-5555-6666-777777777777"
STORE_URL = "https://artist.bandcamp.com/album/test-album"


@pytest.fixture
def cfg(tmp_path):
    return Config(
        paths=PathsConfig(config_dir=tmp_path / "config", music_dir=tmp_path / "music"),
        bandcamp=BandcampConfig(),
        server=ServerConfig(),
        test=TestConfig(mode="fixture"),
    )


@pytest.fixture(autouse=True)
def no_cover_fetch(monkeypatch):
    monkeypatch.setattr("harmonist.cover_art.front_image", lambda *a, **kw: None)


@pytest.fixture(autouse=True)
def default_ttl():
    """`mb_cache.configure` is process-level state — put it back, or a test that
    changed it silently changes a later one (tests run in random order)."""
    yield
    mb_cache.configure(timedelta(hours=1))


@pytest.fixture
def client(cfg):
    cfg.paths.music_dir.mkdir(parents=True, exist_ok=True)
    cfg.paths.config_dir.mkdir(parents=True, exist_ok=True)
    return TestClient(create_app(cfg), headers={"HX-Request": "true"})


class _Counter:
    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    def __call__(self, mbid):
        self.calls += 1
        return self.payload


def _release() -> dict:
    return {
        "id": MBID,
        "title": "Test Album",
        "status": "Official",
        "artist-credit": [{"artist": {"id": "art-1", "name": "Artist"}, "name": "Artist"}],
        "release-group": {"id": "rg-1", "primary-type": "Album"},
        "medium-list": [
            {
                "position": "1",
                "format": "CD",
                "track-list": [
                    {
                        "id": f"rt-{i}",
                        "position": str(i),
                        "title": f"Track {i}",
                        "recording": {"id": f"rec-{i}", "title": f"Track {i}", "length": "1000"},
                    }
                    for i in range(1, 3)
                ],
            }
        ],
    }


def _album(cfg, *, store_url: str | None = None) -> Path:
    d = cfg.paths.music_dir / "Artist" / "Album"
    d.mkdir(parents=True)
    for i in (1, 2):
        f = d / f"0{i} Track {i}.m4a"
        shutil.copy(SINE_M4A, f)
        a = MP4(f)
        a["\xa9alb"] = ["Test Album"]
        a["\xa9ART"] = ["Artist"]
        a["trkn"] = [(i, 2)]
        a["----:com.apple.iTunes:MusicBrainz Album Id"] = [MBID.encode()]
        a.save()
    sidecar_mod.write(
        d,
        Sidecar(
            mb_release_id=MBID,
            tagged_at=datetime(2026, 1, 1, tzinfo=UTC),
            store_url=store_url,
        ),
    )
    return d


def _album_id(cfg, album_dir: Path) -> str:
    from harmonist import scanner

    for a in scanner.scan(cfg.paths.music_dir):
        if a.path == album_dir:
            return a.id
    raise AssertionError(f"no album at {album_dir}")


def _age_stored_release(by: timedelta) -> None:
    """Backdate the stored release, so the next read finds it stale.

    Ageing the ROW rather than shortening the TTL: `mb_cache.configure` is
    process state that every later test inherits (hence the `default_ttl`
    fixture), and an old answer is the situation these tests are about."""
    conn = activity_store._ensure()
    conn.execute(
        "UPDATE mb_release_cache SET fetched_at = ?",
        ((datetime.now(UTC) - by).isoformat(),),
    )
    conn.commit()


def test_opening_an_album_page_twice_costs_one_musicbrainz_request(client, cfg, monkeypatch):
    """The headline saving. Browsing a library used to spend a rate-limited
    request per album page view, every view."""
    d = _album(cfg)
    fetch = _Counter(_release())
    monkeypatch.setattr(mb_lookup, "fetch_release", fetch)
    album_id = _album_id(cfg, d)

    assert client.get(f"/library/{album_id}/compare").status_code == 200
    assert client.get(f"/library/{album_id}/compare").status_code == 200

    assert fetch.calls == 1


def test_the_compare_panel_says_when_it_last_read_musicbrainz(client, cfg, monkeypatch):
    """The affordance that makes a cached comparison honest. Before the cache
    this said "just now" unconditionally, because it always was.

    The row is aged to three hours under a SIX-hour TTL, so it is *within* the
    window and served without anything being asked. A row past the TTL is served
    too since #387, with its real age and a refresh behind it — this is the case
    where the age is all there is, so nothing but the date can be under test.
    """
    d = _album(cfg)
    monkeypatch.setattr(mb_lookup, "fetch_release", _Counter(_release()))
    album_id = _album_id(cfg, d)

    client.get(f"/library/{album_id}/compare")
    mb_cache.configure(timedelta(hours=6))
    _age_stored_release(timedelta(hours=3))
    body = client.get(f"/library/{album_id}/compare").text

    assert "3 hours ago" in body, body[:400]


def test_read_again_forces_a_live_fetch(client, cfg, monkeypatch):
    """The escape hatch from a stale answer (review-gate item 5). Without it a
    user who has just edited MusicBrainz has no way to see the edit short of
    re-tagging."""
    d = _album(cfg)
    fetch = _Counter(_release())
    monkeypatch.setattr(mb_lookup, "fetch_release", fetch)
    album_id = _album_id(cfg, d)

    client.get(f"/library/{album_id}/compare")
    client.get(f"/library/{album_id}/compare?reread=1")

    assert fetch.calls == 2


def test_the_compare_panel_offers_the_re_read_control(client, cfg, monkeypatch):
    """The control has to be ON the panel for the escape hatch to exist. Asserted
    as an absence elsewhere would be untestable, so this is the positive form."""
    d = _album(cfg)
    monkeypatch.setattr(mb_lookup, "fetch_release", _Counter(_release()))
    album_id = _album_id(cfg, d)

    body = client.get(f"/library/{album_id}/compare").text

    assert f'hx-get="/library/{album_id}/compare?reread=1"' in body


def test_the_album_page_states_the_read_time_without_waiting_for_compare(client, cfg, monkeypatch):
    """The panel's "Checked" date is rendered as the page is built (#355).

    It used to arrive with the /compare fetch, because it sat in the note and
    everything else there needs that fetch. It doesn't: `mb_cache.fetched_at` is
    a local SQLite read, so the page can state it immediately — which is the
    whole reason it could move out of the note and let the note narrow to the
    finding alone.

    Asserted on a page load with NO compare behind it, since a compare would
    swap the line in out of band and the test would pass either way.
    """
    d = _album(cfg)
    monkeypatch.setattr(mb_lookup, "fetch_release", _Counter(_release()))
    album_id = _album_id(cfg, d)
    client.get(f"/library/{album_id}/compare")  # the read this date reports

    body = client.get(f"/album/{album_id}").text

    assert f'id="album-checked-{album_id}"' in body
    assert "MusicBrainz release last read" in body


def test_an_album_never_read_claims_no_read_time(client, cfg, monkeypatch):
    """No line rather than a claim about a check that never happened.

    The under-reporting direction is the safe one: a page can be silent about a
    read it hasn't made, but it must never state one it has. Nothing has fetched
    this release, so `fetched_at` is None and the row is absent — the same
    markup the test above asserts present once a read exists.
    """
    d = _album(cfg)
    monkeypatch.setattr(mb_lookup, "fetch_release", _Counter(_release()))
    album_id = _album_id(cfg, d)

    body = client.get(f"/album/{album_id}").text

    assert "MusicBrainz release last read" not in body


def test_a_retag_never_tags_from_a_cached_release(client, cfg, monkeypatch):
    """It writes tags to the user's files. Doing that from an hour-old payload
    would put metadata on disk Harmonist had already been told was superseded."""
    d = _album(cfg)
    fetch = _Counter(_release())
    monkeypatch.setattr(mb_lookup, "fetch_release", fetch)
    album_id = _album_id(cfg, d)

    client.get(f"/library/{album_id}/compare")  # warms the cache
    assert fetch.calls == 1
    assert client.post(f"/retag/{album_id}").status_code == 200

    assert fetch.calls == 2, "the re-tag must have gone to MusicBrainz itself"


def test_a_store_url_search_refreshes_results_without_fetching_a_selection(
    client, cfg, monkeypatch
):
    """Searching again means "I have just edited MusicBrainz". Serving it a
    stored payload would make the button a silent no-op with nothing on screen
    to say why."""
    d = _album(cfg, store_url=STORE_URL)
    sidecar_mod.write(d, Sidecar(store_url=STORE_URL))  # Needs MBID: nothing confirmed
    fetch = _Counter(_release())
    monkeypatch.setattr(mb_lookup, "fetch_release", fetch)
    lookups = []

    def lookup(url):
        lookups.append(url)
        return [{"id": MBID, "title": f"Result {len(lookups)}"}], 1

    monkeypatch.setattr(mb_lookup, "candidate_summaries_for_url", lookup)
    album_id = _album_id(cfg, d)

    mb_cache.fetch_release(MBID)  # warms the cache
    assert fetch.calls == 1
    for attempt in (1, 2):
        r = client.post(f"/manual/{album_id}/candidates", data={"suggest": "true"})
        assert f"Result {attempt}" in r.text
    assert lookups == [STORE_URL, STORE_URL]
    assert fetch.calls == 1, "listing results must not fetch a selected release"


def test_a_forced_read_leaves_the_cache_current_for_the_next_reader(client, cfg, monkeypatch):
    """A bypass that read ROUND the cache would leave the stored row stale at
    exactly the moment MusicBrainz is known to have changed — and that row is
    the gardener's baseline (#32)."""
    d = _album(cfg)
    fetch = _Counter(_release())
    monkeypatch.setattr(mb_lookup, "fetch_release", fetch)
    album_id = _album_id(cfg, d)

    client.post(f"/retag/{album_id}")  # a forced, uncached fetch
    calls_after_retag = fetch.calls
    client.get(f"/library/{album_id}/compare")

    assert fetch.calls == calls_after_retag, "the re-tag should have filled the cache"


# ---------------------------------------------------------------------------
# Serving the stored answer while a fresher one is fetched (#387)
#
# The TTL used to decide whether the comparison was shown AT ALL: a row past it
# was not served, so the page sat on "Checking tags against MusicBrainz…" over
# the top of a payload it already had. Now the TTL decides only whether to ask
# again, and the asking happens behind a rendered comparison.
#
# The request COUNT is what these hold onto: this may not have bought
# responsiveness with a second request against a 1-req/sec budget.
# ---------------------------------------------------------------------------


def test_a_stale_comparison_renders_before_musicbrainz_is_asked(client, cfg, monkeypatch):
    """The whole of #387. An album read last night has the entire answer in
    SQLite, and the page used to block on a live fetch before showing any of it.

    Asserted on the FETCH COUNT, not on the elapsed time: "did not wait" and
    "did not ask" are the same claim here, and only one of them is observable
    without a clock."""
    d = _album(cfg)
    fetch = _Counter(_release())
    monkeypatch.setattr(mb_lookup, "fetch_release", fetch)
    album_id = _album_id(cfg, d)
    client.get(f"/library/{album_id}/compare")  # fills the store
    _age_stored_release(timedelta(hours=20))

    body = client.get(f"/library/{album_id}/compare").text

    assert fetch.calls == 1, "the page must not have waited on MusicBrainz"
    assert 'class="tag-fields' in body, "the comparison itself, not a placeholder"
    # …and the refresh that keeps it from being last night's answer for good.
    assert f'hx-get="/library/{album_id}/compare?check=1"' in body
    # The two controls that act on a release, held until it lands: a re-tag
    # writes from whatever MusicBrainz says when it is pressed, and an ignore is
    # recorded against the version this render came from. (The trigger names
    # itself first only so the selector always matches something — see the
    # template.)
    assert f'#retag-btn-{album_id}, #album-update-ignore-{album_id} input"' in body


def test_the_refresh_behind_a_stale_comparison_costs_one_request_and_stops(
    client, cfg, monkeypatch
):
    """The other half of the count. The stale render asks nothing, so the refresh
    has to ask — once — and must not ask the browser to come back again, or a
    MusicBrainz outage would turn one page view into a loop."""
    d = _album(cfg)
    fetch = _Counter(_release())
    monkeypatch.setattr(mb_lookup, "fetch_release", fetch)
    album_id = _album_id(cfg, d)
    client.get(f"/library/{album_id}/compare")
    _age_stored_release(timedelta(hours=20))
    client.get(f"/library/{album_id}/compare")  # the stale render

    body = client.get(f"/library/{album_id}/compare?check=1").text

    assert fetch.calls == 2, "one page view, one request — the same as before #387"
    assert 'class="tag-fields' in body
    # The MusicBrainz refresh specifically. Since #485 this response also carries
    # the Artwork section, whose own `/artwork?check=1` trigger asks the Cover
    # Art Archive — a different service on a different budget, and not what this
    # is about. A bare "check=1" matched both.
    assert f"/library/{album_id}/compare?check=1" not in body, (
        "a response to a refresh must never ask for another"
    )


def test_a_fresh_stored_comparison_asks_for_nothing_further(client, cfg, monkeypatch):
    """Inside the TTL there is nothing to refresh, so no refresh is scheduled.

    Without this, every album page view would send a second request that the
    cache would then have to answer — free in MusicBrainz terms and a re-read of
    every file in the album for nothing."""
    d = _album(cfg)
    monkeypatch.setattr(mb_lookup, "fetch_release", _Counter(_release()))
    album_id = _album_id(cfg, d)
    client.get(f"/library/{album_id}/compare")

    body = client.get(f"/library/{album_id}/compare").text

    assert 'class="tag-fields' in body
    # Qualified for the reason above (#485): the archive's check rides in this
    # response too, and it is not the request this test counts.
    assert f"/library/{album_id}/compare?check=1" not in body


def test_a_failed_refresh_keeps_the_comparison_it_was_refreshing(client, cfg, monkeypatch):
    """A stale-first render must not be WORSE than the placeholder it replaced.

    The user is reading a comparison; the failure is of a request they never made.
    Wiping what they are reading to report it would make the page unusable at
    exactly the moment MusicBrainz is unreachable — which is when the stored
    answer is worth the most."""
    d = _album(cfg)
    monkeypatch.setattr(mb_lookup, "fetch_release", _Counter(_release()))
    album_id = _album_id(cfg, d)
    client.get(f"/library/{album_id}/compare")
    _age_stored_release(timedelta(hours=20))

    def boom(mbid):
        raise mb_lookup.MBError("network is down")

    monkeypatch.setattr(mb_lookup, "fetch_release", boom)
    body = client.get(f"/library/{album_id}/compare?check=1").text

    assert 'class="tag-fields' in body, "the stored comparison is still there"
    assert "Couldn't read MusicBrainz again" in body, "and the page says what it is"
    assert "Couldn't fetch from MusicBrainz" not in body, "that is the nothing-stored case"


def test_a_failed_fetch_with_nothing_stored_still_says_so(client, cfg, monkeypatch):
    """The live path for the note the test above requires to be absent. With no
    stored answer there is nothing to fall back to, and the section has to report
    the failure rather than render empty."""
    d = _album(cfg)

    def boom(mbid):
        raise mb_lookup.MBError("network is down")

    monkeypatch.setattr(mb_lookup, "fetch_release", boom)
    album_id = _album_id(cfg, d)

    body = client.get(f"/library/{album_id}/compare").text

    assert "Couldn't fetch from MusicBrainz" in body


def test_a_release_deleted_since_the_stored_answer_gives_way_to_the_banner(
    client, cfg, monkeypatch
):
    """A stored payload plus a 404 means the release has been deleted (#194/#210),
    and the page has a whole response built for that. The stale render must give
    way to it rather than sitting there contradicting the banner."""
    d = _album(cfg)
    monkeypatch.setattr(mb_lookup, "fetch_release", _Counter(_release()))
    album_id = _album_id(cfg, d)
    client.get(f"/library/{album_id}/compare")
    _age_stored_release(timedelta(hours=20))

    def gone(mbid):
        raise mb_lookup.ReleaseGoneError(f"no release {mbid}")

    monkeypatch.setattr(mb_lookup, "fetch_release", gone)
    body = client.get(f"/library/{album_id}/compare?check=1").text

    assert "This release is gone from MusicBrainz" in body


def _logged_updates() -> list[str]:
    return [e.message for e in activity.recent(20) if e.message.startswith("Update available")]


def _serving(monkeypatch, title: str) -> None:
    monkeypatch.setattr(mb_lookup, "fetch_release", _Counter(tagged_release(title)))


def test_a_refresh_that_finds_a_new_update_logs_it_once(client, cfg, monkeypatch):
    """#1024. The album page's refresh is a live check like the background
    one, and an update it turns up is logged by the same rules: only when the
    payload moved, and only once however often the page is opened."""
    album = tagged_album(cfg.paths.music_dir, tagged_release())
    _serving(monkeypatch, "Test Album")
    client.get(f"/library/{album.id}/compare")  # the first fetch: no baseline
    _age_stored_release(timedelta(hours=20))
    _serving(monkeypatch, "Test Album (remastered)")

    client.get(f"/library/{album.id}/compare?check=1")
    assert _logged_updates() == ["Update available from MusicBrainz (identity)"]

    # Fresh now, so these are answered from the store…
    client.get(f"/library/{album.id}/compare")
    client.get(f"/library/{album.id}/compare?check=1")
    # …and when MusicBrainz moves again, the update was already waiting.
    _serving(monkeypatch, "Test Album (remastered, again)")
    client.get(f"/library/{album.id}/compare?reread=1")
    assert len(_logged_updates()) == 1


def test_a_first_fetch_logs_nothing(client, cfg, monkeypatch):
    """With nothing stored, nothing says MusicBrainz moved: opening an album
    whose files never took an old edit would otherwise log it as news."""
    album = tagged_album(cfg.paths.music_dir, tagged_release())
    _serving(monkeypatch, "Test Album (remastered)")

    body = client.get(f"/library/{album.id}/compare").text

    assert "Test Album (remastered)" in body
    assert _logged_updates() == []


def _apply_form(body: str) -> dict[str, str]:
    """What the update finding's **Apply updates** posts of its own."""
    form = BeautifulSoup(body, "html.parser").select_one("form.album-tag-apply")
    assert form is not None, "no Apply updates on the comparison"
    return {str(i["name"]): str(i.get("value", "")) for i in form.select("input[name]")}


def _album_tag(album) -> list[str]:
    return list(MP4(next(album.path.glob("*.m4a")))["\xa9alb"])


def _status(response) -> dict:
    return json.loads(response.headers["HX-Trigger"])


def test_apply_updates_writes_the_release_it_showed_without_asking(client, cfg, monkeypatch):
    """The page's Apply is a reviewed write (#532, #1060): the stored release
    the comparison was drawn from, whatever its age by the time it's pressed,
    and no request."""
    album = tagged_album(cfg.paths.music_dir, tagged_release())
    fetch = _Counter(tagged_release("Test Album (remastered)"))
    monkeypatch.setattr(mb_lookup, "fetch_release", fetch)
    form = _apply_form(client.get(f"/library/{album.id}/compare").text)
    assert fetch.calls == 1 and form["release_fingerprint"]
    _age_stored_release(timedelta(hours=20))

    response = client.post(f"/retag/{album.id}", data=form)

    assert _status(response)["harmonist-status"]["verb"] == "Re-tagged"
    assert fetch.calls == 1, "Apply asked MusicBrainz again"
    assert _album_tag(album) == ["Test Album (remastered)"]


def test_apply_updates_writes_nothing_once_the_release_has_moved(client, cfg, monkeypatch):
    """A newer answer stored since the page was drawn isn't what was shown:
    nothing is written, and the page is drawn again to show what there is."""
    album = tagged_album(cfg.paths.music_dir, tagged_release())
    fetch = _Counter(tagged_release("Test Album (remastered)"))
    monkeypatch.setattr(mb_lookup, "fetch_release", fetch)
    form = _apply_form(client.get(f"/library/{album.id}/compare").text)
    fetch.payload = tagged_release("Test Album (remastered, again)")
    client.get(f"/library/{album.id}/compare?reread=1")

    response = client.post(f"/retag/{album.id}", data=form)

    triggers = _status(response)
    assert triggers["harmonist-status"]["verb"] == "Not re-tagged"
    assert triggers["harmonist-status"]["level"] == "warning"
    assert "album-retagged" in triggers
    assert fetch.calls == 2
    assert _album_tag(album) == ["Test Album"]
