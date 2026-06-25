"""Tests for `_recover_store_url_if_missing` — the manual-assign safety net that
captures a Bandcamp store_url before tagging so a manually-added download reaches
Needs Sync instead of Complete."""

from __future__ import annotations

from datetime import UTC, datetime

from harmonist import sidecar as sidecar_mod
from harmonist import url_recovery
from harmonist.models import Sidecar
from harmonist.sidecar import CURRENT_SCHEMA_VERSION
from harmonist.web.main import _recover_store_url_if_missing

URL = "https://myartist.bandcamp.com/album/manual-add"


def test_recovers_and_persists_when_no_sidecar(tmp_path, monkeypatch):
    monkeypatch.setattr(url_recovery, "recover_album_url", lambda _p: URL)
    _recover_store_url_if_missing(tmp_path)
    loaded = sidecar_mod.read(tmp_path)
    assert loaded is not None
    assert loaded.store_url == URL
    assert loaded.mb_release_id is None  # untagged still — assign will tag it


def test_preserves_existing_fields_when_recovering(tmp_path, monkeypatch):
    sidecar_mod.write(
        tmp_path,
        Sidecar(schema_version=CURRENT_SCHEMA_VERSION, added_at=datetime.now(UTC), notes="keep me"),
    )
    monkeypatch.setattr(url_recovery, "recover_album_url", lambda _p: URL)
    _recover_store_url_if_missing(tmp_path)
    loaded = sidecar_mod.read(tmp_path)
    assert loaded.store_url == URL
    assert loaded.notes == "keep me"


def test_noop_when_store_url_already_present(tmp_path, monkeypatch):
    sidecar_mod.write(
        tmp_path,
        Sidecar(
            schema_version=CURRENT_SCHEMA_VERSION,
            store_url="https://existing.bandcamp.com/album/x",
            added_at=datetime.now(UTC),
        ),
    )

    def boom(_p):
        raise AssertionError("recovery must not run when a store_url already exists")

    monkeypatch.setattr(url_recovery, "recover_album_url", boom)
    _recover_store_url_if_missing(tmp_path)
    assert sidecar_mod.read(tmp_path).store_url == "https://existing.bandcamp.com/album/x"


def test_noop_when_nothing_recoverable(tmp_path, monkeypatch):
    monkeypatch.setattr(url_recovery, "recover_album_url", lambda _p: None)
    _recover_store_url_if_missing(tmp_path)
    assert not sidecar_mod.has_sidecar(tmp_path)


def test_swallows_recovery_errors(tmp_path, monkeypatch):
    def boom(_p):
        raise RuntimeError("scrape failed")

    monkeypatch.setattr(url_recovery, "recover_album_url", boom)
    _recover_store_url_if_missing(tmp_path)  # must not raise
    assert not sidecar_mod.has_sidecar(tmp_path)
