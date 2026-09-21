"""The public recording catalogue must work with the real artwork controls."""

import os

import pytest

from harmonist import demo, formats

pytestmark = pytest.mark.skipif(os.environ.get("RUN_E2E") != "1", reason="e2e disabled")
pw = pytest.importorskip("playwright.sync_api")


def test_public_smaller_candidate_is_explicit_and_undoable(public_demo_server):
    base, root = public_demo_server
    album = root / "Barry Jive and the Uptown Five" / "After Hours"
    files = sorted(album.glob("*.flac"))
    assert len(files) == 3
    original = [formats.read_cover(p) for p in files]
    cover = album / "cover.png"
    before = cover.read_bytes()
    candidate = (demo.ASSETS_DIR / "barry-archive.png").read_bytes()
    assert candidate != before
    with pw.sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.on("dialog", lambda dialog: dialog.accept())
        page.goto(f"{base}/album/demo-rel-barryjive")
        section = page.locator("#album-artwork")
        page.wait_for_selector("#album-artwork .art-row")
        load = section.locator('form[hx-post$="/artwork/load-archive"] button')
        pw.expect(load).to_be_visible()
        load.click()
        choose = section.get_by_role("button", name="Use the Cover Art Archive's artwork")
        pw.expect(choose).to_be_visible()
        # Looking at a smaller candidate is not permission to replace anything.
        assert cover.read_bytes() == before
        assert [formats.read_cover(p) for p in files] == original
        choose.click()
        apply = page.locator("#album-findings-demo-rel-barryjive").get_by_role(
            "button", name="Apply updates", exact=True
        )
        pw.expect(apply).to_be_visible()
        with page.expect_response(lambda r: r.url.endswith("/artwork/update")) as response:
            apply.click()
        assert response.value.ok
        pw.expect(apply).to_have_count(0)
        assert cover.read_bytes() == candidate
        assert all(formats.read_cover(p) == (candidate, "image/png") for p in files)
        page.reload()
        undo = page.get_by_role("button", name="Undo this artwork change", exact=True)
        with page.expect_response(lambda r: "/artwork/restore/" in r.url):
            undo.click()
        assert cover.read_bytes() == before
        assert [formats.read_cover(p) for p in files] == original
        browser.close()
