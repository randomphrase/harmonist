"""Upgrading the cache location preserves images and never moves undo backups."""

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from harmonist import activity_store, artwork_store, cover_art
from harmonist.config import Config, PathsConfig
from harmonist.web.main import create_app


@pytest.fixture
def cfg(tmp_path):
    config = Config(paths=PathsConfig(config_dir=tmp_path / "config", music_dir=tmp_path / "music"))
    config.paths.config_dir.mkdir()
    config.paths.music_dir.mkdir()
    return config


def seed_old_storage(cfg):
    old = cfg.artwork_dir / "caa"
    old.mkdir(parents=True)
    cached = old / "release-one.jpg"
    cached.write_bytes(b"cached image")
    os.utime(cached, ns=(1000000000, 2000000000))
    key = artwork_store.digest(b"undo image")
    backup = cfg.artwork_dir / f"{key}.jpg"
    backup.write_bytes(b"undo image")
    return old, backup, key


@pytest.mark.parametrize("cap", [0, 1073741824])
def test_startup_moves_cache_once_and_keeps_backups_usable(cfg, cap):
    old, backup, key = seed_old_storage(cfg)
    cfg.cover_art.image_cache_max_bytes = cap
    target = cfg.paths.config_dir / "caa-cache"
    before = backup.stat()

    for _ in range(2):
        app = create_app(cfg)
        assert target.is_dir()
        assert not old.exists()
        assert (target / "release-one.jpg").read_bytes() == b"cached image"
        assert (target / "release-one.jpg").stat().st_mtime_ns == 2000000000
        assert backup.read_bytes() == b"undo image"
        assert backup.stat().st_ino == before.st_ino
        assert backup.stat().st_mtime_ns == before.st_mtime_ns
        assert artwork_store.path_for(key) == backup
        assert cover_art.cached_image("release-one") == (
            target / "release-one.jpg" if cap else None
        )
        page = TestClient(app, headers={"HX-Request": "true"}).get("/settings").text
        assert f'title="{target}"' in page
        assert f'title="{cfg.artwork_dir}"' in page

    events = activity_store.recent()
    intent = [e for e in events if e.message.startswith("artwork.cache_move ")]
    outcome = [e for e in events if e.message.startswith("Artwork cache moved to ")]
    assert len(intent) == len(outcome) == 1
    assert intent[0].action_id == outcome[0].action_id
    assert intent[0].action_id is not None


def test_fresh_install_writes_to_separate_stores_without_a_migration(cfg):
    from test.helpers import keep_one

    create_app(cfg)
    key = keep_one(b"backup", mime="image/jpeg")
    assert key is not None
    cached = cover_art.cache_image("release-new", b"cache", mime="image/jpeg")
    assert artwork_store.path_for(key).parent == cfg.paths.config_dir / "artwork"
    assert cached.parent == cfg.paths.config_dir / "caa-cache"
    assert not any(e.message.startswith("artwork.cache_move ") for e in activity_store.recent())


@pytest.mark.parametrize("destination_kind", ["empty", "populated", "file", "symlink"])
def test_startup_refuses_existing_destination_without_overwriting(cfg, destination_kind, caplog):
    old, backup, _ = seed_old_storage(cfg)
    target = cfg.paths.config_dir / "caa-cache"
    if destination_kind in {"empty", "populated"}:
        target.mkdir()
        if destination_kind == "populated":
            (target / "release-one.jpg").write_bytes(b"different cached image")
    elif destination_kind == "file":
        target.write_bytes(b"unrelated file")
    else:
        target.symlink_to(cfg.paths.config_dir / "missing")

    with pytest.raises(RuntimeError, match="Artwork cache migration"):
        create_app(cfg)

    assert (old / "release-one.jpg").read_bytes() == b"cached image"
    assert backup.read_bytes() == b"undo image"
    if destination_kind == "populated":
        assert (target / "release-one.jpg").read_bytes() == b"different cached image"
    elif destination_kind == "file":
        assert target.read_bytes() == b"unrelated file"
    elif destination_kind == "symlink":
        assert target.is_symlink()
    else:
        assert list(target.iterdir()) == []
    assert str(old) in caplog.text and str(target) in caplog.text


def test_failed_move_is_audited_and_restart_can_retry(cfg, monkeypatch, caplog):
    old, backup, _ = seed_old_storage(cfg)
    original = Path.rename

    def denied(path, target):
        if path == old:
            assert any(e.message.startswith("artwork.cache_move ") for e in activity_store.recent())
            raise PermissionError("permission denied")
        return original(path, target)

    monkeypatch.setattr(Path, "rename", denied)
    with pytest.raises(RuntimeError, match="Artwork cache migration"):
        create_app(cfg)
    assert "permission denied" in caplog.text
    assert (old / "release-one.jpg").read_bytes() == b"cached image"
    assert backup.read_bytes() == b"undo image"
    assert not (cfg.paths.config_dir / "caa-cache").exists()
    monkeypatch.setattr(Path, "rename", original)
    create_app(cfg)
    assert cover_art.cached_image("release-one").read_bytes() == b"cached image"


@pytest.mark.parametrize("kind", ["file", "symlink"])
def test_legacy_cache_must_be_a_directory(cfg, kind):
    cfg.artwork_dir.mkdir()
    old = cfg.artwork_dir / "caa"
    if kind == "file":
        old.write_bytes(b"unrelated data")
    else:
        external = cfg.paths.config_dir / "external-cache"
        external.mkdir()
        (external / "kept.jpg").write_bytes(b"unrelated data")
        old.symlink_to(external, target_is_directory=True)

    with pytest.raises(RuntimeError, match="not a regular directory"):
        create_app(cfg)
    assert not (cfg.paths.config_dir / "caa-cache").exists()
    if kind == "file":
        assert old.read_bytes() == b"unrelated data"
    else:
        assert old.is_symlink()
        assert (old / "kept.jpg").read_bytes() == b"unrelated data"


def test_demo_migration_and_reset_leave_real_storage_untouched(cfg):
    from harmonist import demo

    real_old, real_backup, _ = seed_old_storage(cfg)
    cfg.demo_mode = True
    # A current demo sandbox needs no reseed on this simulated upgrade.
    (cfg.paths.music_dir / demo.DEMO_MARKER).write_text(f"version: {demo.data_version()}\n")
    old, backup, _ = seed_old_storage(cfg)
    create_app(cfg)
    cached = cover_art.cached_image("release-one")
    assert cached == cfg.paths.music_dir / ".demo-caa-cache" / "release-one.jpg"
    assert cached.read_bytes() == b"cached image"
    assert not old.exists()
    assert backup.exists()

    demo.reset(cfg.paths.music_dir)
    assert not cached.exists()
    assert (real_old / "release-one.jpg").read_bytes() == b"cached image"
    assert real_backup.read_bytes() == b"undo image"
    assert not (cfg.paths.config_dir / "caa-cache").exists()
