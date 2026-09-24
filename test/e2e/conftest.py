"""Shared fixtures for the opt-in browser (e2e) suite.

Opt-in: requires playwright (`pip install -e .[e2e]` + `playwright install
chromium`) and RUN_E2E=1, so `make test` / `make check` stay browser-free.
Run via `make e2e`.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import urllib.error
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
    yield from _run_demo_server(tmp_path_factory.mktemp("e2e"))


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


@pytest.fixture(params=["absent", "cached-group", "failure"])
def sibling_artwork_server(tmp_path, monkeypatch, request):
    monkeypatch.setenv("HARMONIST_TEST_SIBLING_ARTWORK", request.param)
    for base in _run_demo_server(tmp_path, app_module="test.e2e.sibling_artwork_app:app"):
        yield base, request.param


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
def public_demo_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[str, Path]]:
    """Exercise the recording catalogue through the ordinary application entry point."""
    root = tmp_path_factory.mktemp("e2e-public")
    for base in _run_demo_server(root, app_module="harmonist.web.main:app"):
        yield base, root / "harmonist-demo"


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
    port = _free_port()
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
    (root / "music").mkdir()
    (root / "config").mkdir()
    if config_toml is not None:
        (root / "config" / "harmonist.toml").write_text(config_toml, encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", app_module, "--port", str(port)],
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
        try:
            with urllib.request.urlopen(req, timeout=30):
                break
        except urllib.error.HTTPError as error:
            # Startup reconciliation may still own the library. Reset explicitly
            # refuses that race; retry only its busy response, with a deadline.
            if error.code != 409 or time.monotonic() >= deadline:
                raise
            error.close()
            time.sleep(0.1)
    # The reset kicks a rescan; the Library is empty until it lands.
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        with urllib.request.urlopen(f"{base}/library", timeout=10) as r:
            if 'data-total-done="0"' not in r.read().decode():
                return
        time.sleep(0.5)
    pytest.fail("demo library did not re-seed with terminal albums")
