"""The TTL cache in front of the Cover Art Archive's check (#436).

The mirror of `test_mb_cache.py`, and it counts checks for the same reason: the
number of times the archive is asked *is* the feature. A cache that returns the
right answer while still asking has done nothing — and here the cost of asking is
sixteen seconds of somebody's album page rather than a rate-limit slot.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from harmonist import activity_store, caa_cache, cover_art

MBID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


@pytest.fixture(autouse=True)
def fresh_store_and_default_ttl(tmp_path):
    """A file-backed store per test, and the shipped TTL.

    `configure` is process-level state, exactly as it is for `mb_cache`: a test
    that changed it and did not put it back would quietly change how a LATER
    test's cache behaves, and tests run in random order.
    """
    activity_store.init(tmp_path / "activity.db")
    caa_cache.configure(timedelta(days=7))
    yield
    caa_cache.configure(timedelta(days=7))


class _Counter:
    """A stand-in check that records how many times the archive was asked."""

    def __init__(self, answer: activity_store.CachedCoverArt | None = None):
        self.answer = answer
        self.calls = 0
        self.known: list[activity_store.CachedCoverArt | None] = []

    def __call__(self, mbid, *, release_group_mbid=None, known=None, keep_if_wider_than=None):
        self.calls += 1
        self.known.append(known)
        return self.answer or activity_store.CachedCoverArt(
            fetched_at=datetime.now(UTC), image_url="https://caa.example/front.jpg", width=1400
        )


def _stored(age: timedelta, **kw) -> None:
    activity_store.store_cover_art(
        MBID, activity_store.CachedCoverArt(fetched_at=datetime.now(UTC) - age, **kw)
    )


def test_a_second_look_inside_the_ttl_does_not_ask_the_archive(monkeypatch):
    """The whole point. Opening an album page twice must cost one check."""
    check = _Counter()
    monkeypatch.setattr(cover_art, "check_front", check)

    first = caa_cache.front(MBID)
    second = caa_cache.front(MBID)

    assert check.calls == 1
    assert first.image_url == second.image_url == "https://caa.example/front.jpg"


def test_an_answer_past_the_ttl_is_asked_again(monkeypatch):
    check = _Counter()
    monkeypatch.setattr(cover_art, "check_front", check)
    _stored(timedelta(days=8), image_url="https://caa.example/old.jpg")

    answer = caa_cache.front(MBID)

    assert check.calls == 1
    assert answer.image_url == "https://caa.example/front.jpg"


def test_a_stale_answer_is_still_handed_back_for_its_etag(monkeypatch):
    """Expiry means "ask again", never "forget". The archive's ETag is a real
    content validator, so a re-check of unchanged art costs one request and no
    transfer — but only if the stale row goes back out with the question."""
    check = _Counter()
    monkeypatch.setattr(cover_art, "check_front", check)
    _stored(timedelta(days=30), etag='"unchanged"', image_url="https://caa.example/old.jpg")

    caa_cache.front(MBID)

    assert check.known[0] is not None
    assert check.known[0].etag == '"unchanged"'


def test_fresh_forces_a_check_the_stored_answer_would_have_served(monkeypatch):
    """What the album page's re-ask control passes. Serving it a stored answer
    would make the one control that means "look again" a silent no-op."""
    check = _Counter()
    monkeypatch.setattr(cover_art, "check_front", check)
    _stored(timedelta(minutes=1), image_url="https://caa.example/old.jpg")

    answer = caa_cache.front(MBID, max_age=caa_cache.FRESH)

    assert check.calls == 1
    assert answer.image_url == "https://caa.example/front.jpg"


def test_the_answer_is_stored_so_the_next_process_starts_warm(monkeypatch):
    check = _Counter()
    monkeypatch.setattr(cover_art, "check_front", check)

    caa_cache.front(MBID)

    kept = activity_store.cached_cover_art(MBID)
    assert kept is not None
    assert kept.image_url == "https://caa.example/front.jpg"


def test_nothing_in_the_archive_is_an_answer_that_stops_the_asking(monkeypatch):
    """ "No front cover" is the commonest answer for a private Bandcamp release,
    and #276 stores it. What makes caching that negative survivable is the TTL —
    so it must be served inside the window, and asked again outside it."""
    check = _Counter(activity_store.CachedCoverArt(fetched_at=datetime.now(UTC)))
    monkeypatch.setattr(cover_art, "check_front", check)

    caa_cache.front(MBID)
    caa_cache.front(MBID)

    assert check.calls == 1
    assert caa_cache.stored(MBID).has_art is False


def test_a_failed_check_leaves_the_previous_answer_and_its_timestamp(monkeypatch):
    """ "I could not ask" and "there is nothing there" must not be recorded as the
    same thing: the timestamp not moving is what tells the user the check did not
    happen."""

    def boom(mbid, **kw):
        raise cover_art.CoverArtError("the archive is down")

    monkeypatch.setattr(cover_art, "check_front", boom)
    _stored(timedelta(days=30), image_url="https://caa.example/old.jpg")
    before = activity_store.cached_cover_art(MBID)

    with pytest.raises(cover_art.CoverArtError):
        caa_cache.front(MBID)

    after = activity_store.cached_cover_art(MBID)
    assert after.image_url == "https://caa.example/old.jpg"
    assert after.fetched_at == before.fetched_at


def test_due_says_what_the_page_should_do_without_asking_anything(monkeypatch):
    """The page has to decide whether to send the check BEFORE anything leaves
    the machine, so this question must not answer it by making the request."""

    def boom(mbid, **kw):
        raise AssertionError("`due` asked the archive")

    monkeypatch.setattr(cover_art, "check_front", boom)

    assert caa_cache.due(MBID) is True  # never asked
    _stored(timedelta(days=30))
    assert caa_cache.due(MBID) is True  # asked, long ago
    _stored(timedelta(hours=1))
    assert caa_cache.due(MBID) is False  # asked within the window


def test_a_row_stamped_in_the_future_is_stale_rather_than_immortal(monkeypatch):
    """A NAS whose clock has just been corrected backwards must cost a request,
    not an answer that can never expire."""
    check = _Counter()
    monkeypatch.setattr(cover_art, "check_front", check)
    _stored(-timedelta(days=365))

    assert caa_cache.due(MBID) is True
    caa_cache.front(MBID)
    assert check.calls == 1


def test_a_zero_ttl_asks_every_time(monkeypatch):
    check = _Counter()
    monkeypatch.setattr(cover_art, "check_front", check)
    caa_cache.configure(timedelta(0))

    caa_cache.front(MBID)
    caa_cache.front(MBID)

    assert check.calls == 2
