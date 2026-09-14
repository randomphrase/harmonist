"""The primary action stays consistent as artwork proposals change (#509)."""

from __future__ import annotations

import os
from urllib.parse import parse_qs

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("RUN_E2E") != "1", reason="e2e disabled")
pw = pytest.importorskip("playwright.sync_api")


def _settle(page) -> None:
    # Wait for HTMX to finish processing each swap before pressing another
    # control. These tests exercise completed transitions, not #477's race.
    page.wait_for_function("() => !document.querySelector('.htmx-request, .htmx-settling')")


def _choose_archive(page) -> None:
    section = page.locator("#album-artwork")
    load = section.locator('form[hx-post$="/artwork/load-archive"] button')
    if load.count():
        with page.expect_response(lambda r: r.url.endswith("/artwork/load-archive")):
            load.click()
        _settle(page)
    with page.expect_response(lambda r: r.url.endswith("/artwork?use=archive")):
        section.get_by_role("button", name="Use the Cover Art Archive's artwork").click()
    _settle(page)


def _covers(page) -> list[str]:
    return page.locator(
        "#album-artwork .art-row__side:not(.art-row__side--after) img"
    ).evaluate_all("els => els.map(el => el.getAttribute('src'))")


@pytest.mark.parametrize("included", [False, True])
def test_primary_action_honours_the_reviewed_artwork_scope(
    reset_demo_server: str, included: bool
) -> None:
    demo_server = reset_demo_server
    aid = "demo-rel-rural-juror"
    with pw.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.goto(f"{demo_server}/album/{aid}")
        page.wait_for_selector("#album-artwork .art-row")
        _settle(page)
        findings = page.locator(f"#album-findings-{aid}")
        before = _covers(page)
        assert before
        _choose_archive(page)

        inclusion = findings.get_by_role("checkbox", name="Include artwork")
        pw.expect(inclusion).to_be_visible()
        pw.expect(inclusion).to_be_checked()
        pw.expect(findings.locator(".sev--artwork")).to_be_visible()
        pw.expect(findings.get_by_role("button", name="Apply updates", exact=True)).to_have_count(1)
        inclusion.set_checked(included)

        # Keep the exclusion even across a temporarily empty proposal.
        page.locator('#album-artwork button[hx-get$="/artwork"]').click()
        _settle(page)
        _choose_archive(page)
        assert inclusion.is_checked() is included

        # An unrelated MB refresh must preserve both the chosen image and the
        # explicit exclusion. Drive the same HTMX refresh the page uses.
        page.evaluate(
            """id => htmx.ajax('GET', '/library/' + id + '/compare?check=1',
                             {target: '#compare-' + id, swap: 'innerHTML'})""",
            aid,
        )
        _settle(page)
        assert inclusion.is_checked() is included
        with page.expect_response(lambda r: r.url.endswith("/retag/" + aid)) as response:
            findings.get_by_role("button", name="Apply updates", exact=True).click()
        assert (
            parse_qs(response.value.request.post_data)["include_artwork"][-1]
            == str(included).lower()
        )
        page.wait_for_load_state("networkidle")
        page.wait_for_selector("#album-artwork .art-row")
        _settle(page)
        assert (_covers(page) != before) is included
        pw.expect(findings.locator(".album-tag-apply")).to_have_count(0)
        browser.close()


def test_artwork_only_primary_appears_and_clears_with_the_proposal(demo_server: str) -> None:
    aid = "demo-rel-dingoes"
    with pw.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        # Correct the metadata first; its initial artwork gap stays available.
        prepared = page.request.post(
            f"{demo_server}/retag/{aid}",
            headers={"HX-Request": "true"},
            form={"include_artwork": "false"},
        )
        assert prepared.ok
        page.goto(f"{demo_server}/album/{aid}")
        page.wait_for_selector("#album-artwork .art-row")
        _settle(page)
        findings = page.locator(f"#album-findings-{aid}")
        primary = findings.get_by_role("button", name="Apply updates", exact=True)
        pw.expect(primary).to_have_count(1)
        with page.expect_response(lambda r: r.url.endswith("/artwork/update")):
            primary.click()
        _settle(page)
        pw.expect(primary).to_have_count(0)

        _choose_archive(page)
        before = _covers(page)
        assert before
        pw.expect(primary).to_have_count(1)
        pw.expect(findings.locator(".sev--artwork")).to_be_visible()
        page.on("dialog", lambda dialog: dialog.accept())
        with page.expect_response(lambda r: r.url.endswith("/artwork/update")) as response:
            primary.click()
        assert parse_qs(response.value.request.post_data)["use"] == ["archive"]
        _settle(page)
        pw.expect(primary).to_have_count(0)
        assert _covers(page) != before
        browser.close()
