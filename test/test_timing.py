"""Tests for the slow-operation guard (#300).

The guard's whole value is that it is silent when things are fine and says
exactly one useful thing when they are not, so both halves are pinned here — a
guard that warned on every call would be turned off within a week, and one that
stayed quiet when something was slow would be the gap it was built to close.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from pathlib import Path

import pytest

from harmonist import timing


def test_a_fast_block_says_nothing(caplog):
    """The common case, and the one that decides whether this is bearable. An
    install where nothing is wrong must produce no timing output at all."""
    with (
        caplog.at_level(logging.DEBUG, logger="harmonist.timing"),
        timing.warn_if_slow("read tags", timedelta(seconds=30)),
    ):
        pass

    assert caplog.records == []


def test_a_slow_block_warns_once(caplog):
    with (
        caplog.at_level(logging.WARNING, logger="harmonist.timing"),
        timing.warn_if_slow("read tags", timedelta(0)),
    ):
        pass

    assert len(caplog.records) == 1
    assert caplog.records[0].levelno == logging.WARNING
    assert "read tags took" in caplog.records[0].getMessage()


def test_the_warning_names_what_was_slow(caplog):
    """ "Something was slow" is not actionable on a library of thousands. The
    line has to say which album, or nobody can do anything with it."""
    with (
        caplog.at_level(logging.WARNING, logger="harmonist.timing"),
        timing.warn_if_slow(
            "read tags", timedelta(0), album=Path("/music/Aphex Twin/SAW II"), tracks=27
        ),
    ):
        pass

    message = caplog.records[0].getMessage()
    assert 'album="/music/Aphex Twin/SAW II"' in message  # quoted: it has a space
    assert "tracks=27" in message


def test_a_value_with_no_whitespace_is_not_quoted(caplog):
    """Quoting everything would make the common case harder to read; quoting
    nothing would split an album path across fields. Only what needs it."""
    with (
        caplog.at_level(logging.WARNING, logger="harmonist.timing"),
        timing.warn_if_slow("fetch release", timedelta(0), mbid="rel-aaa"),
    ):
        pass

    assert "mbid=rel-aaa" in caplog.records[0].getMessage()


def test_a_slow_failure_is_reported_and_the_exception_still_propagates(caplog):
    """How long something took before failing is a fact worth having — a
    MusicBrainz call that fails after thirty seconds and one that fails at once
    are different problems. The guard must not swallow the failure to say so."""
    with (
        caplog.at_level(logging.WARNING, logger="harmonist.timing"),
        pytest.raises(ValueError, match="boom"),
        timing.warn_if_slow("fetch release", timedelta(0), mbid="rel-aaa"),
    ):
        raise ValueError("boom")

    assert len(caplog.records) == 1
    assert "fetch release took" in caplog.records[0].getMessage()


def test_a_fast_failure_says_nothing_extra(caplog):
    """The exception is somebody else's to log. A guard that also spoke up on
    every fast failure would double every error in the log."""
    with (
        caplog.at_level(logging.DEBUG, logger="harmonist.timing"),
        pytest.raises(ValueError),
        timing.warn_if_slow("fetch release", timedelta(seconds=30)),
    ):
        raise ValueError("boom")

    assert caplog.records == []


def test_a_slow_warning_does_not_reach_the_activity_feed():
    """The feed is what Harmonist DID — actions, and the failures that
    interrupted them. `activity` mirrors every WARNING from a `harmonist.*`
    logger into it so background failures are visible, which is right and which
    assumed every warning is news.

    A timing line is a measurement: nothing went wrong, nothing was lost, and
    there is nothing to act on. And it fires on a threshold, so under exactly
    the conditions worth investigating (#299) it fires on every album page view
    — the feed would fill with rows about its own slowness. Found because this
    warning mirrored into the feed on the first run.
    """
    from harmonist import activity, activity_store

    activity.install_log_handler()
    activity_store.init(":memory:")
    before = len(activity_store.recent(50))

    with timing.warn_if_slow("read tags", timedelta(0), album="/music/A/B"):
        pass

    assert len(activity_store.recent(50)) == before


def test_a_real_warning_still_reaches_the_activity_feed():
    """The exemption must be narrow. If it silenced the mirror generally, every
    background failure would stop being visible — which is what the mirror is
    for."""
    import logging as _logging

    from harmonist import activity, activity_store

    activity.install_log_handler()
    activity_store.init(":memory:")
    before = len(activity_store.recent(50))

    _logging.getLogger("harmonist.test").warning("a real background failure")

    assert len(activity_store.recent(50)) == before + 1
