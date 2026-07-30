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
        yield base
    finally:
        proc.terminate()
        proc.wait(timeout=10)


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
