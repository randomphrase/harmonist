"""The e2e browsers reach the servers under test and nothing else (#1028).

`page.goto` waits for the page's `load`, which once included web fonts from
Google, and from the devcontainer the TLS handshake to Google sometimes stalled
for seconds: past a 5 s timeout, now and then. Harmonist serves its own fonts
now (#1029), and a page asks no other host for anything, but a test still
shouldn't depend on the internet if one ever does.
"""

import os

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("RUN_E2E") != "1", reason="e2e disabled")
pytest.importorskip("playwright.sync_api")


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_pages_get_nothing_from_the_internet(browser_factory, demo_server, engine):
    page = browser_factory(engine).new_page()
    page.goto(demo_server)
    outcome = page.evaluate(
        "url => fetch(url, {mode: 'no-cors'}).then(() => 'answered', () => 'refused')",
        "https://example.com/",
    )
    assert outcome == "refused"
    page.context.close()
