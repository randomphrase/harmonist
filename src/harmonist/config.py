"""Configuration loading: env vars > TOML file > defaults."""
from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator


TestMode = Literal["fixture", "cassette", "live"]
CoverArtSize = Literal["250", "500", "1200", "original"]


class PathsConfig(BaseModel):
    config_dir: Path
    music_dir: Path


class BandcampConfig(BaseModel):
    download_format: str = "alac"
    max_downloads_per_sync: int = 5
    ignores_file: Path | None = None
    cookies_file: Path | None = None


class MusicBrainzConfig(BaseModel):
    user_agent: str = "Harmonist/0.1 ( harmonist@girtby.net )"


class ServerConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8000


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


def _load_toml(config_dir: Path) -> dict:
    toml_path = config_dir / "harmonist.toml"
    if not toml_path.exists():
        return {}
    with open(toml_path, "rb") as f:
        return tomllib.load(f)


def _apply_env_overrides(data: dict) -> dict:
    env = os.environ
    paths = data.setdefault("paths", {})
    bandcamp = data.setdefault("bandcamp", {})
    server = data.setdefault("server", {})
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
    return Config(**data)
