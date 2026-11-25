"""An artist write checks the identity snapshot even when only a picture changes."""

from __future__ import annotations

import sqlite3

import pytest

from harmonist import activity_store, nfo_history, nfo_writer

OLD = b"<artist><name>Alice</name></artist>"
NEW = b"<artist><name>Bob</name></artist>"
JPEG = b"\xff\xd8\xff\xe0" + b"jpeg" * 8


@pytest.fixture
def nfo(tmp_path):
    activity_store.init(tmp_path / "activity.db")
    path = tmp_path / "artist.nfo"
    path.write_bytes(OLD)
    return path


def test_picture_only_write_refuses_a_changed_nfo(nfo):
    nfo.write_bytes(NEW)
    with pytest.raises(nfo_writer.NfoChangedError):
        nfo_history.write(
            nfo,
            root=nfo.parent,
            expected=OLD,
            wanted={},
            picture=nfo_history.NewPicture(
                JPEG, nfo_history.PictureCredit("fanart.tv", "https://fanart.tv/artist/alice/")
            ),
        )
    assert nfo.read_bytes() == NEW
    assert not (nfo.parent / "artist.jpg").exists()


def test_history_failure_is_part_of_the_written_outcome(nfo, monkeypatch):
    """Fail the detail insert after the audit event has committed."""
    ensure = activity_store._ensure

    class FailDetail:
        def execute(self, sql, *args):
            if "INSERT INTO nfo_changes" in sql:
                raise sqlite3.OperationalError("disk full")
            return ensure().execute(sql, *args)

        def commit(self):
            ensure().commit()

    monkeypatch.setattr(activity_store, "_ensure", lambda: FailDetail())
    with activity_store.action():
        written = nfo_history.write(nfo, root=nfo.parent, expected=OLD, wanted={"name": "Bob"})
    assert nfo.read_bytes() == NEW
    assert written is not None
    assert getattr(written, "history_error", None), (
        "a changed file without Undo is a partial result"
    )
