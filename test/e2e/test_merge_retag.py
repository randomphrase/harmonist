"""Where the browser ends up after a re-tag that followed a merge (#375).

MusicBrainz merged the release this album names, so the re-tag writes the
SURVIVING release's id — and the page was opened at the old one. The page
refreshes itself off `album-retagged`, and refreshing in place left the browser
on an address the album had just stopped claiming: still resolvable through the
alias chain, so what came back was right and nothing looked wrong, while the
address bar and any bookmark taken from it named a superseded release.

This rung and no lower. `test_mb_merge.py` covers the header — that the response
names the album's new id — and that is exactly as far as pytest can see: whether
the page then *goes* there is a listener reading an event detail, which is the
#40 blind spot in its purest form. A rendered-HTML assertion would pass against a
handler that ignored the detail entirely.
"""

from __future__ import annotations

import os
from urllib.parse import urlsplit

import pytest

# Opt-in like every other module here: `make check` stays browser-free.
pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_E2E") != "1", reason="e2e disabled (set RUN_E2E=1)"
)
playwright_sync = pytest.importorskip("playwright.sync_api")

# Seeded COMPLETE, tagged as a release demo's MusicBrainz has merged away — its
# id is absent from MB_RELEASES and present in MERGED_INTO, which is the whole of
# what makes it a merge (demo.py). Its page therefore states one, and offers the
# re-tag that follows it.
MERGED_AWAY = "demo-rel-folksmen-dupe"
SURVIVOR = "demo-rel-folksmen"


def test_re_tagging_a_merged_release_leaves_the_browser_on_the_surviving_one(
    demo_server: str,
) -> None:
    with playwright_sync.sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()

        page.goto(f"{demo_server}/album/{MERGED_AWAY}?from_page=2")
        # The merge is discovered by the /compare fetch that lands after the
        # paint — a redirect is something MusicBrainz does, not something the
        # page can know at render time — so the note is what says it has arrived.
        page.wait_for_selector("text=merged this release", timeout=20_000)

        page.get_by_role("button", name="Re-tag from MB").first.click()

        # The address the album now lives at, not the one it was opened at.
        #
        # On the PATH, compared whole. A glob would not do here and the near miss
        # is worth naming: the surviving id is a PREFIX of the merged-away one, so
        # `**/album/demo-rel-folksmen*` matches the address this page started at
        # and the test passes with the bug reintroduced.
        page.wait_for_url(lambda url: urlsplit(url).path == f"/album/{SURVIVOR}", timeout=20_000)
        # …and the way back survives the move: `from_page` is the Library page
        # this album was opened from, and it is the only thing carrying it.
        assert "from_page=2" in page.url, page.url
        # The re-tag settled the merge, so the page it landed on says nothing
        # about one. Asserted after `wait_for_url` rather than instead of it: the
        # note is absent on a page still mid-fetch too.
        page.wait_for_selector("#album-tracks table", timeout=20_000)
        assert page.locator("text=merged this release").count() == 0

        browser.close()
