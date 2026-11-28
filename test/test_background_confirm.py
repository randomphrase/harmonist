"""A confirmed review is written in the background (#1053).

Confirm used to tag and write artwork inside its request, and the review stayed
open, modal, for the whole write: over a minute for a three-disc album on a NAS,
long enough for a reverse proxy to give up and leave the review stranded while
the server finished. Once the review's checks pass there is nothing left for the
user to decide, so the request answers at once and the album is written as
other background writes are, with its outcome in Activity and the status bar.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from bs4 import BeautifulSoup

from harmonist import activity, progress, retag
from test import test_web
from test.helpers import finish_writes
from test.test_web import _confirmation_fields, _confirmation_setup, _id_for

cfg = test_web.cfg
client = test_web.client


@pytest.fixture
def held(monkeypatch) -> Iterator[tuple[threading.Event, threading.Event, list[Any]]]:
    """Hold the confirm's write until the test lets it go: `(started, release,
    calls)`. A write still held after five seconds fails, so a request that
    waits for it answers with that failure rather than hanging the suite."""
    started, release = threading.Event(), threading.Event()
    calls: list[Any] = []
    real = retag.tag_with_release

    def write(*args: Any, **kwargs: Any) -> Any:
        calls.append(args)
        started.set()
        if not release.wait(5):
            raise AssertionError("the request waited for its write")
        return real(*args, **kwargs)

    monkeypatch.setattr(retag, "tag_with_release", write)
    yield started, release, calls
    release.set()


def _review(client: Any, cfg: Any, monkeypatch: Any) -> tuple[str, dict[str, str]]:
    root, *_ = _confirmation_setup(cfg, monkeypatch, old_mbid=None)
    aid = _id_for(cfg, root)
    return aid, _confirmation_fields(client.get(f"/assignments/{aid}").text)


def _matched() -> list[activity.Event]:
    return [e for e in activity.recent() if e.message.startswith("Matched")]


def test_confirm_answers_before_its_write_finishes(client, cfg, monkeypatch, held) -> None:
    started, release, _ = held
    aid, fields = _review(client, cfg, monkeypatch)
    response = client.post(f"/confirm/{aid}/accept", data=fields)
    assert "confirmation-applied" in response.headers.get("HX-Trigger", "")
    assert started.wait(5)
    assert _matched() == []
    release.set()
    finish_writes(client.app)
    assert len(_matched()) == 1


def test_the_outcome_is_grouped_with_the_write(client, cfg, monkeypatch) -> None:
    aid, fields = _review(client, cfg, monkeypatch)
    client.post(f"/confirm/{aid}/accept", data=fields)
    finish_writes(client.app)
    events = activity.recent(include_audit=True)
    matched = next(e for e in events if e.message.startswith("Matched"))
    tagged = next(e for e in events if e.message.startswith("tag.album"))
    assert matched.action_id is not None
    assert tagged.action_id == matched.action_id


def test_a_second_confirm_waits_for_no_write_already_running(
    client, cfg, monkeypatch, held
) -> None:
    started, release, calls = held
    aid, fields = _review(client, cfg, monkeypatch)
    client.post(f"/confirm/{aid}/accept", data=fields)
    assert started.wait(5)
    again = client.post(f"/confirm/{aid}/accept", data=fields)
    assert "confirmation-applied" not in again.headers.get("HX-Trigger", "")
    assert "already being written" in again.text
    release.set()
    finish_writes(client.app)
    assert len(calls) == 1
    assert len(_matched()) == 1


def _changed(*args: Any, **kwargs: Any) -> Any:
    raise retag.ConfirmationChanged("The selected release changed. Review the new suggestion.")


def _disk_full(*args: Any, **kwargs: Any) -> Any:
    raise OSError("disk full")


@pytest.mark.parametrize(
    ("write", "verb", "reason", "level"),
    [
        (_changed, "Not matched", "The selected release changed", "warning"),
        (_disk_full, "Tagging failed", "disk full", "error"),
    ],
    ids=["changed-since", "write-failed"],
)
def test_a_write_that_fails_says_so_in_activity_and_the_status_bar(
    client,
    cfg,
    monkeypatch,
    write: Callable[..., Any],
    verb: str,
    reason: str,
    level: str,
) -> None:
    aid, fields = _review(client, cfg, monkeypatch)
    monkeypatch.setattr(retag, "tag_with_release", write)
    response = client.post(f"/confirm/{aid}/accept", data=fields)
    assert "confirmation-applied" in response.headers.get("HX-Trigger", "")
    finish_writes(client.app)
    entry = next(e for e in activity.recent() if e.message.startswith(verb))
    assert reason in entry.message
    assert entry.level == level
    assert _matched() == []
    finished = progress.status()["finished"]
    assert finished["label"] == verb
    assert reason in finished["message"]
    assert finished["level"] == level


def test_the_inbox_card_is_covered_while_its_album_is_written(
    client, cfg, monkeypatch, held
) -> None:
    started, release, _ = held
    aid, fields = _review(client, cfg, monkeypatch)
    client.post(f"/confirm/{aid}/accept", data=fields)
    assert started.wait(5)
    card = BeautifulSoup(client.get("/tasks").text, "html.parser").select_one(f"#task-{aid}")
    assert card is not None
    assert card.select_one(f"#tagging-{aid}").has_attr("data-writing")
    release.set()
    finish_writes(client.app)


def test_an_album_page_waits_for_the_write_before_redrawing(client, cfg, monkeypatch, held) -> None:
    started, release, _ = held
    aid, fields = _review(client, cfg, monkeypatch)
    client.post(f"/confirm/{aid}/accept", data=fields)
    assert started.wait(5)
    assert client.get(f"/album/{aid}/revision").json().get("writing") is True
    release.set()
    finish_writes(client.app)
    assert client.get(f"/album/{aid}/revision").json().get("writing") is not True
