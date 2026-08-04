"""Browser tests for the Activity feed's live-update behaviour.

The feed re-polls every 2s while visible. Whether that swap *replaces* the DOM
or *patches* it is invisible to the Python suite — both produce identical HTML —
but it's the whole difference between a steady list and one that flickers, drops
text selection, and slams open disclosures shut.

Opt-in: requires playwright (`pip install -e .[e2e]` + `playwright install
chromium`) and RUN_E2E=1. Run via `make e2e`.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_E2E") != "1", reason="e2e disabled (set RUN_E2E=1)"
)
playwright_sync = pytest.importorskip("playwright.sync_api")


def test_loading_older_activity_survives_the_poll(demo_server: str) -> None:
    """#14: paging and a 2s poll are in direct conflict — the poll replaces the
    whole container, so without pausing it, the pages you just loaded are
    discarded a second or two later, mid-read.

    Only observable in a browser: the server has no idea a poll is pending.
    """
    with playwright_sync.sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        page.goto(demo_server)
        page.click('[data-tab="activity"]')

        page.wait_for_selector("#activity-feed li")

        # Stand in for appended page content with a sentinel. Deliberately NOT
        # driven through the Load-more button: how many entries demo happens to
        # have varies run to run (reset, reconcile and scans all append
        # asynchronously), so anything asserting on row counts is racy. The
        # guarantee under test is "a poll doesn't wipe what paging added", and a
        # sentinel tests exactly that, deterministically.
        add_sentinel = """
            const el = document.querySelector('#activity-feed');
            const li = document.createElement('li');
            li.id = 'paged-sentinel';
            el.querySelector('ul').appendChild(li);
        """

        # CONTROL: with the feed live AND something to send, a poll replaces the
        # container and takes the sentinel with it. Without this the test could
        # pass simply because no poll ever ran.
        #
        # The stale version is what makes it deterministic. Since #118 an idle
        # poll answers 204 and deliberately does NOT swap, so "a poll fired" no
        # longer implies "the container was replaced" — this control used to rely
        # on that and would now wait forever for a demo instance that has nothing
        # new to say. Corrupting the stashed version forces the mismatch a real
        # new entry would cause.
        page.evaluate("window.harmonistFeedPaged = false")
        page.evaluate("document.getElementById('feed-version').dataset.version = 'stale'")
        page.evaluate(add_sentinel)
        page.wait_for_timeout(4500)
        assert page.locator("#paged-sentinel").count() == 0, (
            "no poll swapped — the rest of this test would be vacuous"
        )

        # The real assertion: once paged, the poll is paused and content stays.
        page.evaluate("window.harmonistFeedPaged = true")
        page.evaluate(add_sentinel)
        page.wait_for_timeout(4500)
        assert page.locator("#paged-sentinel").count() == 1, (
            "a poll discarded the pages that had been loaded"
        )

        browser.close()


def test_status_pill_is_not_rebuilt_when_unchanged(demo_server: str) -> None:
    """#93: the header status pill must not be re-rendered on every 1.5s poll.

    `renderStatus()` assigned innerHTML unconditionally, which destroys and
    rebuilds the children even when the markup is byte-identical — 40 rebuilds a
    minute for as long as any message shows, and `latestActionMessage` persists
    after any action. Same node-identity check as the feed test below, for the
    same reason: the rendered HTML is identical either way.
    """
    with playwright_sync.sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        page.goto(demo_server)

        # Fire the event the action routes emit, so a pill exists to watch.
        page.evaluate(
            "document.body.dispatchEvent(new CustomEvent('harmonist-status',"
            "{detail:{verb:'Probe', details:'holding steady', level:'info'}}))"
        )
        pill = page.locator("#harmonist-status > *").first
        pill.wait_for(state="attached")
        page.evaluate(
            "window.__pill = document.querySelector('#harmonist-status').firstElementChild"
        )

        page.wait_for_timeout(3500)  # more than two 1.5s status polls

        same_node = page.evaluate(
            "document.querySelector('#harmonist-status').firstElementChild === window.__pill"
        )
        assert same_node, "the status pill was rebuilt despite unchanged content"

        browser.close()


def test_feed_poll_patches_the_dom_instead_of_rebuilding_it(demo_server: str) -> None:
    """#91: the 2s re-poll must morph, not replace.

    Asserted by node IDENTITY — the only thing that actually distinguishes the
    two, since both produce identical HTML. Stash a reference to a live row, wait
    for a poll to land, and check the row in the DOM is still the SAME element.
    Under `hx-swap="innerHTML"` it is a freshly built node, which is the flicker.

    Deliberately not asserted via a `data-*` attribute: idiomorph SYNCS
    attributes, so one absent from the incoming HTML is stripped even when the
    node itself is preserved — that check fails on a working morph.
    """
    with playwright_sync.sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        page.goto(demo_server)
        page.click('[data-tab="activity"]')

        row = page.locator("#activity-feed li").first
        row.wait_for(state="visible")
        page.evaluate("window.__firstRow = document.querySelector('#activity-feed li')")

        # Wait for at least one poll cycle (2s) to land.
        page.wait_for_timeout(3000)

        same_node = page.evaluate(
            "document.querySelector('#activity-feed li') === window.__firstRow"
        )
        assert same_node, "the feed rebuilt its rows instead of morphing them"

        browser.close()


def test_the_polling_feed_is_never_dimmed_by_its_own_poll(demo_server: str) -> None:
    """#118: `.htmx-request { opacity: .55; cursor: progress }` is global, and
    htmx puts that class on whatever issued the request — including a background
    poll. On a large library the feed then sat dimmed with a progress cursor for
    most of every 2s cycle, for work nobody asked for.

    Only a browser can answer this: it's a computed style under a CSS class htmx
    adds and removes on its own schedule, so the class is applied directly here
    rather than raced against a real request.
    """
    with playwright_sync.sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        page.goto(demo_server)
        page.click('[data-tab="activity"]')
        feed = page.locator("#activity-feed")
        feed.wait_for(state="visible")

        page.evaluate("document.getElementById('activity-feed').classList.add('htmx-request')")
        opacity = page.evaluate(
            "getComputedStyle(document.getElementById('activity-feed')).opacity"
        )
        assert float(opacity) == 1.0, f"the polling feed dims itself (opacity {opacity})"

        # Control: a non-polling element with the same class DOES still dim, or
        # the rule would have been removed rather than scoped.
        dimmed = page.evaluate(
            """(() => {
                const el = document.createElement('div');
                el.className = 'htmx-request';
                document.body.appendChild(el);
                return getComputedStyle(el).opacity;
            })()"""
        )
        assert float(dimmed) < 1.0, "the in-flight dim was removed entirely, not scoped"

        browser.close()


def test_an_idle_feed_stops_re_sending_itself(demo_server: str) -> None:
    """#118: the poll carries the version already on screen and the server
    answers 204, so an idle feed transfers nothing. Measured through the browser
    because it's the real poll — hx-vals reads the version out of the DOM."""
    with playwright_sync.sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        page.goto(demo_server)
        page.click('[data-tab="activity"]')
        page.locator("#activity-feed li").first.wait_for(state="visible")

        codes: list[int] = []
        page.on(
            "response",
            lambda r: codes.append(r.status) if "/activity" in r.url else None,
        )
        page.wait_for_timeout(7000)  # ~3 polls

        # NOT `all(...)`: demo start-up is still settling (a scan and reconcile
        # both write entries), and a poll that lands while something genuinely
        # happened SHOULD be a 200. Asserting the run ends quiet is the honest
        # claim, and it can't be broken by incidental start-up churn (#52 taught
        # us what a count-based assertion here costs).
        assert codes, "the feed stopped polling altogether"
        assert codes[-1] == 204, f"idle feed still re-sent itself: {codes}"

        browser.close()
