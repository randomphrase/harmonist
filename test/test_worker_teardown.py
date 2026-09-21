"""The test harness must drain workers before undoing per-test patches."""

import inspect

from test import conftest

pytest_plugins = ["pytester"]


def test_workers_finish_before_fixture_teardown(pytester):
    hook = conftest.pytest_runtest_teardown
    pytester.makeconftest(
        "import threading\nimport time\nimport pytest\n" + inspect.getsource(hook)
    )
    pytester.makepyfile("""
import threading
import pytest

@pytest.fixture
def running_worker(monkeypatch):
    release = threading.Event()
    finished = threading.Event()
    state = {"patched": True}
    observed = []
    def work():
        release.wait()
        observed.append(state["patched"])
        finished.set()
    thread = threading.Thread(target=work, name="harmonist-reconcile", daemon=True)
    join = thread.join
    def release_on_join(timeout=None):
        release.set()
        join(timeout)
    monkeypatch.setattr(thread, "join", release_on_join)
    thread.start()
    # Executor threads intentionally remain idle awaiting more work. They are
    # owned by their executor, not one-shot requests the hook should join.
    idle_release = threading.Event()
    idle = threading.Thread(target=idle_release.wait, name="harmonist-scan_0", daemon=True)
    idle.start()
    yield
    idle_release.set()
    idle.join(5)
    drained = finished.is_set()
    state["patched"] = False
    release.set()
    join(5)
    assert drained, "worker outlived its fixture"
    assert observed == [True], "worker saw restored globals"

def test_background_request(running_worker):
    pass
""")
    result = pytester.runpytest_subprocess("-q")
    result.assert_outcomes(passed=1)
