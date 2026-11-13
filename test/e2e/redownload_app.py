"""Hold the demo sync so a browser can see what a page shows while it waits (#1003).

Re-download starts a sync as its request ends, and the demo's is over in
milliseconds: the album can be back in the library before the page has asked
what to draw. `POST /test/sync/hold` makes the next sync wait for
`POST /test/sync/release`.
"""

import threading

from fastapi import FastAPI
from test.e2e import demo_app

from harmonist import demo

app: FastAPI = demo_app.app
_held = False
_release = threading.Event()
_run_demo_sync = demo.run_demo_sync


@app.post("/test/sync/hold")
def hold():
    global _held
    _held = True
    _release.clear()
    return {}


@app.post("/test/sync/release")
def release():
    _release.set()
    return {}


def run_demo_sync(*args, **kwargs):
    global _held
    if _held:
        _held = False
        if not _release.wait(timeout=10):
            raise RuntimeError("browser did not release the held test sync")
    return _run_demo_sync(*args, **kwargs)


demo.run_demo_sync = run_demo_sync
