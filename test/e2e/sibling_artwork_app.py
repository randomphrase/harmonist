"""Sibling review against missing artwork, a cached group cover, or a CAA outage."""

import os
from datetime import UTC, datetime

from test.e2e import contribution_app

from harmonist import activity_store, cover_art, demo

app = contribution_app.app
SELECTED = "demo-rel-dingoes-digital"
SCENARIO = os.environ["HARMONIST_TEST_SIBLING_ARTWORK"]
_reset = demo.reset
_check = cover_art.check_front


def _group_answer():
    cover_art.cache_image(SELECTED, (demo.ASSETS_DIR / "wyld.png").read_bytes(), "image/png")
    return activity_store.CachedCoverArt(
        fetched_at=datetime.now(UTC),
        image_url="https://caa.example/group.png",
        source="release-group",
    )


def reset(*args, **kwargs):
    _reset(*args, **kwargs)
    if SCENARIO == "cached-group":
        activity_store.store_cover_art(SELECTED, _group_answer())


def check(release_mbid, *, release_group_mbid=None, **kwargs):
    if release_mbid != SELECTED:
        return _check(release_mbid, release_group_mbid=release_group_mbid, **kwargs)
    if SCENARIO == "failure":
        raise cover_art.CoverArtError("CAA unavailable")
    if release_group_mbid:
        return _group_answer()
    return activity_store.CachedCoverArt(fetched_at=datetime.now(UTC))


demo.reset = reset
cover_art.check_front = check
