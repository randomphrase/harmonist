"""Browser smoke test for modal actions (the layer unit tests can't see).

Issue #40 was invisible to the Python suite: the handler, template, and
route were all individually correct — only a real browser exercises the
onclick/htmx event ordering. This smoke test opens the album detail dialog
in demo mode and verifies that a destructive-ish action button actually
fires its confirm and its request.

Opt-in: requires playwright (`pip install -e .[e2e]` + `playwright install
chromium`) and RUN_E2E=1, so `make test` / `make check` stay green without
a browser. Run via `make e2e`.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_E2E") != "1", reason="e2e disabled (set RUN_E2E=1)"
)
playwright_sync = pytest.importorskip("playwright.sync_api")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="module")
def demo_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """Uvicorn in demo mode against throwaway dirs; yields the base URL."""
    root = tmp_path_factory.mktemp("e2e")
    port = _free_port()
    env = os.environ | {
        "HARMONIST_DEMO_MODE": "1",
        "HARMONIST_MUSIC_DIR": str(root / "music"),
        "HARMONIST_CONFIG_DIR": str(root / "config"),
    }
    (root / "music").mkdir()
    (root / "config").mkdir()
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "harmonist.web.main:app", "--port", str(port)],
        env=env,
        cwd=Path(__file__).resolve().parents[2],
    )
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=1):
                    break
            except OSError:
                time.sleep(0.3)
        else:
            pytest.fail("demo server did not come up")

        # Demo mode IGNORES HARMONIST_MUSIC_DIR: it always sandboxes the sample
        # library at a fixed $TMPDIR/harmonist-demo, shared by every run on this
        # machine. These tests mutate albums (a rematch sends one out of the
        # Library for good), so without a reset each run permanently consumes
        # part of the fixture and the suite eventually fails on an empty Library
        # — which looks exactly like a product bug. Reset so runs are repeatable.
        _reset_demo_library(base)
        yield base
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def _reset_demo_library(base: str) -> None:
    """Re-seed the shared demo library, then wait for the rescan to surface it."""
    req = urllib.request.Request(
        f"{base}/demo/reset", method="POST", headers={"HX-Request": "true"}
    )
    with urllib.request.urlopen(req, timeout=30):
        pass
    # The reset kicks a rescan; the Library is empty until it lands.
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        with urllib.request.urlopen(f"{base}/library", timeout=10) as r:
            if 'data-total-done="0"' not in r.read().decode():
                return
        time.sleep(0.5)
    pytest.fail("demo library did not re-seed with terminal albums")


def test_deep_link_opens_album_dialog_without_clobbering_saved_tab(demo_server: str) -> None:
    """#65: `/?album=<id>` must actually OPEN the dialog on load, not merely put
    the right markup on the page.

    The Python suite can assert the hx-get is rendered, but the open depends on
    a chain it structurally cannot see: htmx fires the load trigger, swaps the
    fragment into #modal, and base.html's afterSwap handler calls showModal().
    Any link in that chain can break with the template still asserting fine.

    Also pins the tab behaviour: a deep link shows the Library underneath, but
    must NOT rewrite the user's saved tab — following a link from Activity
    shouldn't quietly change where they land next time.
    """
    with playwright_sync.sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        page.goto(demo_server)

        # Discover a real album id the way the UI exposes it (tile → detail).
        page.click('[data-tab="library"]')
        tile = page.locator('#panel-library [hx-get*="/detail"]').first
        tile.wait_for(state="attached")
        hx_get = tile.get_attribute("hx-get") or ""
        album_id = hx_get.split("/library/")[1].split("/detail")[0]
        assert album_id

        # Set a deliberate preference, so clobbering it would be visible.
        page.evaluate("localStorage.setItem('harmonist-tab', 'activity')")

        # Follow the deep link as a fresh navigation (what an <a href> does).
        page.goto(f"{demo_server}/?album={album_id}")

        # The actual assertion: a native dialog is open and visible.
        page.locator("#modal dialog[open]").wait_for(state="visible")
        assert page.locator("#panel-library").is_visible()
        assert page.evaluate("localStorage.getItem('harmonist-tab')") == "activity"

        browser.close()


def test_missing_album_notice_dismisses_and_clears_the_url(demo_server: str) -> None:
    """#71: the notice must actually go away.

    Both halves are invisible to pytest, which only sees the markup: whether the
    dismiss button really removes the element, and whether `history.replaceState`
    really drops `?album=` (so a refresh doesn't resurrect a message about a
    navigation that already happened).
    """
    with playwright_sync.sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        page.goto(f"{demo_server}/?album=definitely-not-a-real-album-id")

        notice = page.locator("#deep-link-notice")
        notice.wait_for(state="visible")
        # The parameter is gone from the address bar before any interaction...
        assert "album=" not in page.url
        # ...and a reload therefore does not bring the notice back.
        page.reload()
        assert page.locator("#deep-link-notice").count() == 0

        # And the dismiss control removes it outright.
        page.goto(f"{demo_server}/?album=definitely-not-a-real-album-id")
        notice.wait_for(state="visible")
        page.click('#deep-link-notice button[aria-label="Dismiss"]')
        notice.wait_for(state="detached")

        browser.close()


def test_activity_album_link_actually_opens_the_album(demo_server: str) -> None:
    """The full round trip that shipped broken: do an action, then click the
    album link the Activity feed writes for it.

    The Python suite can prove the id is recorded and that `/` resolves it, but
    only this exercises the real chain end to end — action → recorded id →
    rendered link → resolve → dialog. The first cut recorded a pre-mutation id
    that tagging had already erased, so every link landed on the "isn't in your
    library any more" notice.
    """
    with playwright_sync.sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        page.goto(demo_server)

        # An action against a real album, so the feed gets an album-tagged entry.
        page.click('[data-tab="library"]')
        page.locator("#panel-library img").first.click()
        page.locator("#modal dialog[open]").wait_for(state="visible")
        page.on("dialog", lambda d: d.accept())
        with page.expect_request(lambda r: "/rematch" in r.url and r.method == "POST"):
            page.click('button[hx-post*="/rematch"]')
        page.locator("#modal dialog").wait_for(state="detached")

        # Its Activity entry must carry a link on the album name.
        page.click('[data-tab="activity"]')
        link = page.locator('#panel-activity a[href^="/?album="]').first
        link.wait_for(state="visible")
        link.click()

        # Following it opens the album — NOT the unresolvable-album notice.
        page.locator("#modal dialog[open]").wait_for(state="visible")
        assert "isn't in your library any more" not in page.content()

        browser.close()


def test_album_dialog_rematch_fires_confirm_and_post(demo_server: str) -> None:
    with playwright_sync.sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        page.goto(demo_server)

        # Library tab → first tile → native dialog opens.
        page.click('[data-tab="library"]')
        page.locator("#panel-library img").first.click()
        dialog = page.locator("#modal dialog[open]")
        dialog.wait_for(state="visible")

        # Accept the hx-confirm, then click the rematch pencil and require
        # that the POST is actually issued — the #40 regression check.
        page.on("dialog", lambda d: d.accept())
        with page.expect_request(lambda r: "/rematch" in r.url and r.method == "POST"):
            page.click('button[hx-post*="/rematch"]')

        # On success the dialog closes (hx-on::after-request) and the mount
        # is cleared by the close handler in base.html.
        page.locator("#modal dialog").wait_for(state="detached")
        browser.close()
