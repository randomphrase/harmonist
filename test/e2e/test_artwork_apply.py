"""The artwork a page shows is the artwork its buttons write (#469).

`test_web.py` proves the server half: a fingerprint that matches writes, one
that doesn't writes nothing. It cannot prove the browser half, and both halves
of this are browser questions:

- Re-tag from MB carries the fingerprint with `hx-include`, pointing at a hidden
  input that arrives in ANOTHER fragment's out-of-band swap. Whether HTMX finds
  it there when the button is pressed is exactly the kind of wiring a correct
  string in a response says nothing about (#40).
- Apply artwork posts the fingerprint from its own form. A form that lost the
  input would still render, still post, and be refused every time — the section
  redrawing itself with nothing written, which reads like a slow button.

Its own module, so its own demo server: pressing Apply writes into the demo
album, and `test_artwork_section.py` counts that album's images.
"""

from __future__ import annotations

import os
import re

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_E2E") != "1", reason="e2e disabled (set RUN_E2E=1)"
)
playwright_sync = pytest.importorskip("playwright.sync_api")

# The demo album seeded with `"art": "gaps"`: one track carries no image, so the
# plan has an addition to make.
ALBUM_ID = "demo-rel-dingoes"


def test_re_tag_carries_the_fingerprint_the_artwork_section_drew(demo_server: str) -> None:
    with playwright_sync.sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()

        page.goto(f"{demo_server}/album/{ALBUM_ID}")
        page.wait_for_selector("#album-artwork .art-row")

        # The out-of-band half has landed where the page reserved room for it.
        drawn = page.locator(f"#art-plan-{ALBUM_ID}").get_attribute("value")
        assert drawn and re.fullmatch(r"[0-9a-f]{64}", drawn), drawn

        # What HTMX would submit for every control that re-tags this album. Asked
        # of HTMX itself rather than by pressing one, which would re-tag the
        # demo album under the next test.
        carried = page.evaluate(
            """() => [...document.querySelectorAll('[hx-post^="/retag/"]')]
                    .map(el => htmx.values(el).art_plan)"""
        )
        assert carried, "no re-tag control on the page to carry anything"
        assert set(carried) == {drawn}

        browser.close()


def test_apply_updates_gathers_the_whole_artwork_request(demo_server: str) -> None:
    """The combined control's artwork half comes from the Artwork section's own
    element, across the DOM, via `hx-include` (#488).

    A browser question, and the #40 family exactly: the fields render correctly
    in both fragments whether or not HTMX ever associates them with the form, and
    a press that carried none of them would still POST, still re-tag, and simply
    write different artwork from the page it was pressed on — silently, because
    the endpoint's defaults are themselves valid.

    Asked of HTMX rather than by pressing, which would re-tag the demo album
    under the next test.
    """
    with playwright_sync.sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()

        page.goto(f"{demo_server}/album/{ALBUM_ID}")
        page.wait_for_selector("#album-artwork .art-row")
        # `attached`, not visible: these are hidden inputs, and the default wait
        # would sit here until it timed out on fields that are on the page.
        page.wait_for_selector(f".apply-art-{ALBUM_ID}", state="attached")

        carried = page.evaluate(
            """(id) => {
                const form = document.querySelector(`form[hx-post="/retag/${id}"]`);
                return form ? htmx.values(form) : null;
            }""",
            ALBUM_ID,
        )

        assert carried is not None, "no Apply updates form on the page"
        # The scope the fingerprint was taken at, and the slot the chosen image
        # travels in — both only reachable through `hx-include`.
        assert carried.get("artwork_scope") == "all"
        assert "use" in carried, "the choice slot must be on the wire, empty or not"
        # …and the fingerprint it carries is the one that element is showing,
        # rather than a copy this form captured when it was drawn.
        shown = page.evaluate(
            """(id) => document.querySelector(
                   `.apply-art-${id}[name="art_plan"]`).value""",
            ALBUM_ID,
        )
        assert carried.get("art_plan") == shown

        browser.close()


def test_apply_artwork_writes_what_the_section_showed(demo_server: str) -> None:
    with playwright_sync.sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        # A Replacement asks first; accept it, as the user pressing would.
        page.on("dialog", lambda dialog: dialog.accept())

        page.goto(f"{demo_server}/album/{ALBUM_ID}")
        page.wait_for_selector("#album-artwork .art-row")
        section = page.locator("#album-artwork")
        # Lower-cased: the column headings are set in capitals by CSS, and
        # `inner_text` reports what is rendered.
        assert "after apply" in section.inner_text().lower()

        button = section.get_by_role("button", name="Apply this album's artwork")
        with page.expect_response(re.compile(r"/artwork/update$")) as answer:
            button.click()
        assert answer.value.ok
        # The form's own fingerprint went with it…
        assert re.search(r"plan=[0-9a-f]{64}", answer.value.request.post_data or "")

        # …and it matched: the section redrew from the files it just wrote, with
        # nothing left to apply, rather than coming back to say the album had
        # changed under it.
        page.wait_for_function(
            "() => !document.querySelector('#album-artwork')"
            ".innerText.toLowerCase().includes('after apply')"
        )
        assert "changed after the page showed it" not in section.inner_text().lower()

        browser.close()
