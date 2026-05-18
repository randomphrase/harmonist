"""FastAPI application for Harmonist."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Form, HTTPException, Request, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from harmonist import config as config_mod
from harmonist import cover_art, mb_lookup, mb_search, reconcile, scanner, url_recovery
from harmonist import sidecar as sidecar_mod
from harmonist.bandcamp_hook import CapExceededError, HarmonistSyncer
from harmonist.match import assess_match
from harmonist.models import Album, AlbumState, BandcampInfo, Sidecar, store_name
from harmonist.sidecar import CURRENT_SCHEMA_VERSION
from harmonist.tagger import PicardCompatibleTagger, Tagger
from harmonist.web.reconcile_runner import ReconcileRunner, reconcile_pending_orphans
from harmonist.web.sync_runner import AlreadyRunningError, SyncRunner

# Demo mode is conditionally imported in create_app() — keeps demo-only code
# out of the production import path entirely.

import re

_MB_URL_RE = re.compile(r"/release/([a-f0-9-]{36})", re.IGNORECASE)
_MBID_RE = re.compile(r"^[a-f0-9-]{36}$", re.IGNORECASE)


def _extract_mbid(value: str) -> Optional[str]:
    """Pull an MBID out of a raw input — accepts either a full MB URL or the bare MBID."""
    s = (value or "").strip()
    if not s:
        return None
    if m := _MB_URL_RE.search(s):
        return m.group(1).lower()
    if _MBID_RE.fullmatch(s):
        return s.lower()
    return None


log = logging.getLogger(__name__)


HARMONY_BASE = "https://harmony.pulsewidth.org.uk"


def create_app(
    cfg: Optional[config_mod.Config] = None,
    *,
    tagger: Optional[Tagger] = None,
) -> FastAPI:
    """Application factory. Tests can pass a pre-built config and/or a swap-in
    tagger implementation.
    """
    if cfg is None:
        cfg = config_mod.load()
    if tagger is None:
        tagger = PicardCompatibleTagger()

    mb_lookup.configure(cfg.musicbrainz.user_agent)

    sync_runner = SyncRunner(runner_fn=lambda: None)  # placeholder, replaced below

    if cfg.demo_mode:
        from harmonist import demo
        demo.install()
        demo.ensure_seeded(cfg.paths.music_dir)
        runner_fn = lambda: demo.run_demo_sync(
            cfg.paths.music_dir, progress_callback=sync_runner.set_current_item
        )
    else:
        runner_fn = lambda: _run_bandcamp_sync(cfg, progress_callback=sync_runner.set_current_item)
    sync_runner._runner_fn = runner_fn

    # Paths the user has explicitly Forgot. Exempted from auto-reconcile so
    # the runner doesn't immediately undo the user's intent. In-memory only:
    # restart clears the set (acceptable tradeoff per user feedback).
    forgotten_paths: set[Path] = set()

    reconcile_runner = ReconcileRunner(
        runner_fn=lambda status_updater: reconcile_pending_orphans(
            cfg.paths.music_dir,
            fetch_urls=mb_lookup.fetch_release_urls,
            status_updater=status_updater,
            exempt_paths=forgotten_paths,
        ),
    )

    project_root = Path(__file__).resolve().parent.parent.parent.parent
    templates_dir = project_root / "templates"
    static_dir = project_root / "static"
    templates = Jinja2Templates(directory=str(templates_dir))
    templates.env.globals["harmony_base"] = HARMONY_BASE
    templates.env.globals["AlbumState"] = AlbumState
    templates.env.globals["store_name"] = store_name
    templates.env.globals["demo_mode"] = cfg.demo_mode

    app = FastAPI(title="Harmonist")
    app.state.cfg = cfg
    app.state.templates = templates
    app.state.sync_runner = sync_runner
    app.state.reconcile_runner = reconcile_runner
    app.state.forgotten_paths = forgotten_paths
    app.state.tagger = tagger

    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    _register_routes(app)
    if cfg.demo_mode:
        _register_demo_routes(app)
    return app


def _register_demo_routes(app: FastAPI) -> None:
    from harmonist import demo

    @app.post("/demo/reset", response_class=HTMLResponse)
    def demo_reset(request: Request):
        try:
            demo.reset(request.app.state.cfg.paths.music_dir)
        except RuntimeError as e:
            return HTMLResponse(_flash(str(e), level="error"), status_code=400)
        return HTMLResponse(
            _flash("Demo data reset to original state.", level="info"),
            headers={"HX-Trigger": "tasks-changed"},
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ctx(request: Request, **extra) -> dict:
    base = {"request": request, "cfg": request.app.state.cfg, "now": datetime.now(timezone.utc)}
    base.update(extra)
    return base


def _albums(request: Request) -> list[Album]:
    cfg: config_mod.Config = request.app.state.cfg
    return scanner.scan(cfg.paths.music_dir)


def _find_album(request: Request, album_id: str) -> Album:
    for a in _albums(request):
        if a.id == album_id:
            return a
    raise HTTPException(status.HTTP_404_NOT_FOUND, f"album {album_id} not found")


def _inbox_albums(albums: list[Album]) -> list[Album]:
    """Albums that warrant attention in the inbox (everything except DONE)."""
    return [a for a in albums if a.state != AlbumState.DONE]


def _run_bandcamp_sync(cfg: config_mod.Config, *, progress_callback=None):
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
    )


def _apply_match(album: Album, mbid: str, cfg: config_mod.Config, tagger: Tagger) -> tuple[str, str]:
    """Fetch MB release, run match assessment, then either tag or stash candidate.

    Returns (status, message) where status is 'tagged' | 'needs_confirmation'.
    """
    release = mb_lookup.fetch_release(mbid)
    candidate = assess_match(album.path, release)

    if candidate.confidence == "exact":
        _tag_with_release(album, mbid, cfg, tagger)
        return "tagged", "Match exact — files tagged."

    existing = sidecar_mod.read(album.path)
    new = Sidecar(
        schema_version=CURRENT_SCHEMA_VERSION,
        store_url=existing.store_url if existing else None,
        bandcamp=existing.bandcamp if existing else None,
        downloaded_at=existing.downloaded_at if existing else None,
        added_at=(existing.added_at if existing else None) or datetime.now(timezone.utc),
        mb_release_id=None,
        mb_match_candidate=candidate,
        tagged_at=existing.tagged_at if existing else None,
        notes=existing.notes if existing else None,
    )
    sidecar_mod.write(album.path, new)
    return "needs_confirmation", f"Match found ({candidate.confidence}) — please review and confirm."


def _tag_with_release(album: Album, mbid: str, cfg: config_mod.Config, tagger: Tagger) -> None:
    """Fetch MB release, fetch cover, write tags, update sidecar."""
    release = mb_lookup.fetch_release(mbid)
    rg = release.get("release-group") or {}
    cover_path = cover_art.ensure_cover(
        album.path,
        release_mbid=release["id"],
        release_group_mbid=rg.get("id"),
        size=cfg.cover_art.size,
    )
    tagger.tag_album(album.path, release, cover_path=cover_path)

    sc = sidecar_mod.read(album.path)
    new = Sidecar(
        schema_version=CURRENT_SCHEMA_VERSION,
        store_url=sc.store_url if sc else None,
        bandcamp=sc.bandcamp if sc else None,
        downloaded_at=sc.downloaded_at if sc else None,
        added_at=(sc.added_at if sc else None) or datetime.now(timezone.utc),
        mb_release_id=mbid,
        mb_match_candidate=None,  # cleared on tag
        tagged_at=datetime.now(timezone.utc),
        notes=sc.notes if sc else None,
    )
    sidecar_mod.write(album.path, new)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def _register_routes(app: FastAPI) -> None:

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        albums = _albums(request)
        ctx = _ctx(
            request,
            albums=_inbox_albums(albums),
            total_albums=len(albums),
            sync_status=request.app.state.sync_runner.status(),
        )
        return request.app.state.templates.TemplateResponse(request, "index.html", ctx)

    @app.get("/tasks", response_class=HTMLResponse)
    def tasks(request: Request):
        albums = _albums(request)
        # Auto-kick the reconciler if there are Orphans waiting and the
        # debounce window has elapsed. The runner internally handles the
        # "no MBID, skip" case — cost is cheap for non-reconcilable orphans.
        if any(a.state == AlbumState.NEW for a in albums):
            request.app.state.reconcile_runner.start()
        ctx = _ctx(request, albums=_inbox_albums(albums), total_albums=len(albums))
        return request.app.state.templates.TemplateResponse(request, "tasks.html", ctx)

    @app.get("/sync/status")
    def sync_status(request: Request):
        return JSONResponse(request.app.state.sync_runner.status())

    @app.get("/reconcile/status")
    def reconcile_status(request: Request):
        return JSONResponse(request.app.state.reconcile_runner.status())

    @app.post("/reconcile", response_class=HTMLResponse)
    def reconcile_start(request: Request):
        """Manual trigger — same handler the inbox auto-kicks. Useful when
        the user wants to force a re-run after dropping files in."""
        started = request.app.state.reconcile_runner.start()
        if started:
            return HTMLResponse(_flash("Reconcile started — watch the inbox.", level="info"))
        return HTMLResponse(_flash(
            "Reconcile is already running (or just finished).", level="warning"
        ))

    @app.post("/sync", response_class=HTMLResponse)
    def start_sync(request: Request):
        try:
            request.app.state.sync_runner.start()
        except AlreadyRunningError:
            return HTMLResponse(
                _flash("Sync is already running.", level="warning"),
                status_code=status.HTTP_409_CONFLICT,
            )
        return HTMLResponse(_flash("Sync started — watch the inbox.", level="info"))

    @app.get("/library", response_class=HTMLResponse)
    def library(request: Request, offset: int = 0, limit: int = 30):
        """Paginated list of Done albums, sorted by tagged_at desc."""
        from datetime import timezone as _tz, datetime as _dt
        albums = _albums(request)
        done = [a for a in albums if a.state == AlbumState.DONE]
        # Newest tagged first; albums missing tagged_at sink to the bottom.
        _floor = _dt.min.replace(tzinfo=_tz.utc)
        done.sort(
            key=lambda a: (a.sidecar.tagged_at if a.sidecar and a.sidecar.tagged_at else _floor),
            reverse=True,
        )
        limit = max(1, min(limit, 200))  # clamp; defensive
        offset = max(0, offset)
        page = done[offset:offset + limit]
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
        return request.app.state.templates.TemplateResponse(
            request, "partials/library_page.html", ctx
        )

    @app.post("/retag/{album_id}", response_class=HTMLResponse)
    def retag(request: Request, album_id: str):
        album = _find_album(request, album_id)
        sc = album.sidecar
        if not sc or not sc.mb_release_id:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, "no mb_release_id on sidecar to re-tag from"
            )
        try:
            _tag_with_release(
                album, sc.mb_release_id, request.app.state.cfg, request.app.state.tagger,
            )
        except Exception as e:
            log.exception("retag failed")
            return HTMLResponse(_flash(f"Re-tag failed: {e}", level="error"))
        return HTMLResponse(
            _flash("Re-tagged from MusicBrainz.", level="info"),
            headers={"HX-Trigger": "tasks-changed"},
        )

    @app.post("/forget/{album_id}", response_class=HTMLResponse)
    def forget(request: Request, album_id: str):
        """Delete the sidecar — album reverts to Orphan. Files are not touched.

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
        return HTMLResponse(
            _flash("Forgotten. Album is now an Orphan.", level="info"),
            headers={"HX-Trigger": "tasks-changed"},
        )

    @app.get("/healthz")
    def healthz(request: Request):
        cfg: config_mod.Config = request.app.state.cfg
        music = cfg.paths.music_dir
        config_ok = cfg.paths.config_dir.exists() or True  # config dir auto-created on first write
        return JSONResponse({
            "status": "ok",
            "music_dir": str(music),
            "music_dir_exists": music.exists(),
            "music_dir_writable": _is_writable(music),
            "config_dir": str(cfg.paths.config_dir),
            "sync_state": request.app.state.sync_runner.status()["state"],
        })

    @app.get("/cover/{album_id}")
    def cover(request: Request, album_id: str):
        album = _find_album(request, album_id)
        if not album.cover_path or not album.cover_path.exists():
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no cover")
        media_type = "image/png" if album.cover_path.suffix.lower() == ".png" else "image/jpeg"
        return FileResponse(album.cover_path, media_type=media_type)

    @app.post("/reconcile/{album_id}", response_class=HTMLResponse)
    def reconcile_album_route(request: Request, album_id: str):
        """Per-album reconcile trigger. Idempotent — safe to click even if
        the album has already been reconciled by the background runner.

        Also clears any prior Forget exemption: explicit user intent wins.
        """
        album = _find_album(request, album_id)
        request.app.state.forgotten_paths.discard(album.path)
        try:
            sc = reconcile.reconcile_album(
                album.path, fetch_urls=mb_lookup.fetch_release_urls
            )
        except Exception as e:
            log.exception("reconcile failed")
            return HTMLResponse(
                _flash(f"Reconcile failed: {e}", level="error"),
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
        if sc is None:
            # reconcile_album returns None for two reasons: existing sidecar
            # (already reconciled, often by the auto-runner) or no MBID atom.
            if sidecar_mod.has_sidecar(album.path):
                return HTMLResponse(_flash(
                    "Already reconciled — sidecar exists.", level="info",
                ), headers={"HX-Trigger": "tasks-changed"})
            return HTMLResponse(_flash(
                "Could not reconcile — no MusicBrainz Album Id atom on the files.",
                level="warning",
            ))
        label = "Bandcamp source" if sc.store_url else "manual source"
        return HTMLResponse(_flash(
            f"Reconciled ({label}).", level="info",
        ), headers={"HX-Trigger": "tasks-changed"})

    @app.post("/recheck/{album_id}", response_class=HTMLResponse)
    def recheck(request: Request, album_id: str):
        album = _find_album(request, album_id)
        sc = album.sidecar
        if sc is None or not sc.store_url:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "no store URL on sidecar")
        try:
            mbid = mb_lookup.lookup_by_bandcamp_url(sc.store_url)
        except mb_lookup.MBError as e:
            return HTMLResponse(_flash(f"MB lookup failed: {e}", level="error"))
        if not mbid:
            return HTMLResponse(_flash("Still no MusicBrainz match for this URL.", level="warning"))

        try:
            release = mb_lookup.fetch_release(mbid)
        except mb_lookup.MBError as e:
            return HTMLResponse(_flash(f"MB fetch failed: {e}", level="error"))
        candidate = assess_match(album.path, release)

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
                _tag_with_release(album, mbid, request.app.state.cfg, request.app.state.tagger)
                return HTMLResponse(_flash("Match found and tagged.", level="info"),
                                    headers={"HX-Trigger": "tasks-changed"})
            except Exception as e:
                log.exception("tag after recheck failed")
                return HTMLResponse(_flash(f"Match found but tagging failed: {e}", level="error"))
        return HTMLResponse(_flash(
            f"Match found ({candidate.confidence}) — please review and confirm.",
            level="info",
        ), headers={"HX-Trigger": "tasks-changed"})

    @app.post("/confirm/{album_id}", response_class=HTMLResponse)
    def confirm_match(request: Request, album_id: str):
        album = _find_album(request, album_id)
        sc = album.sidecar
        if sc is None or sc.mb_match_candidate is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "no candidate to confirm")
        try:
            _tag_with_release(album, sc.mb_match_candidate.mb_release_id, request.app.state.cfg, request.app.state.tagger)
        except Exception as e:
            log.exception("tag failed")
            return HTMLResponse(_flash(f"Tagging failed: {e}", level="error"))
        return HTMLResponse(_flash("Tagged.", level="info"),
                            headers={"HX-Trigger": "tasks-changed"})

    @app.post("/reject/{album_id}", response_class=HTMLResponse)
    def reject_match(request: Request, album_id: str):
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
        return HTMLResponse(_flash("Match rejected.", level="info"),
                            headers={"HX-Trigger": "tasks-changed"})

    @app.post("/unconfirmed/{album_id}/url", response_class=HTMLResponse)
    def update_unconfirmed_url(request: Request, album_id: str, url: str = Form(...)):
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
        return HTMLResponse(_flash("URL updated. Run sync to confirm.", level="info"),
                            headers={"HX-Trigger": "tasks-changed"})

    @app.post("/manual/{album_id}/search", response_class=HTMLResponse)
    def manual_search(
        request: Request,
        album_id: str,
        artist: str = Form(""),
        title: str = Form(""),
    ):
        # Validate album exists; we don't need it for the search itself but a 404
        # here is the right signal for a stale UI.
        _find_album(request, album_id)
        try:
            results = mb_search.search_releases(artist, title)
        except mb_search.MBSearchError as e:
            return HTMLResponse(_flash(f"MB search failed: {e}", level="error"))
        return request.app.state.templates.TemplateResponse(
            request,
            "partials/manual_search_results.html",
            {"request": request, "results": results, "album_id": album_id, "query": {"artist": artist, "title": title}},
        )

    @app.post("/manual/{album_id}/assign", response_class=HTMLResponse)
    def manual_assign(request: Request, album_id: str, mbid: str = Form(...)):
        album = _find_album(request, album_id)
        extracted = _extract_mbid(mbid)
        if not extracted:
            return HTMLResponse(_flash(
                "Could not parse an MBID from that input. Paste a full MB release URL or the 36-char MBID.",
                level="error",
            ))
        try:
            status_str, msg = _apply_match(album, extracted, request.app.state.cfg, request.app.state.tagger)
        except mb_lookup.MBError as e:
            return HTMLResponse(_flash(f"MB lookup failed: {e}", level="error"))
        except Exception as e:
            log.exception("manual assign failed")
            return HTMLResponse(_flash(f"Assignment failed: {e}", level="error"))
        return HTMLResponse(_flash(msg, level="info"),
                            headers={"HX-Trigger": "tasks-changed"})

    @app.post("/recover/{album_id}", response_class=HTMLResponse)
    def recover_url(request: Request, album_id: str):
        album = _find_album(request, album_id)
        if album.state != AlbumState.NEW:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "URL recovery only applies to new albums")
        try:
            url = url_recovery.recover_album_url(album.path)
        except Exception as e:
            log.exception("URL recovery failed")
            return HTMLResponse(_flash(f"URL recovery failed: {e}", level="error"))
        if not url:
            return HTMLResponse(_flash(
                "Could not recover a store URL — no usable link found in the file's comment tag.",
                level="warning",
            ))
        # Write a partial sidecar with just the URL; user can run Recheck.
        sidecar_mod.write(
            album.path,
            Sidecar(
                schema_version=CURRENT_SCHEMA_VERSION,
                store_url=url,
                added_at=datetime.now(timezone.utc),
            ),
        )
        return HTMLResponse(_flash(f"Recovered URL: {url}. Click Recheck to look it up on MusicBrainz.", level="info"),
                            headers={"HX-Trigger": "tasks-changed"})

    @app.post("/unconfirmed/{album_id}/manual", response_class=HTMLResponse)
    def mark_unconfirmed_manual(request: Request, album_id: str):
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
        return HTMLResponse(_flash("Marked as purchased elsewhere.", level="info"),
                            headers={"HX-Trigger": "tasks-changed"})


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


# Lazy app — most users go through `uvicorn harmonist.web.main:app --factory`
# but for compatibility, also support `uvicorn harmonist.web.main:app --reload`.
app = create_app()
