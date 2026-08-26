"""The TTL cache in front of MusicBrainz's get-by-id fetches (#127).

MusicBrainz rate-limits at one request per second, **per request rather than per
byte**, so the only thing that saves the budget is not asking. Every test here
counts requests, because the request count *is* the feature — a cache that
returns the right answer while still calling MusicBrainz has done nothing.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from harmonist import activity_store, mb_cache, mb_lookup

MBID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


@pytest.fixture(autouse=True)
def fresh_store_and_default_ttl(tmp_path):
    """A file-backed store per test, and the shipped TTL.

    `configure` is process-level state, exactly like `audit.set_library_root` —
    a test that changed it and did not put it back would quietly change how a
    LATER test's cache behaves, and tests run in random order.
    """
    activity_store.init(tmp_path / "activity.db")
    mb_cache.configure(timedelta(hours=1))
    yield
    mb_cache.configure(timedelta(hours=1))


class _Counter:
    """A stand-in fetch that records how many times MusicBrainz was asked."""

    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    def __call__(self, mbid):
        self.calls += 1
        return self.payload


def _release(mbid: str = MBID, title: str = "Geogaddi") -> dict:
    return {"id": mbid, "title": title, "medium-list": []}


def test_a_second_read_inside_the_ttl_does_not_ask_musicbrainz(monkeypatch):
    """The whole point. Opening an album page twice must cost one request, not
    two."""
    fetch = _Counter(_release())
    monkeypatch.setattr(mb_lookup, "fetch_release", fetch)

    first = mb_cache.fetch_release(MBID)
    second = mb_cache.fetch_release(MBID)

    assert fetch.calls == 1
    assert first["title"] == second["title"] == "Geogaddi"


def _age_rows(by: timedelta) -> None:
    """Backdate every cached row, so expiry can be tested without waiting.

    Deliberately not "set a tiny TTL and read again": the elapsed time between
    two statements can genuinely be shorter than any TTL small enough to make
    that quick, so the test would pass or fail on machine speed. Moving the
    clock the row was stamped with is the same arithmetic with no race in it.
    """
    conn = activity_store._ensure()
    for mbid, inc, stamp in conn.execute(
        "SELECT mbid, inc, fetched_at FROM mb_release_cache"
    ).fetchall():
        conn.execute(
            "UPDATE mb_release_cache SET fetched_at = ? WHERE mbid = ? AND inc = ?",
            ((datetime.fromisoformat(stamp) - by).isoformat(), mbid, inc),
        )
    conn.commit()


def test_a_read_after_the_ttl_asks_again(monkeypatch):
    """Freshness is a window, not a latch — otherwise a MusicBrainz edit would
    never be picked up without the user forcing it."""
    fetch = _Counter(_release())
    monkeypatch.setattr(mb_lookup, "fetch_release", fetch)
    mb_cache.fetch_release(MBID)

    _age_rows(timedelta(hours=2))  # the TTL is one hour
    mb_cache.fetch_release(MBID)

    assert fetch.calls == 2


def test_a_forced_read_bypasses_a_fresh_row_and_refreshes_it(monkeypatch):
    """What Recheck and the album page's "read again" control pass. It must
    fetch AND update the stored row — a bypass that read round the cache would
    leave the gardener's baseline stale at exactly the moment MusicBrainz is
    known to have changed."""
    fetch = _Counter(_release(title="Old"))
    monkeypatch.setattr(mb_lookup, "fetch_release", fetch)
    mb_cache.fetch_release(MBID)
    before = mb_cache.fetched_at(MBID)

    fetch.payload = _release(title="New")
    forced = mb_cache.fetch_release(MBID, max_age=mb_cache.FRESH)

    assert fetch.calls == 2
    assert forced["title"] == "New"
    # The row now holds the new payload, so the NEXT ordinary read sees it too.
    assert mb_cache.fetch_release(MBID)["title"] == "New"
    assert fetch.calls == 2
    after = mb_cache.fetched_at(MBID)
    assert before is not None and after is not None and after >= before


def test_a_zero_ttl_disables_serving_but_still_records(monkeypatch):
    """Turning the cache off must not turn change detection off with it: the
    gardener's baseline keeps accruing so #32 still has something to compare
    against."""
    fetch = _Counter(_release())
    monkeypatch.setattr(mb_lookup, "fetch_release", fetch)
    mb_cache.configure(timedelta(0))

    mb_cache.fetch_release(MBID)
    mb_cache.fetch_release(MBID)

    assert fetch.calls == 2
    assert mb_cache.fetched_at(MBID) is not None


def test_the_two_fetch_shapes_do_not_serve_each_other(monkeypatch):
    """`fetch_release` and `fetch_release_urls` ask MusicBrainz for different
    `inc=` parameters, so they are different payloads under one MBID. Serving
    one to a caller expecting the other is wrong data that still parses."""
    full = _Counter(_release())
    urls = _Counter(["https://artist.bandcamp.com/album/x"])
    monkeypatch.setattr(mb_lookup, "fetch_release", full)
    monkeypatch.setattr(mb_lookup, "fetch_release_urls", urls)

    mb_cache.fetch_release(MBID)
    got = mb_cache.fetch_release_urls(MBID)

    # The release fetch did not satisfy the URL fetch...
    assert urls.calls == 1
    assert got == ["https://artist.bandcamp.com/album/x"]
    # ...and each is cached in its own right.
    assert mb_cache.fetch_release_urls(MBID) == got
    assert urls.calls == 1
    assert full.calls == 1


def test_the_cache_key_survives_a_reordering_of_the_includes(monkeypatch):
    """The key is sorted, so shuffling `RELEASE_INCLUDES` cannot orphan every
    row written under the old order. The MusicBrainz request is
    order-insensitive and the key has to be too."""
    fetch = _Counter(_release())
    monkeypatch.setattr(mb_lookup, "fetch_release", fetch)
    mb_cache.fetch_release(MBID)

    monkeypatch.setattr(mb_lookup, "RELEASE_INCLUDES", tuple(reversed(mb_lookup.RELEASE_INCLUDES)))
    mb_cache.fetch_release(MBID)

    assert fetch.calls == 1


def test_a_row_stamped_in_the_future_is_treated_as_stale(monkeypatch):
    """A NAS whose clock jumps back after an NTP correction would otherwise hold
    a row that never expires. The worst a bad clock may cost is a request."""
    fetch = _Counter(_release())
    monkeypatch.setattr(mb_lookup, "fetch_release", fetch)
    mb_cache.fetch_release(MBID)
    conn = activity_store._ensure()
    conn.execute(
        "UPDATE mb_release_cache SET fetched_at = ?",
        ((datetime.now(UTC) + timedelta(days=1)).isoformat(),),
    )
    conn.commit()

    mb_cache.fetch_release(MBID)

    assert fetch.calls == 2


def test_a_release_gone_error_is_not_smothered_by_a_stale_row(monkeypatch):
    """A 404 is an ANSWER (#194/#210), and the album page has a whole response
    built for it. Serving the last good payload instead would hide a deletion
    for as long as the row survived — and the row outlives the TTL on purpose,
    so "as long as it survived" is forever.

    The row is aged past the TTL rather than the TTL being set to zero: zero
    would force a fetch trivially and prove nothing about the fallback. What has
    to be shown is that an EXPIRED row is not quietly used to paper over the
    error its refresh raised.
    """
    monkeypatch.setattr(mb_lookup, "fetch_release", _Counter(_release()))
    mb_cache.fetch_release(MBID)
    _age_rows(timedelta(hours=2))  # the TTL is one hour, so the row is expired

    def _gone(mbid):
        raise mb_lookup.ReleaseGoneError("no longer there")

    monkeypatch.setattr(mb_lookup, "fetch_release", _gone)

    with pytest.raises(mb_lookup.ReleaseGoneError):
        mb_cache.fetch_release(MBID)
    # ...and the row is still there, unmodified — expiry means "ask again",
    # never "forget", which is what makes it #32's change-detection baseline.
    assert mb_cache.fetched_at(MBID) is not None


def test_a_merged_release_is_cached_under_the_id_it_actually_has(monkeypatch):
    """MusicBrainz redirects a merged MBID (#268), so the payload that comes
    back carries a different id. Keying the row by what was ASKED for would
    cache the surviving release under a dead MBID — served to nobody, and
    leaving the real id uncached."""
    survivor = "99999999-8888-7777-6666-555555555555"
    fetch = _Counter(_release(mbid=survivor))
    monkeypatch.setattr(mb_lookup, "fetch_release", fetch)

    mb_cache.fetch_release(MBID)  # asked for the merged-away id

    assert mb_cache.fetched_at(survivor) is not None
    assert mb_cache.fetched_at(MBID) is None
    # And the surviving id now reads from cache rather than asking again.
    mb_cache.fetch_release(survivor)
    assert fetch.calls == 1


def test_an_unreachable_store_still_answers_from_musicbrainz(monkeypatch):
    """A degraded cache must degrade to "ask MusicBrainz", never to a broken
    album page. The failure is loud in the log; the user's request still works.
    """
    fetch = _Counter(_release())
    monkeypatch.setattr(mb_lookup, "fetch_release", fetch)

    def _boom(*a, **kw):
        raise activity_store.sqlite3.Error("database is locked")

    monkeypatch.setattr(activity_store, "_ensure", _boom)

    assert mb_cache.fetch_release(MBID)["title"] == "Geogaddi"
    assert fetch.calls == 1
