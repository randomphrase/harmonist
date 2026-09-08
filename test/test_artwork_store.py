"""The content-addressed store for artwork a tagging overwrote (#131)."""

from __future__ import annotations

import os
import time

import pytest

from harmonist import activity_store, artwork_store

JPEG = b"\xff\xd8\xff" + b"a" * 200
PNG = b"\x89PNG\r\n\x1a\n" + b"b" * 200


@pytest.fixture(autouse=True)
def _store(tmp_path):
    activity_store.init(tmp_path / "activity.db")
    artwork_store.configure(tmp_path / "artwork")
    yield
    artwork_store.configure(None)


def test_keeping_an_image_makes_it_retrievable_by_digest():
    key = artwork_store.keep(JPEG, mime="image/jpeg")

    assert key == artwork_store.digest(JPEG)
    path = artwork_store.path_for(key)
    assert path is not None
    assert path.read_bytes() == JPEG
    # A real extension, because the user may well go looking in this directory
    # and 64 hex characters with no suffix is hostile to every image viewer.
    assert path.suffix == ".jpg"
    assert artwork_store.path_for(artwork_store.digest(PNG)) is None


def test_a_png_keeps_its_own_extension():
    key = artwork_store.keep(PNG, mime="image/png")
    assert key is not None
    path = artwork_store.path_for(key)
    assert path is not None and path.suffix == ".png"


def test_the_same_image_is_stored_once_however_many_tracks_shared_it(tmp_path):
    """The whole reason for content-addressing: an album whose eight tracks
    carry one cover costs one file, not eight."""
    for _ in range(8):
        artwork_store.keep(JPEG, mime="image/jpeg")

    assert len(list((tmp_path / "artwork").iterdir())) == 1


def test_an_unconfigured_store_is_a_no_op_rather_than_an_error():
    """Failing to keep a backup must never stop the tagging it was backing up."""
    artwork_store.configure(None)

    assert artwork_store.keep(JPEG) is None
    assert artwork_store.path_for(artwork_store.digest(JPEG)) is None
    assert artwork_store.usage() == (0, artwork_store.DEFAULT_MAX_BYTES)


def test_an_unwritable_store_reports_failure_instead_of_raising(tmp_path, monkeypatch):
    """A re-tag the user asked for must not be abandoned because the undo store
    is full or read-only. None says honestly that no copy was kept."""

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(artwork_store.Path, "write_bytes", boom)

    assert artwork_store.keep(JPEG, mime="image/jpeg") is None


def test_the_size_cap_evicts_the_oldest_images_first(tmp_path):
    """Unbounded artwork on a NAS is a slow leak, so the store has a ceiling.
    Oldest-first because the oldest changes are the least likely to be undone."""
    artwork_store.configure(tmp_path / "artwork", max_bytes=600)
    images = [bytes([i]) * 250 for i in range(4)]

    keys: list[str] = []
    for i, data in enumerate(images):
        key = artwork_store.keep(data, mime="image/jpeg")
        assert key is not None
        keys.append(key)
        # Distinct mtimes, so "oldest" is well-defined rather than filesystem luck.
        path = artwork_store.path_for(key)
        assert path is not None
        os.utime(path, (1000 + i, 1000 + i))
        artwork_store._evict_if_over_cap()

    used, cap = artwork_store.usage()
    assert cap == 600
    assert used <= 600
    # The two oldest are gone; the newest survives.
    assert artwork_store.path_for(keys[0]) is None
    assert artwork_store.path_for(keys[-1]) is not None


def test_re_keeping_an_image_saves_it_from_being_evicted_as_old(tmp_path):
    """Two albums can share an image — a label's house sleeve, a reissue. Without
    refreshing it on re-keep, a change made TODAY would be evicted before changes
    made months ago, because the file is old even though the change is not."""
    artwork_store.configure(tmp_path / "artwork", max_bytes=600)
    shared, only_old, newest = b"s" * 250, b"o" * 250, b"n" * 250

    shared_key = artwork_store.keep(shared, mime="image/jpeg")
    old_key = artwork_store.keep(only_old, mime="image/jpeg")
    assert shared_key is not None and old_key is not None
    for key, when in ((shared_key, 1000), (old_key, 1001)):
        path = artwork_store.path_for(key)
        assert path is not None
        os.utime(path, (when, when))

    # A second album replaces the SAME image — the change is new even though the
    # stored file is not.
    artwork_store.keep(shared, mime="image/jpeg")
    artwork_store.keep(newest, mime="image/jpeg")

    assert artwork_store.path_for(shared_key) is not None, "a fresh change was evicted"
    assert artwork_store.path_for(old_key) is None


def test_eviction_is_audited_because_it_deletes_the_last_copy(tmp_path):
    """Harmonist removing the only remaining copy of one of the user's images is
    exactly what the audit log is for — even under a policy the user set."""
    from harmonist.activity_store import Source

    artwork_store.configure(tmp_path / "artwork", max_bytes=300)
    artwork_store.keep(b"x" * 250, mime="image/jpeg")
    time.sleep(0.01)
    artwork_store.keep(b"y" * 250, mime="image/jpeg")

    messages = [e.message for e in activity_store.recent(50, source=Source.AUDIT)]
    assert any(m.startswith("artwork.keep") for m in messages)
    assert any(m.startswith("artwork.evict") for m in messages)


def test_a_zero_cap_keeps_nothing():
    """A legitimate choice on a volume with no room: artwork replacement stops
    being reversible, and nothing accumulates."""
    artwork_store.configure(artwork_store._root, max_bytes=0)
    key = artwork_store.keep(JPEG, mime="image/jpeg")

    assert key is not None  # the write happened...
    assert artwork_store.path_for(key) is None  # ...and was immediately evicted


@pytest.mark.parametrize(
    "key",
    [
        "../../../etc/passwd",
        "a" * 63,
        "a" * 65,
        "Z" * 64,
        "*",
        "",
    ],
)
def test_a_key_that_is_not_a_digest_never_reaches_the_filesystem(key):
    """`path_for` takes its argument from a stored record, and the lookup is a
    glob. A value carrying a separator or a wildcard must not be joined to a
    path — history is permanent and unversioned, so a malformed one WILL turn
    up eventually."""
    assert artwork_store.path_for(key) is None


def test_a_partial_write_is_never_visible_under_its_digest(tmp_path):
    """Written via a temp file then renamed, so a crash can't leave half an
    image under a digest that claims to be complete."""
    artwork_store.keep(JPEG, mime="image/jpeg")
    leftovers = [p for p in (tmp_path / "artwork").iterdir() if p.name.endswith(".tmp")]

    assert leftovers == []
    # And a stray .tmp is never served as if it were the image.
    (tmp_path / "artwork" / f"{artwork_store.digest(PNG)}.jpg.tmp").write_bytes(b"half")
    assert artwork_store.path_for(artwork_store.digest(PNG)) is None


# ---------------------------------------------------------------------------
# Per-album retention (#408) — the promise the store actually makes
# ---------------------------------------------------------------------------


def _replaced(album_id: str, before: bytes, after: bytes = b"new") -> int:
    """Record a tagging on `album_id` that replaced `before` with `after`, the
    way `tagger` does — an audit row plus its per-file change detail."""
    event_id = activity_store.append(
        message="tag.track file=01.m4a",
        level=activity_store.Level.INFO,
        source=activity_store.Source.AUDIT,
        album_id=album_id,
    )
    assert event_id is not None
    activity_store.record_tag_changes(
        event_id,
        file="01.m4a",
        changes={"artwork": [artwork_store.digest(before), artwork_store.digest(after)]},
    )
    return event_id


def _image(seed: int) -> bytes:
    return b"\xff\xd8\xff" + bytes([seed]) * 4000


class TestProtectedDigests:
    def test_an_albums_recent_changes_are_protected(self):
        one, two = _image(1), _image(2)
        _replaced("album-a", one)
        _replaced("album-a", two)

        protected = artwork_store.protected_digests()

        assert artwork_store.digest(one) in protected
        assert artwork_store.digest(two) in protected

    def test_only_the_last_n_per_album(self):
        artwork_store.configure(artwork_store._root, keep_per_album=2)
        images = [_image(i) for i in range(4)]
        for img in images:  # oldest first
            _replaced("album-a", img)

        protected = artwork_store.protected_digests()

        # The two most recent survive; the two before them are spendable.
        assert {artwork_store.digest(i) for i in images[2:]} <= protected
        assert not {artwork_store.digest(i) for i in images[:2]} & protected

    def test_each_album_gets_its_own_allowance(self):
        """A busy album must not spend another album's protection — the whole
        failure the global byte cap had."""
        artwork_store.configure(artwork_store._root, keep_per_album=1)
        mine = _image(1)
        _replaced("album-mine", mine)
        for i in range(5):
            _replaced("album-busy", _image(10 + i))

        assert artwork_store.digest(mine) in artwork_store.protected_digests()

    def test_art_a_track_gained_is_not_a_backup(self):
        """Nothing was replaced, so nothing was kept — `artwork_replaced` reads
        the same pair the same way."""
        event_id = activity_store.append(
            message="tag.track file=01.m4a",
            level=activity_store.Level.INFO,
            source=activity_store.Source.AUDIT,
            album_id="album-a",
        )
        assert event_id is not None
        activity_store.record_tag_changes(
            event_id, file="01.m4a", changes={"artwork": [None, "abc"]}
        )

        assert artwork_store.protected_digests() == frozenset()


class TestEviction:
    def test_a_protected_image_outlives_an_unprotected_older_one(self):
        """The point of the two passes: being old is not what decides."""
        old_unprotected, protected = _image(1), _image(2)
        artwork_store.keep(old_unprotected)
        time.sleep(0.01)
        artwork_store.keep(protected)
        _replaced("album-a", protected)
        # A cap that forces exactly one of the two out.
        artwork_store.configure(artwork_store._root, max_bytes=len(protected) + 100)

        artwork_store.keep(_image(3))  # any keep triggers the sweep

        assert artwork_store.path_for(artwork_store.digest(protected)) is not None
        assert artwork_store.path_for(artwork_store.digest(old_unprotected)) is None

    def test_the_cap_still_wins_when_everything_is_protected(self):
        """The backstop. A promise the disk cannot keep is not kept — but it is
        said out loud, because the UI has been offering that Undo."""
        first, second = _image(1), _image(2)
        artwork_store.keep(first)
        _replaced("album-a", first)
        time.sleep(0.01)
        _replaced("album-b", second)
        artwork_store.configure(artwork_store._root, max_bytes=len(second) + 100)

        artwork_store.keep(second)

        assert artwork_store.path_for(artwork_store.digest(first)) is None
        assert artwork_store.path_for(artwork_store.digest(second)) is not None

    def test_an_unreadable_store_evicts_oldest_first_rather_than_nothing(self, monkeypatch):
        """Empty protection is the safe direction: eviction falls back to what it
        did before #408 instead of letting the store grow past its cap."""
        monkeypatch.setattr(activity_store, "artwork_backups", list)
        old, new = _image(1), _image(2)
        artwork_store.keep(old)
        time.sleep(0.01)
        _replaced("album-a", old)
        artwork_store.configure(artwork_store._root, max_bytes=len(new) + 100)

        artwork_store.keep(new)

        assert artwork_store.path_for(artwork_store.digest(old)) is None
