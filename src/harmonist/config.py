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
    user_agent: str = "Harmonist/1.0 ( harmonist@girtby.net )"
    # How long a fetched release may be re-served before Harmonist asks again
    # (#127). MusicBrainz allows one request per second, so an uncached album
    # page spends a rate-limited slot on every view.
    #
    # An hour, because that is roughly how long "I edited this on MusicBrainz
    # and came back to look" takes to stop being a live concern — and the album
    # page shows when it last read, with a control to read again, so a user who
    # cannot wait never has to. 0 disables serving from the cache; payloads are
    # still recorded, so change detection keeps working with it off.
    cache_ttl_seconds: int = Field(default=3600, ge=0)


class ServerConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8000
    # DNS-rebinding protection (Starlette's TrustedHostMiddleware). Default
    # ["*"] is permissive — set this to your real hostname(s) when exposing
    # Harmonist beyond loopback. Loopback aliases are always implicitly
    # allowed regardless, so a tightened list still works for local curl /
    # healthcheck. See docs/deployment.md.
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


class ArtworkStoreConfig(BaseModel):
    """Kept copies of artwork a tagging overwrote, so it can be put back (#131).

    A size cap rather than an age cap: "how much disk is this costing me" is the
    question a user actually asks, and an age cap places no upper bound at all —
    a month of heavy re-tagging could still be tens of gigabytes on a NAS nobody
    is watching. Oldest is evicted first, which makes a restore best-effort by
    design; the UI offers no undo for a change whose image has gone rather than
    a button that would fail.

    Zero disables the store: no copies are kept and artwork replacement stops
    being reversible, which is a legitimate choice on a volume with no room to
    spare.
    """

    max_bytes: int = Field(default=500 * 1024 * 1024, ge=0)


class LibraryConfig(BaseModel):
    # Seconds the music dir must stay quiet after a change before the file
    # watcher triggers a rescan — long enough that a manual copy of many files
    # settles into a single scan instead of one mid-copy. Only relevant on local
    # mounts where inotify fires (see web/dir_watcher.py).
    watch_settle_seconds: float = 5.0


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
    artwork_store: ArtworkStoreConfig = Field(default_factory=ArtworkStoreConfig)
    library: LibraryConfig = Field(default_factory=LibraryConfig)
    test: TestConfig = Field(default_factory=TestConfig)
    log_level: str = "info"
    demo_mode: bool = False

    @property
    def artwork_dir(self) -> Path:
        """Where overwritten artwork is kept (#131). Beside `activity.db` in the
        config dir, which under Docker is the bind-mounted `/config` — so the
        undo history survives a container rebuild, like the rest of the state.

        Demo mode gets its own directory under the temp sandbox: it shares the
        REAL config dir (only the music dir is sandboxed), so writing there
        would deposit demo images among the user's genuine ones. Sandboxed
        rather than disabled, because a demo that can't exercise the flow is
        exactly the demo that stops catching bugs in it. Beside the sandboxed
        library rather than inside it — the images are not music.
        """
        if self.demo_mode:
            return self.paths.music_dir.parent / "harmonist-demo-artwork"
        return self.paths.config_dir / "artwork"

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
    library = data.setdefault("library", {})
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
    if v := env.get("HARMONIST_WATCH_SETTLE_SECONDS"):
        library["watch_settle_seconds"] = float(v)
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
        # The ignores file too (#77): it lives in the CONFIG dir, which demo
        # shares with the real install, so "Don't download" on a demo purchase
        # was appending fixture item_ids to the user's genuine ignores.txt and
        # suppressing them from real syncs. Same trap as the activity store
        # (#69) — sandboxing the music dir alone is not enough for config-dir
        # state that demo actions write to.
        cfg = cfg.model_copy(
            update={
                "paths": cfg.paths.model_copy(update={"music_dir": sandbox}),
                "bandcamp": cfg.bandcamp.model_copy(
                    update={"ignores_file": sandbox / "ignores.txt"}
                ),
            }
        )

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
