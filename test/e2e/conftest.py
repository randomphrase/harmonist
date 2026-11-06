"""Shared fixtures for the opt-in browser (e2e) suite.

Opt-in: requires playwright (`pip install -e .[e2e]` + `playwright install
chromium webkit`) and RUN_E2E=1, so `make test` / `make check` stay browser-free.
Run via `make e2e`.
"""

from __future__ import annotations

import itertools
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import warnings
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_E2E") != "1", reason="e2e disabled (set RUN_E2E=1)"
)
playwright_sync = pytest.importorskip("playwright.sync_api")

# Where to keep a Playwright trace of each failing test, or unset for none. CI
# sets it, because a browser failure there otherwise reads "timeout waiting for
# selector" and nothing else; locally, open a kept trace with
# `playwright show-trace <zip>`.
_TRACE_DIR = os.environ.get("E2E_TRACE_DIR")
_CALL_REPORT = pytest.StashKey[pytest.TestReport]()


@pytest.fixture(scope="session")
def _browser_pool(playwright):
    """One driver and at most one browser per engine in each pytest worker."""
    browsers = {}

    def acquire(engine: str = "chromium"):
        if engine not in browsers:
            browsers[engine] = getattr(playwright, engine).launch()
        return browsers[engine]

    yield acquire
    for browser in browsers.values():
        browser.close()


@pytest.fixture
def browser_factory(_browser_pool, _trace_on_failure, monkeypatch, request):
    """Reuse processes, but close every test's isolated contexts before tracing ends.

    Browser.new_page creates a fresh context, so cookies, storage, routes, clocks,
    and page listeners stay local to the test. Cleanup also runs after failures.
    """
    acquired = {}
    close_context = playwright_sync.BrowserContext.close

    def close(self, *args, **kwargs):
        # A route.fetch callback can still be reading its response when the test
        # ends. Finish it before disposal; otherwise its error reaches the next
        # test through the shared driver. Removing routes also stops new work.
        report = request.node.stash.get(_CALL_REPORT, None)
        # A failed route callback can be the frame unwinding the test itself.
        # Waiting for that frame here deadlocks. The failure is already reported;
        # discard only subsequent disposal errors from its abandoned callbacks.
        behavior = "ignoreErrors" if report is not None and report.failed else "wait"
        for page in self.pages:
            page.unroute_all(behavior=behavior)
        self.unroute_all(behavior=behavior)
        return close_context(self, *args, **kwargs)

    monkeypatch.setattr(playwright_sync.BrowserContext, "close", close)

    def acquire(engine: str = "chromium"):
        browser = _browser_pool(engine)
        acquired[engine] = browser
        return browser

    yield acquire
    for browser in acquired.values():
        for context in browser.contexts:
            context.close()


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[None]
) -> Generator[None, pytest.TestReport, pytest.TestReport]:
    report = yield
    if report.when == "call":
        item.stash[_CALL_REPORT] = report
    return report


@pytest.fixture(autouse=True)
def _trace_on_failure(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch):
    """Trace every browser context a test opens; keep the traces if it fails.

    pytest-playwright's own `--tracing` only sees its `page` fixture. These tests
    open isolated contexts on pooled browsers, so wrap context creation and
    closure. browser_factory cleans up failed tests before this fixture ends.
    """
    if not _TRACE_DIR:
        yield
        return

    out = Path(_TRACE_DIR) / re.sub(r"[^\w.-]+", "_", request.node.nodeid)
    open_contexts: list[Any] = []
    numbers = itertools.count(1)

    def start(context: Any) -> None:
        if any(c is context for c in open_contexts):
            return
        context.tracing.start(title=request.node.nodeid, screenshots=True, snapshots=True)
        open_contexts.append(context)

    def save(context: Any) -> None:
        for i, c in enumerate(open_contexts):
            if c is context:
                del open_contexts[i]
                break
        else:
            return
        path = out / f"trace-{next(numbers)}.zip"
        try:
            context.tracing.stop(path=path)
        except playwright_sync.Error as error:
            # A lost trace must not replace the test's own failure with this one.
            warnings.warn(f"could not save {path}: {error}", stacklevel=1)

    browser_cls = playwright_sync.Browser
    context_cls = playwright_sync.BrowserContext
    manager_cls = type(playwright_sync.sync_playwright())
    new_context = browser_cls.new_context
    new_page = browser_cls.new_page
    close_browser = browser_cls.close
    close_context = context_cls.close
    exit_manager = manager_cls.__exit__

    def traced_new_context(self: Any, *args: Any, **kwargs: Any) -> Any:
        context = new_context(self, *args, **kwargs)
        start(context)
        return context

    def traced_new_page(self: Any, *args: Any, **kwargs: Any) -> Any:
        page = new_page(self, *args, **kwargs)
        start(page.context)
        return page

    def saving_close_context(self: Any, *args: Any, **kwargs: Any) -> Any:
        save(self)
        return close_context(self, *args, **kwargs)

    def saving_close_browser(self: Any, *args: Any, **kwargs: Any) -> Any:
        for context in self.contexts:
            save(context)
        return close_browser(self, *args, **kwargs)

    def saving_exit_manager(self: Any, *args: Any) -> Any:
        while open_contexts:
            save(open_contexts[0])
        return exit_manager(self, *args)

    monkeypatch.setattr(browser_cls, "new_context", traced_new_context)
    monkeypatch.setattr(browser_cls, "new_page", traced_new_page)
    monkeypatch.setattr(browser_cls, "close", saving_close_browser)
    monkeypatch.setattr(context_cls, "close", saving_close_context)
    monkeypatch.setattr(manager_cls, "__exit__", saving_exit_manager)
    yield

    report = request.node.stash.get(_CALL_REPORT, None)
    if report is not None and not report.failed:
        shutil.rmtree(out, ignore_errors=True)


@pytest.fixture
def htmx_complete():
    """Arm a scoped lifecycle observation before the action that must complete."""

    @contextmanager
    def observe(page, target: str, event: str = "htmx:afterSettle"):
        completed = page.evaluate_handle(
            """([target, event]) => {
                const state = {done: false, xhr: null};
                const started = e => {
                    if (e.detail.target?.id !== target || state.xhr) return;
                    state.xhr = e.detail.xhr;
                    document.removeEventListener('htmx:beforeRequest', started);
                };
                const listener = e => {
                    if (!state.xhr || e.detail.xhr !== state.xhr) return;
                    state.done = true;
                    document.removeEventListener(event, listener);
                };
                document.addEventListener('htmx:beforeRequest', started);
                document.addEventListener(event, listener);
                return state;
            }""",
            [target, event],
        )
        yield
        page.wait_for_function("state => state.done", arg=completed, timeout=10_000)
        completed.dispose()

    return observe


@pytest.fixture
def htmx_ready():
    """Keep actionability polling scoped to a control whose fragment is wired.

    HTMX inserts visible HTML before processing its handlers. Include ancestors
    so both in-band and out-of-band swaps are covered, and resolve the locator
    again after reloads. Unrelated Inbox polling must not keep a control waiting.
    """

    def ready(locator):
        return locator.and_(
            locator.page.locator(
                ":not(.htmx-added, .htmx-added *, .htmx-settling, .htmx-settling *)"
            )
        )

    return ready


def review_dialog(page):
    """The release review, open or opening in the page's modal mount (#933)."""
    return page.locator("dialog.review-dialog")


def open_review(page, host, release: str | None = None):
    """Open the Review dialog from `host`, an Inbox card or the album page's
    actions section (#933).

    By default from the suggestion waiting there; with `release`, from the
    paste box, the way a user reviews a release they chose. Returns the dialog
    once it is open; what it must have loaded is the caller's to wait for.
    """
    ready = page.locator(":not(.htmx-added, .htmx-added *, .htmx-settling, .htmx-settling *)")
    if release is None:
        row = host.get_by_role("listitem", name="Suggested release")
        row.get_by_role("button", name="Review", exact=True).and_(ready).click()
    else:
        host.get_by_role("radio", name="By MBID", exact=True).and_(ready).check()
        paste = host.locator('form[hx-get^="/review/"]')
        paste.get_by_placeholder("MB release URL or 36-char MBID").fill(release)
        paste.get_by_role("button", name="Review", exact=True).and_(ready).click()
    dialog = review_dialog(page)
    playwright_sync.expect(dialog).to_be_visible()
    return dialog


@pytest.fixture(scope="module")
def demo_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """Uvicorn in demo mode against throwaway dirs; yields the base URL."""
    yield from _run_demo_server(tmp_path_factory.mktemp("e2e"))


@pytest.fixture(scope="module")
def pregap_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    yield from _run_demo_server(
        tmp_path_factory.mktemp("e2e-pregap"), app_module="test.e2e.pregap_app:app"
    )


@pytest.fixture(scope="module")
def album_formats_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    yield from _run_demo_server(
        tmp_path_factory.mktemp("e2e-formats"), app_module="test.e2e.album_formats_app:app"
    )


@pytest.fixture(scope="module")
def library_filters_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    yield from _run_demo_server(
        tmp_path_factory.mktemp("e2e-filters"), app_module="test.e2e.library_filters_app:app"
    )


@pytest.fixture(scope="module")
def bulk_apply_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    yield from _run_demo_server(
        tmp_path_factory.mktemp("e2e-bulk-apply"), app_module="test.e2e.bulk_apply_app:app"
    )


@pytest.fixture(scope="module")
def stale_cache_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """A demo server whose stored MusicBrainz releases are stale the moment they
    are written (#387).

    A zero TTL means "never serve a stored answer without asking again", which
    is the state an album read yesterday is in — `mb_cache.due` is true either
    way. Reaching it by config rather than by backdating a row keeps the test out
    of the server's database, and makes EVERY album on this server a case rather
    than one prepared album.

    Here rather than in the module that uses it because `test/e2e` is not a
    package, so a test module cannot import from this one — fixtures are the only
    thing it can share.
    """
    yield from _run_demo_server(
        tmp_path_factory.mktemp("e2e-stale"),
        config_toml="[musicbrainz]\ncache_ttl_seconds = 0\n",
    )


@pytest.fixture(scope="module", params=[3600, 0], ids=["fresh", "stale"])
def contribution_server(
    tmp_path_factory: pytest.TempPathFactory, request: pytest.FixtureRequest
) -> Iterator[tuple[str, bool]]:
    for base in _run_demo_server(
        tmp_path_factory.mktemp("e2e-contributions"),
        app_module="test.e2e.contribution_app:app",
        config_toml=f"[musicbrainz]\ncache_ttl_seconds = {request.param}\n",
    ):
        yield base, request.param == 0


@pytest.fixture
def reset_demo_server(demo_server: str) -> str:
    """Start each mutating scenario from the same on-disk demo library."""
    _reset_demo_library(demo_server)
    return demo_server


@pytest.fixture
def write_progress_server(tmp_path):
    yield from _run_demo_server(tmp_path, app_module="test.e2e.write_progress_app:app")


@pytest.fixture
def background_write_server(tmp_path):
    """A demo server a test can write into from outside any page (#975)."""
    yield from _run_demo_server(tmp_path, app_module="test.e2e.background_write_app:app")


@pytest.fixture(params=["linked", "absent", "empty"])
def digital_contribution_server(tmp_path, monkeypatch, request):
    monkeypatch.setenv("HARMONIST_TEST_DIGITAL_CONTRIBUTION", request.param)
    for base in _run_demo_server(
        tmp_path,
        app_module="test.e2e.contribution_app:app",
        config_toml="[musicbrainz]\ncache_ttl_seconds = 0\n",
    ):
        yield base, request.param


@pytest.fixture
def release_match_server(tmp_path):
    """One release-match scenario per downloaded demo album (#618)."""
    yield from _run_demo_server(tmp_path, app_module="test.e2e.release_match_app:app")


@pytest.fixture
def release_picker_server(tmp_path):
    yield from _run_demo_server(tmp_path, app_module="test.e2e.release_picker_app:app")


@pytest.fixture
def release_picker_no_siblings_server(tmp_path, monkeypatch):
    monkeypatch.setenv("HARMONIST_TEST_NO_SIBLINGS", "1")
    yield from _run_demo_server(tmp_path, app_module="test.e2e.release_picker_app:app")


@pytest.fixture(params=["absent", "cached-group", "failure"])
def sibling_artwork_server(tmp_path, monkeypatch, request):
    monkeypatch.setenv("HARMONIST_TEST_SIBLING_ARTWORK", request.param)
    for base in _run_demo_server(tmp_path, app_module="test.e2e.sibling_artwork_app:app"):
        yield base, request.param


@pytest.fixture(scope="module")
def carousel_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """Every release lists a front, a back and a second front (#659)."""
    yield from _run_demo_server(
        tmp_path_factory.mktemp("e2e-carousel"), app_module="test.e2e.artwork_carousel_app:app"
    )


@pytest.fixture
def artwork_watch_server(tmp_path):
    yield from _run_demo_server(tmp_path, app_module="test.e2e.artwork_watch_app:app")


@pytest.fixture
def reset_carousel_server(carousel_server: str) -> str:
    """Each carousel scenario from a library and history nobody has stepped
    through: a release listed by one test would otherwise be in the next one's
    carousel before it steps."""
    _reset_demo_library(carousel_server)
    return carousel_server


@pytest.fixture(
    params=[
        "identical",
        "same-size-off",
        "same-size-on",
        "protected",
        "protected-folder",
        "tracks-only",
    ]
)
def confirmation_outcome_server(tmp_path, monkeypatch, request):
    monkeypatch.setenv("HARMONIST_TEST_ARTWORK_OUTCOME", request.param)
    for base in _run_demo_server(tmp_path, app_module="test.e2e.confirmation_outcome_app:app"):
        yield base, request.param, tmp_path / "harmonist-demo"


@pytest.fixture(scope="module")
def artwork_inspection_server(tmp_path_factory):
    yield from _run_demo_server(
        tmp_path_factory.mktemp("e2e-inspection"),
        app_module="test.e2e.artwork_inspection_app:app",
    )


@pytest.fixture
def artwork_refresh_server(tmp_path):
    """Each refresh race starts with an uncached Cover Art Archive answer."""
    yield from _run_demo_server(tmp_path, app_module="test.e2e.artwork_inspection_app:app")


@pytest.fixture(scope="module")
def barcode_server(tmp_path_factory):
    root = tmp_path_factory.mktemp("e2e-barcode")
    for base in _run_demo_server(root, app_module="test.e2e.barcode_app:app"):
        yield base, root / "harmonist-demo"


@pytest.fixture(params=["0", "1", "error"])
def barcode_outcome_server(tmp_path, monkeypatch, request):
    monkeypatch.setenv("HARMONIST_TEST_BARCODE_RESULTS", request.param)
    for base in _run_demo_server(tmp_path, app_module="test.e2e.barcode_app:app"):
        yield base, tmp_path / "harmonist-demo", request.param


@pytest.fixture(scope="module")
def upc_contribution_server(tmp_path_factory):
    root = tmp_path_factory.mktemp("e2e-upc-contribution")
    for base in _run_demo_server(root, app_module="test.e2e.upc_contribution_app:app"):
        yield base, root / "harmonist-demo"


@pytest.fixture(params=["linked", "absent"])
def barcode_contribution_server(tmp_path, monkeypatch, request):
    monkeypatch.setenv("HARMONIST_TEST_UPC_CONTRIBUTION", request.param)
    for base in _run_demo_server(
        tmp_path,
        app_module="test.e2e.upc_contribution_app:app",
        config_toml="[musicbrainz]\ncache_ttl_seconds = 0\n",
    ):
        yield base, request.param


@pytest.fixture(scope="module")
def public_demo_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[str, Path]]:
    """Exercise the recording catalogue through the ordinary application entry point."""
    root = tmp_path_factory.mktemp("e2e-public")
    for base in _run_demo_server(root, app_module="harmonist.web.main:app"):
        yield base, root / "harmonist-demo"


@pytest.fixture
def reset_public_demo_server(public_demo_server: tuple[str, Path]) -> str:
    base, _root = public_demo_server
    _reset_demo_library(base)
    return base


def _run_demo_server(
    root: Path,
    *,
    config_toml: str | None = None,
    app_module: str = "test.e2e.demo_app:app",
) -> Iterator[str]:
    """The body of the server fixtures, so one that needs a differently
    CONFIGURED server can have it without copying the launcher.

    `config_toml` is written to `harmonist.toml` in the throwaway config dir
    before startup — the only way to reach settings that have no env override
    (the cache TTLs, for one). Everything else is identical, including the demo
    library reset, so a test module choosing its own settings does not also
    quietly choose its own fixture library."""
    env = os.environ | {
        "HARMONIST_DEMO_MODE": "1",
        "HARMONIST_DEMO_DELAY": "0",
        "HARMONIST_MUSIC_DIR": str(root / "music"),
        "HARMONIST_CONFIG_DIR": str(root / "config"),
        # Demo music AND artwork caches live under gettempdir(), independently
        # of the configured music/config dirs. Give each server its own so a
        # candidate loaded in one module cannot leak into another (#509).
        "TMPDIR": str(root),
    }
    # CI's response times on demand: check a race fix with E2E_LATENCY_MS=300 (#834).
    if os.environ.get("E2E_LATENCY_MS"):
        env["E2E_APP"] = app_module
        app_module = "test.e2e.latency_app:app"
    (root / "music").mkdir()
    (root / "config").mkdir()
    if config_toml is not None:
        (root / "config" / "harmonist.toml").write_text(config_toml, encoding="utf-8")
    # Bind here and hand uvicorn the listening socket. Picking a free port for
    # the server to bind later leaves a gap in which another worker's server
    # can take it, and this fixture then resets that server mid-test (#821).
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", app_module, "--fd", str(listener.fileno())],
            env=env,
            cwd=Path(__file__).resolve().parents[2],
            pass_fds=[listener.fileno()],
        )
    base = f"http://127.0.0.1:{port}"
    try:
        # Requests queue on the socket until the app has started; a server
        # that dies first closes it, failing this instead of hanging.
        with urllib.request.urlopen(f"{base}/status", timeout=30):
            pass

        # Reset through the real route and wait for the initial scan to settle
        # before handing the seeded library to a test.
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
    deadline = time.monotonic() + 30
    while True:
        # A rejected POST still schedules a scan. Repeating it while busy
        # perpetually restarts the work reset is waiting for. Observe readiness
        # through the read-only endpoint before each attempt instead.
        while time.monotonic() < deadline:
            with urllib.request.urlopen(f"{base}/status", timeout=10) as response:
                status = json.load(response)
            if (
                status["sync"]["state"] != "running"
                and status["reconcile"]["state"] != "running"
                and status["scan"]["state"] != "scanning"
            ):
                break
        else:
            pytest.fail(f"demo remained busy before reset: {status}")
        try:
            with urllib.request.urlopen(req, timeout=30):
                break
        except urllib.error.HTTPError as error:
            # Startup reconciliation may still own the library. Reset explicitly
            # refuses that race; retry only its busy response, with a deadline.
            if error.code != 409 or time.monotonic() >= deadline:
                raise
            error.close()
    # The reset kicks a rescan; the Library is empty until it lands.
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        with urllib.request.urlopen(f"{base}/library", timeout=10) as r:
            if 'data-total-done="0"' not in r.read().decode():
                return
    pytest.fail("demo library did not re-seed with terminal albums")
