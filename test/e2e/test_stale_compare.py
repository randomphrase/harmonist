"""The comparison shows the stored release, then refreshes itself (#387).

The Python suite can prove the response: that a stale album renders its
comparison without asking MusicBrainz, and that the markup carries an
out-of-band trigger for the fresh one. It cannot prove the trigger FIRES — that
is the #40 class exactly, where markup asserted correct did nothing in a
browser, and it is the whole feature here: a trigger that never fires leaves the
user looking at last night's answer with nothing on its way and no sign that
anything is missing.

What is deliberately NOT here: the in-flight state — the spinner in the panel and
the hold on Re-tag and the ignore box. Demo mode answers MusicBrainz from a dict,
so the window those live in does not exist to be observed, and a test that waited
for it would be waiting on a race. Their markup is asserted in
`test_mb_cache_routes.py` and their behaviour is htmx's own
(`hx-indicator` / `hx-disabled-elt`), exercised by hand in demo mode.
"""

from __future__ import annotations

import os

import pytest

# Opt-in like every other module here: `make check` stays browser-free.
pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_E2E") != "1", reason="e2e disabled (set RUN_E2E=1)"
)
playwright_sync = pytest.importorskip("playwright.sync_api")

ALBUM = "demo-rel-dingoes"


def test_a_stale_comparison_refreshes_itself(stale_cache_server: str) -> None:
    """Open an album twice: the second view has a stored release to draw from,
    so it must draw it AND go and get a newer one."""
    with playwright_sync.sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        # htmx reports a selector that matched nothing to the console and carries
        # on, so a mistyped `hx-disabled-elt` / `hx-indicator` leaves a page that
        # works, holds nothing while the refresh runs, and says so only here.
        console: list[str] = []
        page.on("console", lambda m: console.append(m.text))

        # The first view is the one that fills the store — nothing is stale yet
        # because nothing has been read. Waiting for the comparison is what makes
        # the second view's precondition true rather than hoped for.
        page.goto(f"{stale_cache_server}/album/{ALBUM}")
        page.wait_for_selector(f"#compare-{ALBUM} .tag-fields", timeout=15_000)

        with page.expect_response(lambda r: "check=1" in r.url, timeout=15_000) as got:
            page.goto(f"{stale_cache_server}/album/{ALBUM}")
        assert got.value.ok, "the refresh behind the stale render never landed"

        # …and it was SWAPPED IN, not merely received: one response, several
        # destinations, and a mis-targeted swap would leave the Tags section
        # holding the answer this one was sent to replace.
        page.wait_for_selector(f"#compare-{ALBUM} .tag-fields", timeout=15_000)
        assert page.locator("#album-tracks .tracklist").is_visible()
        assert page.locator(
            f'#album-checked-{ALBUM} dd[title^="MusicBrainz release last read"]'
        ).is_visible()

        # The trigger retires itself with the swap. Left in place it would fire
        # again on every response it produced — a refresh loop against a
        # 1-req/sec budget, invisible in the markup of any single response.
        assert page.locator(f'#compare-{ALBUM} [hx-get*="check=1"]').count() == 0

        assert not [m for m in console if "returned no matches" in m], console

        browser.close()
