"""Library readers keep one coherent view while assessments finish elsewhere."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event

from harmonist import contributions, gardener, sidecar
from harmonist.models import Album
from harmonist.transforms import TaggingChoices, TagTransform
from harmonist.web.scan_runner import ScanRunner
from test.test_gardener import _release, _store, _tagged


def test_assessing_an_album_does_not_change_a_readers_snapshot(tmp_path):
    _tagged(tmp_path, _release())
    runner = ScanRunner(tmp_path)
    runner.refresh_now()
    reading = runner.albums()[0]

    assessment = gardener.refresh_flag(reading, _release("A different title"))

    assert assessment.verdict is not None
    assert not reading.update_available
    assert reading.mb_version is None


def test_a_contribution_fetch_does_not_change_a_readers_snapshot(tmp_path):
    _tagged(tmp_path, _release())
    runner = ScanRunner(tmp_path)
    runner.refresh_now()
    reading = runner.albums()[0]
    before = reading.contribution_observation
    fetched = _release()
    fetched["barcode"] = "123456789012"

    contributions.observe(reading, fetched, None)

    assert reading.contribution_observation is before


def test_publishing_an_assessment_preserves_readers_and_unchanged_scans(tmp_path):
    _tagged(tmp_path, _release())
    runner = ScanRunner(tmp_path)
    runner.refresh_now()
    gardener.configure(None, owner=runner)
    reading = runner.albums()[0]
    sequence = runner.seq

    result = gardener.refresh_flag(reading, _release("A different title"))

    assert result.accepted
    assert runner.albums()[0].update_available
    assert not reading.update_available
    assert runner.seq > sequence
    published = runner.albums()[0]
    runner.refresh_now()
    assert runner.albums()[0] is published


def test_a_refresh_overtaking_an_assessment_requeues_current_reading(tmp_path, monkeypatch):
    _tagged(tmp_path, _release())
    runner = ScanRunner(tmp_path)
    runner.refresh_now()
    gardener.configure(None, owner=runner)
    reading = runner.albums()[0]
    entered, release = Event(), Event()
    plan = gardener.plan_for
    rechecks: list[Album] = []
    runner.set_on_rebuilt(rechecks.extend)

    def held(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return plan(*args, **kwargs)

    monkeypatch.setattr(gardener, "plan_for", held)
    with ThreadPoolExecutor(max_workers=1) as worker:
        pending = worker.submit(gardener.refresh_flag, reading, _release("A different title"))
        try:
            assert entered.wait(5)
            assert reading.sidecar is not None
            sidecar.write(reading.path, replace(reading.sidecar, notes="Changed during assessment"))
            runner.refresh_album(reading.folders)
            current = runner.albums()[0]
            rechecks.clear()
        finally:
            release.set()
        result = pending.result(5)

    assert not result.accepted
    assert runner.albums()[0] is current
    assert not current.update_available
    assert rechecks == [current]


def test_settings_change_overtaking_assessment_requeues_without_publishing(tmp_path, monkeypatch):
    _tagged(tmp_path, _release())
    runner = ScanRunner(tmp_path)
    runner.refresh_now()
    choices = TaggingChoices()
    gardener.configure(lambda: choices, owner=runner)
    reading = runner.albums()[0]
    entered, release = Event(), Event()
    plan = gardener.plan_for
    rechecks: list[Album] = []
    runner.set_on_rebuilt(rechecks.extend)

    def held(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return plan(*args, **kwargs)

    monkeypatch.setattr(gardener, "plan_for", held)
    with ThreadPoolExecutor(max_workers=1) as worker:
        pending = worker.submit(gardener.refresh_flag, reading, _release("A different title"))
        try:
            assert entered.wait(5)
            choices = replace(choices, transforms=frozenset({TagTransform.ALBUM_DISAMBIGUATION}))
        finally:
            release.set()
        result = pending.result(5)

    assert not result.accepted
    assert runner.albums()[0] is reading
    assert rechecks == [reading]


def test_new_cached_payload_overtaking_assessment_cannot_publish_old_verdict(tmp_path, monkeypatch):
    _tagged(tmp_path, _release())
    older = _release("Older title")
    _store(older)
    runner = ScanRunner(tmp_path)
    runner.refresh_now()
    gardener.configure(None, owner=runner)
    reading = runner.albums()[0]
    entered, release = Event(), Event()
    plan = gardener.plan_for
    rechecks: list[Album] = []
    runner.set_on_rebuilt(rechecks.extend)

    def held(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return plan(*args, **kwargs)

    monkeypatch.setattr(gardener, "plan_for", held)
    with ThreadPoolExecutor(max_workers=1) as worker:
        pending = worker.submit(gardener.refresh_flag, reading, older)
        try:
            assert entered.wait(5)
            _store(_release("Latest title"))
        finally:
            release.set()
        result = pending.result(5)

    assert not result.accepted
    assert runner.albums()[0] is reading
    assert rechecks == [reading]


def test_an_unrelated_album_refresh_does_not_invalidate_a_result(tmp_path):
    _tagged(tmp_path, _release(), name="First")
    _tagged(tmp_path, _release(mbid="rel-bbb"), name="Second")
    runner = ScanRunner(tmp_path)
    runner.refresh_now()
    first, second = sorted(runner.albums(), key=lambda a: a.path.name)
    result = gardener.refresh_flag(first, _release("Updated title"))

    assert second.sidecar is not None
    sidecar.write(second.path, replace(second.sidecar, notes="Unrelated edit"))
    runner.refresh_album(second.folders)
    assert runner.publish_observations(first, result.album)
    assert runner.current(first).update_available
    assert not first.update_available
