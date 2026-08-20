"""Every public module must import cleanly on its own.

A circular import between two modules is invisible while some third module
always gets imported first: by the time the cycle is reached, the module at the
top of it is already fully initialised. That is exactly what happened in #200 —
`album_files` imported `sidecar`, closing

    sidecar → library_index → url_recovery → album_files → sidecar

and `import harmonist.sidecar` raised ImportError, while the entire test suite,
the app and CI stayed green because `harmonist.web.main` and `harmonist.models`
reliably imported first.

So each module is imported in a FRESH interpreter, as the first thing that
happens. Nothing else can prime the cycle, which is the only way this class of
bug fails loudly instead of lying in wait for whoever imports in a new order.
"""

from __future__ import annotations

import pkgutil
import subprocess
import sys

import pytest

import harmonist


def _module_names() -> list[str]:
    return sorted(
        m.name
        for m in pkgutil.walk_packages(harmonist.__path__, prefix="harmonist.")
        if not m.name.rpartition(".")[2].startswith("_")
    )


@pytest.mark.parametrize("module", _module_names())
def test_module_imports_first(module: str) -> None:
    r = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        capture_output=True,
        text=True,
        check=False,  # the assertion below reports the traceback, not a CalledProcessError
    )
    assert r.returncode == 0, f"`import {module}` fails on its own:\n{r.stderr}"
