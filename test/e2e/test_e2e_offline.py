"""The e2e browsers reach the servers under test and nothing else (#1028).

A page's `load` waits for its stylesheets and the fonts they start, and
`base.html` takes its web fonts from Google. So every `page.goto` waited on the
internet, and from the devcontainer the TLS handshake to Google sometimes stalls
for seconds: past a 5 s timeout, now and then. No test needs those fonts, and the
page falls back to the system's.
"""

import os
from urllib.parse import urlsplit

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("RUN_E2E") != "1", reason="e2e disabled")
pytest.importorskip("playwright.sync_api")


def _outside(url: str) -> bool:
    return urlsplit(url).hostname != "127.0.0.1"


@pytest.mark.parametrize("engine", ["chromium", "webkit"])
def test_pages_get_nothing_from_the_internet(browser_factory, demo_server, engine):
    page = browser_factory(engine).new_page()
    answered: list[str] = []
    refused: list[str] = []
    page.on("requestfinished", lambda r: answered.append(r.url) if _outside(r.url) else None)
    page.on("requestfailed", lambda r: refused.append(r.url) if _outside(r.url) else None)
    page.goto(demo_server)
    assert answered == []
    # The page does ask, for the web fonts, so the request was refused rather
    # than never made.
    assert refused
    page.context.close()
