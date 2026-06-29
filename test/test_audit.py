"""Tests for the destructive-op audit log (harmonist.audit) and the
sidecar.write identity-change hook that feeds it."""

from __future__ import annotations

import logging

from harmonist import audit
from harmonist import sidecar as sc
from harmonist.models import BandcampInfo, Sidecar
from harmonist.sidecar import CURRENT_SCHEMA_VERSION


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
    return Sidecar(schema_version=CURRENT_SCHEMA_VERSION, **kw)


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
