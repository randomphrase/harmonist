"""A request that fails without an answer is reported in the status bar (#1052).

An action that fails on the server says so itself, with `harmonist-status`. These
are the failures nothing answered for: a reverse proxy that gave up waiting, a
dropped connection, an error Harmonist raised without a message of its own. htmx
swaps none of them, so before #1052 the page showed nothing at all, as when a
long confirm outlived a proxy's 60 s timeout and its review was left as if Confirm
had not been pressed. Which events reach which listener, and in what order, is
the page's script; no rendered-HTML test can see it.

Opt-in: requires playwright (`pip install -e .[e2e]` + `playwright install
chromium`) and RUN_E2E=1. Run via `make e2e`.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from typing import Any

import pytest
from test.e2e.conftest import open_review

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_E2E") != "1", reason="e2e disabled (set RUN_E2E=1)"
)
pw = pytest.importorskip("playwright.sync_api")

SUGGESTED = "Gimme Some Money"
CONFIRM = "**/confirm/*/accept*"


def _review(page: Any, base: str) -> Any:
    """The suggestion's review, open over the Inbox; returns its Confirm."""
    page.goto(f"{base}/?tab=inbox")
    card = page.locator('div[id^="task-"].relative').filter(has_text=SUGGESTED)
    pw.expect(card).to_have_count(1)
    dialog = open_review(page, card)
    return dialog.get_by_role("button", name="Confirm suggestion", exact=True)


def _proxy_timeout(route: Any) -> None:
    route.fulfill(
        status=504,
        content_type="text/html",
        body="<html><body><h1>504 Gateway Time-out</h1></body></html>",
    )


def _dropped(route: Any) -> None:
    route.abort()


def _refused(route: Any) -> None:
    # FastAPI's answer to an HTTPException: JSON, and no harmonist-status.
    route.fulfill(status=400, json={"detail": "no candidate to confirm"})


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        (_proxy_timeout, ["No answer", "(504)", "may still have finished"]),
        (_dropped, ["No answer", "may still have finished"]),
        (_refused, ["Failed", "no candidate to confirm"]),
    ],
    ids=["proxy-timeout", "dropped", "refused"],
)
def test_a_confirm_that_fails_without_an_answer_is_reported(
    browser_factory: Any,
    demo_server: str,
    htmx_ready: Any,
    answer: Callable[[Any], None],
    expected: list[str],
) -> None:
    page = browser_factory("chromium").new_page(viewport={"width": 1280, "height": 900})
    page.route(CONFIRM, answer)
    confirm = _review(page, demo_server)
    htmx_ready(confirm).click()
    bar = page.locator("#harmonist-status")
    for text in expected:
        pw.expect(bar).to_contain_text(text)
    page.context.close()


def test_a_failure_harmonist_reported_keeps_its_own_words(
    browser_factory: Any, demo_server: str, htmx_ready: Any
) -> None:
    # `_flash_response(..., status_code=400)`: an error status that carries its
    # own message, which htmx raises before it reports the status as an error.
    report = {
        "harmonist-status": {"verb": "Tagging failed", "details": "disk full", "level": "error"}
    }
    page = browser_factory("chromium").new_page(viewport={"width": 1280, "height": 900})
    page.route(
        CONFIRM,
        lambda route: route.fulfill(
            status=400, headers={"HX-Trigger": json.dumps(report)}, body=""
        ),
    )
    confirm = _review(page, demo_server)
    htmx_ready(confirm).click()
    bar = page.locator("#harmonist-status")
    # Both reports land in the one task that handles the response, so the bar
    # never shows the first while the second is pending: what it settles on is
    # the server's.
    pw.expect(bar).to_contain_text("Tagging failed")
    pw.expect(bar).to_contain_text("disk full")
    page.context.close()


def test_a_poll_that_fails_is_not_reported(browser_factory: Any, demo_server: str) -> None:
    # Nobody is waiting on a poll, and while Harmonist restarts /status would
    # fail every 1.5 s.
    page = browser_factory("chromium").new_page(viewport={"width": 1280, "height": 900})
    page.goto(f"{demo_server}/?tab=inbox")
    page.route("**/status", lambda route: route.fulfill(status=502, body="Bad Gateway"))
    failed = page.evaluate_handle(
        """() => new Promise(resolve => document.body.addEventListener('htmx:afterRequest', e => {
            if (e.detail.elt.id === 'app-status' && !e.detail.successful) resolve(e.detail.xhr.status);
        }))"""
    )
    # htmx raises responseError while handling the response, before afterRequest.
    assert failed.json_value() == 502
    pw.expect(page.locator("#harmonist-status")).not_to_contain_text("No answer")
    page.context.close()
