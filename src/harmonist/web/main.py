"""FastAPI application for Harmonist."""

from __future__ import annotations

import asyncio
import json
import logging
import os

# Demo mode is conditionally imported in create_app() — keeps demo-only code
# out of the production import path entirely.
import re
import sys
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError as PydanticValidationError
from starlette.middleware.trustedhost import TrustedHostMiddleware

from harmonist import (
    activity,
    cover_art,
    formats,
    mb_lookup,
    mb_search,
    reconcile,
    scanner,
)
from harmonist import config as config_mod
from harmonist import sidecar as sidecar_mod
from harmonist.bandcamp_hook import HarmonistSyncer, album_slug
from harmonist.match import assess_match, best_match
from harmonist.models import (
    Album,
    AlbumState,
    BandcampInfo,
    MatchCandidate,
    Release,
    Sidecar,
    store_name,
)
from harmonist.sidecar import CURRENT_SCHEMA_VERSION
from harmonist.tagger import PicardCompatibleTagger, Tagger
from harmonist.web import dir_watcher
from harmonist.web.reconcile_runner import ReconcileRunner, reconcile_pending_orphans
from harmonist.web.scan_runner import ScanRunner
from harmonist.web.security import BasicAuthMiddleware, CSRFMiddleware
from harmonist.web.sync_runner import AlreadyRunningError, SyncRunner

_MB_URL_RE = re.compile(r"/release/([a-f0-9-]{36})", re.IGNORECASE)
# A bare MBID can be a real UUID (36 hex+dashes) OR a demo-mode pseudo-MBID
# like "demo-rel-thamesmen". Accept any alphanumeric-plus-dashes token; the
# downstream MB lookup will fail clearly if the value isn't actually valid.
_MBID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]*$")


def _extract_mbid(value: str) -> str | None:
    """Pull an MBID out of a raw input — accepts either a full MB release
    URL (extracts the UUID) or any bare MBID-shaped token.
    """
    s = (value or "").strip()
    if not s:
        return None
    if m := _MB_URL_RE.search(s):
        return m.group(1).lower()
    if _MBID_RE.fullmatch(s):
        return s
    return None


log = logging.getLogger(__name__)


HARMONY_BASE = "https://harmony.pulsewidth.org.uk"

# Terminal states — hidden from the inbox, shown in the library.
_TERMINAL_STATES = {AlbumState.COMPLETE, AlbumState.INCOMPLETE}


_logging_configured = False


def _configure_logging(cfg: config_mod.Config) -> None:
    """Send `harmonist.*` logs (with tracebacks) to stdout so they show up in
    `docker logs`.

    Without this, the only handler on the `harmonist` logger is the activity
    feed mirror (`activity.install_log_handler`), which records just the
    message text and drops `exc_info`. Because that handler *exists*, Python's
    `logging.lastResort` stderr fallback is suppressed — so a `log.exception`
    in a background thread surfaces as a one-line flash with no stack trace
    anywhere. A real stream handler with a formatter fixes that.

    Idempotent: `create_app()` runs many times under test. The level always
    tracks the current config; the stdout handler is installed once.
    """
    global _logging_configured
    level = getattr(logging, cfg.log_level.upper(), logging.INFO)
    logger = logging.getLogger("harmonist")
    logger.setLevel(level)

    # Quiet bandcampsync's own loggers (named "ignores", "sync", … — see its
    # logger.py). They flood every sync with one line per purchase: a
    # "Syncing item N of M" (INFO) and, worse, a "Skipping item … present in
    # the ignore file" (WARNING — for a perfectly NORMAL already-downloaded
    # item) for all ~400. Raise their thresholds so genuine third-party
    # problems still surface but the per-item normal-operation chatter doesn't.
    # Honour DEBUG: if the operator asked for DEBUG, leave them verbose.
    # (Left "bandcamp" at INFO — its "Found item …" lines are per-item but
    # have been useful for diagnosing matching; revisit in the log audit, #53.)
    if level > logging.DEBUG:
        logging.getLogger("ignores").setLevel(logging.ERROR)
        logging.getLogger("sync").setLevel(logging.WARNING)

    if not _logging_configured:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        logger.addHandler(handler)
        # We own the harmonist logger's output; don't also bubble to the root
        # logger (avoids duplicate lines if anything ever configures root).
        logger.propagate = False
        _logging_configured = True


def _validate_runtime_paths(cfg: config_mod.Config) -> None:
    """Log the process uid/gid and verify the music + config dirs are writable.

    A bind-mount permission problem otherwise surfaces as a silent "jam" — the
    scan/reconcile runs but every sidecar/config write fails — so fail fast at
    startup with an actionable message. Gates startup from the lifespan.
    """
    ids = ""
    if hasattr(os, "getuid"):
        ids = f"uid={os.getuid()} gid={os.getgid()} groups={sorted(os.getgroups())}"
    log.info("Harmonist starting (%s)", ids or "user id unavailable on this platform")
    for label, d in (("music", cfg.paths.music_dir), ("config", cfg.paths.config_dir)):
        try:
            d.mkdir(parents=True, exist_ok=True)
            probe = d / f".harmonist-write-test-{os.getpid()}"
            probe.touch()
            probe.unlink()
        except OSError as e:
            raise RuntimeError(
                f"The {label} directory {d} is not writable by this process "
                f"({ids or 'current user'}): {e}. Harmonist must write "
                f"{'sidecars + cover art' if label == 'music' else 'config + the id registry'} "
                f"there. Fix the bind-mount's ownership/permissions — set the container's "
                f"`user:` to the directory's owner (`id -u`/`id -g`), or chown the directory "
                f"— and restart."
            ) from e
    log.info("Path check OK — %s and %s are writable", cfg.paths.music_dir, cfg.paths.config_dir)


def create_app(
    cfg: config_mod.Config | None = None,
    *,
    tagger: Tagger | None = None,
) -> FastAPI:
    """Application factory. Tests can pass a pre-built config and/or a swap-in
    tagger implementation.
    """
    if cfg is None:
        cfg = config_mod.load()
    if tagger is None:
        tagger = PicardCompatibleTagger()

    _configure_logging(cfg)
    mb_lookup.configure(cfg.musicbrainz.user_agent)
    activity.install_log_handler()

    sync_runner = SyncRunner(runner_fn=lambda: None)  # placeholder, replaced below
    scan_runner = ScanRunner(cfg.paths.music_dir)

    if cfg.demo_mode:
        from harmonist import demo

        log.warning(
            "DEMO MODE ACTIVE — mocked MusicBrainz/Bandcamp, sandboxed music dir at %s "
            "(the configured music_dir is NOT touched)",
            cfg.paths.music_dir,
        )
        demo.install()
        demo.ensure_seeded(cfg.paths.music_dir)

        def runner_fn() -> Any:
            result = demo.run_demo_sync(
                cfg.paths.music_dir, progress_callback=sync_runner.set_current_item
            )
            # Downloads done; the status bar shouldn't stay pinned to the last
            # album's name while we wrap up.
            sync_runner.set_current_item("finishing up…")
            scan_runner.request_scan()  # downloads landed → refresh the snapshot
            return result
    else:

        def resolve_after_download(album_dir: Path) -> None:
            # Each freshly-downloaded album: look up its store URL on MB and
            # tag immediately, so an in-MB release lands straight in the
            # Library rather than waiting in NEEDS_MBID for a manual Recheck.
            _resolve_by_store_url(album_dir, cfg, tagger)

        def runner_fn() -> Any:
            # Albums waiting to link to a purchase (NEEDS_SYNC) usually need an
            # OLD purchase that an incremental sync wouldn't re-page — so the
            # backfill could never see it. Force a full collection re-page when
            # any exist (clear the checkpoint; bandcampsync rewrites a fresh one
            # at the end, so later syncs go back to incremental). Self-limiting:
            # a full sync resolves every NEEDS_SYNC album (link OR surrender).
            _force_full_sync_if_pending_links(cfg, scan_runner)
            result = _run_bandcamp_sync(
                cfg,
                progress_callback=sync_runner.set_current_item,
                post_download_callback=resolve_after_download,
            )
            # Downloads are done; the remaining work (mis-tag detection, the
            # unmatched report, the rescan) can take a few seconds. Re-label the
            # status so it doesn't sit pinned to the last album's name.
            sync_runner.set_current_item("finishing up — checking matches…")
            # Spot mis-tags first (release-group join → demote to Needs MBID
            # with a suggestion), then handle whatever's genuinely still
            # unlinked. Both write to the log + Activity.
            _detect_mistags_after_sync(cfg, result)
            # `collection_checkpoint_token is None` means bandcampsync paged the
            # WHOLE collection (no checkpoint applied) — only then is "no matching
            # purchase" conclusive enough to surrender an album to NEEDS_MBID.
            full_sync = getattr(result, "collection_checkpoint_token", None) is None
            _report_unmatched_after_sync(cfg, full_sync=full_sync)
            scan_runner.request_scan()  # downloads landed → refresh the snapshot
            return result

    sync_runner._runner_fn = runner_fn

    # Paths the user has explicitly Forgot. Exempted from auto-reconcile so
    # the runner doesn't immediately undo the user's intent. In-memory only:
    # restart clears the set (acceptable tradeoff per user feedback).
    forgotten_paths: set[Path] = set()

    def reconcile_runner_fn(status_updater: Callable[..., None]) -> None:
        # Scan ONCE, when the whole pass is done — not mid-pass. Rebuilding the
        # snapshot repeatedly while reconcile runs means a full filesystem walk
        # every few seconds, which is punishing on a network mount. The status
        # bar (reading reconcile.status directly) carries live progress
        # meanwhile; the inbox/library counts snap to correct on completion.
        reconcile_pending_orphans(
            cfg.paths.music_dir,
            fetch_urls=mb_lookup.fetch_release_urls,
            status_updater=status_updater,
            exempt_paths=forgotten_paths,
        )
        scan_runner.request_scan()  # sidecars written → refresh the snapshot

    reconcile_runner = ReconcileRunner(runner_fn=reconcile_runner_fn)

    project_root = Path(__file__).resolve().parent.parent.parent.parent
    templates_dir = project_root / "templates"
    static_dir = project_root / "static"
    templates = Jinja2Templates(directory=str(templates_dir))
    templates.env.globals["harmony_base"] = HARMONY_BASE
    templates.env.globals["AlbumState"] = AlbumState
    templates.env.globals["store_name"] = store_name
    templates.env.globals["display_path"] = _display_path
    templates.env.globals["rel_path"] = _rel_path
    templates.env.globals["demo_mode"] = cfg.demo_mode
    # Evaluated per-render (callable, not a constant) so the header's
    # Sync/Set-up button flips the moment cookies are saved.
    templates.env.globals["bandcamp_configured"] = lambda: _bandcamp_configured(cfg)
    # Cache-bust the CSS link by the bundle's mtime, so a rebuilt stylesheet is
    # always re-fetched — a newly-added utility class can't be missed because
    # the browser served a stale bundle. Re-read per render (cheap stat) so a
    # `make css` during dev takes effect without a server restart.
    css_file = static_dir / "harmonist.css"
    templates.env.globals["css_version"] = lambda: (
        int(css_file.stat().st_mtime) if css_file.exists() else 0
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        # Fail fast on a bind-mount permission problem (otherwise it looks like
        # a silent scan/reconcile jam) and log the process uid/gid.
        _validate_runtime_paths(cfg)
        # Engage the background scanner once the event loop is running, kicking
        # the initial library scan off the request path.
        scan_runner.attach_loop()
        # Watch the music dir so files added/removed outside the app (manual
        # copies) trigger a rescan. Fires only on local mounts (see dir_watcher).
        watch_stop = asyncio.Event()
        watch_task = asyncio.create_task(
            dir_watcher.watch_music_dir(
                cfg.paths.music_dir,
                scan_runner.request_scan,
                settle_seconds=cfg.library.watch_settle_seconds,
                stop_event=watch_stop,
            )
        )
        try:
            yield
        finally:
            watch_stop.set()
            watch_task.cancel()
            with suppress(asyncio.CancelledError):
                await watch_task

    app = FastAPI(title="Harmonist", lifespan=lifespan)
    app.state.cfg = cfg
    app.state.templates = templates
    app.state.sync_runner = sync_runner
    app.state.reconcile_runner = reconcile_runner
    app.state.scan_runner = scan_runner
    app.state.forgotten_paths = forgotten_paths
    app.state.tagger = tagger

    @app.middleware("http")
    async def _rescan_after_mutation(request: Request, call_next: Any) -> Response:
        response: Response = await call_next(request)
        # A state-changing request likely touched the library (tag, forget,
        # confirm, erase…). Trigger a background re-scan; the per-album mtime
        # cache keeps it cheap, and request_scan() is a no-op until engaged.
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            request.app.state.scan_runner.request_scan()
        return response

    _install_security_middleware(app, cfg)

    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    _register_routes(app)
    if cfg.demo_mode:
        _register_demo_routes(app)
    return app


def _install_security_middleware(app: FastAPI, cfg: config_mod.Config) -> None:
    """Install the security stack from inside out.

    Starlette wraps middleware in registration order so that the *last*
    one added is the outermost. We want hostname rejection to happen
    first (cheapest, blocks DNS rebinding before any other code runs),
    then CSRF (no DB lookup, fast reject), then optional auth (innermost
    so a failed auth challenge doesn't expose internal headers to
    untrusted hosts). Hence: auth → CSRF → trusted-host, in that order.
    """
    if cfg.auth.enabled:
        if not cfg.auth.username or not cfg.auth.password_hash:
            log.error(
                "auth.enabled=true but auth.username/password_hash is empty; "
                "REFUSING TO START with broken auth. Run "
                "`python -m harmonist.web.security` to generate a password hash."
            )
            raise RuntimeError("auth.enabled requires username and password_hash")
        app.add_middleware(
            BasicAuthMiddleware,
            username=cfg.auth.username,
            password_hash=cfg.auth.password_hash,
        )

    app.add_middleware(CSRFMiddleware)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=cfg.server.allowed_hosts)

    # Best-effort warning: a non-loopback bind with a permissive host
    # allow-list is the configuration that hands the worst-case DNS-
    # rebinding attack to a passing browser. We don't refuse to start
    # — some setups (Docker behind a trusted proxy) intentionally use
    # ["*"] — but we want this to land in the logs.
    if cfg.server.host not in ("127.0.0.1", "localhost", "::1") and cfg.server.allowed_hosts == [
        "*"
    ]:
        log.warning(
            "server.host=%s but server.allowed_hosts=['*']. For non-loopback "
            "binds, set allowed_hosts to your actual hostname(s) to enable "
            "DNS-rebinding protection. See README §Security.",
            cfg.server.host,
        )


def _register_demo_routes(app: FastAPI) -> None:
    from harmonist import demo

    @app.post("/demo/reset", response_class=HTMLResponse)
    def demo_reset(request: Request) -> Response:
        try:
            demo.reset(request.app.state.cfg.paths.music_dir)
        except RuntimeError as e:
            return _flash_response(
                "Demo reset failed", str(e), level="error", tasks_changed=False, status_code=400
            )
        return _flash_response("Demo data reset", "back to original state")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _display_path(p: Path | str) -> str:
    """Friendlier path for the UI: abbreviate the home dir to ~. Absolute
    paths rarely mean anything to the user; the tail is what matters."""
    path = Path(p)
    try:
        return "~/" + str(path.relative_to(Path.home()))
    except ValueError:
        return str(path)


def _rel_path(p: Path | str, base: Path | str) -> str:
    """Album path shown relative to the music root (full paths are noise)."""
    try:
        return str(Path(p).relative_to(base))
    except ValueError:
        return _display_path(p)


# Libraries Harmonist builds on, for the About page. (name, pip distribution or
# None for non-Python deps, homepage, licence). Versions are filled in live.
_CREDITS: list[tuple[str, str | None, str, str]] = [
    ("FastAPI", "fastapi", "https://fastapi.tiangolo.com", "MIT"),
    ("Uvicorn", "uvicorn", "https://www.uvicorn.org", "BSD-3-Clause"),
    ("Pydantic", "pydantic", "https://docs.pydantic.dev", "MIT"),
    ("Jinja2", "jinja2", "https://jinja.palletsprojects.com", "BSD-3-Clause"),
    ("HTMX", None, "https://htmx.org", "0BSD"),
    ("Tailwind CSS", None, "https://tailwindcss.com", "MIT"),
    ("mutagen", "mutagen", "https://mutagen.readthedocs.io", "GPL-2.0-or-later"),
    (
        "musicbrainzngs",
        "musicbrainzngs",
        "https://python-musicbrainzngs.readthedocs.io",
        "BSD-2-Clause",
    ),
    ("bandcampsync", "bandcampsync", "https://github.com/meeb/bandcampsync", "BSD-3-Clause"),
    ("HTTPX", "httpx", "https://www.python-httpx.org", "BSD-3-Clause"),
    ("tomlkit", "tomlkit", "https://github.com/python-poetry/tomlkit", "MIT"),
]


def _app_version() -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("harmonist")
    except PackageNotFoundError:
        return "dev"


def _credits() -> list[dict[str, str]]:
    from importlib.metadata import PackageNotFoundError, version

    out: list[dict[str, str]] = []
    for name, dist, url, lic in _CREDITS:
        ver = ""
        if dist:
            try:
                ver = version(dist)
            except PackageNotFoundError:
                ver = ""
        out.append({"name": name, "version": ver, "url": url, "license": lic})
    return out


def _templates(request: Request) -> Jinja2Templates:
    """Typed accessor for the app's Jinja2Templates. `app.state` is dynamically
    typed (Any), so going through here keeps route return types as Response."""
    templates: Jinja2Templates = request.app.state.templates
    return templates


def _ctx(request: Request, **extra: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "request": request,
        "cfg": request.app.state.cfg,
        "now": datetime.now(UTC),
    }
    base.update(extra)
    return base


# bandcampsync's collection checkpoint (Syncer.STATE_FILENAME) — lives in the
# music dir root. Hardcoded to avoid importing bandcampsync internals here.
_BANDCAMPSYNC_STATE_FILE = ".bandcampsync-state.json"


def _clear_bandcampsync_checkpoint(music_dir: Path) -> bool:
    """Remove bandcampsync's collection-checkpoint file if present. Returns
    True if a file was removed. Never raises — best-effort."""
    state_file = music_dir / _BANDCAMPSYNC_STATE_FILE
    try:
        if state_file.is_file():
            state_file.unlink()
            return True
    except OSError as e:
        log.warning("could not remove bandcampsync checkpoint %s: %s", state_file, e)
    return False


def _force_full_sync_if_pending_links(cfg: config_mod.Config, scan_runner: ScanRunner) -> None:
    """If any album is waiting to link to a Bandcamp purchase (NEEDS_SYNC),
    clear the collection checkpoint so the upcoming sync re-pages the WHOLE
    collection. Their purchase is usually an old one that an incremental sync
    wouldn't load, so the backfill could never see it. A full sync resolves
    every NEEDS_SYNC album (links it, or surrenders it to NEEDS_MBID), so this
    is self-limiting — once they're cleared, syncs return to incremental.

    Uses the scanner's existing snapshot (no fresh walk) when available.
    Best-effort: never raises into the sync runner.
    """
    try:
        albums = (
            scan_runner.albums() if scan_runner.is_engaged() else scanner.scan(cfg.paths.music_dir)
        )
        pending = sum(1 for a in albums if a.state == AlbumState.NEEDS_SYNC)
        if pending and _clear_bandcampsync_checkpoint(cfg.paths.music_dir):
            log.info(
                "Forcing a full Bandcamp sync: %d album(s) await a purchase link",
                pending,
            )
    except Exception:
        log.exception("force-full-sync check failed")


# Mis-tag detection does ~1 MB browse per still-unlinked album, so it's bounded
# by Set A (the "unmatched after sync" albums) — NOT by the collection. If even
# Set A is this large after a sync, something's off; bail rather than storm MB.
_MISTAG_DETECTION_MAX_ALBUMS = 200


class _UnmatchedSource(Protocol):
    """Structural type for mis-tag detection's only dependency on the syncer:
    the list of owned purchases that linked to no album. A real
    `HarmonistSyncer` satisfies it, as does any test double."""

    def unmatched_purchases(self) -> list[tuple[str, str]]: ...


def _release_group_id(release: Release) -> str | None:
    g = release.get("release-group") or {}
    rg = g.get("id")
    return str(rg) if rg else None


def _release_name_parts(release: Release) -> tuple[str, str]:
    """Split an MB release into ('Artist / Title', 'disambiguation') so the UI
    can render the disambiguation visually distinct from the title (as MB does).
    The disambiguation is "" when the release has none."""
    artist = (release.get("artist-credit-phrase") or "").strip()
    if not artist:
        parts = []
        for ac in release.get("artist-credit") or []:
            if isinstance(ac, dict):
                parts.append(ac.get("name") or ac.get("artist", {}).get("name", ""))
        artist = "".join(parts).strip()
    title = (release.get("title") or "").strip()
    name = f"{artist} / {title}" if artist else title
    return name, (release.get("disambiguation") or "").strip()


def _demote_to_needs_mbid(
    album_path: Path, sc: Sidecar, *, candidate: MatchCandidate | None
) -> None:
    """Drop a mis-tagged album back to NEEDS_MBID so the user can re-match it:
    clear the wrong MBID but KEEP the store_url, and pre-load the correct
    release as `mb_match_candidate` — the NEEDS_MBID card then shows the
    side-by-side and a one-click Confirm."""
    sidecar_mod.write(
        album_path,
        Sidecar(
            schema_version=sc.schema_version,
            store_url=sc.store_url,
            bandcamp=sc.bandcamp,
            downloaded_at=sc.downloaded_at,
            added_at=sc.added_at,
            mb_release_id=None,
            mb_match_candidate=candidate,
            tagged_at=None,
            track_count_expected=None,
            notes=sc.notes,
        ),
    )


def _detect_mistags_after_sync(
    cfg: config_mod.Config,
    syncer: _UnmatchedSource,
    *,
    browse_rg: Callable[[str], list[tuple[str, list[str]]]] = (
        mb_lookup.browse_release_group_releases
    ),
    fetch_release: Callable[[str], Release] = mb_lookup.fetch_release,
) -> None:
    """Spot mis-tags driven by the "unmatched after sync" albums.

    For each on-disk NEEDS_SYNC album (tagged, but no purchase linked), look up
    the *other editions in its MusicBrainz release group* and check whether the
    user OWNS one of them (a Bandcamp purchase that linked to no album). If an
    owned sibling edition differs from the tag, the album is the same record,
    mis-tagged (e.g. 24-bit files tagged as the standard release while you own
    the 24-bit on Bandcamp) — demote it to NEEDS_MBID with that edition
    suggested.

    Cost is bounded by the unmatched-album set (one browse per album), NOT by
    the collection: the owned purchases are just an in-memory slug set we test
    membership against. Best-effort; never raises into the sync runner.
    """
    try:
        # Owned-but-unlinked purchases → a slug set (no MB calls). album_slug is
        # subdomain-agnostic, so a label vs artist page for the same edition
        # still matches.
        owned: dict[str, tuple[str, str]] = {}  # slug -> (url, label)
        for url, label in syncer.unmatched_purchases():
            slug = album_slug(url)
            if slug:
                owned.setdefault(slug, (url, label))

        albums = [
            a
            for a in scanner.scan(cfg.paths.music_dir)
            if a.state == AlbumState.NEEDS_SYNC and a.sidecar and a.sidecar.mb_release_id
        ]
    except Exception:
        log.exception("mis-tag detection: setup failed")
        return
    if not albums or not owned:
        return
    if len(albums) > _MISTAG_DETECTION_MAX_ALBUMS:
        activity.record(
            f"Mis-tag detection skipped: {len(albums)} unmatched albums after sync exceeds "
            f"the cap of {_MISTAG_DETECTION_MAX_ALBUMS} — something looks wrong with this sync.",
            level="warning",
        )
        return

    # Only act on release groups with exactly one unmatched album — otherwise we
    # can't tell which album an owned release pairs with. Keep each album's
    # currently-tagged (wrong) release so we can name it in the UI without a
    # second fetch.
    rg_albums: dict[str, list[tuple[Album, Release]]] = {}
    for a in albums:
        assert a.sidecar is not None  # guaranteed by the comprehension filter
        assert a.sidecar.mb_release_id is not None
        try:
            tagged_release = fetch_release(a.sidecar.mb_release_id)
        except mb_lookup.MBError:
            continue
        rg = _release_group_id(tagged_release)
        if rg:
            rg_albums.setdefault(rg, []).append((a, tagged_release))

    for rg, albs in rg_albums.items():
        if len(albs) != 1:
            continue  # ambiguous: multiple unmatched albums in this group
        album, tagged_release = albs[0]
        assert album.sidecar is not None
        tagged = album.sidecar.mb_release_id
        try:
            siblings = browse_rg(rg)
        except mb_lookup.MBError:
            continue
        # Releases in this group the user OWNS (Bandcamp URL slug in `owned`),
        # other than the one it's currently tagged as.
        owned_siblings = {
            mbid: s
            for mbid, urls in siblings
            if mbid != tagged
            for u in urls
            if (s := album_slug(u)) in owned
        }
        if len(owned_siblings) != 1:
            continue  # 0 = not a mis-tag; ≥2 = you own several releases, ambiguous
        owned_mbid, owned_slug = next(iter(owned_siblings.items()))
        url, label = owned[owned_slug]
        try:
            rel = fetch_release(owned_mbid)
        except mb_lookup.MBError:
            continue
        candidate = best_match(album.path, [rel])
        if candidate is not None:
            # Mis-tag provenance as STRUCTURED fields, not a free-text note — so
            # the UI can name both releases (each linked to MB, disambiguation
            # rendered distinctly) and the purchase URL, separate from the
            # matcher's technical notes (file/track count).
            owned_name, owned_disambig = _release_name_parts(rel)
            tagged_name, tagged_disambig = _release_name_parts(tagged_release)
            candidate.mistag_owned_url = url
            candidate.mistag_owned_label = owned_name
            candidate.mistag_owned_disambig = owned_disambig
            candidate.mistag_tagged_mbid = tagged
            candidate.mistag_tagged_label = tagged_name
            candidate.mistag_tagged_disambig = tagged_disambig
            candidate.mistag_release_group_mbid = rg
        _demote_to_needs_mbid(album.path, album.sidecar, candidate=candidate)
        activity.record(
            f"Possible mis-tag: {album.artist} — {album.title}. You own “{label}” on "
            f"Bandcamp ({url}) — the same release group but a different release than it's "
            f"tagged as. Moved to Needs MBID with {owned_mbid} suggested; confirm to re-tag.",
            level="warning",
        )


def _report_unmatched_after_sync(cfg: config_mod.Config, *, full_sync: bool) -> None:
    """After a sync, handle albums still lacking a Bandcamp link.

    An album reaches `NEEDS_SYNC` with `bandcamp.item_id` still unset when the
    sync's store_url + slug + title match couldn't tie it to a purchase.

    What we do depends on whether the WHOLE collection was paged:

    - **Full sync** (`full_sync=True`, no collection checkpoint applied): we've
      genuinely seen every purchase and still can't link it, so we stop nagging
      and hand control to the user — drop the album back to NEEDS_MBID, keeping
      its current release as a *read-only* suggestion (`unmatched_purchase`) plus
      a "couldn't find a purchase" note. From there they can seed the release on
      Harmony or fix the store URL.
    - **Partial sync** (checkpoint-limited): the purchase may simply not have
      been paged this run, so we must NOT demote — just warn, pointing at the
      manual fix. A later full sync resolves or surrenders it.

    Best-effort: never raises into the sync runner.
    """
    try:
        albums = scanner.scan(cfg.paths.music_dir)
    except Exception:
        log.exception("post-sync unmatched scan failed")
        return
    unmatched = [a for a in albums if a.state == AlbumState.NEEDS_SYNC]
    if not unmatched:
        log.info("Sync: all Bandcamp-sourced albums are linked")
        return

    if not full_sync:
        # Partial sync — only warn; the purchase may be below the checkpoint.
        log.info(
            "Sync: %d album(s) not linked to a Bandcamp purchase (partial sync — "
            "not demoting; a full sync will resolve or surrender them)",
            len(unmatched),
        )
        for a in unmatched:
            store_url = a.sidecar.store_url if a.sidecar else None
            activity.record(
                f"Not linked to a Bandcamp purchase: {a.artist} — {a.title} "
                f"[{store_url or 'no store URL'}] (use 'Try a different URL' to link it)",
                level="warning",
            )
        return

    # Albums already LINKED to a purchase, keyed by release — used to flag a
    # surrendered album that's tagged as the SAME release as a linked one (a
    # likely duplicate copy, OR a legitimate release split across directories —
    # we don't try to tell them apart here, just surface it).
    linked_by_release: dict[str, list[Album]] = {}
    for a in albums:
        s = a.sidecar
        if s and s.mb_release_id and s.bandcamp and s.bandcamp.item_id is not None:
            linked_by_release.setdefault(s.mb_release_id, []).append(a)

    # Full sync: surrender — the whole collection was paged and these still have
    # no matching purchase. Drop each back to NEEDS_MBID for manual resolution.
    for a in unmatched:
        sc = a.sidecar
        if sc is None or not sc.mb_release_id:
            continue  # nothing to keep as a suggestion
        twins = linked_by_release.get(sc.mb_release_id, [])
        candidate = MatchCandidate(
            mb_release_id=sc.mb_release_id,
            confidence="exact",  # the files are already tagged with this release
            file_count=a.track_count,
            track_count=a.track_count,
            unmatched_purchase=True,
        )
        _demote_to_needs_mbid(a.path, sc, candidate=candidate)
        activity.record(
            f"No Bandcamp purchase matched {a.artist} — {a.title} "
            f"[{sc.store_url or 'no store URL'}]. Moved to Needs MBID (still tagged "
            f"correctly) — seed/fix the release on Harmony or assign a different one.",
            level="warning",
        )
        if twins:
            activity.record(
                f"Heads up: {a.artist} — {a.title} is tagged as the same MusicBrainz "
                f"release as “{twins[0].title}” ({twins[0].path.name}), which already "
                f"linked to a purchase — possibly a duplicate copy, or a release split "
                f"across directories.",
                level="warning",
            )


def _embedded_cover(album_path: Path) -> tuple[bytes, str] | None:
    """Extract embedded cover art (bytes, mime) from the album's first audio
    file, or None. Used by /cover to serve art without writing it to disk."""
    try:
        files = sorted(p for p in album_path.iterdir() if formats.is_supported(p))
    except OSError:
        return None
    if not files:
        return None
    return formats.read_cover(files[0])


def _albums(request: Request) -> list[Album]:
    cfg: config_mod.Config = request.app.state.cfg
    runner: ScanRunner = request.app.state.scan_runner
    # In production the background scanner is engaged (lifespan ran): serve its
    # snapshot instantly — never walk the tree on the request path. In unit
    # tests the TestClient is built without the lifespan, so the runner isn't
    # engaged and we scan synchronously, preserving request-time freshness.
    if runner.is_engaged():
        return runner.albums()
    return scanner.scan(cfg.paths.music_dir)


def _find_album(request: Request, album_id: str) -> Album:
    """Look up an album by its canonical id (mb_release_id, temp_uid, or
    registry UUID for NEW albums). Falls back to a registry reverse lookup
    so a stale inbox URL still works when auto-reconcile has rewritten
    the album's identity between page render and click. 404 only when
    we can't resolve the id any way.

    URLs to sidecar'd albums are stable across directory renames (the
    UUID lives in the sidecar JSON which moves with the directory).
    """
    from harmonist import id_registry

    albums = _albums(request)
    for a in albums:
        if a.id == album_id:
            return a
    # Race fallback: the rendered page may hold a registry UUID for an
    # album whose canonical id has since changed (auto-reconcile beat the
    # user). The registry remembers the original path → UUID, so look up
    # by path.
    legacy_path = id_registry.path_for(album_id)
    if legacy_path is not None:
        for a in albums:
            if a.path == legacy_path:
                return a
    raise HTTPException(status.HTTP_404_NOT_FOUND, f"album {album_id} not found")


def _inbox_albums(albums: list[Album]) -> list[Album]:
    """Albums that warrant attention in the inbox (terminal states excluded)."""
    return [a for a in albums if a.state not in _TERMINAL_STATES]


def _bandcamp_configured(cfg: config_mod.Config) -> bool:
    """True when a non-empty cookies file is present, i.e. Bandcamp sync
    has been set up. Drives the header's Sync vs Set-up button.

    Always True in demo mode — demo sync is mocked and needs no real
    cookies, so the Sync button should be available out of the box.
    """
    if cfg.demo_mode:
        return True
    try:
        f = cfg.cookies_file
        return f.exists() and f.stat().st_size > 0
    except OSError:
        return False


def _run_bandcamp_sync(
    cfg: config_mod.Config,
    *,
    progress_callback: Callable[[str], None] | None = None,
    post_download_callback: Callable[[Path], None] | None = None,
) -> HarmonistSyncer:
    """Build a HarmonistSyncer and let it run end-to-end."""
    if not cfg.cookies_file.exists():
        raise FileNotFoundError(
            f"cookies file not found at {cfg.cookies_file} — Bandcamp sync requires a cookies.txt"
        )
    cookies = cfg.cookies_file.read_text(encoding="utf-8")
    cfg.paths.music_dir.mkdir(parents=True, exist_ok=True)
    cfg.ignores_file.parent.mkdir(parents=True, exist_ok=True)
    return HarmonistSyncer(
        cookies=cookies,
        # bandcampsync's LocalMedia uses .iterdir() / Path arithmetic on
        # whatever we hand it — must be a Path, not a string.
        dir_path=cfg.paths.music_dir,
        media_format=cfg.bandcamp.download_format,
        temp_dir_root=None,
        ign_file_path=str(cfg.ignores_file),
        ign_patterns="",
        notify_url=None,
        max_downloads_per_sync=cfg.bandcamp.max_downloads_per_sync,
        progress_callback=progress_callback,
        post_download_callback=post_download_callback,
    )


def _apply_best_match(
    album_path: Path, mbids: list[str], cfg: config_mod.Config, tagger: Tagger
) -> tuple[str, str]:
    """Fetch every candidate MB release, pick the best fit, then tag or stash.

    A Bandcamp URL can resolve to several MB releases; we assess the album
    against each and act on the strongest match (``match.best_match``).

    Returns (status, message) where status is
    'tagged' | 'needs_confirmation' | 'no_match'.
    """
    releases = [mb_lookup.fetch_release(m) for m in mbids]
    candidate = best_match(album_path, releases)
    if candidate is None:
        return "no_match", "No MusicBrainz release linked."

    if candidate.confidence == "exact":
        _tag_with_release(album_path, candidate.mb_release_id, cfg, tagger)
        return "tagged", "Match exact — files tagged."

    existing = sidecar_mod.read(album_path)
    new = Sidecar(
        schema_version=CURRENT_SCHEMA_VERSION,
        store_url=existing.store_url if existing else None,
        bandcamp=existing.bandcamp if existing else None,
        downloaded_at=existing.downloaded_at if existing else None,
        added_at=(existing.added_at if existing else None) or datetime.now(UTC),
        mb_release_id=None,
        mb_match_candidate=candidate,
        tagged_at=existing.tagged_at if existing else None,
        notes=existing.notes if existing else None,
    )
    sidecar_mod.write(album_path, new)
    return (
        "needs_confirmation",
        f"Match found ({candidate.confidence}) — please review and confirm.",
    )


def _tag_with_release(
    album_path: Path,
    mbid: str,
    cfg: config_mod.Config,
    tagger: Tagger,
    *,
    incomplete: bool = False,
    store_url_override: str | None = None,
) -> None:
    """Fetch MB release, fetch cover, write tags, update sidecar.

    `incomplete=True` runs the tagger in incomplete mode (file_count <
    MB track count allowed) and persists track_count_expected on the
    sidecar so the scanner can derive INCOMPLETE on future scans.

    `store_url_override` replaces the sidecar's store_url. Used when confirming
    a mis-tag: the album is actually the *owned* edition, so its store_url must
    become the URL where the user purchased it (the candidate's
    `mistag_owned_url`) — otherwise the old (wrong-edition) URL matches no
    purchase and the album can never link, falling through to surrender.
    """
    release = mb_lookup.fetch_release(mbid)
    rg = release.get("release-group") or {}
    cover_path = cover_art.ensure_cover(
        album_path,
        release_mbid=release["id"],
        release_group_mbid=rg.get("id"),
        size=cfg.cover_art.size,
    )
    tagger.tag_album(album_path, release, cover_path=cover_path, incomplete=incomplete)

    track_count_expected = sum(len(m.get("track-list", [])) for m in release.get("medium-list", []))

    sc = sidecar_mod.read(album_path)
    store_url = store_url_override or (sc.store_url if sc else None)
    if store_url is None:
        # No store_url yet (e.g. a manual download assigned an MBID directly).
        # Derive the Bandcamp store URL so a purchase lands in Needs Sync rather
        # than Complete: embedded ©cmt URL → MB url-rel → artist-root placeholder,
        # all gated by ©cmt Bandcamp evidence. Best-effort — never blocks tagging.
        try:
            store_url = reconcile.store_url_for_tagging(
                album_path, mbid, fetch_urls=mb_lookup.fetch_release_urls
            )
        except Exception:
            log.exception("store_url derivation during tagging failed")
    new = Sidecar(
        schema_version=CURRENT_SCHEMA_VERSION,
        store_url=store_url,
        bandcamp=sc.bandcamp if sc else None,
        downloaded_at=sc.downloaded_at if sc else None,
        added_at=(sc.added_at if sc else None) or datetime.now(UTC),
        mb_release_id=mbid,
        mb_match_candidate=None,  # cleared on tag
        tagged_at=datetime.now(UTC),
        track_count_expected=track_count_expected,
        notes=sc.notes if sc else None,
    )
    sidecar_mod.write(album_path, new)


def _resolve_by_store_url(album_path: Path, cfg: config_mod.Config, tagger: Tagger) -> str:
    """Auto-resolve a sidecar's store_url against MusicBrainz.

    Used right after a Bandcamp download so a release that IS in MB goes
    straight to COMPLETE (Library) instead of waiting in NEEDS_MBID for a
    manual Recheck. Looks up the store URL, and on a match runs the normal
    match assessment: exact → tag (COMPLETE), approximate → stash candidate
    (NEEDS_MBID with a suggestion shown), no match → NEEDS_MBID. Never raises — returns a
    short status string for logging.
    """
    sc = sidecar_mod.read(album_path)
    if sc is None or not sc.store_url or sc.mb_release_id:
        return "skipped"  # nothing to resolve, or already resolved
    try:
        mbids = mb_lookup.lookup_by_bandcamp_url(sc.store_url)
        if not mbids:
            activity.record(f"Synced {album_path.name} — no MusicBrainz match yet", "info")
            return "no_match"
        status_str, _ = _apply_best_match(album_path, mbids, cfg, tagger)
        if status_str == "tagged":
            activity.record(f"Auto-tagged {album_path.name} from MusicBrainz after sync", "info")
        else:
            activity.record(f"Synced {album_path.name} — MusicBrainz suggestion to review", "info")
        return status_str
    except Exception as e:
        log.warning("auto-resolve failed for %s: %s", album_path, e)
        return "error"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def _register_routes(app: FastAPI) -> None:

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> Response:
        albums = _albums(request)
        ctx = _ctx(
            request,
            albums=_inbox_albums(albums),
            total_albums=len(albums),
            sync_status=request.app.state.sync_runner.status(),
        )
        return _templates(request).TemplateResponse(request, "index.html", ctx)

    @app.get("/tasks", response_class=HTMLResponse)
    def tasks(request: Request) -> Response:
        albums = _albums(request)
        # Capture reconcile status BEFORE (maybe) kicking a new pass, so THIS
        # render reflects only a genuinely in-flight reconcile. A pass this very
        # request starts shouldn't flip the inbox to "Reconciling…" on the same
        # response — it surfaces on the next poll. (Otherwise opening the inbox
        # with any NEW album would always flash "Reconciling".)
        reconcile_status = request.app.state.reconcile_runner.status()
        # Auto-kick the reconciler ONLY when there's an orphan it can actually
        # resolve: a NEW album whose tags carry an MBID, and which the user
        # hasn't Forgotten. Reconcile writes a sidecar for every such album, so
        # it leaves NEW — meaning a finished pass clears its own trigger and we
        # don't re-fire on incidental inbox refreshes (after a Recheck, a tag,
        # etc.). Untagged orphans are never reconcilable, so they never kick it.
        forgotten: set[Path] = request.app.state.forgotten_paths
        if any(
            a.state == AlbumState.NEW and a.has_tag_mbid and a.path not in forgotten for a in albums
        ):
            request.app.state.reconcile_runner.start()
        ctx = _ctx(
            request,
            albums=_inbox_albums(albums),
            total_albums=len(albums),
            scan=request.app.state.scan_runner.status(),
            reconcile=reconcile_status,
            sync=request.app.state.sync_runner.status(),
        )
        return _templates(request).TemplateResponse(request, "tasks.html", ctx)

    @app.get("/activity", response_class=HTMLResponse)
    def activity_feed(request: Request) -> Response:
        ctx = _ctx(request, events=activity.recent(100))
        return _templates(request).TemplateResponse(request, "partials/activity.html", ctx)

    @app.get("/about", response_class=HTMLResponse)
    def about_page(request: Request) -> Response:
        ctx = _ctx(request, app_version=_app_version(), credits=_credits())
        return _templates(request).TemplateResponse(request, "about.html", ctx)

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request) -> Response:
        cfg: config_mod.Config = request.app.state.cfg
        ctx = _ctx(
            request,
            bandcamp_ok=_bandcamp_configured(cfg),
            sidecar_count=sidecar_mod.count_all(cfg.paths.music_dir),
        )
        return _templates(request).TemplateResponse(request, "settings.html", ctx)

    @app.post("/settings/erase-sidecars", response_class=HTMLResponse)
    def erase_sidecars(request: Request) -> Response:
        cfg: config_mod.Config = request.app.state.cfg
        removed = sidecar_mod.delete_all(cfg.paths.music_dir)
        # "Start fresh" should also forget where the last sync left off, so the
        # next sync re-pages the whole Bandcamp collection rather than stopping
        # at bandcampsync's saved checkpoint. ignores.txt is deliberately left
        # alone — clearing it would re-download audio, which nuke is not about.
        state_cleared = _clear_bandcampsync_checkpoint(cfg.paths.music_dir)
        suffix = " · sync checkpoint reset" if state_cleared else ""
        activity.record(
            f"Erased {removed} sidecar(s) — albums revert to tag-derived state{suffix}", "warning"
        )
        return _flash_response(
            "Sidecars erased",
            f"{removed} removed — audio untouched; albums re-derive on next scan{suffix}",
            level="warning",
        )

    @app.post("/settings", response_class=HTMLResponse)
    def settings_save(
        request: Request,
        download_format: str = Form(...),
        max_downloads_per_sync: int = Form(...),
        user_agent: str = Form(...),
        cover_art_size: str = Form(...),
        log_level: str = Form(...),
    ) -> Response:
        cfg: config_mod.Config = request.app.state.cfg
        # Re-validate by constructing fresh sub-models (model_copy does NOT
        # validate). Bad values (e.g. an invalid cover-art size) raise here.
        try:
            new_bandcamp = config_mod.BandcampConfig(
                download_format=download_format.strip(),
                max_downloads_per_sync=max_downloads_per_sync,
                ignores_file=cfg.bandcamp.ignores_file,
                cookies_file=cfg.bandcamp.cookies_file,
            )
            new_mb = config_mod.MusicBrainzConfig(user_agent=user_agent.strip())
            # model_validate (vs the constructor) keeps mypy happy about the
            # str→Literal narrowing while still validating the value at runtime.
            new_cover = config_mod.CoverArtConfig.model_validate({"size": cover_art_size})
            new_cfg = cfg.model_copy(
                update={
                    "bandcamp": new_bandcamp,
                    "musicbrainz": new_mb,
                    "cover_art": new_cover,
                    "log_level": log_level.strip().lower(),
                }
            )
        except (PydanticValidationError, ValueError) as e:
            ctx = _ctx(
                request,
                bandcamp_ok=_bandcamp_configured(cfg),
                sidecar_count=sidecar_mod.count_all(cfg.paths.music_dir),
                error=str(e),
            )
            return _templates(request).TemplateResponse(request, "settings.html", ctx)

        config_mod.write_settings(
            cfg.paths.config_dir,
            {
                "bandcamp.download_format": new_bandcamp.download_format,
                "bandcamp.max_downloads_per_sync": new_bandcamp.max_downloads_per_sync,
                "musicbrainz.user_agent": new_mb.user_agent,
                "cover_art.size": new_cover.size,
                "log_level": new_cfg.log_level,
            },
        )
        # Apply live — code reads these from app.state.cfg at use-time. The
        # MB user-agent is applied at startup, so re-configure it now too.
        request.app.state.cfg = new_cfg
        mb_lookup.configure(new_cfg.musicbrainz.user_agent)
        activity.record("Settings updated", "info")

        ctx = _ctx(
            request,
            bandcamp_ok=_bandcamp_configured(new_cfg),
            sidecar_count=sidecar_mod.count_all(new_cfg.paths.music_dir),
            saved=True,
        )
        return _templates(request).TemplateResponse(request, "settings.html", ctx)

    @app.get("/sync/status")
    def sync_status(request: Request) -> Response:
        return JSONResponse(request.app.state.sync_runner.status())

    @app.get("/reconcile/status")
    def reconcile_status(request: Request) -> Response:
        return JSONResponse(request.app.state.reconcile_runner.status())

    @app.get("/scan/status")
    def scan_status(request: Request) -> Response:
        return JSONResponse(request.app.state.scan_runner.status())

    @app.get("/status")
    def app_status(request: Request) -> Response:
        """Consolidated status — one poll instead of three. The status bar
        polls only this; the individual endpoints above remain for tests/curl."""
        state = request.app.state
        return JSONResponse(
            {
                "sync": state.sync_runner.status(),
                "reconcile": state.reconcile_runner.status(),
                "scan": state.scan_runner.status(),
            }
        )

    @app.post("/reconcile", response_class=HTMLResponse)
    def reconcile_start(request: Request) -> Response:
        """Manual trigger — same handler the inbox auto-kicks. Useful when
        the user wants to force a re-run after dropping files in."""
        started = request.app.state.reconcile_runner.start()
        if started:
            return _flash_response("Reconcile started", "watch the inbox", tasks_changed=False)
        return _flash_response(
            "Reconcile busy",
            "already running or just finished",
            level="warning",
            tasks_changed=False,
        )

    @app.post("/sync", response_class=HTMLResponse)
    def start_sync(request: Request) -> Response:
        # Backstop the UI gating: don't kick a sync while a reconcile pass is
        # in flight (it's mutating sidecars / the inbox). The button is
        # disabled client-side, but a stale page or the race window before the
        # next /status poll could still POST here.
        if request.app.state.reconcile_runner.is_running:
            return _flash_response(
                "Sync unavailable",
                "reconciling — try again in a moment",
                level="warning",
                tasks_changed=False,
                status_code=status.HTTP_409_CONFLICT,
            )
        try:
            request.app.state.sync_runner.start()
        except AlreadyRunningError:
            return _flash_response(
                "Sync busy",
                "already running",
                level="warning",
                tasks_changed=False,
                status_code=status.HTTP_409_CONFLICT,
            )
        return _flash_response("Sync started", "watch the inbox", tasks_changed=False)

    @app.get("/bandcamp/setup", response_class=HTMLResponse)
    def bandcamp_setup(request: Request) -> Response:
        """Return the cookie-setup modal fragment."""
        return _templates(request).TemplateResponse(
            request,
            "partials/bandcamp_setup_modal.html",
            {"request": request},
        )

    @app.post("/bandcamp/cookies", response_class=HTMLResponse)
    async def bandcamp_cookies(
        request: Request,
        cookies_text: str = Form(""),
        cookies_file: UploadFile | None = File(None),
    ) -> Response:
        """Persist a pasted/uploaded cookies.txt, then reload so the header
        flips from 'Set up Bandcamp sync' to 'Sync Bandcamp'.
        """
        content = ""
        if cookies_file is not None and cookies_file.filename:
            content = (await cookies_file.read()).decode("utf-8", errors="replace")
        elif cookies_text.strip():
            content = cookies_text
        if not content.strip():
            # Re-render the modal with an error rather than refreshing.
            return _templates(request).TemplateResponse(
                request,
                "partials/bandcamp_setup_modal.html",
                {"request": request, "error": "Paste your cookies.txt contents or choose a file."},
            )
        cfg: config_mod.Config = request.app.state.cfg
        cfg.cookies_file.parent.mkdir(parents=True, exist_ok=True)
        cfg.cookies_file.write_text(content, encoding="utf-8")
        # Full reload: the header re-renders with the Sync button enabled.
        return HTMLResponse("", headers={"HX-Refresh": "true"})

    @app.get("/library", response_class=HTMLResponse)
    def library(request: Request, offset: int = 0, limit: int = 30) -> Response:
        """Paginated list of terminal albums (Complete + Incomplete),
        sorted by tagged_at desc."""
        from datetime import datetime as _dt

        albums = _albums(request)
        done = [a for a in albums if a.state in _TERMINAL_STATES]
        # Newest tagged first; albums missing tagged_at sink to the bottom.
        _floor = _dt.min.replace(tzinfo=UTC)
        done.sort(
            key=lambda a: a.sidecar.tagged_at if a.sidecar and a.sidecar.tagged_at else _floor,
            reverse=True,
        )
        limit = max(1, min(limit, 200))  # clamp; defensive
        offset = max(0, offset)
        page = done[offset : offset + limit]
        has_more = offset + limit < len(done)
        next_offset = offset + limit if has_more else None
        ctx = _ctx(
            request,
            rows=page,
            has_more=has_more,
            next_offset=next_offset,
            limit=limit,
            total_done=len(done),
            is_first_page=(offset == 0),
        )
        return _templates(request).TemplateResponse(request, "partials/library_page.html", ctx)

    @app.get("/library/{album_id}/compare", response_class=HTMLResponse)
    def library_compare(request: Request, album_id: str) -> Response:
        """On-demand disk-vs-MB track comparison for a tagged album — a sanity
        check that the right release was applied. Computed live (a fresh MB
        fetch + assess_match); never persisted."""
        album = _find_album(request, album_id)
        sc = album.sidecar
        if sc is None or not sc.mb_release_id:
            return HTMLResponse(
                '<p class="text-2xs text-muted italic mt-2">'
                "No MusicBrainz release to compare against.</p>"
            )
        try:
            release = mb_lookup.fetch_release(sc.mb_release_id)
        except mb_lookup.MBError as e:
            return HTMLResponse(
                f'<p class="text-2xs text-red-700 mt-2">Couldn\'t fetch from MusicBrainz: {e}</p>'
            )
        candidate = assess_match(album.path, release)
        ctx = _ctx(request, candidate=candidate)
        return _templates(request).TemplateResponse(request, "partials/library_compare.html", ctx)

    @app.post("/retag/{album_id}", response_class=HTMLResponse)
    def retag(request: Request, album_id: str) -> Response:
        album = _find_album(request, album_id)
        sc = album.sidecar
        if not sc or not sc.mb_release_id:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, "no mb_release_id on sidecar to re-tag from"
            )
        try:
            _tag_with_release(
                album.path,
                sc.mb_release_id,
                request.app.state.cfg,
                request.app.state.tagger,
            )
        except Exception as e:
            log.exception("retag failed")
            return _flash_response("Re-tag failed", str(e), level="error", tasks_changed=False)
        return _flash_response("Re-tagged", album.title)

    @app.post("/forget/{album_id}", response_class=HTMLResponse)
    def forget(request: Request, album_id: str) -> Response:
        """Delete the sidecar — album reverts to NEW. Files are not touched.

        Adds the album's path to the in-memory forgotten_paths set so the
        auto-reconciliation runner won't immediately undo this. The user's
        Forget intent is respected until they explicitly Reconcile, or
        until the server restarts.
        """
        album = _find_album(request, album_id)
        sc_path = sidecar_mod.sidecar_path(album.path)
        if sc_path.exists():
            sc_path.unlink()
        request.app.state.forgotten_paths.add(album.path)
        return _flash_response("Forgotten", f"{album.title} reverted to NEW")

    @app.get("/healthz")
    def healthz(request: Request) -> Response:
        cfg: config_mod.Config = request.app.state.cfg
        music = cfg.paths.music_dir
        return JSONResponse(
            {
                "status": "ok",
                "music_dir": str(music),
                "music_dir_exists": music.exists(),
                "music_dir_writable": _is_writable(music),
                "config_dir": str(cfg.paths.config_dir),
                "sync_state": request.app.state.sync_runner.status()["state"],
            }
        )

    @app.get("/cover/{album_id}")
    def cover(request: Request, album_id: str) -> Response:
        # Sync route → FastAPI runs it in its threadpool, so the (blocking)
        # cover read is already off the event loop.
        album = _find_album(request, album_id)
        if album.cover_path and album.cover_path.exists():
            media_type = "image/png" if album.cover_path.suffix.lower() == ".png" else "image/jpeg"
            return FileResponse(album.cover_path, media_type=media_type)
        # No folder cover — serve the art embedded in the tracks directly,
        # extracted on the fly (no need to write a cover.* to disk).
        embedded = _embedded_cover(album.path)
        if embedded is not None:
            data, media_type = embedded
            return Response(content=data, media_type=media_type)
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no cover")

    @app.post("/reconcile/{album_id}", response_class=HTMLResponse)
    def reconcile_album_route(request: Request, album_id: str) -> Response:
        """Per-album reconcile trigger. Idempotent — safe to click even if
        the album has already been reconciled by the background runner.

        Also clears any prior Forget exemption: explicit user intent wins.
        """
        album = _find_album(request, album_id)
        request.app.state.forgotten_paths.discard(album.path)
        try:
            sc = reconcile.reconcile_album(album.path, fetch_urls=mb_lookup.fetch_release_urls)
        except Exception as e:
            log.exception("reconcile failed")
            return _flash_response(
                "Reconcile failed",
                str(e),
                level="error",
                tasks_changed=False,
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
        if sc is None:
            # reconcile_album returns None for two reasons: existing sidecar
            # (already reconciled, often by the auto-runner) or no MBID atom.
            if sidecar_mod.has_sidecar(album.path):
                return _flash_response("Already reconciled", album.title)
            return _flash_response(
                "No MBID atom",
                f"{album.title} has no MusicBrainz Album Id",
                level="warning",
                tasks_changed=False,
            )
        label = "Bandcamp source" if sc.store_url else "manual source"
        return _flash_response("Reconciled", f"{album.title} ({label})")

    @app.post("/recheck/{album_id}", response_class=HTMLResponse)
    def recheck(request: Request, album_id: str) -> Response:
        album = _find_album(request, album_id)
        sc = album.sidecar
        if sc is None or not sc.store_url:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "no store URL on sidecar")
        try:
            mbids = mb_lookup.lookup_by_bandcamp_url(sc.store_url)
        except mb_lookup.MBError as e:
            return _flash_response("MB lookup failed", str(e), level="error", tasks_changed=False)
        if not mbids:
            return _flash_response(
                "Still no match",
                f"{album.title}: no MusicBrainz release for this URL yet",
                level="warning",
                tasks_changed=False,
            )

        # A URL can map to several MB releases (e.g. a long digital edition plus
        # a shorter CD mix). Don't guess which one — surface them all and let the
        # user pick (into the card's shared, preserved results box).
        if len(mbids) > 1:
            try:
                results, total = mb_lookup.candidate_summaries_for_url(sc.store_url)
            except mb_lookup.MBError as e:
                return _flash_response(
                    "MB lookup failed", str(e), level="error", tasks_changed=False
                )
            return _render_release_picker(
                request,
                album,
                results,
                total,
                heading="Several releases share this store URL — pick the right one",
                retarget=True,
            )

        try:
            releases = [mb_lookup.fetch_release(m) for m in mbids]
        except mb_lookup.MBError as e:
            return _flash_response("MB fetch failed", str(e), level="error", tasks_changed=False)
        candidate = best_match(album.path, releases)
        assert candidate is not None  # releases is non-empty (mbids guarded)
        mbid = candidate.mb_release_id

        new_sc = Sidecar(
            schema_version=sc.schema_version,
            store_url=sc.store_url,
            bandcamp=sc.bandcamp,
            downloaded_at=sc.downloaded_at,
            added_at=sc.added_at,
            mb_release_id=mbid if candidate.confidence == "exact" else None,
            mb_match_candidate=None if candidate.confidence == "exact" else candidate,
            tagged_at=sc.tagged_at,
            notes=sc.notes,
        )
        sidecar_mod.write(album.path, new_sc)

        if candidate.confidence == "exact":
            try:
                _tag_with_release(album.path, mbid, request.app.state.cfg, request.app.state.tagger)
                return _flash_response("Tagged", f"{album.title} (match found via Recheck)")
            except Exception as e:
                log.exception("tag after recheck failed")
                return _flash_response(
                    "Tagging failed",
                    str(e),
                    level="error",
                    tasks_changed=False,
                )
        return _flash_response(
            "Needs review",
            f"{album.title}: {candidate.confidence} match — please review and confirm",
        )

    @app.post("/confirm/{album_id}", response_class=HTMLResponse)
    def confirm_match(request: Request, album_id: str) -> Response:
        album = _find_album(request, album_id)
        sc = album.sidecar
        if sc is None or sc.mb_match_candidate is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "no candidate to confirm")
        try:
            _tag_with_release(
                album.path,
                sc.mb_match_candidate.mb_release_id,
                request.app.state.cfg,
                request.app.state.tagger,
                # Mis-tag confirm: adopt the owned edition's purchase URL so the
                # album can link to that purchase on the next sync.
                store_url_override=sc.mb_match_candidate.mistag_owned_url,
            )
        except Exception as e:
            log.exception("tag failed")
            return _flash_response("Tagging failed", str(e), level="error", tasks_changed=False)
        return _flash_response("Tagged", album.title)

    @app.post("/confirm/{album_id}/incomplete", response_class=HTMLResponse)
    def confirm_match_incomplete(request: Request, album_id: str) -> Response:
        """Confirm-as-Incomplete: tag the album knowing on-disk file count
        is less than the MB release's track count. Persists the expected
        track count on the sidecar so the scanner can derive INCOMPLETE
        (and auto-promote to COMPLETE if the user adds files later).
        """
        album = _find_album(request, album_id)
        sc = album.sidecar
        if sc is None or sc.mb_match_candidate is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "no candidate to confirm")
        try:
            _tag_with_release(
                album.path,
                sc.mb_match_candidate.mb_release_id,
                request.app.state.cfg,
                request.app.state.tagger,
                incomplete=True,
                store_url_override=sc.mb_match_candidate.mistag_owned_url,
            )
        except Exception as e:
            log.exception("incomplete tag failed")
            return _flash_response("Tagging failed", str(e), level="error", tasks_changed=False)
        return _flash_response("Tagged as incomplete", album.title)

    @app.post("/reject/{album_id}", response_class=HTMLResponse)
    def reject_match(request: Request, album_id: str) -> Response:
        album = _find_album(request, album_id)
        sc = album.sidecar
        if sc is None or sc.mb_match_candidate is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "no candidate to reject")
        new_sc = Sidecar(
            schema_version=sc.schema_version,
            store_url=sc.store_url,
            bandcamp=sc.bandcamp,
            downloaded_at=sc.downloaded_at,
            added_at=sc.added_at,
            mb_release_id=None,
            mb_match_candidate=None,
            tagged_at=sc.tagged_at,
            notes=sc.notes,
        )
        sidecar_mod.write(album.path, new_sc)
        return _flash_response("Match rejected", album.title)

    @app.post("/unconfirmed/{album_id}/url", response_class=HTMLResponse)
    def update_unconfirmed_url(request: Request, album_id: str, url: str = Form(...)) -> Response:
        album = _find_album(request, album_id)
        sc = album.sidecar
        if sc is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "no sidecar")
        # Update the store URL; clear bandcamp.item_id since the URL changed
        # and the old item_id (if any) is no longer authoritative.
        new_bandcamp = None
        if sc.bandcamp and sc.bandcamp.band_id is not None:
            from harmonist.models import BandcampInfo

            new_bandcamp = BandcampInfo(item_id=None, band_id=sc.bandcamp.band_id)
        new_sc = _replace_url(sc, url.strip(), new_bandcamp)
        sidecar_mod.write(album.path, new_sc)
        return _flash_response("URL updated", f"{album.title} — run Sync to confirm")

    @app.post("/manual/{album_id}/search", response_class=HTMLResponse)
    def manual_search(
        request: Request,
        album_id: str,
        artist: str = Form(""),
        title: str = Form(""),
    ) -> Response:
        # Validate album exists; a 404 is the right signal for a stale UI.
        album = _find_album(request, album_id)
        try:
            # Cap to a handful — beyond this, MB's own search is the better
            # tool. Each row links out to the release for closer inspection.
            results = mb_search.search_releases(artist, title, limit=5)
        except mb_search.MBSearchError as e:
            return _flash_response("MB search failed", str(e), level="error", tasks_changed=False)
        return _render_release_picker(
            request, album, results, len(results), heading="MusicBrainz search results"
        )

    @app.post("/manual/{album_id}/candidates", response_class=HTMLResponse)
    def manual_candidates(request: Request, album_id: str) -> Response:
        """List the MB releases linked to this album's store URL so the user can
        pick the right one. Fresh lookup each call — no caching — so a fix made
        on MusicBrainz shows up immediately."""
        album = _find_album(request, album_id)
        sc = album.sidecar
        if sc is None or not sc.store_url:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "no store URL on sidecar")
        try:
            results, total = mb_lookup.candidate_summaries_for_url(sc.store_url)
        except mb_lookup.MBError as e:
            return _flash_response("MB lookup failed", str(e), level="error", tasks_changed=False)
        return _render_release_picker(
            request, album, results, total, heading="Releases linked to this store URL"
        )

    @app.post("/manual/{album_id}/assign", response_class=HTMLResponse)
    def manual_assign(request: Request, album_id: str, mbid: str = Form(...)) -> Response:
        album = _find_album(request, album_id)
        extracted = _extract_mbid(mbid)
        if not extracted:
            return _flash_response(
                "Could not parse",
                "Paste a full MB release URL or the 36-char MBID",
                level="error",
                tasks_changed=False,
            )
        try:
            status_str, msg = _apply_best_match(
                album.path, [extracted], request.app.state.cfg, request.app.state.tagger
            )
        except mb_lookup.MBError as e:
            return _flash_response("MB lookup failed", str(e), level="error", tasks_changed=False)
        except Exception as e:
            log.exception("manual assign failed")
            return _flash_response("Assignment failed", str(e), level="error", tasks_changed=False)
        # status_str is 'tagged' or 'needs_confirmation' — use the friendlier
        # verb from msg's first clause.
        verb = "Tagged" if status_str == "tagged" else "Needs review"
        return _flash_response(verb, f"{album.title}: {msg}")

    @app.post("/unconfirmed/{album_id}/manual", response_class=HTMLResponse)
    def mark_unconfirmed_manual(request: Request, album_id: str) -> Response:
        """Drop the store URL + Bandcamp block. Album becomes "manually
        sourced" (store_url is None, store_name() returns None)."""
        album = _find_album(request, album_id)
        sc = album.sidecar
        if sc is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "no sidecar")
        new_sc = Sidecar(
            schema_version=sc.schema_version,
            store_url=None,
            bandcamp=None,
            downloaded_at=sc.downloaded_at,
            added_at=sc.added_at,
            mb_release_id=sc.mb_release_id,
            mb_match_candidate=sc.mb_match_candidate,
            tagged_at=sc.tagged_at,
            notes="marked as purchased elsewhere",
        )
        sidecar_mod.write(album.path, new_sc)
        return _flash_response("Marked manual", f"{album.title} (purchased elsewhere)")


def _replace_url(sc: Sidecar, new_url: str, new_bandcamp: BandcampInfo | None) -> Sidecar:
    """Build a new Sidecar with store_url and bandcamp block replaced."""
    return Sidecar(
        schema_version=sc.schema_version,
        store_url=new_url,
        bandcamp=new_bandcamp,
        downloaded_at=sc.downloaded_at,
        added_at=sc.added_at,
        mb_release_id=sc.mb_release_id,
        mb_match_candidate=sc.mb_match_candidate,
        tagged_at=sc.tagged_at,
        notes=sc.notes,
    )


def _is_writable(path: Path) -> bool:
    try:
        return path.exists() and (path.is_dir() or path.parent.is_dir())
    except OSError:
        return False


def _flash(message: str, *, level: str) -> str:
    """Render a small flash message fragment for HTMX swap-or-replace."""
    classes = {
        "info": "bg-bc-teal/10 text-bc-teal border-bc-teal/30",
        "warning": "bg-amber-500/10 text-amber-300 border-amber-500/30",
        "error": "bg-red-500/10 text-red-300 border-red-500/30",
    }.get(level, "bg-slate-700/30 text-slate-200 border-slate-600")
    return f'<div class="px-4 py-2 border rounded {classes} text-sm font-bold">{message}</div>'


def _flash_response(
    verb: str,
    details: str | None = None,
    *,
    level: str = "info",
    tasks_changed: bool = True,
    status_code: int = 200,
) -> HTMLResponse:
    """Standard action response: flash HTML body + HX-Trigger events.

    The status bar renders the message as a level-coloured pill around
    `verb` followed by `details` in plain text. Splitting the two keeps
    the status bar visually light when the details run long.

    Emits:
      - `harmonist-status` — picked up by the status-bar JS in index.html.
      - `tasks-changed` (when `tasks_changed=True`) — inbox + library refresh.

    Use for every endpoint that mutates album state. For pure-display
    failures (e.g. MB lookup error with no state change), pass
    `tasks_changed=False` to avoid spurious refreshes.
    """
    message = verb if not details else f"{verb} — {details}"
    # Every action outcome is also an activity-log entry (the Activity tab).
    activity.record(message, level if level in ("info", "warning", "error") else "info")
    triggers: dict[str, Any] = {
        "harmonist-status": {
            "verb": verb,
            "details": details,
            "level": level,
        }
    }
    if tasks_changed:
        triggers["tasks-changed"] = True
    return HTMLResponse(
        _flash(message, level=level),
        status_code=status_code,
        headers={"HX-Trigger": json.dumps(triggers)},
    )


def _render_release_picker(
    request: Request,
    album: Album,
    results: list[dict[str, Any]],
    total: int,
    *,
    heading: str | None,
    retarget: bool = False,
) -> Response:
    """Render the shared candidate-release list (store-URL picker or name
    search). `retarget` rewrites the swap to the card's preserved results box —
    needed when the trigger (e.g. the Recheck button) posts with hx-swap=none.
    """
    headers: dict[str, str] = {}
    if retarget:
        headers["HX-Retarget"] = f"#mbid-results-{album.id}"
        headers["HX-Reswap"] = "innerHTML"
    return _templates(request).TemplateResponse(
        request,
        "partials/manual_search_results.html",
        {
            "request": request,
            "results": results,
            "album_id": album.id,
            "heading": heading,
            "more_count": total,
            # Local facts so the rows can flag obvious mismatches inline.
            "local_track_count": album.track_count,
            "local_artist": album.artist,
        },
        headers=headers,
    )


# The ASGI app is created lazily on attribute access (PEP 562) rather than at
# import. Merely importing this module — which the test suite does — must NOT
# run create_app() with ambient config: in demo mode that would monkeypatch the
# global MB/Bandcamp services at import time and leak into unrelated tests.
# `uvicorn harmonist.web.main:app` triggers creation on first access; the
# `--factory` form (`...:create_app --factory`) works too.
#
# Memoized: uvicorn accesses `.app` more than once during startup, and an
# unmemoized factory would build (and run startup for) a second app — doubling
# every startup log and, once scanning moves to a startup task, kicking two
# scans. Tests never touch `.app` (they call create_app() directly), so the
# import-time-safety note above still holds.
_app_singleton: FastAPI | None = None


def __getattr__(name: str) -> Any:
    if name == "app":
        global _app_singleton
        if _app_singleton is None:
            _app_singleton = create_app()
        return _app_singleton
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
