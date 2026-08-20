"""The deleted-release banner reaches the page, out of band (#210).

Out-of-band swaps are exactly what the Python suite cannot check: it sees the
response HTML and asserts the right markup came back, but not whether htmx
placed it in the right elements — or whether those elements exist on the page at
all. A typo in an id produces a perfectly correct-looking response that lands
nowhere.
"""

from __future__ import annotations

import os

import pytest

# Opt-in like every other module here: `make check` stays browser-free, and
# without this the playwright plugin's fixtures reach the default run and break
# unrelated asyncio tests.
pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_E2E") != "1", reason="e2e disabled (set RUN_E2E=1)"
)


def test_the_banner_and_the_disabled_retag_land_on_the_page(page, demo_server):
    """Demo seeds one album whose release is absent from the catalogue."""
    page.goto(f"{demo_server}/?tab=library")
    page.wait_for_load_state("networkidle")

    page.goto(f"{demo_server}/album/demo-rel-deleted")
    # The banner arrives with the /compare fetch, not with the page.
    banner = page.locator("text=This release is gone from MusicBrainz")
    banner.wait_for(timeout=10_000)

    assert banner.is_visible(), "OOB swap landed in #album-alert-*"

    retag = page.locator("#retag-btn-demo-rel-deleted")
    assert retag.is_disabled(), "OOB swap replaced the live Re-tag button"

    find = page.get_by_role("button", name="Find a new release")
    assert find.is_visible()


def test_a_healthy_album_gets_no_banner(page, demo_server):
    """The control — the OOB swaps must not fire for an album that is fine."""
    page.goto(f"{demo_server}/album/demo-rel-rural-juror")
    page.wait_for_selector("#album-tracks table, #album-tracks", timeout=10_000)

    assert page.locator("text=This release is gone from MusicBrainz").count() == 0
    assert not page.locator("#retag-btn-demo-rel-rural-juror").is_disabled()
