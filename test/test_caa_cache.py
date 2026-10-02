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


def _stored(age: timedelta, *, listed: bool = True, **kw) -> None:
    """A stored front answer — and, unless `listed=False`, the listing a check
    since #659 stores beside it, so a test of the front is not also a test of
    a listing nobody asked for."""
    activity_store.store_cover_art(
        MBID, activity_store.CachedCoverArt(fetched_at=datetime.now(UTC) - age, **kw)
    )
    if listed:
        activity_store.store_release(MBID, "caa-listing:release", {"images": []})


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

    assert caa_cache.due(MBID, keep_if_wider_than=0) is True  # never asked
    _stored(timedelta(days=30))
    assert caa_cache.due(MBID, keep_if_wider_than=0) is True  # asked, long ago
    _stored(timedelta(hours=1))
    assert caa_cache.due(MBID, keep_if_wider_than=0) is False  # asked within the window


def test_a_row_stamped_in_the_future_is_stale_rather_than_immortal(monkeypatch):
    """A NAS whose clock has just been corrected backwards must cost a request,
    not an answer that can never expire."""
    check = _Counter()
    monkeypatch.setattr(cover_art, "check_front", check)
    _stored(-timedelta(days=365))

    assert caa_cache.due(MBID, keep_if_wider_than=0) is True
    caa_cache.front(MBID)
    assert check.calls == 1


def test_a_zero_ttl_asks_every_time(monkeypatch):
    check = _Counter()
    monkeypatch.setattr(cover_art, "check_front", check)
    caa_cache.configure(timedelta(0))

    caa_cache.front(MBID)
    caa_cache.front(MBID)

    assert check.calls == 2


def test_release_only_absence_does_not_suppress_ordinary_group_fallback(monkeypatch):
    calls = []

    def check(mbid, *, release_group_mbid=None, known=None, **kw):
        calls.append((release_group_mbid, known))
        return activity_store.CachedCoverArt(
            fetched_at=datetime.now(UTC),
            image_url="https://caa.example/group.jpg" if release_group_mbid else None,
            source="release-group" if release_group_mbid else None,
        )

    monkeypatch.setattr(cover_art, "check_front", check)
    assert not caa_cache.front(MBID, release_only=True).has_art
    assert not caa_cache.front(MBID, release_only=True).has_art
    assert len(calls) == 1  # A release-only absence still benefits from the TTL.
    # The ordinary album page may still find group artwork.
    assert caa_cache.due(MBID, keep_if_wider_than=0)
    assert caa_cache.front(MBID, release_group_mbid="group").from_release_group
    assert caa_cache.front(MBID, release_group_mbid="group").has_art
    assert len(calls) == 2
    assert not caa_cache.front(MBID, release_only=True).has_art
    assert len(calls) == 3
    assert calls == [(None, None), ("group", None), (None, None)]


# ---------- a winner the image cache has since evicted (#439) ----------
#
# The image cache is capped now, so a fresh answer can outlive its picture. The
# measurement still stands; only the bytes are gone, and without them the
# Artwork section has no winner to show and no Update artwork to offer — for as
# long as the answer stays fresh, which is a week.


@pytest.fixture
def evicted_winner(tmp_path, monkeypatch):
    """A fresh answer whose image beat a 500px album, with the cache empty, and
    every call to the archive recorded rather than made."""
    monkeypatch.setattr(cover_art, "_caa_root", tmp_path / "caa")
    _stored(timedelta(hours=1), image_url="https://caa.example/front.jpg", width=1400)

    def no_check(mbid, **kw):
        raise AssertionError("a fresh answer was asked again")

    fetched: list[tuple[str, str]] = []

    def fetch(mbid, url, **kw):
        fetched.append((mbid, url))
        return cover_art.cache_image(mbid, b"\xff\xd8\xff", "image/jpeg")

    monkeypatch.setattr(cover_art, "check_front", no_check)
    monkeypatch.setattr(cover_art, "fetch_image", fetch)
    return fetched


def test_a_fresh_winner_whose_image_was_evicted_is_fetched_again(evicted_winner):
    """The picture, and only the picture: the stored answer is fresh, so the
    listing is not asked again."""
    assert caa_cache.due(MBID, keep_if_wider_than=500) is True

    caa_cache.front(MBID, keep_if_wider_than=500)

    assert evicted_winner == [(MBID, "https://caa.example/front.jpg")]
    assert cover_art.cached_image(MBID) is not None
    assert caa_cache.due(MBID, keep_if_wider_than=500) is False


def test_a_fresh_loser_is_not_fetched_because_it_is_missing(evicted_winner):
    """A losing image is never downloaded by a check (#276); it being absent is
    the ordinary state, not something to repair."""
    assert caa_cache.due(MBID, keep_if_wider_than=2000) is False

    caa_cache.front(MBID, keep_if_wider_than=2000)

    assert evicted_winner == []


def test_a_winner_is_not_fetched_with_the_image_cache_switched_off(evicted_winner, monkeypatch):
    """Nowhere to keep it, so it would be missing again on the very next open —
    a page that downloads megabytes to throw them away, every time."""
    monkeypatch.setattr(cover_art, "_caa_root", None)

    assert caa_cache.due(MBID, keep_if_wider_than=500) is False
    caa_cache.front(MBID, keep_if_wider_than=500)

    assert evicted_winner == []


# ---------- every image a listing offers (#659) ----------

CANDIDATES = [
    cover_art.Candidate(
        image_id="1",
        image_url="https://caa.example/1.jpg",
        thumbnail_url="https://caa.example/1-500.jpg",
        types=("front",),
        front=True,
    ),
    cover_art.Candidate(
        image_id="2",
        image_url="https://caa.example/2.jpg",
        thumbnail_url="https://caa.example/2.jpg",
        types=(),
        front=False,
    ),
]


class _Lister:
    """A stand-in listing that records how many times the archive was asked."""

    def __init__(self, answer: list[cover_art.Candidate] | Exception = CANDIDATES):
        self.answer = answer
        self.calls: list[tuple[str, str]] = []

    def __call__(self, kind, mbid, *, client=None):
        self.calls.append((kind, mbid))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def test_a_listing_is_asked_for_once_inside_the_ttl(monkeypatch):
    lister = _Lister()
    monkeypatch.setattr(cover_art, "fetch_listing", lister)

    first = caa_cache.listing("release", MBID)
    second = caa_cache.listing("release", MBID)

    assert lister.calls == [("release", MBID)]
    assert first == second == CANDIDATES


def test_a_release_and_its_group_are_listed_separately(monkeypatch):
    """One id can name a release and, in a test at least, a release group —
    the listings are different resources and must not answer for each other."""
    lister = _Lister()
    monkeypatch.setattr(cover_art, "fetch_listing", lister)

    caa_cache.listing("release", MBID)
    caa_cache.listing("release-group", MBID)

    assert lister.calls == [("release", MBID), ("release-group", MBID)]


def test_a_stored_listing_is_read_without_asking(monkeypatch):
    """What the page renders from: never the network, however old (#436)."""
    lister = _Lister()
    monkeypatch.setattr(cover_art, "fetch_listing", lister)

    assert caa_cache.stored_listing("release", MBID) is None
    caa_cache.listing("release", MBID)
    caa_cache.configure(timedelta(0))  # everything is stale now

    assert caa_cache.stored_listing("release", MBID) == CANDIDATES
    assert lister.calls == [("release", MBID)]


def test_a_fresh_answer_that_was_never_listed_is_due(monkeypatch):
    """An answer stored before the picker existed has a front and no listing
    (#659): it is asked again now, rather than offering nothing for a week."""
    _stored(timedelta(hours=1), listed=False, image_url="https://caa.example/front.jpg", width=100)
    assert caa_cache.due(MBID, keep_if_wider_than=1000) is True

    monkeypatch.setattr(cover_art, "fetch_listing", _Lister())
    caa_cache.listing("release", MBID)

    assert caa_cache.due(MBID, keep_if_wider_than=1000) is False


def test_a_listing_that_could_not_be_asked_stores_nothing(monkeypatch):
    """An outage is not "the archive has no images" (#458): the failure
    propagates, and the previous answer — here, none — stays."""
    monkeypatch.setattr(cover_art, "fetch_listing", _Lister(cover_art.CoverArtError("down")))

    with pytest.raises(cover_art.CoverArtError):
        caa_cache.listing("release", MBID)

    assert caa_cache.stored_listing("release", MBID) is None
