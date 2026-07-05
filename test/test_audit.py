"""Tests for the destructive-op audit log (harmonist.audit) and the
sidecar.write identity-change hook that feeds it."""

from __future__ import annotations

import logging

import pytest

from harmonist import audit
from harmonist import sidecar as sc
from harmonist.models import BandcampInfo, Sidecar


def test_audit_record_formats_key_values(caplog):
    with caplog.at_level(logging.INFO, logger="harmonist.audit"):
        audit.record("download", item_id=123, fmt="alac", path="/music/A B/x")
        audit.record("checkpoint.clear", path=None)  # None → "-"
        audit.record("bare")  # no fields
    msgs = [r.getMessage() for r in caplog.records if r.name == "harmonist.audit"]
    assert 'download item_id=123 fmt=alac path="/music/A B/x"' in msgs  # spaces quoted
    assert "checkpoint.clear path=-" in msgs
    assert "bare" in msgs


def _sc(**kw) -> Sidecar:
    return Sidecar(**kw)


def test_sidecar_write_audits_create_and_identity_change(tmp_path, caplog):
    d = tmp_path / "Album"
    d.mkdir()
    url = "https://x.bandcamp.com/album/a"
    with caplog.at_level(logging.INFO, logger="harmonist.audit"):
        sc.write(d, _sc(store_url=url, mb_release_id="rel-1"))  # create
        before = len([r for r in caplog.records if r.name == "harmonist.audit"])
        sc.write(d, _sc(store_url=url, mb_release_id="rel-1"))  # no-op → no audit
        after = len([r for r in caplog.records if r.name == "harmonist.audit"])
        assert after == before
        # Link: item_id None → 555 → sidecar.update
        sc.write(d, _sc(store_url=url, mb_release_id="rel-1", bandcamp=BandcampInfo(item_id=555)))
    msgs = [r.getMessage() for r in caplog.records if r.name == "harmonist.audit"]
    assert any(m.startswith("sidecar.create") and "mbid=rel-1" in m for m in msgs)
    assert any(m.startswith("sidecar.update") and "item_id=None->555" in m for m in msgs)


def test_move_file_is_audited(tmp_path, caplog):
    """Importing bandcamp_hook wraps bandcampsync's move_file so every extract
    move is recorded — and still actually moves the file."""
    import bandcampsync.sync as bcsync

    import harmonist.bandcamp_hook  # noqa: F401 — applies the move_file patch

    src = tmp_path / "01 Track.flac"
    src.write_text("audio")
    dst = tmp_path / "dest.flac"
    with caplog.at_level(logging.INFO, logger="harmonist.audit"):
        bcsync.move_file(str(src), str(dst))
    assert dst.exists()  # genuinely moved
    assert not src.exists()
    msgs = [r.getMessage() for r in caplog.records if r.name == "harmonist.audit"]
    assert any(m.startswith("move ") and "overwrite=False" in m for m in msgs)


def test_case_collision_detection(tmp_path):
    from harmonist.bandcamp_hook import _case_collision

    (tmp_path / "Variant").mkdir()
    if (tmp_path / "variant").exists():  # case-insensitive FS (e.g. macOS) → no collision
        pytest.skip("filesystem is case-insensitive")
    assert _case_collision(tmp_path / "variant") == tmp_path / "Variant"  # differs only by case
    assert _case_collision(tmp_path / "Brandnew") is None  # no sibling
    assert _case_collision(tmp_path / "Variant") is None  # exact match exists → not a collision
