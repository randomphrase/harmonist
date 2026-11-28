"""Pause real demo writes at each track so browser progress is observable, or
fail them (`arm/fail`) so a failed write's report is."""

import threading

from fastapi import FastAPI
from test.e2e import demo_app

from harmonist import formats

app: FastAPI = demo_app.app
_kind = ""
_step = threading.Event()
_write_tags = formats.write_tags
_write_cover = formats.write_cover


@app.post("/test/write/arm/{kind}")
def arm(kind: str):
    global _kind
    _kind = kind
    _step.clear()
    return {}


@app.post("/test/write/continue")
def advance():
    _step.set()
    return {}


def pause(kind):
    if _kind == kind:
        if not _step.wait(timeout=10):
            raise RuntimeError("browser did not release the paused test write")
        _step.clear()


def write_tags(*args, **kwargs):
    # A write that fails before changing anything: it publishes no new scan,
    # so only the write's own end can tell the page to redraw (#1053).
    if _kind == "fail":
        raise OSError("disk full")
    pause("tags")
    return _write_tags(*args, **kwargs)


def write_cover(*args, **kwargs):
    pause("artwork")
    return _write_cover(*args, **kwargs)


formats.write_tags = write_tags
formats.write_cover = write_cover
