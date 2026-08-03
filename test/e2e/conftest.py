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
