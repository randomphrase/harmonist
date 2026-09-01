"""The Tracks header's "Show identifiers" control actually reveals them (#328).

The control moved out of the tracklist and into the section header, to match
History's "Show details" — under the table it sat below the last disc's block
and read as belonging to that disc. That move changed the one thing about it
that matters: it used to climb to its table with `closest('.tracklist')`, and
from the header it is OUTSIDE that wrapper, so it now reaches DOWN into the
section instead.

`test_web.py` can say the handler string is what we wrote and that both elements
are in the response. It cannot say the selector finds anything, which is the
whole question — a control that silently does nothing is the #40 bug class, and
#40 shipped with a green suite and 91% coverage.

Drives a browser via `sync_playwright()` opened per test, like every other module
here — see test_release_gone.py for why mixing in the pytest-playwright `page`
fixture kills the rest of the session.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_E2E") != "1", reason="e2e disabled (set RUN_E2E=1)"
)
playwright_sync = pytest.importorskip("playwright.sync_api")

# A COMPLETE album whose files agree with the catalogue, and whose per-track
# MusicBrainz ids differ from what the demo release states — so the identifier
# columns are earned, and hidden, which is the state this test needs.
ALBUM_ID = "demo-rel-rural-juror"


def test_the_header_control_reveals_the_identifier_columns(demo_server: str) -> None:
    with playwright_sync.sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()

        page.goto(f"{demo_server}/album/{ALBUM_ID}")
        page.wait_for_load_state("networkidle")

        toggle = page.locator("label:has-text('Show identifiers') input[type=checkbox]")
        toggle.wait_for(timeout=10_000)

        # The heading of an identifier column, which `.tracklist--ids` governs.
        # By CELL rather than by the word "Recording": that word is also in the
        # sentence beside the checkbox, so matching text alone would pass with
        # the columns still hidden — the #144 trap of asserting on a string that
        # occurs elsewhere in the markup.
        hidden = page.locator("th.track-diff__id").first
        hidden.wait_for(state="attached", timeout=10_000)
        assert not hidden.is_visible(), "identifiers start hidden, or there is nothing to reveal"

        toggle.check()
        assert hidden.is_visible(), "the control reaches its table from the section header"

        toggle.uncheck()
        assert not hidden.is_visible(), "and puts them back"

        browser.close()
