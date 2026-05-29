"""Configuration loading: env vars > TOML file > defaults."""

from __future__ import annotations

import os
import tempfile
import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

TestMode = Literal["fixture", "cassette", "live"]
CoverArtSize = Literal["250", "500", "1200", "original"]


class PathsConfig(BaseModel):
    config_dir: Path
    music_dir: Path


class BandcampConfig(BaseModel):
    # FLAC by default: lossless, broadly compatible, and the safest choice
    # for an archive. Users can override (e.g. to alac) via config/env.
    download_format: str = "flac"
    max_downloads_per_sync: int = 5
    ignores_file: Path | None = None
    cookies_file: Path | None = None


class MusicBrainzConfig(BaseModel):
    user_agent: str = "Harmonist/0.1 ( harmonist@girtby.net )"


class ServerConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8000
    # DNS-rebinding protection (Starlette's TrustedHostMiddleware). Default
    # ["*"] is permissive — set this to your real hostname(s) when exposing
    # Harmonist beyond loopback. Loopback aliases are always implicitly
    # allowed regardless, so a tightened list still works for local curl /
    # healthcheck. See docs §security in the README.
    allowed_hosts: list[str] = Field(default_factory=lambda: ["*"])


class AuthConfig(BaseModel):
    # Optional HTTP Basic auth — off by default. Defense in depth for users
    # who don't run a reverse proxy with auth in front. The canonical
    # deployment is "reverse proxy handles auth"; this knob exists for
    # everyone else. Generate the hash with `python -m harmonist.web.security`.
    enabled: bool = False
    username: str = ""
    password_hash: str = ""  # pbkdf2_sha256$<iter>$<salt_b64>$<hash_b64>


class CoverArtConfig(BaseModel):
    size: CoverArtSize = "original"


class TestConfig(BaseModel):
    mode: TestMode = "fixture"
    unignore_item_ids: list[int] = Field(default_factory=list)


class Config(BaseModel):
    paths: PathsConfig
    bandcamp: BandcampConfig = Field(default_factory=BandcampConfig)
    musicbrainz: MusicBrainzConfig = Field(default_factory=MusicBrainzConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    cover_art: CoverArtConfig = Field(default_factory=CoverArtConfig)
    test: TestConfig = Field(default_factory=TestConfig)
    log_level: str = "info"
    demo_mode: bool = False

    @property
    def ignores_file(self) -> Path:
        return self.bandcamp.ignores_file or (self.paths.config_dir / "ignores.txt")

    @property
    def cookies_file(self) -> Path:
        return self.bandcamp.cookies_file or (self.paths.config_dir / "cookies.txt")

    @field_validator("log_level")
    @classmethod
    def _normalise_log_level(cls, v: str) -> str:
        return v.lower()


def _default_config_dir() -> Path:
    if Path("/config").exists() and Path("/config").is_dir():
        return Path("/config")
    return Path.home() / ".config" / "harmonist"


def _default_music_dir() -> Path:
    if Path("/music").exists() and Path("/music").is_dir():
        return Path("/music")
    return Path("./music").resolve()


def _load_toml(config_dir: Path) -> dict[str, Any]:
    toml_path = config_dir / "harmonist.toml"
    if not toml_path.exists():
        return {}
    with open(toml_path, "rb") as f:
        return tomllib.load(f)


def _apply_env_overrides(data: dict[str, Any]) -> dict[str, Any]:
    env = os.environ
    paths = data.setdefault("paths", {})
    bandcamp = data.setdefault("bandcamp", {})
    server = data.setdefault("server", {})
    auth = data.setdefault("auth", {})
    cover_art = data.setdefault("cover_art", {})
    test = data.setdefault("test", {})

    if v := env.get("HARMONIST_MUSIC_DIR"):
        paths["music_dir"] = v
    if v := env.get("HARMONIST_DOWNLOAD_FORMAT"):
        bandcamp["download_format"] = v
    if v := env.get("HARMONIST_MAX_DOWNLOADS_PER_SYNC"):
        bandcamp["max_downloads_per_sync"] = int(v)
    if v := env.get("HARMONIST_HOST"):
        server["host"] = v
    if v := env.get("HARMONIST_PORT"):
        server["port"] = int(v)
    if v := env.get("HARMONIST_ALLOWED_HOSTS"):
        # Comma-separated list, e.g. "harmonist.example.com,localhost".
        server["allowed_hosts"] = [h.strip() for h in v.split(",") if h.strip()]
    if v := env.get("HARMONIST_AUTH_ENABLED"):
        auth["enabled"] = v.strip() not in ("", "0", "false", "False", "no")
    if v := env.get("HARMONIST_AUTH_USERNAME"):
        auth["username"] = v
    if v := env.get("HARMONIST_AUTH_PASSWORD_HASH"):
        auth["password_hash"] = v
    if v := env.get("HARMONIST_TEST_MODE"):
        test["mode"] = v
    if v := env.get("HARMONIST_LOG_LEVEL"):
        data["log_level"] = v
    if v := env.get("HARMONIST_COVER_ART_SIZE"):
        cover_art["size"] = v
    if v := env.get("HARMONIST_DEMO_MODE"):
        data["demo_mode"] = v.strip() not in ("", "0", "false", "False", "no")
    return data


def load() -> Config:
    """Load config from env + optional TOML file. Env wins over TOML wins over defaults."""
    config_dir = Path(os.environ.get("HARMONIST_CONFIG_DIR", str(_default_config_dir())))
    music_dir_env = os.environ.get("HARMONIST_MUSIC_DIR")
    music_dir = Path(music_dir_env) if music_dir_env else _default_music_dir()

    data = _load_toml(config_dir)
    paths = data.setdefault("paths", {})
    paths["config_dir"] = str(config_dir)
    paths.setdefault("music_dir", str(music_dir))

    data = _apply_env_overrides(data)
    cfg = Config(**data)

    if cfg.demo_mode:
        # Demo mode is a sandbox: NEVER operate on the configured/real library.
        # Force the music dir to a stable temp location, ignoring music_dir from
        # toml/env. (Tests build Config directly and don't go through load(), so
        # they keep their own isolated dirs.)
        sandbox = Path(tempfile.gettempdir()) / "harmonist-demo"
        cfg = cfg.model_copy(update={"paths": cfg.paths.model_copy(update={"music_dir": sandbox})})

    return cfg


def write_settings(config_dir: Path, updates: dict[str, object]) -> None:
    """Persist a handful of editable settings to harmonist.toml in place.

    `updates` keys are dotted (e.g. "bandcamp.download_format", "log_level").
    Uses tomlkit so existing comments / formatting / unmanaged keys (incl.
    demo_mode and [paths]) survive the round-trip.
    """
    import tomlkit

    path = config_dir / "harmonist.toml"
    doc = tomlkit.parse(path.read_text(encoding="utf-8")) if path.exists() else tomlkit.document()
    for dotted, value in updates.items():
        if "." in dotted:
            table_name, key = dotted.split(".", 1)
            table = doc.get(table_name)
            if table is None:
                table = tomlkit.table()
                doc[table_name] = table
            table[key] = value
        else:
            doc[dotted] = value
    config_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(tomlkit.dumps(doc), encoding="utf-8")
