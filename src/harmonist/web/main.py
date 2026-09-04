"""FastAPI application for Harmonist."""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os

# Demo mode is conditionally imported in create_app() — keeps demo-only code
# out of the production import path entirely.
import re
import sys
import threading
import unicodedata
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager, suppress
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError as PydanticValidationError
from starlette.middleware.trustedhost import TrustedHostMiddleware

from harmonist import (
    activity,
    activity_store,
    album_files,
    archive,
    artwork_store,
    audit,
    compare,
    cover_art,
    formats,
    gardener,
    id_registry,
    library_index,
    live_counts,
    match,
    mb_cache,
    mb_lookup,
    mb_search,
    pending_downloads,
    reconcile,
    redownloads,
    scanner,
    tag_history,
    timing,
)
from harmonist import config as config_mod
from harmonist import sidecar as sidecar_mod
from harmonist import tagger as tagger_mod
from harmonist.activity_store import Level
from harmonist.bandcamp_hook import HarmonistSyncer, album_slug
from harmonist.formats import owned
from harmonist.match import best_match
from harmonist.models import (
    Album,
    AlbumState,
    BandcampInfo,
    MatchCandidate,
    Release,
    Sidecar,
    store_name,
    title_with_disambiguation,
    title_words,
    titles_match,
)
from harmonist.tagger import PicardCompatibleTagger, Tagger, tagsets_for
from harmonist.web import dir_watcher, periodic
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

# For a `log.exception` that sits beside a `_flash_response(..., album=...)`: the
# traceback belongs in the SERVER log, not in the feed. `_flash_response` has
# already recorded the failure as a user-facing entry, attributed to the album
# and carrying the reason — so without this flag the feed's WARNING+ mirror
# (activity._ActivityLogHandler) adds a second ERROR row saying "tag failed" and
# nothing else, under no album at all. One failure, one entry (#258).
#
# Only for the paired sites. A `log.exception` with no flash beside it — the
# background passes, the ignores-file writes — is the ONLY notice the user gets
# and must go on mirroring.
_LOG_ONLY = {"_activity": True}


HARMONY_BASE = "https://harmony.pulsewidth.org.uk"

# Most audit rows to render under one activity entry's "what changed". One action
# can produce hundreds — the first scan of a large library records every album it
# meets — and the feed re-polls every 2s, so rendering them all put ~150 KB on the
# wire each time. The count in the summary is always the true total; the album's
# own page carries its full history (#118).
AUDIT_DETAIL_LIMIT = 20

# Terminal states — hidden from the inbox, shown in the library.
_TERMINAL_STATES = {AlbumState.COMPLETE, AlbumState.INCOMPLETE}

# Albums per page of the Library grid, and the sizes the pager's control offers
# (#144). Each offered size divides evenly by 4 and 5 — the lg/xl column counts
# of the grid — so a full page never ends in a ragged part-row.
#
# This used to be one fixed number, on the grounds that a size changing underneath
# the reader would make the same `?page=N` link resolve to different albums. That
# holds only while the size is invisible to the URL: `?page=2&limit=40` names one
# set of albums for good. So the size is addressable state alongside the page, and
# the cookie below is only ever consulted for a URL that omits it.
_LIBRARY_PAGE_SIZES = (20, 40, 60)
_LIBRARY_PAGE_SIZE = _LIBRARY_PAGE_SIZES[0]
# Largest page the grid will render, whatever a URL asks for. Off-menu sizes are
# honoured (a hand-typed `?limit=7` is harmless, and the pager copes), but an
# unbounded one would put the whole library through a template on one request.
_LIBRARY_LIMIT_MAX = 200
# Remembers the reader's chosen page size so a bare visit to `/` renders it in the
# first paint. A cookie rather than localStorage precisely because the server has
# to know: the Library grid is server-rendered inline (#139), so a size the client
# held would mean rendering the default and then replacing it — a visible reflow
# and a second request on every load.
_LIBRARY_LIMIT_COOKIE = "harmonist-library-limit"
_LIBRARY_LIMIT_COOKIE_MAX_AGE = 365 * 24 * 60 * 60

# Tabs on the index page. `?tab=` is validated against this before it reaches the
# template — the value is interpolated into a `panel-<name>` lookup in the tab
# script, so an unchecked one from the URL would be reflected into the page.
_INDEX_TABS = ("inbox", "library", "activity")


# Library filters (#174), slug → (label, predicate). Every predicate reads a field
# already on the in-memory Album, so filtering costs no lookup and no rescan
# (#140's no-MB-on-the-request-path constraint) and persists nothing — the grid
# asks a question about state it can already see, it does not record an answer.
#
# The control asks TWO questions, not one.
#
# *What is broken?* — the first three, which pick out albums that are terminal but
# wrong. That is the Library's original problem: it is where a defect goes to be
# forgotten. INCOMPLETE at least has a tile badge; a partially tagged album derives
# COMPLETE (`_files_tagged_with` is an `any()`) and shows nothing but a 10-pixel
# "8/10 tagged" line, and a coverless album shows nothing at all. Neither is
# findable at library scale.
#
# *What has moved that I could take?* — `update-available` (#287), which is not the
# same question and must not be read as one. An album whose release has grown an
# ISRC is not wrong; it is current as of the last time anything looked. It is
# invisible for a different reason: today the only way to discover an update is to
# open that album's page, so at library scale improvements to MusicBrainz land
# where nobody sees them.
#
# `update_available` is the one predicate NOT derived by the scanner — it is set by
# `gardener.refresh_flag` from the album page and the startup warm-up. Reading it
# is still free; the cost was paid when the flag was written. See the field's own
# note for why a False can mean "we have not looked".
#
# Insertion order is the order the control offers them. Slugs are URL-visible and
# outlive any wording change, so they are not derived from the labels.
def _is_actionable_incomplete(a: Album) -> bool:
    """INCOMPLETE, and the user has not already accepted it as finished (#196).

    The filter answers "what is wrong that I could fix?", so an album whose
    missing tracks are known to be unobtainable does not belong in it — the
    Pink Floyd Blu-ray where only the stereo mixes were ever ripped is not a
    defect, it is a decision. The album is still INCOMPLETE and still says so on
    its tile; it simply stops presenting itself as work.
    """
    return a.state == AlbumState.INCOMPLETE and not (
        a.sidecar is not None and a.sidecar.tracks_unavailable
    )


def _library_filters(
    ignored: Mapping[str, activity_store.IgnoredUpdate],
) -> dict[str, tuple[str, Callable[[Album], bool]]]:
    """The filter chips, in the order the control offers them.

    A function of this render's ignored updates rather than a constant, because
    one of the four is: an album whose update the user has ignored (#271) is not
    listed as work until MusicBrainz moves the release again.

    Only the FILTER subtracts them. The tile keeps its Update badge, exactly as
    an album accepted with `tracks_unavailable` keeps its Incomplete one — the
    difference is still a true fact about the album, and hiding a fact is a
    different act from not presenting it as something to do.
    """
    return {
        "incomplete": ("Incomplete", _is_actionable_incomplete),
        "partial": ("Partially tagged", lambda a: a.partial_tag_count is not None),
        "no-artwork": ("No artwork", lambda a: not a.has_cover),
        "update-available": (
            "Update available",
            lambda a: a.update_available and not gardener.is_ignored(a, ignored),
        ),
    }


#: The slugs the filter control accepts, and the label each one renders under.
#: Split out because two readers need only this half — validating a query
#: parameter and titling a filtered grid — and neither has an ignore map to
#: hand, nor any use for one.
_LIBRARY_FILTER_LABELS: dict[str, str] = {
    slug: label for slug, (label, _) in _library_filters({}).items()
}


def _ignored_updates() -> dict[str, activity_store.IgnoredUpdate]:
    """The user's ignored updates, or an empty map when the store cannot answer.

    **Falls open**, and the direction is the whole of the judgement. An empty map
    lists albums the user has asked to be left alone, which is annoying and
    visible and recoverable by pressing Ignore again; a map that wrongly said
    "ignored" would hide real work with no symptom at all. So the failure that
    shows too much is the one to take.

    Loud in the log rather than on the page: the surface it degrades is a filter
    count, and a banner over the Library saying a table could not be read is a
    worse answer than the Library simply being complete.
    """
    try:
        return activity_store.ignored_updates()
    except activity_store.StoreUnavailableError:
        log.exception("could not read which updates are ignored — listing them all this time")
        return {}


# When reading one album's tags is slow enough to say so (#300). Generous: this
# is every file in the album opened and parsed, so a long album on a modest NAS
# is legitimately a second or two, and #299 is the case where it is minutes.
_SLOW_ALBUM_READ = timedelta(seconds=10)

# When the whole disk-vs-MusicBrainz comparison is slow enough to say so. The
# most valuable of the three, and the reason it exists as well as the two
# beneath it: a page can be slow with no single phase crossing its own
# threshold, and this is the line that says so anyway.
_SLOW_COMPARE = timedelta(seconds=15)

# Longest search query the Library will act on (#180). Unlike `?filter=`'s slug,
# `q` is free text, and it is reflected into every URL this page builds — seventeen
# of them — so an unbounded one bloats the whole render. A hundred characters is
# past any album or artist name; beyond it, someone pasted something.
_LIBRARY_QUERY_MAX = 100

# How often the library is rescanned regardless of what the file watcher did
# (#151). A constant, not a setting: it is the period of a backstop nobody
# should have to reason about, and the two things it protects against — a
# network mount inotify can't see, a watcher killed by an exhausted watch limit
# — give the user nothing to tune against. The cost that would justify a knob
# isn't there either: an unchanged library is a stat per file and no tag reads.
_RESCAN_INTERVAL = timedelta(hours=1)

# The update check's cadence lives in `gardener` (`SWEEP_TICK`), not here
# (#349). It used to be `_UPDATE_CHECK_INTERVAL` beside the rescan's,
# which was wrong in a way that only shows up in the arithmetic: the tick is not
# an independent knob, it is the denominator the pass divides `SWEEP_WINDOW` by
# to size its slice. Kept in two modules the pair drifted, and the rate ended up
# at roughly eight times what the goal needed, delivered in hundred-request
# bursts. One constant, owned by the code that has to reason about it.

# The header that tells `/library` to name the resolved view in the address bar
# (#180). The search form cannot spell its own `hx-push-url`: it does not know the
# query until the reader types it, and the page size and filter it must carry come
# from the server anyway.
#
# A header rather than a hidden input, precisely so it stays OUT of the URL. The
# form degrades to a real GET on `action` without JS, and a marker in the field set
# would strand itself in the address bar there — the same wart `?anchor=` leaves,
# which is the bug `web-ui` rule 2b was written about. Nothing to strand if it was
# never a field.
_LIBRARY_PUSH_HEADER = "x-harmonist-push-url"


_logging_configured = False

# Log-line format, with vs without a timestamp. In a container the log driver
# stamps every line at capture (`docker logs -t`, or a collector) in one clock, so
# an app timestamp only collides with the driver's in a possibly-different TZ (a
# double-stamp) — omit it there, matching uvicorn's own convention (its
# access/error lines carry none). In bare local dev nothing else stamps, so keep a
# timestamp for readability.
_LOG_FORMAT_WITH_TS = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_LOG_FORMAT_NO_TS = "%(levelname)s %(name)s: %(message)s"


def _in_container() -> bool:
    """True when running inside a container. `HARMONIST_IN_CONTAINER` is baked into
    our image (explicit, build-time); `/.dockerenv` is Docker's runtime marker, so
    the package also self-detects if run in a container we didn't build."""
    return os.environ.get("HARMONIST_IN_CONTAINER") == "1" or Path("/.dockerenv").exists()


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
        fmt = _LOG_FORMAT_NO_TS if _in_container() else _LOG_FORMAT_WITH_TS
        handler.setFormatter(logging.Formatter(fmt))
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
    import platform

    log.info(
        "Harmonist %s — build %s — Python %s on %s",
        _app_version(),
        _git_sha(),
        platform.python_version(),
        platform.platform(),
    )
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
    # Durable activity + audit store (issue #33) — point it at the config dir before
    # anything records, so nothing is lost and the feed survives restarts. Demo mode
    # stays in memory: it shares the REAL config dir (only the music dir is
    # sandboxed), so a file-backed store would write demo events into the user's
    # genuine history — and re-open a DB the demo has no business touching (#69).
    if cfg.demo_mode:
        activity_store.init_memory()
    else:
        activity_store.init(cfg.paths.config_dir / "activity.db")
    # How long a fetched MusicBrainz release may be re-served (#127). After the
    # store is initialised, since that is where the rows live.
    mb_cache.configure(timedelta(seconds=cfg.musicbrainz.cache_ttl_seconds))
    # Copies of artwork a re-tag overwrote, so replacing it can be undone (#131).
    # `artwork_dir` sandboxes itself in demo mode rather than switching off, so
    # the demo exercises the real path.
    artwork_store.configure(cfg.artwork_dir, max_bytes=cfg.artwork_store.max_bytes)
    # Audit paths are recorded relative to the library (#98). Demo mode already
    # has its sandbox substituted into cfg, so this follows it automatically.
    audit.set_library_root(cfg.paths.music_dir)
    # A sidecar-less album's id derives from its path relative to the same root,
    # so re-pointing a bind-mount at the same library doesn't re-identify all of
    # it (#114).
    id_registry.set_library_root(cfg.paths.music_dir)
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

        def demo_resolve_after_download(album_dir: Path) -> None:
            # The demo twin of `resolve_after_download` below. MB is monkey-patched
            # by demo.install(), so this reaches the mocked catalogue rather than
            # the network — but it is otherwise the same code, which is the point:
            # a re-download's tagging is what the demo is there to show.
            _resolve_by_store_url(album_dir, cfg, tagger)

        def runner_fn() -> Any:
            # Same link-only rule as the real runner: the popover override wins,
            # else auto-detect (any Needs-Link album or pending potential-download).
            override = sync_runner.link_only_override
            sync_runner.link_only_override = None
            auto = live_counts.to_status()["needs_sync"] > 0 or pending_downloads.count() > 0
            link_only = override if override is not None else auto
            activity.info(
                "Sync started (link-only) — downloads are paused this sync."
                if link_only
                else "Sync started (full) — new purchases will be downloaded."
            )
            result = demo.run_demo_sync(
                cfg.paths.music_dir,
                link_only=link_only,
                ignores_file=cfg.ignores_file,
                progress_callback=sync_runner.set_current_item,
                # Re-downloads (#132) go through the REAL post-download resolve,
                # so the demo exercises the carry-through rather than staging its
                # outcome. See run_demo_sync for why only they do.
                post_download_callback=demo_resolve_after_download,
            )
            # Run the REAL post-sync mis-tag detection (like the non-demo runner),
            # so a mis-tag surfaces AFTER a sync rather than being pre-seeded. Pass
            # the demo-patched MB fns explicitly — the defaults were bound at import,
            # before demo.install() monkey-patched them.
            if result.unmatched_purchases():
                _detect_mistags_after_sync(
                    cfg,
                    result,
                    browse_rg=mb_lookup.browse_release_group_releases,
                    fetch_release=mb_cache.fetch_release,
                    albums=scan_runner.scan_now(),
                    progress=sync_runner.set_current_item,
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
            # Read config FRESH each run (app.state.cfg, set just below) so Settings
            # / Sync-popover changes — e.g. max-downloads — take effect without a
            # restart, rather than using a value captured at create_app time.
            cfg = sync_runner.app.state.cfg
            # Albums waiting to link to a purchase (NEEDS_SYNC) usually need an
            # OLD purchase that an incremental sync wouldn't re-page — so the
            # backfill could never see it. Force a full collection re-page when
            # any exist (clear the checkpoint; bandcampsync rewrites a fresh one
            # at the end, so later syncs go back to incremental). Self-limiting:
            # a full sync resolves every NEEDS_SYNC album (link OR surrender).
            pending_links = _force_full_sync_if_pending_links(cfg, scan_runner)
            # Adopt the existing library before fetching anything new: while any
            # album is unlinked (Needs Link), this sync runs LINK-ONLY — it links
            # every on-disk match and surrenders the rest, but downloads nothing,
            # so we never re-download a copy of an album already on disk. The Sync
            # popover can force link-only either way (e.g. adopt a fully-reconciled
            # library); a forced link-only with nothing pending still needs the
            # full re-page that _force_full_sync only does when pending > 0.
            override = sync_runner.link_only_override
            sync_runner.link_only_override = None
            link_only = override if override is not None else pending_links > 0
            if link_only and pending_links == 0:
                _clear_bandcampsync_checkpoint(
                    cfg.paths.music_dir, reason="link-only sync forced from the popover"
                )
            # The ONE sync-start entry, written here because this is where the
            # mode is decided — the runner's generic "started" line fired before
            # this point and so could never name it (#101).
            if link_only:
                activity.info(
                    f"Sync started (link-only) — {pending_links} album(s) to match. "
                    "Downloads are paused this sync and resume on the next one.",
                )
            else:
                activity.info("Sync started (full) — new purchases will be downloaded.")
            result = _run_bandcamp_sync(
                cfg,
                progress_callback=sync_runner.set_current_item,
                post_download_callback=resolve_after_download,
                link_only=link_only,
            )
            # Downloads are done; the remaining work (mis-tag detection, the
            # unmatched report, the rescan) can take a few seconds. Re-label the
            # status so it doesn't sit pinned to the last album's name.
            sync_runner.set_current_item("finishing up — checking matches…")
            # Post-sync matching used to cold-scan the whole library TWICE (~80s
            # each on a big NAS) for a silent multi-minute hang. Now: each pass
            # gets a FAST cache-backed scan_now() (only just-changed albums re-read
            # tags), runs in pipeline order so each sees the prior pass's links/
            # demotes (a shared stale snapshot would re-surrender a just-linked
            # album), shows progress, and the relink + mis-tag passes are SKIPPED
            # entirely when no purchase linked to nothing (the common case).
            #   1. relink albums whose purchase used a DIFFERENT one of the
            #      release's Bandcamp URLs than the tagged slug,
            #   2. spot mis-tags (release-group join → demote with a suggestion),
            #   3. surrender whatever's genuinely still unlinked.
            if result.unmatched_purchases():
                # The relink + mis-tag passes do one MB lookup per still-unlinked
                # album, and MusicBrainz caps us at ~1/sec — so tell the user why
                # "finishing up" can sit for minutes, rather than looking hung.
                activity.record(
                    "Cross-checking unmatched albums against MusicBrainz — limited to "
                    "~1 lookup/sec, so this can take a few minutes on a large sync.",
                )
                _link_unmatched_by_release_urls(
                    cfg,
                    result,
                    albums=scan_runner.scan_now(),
                    progress=sync_runner.set_current_item,
                )
                _detect_mistags_after_sync(
                    cfg,
                    result,
                    albums=scan_runner.scan_now(),
                    progress=sync_runner.set_current_item,
                )
            # `collection_checkpoint_token is None` means bandcampsync paged the
            # WHOLE collection (no checkpoint applied) — only then is "no matching
            # purchase" conclusive enough to surrender an album to NEEDS_MBID.
            full_sync = getattr(result, "collection_checkpoint_token", None) is None
            _report_unmatched_after_sync(cfg, full_sync=full_sync, albums=scan_runner.scan_now())
            # Observability for failed links: pair the surrender line's store_url
            # with the purchase URLs to answer "why didn't X link?". On a full
            # library these are few and meaningful (a real mismatch / re-download),
            # so log the count summary AND each purchase URL at INFO.
            unmatched = result.unmatched_purchases()
            if unmatched:
                audit.record("unmatched_purchases", count=len(unmatched))
                for pid, purl, plabel in unmatched:
                    audit.record("unmatched_purchase", item_id=pid, url=purl, label=plabel)
            scan_runner.request_scan()  # downloads/links landed → refresh the snapshot
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
        # Reuse the scanner's just-completed snapshot instead of re-walking the
        # whole library again (that second scan was minutes of silent, wasted
        # work + a second copy of the snapshot in RAM). Fall back to an internal
        # scan only if the background scanner hasn't produced one yet.
        snapshot = scan_runner.albums() if scan_runner.has_completed() else None
        reconcile_pending_orphans(
            cfg.paths.music_dir,
            fetch_urls=mb_cache.fetch_release_urls,
            fetch_video_media=mb_lookup.fetch_video_media,
            status_updater=status_updater,
            exempt_paths=forgotten_paths,
            albums=snapshot,
        )
        scan_runner.request_scan()  # sidecars written → refresh the snapshot

    reconcile_runner = ReconcileRunner(runner_fn=reconcile_runner_fn)

    # Auto-run reconcile once the initial library scan finishes — so MBID-tagged
    # (and ©cmt-URL-recoverable) orphans get sidecars on startup without needing
    # someone to open the inbox first.
    # Two things want the first snapshot, so the slot holds both rather than
    # either one quietly displacing the other: reconcile, and #287's warm-up of
    # the update-available flags. Reconcile goes first — it can change an album's
    # identity, and warming a flag onto an Album that reconcile is about to
    # replace would be work thrown away.
    def _on_first_scan() -> None:
        reconcile_runner.start()
        _start_flag_warm_up(scan_runner)

    scan_runner.set_on_first_complete(_on_first_scan)

    project_root = Path(__file__).resolve().parent.parent.parent.parent
    templates_dir = project_root / "templates"
    static_dir = project_root / "static"
    templates = Jinja2Templates(directory=str(templates_dir))
    templates.env.globals["harmony_base"] = HARMONY_BASE
    templates.env.globals["AlbumState"] = AlbumState
    templates.env.globals["store_name"] = store_name
    templates.env.globals["display_path"] = _display_path
    templates.env.globals["rel_path"] = _rel_path
    templates.env.globals["ago"] = _ago
    templates.env.globals["missing_discs"] = _missing_discs
    # The one MusicBrainz note's legend (#328), composed from BOTH comparisons.
    # A global rather than a context key because the two partials that render
    # the note are included from two different responses, and threading one more
    # value through each call site is how they drift apart.
    templates.env.globals["headline"] = compare.headline
    # Whether that note has anything to act on (#352) — a global for the same
    # reason, and reading the same two comparisons, so the tint cannot disagree
    # with the legend it is drawn behind.
    templates.env.globals["advisory"] = compare.advisory
    templates.env.globals["AUDIT_DETAIL_LIMIT"] = AUDIT_DETAIL_LIMIT
    # No `track_columns` global any more (#309). The tracklist's headings used
    # to be a module constant, which only worked while the answer was the same
    # for every album; they are now a property of the comparison, which is what
    # lets a column be EARNED by this album's tags. `_track_list.html` reads
    # `tracklist.columns`, so the headings and the row's cells still come from
    # one source and cannot get out of step.
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
        # Opt-in allocation tracing for memory diagnosis (off by default — it
        # roughly doubles per-object overhead). Set HARMONIST_TRACEMALLOC=1, then
        # read the top allocations from GET /debug/memory.
        if os.environ.get("HARMONIST_TRACEMALLOC"):
            import tracemalloc

            tracemalloc.start(int(os.environ.get("HARMONIST_TRACEMALLOC_FRAMES", "1")))
            log.info("tracemalloc enabled — GET /debug/memory for top allocations")
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
                settle=timedelta(seconds=cfg.library.watch_settle_seconds),
                stop_event=watch_stop,
            )
        )
        # And rescan on a timer regardless, as the backstop for a watcher that
        # isn't working — blind on a network mount (#152), or dead from an
        # exhausted inotify watch limit. Both are silent, which is the whole
        # reason this exists; neither is common, which is why it stays out of
        # sight (#151). It coalesces with the watcher's kick through `_dirty`
        # rather than competing with it — two kicks are one scan — and a rescan
        # that finds nothing changed is a stat per file and no tag reads.
        rescan_task = asyncio.create_task(
            periodic.run_periodically(
                _RESCAN_INTERVAL,
                lambda: _periodic_rescan_if_idle(sync_runner, reconcile_runner, scan_runner),
                name="library rescan",
                stop_event=watch_stop,
            )
        )
        # And ask MusicBrainz what it has been doing (#270) — still off unless
        # the user turned it on, but the *tick* is what checks, not the startup
        # config. The level moves from Settings now (#312), and a task that was
        # never created cannot be started by a config change; a sleeping timer
        # that returns immediately costs one asyncio task per process, which is
        # cheaper than the restart it saves. `_app.state.cfg` is read per tick
        # for the same reason — the closure's `cfg` is the startup one, and
        # reading it would make the setting save, look applied, and do nothing.
        check_task = asyncio.create_task(
            periodic.run_periodically(
                gardener.SWEEP_TICK,
                lambda: _update_check_if_idle(_app, sync_runner, reconcile_runner, scan_runner),
                name="update check",
                stop_event=watch_stop,
            )
        )
        try:
            yield
        finally:
            watch_stop.set()
            watch_task.cancel()
            rescan_task.cancel()
            check_task.cancel()
            with suppress(asyncio.CancelledError):
                await watch_task
            with suppress(asyncio.CancelledError):
                await rescan_task
            with suppress(asyncio.CancelledError):
                await check_task

    app = FastAPI(title="Harmonist", lifespan=lifespan)
    app.state.cfg = cfg
    sync_runner.app = app  # lets runner_fn read app.state.cfg fresh each sync
    app.state.templates = templates
    app.state.sync_runner = sync_runner
    app.state.reconcile_runner = reconcile_runner
    app.state.scan_runner = scan_runner
    app.state.forgotten_paths = forgotten_paths
    app.state.tagger = tagger

    @app.middleware("http")
    async def _rescan_after_mutation(request: Request, call_next: Any) -> Response:
        # One action scope per state-changing request, so the activity entry the
        # handler writes and every audit record beneath it share an action_id
        # (#84). Opened HERE, before call_next, because the id must be in scope
        # while the handler runs — Starlette copies the context into its
        # threadpool, so a sync `def` handler sees it without any plumbing.
        # GETs are excluded: they mutate nothing, so they produce no audit rows.
        response: Response
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            with activity_store.action():
                response = await call_next(request)
        else:
            response = await call_next(request)
        # A state-changing request likely touched the library (tag, forget,
        # confirm, erase…). Trigger a background re-scan; the per-album mtime
        # cache keeps it cheap, and request_scan() is a no-op until engaged.
        # An endpoint that changed nothing the inbox reflects (e.g. skipping a
        # potential download, or matching one to a Library album) sets
        # `request.state.skip_rescan` to opt out — a rescan there is pure inbox
        # flicker while it runs.
        if request.method in ("POST", "PUT", "PATCH", "DELETE") and not getattr(
            request.state, "skip_rescan", False
        ):
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
    # Always allow loopback so the container healthcheck (Host: 127.0.0.1) and
    # local curl keep working when the list is tightened to real hostnames —
    # TrustedHostMiddleware strips the port, so bare loopback names suffice.
    # (Skip when "*" is present; the list is already permissive.)
    allowed_hosts = list(cfg.server.allowed_hosts)
    if "*" not in allowed_hosts:
        allowed_hosts += ["127.0.0.1", "localhost", "::1"]
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)

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
            "DNS-rebinding protection. See docs/deployment.md.",
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
                "Demo reset failed", str(e), level=Level.ERROR, tasks_changed=False, status_code=400
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


def _album_comparison(
    album_dir: Path, release: Release, paths: Sequence[Path] | None = None
) -> tuple[compare.AlbumComparison, compare.TracklistComparison]:
    """Read the album's files and compare their tags to `release` (#106, #135).

    Per-track rather than one album-level read, because whether the tracks agree
    with each other is itself information the panel shows — a field carried by
    six of eight tracks is what the album says, with two to point at.

    The MusicBrainz side goes through `tagsets_for`, so the comparison is against
    what tagging WOULD write rather than a second reading of the release. Files
    are read in track order, which is what breaks a tie.

    Both halves of the page come out of this ONE pass over the files. The album
    panel and the tracklist want the same tags, and this is a full open of every
    file in the album — the read cost #106 flags, and the same cost problem as
    #44 / #74. Reading them twice for one page view would be careless.
    """
    audio, video = _album_tracks(album_dir, paths)
    tagsets = tagsets_for(release)
    mb_tracks = [
        compare.MBTrack(tags=ts, length_ms=length)
        for ts, length in zip(tagsets, match.mb_track_lengths(release), strict=True)
    ]
    return (
        # The album panel is the AUDIO's account of itself, deliberately (#226).
        # A video carries the same album-level tags but Harmonist never rewrites
        # them, so the first re-tag of an album with a bonus DVD would leave the
        # two halves disagreeing for good — a "label differs on 26 of 47 tracks"
        # pill that nothing on the page can clear.
        compare.AlbumComparison(
            fields=compare.album_fields(
                audio,
                tagsets[0] if tagsets else None,
                # Picard writes the release disambiguation into the album title
                # when told to, and that is the same album — not a mismatch to
                # report on every page view forever (#283).
                album_title_alias=title_with_disambiguation(
                    release.get("title"), release.get("disambiguation")
                ),
                # …and any country the release names is the country it came out
                # in, whichever one Picard's `preferred_release_countries` put
                # on the file (#346). The panel has to reach the same verdict as
                # `plan_album`, or the page reports a difference the Library has
                # already decided is not one.
                accepted_countries=tagger_mod.release_countries(release),
            )
        ),
        compare.tracklist(_in_track_order(audio + video), mb_tracks, _media_of(release)),
    )


def _album_disk_view(
    album_dir: Path, paths: Sequence[Path] | None = None
) -> tuple[compare.AlbumComparison, compare.TracklistComparison]:
    """The same two halves as `_album_comparison`, from the files ALONE (#228).

    For an album whose MusicBrainz release has been deleted. Both panels used to
    decline to show anything, which drops the wrong half: the banner says "your
    files are untouched and still carry its tags" and the page then declines to
    show them, when those tags are the evidence the user would use to find the
    replacement release.

    One pass over the files, for the same reason `_album_comparison` makes one.
    """
    audio, video = _album_tracks(album_dir, paths)
    return (
        compare.AlbumComparison(fields=compare.album_fields(audio, None), mb_available=False),
        compare.disk_tracklist(_in_track_order(audio + video)),
    )


def _album_tracks(
    album_dir: Path, paths: Sequence[Path] | None
) -> tuple[list[tuple[str, formats.TrackTags]], list[tuple[str, formats.TrackTags]]]:
    """The album's files read as `(name, tags)`, audio and video kept apart.

    Video is read too since #226 — a Picard-tagged `.m4v` states its disc, its
    position and its title exactly as the audio does, and not looking is what
    made a DVD with 26 of its 29 videos on disk report as entirely absent.

    Two lists rather than one because the two halves are used differently: the
    tracklist wants both, the album panel wants only the audio, and nothing that
    WRITES tags may see the video at all (#66).

    Names are relative to the album, so the two "01 - Intro.m4a" of a
    multi-folder album (#197) stay distinguishable in the tracklist. Identical
    to the bare name for a one-folder album.
    """
    audio = album_files.for_paths(paths) if paths else album_files.audio_files(album_dir)
    video = album_files.videos_for_paths(paths) if paths else album_files.video_files(album_dir)
    # The whole pass, not each file: one line naming a slow album is the signal,
    # twenty-seven lines naming its tracks is the noise (#300).
    with timing.warn_if_slow(
        "album tag read", _SLOW_ALBUM_READ, album=album_dir, files=len(audio) + len(video)
    ):
        return (
            [(_rel_to(f, album_dir), formats.read_tags(f)) for f in audio],
            [(_rel_to(f, album_dir), formats.read_video_tags(f)) for f in video],
        )


def _in_track_order(
    tracks: list[tuple[str, formats.TrackTags]],
) -> list[tuple[str, formats.TrackTags]]:
    """Audio and video interleaved back into one list, in track order.

    By name, which is the order both halves already arrived in — so "2-01
    Intro.m4v" lands after "1-21 Outro.m4a" rather than the videos trailing the
    whole album. Only matters where file order is what decides something: the
    disk-only tracklist (#228) renders in this order, and files carrying no
    track number at all are dealt into MusicBrainz's free slots by it.
    """
    return sorted(tracks, key=lambda t: album_files.sort_key(Path(t[0])))


def _merge_unscoped_audit(events: list[activity.Event], since: datetime) -> list[activity.Event]:
    """Fold audit rows that belong to no action into a page of activity entries,
    newest first (#123).

    They can't hang off an entry the way scoped rows do, so they take rows of
    their own. Merged by timestamp rather than appended, or a reconcile's
    `sidecar.create` would sit at the bottom of the page instead of beside the
    entry it happened alongside.
    """
    unscoped = [
        activity.Event(
            ts=r.ts,
            level=r.level,
            message=r.message,
            album_id=r.album_id,
            album_label=r.album_label,
            action_id=r.action_id,
            source=r.source,
        )
        for r in activity_store.audit_without_action(since)
    ]
    if not unscoped:
        return events
    return sorted([*events, *unscoped], key=lambda e: e.ts, reverse=True)


def _missing_discs(absent: frozenset[int], disc_total: int | None) -> str:
    """The completeness badge's text — "Disc 2 of 2 is missing" — for an album
    whose shortfall is a whole medium with no files at all (#245).

    Names the disc because it can: `absent_media` is scanner-derived from the
    files' own `disk` tags, so this costs no MusicBrainz call. What it cannot
    name is the disc's TITLE — MusicBrainz has that, the files don't, and the
    badge renders before the release is fetched. The number is what's free.

    "of N" only in the singular. "Discs 2 and 3 of 4 are missing" parses as a
    fraction of a fraction; the plural drops it and stays readable.

    Deliberately says nothing about "disk". The badge's other branch already ends
    "tracks on disk", and this one used to read "a disc of this release has no
    tracks on disk" — two load-bearing near-homophones four words apart, which is
    a pun to parse before the fact arrives (#245).
    """
    discs = sorted(absent)
    if not discs:
        return ""  # caller's branch guards this; nothing sensible to say
    if len(discs) == 1:
        of_total = f" of {disc_total}" if disc_total else ""
        return f"Disc {discs[0]}{of_total} is missing"
    listed = f"{', '.join(str(d) for d in discs[:-1])} and {discs[-1]}"
    return f"Discs {listed} are missing"


def _ago(when: datetime | None) -> str:
    """A timestamp as rough elapsed time — "3 days ago".

    What the user actually wants to know from these is recency ("did this happen
    recently?"), not the calendar date, and a relative figure answers that at a
    glance. Templates pair it with the exact timestamp in a `title` so nothing is
    lost. Deliberately coarse: no "1 month" vs "4 weeks" hair-splitting, since
    the precise answer is one hover away."""
    if when is None:
        return ""
    seconds = (datetime.now(UTC) - when).total_seconds()
    if seconds < 0:
        return "just now"  # clock skew; don't render "in -3 days"
    for size, unit in ((31_536_000, "year"), (2_592_000, "month"), (86_400, "day"), (3600, "hour")):
        if seconds >= size:
            n = int(seconds // size)
            return f"{n} {unit}{'s' if n != 1 else ''} ago"
    if seconds >= 60:
        n = int(seconds // 60)
        return f"{n} minute{'s' if n != 1 else ''} ago"
    return "just now"


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


def _git_sha() -> str:
    """The build's git commit — so a startup log line answers 'which build is
    this?' and prevents testing a stale deploy. Baked at Docker build time via
    HARMONIST_GIT_SHA; falls back to `git rev-parse` for a dev checkout (marking a
    dirty tree); else 'unknown'."""
    import subprocess

    if sha := os.environ.get("HARMONIST_GIT_SHA", "").strip():
        return sha[:12]
    root = Path(__file__).resolve().parent
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        if r.returncode != 0 or not r.stdout.strip():
            return "unknown"
        sha = r.stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        return f"{sha}-dirty" if dirty.returncode == 0 and dirty.stdout.strip() else sha
    except Exception:
        return "unknown"


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


def _completeness_oob(request: Request, album: Album) -> str:
    """The album page's completeness badge + its acceptance checkbox, rendered as
    an out-of-band swap (#227).

    The album passed in must be the album as it is AFTER whatever just changed —
    the badge states what the sidecar now says, and the caller holds the new one.
    Renders to nothing for an album that isn't INCOMPLETE, which is exactly right:
    there is no such blob on that page to replace.
    """
    template = _templates(request).env.get_template("partials/_completeness.html")
    return template.render(_ctx(request, album=album, oob=True))


def _update_ignore_oob(request: Request, album: Album, *, ignored: bool) -> str:
    """The album page's Ignore block, as an out-of-band swap (#271).

    The album passed in must carry the flag and the version as they are AFTER
    whatever just changed, since that is what the block reads. Renders to an
    empty wrapper when there is no update to act on, which is what clears the
    block after a re-tag has taken one.
    """
    template = _templates(request).env.get_template("partials/_update_ignore.html")
    # `oob` here and NOT when the update section includes this block: the section
    # is itself an out-of-band swap, and an OOB element nested inside one would
    # be processed twice (#366).
    return template.render(_ctx(request, album=album, update_ignored=ignored, oob=True))


def _update_check_oob(request: Request, outcome: str, *, ok: bool) -> str:
    """The background update check's note + Check now button, as an out-of-band
    swap carrying what the press just produced (#312).

    A partial rather than a string of markup built here, so its class names sit
    under `templates/` where Tailwind's `@source` globs can see them — a utility
    minted only in Python is silently absent from the bundle.
    """
    template = _templates(request).env.get_template("partials/_update_check.html")
    return template.render(_ctx(request, outcome=outcome, outcome_ok=ok, oob=True))


def _retag_short_oob(
    request: Request, album: Album, *, files: int, tracks: int, overwrite_art: bool
) -> str:
    """The album page's alert slot, stating that MusicBrainz now lists more tracks
    than the album has files, and offering the re-tag that accepts that (#252).

    Rendered out of band because the page had no way to know: the counts only
    disagree once the re-tag has fetched the release, and the album's own state
    says what MusicBrainz said at *tagging* time (#195), not what it says now.
    """
    template = _templates(request).env.get_template("partials/_retag_short.html")
    return template.render(
        _ctx(request, album=album, files=files, tracks=tracks, overwrite_art=overwrite_art)
    )


def _library_limit(request: Request, limit: int | None) -> int:
    """The page size for this render: the URL's `?limit=` when it names one, else
    the size the reader last chose, else the default (#144).

    The cookie is untrusted input like any other header, and it outlives the code
    that wrote it — a value from an older build, or one hand-edited — so it is
    parsed defensively and anything unreadable falls through to the default. A
    bad cookie must degrade to a normal-looking Library, never to a 500.
    """
    if limit is not None:
        return max(1, min(limit, _LIBRARY_LIMIT_MAX))
    remembered = request.cookies.get(_LIBRARY_LIMIT_COOKIE)
    if remembered:
        try:
            return max(1, min(int(remembered), _LIBRARY_LIMIT_MAX))
        except ValueError:
            pass
    return _LIBRARY_PAGE_SIZE


def _remember_library_limit(response: Response, limit: int | None) -> None:
    """Persist a page size the URL explicitly named, so the reader's choice
    survives the next visit.

    Only an explicit `?limit=` is remembered. Writing the cookie on every render
    would echo the default back at readers who never chose it, and would then keep
    re-asserting it — a preference nobody set, that nothing but clearing cookies
    could shift.
    """
    if limit is None:
        return
    response.set_cookie(
        _LIBRARY_LIMIT_COOKIE,
        str(max(1, min(limit, _LIBRARY_LIMIT_MAX))),
        max_age=_LIBRARY_LIMIT_COOKIE_MAX_AGE,
        httponly=True,  # read server-side only; no script needs it
        samesite="lax",
        # Deliberately NOT `secure`: Harmonist is commonly reached over plain HTTP
        # on a LAN (http://nas.local:8080). A Secure cookie there is silently never
        # stored, so the preference would appear to save and never stick.
    )


def _library_filter(value: str | None) -> str | None:
    """The validated filter slug for this render, or None for "All" (#174).

    `?filter=` is untrusted input reflected into the page (the `<select>`'s state,
    and every pager URL), so it is checked against the known set exactly as `?tab=`
    is. An unrecognised value degrades to All rather than to an empty grid: a slug
    from an older build, or a mangled link, should show the reader their library.
    """
    return value if value in _LIBRARY_FILTER_LABELS else None


def _library_search(value: str | None) -> str | None:
    """The search query for this render, or None for "not searching" (#180).

    `?filter=` could be checked against a known set; `q` is free text and cannot
    be, so it is normalised instead: stripped, length-clamped, and reduced to None
    when nothing is left. That last step matters more than it looks — it makes a
    blank box indistinguishable from no box at all, so `?q=` never appears in a URL
    promising a search that isn't happening, and every "is a search on?" test in
    the template is a plain truthiness check.
    """
    if value is None:
        return None
    # Clamp between two strips, so a cut landing mid-space doesn't leave a trailing
    # one to show up in the box and in every URL.
    return value.strip()[:_LIBRARY_QUERY_MAX].strip() or None


def _search_key(text: str) -> str:
    """`text` folded for searching: accents stripped, casefolded, anything that
    isn't a word character flattened to a space.

    So `Bjork` finds *Björk*, `dont` finds *Don't*, and `85 92` finds
    *…Works 85-92*. Deliberately its OWN function rather than `models.title_words`:
    those are the approximate-matching primitives that decide what gets LINKED to a
    MusicBrainz release, and they are safe only because their callers wrap them in a
    uniqueness guard. Sharing them here would create a path where loosening a search
    box loosens what Harmonist is willing to tag.
    """
    decomposed = unicodedata.normalize("NFKD", text)
    unaccented = "".join(c for c in decomposed if not unicodedata.combining(c))
    return re.sub(r"\W+", " ", unaccented).casefold()


def _search_matches(albums: list[Album], q: str) -> list[Album]:
    """`albums` narrowed to those whose artist or title matches `q` (#180).

    Every whitespace-separated term must appear somewhere in the folded
    "artist title" — so `aphex ambient` finds *Aphex Twin — Selected Ambient
    Works*, and word order doesn't matter. Substring, not prefix: `ambient` should
    find *Ambient* wherever it sits.

    Costs one fold per album per request and touches no disk and no network — the
    artist and title are already in the scanned `Album` (#140's constraint).
    """
    terms = _search_key(q).split()
    if not terms:
        # Folding left nothing word-like — and bands called `!!!` or `†††` are real,
        # so this is not a hypothetical. Matching everything (which `all([])` would
        # do) would silently ignore a query the reader can still see in the box, so
        # fall back to a raw case-insensitive substring test, which is exactly right
        # for a name made entirely of punctuation.
        needle = q.casefold()
        return [a for a in albums if needle in f"{a.artist} {a.title}".casefold()]
    return [a for a in albums if all(t in _search_key(f"{a.artist} {a.title}") for t in terms)]


def _library_index_url(page: int, limit: int, filter_: str | None, q: str | None = None) -> str:
    """The index URL naming one Library view. The template's `library_query` macro
    builds the same string for links it renders; this is for the two cases only the
    server can answer — where an `?anchor=` landed (#144), and what the search form
    just asked for (#180).

    `filter_` and `q` are omitted when empty so that the default view keeps the
    short URL. The slug is interpolated raw, which is safe *because* it came through
    `_library_filter`; `q` is free text, so it is percent-encoded — an unencoded `&`
    or `#` in a query would truncate this URL and silently drop the parameters after
    it.
    """
    url = f"/?tab=library&page={page}&limit={limit}"
    if filter_:
        url += f"&filter={filter_}"
    if q:
        url += f"&q={quote(q)}"
    return url


def _library_page_vars(
    albums: list[Album],
    page: int,
    limit: int,
    anchor: int | None = None,
    filter_: str | None = None,
    q: str | None = None,
) -> dict[str, Any]:
    """Everything `partials/library_page.html` needs to render one page of the
    Library grid (#139).

    Shared by `/library` and by the index, which renders the first page inline
    rather than fetching it: that keeps the grid's page a property of the
    server-rendered HTML, so a `?page=` link, a reload and a Back all produce the
    same thing, with no client-side state to fall out of step.
    """
    done = [a for a in albums if a.state in _TERMINAL_STATES]
    # Newest tagged first; albums missing tagged_at sink to the bottom.
    _floor = datetime.min.replace(tzinfo=UTC)
    done.sort(
        key=lambda a: a.sidecar.tagged_at if a.sidecar and a.sidecar.tagged_at else _floor,
        reverse=True,
    )
    # How many terminal albums exist, before any filter — the Library's own count,
    # and the number `data-total-done` reports. Captured here because `shown` below
    # is a DIFFERENT number once a filter is on, and the two must not be conflated:
    # the dataset says how big the library is, the pager says how much of it is on
    # screen (#174).
    total_done = len(done)
    # Search narrows BEFORE the filter, and the filter counts below are taken after
    # it, so every chip reports what it would yield *within this search* (#180).
    # The other order would have "No artwork · 40" sitting above a searched grid and
    # deliver 2 when clicked — a count that describes a population the reader can't
    # see is worse than no count.
    if q:
        done = _search_matches(done, q)
    # After the search, before the filter: the All chip's number, and the
    # denominator every other chip is a subset of.
    total_matched = len(done)
    # The options the control offers, each with the count it would yield. Counted
    # off the same `done` list the grid pages, so a count can never describe a
    # different population than selecting it would show. Computed on every render,
    # filtered or not — an option worth 0 albums should say so before it's picked
    # rather than answering with an empty grid.
    # One read for the whole render, so the chip's count and the grid it yields
    # are drawn from the same answer — two reads either side of a press would let
    # the count and the grid disagree by one.
    library_filters = _library_filters(_ignored_updates())
    filters = [
        {"slug": slug, "label": label, "count": sum(1 for a in done if pred(a))}
        for slug, (label, pred) in library_filters.items()
    ]
    if filter_ is not None:
        done = [a for a in done if library_filters[filter_][1](a)]
    limit = max(1, min(limit, _LIBRARY_LIMIT_MAX))  # clamp; defensive
    total_pages = max(1, -(-len(done) // limit))  # ceil; always at least one page
    # `anchor` is the 1-based position of the first album on screen at the moment
    # the reader changed the page size (#144), and it wins over `page`. Carrying
    # the page NUMBER across a size change teleports them — "page 3" is rows 41–60
    # at 20 per page and rows 81–120 at 40 — whereas resolving the anchor against
    # the new size leaves the album they were looking at on screen. Like `page`,
    # it's a hint and not identity: clamped, never trusted to be in range.
    if anchor is not None:
        page = (max(1, anchor) - 1) // limit + 1
    # Clamp rather than serve an empty grid. A page number outlives the albums that
    # filled it — bookmarked, restored by Back, or just held while a sync removed a
    # few — and a saved link resolving to a blank screen reads as "my library is
    # gone".
    page = max(1, min(page, total_pages))
    start = (page - 1) * limit
    rows = done[start : start + limit]
    return {
        "rows": rows,
        "page": page,
        "total_pages": total_pages,
        "prev_page": page - 1 if page > 1 else None,
        "next_page": page + 1 if page < total_pages else None,
        "limit": limit,
        # The sizes the control offers. An off-menu `limit` (hand-typed, or held
        # from an older build) is rendered as an extra option by the template
        # rather than being silently corrected, so the control always shows the
        # truth about what's on screen.
        "page_sizes": _LIBRARY_PAGE_SIZES,
        # The whole Library, unfiltered — the `data-total-done` attribute and the
        # "search all N" links both read this. A filtered grid must not let it
        # start reporting the rows it happens to be rendering (#140).
        "total_done": total_done,
        # How many albums the search left, before the filter — what the All chip
        # says, and what the chips beside it are subsets of. Equal to `total_done`
        # when nothing is being searched for (#180).
        "total_matched": total_matched,
        # How many albums the CURRENT view holds: the same number when nothing is
        # filtered, the matching subset when something is. Only the pager reads it.
        "total_shown": len(done),
        # The active filter slug, already validated — None means All. Rides in every
        # pager URL, the size form and the tile links, so the whole view stays one
        # set of parameters (#174).
        "filter": filter_,
        # Its human label, for the sentence the search box uses to say what it is
        # searching over. Resolved here so the template doesn't have to hunt through
        # `filters` for the active one (#180).
        "filter_label": _LIBRARY_FILTER_LABELS[filter_] if filter_ else None,
        "filters": filters,
        # The search query, normalised — None means no search. Rides in every URL
        # this page builds, and is echoed back into the box (#180).
        "q": q,
        # 1-based inclusive range of this page within the whole list, for the
        # "31–60 of 412" readout — a bare page number says nothing about scale.
        "first_row": start + 1 if rows else 0,
        "last_row": start + len(rows),
    }


def _ctx(request: Request, **extra: Any) -> dict[str, Any]:
    cfg: config_mod.Config = request.app.state.cfg
    base: dict[str, Any] = {
        "request": request,
        "cfg": cfg,
        "now": datetime.now(UTC),
        # Sync-popover state (header renders on every page): the current per-sync
        # cap, and whether link-only should default ON (unlinked albums or pending
        # downloads exist → the user is mid-adoption).
        "sync_max_downloads": cfg.bandcamp.max_downloads_per_sync,
        "sync_link_only_default": (
            live_counts.to_status()["needs_sync"] > 0 or pending_downloads.count() > 0
        ),
        # Why the header's global actions are inert on this page, or None if they
        # aren't. Derived here rather than passed per-route so a new page can't
        # forget it, and server-side because the poll JS that does the *busy*-state
        # gating only runs on the index page.
        "globals_off_reason": (
            "Finish in Settings first — a sync started here would read settings you're"
            " partway through changing."
            if request.url.path == "/settings"
            else None
        ),
    }
    base.update(extra)
    return base


# bandcampsync's collection checkpoint (Syncer.STATE_FILENAME) — lives in the
# music dir root. Hardcoded to avoid importing bandcampsync internals here.
_BANDCAMPSYNC_STATE_FILE = ".bandcampsync-state.json"


def _clear_bandcampsync_checkpoint(music_dir: Path, *, reason: str) -> bool:
    """Remove bandcampsync's collection-checkpoint file if present. Returns
    True if a file was removed. Never raises — best-effort.

    `reason` goes in the audit line. There are two callers now and they clear it
    for genuinely different reasons — unlinked albums whose purchase an
    incremental sync wouldn't re-page, and a re-download whose purchase is
    older still (#132) — so the reason can't be a constant here without the log
    confidently misattributing half of them."""
    state_file = music_dir / _BANDCAMPSYNC_STATE_FILE
    try:
        if state_file.is_file():
            audit.record("checkpoint.clear", path=state_file, reason=reason)
            state_file.unlink()
            return True
    except OSError as e:
        log.warning("could not remove bandcampsync checkpoint %s: %s", state_file, e)
    return False


def _force_full_sync_if_pending_links(cfg: config_mod.Config, scan_runner: ScanRunner) -> int:
    """Count albums waiting to link to a Bandcamp purchase (NEEDS_SYNC) at sync
    start, and if any, clear the collection checkpoint so the upcoming sync
    re-pages the WHOLE collection (their purchase is usually an old one an
    incremental sync wouldn't load). Returns that count.

    The caller uses ``>0`` to run the sync **link-only** — adopt the existing
    library (link every match, surrender the rest) and download nothing — so we
    never re-download a copy of an album that's sitting on disk unlinked. A full
    link-only pass drains NEEDS_SYNC to 0; downloads resume on the next sync.
    Self-limiting and self-correcting.

    Uses the scanner's existing snapshot (no fresh walk) when available.
    Best-effort: never raises into the sync runner (returns 0 on error).
    """
    try:
        albums = (
            scan_runner.albums() if scan_runner.is_engaged() else scanner.scan(cfg.paths.music_dir)
        )
        pending = sum(1 for a in albums if a.state == AlbumState.NEEDS_SYNC)
        if pending and _clear_bandcampsync_checkpoint(
            cfg.paths.music_dir, reason="pending Needs-Sync links"
        ):
            log.info(
                "Forcing a full Bandcamp sync: %d album(s) await a purchase link",
                pending,
            )
        return pending
    except Exception:
        log.exception("force-full-sync check failed")
        return 0


# Mis-tag detection does ~1 MB browse per still-unlinked album, so it's bounded
# by Set A (the "unmatched after sync" albums) — NOT by the collection. If even
# Set A is this large after a sync, something's off; bail rather than storm MB.
_MISTAG_DETECTION_MAX_ALBUMS = 200


class _UnmatchedSource(Protocol):
    """Structural type for mis-tag detection's only dependency on the syncer:
    the list of owned purchases that linked to no album. A real
    `HarmonistSyncer` satisfies it, as does any test double."""

    def unmatched_purchases(self) -> list[tuple[int, str, str]]: ...


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
            # Bare strings are the join phrases — see `_artist_phrase` in tagger.py.
            if isinstance(ac, str):
                parts.append(ac)
            elif isinstance(ac, dict):
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
    # `replace`, not a fresh `Sidecar(...)` (#263). The surrenders are claims
    # about the SOURCE — no purchase exists, no more tracks exist — so a demote
    # over the release identity leaves them as true as it found them. Stale
    # `video_media` rides along harmlessly: nothing reads it while the album has
    # no MBID, and `_tag_with_release` recomputes it from the release it uses.
    sidecar_mod.write(
        album_path,
        replace(sc, mb_release_id=None, mb_match_candidate=candidate, tagged_at=None),
    )
    # Surrender / mis-tag demote: the album was an unlinked NEEDS_SYNC, now back
    # to NEEDS_MBID. Keep the live counts moving between scans.
    live_counts.move(AlbumState.NEEDS_SYNC, AlbumState.NEEDS_MBID)


def _link_album_to_purchase(album_path: Path, sc: Sidecar, *, item_id: int, store_url: str) -> None:
    """Fill in the Bandcamp item_id on an already-tagged album's sidecar (Needs
    Sync → Library), adopting the matched purchase URL as the store_url."""
    # `replace`, not a fresh `Sidecar(...)` (#263).
    sidecar_mod.write(
        album_path,
        replace(
            sc,
            store_url=store_url,
            bandcamp=BandcampInfo(
                item_id=item_id,
                band_id=sc.bandcamp.band_id if sc.bandcamp else None,
                is_private=sc.bandcamp.is_private if sc.bandcamp else False,
            ),
            # Cleared deliberately — see `_link_pending_to_album`. A linked
            # purchase falsifies "there is no purchase to link, ever".
            purchase_unavailable=False,
        ),
    )
    # NEEDS_SYNC → Library (COMPLETE proxy; the scan reset splits the library
    # total into COMPLETE/INCOMPLETE exactly).
    live_counts.move(AlbumState.NEEDS_SYNC, AlbumState.COMPLETE)


def _link_unmatched_by_release_urls(
    cfg: config_mod.Config,
    syncer: _UnmatchedSource,
    *,
    fetch_urls: Callable[[str], list[str]] = mb_cache.fetch_release_urls,
    albums: list[Album] | None = None,
    progress: Callable[[str], None] | None = None,
) -> None:
    """Link an unmatched NEEDS_SYNC album to an unmatched purchase when the
    purchase's slug is ANY of the album's MB release's Bandcamp URLs — not just
    the single store_url it was tagged with.

    A release often exposes several Bandcamp URLs (e.g. ``/album/x`` and
    ``/album/x-2``, or an artist page plus a label page). The purchase frequently
    uses a different slug than the one the album was tagged with, so the plain
    slug match misses it and the album would needlessly surrender to NEEDS_MBID.
    Runs BEFORE mis-tag detection/surrender. Cost is one MB url-rels call per
    unmatched album — bounded by the (small) failed set. Best-effort; never
    raises into the sync runner."""
    try:
        owned: dict[str, tuple[int, str]] = {}  # slug -> (item_id, url)
        for item_id, url, _label in syncer.unmatched_purchases():
            if slug := album_slug(url):
                owned.setdefault(slug, (item_id, url))
        scanned = albums if albums is not None else scanner.scan(cfg.paths.music_dir)
        unmatched = [
            a
            for a in scanned
            if a.state == AlbumState.NEEDS_SYNC and a.sidecar and a.sidecar.mb_release_id
        ]
    except Exception:
        log.exception("relink-by-release-urls: setup failed")
        return
    if not unmatched or not owned:
        return
    if len(unmatched) > _MISTAG_DETECTION_MAX_ALBUMS:
        return  # the mis-tag step reports the over-cap warning; don't double-report

    for i, a in enumerate(unmatched, 1):
        if progress:
            progress(f"checking matches on MusicBrainz (~1/sec)… linking {i}/{len(unmatched)}")
        assert a.sidecar is not None  # guaranteed by the comprehension filter
        assert a.sidecar.mb_release_id is not None
        try:
            urls = fetch_urls(a.sidecar.mb_release_id)
        except mb_lookup.MBError:
            continue
        release_slugs = {s for u in urls if (s := album_slug(u))}
        matches = {s: owned[s] for s in release_slugs if s in owned}
        if len(matches) != 1:
            continue  # 0 = no owned purchase for this release; ≥2 = ambiguous
        item_id, purchase_url = next(iter(matches.values()))
        _link_album_to_purchase(a.path, a.sidecar, item_id=item_id, store_url=purchase_url)
        # Don't let a second album claim the same purchase this pass.
        owned = {s: v for s, v in owned.items() if v[0] != item_id}
        activity.record(
            f"{a.artist} — {a.title}: Needs Link → Library "
            f"(linked to Bandcamp purchase {item_id} via the release's MB URL)"
        )


def _detect_mistags_after_sync(
    cfg: config_mod.Config,
    syncer: _UnmatchedSource,
    *,
    browse_rg: Callable[[str], list[tuple[str, list[str]]]] = (
        mb_lookup.browse_release_group_releases
    ),
    fetch_release: Callable[[str], Release] = mb_cache.fetch_release,
    albums: list[Album] | None = None,
    progress: Callable[[str], None] | None = None,
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
        owned: dict[str, tuple[int, str, str]] = {}  # slug -> (item_id, url, label)
        for item_id, url, label in syncer.unmatched_purchases():
            slug = album_slug(url)
            if slug:
                owned.setdefault(slug, (item_id, url, label))

        scanned = albums if albums is not None else scanner.scan(cfg.paths.music_dir)
        albums = [
            a
            for a in scanned
            if a.state == AlbumState.NEEDS_SYNC and a.sidecar and a.sidecar.mb_release_id
        ]
    except Exception:
        log.exception("mis-tag detection: setup failed")
        return
    if not albums or not owned:
        return
    if len(albums) > _MISTAG_DETECTION_MAX_ALBUMS:
        activity.warning(
            f"Mis-tag detection skipped: {len(albums)} unmatched albums after sync exceeds "
            f"the cap of {_MISTAG_DETECTION_MAX_ALBUMS} — something looks wrong with this sync."
        )
        return

    # Only act on release groups with exactly one unmatched album — otherwise we
    # can't tell which album an owned release pairs with. Keep each album's
    # currently-tagged (wrong) release so we can name it in the UI without a
    # second fetch.
    rg_albums: dict[str, list[tuple[Album, Release]]] = {}
    for i, a in enumerate(albums, 1):
        if progress:
            progress(f"checking matches on MusicBrainz (~1/sec)… mis-tags {i}/{len(albums)}")
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
        owned_item_id, url, label = owned[owned_slug]
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
        # Claim the purchase out of the potential-downloads list: it's now
        # represented by this mis-tag card (confirming re-tags + links it), so it
        # must NOT also show as a potential download. `replace_all` already ran
        # during the sync, so remove the now-claimed id.
        pending_downloads.remove(owned_item_id)
        # Id read back AFTER _demote_to_needs_mbid rewrote the sidecar — that
        # write clears the MBID, so a pre-demote id is already dead (#65).
        mistag_id, mistag_label = _live_album_ref(album)
        activity.warning(
            f"Possible mis-tag. You own “{label}” on Bandcamp ({url}) — the same "
            f"release group but a different release than it's tagged as. Moved to "
            f"Needs MBID with {owned_mbid} suggested; confirm to re-tag.",
            album_id=mistag_id,
            album_label=mistag_label,
        )


def _report_unmatched_after_sync(
    cfg: config_mod.Config, *, full_sync: bool, albums: list[Album] | None = None
) -> None:
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
        scanned = albums if albums is not None else scanner.scan(cfg.paths.music_dir)
    except Exception:
        log.exception("post-sync unmatched scan failed")
        return
    unmatched = [a for a in scanned if a.state == AlbumState.NEEDS_SYNC]
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
            activity.warning(
                f"Not linked to a Bandcamp purchase: {a.artist} — {a.title} "
                f"[{store_url or 'no store URL'}] (use 'Try a different URL' to link it)"
            )
        return

    # Albums already LINKED to a purchase, keyed by release — used to flag a
    # surrendered album that's tagged as the SAME release as a linked one (a
    # likely duplicate copy, OR a legitimate release split across directories —
    # we don't try to tell them apart here, just surface it).
    linked_by_release: dict[str, list[Album]] = {}
    for a in scanned:
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
        activity.warning(
            f"No Bandcamp purchase matched {a.artist} — {a.title} — kept its tags, moved "
            "to Needs MBID. Add its Bandcamp URL to the MusicBrainz release (or match it "
            "manually) so it links instead of risking a duplicate download."
        )
        if twins:
            activity.warning(
                f"Heads up: {a.artist} — {a.title} is tagged as the same MusicBrainz "
                f"release as “{twins[0].title}” ({twins[0].path.name}), which already "
                f"linked to a purchase — possibly a duplicate copy, or a release split "
                f"across directories."
            )


def _artwork_plan(album: Album, event_id: int) -> dict[str, str]:
    """`{file_name: digest}` for the artwork change shown under `event_id`.

    Rebuilt from this album's own stored records — never from client input —
    and grouped by exactly the function that produced the summary the user is
    looking at, so Undo cannot act on a different set of files from the one it
    described.
    """
    history = activity_store.album_history(album.id)
    detail = activity_store.tag_changes_for([e.id for e in history])
    records = tag_history.group_records(history, detail).get(event_id)
    return tag_history.artwork_replaced(records) if records else {}


def _restorable_anchors(
    history: list[activity_store.StoredEvent],
    detail: dict[int, activity_store.TagChanges],
) -> set[int]:
    """Which history rows can actually have their artwork undone.

    Checked before rendering rather than on click: the store evicts oldest
    first, so an old enough change has no images left, and offering a button
    that would fail is worse than offering none. One `path_for` glob per
    distinct digest — cheap enough for a page render.
    """
    out: set[int] = set()
    for anchor, records in tag_history.group_records(history, detail).items():
        plan = tag_history.artwork_replaced(records)
        if plan and all(artwork_store.path_for(d) is not None for d in set(plan.values())):
            out.add(anchor)
    return out


def _revert_plan(album: Album, event_id: int) -> tuple[tag_history.FileRevert, ...]:
    """The per-file revert for the tagging shown under `event_id`.

    Rebuilt from this album's own stored records — never from client input —
    and grouped by exactly the function that produced the summary the user is
    looking at, so Undo cannot act on a different set of files from the one it
    described. Same contract as `_artwork_plan`.
    """
    history = activity_store.album_history(album.id)
    detail = activity_store.tag_changes_for([e.id for e in history])
    records = tag_history.group_records(history, detail).get(event_id)
    return tag_history.revert_plan(records) if records else ()


def _unlink_after_revert(album: Album, outcome: tagger_mod.RevertOutcome) -> bool:
    """Make the sidecar follow the files after an undo moved `mb_album_id` (#158).

    A sidecar naming a release the files no longer carry derives as **TAGGING**
    (`scanner._derive_state`) — the transient spinner state, with no action on
    it and no way out. So when the undo changes the album's identity, the
    sidecar's release id goes with it and the album derives as NEEDS_MBID.

    **The release is kept as a suggestion**, not thrown away: the card then
    offers Confirm & Tag, which is the one-click way back, and a note says why
    the album is there. Without it the user would have to know an MBID by heart
    to undo their own undo.

    **Which release to suggest** is whatever the files now say, falling back to
    the one being unlinked when they say nothing. Undoing a re-match reverts the
    files to the *older* release, and that older release — not the one the
    sidecar happened to be holding — is what the user just asked to go back to.

    Confirming re-tags through the ordinary path, which rewrites the release's
    own totals into the files, so nothing here has to guess a track count it has
    no lookup for.

    The mutation itself is `sidecar.unlink`, shared with the "wrong match"
    pencil (#166) — the two differ only in the candidate they pass, which is
    exactly the part that should differ.

    Returns whether the sidecar was rewritten.
    """
    sc = album.sidecar
    if not outcome.release_id_reverted or sc is None:
        return False
    if not owned.values_differ(sc.mb_release_id, outcome.release_id_now):
        return False  # already agrees — nothing to do

    suggest = outcome.release_id_now or sc.mb_release_id
    files = len(album_files.audio_files(album.path))
    candidate = (
        MatchCandidate(
            mb_release_id=suggest,
            # It WAS the confirmed release — this is not a fresh guess, and
            # calling it approximate would invite the user to re-check a match
            # they made themselves.
            confidence="exact",
            file_count=files,
            # The count the FILES report (#195), so the card offers the same
            # Confirm it would have before. `track_comparisons` stays empty: it
            # would need an MB fetch, and an undo makes no network call.
            track_count=album.expected_track_count or files,
            proposed_at=datetime.now(UTC),
            notes=["Unlinked when you undid the tagging that linked this album"],
        )
        if suggest
        else None
    )
    sidecar_mod.unlink(album.path, sc, candidate=candidate)
    return True


def _revert_detail(outcome: tagger_mod.RevertOutcome, *, unlinked: bool = False) -> str:
    """What the undo did, as one line under the flash heading.

    Counts what went back, but NAMES what didn't. The fields that were put back
    are the ones the user just read in the summary and expected to move; the
    ones left alone are the surprise, and a bare count of those would leave them
    guessing which. Usually there are none, sometimes one or two, and the case
    where a later re-tag moved everything is exactly the case worth spelling out.
    """
    parts: list[str] = []
    if outcome.files:
        fields = len(outcome.restored)
        parts.append(
            f"{fields} field{'s' if fields != 1 else ''} across "
            f"{outcome.files} file{'s' if outcome.files != 1 else ''}"
        )
    if outcome.stale:
        parts.append(f"{_named_fields(outcome.stale)} left alone (changed since)")
    if unlinked:
        # The one consequence that isn't a tag: the album has left the Library.
        # Saying where it went, and that the release is still on offer, is the
        # difference between an undo and an album that vanished.
        parts.append("now Needs MBID — its release is kept as a suggestion")
    return "; ".join(parts)


#: How many field names a message spells out before it starts counting. Naming
#: the outliers is the point, but a re-tag that moved everything would otherwise
#: put the whole owned set in the status bar.
_NAMED_FIELD_LIMIT = 4


def _named_fields(fields: tuple[str, ...]) -> str:
    """Field names for prose, capped so a long list stays a sentence."""
    named = [tag_history.label_for(f) for f in fields[:_NAMED_FIELD_LIMIT]]
    rest = len(fields) - len(named)
    return ", ".join(named) + (f" and {rest} more" if rest else "")


def _revertable_anchors(
    history: list[activity_store.StoredEvent],
    detail: dict[int, activity_store.TagChanges],
) -> set[int]:
    """Which history rows are worth offering an Undo on.

    Deliberately a WEAKER check than `_restorable_anchors` does for artwork,
    and the asymmetry is the point. Artwork availability is one `glob` per
    digest — cheap enough to answer before rendering, so no button is offered
    that would fail. Whether a tag revert would still apply can only be
    answered by reading every file's tags, which is a full pass over the album
    per tagging on the page, on the request path. Not worth it.

    So the button appears when the tagging changed something revertable at all,
    and a revert that finds every field already changed says so when clicked —
    "nothing left to put back" rather than a silent success.
    """
    out: set[int] = set()
    for anchor, records in tag_history.group_records(history, detail).items():
        if any(item.fields for item in tag_history.revert_plan(records)):
            out.add(anchor)
    return out


def _media_of(release: Release) -> list[compare.Medium]:
    """The release's discs, in position order — position, name, format.

    MusicBrainz names a medium where it has a name, and the names are often
    better than "Disc 2": Hybrid's two are *Wide Angle* and *Live Angle*. Most
    releases have none, in which case the tracklist heading falls back to the
    number (#216).
    """
    out: list[compare.Medium] = []
    for medium in release.get("medium-list") or []:
        try:
            position = int(medium.get("position", 0))
        except (TypeError, ValueError):
            continue
        out.append(
            compare.Medium(
                position=position,
                title=(medium.get("title") or None),
                format=(medium.get("format") or None),
            )
        )
    return out


def _shape_mismatch(album: Album, release: Release) -> tuple[int, int] | None:
    """`(what the files say, what MusicBrainz says)` when they disagree about how
    many media the release has — else None.

    TISM's *The White Albun* is the case (#204): 16 files tagged `disc 1/1`,
    against a release MusicBrainz says is a DVD, a CD and another DVD. The album
    derives COMPLETE because by its own tags it IS complete, and nothing else
    ever notices — the disagreement is the only evidence, and it is discarded.

    That is not an incompleteness to count. It means the album is tagged against
    a release its own tags do not describe, which is closer to a mis-tag (#17):
    either the tags are stale, or the release is the wrong one. Both are the
    user's call, and both are fixed from this page — Re-tag rewrites the tags
    from the release, Wrong MusicBrainz match picks a different one.

    Checked here rather than at scan time because it needs the release, and the
    scan has no MusicBrainz by design. Same discoverability limit as #194: it
    surfaces when the album is opened, not before.
    """
    if album.disc_total is None:
        return None  # the files do not agree on a shape, so there is none to check
    media = len(release.get("medium-list") or [])
    return None if media in (0, album.disc_total) else (album.disc_total, media)


def _absent_media_summary(album: Album, release: Release) -> list[tuple[int, str, int]]:
    """The release's media that have no files on disk, as (position, format, tracks).

    Read off the release the page has just fetched for its comparison, so it
    costs nothing extra. The state derivation cannot do this — it runs at scan
    time with no MusicBrainz — which is exactly why an album missing only video
    discs needs `sidecar.video_media` recorded to come out COMPLETE (#206).
    Showing them is the other half: "complete" must not mean "we quietly stopped
    mentioning two whole discs".
    """
    if not album.absent_media:
        return []
    out: list[tuple[int, str, int]] = []
    for medium in release.get("medium-list") or []:
        try:
            position = int(medium.get("position", 0))
        except (TypeError, ValueError):
            continue
        if position in album.absent_media:
            tracks = medium.get("track-list") or []
            count = len(tracks) or int(medium.get("track-count") or 0)
            out.append((position, str(medium.get("format") or "Disc"), count))
    return out


def _album_folders(album: Album, music_dir: Path) -> list[tuple[str, int]]:
    """Every directory this album's files come from, as (relative path, count).

    An album can span several directories since #16, and the page's single Path
    row could then only ever name one of them. Listing them is how the user
    confirms Harmonist picked up ALL the files — the grouping is otherwise
    invisible, and the only alternative check is counting tracks and hoping
    (#198).

    Reads `album.folders`, which is the album's own answer since #197 — NOT
    `audio_files(album.path)`, which is directory-scoped and therefore sees only
    the primary folder. Getting that wrong made this feature silently report a
    single folder for exactly the albums it was built for.

    Returns [] for the ordinary one-folder album — the Path row above already
    names it, so there is nothing to add.

    Paths are relative to the LIBRARY ROOT rather than to the album, because
    since #197 an album's folders need not share a parent at all: `Hybrid/Wide
    Angle` and `Live Albums/Hybrid/Live Angle` is a supported layout, and a path
    relative to one of them would be nonsense.
    """
    if len(album.folders) <= 1:
        return []
    return [(_rel_to(d, music_dir), len(album_files.audio_files(d))) for d in sorted(album.folders)]


def _rel_to(path: Path, root: Path) -> str:
    """`path` relative to `root`, or its bare name when it is not underneath."""
    try:
        return str(path.relative_to(root))
    except ValueError:
        return path.name


def _embedded_cover(album_path: Path) -> tuple[bytes, str] | None:
    """Extract embedded cover art (bytes, mime) from the album's first audio
    file, or None. Used by /cover to serve art without writing it to disk."""
    try:
        files = album_files.audio_files(album_path)
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


def _periodic_rescan_if_idle(
    sync_runner: SyncRunner, reconcile_runner: ReconcileRunner, scan_runner: ScanRunner
) -> None:
    """One tick of the hourly library rescan (#151) — unless something else is
    mid-flight.

    The watcher waits for the music dir to go quiet precisely so it can never
    scan mid-copy. A timer has no such instinct, and a rescan landing halfway
    through a sync would read a part-written album directory and hand it to
    `_announce_discoveries`, which would record Harmonist as having "started
    tracking" a folder that was still being written — a permanent feed entry
    about a transient. Reconcile is milder, since sidecar writes are atomic, but
    it is actively invalidating the snapshot the rescan would build.

    Skipping the tick is enough, and better than deferring it: the next one
    comes round soon, and both runners request a scan of their own when they
    finish, so nothing is waiting on this one.
    """
    if sync_runner.is_running or reconcile_runner.is_running:
        log.info("Skipping periodic rescan: sync or reconcile in progress")
        return
    scan_runner.request_quiet_rescan()


# Held for the duration of a pass, so a slow one cannot be started again on top
# of itself. A pass is capped at a hundred albums and normally takes two
# minutes, but a MusicBrainz that answers slowly rather than failing can stretch
# it past the hour — and two passes at once would ask about the same albums
# twice and spend twice the budget doing it.
_update_check_lock = threading.Lock()


def _update_check_if_idle(
    app: FastAPI,
    sync_runner: SyncRunner,
    reconcile_runner: ReconcileRunner,
    scan_runner: ScanRunner,
) -> str | None:
    """One gardener pass (#270), unless something with a better claim is running.

    Returns `None` when a pass was started, and otherwise the reason it wasn't,
    phrased for a person: the hourly tick discards it — the log line beside each
    guard is its channel — but **Check now** on the Settings page is a button a
    user just pressed, and a control that answers a press with silence reads as
    broken whether it declined or ran.

    `app` rather than the level itself, because the level moves at runtime
    (#312): `app.state.cfg` is re-read on every tick, so saving the setting
    takes effect on the next one instead of at the next restart. The runners are
    the same three objects for the life of the process, so they are passed.

    Four guards, in the order they are checked. The **level** comes first and is
    not about timing at all: `off` means Harmonist has not been given the budget
    to go and look, so there is nothing to weigh.

    The other three share one reason: the MusicBrainz rate limit is a single
    shared queue, and anything the user set in motion is waiting on it. A sync or
    a reconcile pass is spending it already, and the check is in no hurry — its
    albums have been unasked-about for a week and ten more minutes change
    nothing.
    It skips the tick rather than deferring it, like the periodic rescan does,
    because the next one comes round soon and nothing is waiting on this one. The
    first scan is the same argument at startup: the pass reads
    `scan_runner.albums()`, which is empty until then, so an early tick would
    report a library of nothing. And a pass still running refuses a second on top
    of itself, which would ask about the same albums twice.

    **Its own thread.** The pass blocks on the network for a second per album
    and then on the disk, which are the two things that must never happen on the
    event loop. Daemon, and not cancelled at shutdown: it records a hint and
    persists nothing but the cache rows it fills on the way, so being killed
    part-way through loses only work the next pass redoes.
    """
    cfg: config_mod.Config = app.state.cfg
    if cfg.gardener.level == "off":
        log.debug("Skipping update check: background update checks are off")
        return "background update checks are off"
    if sync_runner.is_running or reconcile_runner.is_running:
        # DEBUG since #349 shortened the tick to ten minutes: a long sync would
        # otherwise write this same line six times an hour for as long as it
        # ran, which is the noise that makes a log unread. Nothing is lost by
        # standing aside quietly — the user started the sync, and **Check now**
        # still answers with the reason in words.
        log.debug("Skipping update check: sync or reconcile in progress")
        return "a sync or reconcile is using the MusicBrainz budget"
    if not scan_runner.has_completed():
        # DEBUG for the same reason: the first scan of a large library outlasts
        # several ten-minute ticks, and "not scanned yet" during the first scan
        # is the expected state rather than news. The scan announces itself.
        log.debug("Skipping update check: the library has not been scanned yet")
        return "the library hasn't finished scanning yet"
    if not _update_check_lock.acquire(blocking=False):
        log.warning("Skipping update check: the previous pass is still running")
        return "a check is already running"

    def _run() -> None:
        try:
            gardener.sweep(scan_runner.albums())
        except Exception:
            # Boundary catch: the pass already absorbs the failures it expects
            # — a fetch that errors, a file it cannot read — so anything
            # arriving here is a defect rather than a bad night. Loud, and
            # loud every time: at 3am the log is the only channel, and a pass
            # that has been dying quietly for a month is worth repeating.
            log.exception("update check failed")
        finally:
            _update_check_lock.release()

    threading.Thread(target=_run, name="harmonist-update-check", daemon=True).start()
    return None


def _start_flag_warm_up(scan_runner: ScanRunner) -> None:
    """Rebuild the update-available flags once the library snapshot exists (#287).

    The flags live in process memory, so a restart leaves the Library's "Update
    available" filter reading zero until something looks at each album again.
    `gardener.warm_from_cache` refills it from stored MusicBrainz payloads and
    the files on disk, spending **no** MusicBrainz requests.

    Its own thread, not the scan thread that triggers it: this is minutes of
    file reads on a large library, and blocking there would hold up the scan
    completion every other startup task is waiting behind. Not an asyncio task
    either — it must never run on the event loop, and there is nothing to await
    it for.

    Daemon, and deliberately not cancelled at shutdown: it reads, records a
    hint, and persists nothing, so being killed mid-pass loses only work that
    the next startup redoes.
    """

    def _run() -> None:
        try:
            gardener.warm_from_cache(scan_runner.albums())
        except Exception:
            # Boundary catch: one unforeseen failure in a background hint must
            # not take down the thread silently. Loud, because nobody is
            # watching — the visible symptom would otherwise be a filter that
            # is simply emptier than it should be, which reads as "no updates".
            log.exception("update-available warm-up failed")

    threading.Thread(target=_run, name="harmonist-flag-warmup", daemon=True).start()


def _refreshed_from_disk(request: Request, album: Album) -> Album:
    """Re-read this album's directories and return it as it is on disk NOW (#151).

    The album page is where the user decides whether to re-tag, so it is the one
    page whose reading must not be a snapshot of whenever the last scan ran —
    and on a network mount the watcher never fires, so "whenever" can be
    startup. One directory listing and a `stat` per file buys the guarantee.

    Not wired to the inbox or the Library: those render every album, so the same
    check there would be a walk of the library per page view — which is the
    hourly rescan's job, on the hourly rescan's cadence.

    When the runner isn't engaged (a TestClient without the lifespan) every
    request already scans, so there is nothing to refresh.

    The album is re-resolved by DIRECTORY rather than by id, because the reading
    that just landed may have changed the id — an externally-written sidecar
    naming a different release, or parts merging into one album (#197). The
    pre-refresh album is the fallback for the case where the refresh dropped it:
    saying "not found" about an album the user is looking at, on the strength of
    one directory read, is the worse answer.
    """
    runner: ScanRunner = request.app.state.scan_runner
    if not runner.is_engaged():
        return album
    known = album.paths or (album.path,)
    runner.refresh_album(known)
    for a in runner.albums():
        if any(p in known for p in (a.paths or (a.path,))):
            return a
    return album


def _find_album(request: Request, album_id: str) -> Album:
    """Look up an album by its canonical id (mb_release_id, temp_uid, or
    registry UUID for NEW albums). Falls back to a registry reverse lookup
    so a stale inbox URL still works when auto-reconcile has rewritten
    the album's identity between page render and click. 404 only when
    we can't resolve the id any way.

    URLs to sidecar'd albums are stable across directory renames (the
    UUID lives in the sidecar JSON which moves with the directory).
    """
    albums = _albums(request)
    for a in albums:
        if a.id == album_id:
            return a
    # Race fallback: the rendered page may hold the pre-sidecar id of an album
    # whose canonical id has since changed (auto-reconcile beat the user). That
    # id derives from the album's path, so re-derive it for each album on disk
    # and see which one matches.
    legacy_path = id_registry.path_for(album_id, [a.path for a in albums])
    if legacy_path is not None:
        for a in albums:
            if a.path == legacy_path:
                return a
    # Durable fallback: the album's identity has since MOVED (tagging replaced its
    # temp_uid with an MBID, a re-match rewrote the MBID). The registry can't help
    # — it only derives the pre-sidecar id, so it can't answer for anything whose
    # identity has moved on since. The alias chain, recorded at each
    # change, does: it maps the superseded id forward to the current one (#33).
    # This is what keeps an old activity-feed deep link working.
    #
    # A store failure here is NOT a miss: we simply couldn't check, and the album
    # may well be on disk. Answer 503, never 404 — telling the user their album is
    # gone because SQLite hiccuped is the failure the error-handling skill opens
    # with (#104).
    try:
        current_id = activity_store.resolve_alias(album_id)
    except activity_store.StoreUnavailableError as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "can't resolve this link right now — the history store is unavailable. "
            "The album may still be in your library; try the Library tab.",
        ) from exc
    if current_id is not None:
        for a in albums:
            if a.id == current_id:
                return a
    raise HTTPException(status.HTTP_404_NOT_FOUND, f"album {album_id} not found")


def _norm_name(s: str) -> str:
    """Casefold + strip to alphanumerics for approximate artist/title matching:
    '&' → 'and', drop punctuation, collapse whitespace. Deliberately strict — we
    require an exact *normalized* match AND a unique candidate, so the safety comes
    from uniqueness, not fuzziness. Loosen later if it misses (e.g. compilations)."""
    s = (s or "").casefold().replace("&", " and ")
    return " ".join(re.sub(r"[^a-z0-9]+", " ", s).split())


_ARTIST_FEAT_MARKERS = {"feat", "featuring", "ft"}


def _norm_artist(s: str) -> str:
    """Normalize an artist for the match scope so trivially-different renderings of
    a collaboration land in the same bucket: Bandcamp writes 'A / B', tags write
    'A and B' or 'A & B' — drop the 'and' (the '/', ',' separators already collapse
    to gaps, '&' → 'and') so all three match. Also truncate at a featuring marker,
    since Bandcamp's band page is usually the primary artist ('A feat. B' ↔ 'A')."""
    words = _norm_name(s).split()
    for i, w in enumerate(words):
        if w in _ARTIST_FEAT_MARKERS:
            words = words[:i]
            break
    return " ".join(w for w in words if w != "and")


def _reconcile_suggestions(
    albums: list[Album],
    pending: list[pending_downloads.PendingPurchase],
    base: Path,
) -> tuple[dict[int, dict[str, str]], dict[str, pending_downloads.PendingPurchase]]:
    """Best-effort case-B pairing between potential downloads and on-disk albums by
    approximate (normalized) artist+title — the store_url join having failed
    (re-slug, or a pre-existing CD rip). Returns:

    - ``pending_suggestions``: ``item_id`` → the on-disk album a potential download
      probably already IS (shown on the potential-download card), and
    - ``surrender_suggestions``: ``album.id`` → the potential download a NEEDS_SYNC
      album probably IS (shown on the surrender card).

    Match rule: **exact artist** (normalized) scopes the comparison to one
    artist's catalogue, then titles match by **word-subsequence** (`titles_match`)
    — loose is fine at that scope, and it absorbs MB-vs-Bandcamp title differences
    (a trailing "EP", "(Deluxe)", a dropped "The", …). Only **unambiguous** pairs
    are offered: exactly one candidate on that side (non-empty title; album side
    not already ``item_id``-linked). Otherwise the manual search box is the
    fallback. Both directions call the same ``/pending/{id}/match`` link."""
    # Unlinked on-disk albums grouped by normalized artist → (title words, album).
    albums_by_artist: dict[str, list[tuple[tuple[str, ...], Album]]] = {}
    for a in albums:
        linked = bool(a.sidecar and a.sidecar.bandcamp and a.sidecar.bandcamp.item_id)
        words = title_words(a.title)
        if linked or not words:
            continue
        albums_by_artist.setdefault(_norm_artist(a.artist), []).append((words, a))

    pend_by_artist: dict[str, list[tuple[tuple[str, ...], pending_downloads.PendingPurchase]]] = {}
    for p in pending:
        words = title_words(p.title)
        if not words:
            continue
        pend_by_artist.setdefault(_norm_artist(p.band), []).append((words, p))

    pending_suggestions: dict[int, dict[str, str]] = {}
    for p in pending:
        pw = title_words(p.title)
        cand_albums = albums_by_artist.get(_norm_artist(p.band), [])
        alb_cands = [a for (w, a) in cand_albums if titles_match(pw, w)]
        if len(alb_cands) == 1:
            a = alb_cands[0]
            pending_suggestions[p.item_id] = {
                "id": a.id,
                "artist": a.artist,
                "title": a.title,
                "path": str(a.path),
                "rel_path": _rel_path(a.path, base),
            }

    # The reverse suggestion shows on cards for owned-but-unlinked albums: a
    # NEEDS_SYNC album, or a *surrendered* one (NEEDS_MBID whose candidate is an
    # `unmatched_purchase` — a full sync couldn't find its purchase). Both are
    # "you own this, we just couldn't link it" — the natural place to offer the
    # matching potential download.
    surrender_suggestions: dict[str, pending_downloads.PendingPurchase] = {}
    for a in albums:
        cand = a.sidecar.mb_match_candidate if a.sidecar else None
        surrendered = (
            a.state == AlbumState.NEEDS_MBID and cand is not None and cand.unmatched_purchase
        )
        if a.state != AlbumState.NEEDS_SYNC and not surrendered:
            continue
        aw = title_words(a.title)
        cand_pend = pend_by_artist.get(_norm_artist(a.artist), [])
        pend_cands = [p for (w, p) in cand_pend if titles_match(aw, w)]
        if len(pend_cands) == 1:
            surrender_suggestions[a.id] = pend_cands[0]

    return pending_suggestions, surrender_suggestions


def _pending_suggestions(request: Request) -> dict[int, dict[str, str]]:
    """The potential-download → on-disk-album suggestions for the current state —
    used when re-rendering the pending section/card outside the full inbox."""
    cfg: config_mod.Config = request.app.state.cfg
    ps, _ = _reconcile_suggestions(
        _albums(request), pending_downloads.all_pending(), cfg.paths.music_dir
    )
    return ps


def _render_ignored_section(request: Request) -> Response:
    """Re-render the #ignored-section partial (the target of a Restore swap)."""
    cfg: config_mod.Config = request.app.state.cfg
    ctx = _ctx(request, ignored=_read_user_ignores(cfg.ignores_file))
    return _templates(request).TemplateResponse(request, "partials/_ignored.html", ctx)


def _render_pending_section(request: Request) -> Response:
    """Re-render the #pending-section partial (the target of every action swap)."""
    ctx = _ctx(
        request,
        pending=pending_downloads.all_pending(),
        pending_suggestions=_pending_suggestions(request),
    )
    return _templates(request).TemplateResponse(request, "partials/_pending.html", ctx)


# A comment line of 10+ '=' splits ignores.txt: user-entered ids above,
# bandcampsync's "already downloaded" ids below. Mirrors its own detection.
_IGNORES_SEPARATOR = re.compile(r"^\s*#.*={10,}")


def _ignores_split(text: str) -> tuple[list[str], list[str]]:
    """`(user_lines, auto_lines)` — the halves of ignores.txt either side of the
    separator. With no separator the whole file is the user region, which is
    correct: bandcampsync appends the separator *at the end* when it first adds
    one, so everything already present becomes user data."""
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if _IGNORES_SEPARATOR.match(line):
            return lines[:i], lines[i:]
    return lines, []


def _ignored_ids(text: str) -> set[str]:
    """Every item_id already present in ignores.txt, from BOTH regions —
    bandcampsync honours an id wherever it appears in the file."""
    ids: set[str] = set()
    for line in text.splitlines():
        token = line.partition("#")[0].strip()
        if token.isdigit():
            ids.add(token)
    return ids


def _append_ignore(ignores_file: Path, item_id: int, label: str) -> None:
    """Record a purchase the user declined, in ignores.txt's USER region.

    Written above the separator, not appended (#77). Below it is bandcampsync's
    auto-managed list of already-downloaded ids, which it rewrites wholesale from
    the copy it read at startup — so an append there is both semantically wrong
    (indistinguishable from "already downloaded", which is what blocks a UI over
    this data) and racy: bandcampsync's own source notes that changes made while
    it runs are lost.

    Best-effort; a failed write just means the purchase may re-surface."""
    try:
        ignores_file.parent.mkdir(parents=True, exist_ok=True)
        text = ignores_file.read_text(encoding="utf-8") if ignores_file.exists() else ""
        # Already ignored? Nothing to do (#79). Checked across BOTH regions: an
        # id in the auto-managed region is honoured just the same, so re-adding
        # it changes no behaviour and only inflates the file. bandcampsync guards
        # its own writes this way; ours didn't, so re-deciding a purchase — which
        # the pre-#77 write race made routine — appended a duplicate each time.
        if str(item_id) in _ignored_ids(text):
            return
        user, auto = _ignores_split(text)
        if user and not user[-1].endswith("\n"):
            user[-1] += "\n"
        user.append(f"{item_id}  # {label}\n")
        ignores_file.write_text("".join(user + auto), encoding="utf-8")
    except OSError as e:
        log.warning("could not append %s to ignores %s: %s", item_id, ignores_file, e)


def _read_user_ignores(ignores_file: Path) -> list[dict[str, Any]]:
    """Purchases the user explicitly declined — newest first.

    ONLY the user region (above the separator). The auto-managed region below it
    is bandcampsync's record of everything already downloaded; listing that would
    present the user's whole collection as "ignored" (#19).

    Entries written before #77 landed in the auto region and are unrecoverable as
    user intent, so they simply don't appear. That's the safe direction to fail:
    an ignore we can't prove was deliberate is better hidden than wrongly offered
    for un-ignoring."""
    try:
        text = ignores_file.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []  # nothing declined yet — a genuinely empty answer
    except OSError:
        # A permission or I/O error is NOT "you have declined nothing". Settings
        # would show an empty list, so nothing could be restored and the user
        # would have no way to tell why (#104).
        log.exception(
            "could not read ignores %s — declined purchases can't be listed", ignores_file
        )
        return []
    user, _ = _ignores_split(text)
    out: list[dict[str, Any]] = []
    for line in user:
        token, _, comment = line.partition("#")
        token = token.strip()
        if token.isdigit():
            out.append({"item_id": int(token), "label": comment.strip() or token})
    out.reverse()  # newest first — they're appended in order
    return out


def _remove_user_ignore(ignores_file: Path, item_id: int) -> bool:
    """Drop one id from the USER region so the next sync considers it again.
    Returns True if a line was removed. Never touches the auto-managed region —
    removing an already-downloaded id there would re-download the album."""
    # False means "there was no such line", which the caller treats as a no-op and
    # reports nothing. A read/write failure is a different thing entirely — the
    # user pressed Restore and nothing happened — so it has to be loud (#104).
    try:
        text = ignores_file.read_text(encoding="utf-8")
    except OSError:
        log.exception("could not read ignores %s — Restore did nothing", ignores_file)
        return False
    user, auto = _ignores_split(text)
    kept = [ln for ln in user if (ln.partition("#")[0].strip() or None) != str(item_id)]
    if len(kept) == len(user):
        return False
    try:
        ignores_file.write_text("".join(kept + auto), encoding="utf-8")
    except OSError:
        log.exception("could not rewrite ignores %s — Restore did nothing", ignores_file)
        return False
    return True


def _remove_ignore_anywhere(
    ignores_file: Path, item_id: int
) -> Literal["removed", "absent", "failed"]:
    """Drop one id from BOTH regions so the next sync downloads it again (#132).

    The sibling above refuses to touch the auto-managed region, on the grounds
    that removing an already-downloaded id there would re-download the album.
    That is precisely the intent here: the album has just been archived off disk,
    so there is no copy to duplicate and the ignore is now the only thing
    standing between the user and the files they asked for.

    Rewriting bandcampsync's region is safe *between* syncs and only then — it
    reads the file once at startup and rewrites it wholesale from that snapshot,
    so an edit made while a sync runs is silently discarded. The caller refuses
    to archive anything while a sync is in flight, which is what makes this hold.

    Three outcomes, not two, because "the id wasn't in the file" and "I couldn't
    rewrite the file" want opposite responses and a bool would conflate them. An
    adopted album was never downloaded by bandcampsync, so it has no auto-region
    line and `absent` is the *ordinary* result — warning about it would cry wolf
    on the common case. `failed` is the one worth interrupting the user for: the
    ignore stands, so the replacement they were promised will never arrive.
    """
    try:
        text = ignores_file.read_text(encoding="utf-8")
    except FileNotFoundError:
        return "absent"  # nothing ignored yet — genuinely nothing to remove
    except OSError:
        log.exception("could not read ignores %s — %s stays ignored", ignores_file, item_id)
        return "failed"

    def keep(line: str) -> bool:
        return (line.partition("#")[0].strip() or None) != str(item_id)

    user, auto = _ignores_split(text)
    kept_user, kept_auto = [ln for ln in user if keep(ln)], [ln for ln in auto if keep(ln)]
    if len(kept_user) + len(kept_auto) == len(user) + len(auto):
        return "absent"
    try:
        ignores_file.write_text("".join(kept_user + kept_auto), encoding="utf-8")
    except OSError:
        log.exception("could not rewrite ignores %s — %s stays ignored", ignores_file, item_id)
        return "failed"
    audit.record("ignores.remove", item_id=item_id, file=ignores_file, reason="re-download")
    return "removed"


def _search_albums(request: Request, q: str, *, limit: int = 25) -> list[dict[str, str]]:
    """On-disk albums whose artist/title/folder matches `q` — the Match picker's
    candidates, each carrying its display path for disambiguation."""
    ql = q.strip().lower()
    if not ql:
        return []
    base = request.app.state.cfg.paths.music_dir
    out: list[dict[str, str]] = []
    for a in _albums(request):
        if ql in f"{a.artist} {a.title} {a.path.name}".lower():
            out.append(
                {
                    "id": a.id,
                    "artist": a.artist,
                    "title": a.title,
                    "path": str(a.path),
                    "rel_path": _rel_path(a.path, base),
                }
            )
            if len(out) >= limit:
                break
    return out


def _link_pending_to_album(album: Album, p: pending_downloads.PendingPurchase) -> None:
    """Link a potential download to an existing on-disk album: fill the purchase's
    item_id (+ store_url) onto its sidecar, creating a minimal one if untagged.

    If the album had **surrendered** (a full sync couldn't find its purchase, so it
    was demoted to NEEDS_MBID with an `unmatched_purchase` candidate), the purchase
    turned out to exist after all — so un-surrender it: restore the release id from
    the candidate (the files are still tagged with it, so no re-tag) → Library.
    """
    sc = album.sidecar
    if sc is None:
        sidecar_mod.write(
            album.path,
            Sidecar(
                store_url=p.url,
                bandcamp=BandcampInfo(item_id=p.item_id),
            ),
        )
        return

    cand = sc.mb_match_candidate
    surrendered = sc.mb_release_id is None and cand is not None and cand.unmatched_purchase
    resolved_mbid = cand.mb_release_id if (surrendered and cand is not None) else sc.mb_release_id
    sidecar_mod.write(
        album.path,
        # `replace`, not a fresh `Sidecar(...)` (#263).
        replace(
            sc,
            store_url=p.url,
            bandcamp=BandcampInfo(
                item_id=p.item_id,
                band_id=sc.bandcamp.band_id if sc.bandcamp else None,
                is_private=sc.bandcamp.is_private if sc.bandcamp else False,
            ),
            mb_release_id=resolved_mbid,
            mb_match_candidate=None if surrendered else sc.mb_match_candidate,
            # Cleared DELIBERATELY, not carried (#263). `purchase_unavailable`
            # is the claim "there is no purchase to link, ever" — and a purchase
            # is being linked on this very line, so the claim is now false. This
            # is the one place the flag is falsified by evidence rather than by
            # the user changing their mind.
            purchase_unavailable=False,
        ),
    )
    # If it's now tagged, it leaves the inbox for the Library — move counts from
    # the album's ACTUAL state (NEEDS_SYNC or surrendered NEEDS_MBID). The scan
    # reset splits COMPLETE/INCOMPLETE exactly.
    if resolved_mbid is not None and album.state not in _TERMINAL_STATES:
        live_counts.move(album.state, AlbumState.COMPLETE)


def _persist_max_downloads(request: Request, value: int) -> None:
    """Update + persist the per-sync download cap (the SAME setting as the Settings
    page — the Sync popover just exposes it inline). The runner reads app.state.cfg
    fresh each sync, so it takes effect immediately."""
    cfg: config_mod.Config = request.app.state.cfg
    if value == cfg.bandcamp.max_downloads_per_sync:
        return
    new_bandcamp = cfg.bandcamp.model_copy(update={"max_downloads_per_sync": value})
    new_cfg = cfg.model_copy(update={"bandcamp": new_bandcamp})
    config_mod.write_settings(cfg.paths.config_dir, {"bandcamp.max_downloads_per_sync": value})
    request.app.state.cfg = new_cfg


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


# bandcampsync's own ignores template, verbatim. It ships this ONLY inside its
# Docker image (at the hard-coded path "/ignores.template.txt"), not in the pip
# package, so we vendor it here. The auto-managed id section + delimiter is
# written by bandcampsync itself on first add, so this is just the documented
# header; pre-writing it means bandcampsync's broken `copyfile` is skipped.
_IGNORES_TEMPLATE = """\
# This file allows you to exclude releases from downloads.
#
# Add one bandcamp item id per line, optionally followed by a comment.
# For example:
# 1546934218  # Chrome Sparks / Sparks EP
# 1418240212  # Chrome Sparks / Goddess EP
#
# To get an item id, you can click on Share/Embed on the release page, click
# "Embed this album", choose an embed size, and within the embed code, look for
# the album=<...> portion of the link.
"""


def _run_bandcamp_sync(
    cfg: config_mod.Config,
    *,
    progress_callback: Callable[[str], None] | None = None,
    post_download_callback: Callable[[Path], None] | None = None,
    link_only: bool = False,
) -> HarmonistSyncer:
    """Build a HarmonistSyncer and let it run end-to-end.

    ``link_only`` runs the sync in adopt mode: link on-disk matches + surrender
    the rest, download nothing (used while any album is still Needs Link).
    """
    if not cfg.cookies_file.exists():
        raise FileNotFoundError(
            f"cookies file not found at {cfg.cookies_file} — Bandcamp sync requires a cookies.txt"
        )
    cookies = cfg.cookies_file.read_text(encoding="utf-8")
    cfg.paths.music_dir.mkdir(parents=True, exist_ok=True)
    cfg.ignores_file.parent.mkdir(parents=True, exist_ok=True)
    # Seed a missing ignores file from the vendored template (see above), so
    # bandcampsync's first-run `copyfile` of its image-only template is skipped.
    if not cfg.ignores_file.exists():
        cfg.ignores_file.write_text(_IGNORES_TEMPLATE, encoding="utf-8")
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
        link_only=link_only,
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
    # Cached: this is assessment, not action. `_tag_with_release` below re-reads
    # the chosen release fresh before it writes anything (#127).
    releases = [mb_cache.fetch_release(m) for m in mbids]
    candidate = best_match(album_path, releases)
    if candidate is None:
        return "no_match", "No MusicBrainz release linked."

    if candidate.confidence == "exact":
        _tag_with_release(album_path, candidate.mb_release_id, cfg, tagger)
        return "tagged", "Match exact — files tagged."

    existing = sidecar_mod.read(album_path)
    # `replace` off whatever is there (#263) — a recheck that lands a suggestion
    # must not also silently undo a surrender the user recorded.
    new = replace(
        existing or Sidecar(),
        added_at=(existing.added_at if existing else None) or datetime.now(UTC),
        mb_release_id=None,
        mb_match_candidate=candidate,
    )
    sidecar_mod.write(album_path, new)
    return (
        "needs_confirmation",
        f"Match found ({candidate.confidence}) — please review and confirm.",
    )


def _claim_pending_by_store_url(store_url: str | None) -> None:
    """Drop any potential download whose Bandcamp slug matches `store_url` — the
    purchase is now represented on disk (just tagged), so it mustn't linger as a
    pending download. Subdomain-insensitive via `album_slug`. Covers confirming a
    mis-tag (store_url set to the owned edition's URL) and any tag that resolves a
    pending purchase."""
    if not store_url:
        return
    slug = album_slug(store_url)
    if not slug:
        return
    for p in pending_downloads.all_pending():
        if album_slug(p.url) == slug:
            pending_downloads.remove(p.item_id)


def _record_merge(album_path: Path, old_mbid: str, new_mbid: str) -> None:
    """Say that MusicBrainz merged one release into another (#268).

    Called AFTER the sidecar write, for two reasons. The album's id has just
    moved with it, and an entry stamped with the pre-write id would hang off a
    release the sidecar no longer names (#65). And the entry is a report of
    something that happened, not of something attempted — a tagging that threw
    part-way should not leave a line claiming the identity moved.

    The alias that keeps the album's pre-merge history reachable is NOT recorded
    here: `sidecar.write()` already records one whenever the canonical id
    changes, and this is one of those. Adding a call would duplicate it at one
    call site while leaving every other identity change relying on the write —
    the shape the event-recording skill warns about.

    Both sinks, because they answer different questions. The activity entry is
    the outcome in the user's language, and the audit line is the pair of ids —
    which is forensics, and doesn't belong in prose the feed renders next to an
    album name it already shows in its own column.
    """
    album_id = sidecar_mod.album_id_for(album_path)
    audit.record(
        "release.merged",
        album_id=album_id,
        album=album_path,
        old=old_mbid,
        new=new_mbid,
    )
    activity.record(
        "MusicBrainz merged the release this album named into another one — "
        "it now follows the surviving release",
        album_id=album_id,
        album_label=album_path.name,
    )


def _tag_with_release(
    album_path: Path,
    mbid: str,
    cfg: config_mod.Config,
    tagger: Tagger,
    *,
    incomplete: bool = False,
    store_url_override: str | None = None,
    overwrite_art: bool = False,
    paths: Sequence[Path] | None = None,
) -> None:
    """Fetch MB release, fetch cover, write tags, update sidecar.

    `mbid` is what to ask MusicBrainz for, not necessarily what the album ends
    up tagged as: a merged release redirects, and this follows the release it
    actually got (#268). See the rebinding below.

    `incomplete=True` runs the tagger in incomplete mode (file_count <
    MB track count allowed). Nothing is persisted about the count: the tagging
    writes the release's own totals into every file, and that is what the
    scanner derives INCOMPLETE from afterwards (#195).

    `store_url_override` replaces the sidecar's store_url. Used when confirming
    a mis-tag: the album is actually the *owned* edition, so its store_url must
    become the URL where the user purchased it (the candidate's
    `mistag_owned_url`) — otherwise the old (wrong-edition) URL matches no
    purchase and the album can never link, falling through to surrender.
    """
    # FRESH, never cached (#127). This writes tags to the user's files, and
    # doing that from an hour-old payload would put metadata on disk that
    # Harmonist had already been told was superseded. It still goes through
    # `mb_cache` rather than round it, so the stored row — the gardener's
    # baseline — is refreshed by the fetch that was happening anyway.
    release = mb_cache.fetch_release(mbid, max_age=mb_cache.FRESH)
    # MusicBrainz REDIRECTS a merged MBID, so the release that comes back can
    # carry a different id from the one asked for. That difference IS the merge
    # notification — cheap, exact, and the only one there is (#268). Everything
    # downstream follows the id the release actually has, because the tagger
    # writes `release["id"]` into the files either way: disagreeing with it left
    # the sidecar naming a release that no longer exists, the album deriving
    # TAGGING off its own files, and reconcile rewriting the identity through a
    # path meant for "the user re-tagged in Picard".
    #
    # `mbid` is REBOUND rather than a second name being introduced, so the
    # correction holds by construction: a line added below that reaches for the
    # album's release gets the one it was tagged as, not the one that was asked
    # for. Only the merge check itself wants the original.
    #
    # Unconditional, and it stays unconditional under #32's unattended pass:
    # there is nothing to authorise, because the merge has already happened on
    # MusicBrainz and this is only noticing it. Undo is the escape hatch, as it
    # is for any tagging. Not the significance classifier's business (#267) —
    # that decides which TAG changes may auto-apply, and this is an identity
    # fact arriving beside them.
    requested_mbid, mbid = mbid, release["id"]
    rg = release.get("release-group") or {}
    cover_path = cover_art.ensure_cover(
        album_path,
        release_mbid=release["id"],
        release_group_mbid=rg.get("id"),
        size=cfg.cover_art.size,
    )
    tagger.tag_album(
        album_path,
        release,
        cover_path=cover_path,
        incomplete=incomplete,
        overwrite_art=overwrite_art,
        # An album can span several directories (#197); `album_path` is only its
        # primary one, so tagging what is under that alone would leave the rest
        # of the album on its old tags.
        files=album_files.for_paths(paths) if paths else None,
    )

    sc = sidecar_mod.read(album_path)
    store_url = store_url_override or (sc.store_url if sc else None)
    if store_url is None:
        # No store_url yet (e.g. a manual download assigned an MBID directly).
        # Derive the Bandcamp store URL so a purchase lands in Needs Link rather
        # than Complete: embedded ©cmt URL → MB url-rel → artist-root placeholder,
        # all gated by ©cmt Bandcamp evidence. Best-effort — never blocks tagging.
        try:
            store_url = reconcile.store_url_for_tagging(
                album_path, mbid, fetch_urls=mb_cache.fetch_release_urls
            )
        except Exception:
            log.exception("store_url derivation during tagging failed")
    # `replace`, not a fresh `Sidecar(...)` — anything not named here carries
    # through BY CONSTRUCTION (#239). Listing the fields to keep meant every
    # field left off the list was silently reset to its default on every
    # re-tag: `video_media` (so an album with a bonus DVD went Incomplete until
    # a later pass spent a MusicBrainz request re-learning it), and the
    # `purchase_unavailable` / `tracks_unavailable` surrenders, whose whole
    # purpose is to be permanent. A field added to the model later would have
    # joined them, silently, with no test to notice.
    base = sc or Sidecar()
    new = replace(
        base,
        store_url=store_url,
        added_at=base.added_at or datetime.now(UTC),
        mb_release_id=mbid,
        mb_match_candidate=None,  # cleared on tag — a suggestion, now acted on
        tagged_at=datetime.now(UTC),
        # Read off the release this tagging used, so the one fact the files
        # cannot carry (#206) is recorded at the moment it is known rather than
        # left for a reconcile pass to fetch again. Pure — no request (#237).
        video_media=mb_lookup.video_media_of(release),
    )
    sidecar_mod.write(album_path, new)
    # The files now carry what MusicBrainz says, so there is nothing left to be
    # waiting for and an Ignore has nothing to mute (#271). Left behind, it would
    # mute the NEXT divergence on this album if MusicBrainz happened not to have
    # moved in between — an external re-tag in Picard is the way that happens.
    #
    # Under BOTH ids, because a merge moves the album's identity right here: the
    # bookmark was written under the id the user was looking at, which is the one
    # that was requested rather than the one that came back.
    activity_store.unignore_update(mbid)
    if mbid != requested_mbid:
        activity_store.unignore_update(requested_mbid)
        _record_merge(album_path, requested_mbid, mbid)
    _claim_pending_by_store_url(store_url)


def _tag_as_redownloaded(
    album_path: Path, sc: Sidecar, cfg: config_mod.Config, tagger: Tagger
) -> str | None:
    """Tag a just-downloaded album as the release its archived copy was (#132).

    Returns a status string if it handled the album, or **None to fall through**
    to the ordinary store-URL resolution — which is every case this can't settle,
    so the fallback is always today's behaviour rather than a worse one.

    **Why carry the release at all.** Re-downloading says the *files* are wrong,
    not the match: the user is looking at an album they already accepted as this
    release and replacing its audio. Re-resolving the store URL from scratch
    re-opens a question they did not ask — it can land on a different release, or
    on none, turning a finished album into inbox work. Carrying it also keeps the
    album's id stable across the round trip, which is what lets its history span
    the archive (§2.4.1).

    This is `Confirm`'s semantics, not a guess: an explicit user decision to tag
    an album as a named release, so the match-confidence assessment is skipped
    exactly as Confirm skips it. What is NOT skipped is the tagger's own count
    guard, and that is the right remaining check — it asks whether the tagging is
    *representable*, which no assertion by the user can make true. Files that
    don't fit the release fall through, and the user meets the ordinary
    side-by-side with Confirm / Confirm as Incomplete on it.
    """
    item_id = sc.bandcamp.item_id if sc.bandcamp else None
    if item_id is None:
        return None
    carried = redownloads.take_match(item_id)
    if carried is None:
        return None  # not a re-download, or its match was already consumed
    mbid = carried.mb_release_id
    try:
        # `incomplete` is carried, not decided here. An album that was already
        # short keeps being taggable while short — refusing would cost it its
        # tags over a shortfall it had before the user pressed anything. One that
        # was COMPLETE gets the strict guard, so a truncated download is caught.
        _tag_with_release(album_path, mbid, cfg, tagger, incomplete=carried.incomplete)
    except tagger_mod.TagMismatchError as e:
        # The replacement doesn't fit the release the old copy was. Genuinely
        # possible and worth seeing: MusicBrainz may not have caught up with a
        # release the artist has grown, and extra files on disk are out of scope
        # for the tagger either way (§13.3). Fall through to the normal path,
        # which stashes a suggestion and puts it in the inbox with the tools.
        log.info("re-download of %s doesn't fit release %s: %s", album_path.name, mbid, e)
        activity.record(
            "Re-downloaded — the new files don't fit the release it was tagged as, "
            "so it needs a look",
            album_id=sidecar_mod.album_id_for(album_path),
            album_label=album_path.name,
            level=Level.WARNING,
        )
        return None
    except mb_lookup.MBError:
        # The release is gone from MusicBrainz, or MB is down. Same answer.
        log.exception("could not fetch release %s for the re-download of %s", mbid, album_path)
        return None
    activity.info(
        "Re-downloaded and tagged as the same MusicBrainz release",
        album_id=sidecar_mod.album_id_for(album_path),
        album_label=album_path.name,
    )
    return "tagged"


def _resolve_by_store_url(album_path: Path, cfg: config_mod.Config, tagger: Tagger) -> str:
    """Auto-resolve a sidecar's store_url against MusicBrainz.

    Used right after a Bandcamp download so a release that IS in MB goes
    straight to COMPLETE (Library) instead of waiting in NEEDS_MBID for a
    manual Recheck. Looks up the store URL, and on a match runs the normal
    match assessment: exact → tag (COMPLETE), approximate → stash candidate
    (NEEDS_MBID with a suggestion shown), no match → NEEDS_MBID. Never raises — returns a
    short status string for logging.

    A **re-download** (#132) short-circuits all of that: the album is tagged as
    the release its archived copy was, because that match is not what the user
    asked to revisit. Only if those files won't fit it does this run.
    """
    sc = sidecar_mod.read(album_path)
    if sc is None or not sc.store_url or sc.mb_release_id:
        return "skipped"  # nothing to resolve, or already resolved
    if (carried := _tag_as_redownloaded(album_path, sc, cfg, tagger)) is not None:
        return carried
    # The album's name for the feed's album column. No artist/title is available
    # on this path — neither MatchCandidate nor BandcampInfo carries one, and
    # _apply_best_match returns plain strings — so use the directory name, which
    # is what these messages already displayed and what bandcampsync put on disk.
    label = album_path.name
    try:
        mbids = mb_lookup.lookup_by_bandcamp_url(sc.store_url)
        if not mbids:
            # Ids are read back from disk AFTER each mutation below, never from
            # `sc` above: tagging drops temp_uid for the MBID, so the id captured
            # before the write is often already dead (#65).
            activity.record(
                "Synced — no MusicBrainz match yet",
                album_id=sidecar_mod.album_id_for(album_path),
                album_label=label,
            )
            return "no_match"
        status_str, _ = _apply_best_match(album_path, mbids, cfg, tagger)
        album_id = sidecar_mod.album_id_for(album_path)
        if status_str == "tagged":
            activity.info(
                "Auto-tagged from MusicBrainz after sync", album_id=album_id, album_label=label
            )
        else:
            activity.info(
                "Synced — MusicBrainz suggestion to review",
                album_id=album_id,
                album_label=label,
            )
        return status_str
    except Exception as e:
        log.warning("auto-resolve failed for %s: %s", album_path, e)
        return "error"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def _register_routes(app: FastAPI) -> None:

    @app.get("/", response_class=HTMLResponse)
    def index(
        request: Request,
        album: str | None = None,
        tab: str | None = None,
        page: int = 1,
        limit: int | None = None,
        anchor: int | None = None,
        # Named for the URL parameter it binds, `?filter=` — FastAPI takes the
        # query key from the parameter name, and the shadowed builtin is not one
        # this function has any use for.
        filter: str | None = None,
        q: str | None = None,
    ) -> Response:
        albums = _albums(request)
        # `?album=<id>` was the deep link before there was an album page (#65).
        # Now there is one, so redirect rather than opening a modal — links
        # already written into activity entries keep working, and land somewhere
        # better. Kept permanently: those entries are durable, so this URL will
        # be arriving for as long as the store holds them.
        #
        # An id that resolves to nothing still degrades to the normal page plus a
        # notice, never a whole-page 404 or a silently dead link.
        deep_link_missing = False
        if album:
            try:
                return RedirectResponse(
                    f"/album/{_find_album(request, album).id}",
                    status_code=status.HTTP_303_SEE_OTHER,
                )
            except HTTPException:
                deep_link_missing = True
        ctx = _ctx(
            request,
            albums=_inbox_albums(albums),
            total_albums=len(albums),
            sync_status=request.app.state.sync_runner.status(),
            deep_link_missing=deep_link_missing,
            # `?tab=` / `?page=` make the page the user was looking at a real URL
            # (#139): the Library pager pushes them, and the album page's Back link
            # carries them. Unrecognised tab → None, and the script falls back to
            # the remembered one, so a mangled link still lands somewhere sane.
            initial_tab=tab if tab in _INDEX_TABS else None,
            # The Library grid renders inline, so `?page=` / `?limit=` / `?filter=` /
            # `?q=` are honoured by the HTML itself rather than by a follow-up fetch
            # that would have to be told which page it's on.
            **_library_page_vars(
                albums,
                page,
                _library_limit(request, limit),
                anchor,
                _library_filter(filter),
                _library_search(q),
            ),
        )
        response = _templates(request).TemplateResponse(request, "index.html", ctx)
        _remember_library_limit(response, limit)
        return response

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
        # NEW (MBID-tagged) orphans get a sidecar; TAGGING albums (sidecar MBID
        # disagrees with the file tags — an external re-tag) get the file tags
        # adopted. Both are reconcile's job, so either kicks it.
        if any(
            a.path not in forgotten
            and ((a.state == AlbumState.NEW and a.has_tag_mbid) or a.state == AlbumState.TAGGING)
            for a in albums
        ):
            request.app.state.reconcile_runner.start()
        pending = pending_downloads.all_pending()
        # Awaited re-downloads clear by derivation (#132): anything whose purchase
        # the library can see again has come back, whether the sync we kicked
        # fetched it, a later one did, or the user unzipped the archive by hand.
        redownloads.prune(library_index.item_ids())
        pending_suggestions, surrender_suggestions = _reconcile_suggestions(
            albums, pending, request.app.state.cfg.paths.music_dir
        )
        ctx = _ctx(
            request,
            albums=_inbox_albums(albums),
            total_albums=len(albums),
            pending=pending,
            redownloading=redownloads.all_pending(),
            pending_suggestions=pending_suggestions,
            surrender_suggestions=surrender_suggestions,
            scan=request.app.state.scan_runner.status(),
            reconcile=reconcile_status,
            sync=request.app.state.sync_runner.status(),
        )
        return _templates(request).TemplateResponse(request, "tasks.html", ctx)

    # ----- Potential-download actions (in-memory; see pending_downloads) -----

    @app.post("/pending/{item_id}/skip", response_class=HTMLResponse)
    def pending_skip(request: Request, item_id: int) -> Response:
        cfg: config_mod.Config = request.app.state.cfg
        p = pending_downloads.get(item_id)
        label = f"{p.band} — {p.title}" if p else str(item_id)
        _append_ignore(cfg.ignores_file, item_id, label)
        pending_downloads.remove(item_id)
        activity.info(f"Won't download {label} — added to your Bandcamp ignores")
        request.state.skip_rescan = True  # only removed a pending; no album changed
        return _render_pending_section(request)

    @app.post("/pending/{item_id}/download", response_class=HTMLResponse)
    def pending_download(request: Request, item_id: int) -> Response:
        p = pending_downloads.get(item_id)
        label = f"{p.band} — {p.title}" if p else str(item_id)
        pending_downloads.approve(item_id)
        activity.record(f"Will download {label} on the next sync — click Sync")
        request.state.skip_rescan = True  # only approved a pending; no album changed
        return _render_pending_section(request)

    @app.get("/pending/{item_id}/match/results", response_class=HTMLResponse)
    def pending_match_results(request: Request, item_id: int, q: str = "") -> Response:
        # Live results for a card's inline "already in your library?" search. Empty
        # query → the seeded auto-match is already shown by the card, so return the
        # empty/hint state (the card's initial render carries the suggestion).
        ctx = _ctx(request, results=_search_albums(request, q), item_id=item_id, q=q)
        return _templates(request).TemplateResponse(
            request, "partials/_pending_match_results.html", ctx
        )

    @app.post("/pending/{item_id}/match", response_class=HTMLResponse)
    def pending_match_link(request: Request, item_id: int, album_id: str = Form(...)) -> Response:
        cfg: config_mod.Config = request.app.state.cfg
        p = pending_downloads.get(item_id)
        if p is None:
            return _render_pending_section(request)
        album = _find_album(request, album_id)
        # Only a match to an INBOX album (a surrender leaving Needs Link) changes
        # the inbox, so only then let the post-mutation middleware rescan. Matching
        # a Library album (the adoption case) leaves it COMPLETE — a rescan there is
        # pure overhead and just flickers the inbox while it runs.
        was_inbox = album.state not in _TERMINAL_STATES
        _link_pending_to_album(album, p)
        _append_ignore(cfg.ignores_file, item_id, f"{p.band} — {p.title}")
        pending_downloads.remove(item_id)
        # This one IS about an on-disk album (unlike the skip/approve entries
        # above, which concern a purchase with nothing on disk yet), so it links.
        # Id read back after _link_pending_to_album wrote the sidecar.
        album_id_now, album_label = _live_album_ref(album)
        activity.record(
            f"Linked purchase {p.band} — {p.title}",
            album_id=album_id_now,
            album_label=album_label,
        )
        request.state.skip_rescan = not was_inbox
        return _render_pending_section(request)

    @app.post("/ignored/{item_id}/restore", response_class=HTMLResponse)
    def ignored_restore(request: Request, item_id: int) -> Response:
        """Un-ignore a declined purchase so the next sync considers it again (#19).

        "Considers", not "offers": whether it resurfaces as a potential-download
        card or is fetched outright depends on link-only vs a full sync.

        Only ever removes from the USER region — the auto-managed region records
        what is already downloaded, and dropping an id from there would make the
        next sync re-download an album the user already has."""
        cfg: config_mod.Config = request.app.state.cfg
        entry = next(
            (i for i in _read_user_ignores(cfg.ignores_file) if i["item_id"] == item_id), None
        )
        if _remove_user_ignore(cfg.ignores_file, item_id):
            label = entry["label"] if entry else str(item_id)
            activity.info(f"Restored {label} — the next sync will consider it again")
        # Nothing on disk changed; a rescan here would just flicker the inbox.
        request.state.skip_rescan = True
        return _render_ignored_section(request)

    @app.get("/activity", response_class=HTMLResponse)
    def activity_feed(
        request: Request,
        offset: int = 0,
        limit: int = 50,
        audit: bool = False,
        have: str = "",
    ) -> Response:
        """One page of the Activity feed.

        `audit=1` interleaves the raw audit records. They're opt-in because they
        are forensics rather than outcomes — but they matter: rows written
        outside an action scope have no `action_id`, so no "what changed"
        disclosure can show them, and without this they'd be unreachable.

        `have` is the version the client already has on screen. When it matches,
        the answer is 204 and htmx leaves the DOM alone — the feed polls every 2s
        and was otherwise re-sending every entry each time, 154 KB of it on a
        large library, for a panel that usually hasn't changed (#118).
        """
        limit = max(1, min(limit, 200))  # clamp; defensive
        offset = max(0, offset)
        # The params are part of the token, so toggling "Technical detail" or
        # paging can't be mistaken for "nothing changed" — the client's `have` was
        # computed under the old params and won't match.
        try:
            feed_version = f"{activity_store.version()}-{audit:d}-{offset}-{limit}"
        except activity_store.StoreUnavailableError:
            # Can't tell whether anything changed, so don't claim it hasn't; fall
            # through and let the render below report the store as unavailable.
            feed_version = ""
        if have and have == feed_version:
            # 204: htmx's documented "do not swap". Deliberately not 304 — the
            # browser turns that back into a 200-from-cache for XHR, so htmx would
            # swap and idiomorph would still diff the whole feed.
            return Response(status_code=status.HTTP_204_NO_CONTENT)
        # An unreadable store must render as "can't read the feed", never as an
        # empty feed — the feed is re-polled every couple of seconds, so a silent
        # empty page is exactly how a broken store would go unnoticed for weeks
        # (#104). Caught rather than raised so the poll doesn't 500 on a loop.
        events: list[activity.Event] = []
        audit_detail: dict[str, list[activity_store.StoredEvent]] = {}
        has_more = False
        store_unavailable = False
        try:
            # The page is ALWAYS activity rows. Audit rows used to be interleaved
            # into it when `audit` was on, which sorted them by id — so one action
            # that wrote 970 rows (a first scan of a large library) filled the
            # whole page with identical `album.discovered` lines and pushed every
            # outcome off it. Detail now hangs off its entry instead (#123).
            #
            # Ask for one MORE than needed: its presence answers "is there another
            # page?" without a COUNT over a table that grows without bound and is
            # re-read every couple of seconds.
            page = activity.recent(limit + 1, offset=offset)
            has_more = len(page) > limit
            events = page[:limit]
            if audit:
                # The audit records behind each entry, fetched for the whole page
                # in ONE grouped query (#84) — per-entry lookups would be an N+1
                # across the page, re-polled every couple of seconds.
                audit_detail = activity_store.audit_by_action(
                    [e.action_id for e in events if e.action_id]
                )
                # Rows written outside any action have no entry to sit under, so
                # they get rows of their own — otherwise the UI would never show
                # them at all. Most writes ARE scoped (the HTTP middleware
                # wraps every mutating request; reconcile scopes per album), but
                # the post-sync surrender / unmatched-purchase pass runs on the
                # sync thread outside any request, and those rows are the only
                # record that a surrender happened.
                if events:
                    events = _merge_unscoped_audit(events, events[-1].ts)
        except activity_store.StoreUnavailableError:
            # Already logged with a traceback in the store. Drop any partial page:
            # half a feed presented as the whole feed is the same lie in miniature.
            events, audit_detail, has_more = [], {}, False
            store_unavailable = True
        ctx = _ctx(
            request,
            events=events,
            audit_detail=audit_detail,
            has_more=has_more,
            next_offset=(offset + limit) if has_more else None,
            limit=limit,
            include_audit=audit,
            is_first_page=(offset == 0),
            store_unavailable=store_unavailable,
            feed_version=feed_version,
        )
        return _templates(request).TemplateResponse(request, "partials/activity.html", ctx)

    @app.get("/about", response_class=HTMLResponse)
    def about_page(request: Request) -> Response:
        ctx = _ctx(request, app_version=_app_version(), git_sha=_git_sha(), credits=_credits())
        return _templates(request).TemplateResponse(request, "about.html", ctx)

    def _settings_ctx(request: Request, cfg: config_mod.Config, **extra: Any) -> dict[str, Any]:
        """The settings page's context, in one place.

        Three routes render this template — the page, a save, and a rejected
        save — and each used to assemble this by hand. A key added to only some
        of them is a template that renders on the paths you tested and raises on
        the one you didn't, which is exactly what adding the artwork figure did.
        """
        return _ctx(
            request,
            bandcamp_ok=_bandcamp_configured(cfg),
            sidecar_count=sidecar_mod.count_all(cfg.paths.music_dir),
            ignored=_read_user_ignores(cfg.ignores_file),
            artwork_usage=artwork_store.usage(),
            **extra,
        )

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request) -> Response:
        cfg: config_mod.Config = request.app.state.cfg
        return _templates(request).TemplateResponse(
            request, "settings.html", _settings_ctx(request, cfg)
        )

    @app.post("/settings/erase-sidecars", response_class=HTMLResponse)
    def erase_sidecars(request: Request) -> Response:
        cfg: config_mod.Config = request.app.state.cfg
        removed = sidecar_mod.delete_all(cfg.paths.music_dir)
        # "Start fresh" should also forget where the last sync left off, so the
        # next sync re-pages the whole Bandcamp collection rather than stopping
        # at bandcampsync's saved checkpoint. ignores.txt is deliberately left
        # alone — clearing it would re-download audio, which nuke is not about.
        state_cleared = _clear_bandcampsync_checkpoint(
            cfg.paths.music_dir, reason="sidecars erased — start fresh"
        )
        suffix = " · sync checkpoint reset" if state_cleared else ""
        # Drop the now-stale snapshot + counts and kick a fresh scan, so the inbox
        # shows the "Scanning…" screen (then the rebuilt inbox) when the user
        # returns to the main page — not the pre-erase cards lingering.
        live_counts.reset_from([])
        request.app.state.scan_runner.reset_and_rescan()
        activity.warning(
            f"Erased {removed} sidecar(s) — albums revert to tag-derived state{suffix}"
        )
        return _flash_response(
            "Sidecars erased",
            f"{removed} removed — audio untouched; albums re-derive on next scan{suffix}",
            level=Level.WARNING,
        )

    @app.post("/settings", response_class=HTMLResponse)
    def settings_save(
        request: Request,
        download_format: str = Form(...),
        max_downloads_per_sync: int = Form(...),
        user_agent: str = Form(...),
        cover_art_size: str = Form(...),
        gardener_level: str = Form(...),
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
            new_gardener = config_mod.GardenerConfig.model_validate(
                {"level": gardener_level.strip()}
            )
            new_cfg = cfg.model_copy(
                update={
                    "bandcamp": new_bandcamp,
                    "musicbrainz": new_mb,
                    "cover_art": new_cover,
                    "gardener": new_gardener,
                    "log_level": log_level.strip().lower(),
                }
            )
        except (PydanticValidationError, ValueError) as e:
            return _templates(request).TemplateResponse(
                request, "settings.html", _settings_ctx(request, cfg, error=str(e))
            )

        config_mod.write_settings(
            cfg.paths.config_dir,
            {
                "bandcamp.download_format": new_bandcamp.download_format,
                "bandcamp.max_downloads_per_sync": new_bandcamp.max_downloads_per_sync,
                "musicbrainz.user_agent": new_mb.user_agent,
                "cover_art.size": new_cover.size,
                "gardener.level": new_gardener.level,
                "log_level": new_cfg.log_level,
            },
        )
        # Apply live — code reads these from app.state.cfg at use-time. The
        # MB user-agent is applied at startup, so re-configure it now too.
        # The gardener's level needs nothing further: its timer is always
        # running and reads the level off this config on its next tick (#312).
        request.app.state.cfg = new_cfg
        mb_lookup.configure(new_cfg.musicbrainz.user_agent)
        activity.info("Settings updated")

        return _templates(request).TemplateResponse(
            request, "settings.html", _settings_ctx(request, new_cfg, saved=True)
        )

    @app.post("/settings/update-check", response_class=HTMLResponse)
    def run_update_check_now(request: Request) -> Response:
        """Run the background pass now, instead of waiting for the next tick.

        `run_periodically` fires one full interval after startup and never at
        startup, so turning the check on buys an interval of a library that looks
        exactly as it did — which reads as a setting that didn't take (#312).
        This is the escape hatch from that wait, and the only way to see the pass
        work on demand.

        **It runs one ordinary tick, not a bigger one** (#349). A tick is now a
        share of `gardener.SWEEP_WINDOW` — a couple of albums rather than a
        hundred — so the press buys the ten minutes to the next one and not a
        sweep. Deliberately: giving the button its own larger budget would put
        the burst #349 removed back into the app at the one moment somebody is
        certainly sitting in front of it, and the honest reading of "check now"
        is that the check starts now, which it does.

        Not a mutation: it starts a read-only pass on a worker thread and
        returns at once, so there is nothing for the inbox or the library to
        refresh yet (`tasks_changed=False`). What it finds arrives the way the
        pass's findings always arrive — the Update badge on a tile, and the
        Library's **Update available** filter.

        The outcome goes back out of band as well as in the flash, because on
        this page the flash alone is silence: the status bar's JS is defined in
        index.html, so a `harmonist-status` event fired on /settings has nobody
        listening for it. The OOB fragment answers beside the button that was
        pressed, which is where the answer belongs anyway.
        """
        state = request.app.state
        reason = _update_check_if_idle(
            request.app, state.sync_runner, state.reconcile_runner, state.scan_runner
        )
        if reason is not None:
            return _flash_response(
                "Update check not started",
                reason,
                level=Level.WARNING,
                tasks_changed=False,
                # No feed entry: nothing happened, the press has its answer on
                # screen, and the one decline that IS news — a pass still
                # running — already reaches the feed through its mirrored
                # `log.warning`. Recording here too would post it twice (#258).
                record_activity=False,
                oob=_update_check_oob(request, f"Not started — {reason}.", ok=False),
            )
        return _flash_response(
            "Update check started",
            "asking MusicBrainz about the albums due; anything it finds appears "
            "under the Library's Update available filter",
            tasks_changed=False,
            oob=_update_check_oob(
                request,
                "Checking now — anything it finds appears under the Library's "
                "Update available filter.",
                ok=True,
            ),
        )

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
                # Single source of truth for the inbox/library counts — kept live
                # by transitions (live_counts.move) and reset from each scan.
                "counts": live_counts.to_status(),
                # Potential downloads awaiting a decision (in-memory, from the last
                # link-only sync). They need attention, so they count toward inbox.
                "pending": pending_downloads.count(),
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
            level=Level.WARNING,
            tasks_changed=False,
        )

    @app.post("/sync", response_class=HTMLResponse)
    def start_sync(
        request: Request,
        link_only: bool | None = Form(None),
        max_downloads: int | None = Form(None),
        from_popover: bool = Form(False),
    ) -> Response:
        # Backstop the UI gating: don't kick a sync while a reconcile pass is
        # in flight (it's mutating sidecars / the inbox). The button is
        # disabled client-side, but a stale page or the race window before the
        # next /status poll could still POST here.
        if request.app.state.reconcile_runner.is_running:
            return _flash_response(
                "Sync unavailable",
                "reconciling — try again in a moment",
                level=Level.WARNING,
                tasks_changed=False,
                status_code=status.HTTP_409_CONFLICT,
            )
        # Same backstop for the cold-start scan: until it lands there's no
        # snapshot, so a sync would run against an inbox/library it can't see.
        # `seq == 0` narrows this to the FIRST scan (the counter only increments
        # on a completed one), leaving later rescans free to overlap a sync.
        scan = request.app.state.scan_runner.status()
        if scan.get("state") == "scanning" and scan.get("seq") == 0:
            return _flash_response(
                "Sync unavailable",
                "still scanning your library — try again when it finishes",
                level=Level.WARNING,
                tasks_changed=False,
                status_code=status.HTTP_409_CONFLICT,
            )
        # Sync-popover knobs. max-downloads persists (it's the same setting as the
        # Settings page); link-only is a one-shot override for THIS sync.
        if max_downloads is not None and max_downloads >= 0:
            _persist_max_downloads(request, max_downloads)
        runner = request.app.state.sync_runner
        # Only the popover sends an explicit link-only choice (its checkbox, present
        # or absent). The plain Sync button sends nothing → None → auto-detect.
        runner.link_only_override = bool(link_only) if from_popover else None
        try:
            runner.start()
        except AlreadyRunningError:
            return _flash_response(
                "Sync busy",
                "already running",
                level=Level.WARNING,
                tasks_changed=False,
                status_code=status.HTTP_409_CONFLICT,
            )
        # No activity entry here: the runner writes the single sync-start line
        # once it knows link-only vs full (#101). This is just the status flash.
        return _flash_response(
            "Sync started", "watch the inbox", tasks_changed=False, record_activity=False
        )

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
    def library(
        request: Request,
        page: int = 1,
        limit: int | None = None,
        anchor: int | None = None,
        # Named for the URL parameter it binds, `?filter=` — FastAPI takes the
        # query key from the parameter name, and the shadowed builtin is not one
        # this function has any use for.
        filter: str | None = None,
        q: str | None = None,
    ) -> Response:
        """One page of terminal albums (Complete + Incomplete), newest tagged first.

        Paged rather than accumulated (#139). The position is a *parameter*, not
        DOM built up by clicking Load more, which is what lets the pager push it
        onto the browser's history and the album page's Back link carry it — so a
        Library → album → Library round-trip lands where it started instead of
        snapping back to the newest page.

        `?limit=` is the page size and belongs to the same addressable view (#144);
        `?anchor=` is how the size control asks to keep the album at the top of the
        screen on screen across a size change.

        `?filter=` narrows the grid to terminal albums that are nonetheless wrong —
        incomplete, partially tagged, missing artwork (#174). Unlike `?limit=` it is
        deliberately NOT remembered: a page size is a standing preference, whereas a
        filter is a question asked once, and one silently restored weeks later reads
        as "my library has shrunk".

        `?q=` searches artist and title (#180). It composes with `?filter=` rather
        than replacing it, and is remembered exactly as little — for the same
        reason.
        """
        vars_ = _library_page_vars(
            _albums(request),
            page,
            _library_limit(request, limit),
            anchor,
            _library_filter(filter),
            _library_search(q),
        )
        response = _templates(request).TemplateResponse(
            request, "partials/library_page.html", _ctx(request, **vars_)
        )
        _remember_library_limit(response, limit)
        if anchor is not None or request.headers.get(_LIBRARY_PUSH_HEADER):
            # Only the server knows which page the anchor resolved to, and only the
            # server knows the whole view the search form just asked for, so the
            # address bar is corrected from here rather than by an hx-push-url the
            # control would have to guess before the answer existed.
            #
            # Confined to those two cases deliberately: this grid re-requests itself
            # on every `tasks-changed`, and a push per background refresh would bury
            # the Back button under a stack of identical entries. Every other control
            # here is a link that already spells its own URL.
            response.headers["HX-Push-Url"] = _library_index_url(
                vars_["page"], vars_["limit"], vars_["filter"], vars_["q"]
            )
        return response

    @app.get("/album/{album_id}", response_class=HTMLResponse)
    def album_page(
        request: Request,
        album_id: str,
        from_page: int = 1,
        from_filter: str | None = None,
        from_q: str | None = None,
    ) -> Response:
        """The standalone album page (#103) — full tracklist plus the album's
        history, neither of which fits a viewport-constrained dialog.

        `?from_page=` is the Library page the tile was clicked on, so Back can
        return there (#139). It is a hint, not identity: absent (a bookmark, a
        link from Activity) simply means page 1, and the page renders the same
        either way.

        `?from_filter=` is the same idea for the Library's filter (#174), and it
        has to be carried explicitly: the page size survives this round trip in a
        cookie, but a filter is deliberately not remembered anywhere, so without
        this the trip out to an album and back would silently drop it — landing the
        reader in the whole library, one album into a list they were working
        through. Validated here too; this value reaches an href.

        `?from_q=` carries the search the same way and for the same reason (#180) —
        an album opened from a search of "aphex" goes Back to that search, not to
        the whole library.

        Served for a stale id too: `_find_album` resolves one forward through the
        alias chain, so a link written before the album was re-identified still
        lands here rather than 404-ing.

        The album's own directories are re-read before rendering (#151), so what
        this page shows is what is on disk now — not what the last background
        scan happened to see, which on a network mount could be startup.
        """
        album = _refreshed_from_disk(request, _find_album(request, album_id))
        # The tracklist and actions don't depend on the store, so a broken store
        # must not take the whole page down — but the history section has to say
        # it couldn't read rather than fall through to "Nothing recorded for this
        # album yet", which is the confident lie #104 is about.
        history: list[activity_store.StoredEvent] = []
        history_unavailable = False
        tag_changes: dict[int, tuple[tag_history.FieldChange, ...]] = {}
        restorable: set[int] = set()
        revertable: set[int] = set()
        try:
            # Keyed on the album's CURRENT id — album_history unions backwards
            # over the chain from there, so passing the (possibly stale) URL id
            # would find only the tail of its own history.
            history = activity_store.album_history(album.id)
            # What each tagging actually changed, field by field (#86). ONE
            # query for the whole page rather than one per row: an album
            # re-tagged a few times has a `tag.track` row per file per tagging,
            # and this table only grows.
            detail = activity_store.tag_changes_for([e.id for e in history])
            tag_changes = tag_history.group_by_action(history, detail)
            # Only offer Undo where the images are actually still there (#131).
            restorable = _restorable_anchors(history, detail)
            # And where the tagging changed a tag this build can put back (#157).
            revertable = _revertable_anchors(history, detail)
        except activity_store.StoreUnavailableError:
            history_unavailable = True  # already logged with a traceback in the store
        ctx = _ctx(
            request,
            album=album,
            folders=_album_folders(album, request.app.state.cfg.paths.music_dir),
            history=history,
            history_unavailable=history_unavailable,
            tag_changes=tag_changes,
            restorable=restorable,
            revertable=revertable,
            # When this release was last read from MusicBrainz, for the panel's
            # "Checked" date (#355). A local SQLite read with no MusicBrainz call
            # in it, so the panel can state it as the page is built rather than
            # waiting on the /compare fetch that lands afterwards — which is what
            # let this move out of the note, whose every other value does need
            # that fetch.
            #
            # None when nothing has ever been read, and on an untagged album
            # there is no release to have read: both render no line at all,
            # rather than a claim about a check that never happened. No fallback
            # to `now` here, unlike /compare — that response IS a read, and this
            # one is not.
            mb_read_at=(
                mb_cache.fetched_at(album.sidecar.mb_release_id)
                if album.sidecar and album.sidecar.mb_release_id
                else None
            ),
            # The inbox's "you may already own this" pairing, for the action
            # blocks this page now renders (#150). Without it a surrendered
            # album's page would show the no-purchase panel while its inbox card
            # offers the Link — the same album, two answers, decided by which
            # surface you happened to open.
            #
            # In-memory and O(albums + potential downloads), against a page that
            # has already re-read the album's folders from disk. `_inbox_albums`
            # is not applied: this album's own suggestion is wanted whatever
            # group it would or wouldn't be filed under.
            surrender_suggestions=_reconcile_suggestions(
                _albums(request),
                pending_downloads.all_pending(),
                request.app.state.cfg.paths.music_dir,
            )[1],
            from_page=max(1, from_page),
            from_filter=_library_filter(from_filter),
            from_q=_library_search(from_q),
        )
        return _templates(request).TemplateResponse(request, "album.html", ctx)

    @app.get("/library/{album_id}/compare", response_class=HTMLResponse)
    def library_compare(request: Request, album_id: str, reread: bool = False) -> Response:
        """On-demand disk-vs-MB comparison for a tagged album — the per-field tag
        comparison (#106) and the per-track one, from a SINGLE MusicBrainz fetch.

        Both halves need the same release, so they share one request rather than
        costing two against a 1-req/sec budget (review-gate item 6).

        Served from the release cache (#127), so opening album pages no longer
        spends a rate-limited request each. `reread=True` is the user pressing
        "read again" on the staleness line: it forces a live fetch and refreshes
        the stored row. That control is what keeps a cached comparison from
        being a dead end — the user can always see how old the answer is, and
        always get a newer one, without touching a config file.
        """
        album = _find_album(request, album_id)
        sc = album.sidecar
        if sc is None or not sc.mb_release_id:
            return HTMLResponse(
                '<p class="text-2xs text-muted italic mt-2">'
                "No MusicBrainz release to compare against.</p>"
            )
        # Around the whole thing (#300). The MusicBrainz fetch and the tag read
        # carry their own guards, but this is the one that catches a page that
        # was slow without any single phase crossing its threshold — which is
        # the shape of #299, where the page stalls and nothing says why.
        with timing.warn_if_slow(
            "album comparison", _SLOW_COMPARE, album=album.path, mbid=sc.mb_release_id
        ):
            return _compare_response(request, album, sc.mb_release_id, reread=reread)

    def _compare_response(request: Request, album: Album, mbid: str, *, reread: bool) -> Response:
        """The body of `library_compare`, split out only so the timing guard
        above can wrap it — every `return` in here is a way the comparison can
        end, and a guard that covered some of them would report the fast paths
        and stay silent on whichever one was slow.

        Takes the release id rather than the sidecar: the caller has already
        established it is present, and passing the narrowed value states that
        precondition in the signature instead of leaving it as something the
        two functions have to agree about silently."""
        try:
            release = mb_cache.fetch_release(mbid, max_age=mb_cache.FRESH if reread else None)
        except mb_lookup.ReleaseGoneError:
            # A 404 is an ANSWER, not a failure: the release has been deleted
            # from MusicBrainz, and no amount of retrying will bring it back.
            # Saying "couldn't fetch" would invite the user to try again forever.
            #
            # This is the only place that finds out, so it carries the whole
            # response to it (#210): the tags here, the tracklist and a banner
            # out-of-band, and a disabled Re-tag out-of-band — nothing behind
            # that button can succeed. The banner ASKS rather than sending the
            # album to the inbox: the user may have opened this page for
            # something else.
            #
            # Both panels still render, from the files alone (#228). MusicBrainz
            # is gone; the tags are not, and they are what the user needs to go
            # find the replacement release.
            log.warning(
                "album %s names release %s, which MusicBrainz no longer has",
                album.path,
                mbid,
            )
            comparison, tracks = _album_disk_view(album.path, album.folders)
            return _templates(request).TemplateResponse(
                request,
                "partials/_release_gone.html",
                # No release, so no names to put on the ids the files carry
                # (#298) and no credits to break an artist phrase into (#309) —
                # they render as raw MBIDs, still linked, and as the flat strings
                # the files carry, which is what the user searches MusicBrainz
                # with to find the replacement.
                _ctx(
                    request,
                    album=album,
                    comparison=comparison,
                    tracklist=tracks,
                    mb_names={},
                    mb_credits={},
                    # …and no release events either (#329): MusicBrainz has
                    # deleted the release, so there is nothing to say about
                    # where it came out beyond the country the files carry.
                    mb_release_events=(),
                ),
            )
        except mb_lookup.MBError as e:
            # A template rather than a bare string so the failure reaches BOTH
            # halves of the page (#228): the in-band note here settled Tags, and
            # Tracks was left on its "checking…" placeholder forever, reading as
            # a request still in flight when this one had already failed.
            #
            # Jinja autoescapes `error`, which is what keeps upstream
            # musicbrainzngs text — off-box, unsanitised — out of the DOM as
            # markup.
            return _templates(request).TemplateResponse(
                request,
                "partials/_compare_failed.html",
                _ctx(request, album=album, error=str(e)),
            )
        # No `assess_match` here any more (#135). It re-opened every file in the
        # album for a duration and a title that `_album_comparison` had just
        # read, to produce a release-fit verdict that is stale news on an album
        # already linked to that release — the tracklist now says what actually
        # differs, track by track. It stays where it earns its keep: behind the
        # Needs MBID suggestion card, deciding whether to link at all.
        comparison, tracks = _album_comparison(album.path, release, album.folders)
        # Opening an album is a look at exactly the question the Library filter
        # asks, against a release already in hand — so answer it here too and
        # record it on the snapshot's Album (#287). That is what lets the filter
        # be useful before the background pass exists (#270): browsing fills it
        # in, and a re-tag taken from this very page clears the flag on the next
        # visit, because the plan then comes back empty.
        #
        # Costs one read per file on top of the comparison, which is affordable
        # on a page the user asked for and is exactly why the Library's own
        # render cannot do this for every tile.
        plan = gardener.refresh_flag(album, release).plan
        ctx = _ctx(
            request,
            album=album,
            # Read AFTER `refresh_flag`, which is what sets `album.mb_version` —
            # the thing an ignore is compared against. Reading it before would
            # ask whether the ignore holds for the payload we had a moment ago.
            update_ignored=gardener.is_ignored(album, _ignored_updates()),
            comparison=comparison,
            tracklist=tracks,
            absent_media=_absent_media_summary(album, release),
            shape_mismatch=_shape_mismatch(album, release),
            # What the note beside the hexagon reports — now a real timestamp
            # rather than an assumed "just now" (#127). The release may have come
            # from the cache, in which case this is when it was actually read,
            # which is the whole point: a user comparing their tags against
            # MusicBrainz needs to know whether an edit they just made upstream
            # is in what they are looking at.
            #
            # Falls back to now only when the row is somehow missing (a store
            # that failed the write it was just asked for) — "read just now" is
            # then still true of the fetch that produced this response.
            mb_read_at=mb_cache.fetched_at(mbid) or datetime.now(UTC),
            # What the panel's MusicBrainz ids are called (#298). Off the same
            # release the comparison is built from, so an id and the name shown
            # for it can never come from two different payloads — which is the
            # one way this could put an artist's name over another artist's id.
            mb_names=tagger_mod.mbid_names(release),
            # How MusicBrainz spells each artist credit on this release, keyed by
            # the phrase it renders as (#309), so "A feat. B" can be drawn as the
            # two artists it names rather than as one flat string. Off the same
            # release as the comparison, for the reason `mb_names` is: the phrase
            # and its parts must come from one payload, or the page shows one
            # artist's name over another artist's link.
            mb_credits=tagger_mod.artist_credits(release),
            # Where and when MusicBrainz says this release came out (#329). The
            # Country row shows one code because that is what the tag carries —
            # Picard's `releasecountry` is a scalar too — and a release issued
            # in three countries then reads as MusicBrainz knowing only one.
            #
            # Page context rather than part of the comparison, alongside
            # `mb_names` and `mb_credits` and for the same reason: `compare` is
            # pure functions over values and never sees a release payload.
            mb_release_events=tagger_mod.release_events(release),
            # What a re-tag would change in the fields nothing else on this page
            # shows (#291, narrowed by #297, narrowed again by #309). Free:
            # `refresh_flag` just built this plan to set the flag, so rendering
            # it costs no further reads.
            #
            # Scoped rather than complete, and that is the whole point. The box
            # was written when the panel compared nine album fields out of the
            # thirty the plan covers; #295 widened the panel to all of them, and
            # #309 gave the tracklist columns for the per-track ones that
            # actually differ. What is left is the overflow — the fields that
            # earned a column but did not fit the cap — which has nowhere else
            # on this page to appear.
            #
            # The two halves of the scope come from the two surfaces themselves:
            # the panel's rows are a module constant because they are the same
            # for every album, and the tracklist's are a property of THIS
            # comparison because its columns are earned. Union, not either — a
            # field the box may show is one neither surface did.
            update_changes=(
                tuple(
                    c
                    for c in tag_history.from_plan(
                        plan, album.path, album_files.for_paths(album.folders)
                    )
                    if c.field not in compare.PANEL_FIELDS | tracks.shown_fields
                )
                if plan is not None and plan.changes
                else ()
            ),
        )
        return _templates(request).TemplateResponse(request, "partials/library_compare.html", ctx)

    @app.post("/library/{album_id}/unlink", response_class=HTMLResponse)
    def library_unlink(request: Request, album_id: str, forget_url: bool = Form(False)) -> Response:
        """Undo a Bandcamp link. Two modes:

        - **Unlink** (`forget_url=False`): clear the purchase `item_id` but keep the
          `store_url`, so the album reverts to Needs Link and a later sync/manual
          match can re-link it. For temporarily undoing a *correct* link.
        - **Wrong match** (`forget_url=True`): also drop the `store_url` (and the
          bandcamp block), so no future sync re-links it by slug and adoption can't
          re-adopt it. The album stays correctly tagged in the Library (COMPLETE);
          the freed purchase re-surfaces as unmatched for correct handling. Without
          this, keeping the wrong `store_url` would just re-link the same wrong
          purchase on the next sync.

        The activity entry is `_flash_response`'s, and only its — recording one
        here as well wrote the feed twice for one press (#342). The purchase id
        being dropped isn't lost with it: `_audit_sidecar_change` diffs `item_id`
        (and `store_url`) as `old->new` under this request's action id."""
        album = _find_album(request, album_id)
        sc = album.sidecar
        if sc is None or sc.bandcamp is None or sc.bandcamp.item_id is None:
            return _flash_response(
                "Nothing to unlink",
                "not linked to a Bandcamp purchase",
                level=Level.WARNING,
                album=album,
            )
        if forget_url:
            new_sc = replace(sc, store_url=None, bandcamp=None)
            dest = "Library (URL forgotten)"
        else:
            new_sc = replace(sc, bandcamp=BandcampInfo(item_id=None, band_id=sc.bandcamp.band_id))
            dest = "Needs Link"
        sidecar_mod.write(album.path, new_sc)
        request.app.state.scan_runner.request_scan()
        return _flash_response("Unlinked", f"now {dest}", album=album)

    @app.post("/library/{album_id}/rematch", response_class=HTMLResponse)
    def library_rematch(request: Request, album_id: str) -> Response:
        """Mark the MusicBrainz match as wrong, deriving the album back to Needs
        MBID — where the manual search / store-URL-candidate tools let the user
        pick the correct release and re-tag.

        Unlinks through `sidecar.unlink`, shared with the undo that removes a
        release id (#158/#166): it mints the path-derived `temp_uid`, audits the
        identity change, and keeps the Bandcamp link (`store_url` / `item_id`).
        No candidate — re-offering the release the user just called wrong would
        undo their own judgement, which is the one way this differs from the undo.

        The activity entry is `_flash_response`'s, and only its — recording one
        here as well wrote the feed twice for one press (#342). The release id
        being cleared isn't lost with it: the `sidecar.unlink` write above audits
        `mbid` as `old->new` under this request's action id.

        The on-disk tags are left untouched until the user re-tags.
        Non-destructive."""
        album = _find_album(request, album_id)
        sc = album.sidecar
        if sc is None or not sc.mb_release_id:
            return _flash_response(
                "Nothing to re-match",
                "no MusicBrainz release to replace",
                level=Level.WARNING,
                album=album,
            )
        sidecar_mod.unlink(album.path, sc)
        request.app.state.scan_runner.request_scan()
        return _flash_response(
            "MB match cleared",
            "→ Needs MBID — pick the correct release",
            album=album,
        )

    @app.post("/retag/{album_id}", response_class=HTMLResponse)
    def retag(
        request: Request,
        album_id: str,
        overwrite_art: bool = Form(False),
        accept_short: bool = Form(False),
    ) -> Response:
        """Re-tag a Library album from the MusicBrainz release it names.

        `accept_short=True` is the user's answer to the offer this endpoint makes
        when the guard refuses (#252) — tag the files against a release that lists
        more tracks than are on disk. It is a decision, so it arrives from a
        control the user pressed; the endpoint never infers it from the counts.
        """
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
                # An album already known to be missing tracks has to be
                # re-tagged in incomplete mode, or the tagger's file-count guard
                # refuses it and re-tagging is impossible for exactly the albums
                # a MusicBrainz correction is most likely to affect (#133).
                #
                # Keyed on the album's DERIVED state (#195), which is the honest
                # form of what this always meant. It used to test
                # `track_count_expected is not None` and call that "the persisted
                # confirmation" — but `_tag_with_release` wrote that field on
                # every tag, incomplete or not, so the test was true for every
                # album Harmonist had ever tagged and distinguished nothing. The
                # state does distinguish: a COMPLETE album still gets
                # `incomplete=False` and the §15.3 guard still applies to it.
                #
                # `accept_short` is the residual half (#252): the state answers
                # "were the files short of what MusicBrainz said when they were
                # tagged", and the guard asks "are they short of what it says
                # now". They diverge on exactly the album a MusicBrainz
                # correction has grown, and no derived fact can settle that — so
                # the refusal below turns into a question, and this carries the
                # user's answer to it.
                incomplete=accept_short or album.state == AlbumState.INCOMPLETE,
                overwrite_art=overwrite_art,
                paths=album.folders,
            )
        except mb_lookup.ReleaseGoneError:
            # Not a failure to report as one: MusicBrainz has deleted the release
            # this album names, so re-tagging from it is impossible now and will
            # stay impossible. "Re-tag failed: HTTP Error 404" reads as something
            # to retry; this says what is actually true and names the way out
            # (#194).
            log.warning("retag: MusicBrainz no longer has release %s", sc.mb_release_id)
            return _flash_response(
                "That release is gone from MusicBrainz",
                "use Wrong MusicBrainz match to pick the current one",
                level=Level.WARNING,
                tasks_changed=False,
                album=album,
            )
        except tagger_mod.TagMismatchError as e:
            if not e.short:
                # More files than the release has tracks. Out of scope for the
                # tagger in *both* modes (§15.3), so there is no decision to
                # offer — it stays an error, as it was.
                log.exception("retag failed", extra=_LOG_ONLY)
                return _flash_response(
                    "Re-tag failed", str(e), level=Level.ERROR, tasks_changed=False, album=album
                )
            # Not a failure to report as one (#252). Nothing was written and the
            # guard did its job; what the user needs is the two counts and the
            # one control that resolves them, which rides back out of band.
            log.info("retag: %s", e)
            return _flash_response(
                "MusicBrainz lists more tracks than you have",
                f"{e.tracks} there, {e.files} here — re-tag as incomplete to take its tags anyway",
                level=Level.WARNING,
                tasks_changed=False,
                album=album,
                oob=_retag_short_oob(
                    request, album, files=e.files, tracks=e.tracks, overwrite_art=overwrite_art
                ),
            )
        except Exception as e:
            log.exception("retag failed", extra=_LOG_ONLY)
            return _flash_response(
                "Re-tag failed", str(e), level=Level.ERROR, tasks_changed=False, album=album
            )
        # Say what the re-tag DID, not only that it ran. A short re-tag has just
        # written a longer tracklist's totals into the files, so the album derives
        # INCOMPLETE from here (§13.3) and starts showing a shortfall badge — a
        # visible change to how it is listed, which the entry should name rather
        # than leave to be discovered. It stays in the Library either way.
        detail_parts = [
            *(["now listed as incomplete"] if accept_short else []),
            *(["artwork replaced"] if overwrite_art else []),
        ]
        details = ", ".join(detail_parts) or None
        # A re-tag can move the album's state — the totals it just wrote are what
        # COMPLETE/INCOMPLETE is derived from (#195) — and the album page reloads
        # itself on `album-retagged`. Refresh the snapshot in this request, or that
        # reload races the async rescan and re-renders the state the re-tag has
        # just replaced: a short album accepted as incomplete comes back still
        # claiming to be complete (#252).
        #
        # NOT paired with `skip_rescan`, unlike the other single-album mutations:
        # `refresh_now` patches the snapshot only, and a moved state also has to
        # reach `live_counts`, which the full rescan is what resets.
        runner = request.app.state.scan_runner
        if runner.is_engaged():
            runner.refresh_now()
        # Reload the open detail modal so its disk-vs-MB comparison + metadata
        # reflect the just-written tags (tasks-changed only refreshes the tiles).
        return _flash_response(
            "Re-tagged", details, extra_triggers={"album-retagged": True}, album=album
        )

    @app.post("/artwork/restore/{album_id}", response_class=HTMLResponse)
    def restore_artwork(request: Request, album_id: str, event_id: int = Form(...)) -> Response:
        """Put back the artwork one tagging replaced (#131).

        `event_id` names the history row the change is shown under, not the
        images — so the button undoes exactly the change the user just read,
        and the plan is rebuilt server-side from the stored records rather than
        trusted from the form. A client cannot ask for arbitrary files or
        digests; it can only name a row of this album's own history.
        """
        album = _find_album(request, album_id)
        try:
            plan = _artwork_plan(album, event_id)
        except activity_store.StoreUnavailableError:
            return _flash_response(
                "Couldn't undo",
                "this album's history can't be read right now",
                level=Level.ERROR,
                tasks_changed=False,
                album=album,
            )
        if not plan:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, "no artwork change to undo on that history entry"
            )
        try:
            restored = tagger_mod.restore_artwork(album.path, plan)
        except tagger_mod.ArtworkUnavailableError as e:
            # Expected, not exceptional: the store evicts oldest-first, so an
            # old enough change is genuinely unrevertable and saying so plainly
            # beats a stack trace.
            return _flash_response(
                "Couldn't undo", str(e), level=Level.ERROR, tasks_changed=False, album=album
            )
        except Exception as e:
            log.exception("artwork restore failed", extra=_LOG_ONLY)
            return _flash_response(
                "Couldn't undo", str(e), level=Level.ERROR, tasks_changed=False, album=album
            )
        if not restored:
            return _flash_response(
                "Nothing to undo", "the artwork already matches", tasks_changed=False, album=album
            )
        return _flash_response(
            "Artwork restored",
            f"{restored} file{'s' if restored != 1 else ''}",
            extra_triggers={"album-retagged": True},
            album=album,
        )

    @app.post("/tags/restore/{album_id}", response_class=HTMLResponse)
    def restore_tags(request: Request, album_id: str, event_id: int = Form(...)) -> Response:
        """Put back the tags one tagging changed (#157).

        `event_id` names the history row the change is shown under, not the
        fields — so the button undoes exactly the tagging the user just read,
        and the plan is rebuilt server-side from the stored records rather than
        trusted from the form. A client cannot name a file, a field or a value;
        only a row of this album's own history. Same contract as the artwork
        restore beside it.
        """
        album = _find_album(request, album_id)
        try:
            plan = _revert_plan(album, event_id)
        except activity_store.StoreUnavailableError:
            return _flash_response(
                "Couldn't undo",
                "this album's history can't be read right now",
                level=Level.ERROR,
                tasks_changed=False,
                album=album,
            )
        if not plan:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, "no tag change to undo on that history entry"
            )
        try:
            outcome = tagger_mod.revert_tags(album.path, plan)
        except tagger_mod.RevertUnavailableError as e:
            # Expected, not exceptional: files get renamed and deleted, and
            # saying so plainly beats a stack trace.
            return _flash_response(
                "Couldn't undo", str(e), level=Level.ERROR, tasks_changed=False, album=album
            )
        except Exception as e:
            log.exception("tag revert failed", extra=_LOG_ONLY)
            return _flash_response(
                "Couldn't undo", str(e), level=Level.ERROR, tasks_changed=False, album=album
            )
        if not outcome.changed:
            return _flash_response(
                "Nothing to undo",
                "these tags have changed since that tagging"
                if outcome.stale
                else "these tags are already back",
                tasks_changed=False,
                album=album,
                # A no-op writes no history line — the same "silence is the
                # feature" rule a re-tag that changed nothing follows (#86).
                # Without this, undoing twice leaves a permanent entry whose
                # whole content is that nothing happened, and it does it by
                # reciting every field in the album.
                record_activity=False,
            )
        unlinked = _unlink_after_revert(album, outcome)
        if unlinked:
            request.app.state.scan_runner.request_scan()
        return _flash_response(
            "Tags put back",
            _revert_detail(outcome, unlinked=unlinked),
            extra_triggers={"album-retagged": True},
            album=album,
        )

    @app.post("/library/{album_id}/redownload", response_class=HTMLResponse)
    def redownload(request: Request, album_id: str) -> Response:
        """Archive the album's files off disk and let the next sync fetch it again
        (#132) — a format upgrade, or a release the artist has since added to.

        Four things have to be undone for a sync to re-fetch a purchase it has
        already got, and the order matters:

        1. **Archive and delete the directories** (`archive.archive_and_remove`,
           which verifies the zip before removing anything). This is what clears
           `library_index`, the `bandcamp_item_id.txt` marker and the on-disk
           `store_url` — the three things `sync_item` short-circuits on.
        2. **Un-ignore the purchase**, including bandcampsync's auto-managed
           region. Done here rather than during the sync because bandcampsync
           snapshots the file at startup and discards concurrent edits.
        3. **Approve the download**, which is the existing flag that gets an item
           past both link-only mode and the per-sync cap.
        4. **Clear the collection checkpoint**, or an incremental sync starts past
           an old purchase and never re-pages it.

        Then start the sync. A sync already in flight is refused outright: step 2
        would be thrown away by the run that is already going.
        """
        cfg: config_mod.Config = request.app.state.cfg
        album = _find_album(request, album_id)
        sc = album.sidecar
        item_id = sc.bandcamp.item_id if sc and sc.bandcamp else None
        if item_id is None:
            # No purchase id, no re-download: an album linked only by
            # `candidate_item_ids` has several purchases it could be and we would
            # be guessing which to fetch (review-gate item 2).
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "This album isn't linked to a single Bandcamp purchase, so there's "
                "nothing to re-download.",
            )
        if request.app.state.sync_runner.is_running:
            return _flash_response(
                "Can't re-download now",
                "a sync is already running — wait for it to finish and try again",
                level=Level.WARNING,
                tasks_changed=False,
                status_code=409,
                album=album,
            )

        # Captured BEFORE the mutation, unlike everywhere else (event-recording
        # rule 2), because the mutation *destroys* the sidecar this id would be
        # re-read from. It is the right id regardless: a re-downloadable album is
        # matched, so `Album.id` is its `mb_release_id`, which the replacement
        # will be tagged with in turn — so the archive, the delete and the
        # re-tagging all land on one album history.
        aid = album.id
        label = f"{album.artist} — {album.title}".strip(" —")
        # One scope, so the audit rows beneath the archive attach to the activity
        # entry as its "what changed" (#97).
        with activity_store.action():
            try:
                result = archive.archive_and_remove(
                    album.folders,
                    music_root=cfg.paths.music_dir,
                    label=label,
                    album_id=aid,
                )
            except archive.ArchiveError as e:
                # Nothing was deleted (or, in the one partial-removal case, the
                # message says so) and no ignore was touched — the album is where
                # it was and the user can try again.
                return _flash_response(
                    "Couldn't archive this album",
                    str(e),
                    level=Level.ERROR,
                    tasks_changed=False,
                    status_code=500,
                    album=album,
                )
            # Drop it from the in-memory index NOW rather than waiting for the
            # rescan: `sync_items` seeds bandcampsync's ignore set from
            # `library_index.item_ids()`, and the sync starts below.
            for d in album.folders:
                library_index.remove(d)
            unignore = _remove_ignore_anywhere(cfg.ignores_file, item_id)
            pending_downloads.approve(item_id)
            _clear_bandcampsync_checkpoint(
                cfg.paths.music_dir, reason=f"re-download of purchase {item_id}"
            )
            redownloads.add(
                redownloads.PendingRedownload(
                    item_id=item_id,
                    artist=album.artist,
                    title=album.title,
                    url=sc.store_url or "" if sc else "",
                    archive_name=result.path.name,
                    requested_at=datetime.now(UTC),
                ),
                # Carried through the round trip so the replacement is tagged as
                # THIS release rather than re-resolved from the store URL (#132).
                # Re-downloading says the files are wrong, not the match.
                match=(
                    redownloads.CarriedMatch(
                        mb_release_id=sc.mb_release_id,
                        # Re-downloading an INCOMPLETE album is the issue's own
                        # case: the artist added tracks. It may well come back
                        # just as short (they haven't, or Bandcamp hasn't caught
                        # up), and that is the state it was already in — so it
                        # stays taggable rather than being demoted to the inbox.
                        incomplete=album.state == AlbumState.INCOMPLETE,
                    )
                    if sc and sc.mb_release_id
                    else None
                ),
            )
            # The album has left every state — it isn't on disk any more. The
            # next scan's reset_from would correct this anyway; doing it here
            # keeps the Library count honest in the same render (#11).
            live_counts.move(album.state, None)
            activity.record(
                f"Archived to {result.path.name} ({result.file_count} files) — "
                "re-downloading from Bandcamp",
                album_id=aid,
                album_label=label,
            )
        if unignore == "failed":
            # The album is already gone, so this is not fatal — but bandcampsync
            # will skip the purchase and the replacement never arrives. Say so
            # where the user will see it rather than only in the log. ("absent"
            # is not this: an adopted album was never in the ignores file, and
            # nothing was blocking the download in the first place.)
            activity.warning(
                f"{label}: couldn't take Bandcamp purchase {item_id} out of your ignores "
                "file, so the sync may skip it. Restore it from Settings → Ignored, or "
                "unzip the archive to put the album back.",
                album_id=aid,
            )
        try:
            request.app.state.sync_runner.start()
        except AlreadyRunningError:
            # Raced with a sync started between the check above and here. The
            # archive stands and the purchase is un-ignored, so the *next* sync
            # fetches it; the card stays up meanwhile saying exactly that.
            log.info("a sync started while archiving %s — the next one will fetch it", label)
        return _flash_response(
            "Re-downloading",
            f"old files archived to {result.path.name} in your music folder",
            album=album,
        )

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
        return _flash_response("Forgotten", "reverted to NEW", album=album)

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

    @app.get("/debug/memory")
    def debug_memory(request: Request) -> Response:
        """Live memory snapshot for diagnosis: process RSS, the size of the
        in-memory scan snapshot + cache, GC generation counts, and (when
        HARMONIST_TRACEMALLOC=1) the top allocation sites."""
        import gc
        import tracemalloc

        scan_runner = request.app.state.scan_runner
        rss = _process_rss_bytes()
        payload: dict[str, Any] = {
            "rss_mb": round(rss / 1e6, 1) if rss is not None else None,
            "albums_in_snapshot": len(scan_runner.albums()),
            "scan_cache_entries": scan_runner.cache_size(),
            "gc_counts": gc.get_count(),
            "tracemalloc": None,
        }
        if tracemalloc.is_tracing():
            current, peak = tracemalloc.get_traced_memory()
            top = tracemalloc.take_snapshot().statistics("lineno")[:15]
            payload["tracemalloc"] = {
                "current_mb": round(current / 1e6, 1),
                "peak_mb": round(peak / 1e6, 1),
                "top": [
                    {
                        "source": str(stat.traceback),
                        "size_mb": round(stat.size / 1e6, 2),
                        "blocks": stat.count,
                    }
                    for stat in top
                ],
            }
        else:
            payload["tracemalloc_hint"] = (
                "set HARMONIST_TRACEMALLOC=1 and restart for top allocations"
            )
        return JSONResponse(payload)

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
            sc = reconcile.reconcile_album(album.path, fetch_urls=mb_cache.fetch_release_urls)
        except Exception as e:
            log.exception("reconcile failed", extra=_LOG_ONLY)
            return _flash_response(
                "Reconcile failed",
                str(e),
                level=Level.ERROR,
                tasks_changed=False,
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                album=album,
            )
        if sc is None:
            # reconcile_album returns None for two reasons: existing sidecar
            # (already reconciled, often by the auto-runner) or no MBID atom.
            if sidecar_mod.has_sidecar(album.path):
                return _flash_response("Already reconciled", album=album)
            return _flash_response(
                "No MBID atom",
                "no MusicBrainz Album Id in its tags",
                level=Level.WARNING,
                tasks_changed=False,
                album=album,
            )
        label = "Bandcamp source" if sc.store_url else "manual source"
        return _flash_response("Reconciled", label, album=album)

    @app.post("/recheck/{album_id}", response_class=HTMLResponse)
    def recheck(request: Request, album_id: str, on_album_page: bool = Form(False)) -> Response:
        album = _find_album(request, album_id)
        sc = album.sidecar
        if sc is None or not sc.store_url:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "no store URL on sidecar")
        try:
            mbids = mb_lookup.lookup_by_bandcamp_url(sc.store_url)
        except mb_lookup.MBError as e:
            return _flash_response(
                "MB lookup failed",
                str(e),
                level=Level.ERROR,
                tasks_changed=False,
                album=album,
            )
        if not mbids:
            return _flash_response(
                "Still no match",
                "no MusicBrainz release for this URL yet",
                level=Level.WARNING,
                tasks_changed=False,
                album=album,
            )

        # A URL can map to several MB releases (e.g. a long digital edition plus
        # a shorter CD mix). Don't guess which one — surface them all and let the
        # user pick (into the card's shared, preserved results box).
        if len(mbids) > 1:
            try:
                results, total = mb_lookup.candidate_summaries_for_url(sc.store_url)
            except mb_lookup.MBError as e:
                return _flash_response(
                    "MB lookup failed",
                    str(e),
                    level=Level.ERROR,
                    tasks_changed=False,
                    album=album,
                )
            return _render_release_picker(
                request,
                album,
                results,
                total,
                heading="Several releases share this store URL — pick the right one",
                retarget=True,
                on_album_page=on_album_page,
            )

        try:
            # FRESH, never cached. "Recheck" means "I have just edited
            # MusicBrainz" — serving a stored payload would make the button a
            # silent no-op, with nothing on screen to say why (#127).
            releases = [mb_cache.fetch_release(m, max_age=mb_cache.FRESH) for m in mbids]
        except mb_lookup.MBError as e:
            return _flash_response(
                "MB fetch failed", str(e), level=Level.ERROR, tasks_changed=False, album=album
            )
        candidate = best_match(album.path, releases)
        assert candidate is not None  # releases is non-empty (mbids guarded)
        mbid = candidate.mb_release_id

        # `replace`, not a fresh `Sidecar(...)` (#263).
        new_sc = replace(
            sc,
            mb_release_id=mbid if candidate.confidence == "exact" else None,
            mb_match_candidate=None if candidate.confidence == "exact" else candidate,
        )
        sidecar_mod.write(album.path, new_sc)

        if candidate.confidence == "exact":
            try:
                _tag_with_release(
                    album.path,
                    mbid,
                    request.app.state.cfg,
                    request.app.state.tagger,
                    paths=album.folders,
                )
                return _flash_response("Tagged", "match found via Recheck", album=album)
            except Exception as e:
                log.exception("tag after recheck failed", extra=_LOG_ONLY)
                return _flash_response(
                    "Tagging failed",
                    str(e),
                    level=Level.ERROR,
                    tasks_changed=False,
                    album=album,
                )
        return _flash_response(
            "Needs review",
            f"{candidate.confidence} match — please review and confirm",
            album=album,
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
                paths=album.folders,
            )
        except Exception as e:
            log.exception("tag failed", extra=_LOG_ONLY)
            return _flash_response(
                "Tagging failed", str(e), level=Level.ERROR, tasks_changed=False, album=album
            )
        return _flash_response("Tagged", album=album)

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
                paths=album.folders,
            )
        except Exception as e:
            log.exception("incomplete tag failed", extra=_LOG_ONLY)
            return _flash_response(
                "Tagging failed", str(e), level=Level.ERROR, tasks_changed=False, album=album
            )
        return _flash_response("Tagged as incomplete", album=album)

    @app.post("/reject/{album_id}", response_class=HTMLResponse)
    def reject_match(request: Request, album_id: str) -> Response:
        album = _find_album(request, album_id)
        sc = album.sidecar
        if sc is None or sc.mb_match_candidate is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "no candidate to reject")
        # `replace`, not a fresh `Sidecar(...)` (#263).
        new_sc = replace(sc, mb_release_id=None, mb_match_candidate=None)
        sidecar_mod.write(album.path, new_sc)
        return _flash_response("Match rejected", album=album)

    @app.post("/library/{album_id}/tracks-unavailable", response_class=HTMLResponse)
    def set_tracks_unavailable(
        request: Request, album_id: str, accept: bool = Form(False)
    ) -> Response:
        """Accept an INCOMPLETE album as finished, or take that acceptance back.

        The claim being recorded is about the SOURCE — there are no more tracks
        to get — so it is offered only where that claim can be true: on an album
        that really is short. Accepting a complete album would be recording a
        fact about nothing.

        `accept` defaults to **False** because the control is a checkbox (#227),
        and an unticked checkbox posts no value at all — that absence IS the
        request to take the acceptance back. A caller that means to accept says
        so; nothing accepts by omission.

        Deliberately does NOT touch state or files. The album is still
        INCOMPLETE, its tile still reports the count, and no tag is rewritten;
        the only thing that changes is that the Library stops listing it as
        something to fix (#196).
        """
        album = _find_album(request, album_id)
        sc = album.sidecar
        if sc is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "no sidecar on this album")
        if accept and album.state != AlbumState.INCOMPLETE:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "only an incomplete album can be accepted as finished",
            )
        if sc.tracks_unavailable == accept:
            # Idempotent: the sidecar write would be a no-op anyway, but returning
            # here keeps a double-click out of the audit log entirely. Still
            # re-states the badge, so a checkbox that raced itself ends up
            # showing what is on disk rather than what the last click typed.
            return _flash_response("No change", album=album, oob=_completeness_oob(request, album))
        updated = replace(sc, tracks_unavailable=accept)
        sidecar_mod.write(album.path, updated)
        # The Library's filter counts change, and nothing else does — the state is
        # untouched, so there is no live_counts move to make. Refresh the snapshot
        # in one render rather than letting the async rescan dim the page (#11).
        runner = request.app.state.scan_runner
        if runner.is_engaged():
            runner.refresh_now()
        request.state.skip_rescan = True
        # The album page states the completeness beside this control (#227), so it
        # is now stale on the very page the click came from. Swap it out of band
        # from the album as it is AFTER the write — `album` was resolved before it.
        return _flash_response(
            "Accepted as complete" if accept else "Back in the Incomplete list",
            album=album,
            oob=_completeness_oob(request, replace(album, sidecar=updated)),
        )

    @app.post("/library/{album_id}/ignore-update", response_class=HTMLResponse)
    def ignore_update(request: Request, album_id: str, ignore: bool = Form(False)) -> Response:
        """Start or stop ignoring this album's update, from one checkbox (#366).

        ONE endpoint for both directions, and `ignore` defaulting to False is the
        whole of the mechanism: a ticked box posts `ignore=true`, an unticked one
        posts nothing at all, and the default reads that absence as taking the
        ignore back. That is the platform's own contract for a checkbox, and the
        same shape `/tracks-unavailable` already uses — which is why the two
        endpoints this used to need became one when the two buttons became a box.

        Stop listing this album's update until MusicBrainz changes the release.

        A bookmark, not a refusal, and the wording of every surface says so
        (#271). MusicBrainz is canonical — Harmonist keeps no local exception to
        what it says — so the answer to an update you disagree with is to edit
        the release, and this is how you stop being asked about it in the
        meantime. The edit landing is exactly what brings the album back.

        Recorded against `album.mb_version`, which is the version of the cached
        payload the flag was last computed from. Refused when there isn't one:
        nothing has compared this album yet, so there is no update on offer and
        nothing to be ignoring — and a bookmark against no version could never
        lapse, which would make the mute permanent.

        Writes nothing to the album. The files, the sidecar and the derived state
        are all untouched; the flag stays true, because it is a fact about the
        files against MusicBrainz rather than a piece of work. Only the Library's
        filter and this block change.
        """
        album = _find_album(request, album_id)
        if not ignore:
            # Unconditional, unlike its opposite. Ignoring needs an update on
            # offer to bookmark; un-ignoring is undoing a bookmark, and refusing
            # it on an album whose flag happens to read False right now would
            # leave the row in place with no control anywhere that could remove
            # it.
            if not activity_store.unignore_update(album.id):
                return _flash_response(
                    "Not ignored",
                    "this album's updates were already being listed",
                    level=Level.WARNING,
                    album=album,
                    tasks_changed=False,
                )
            runner = request.app.state.scan_runner
            if runner.is_engaged():
                runner.refresh_now()
            request.state.skip_rescan = True
            return _flash_response(
                "Listing updates again",
                album=album,
                oob=_update_ignore_oob(request, album, ignored=False),
            )
        if not album.update_available or album.mb_version is None:
            return _flash_response(
                "Nothing to ignore",
                "no update is outstanding for this album",
                level=Level.WARNING,
                album=album,
                tasks_changed=False,
            )
        activity_store.ignore_update(album.id, release_version=album.mb_version)
        # The Library's filter counts change and nothing else does — no state
        # moved, so there is no `live_counts` adjustment to make. Refresh the
        # snapshot in one render rather than letting the async rescan dim the
        # page (#11).
        runner = request.app.state.scan_runner
        if runner.is_engaged():
            runner.refresh_now()
        request.state.skip_rescan = True
        return _flash_response(
            "Ignored",
            "listed again when MusicBrainz next changes the release",
            album=album,
            oob=_update_ignore_oob(request, album, ignored=True),
        )

    @app.post("/surrender/{album_id}/keep", response_class=HTMLResponse)
    def surrender_keep(request: Request, album_id: str) -> Response:
        """Accept a surrendered album as done — there's no purchase to link (the
        release was withdrawn from Bandcamp, or it was bought elsewhere / ripped).
        Restore the release id from the read-only candidate, flag the purchase as
        unavailable, and clear the candidate → the scanner classifies it as a
        terminal Library album (COMPLETE/INCOMPLETE) and no future sync re-surrenders
        it. The files are already tagged with this release, so nothing is re-written."""
        album = _find_album(request, album_id)
        sc = album.sidecar
        cand = sc.mb_match_candidate if sc else None
        if sc is None or cand is None or not cand.unmatched_purchase:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "not a surrendered album")
        sidecar_mod.write(
            album.path,
            # `replace`, not a fresh `Sidecar(...)` (#263) — recording one
            # surrender used to erase the other, so an album accepted as
            # incomplete re-accused itself of missing tracks the moment its
            # purchase was accepted as gone.
            replace(
                sc,
                mb_release_id=cand.mb_release_id,
                mb_match_candidate=None,
                purchase_unavailable=True,
            ),
        )
        if album.state not in _TERMINAL_STATES:
            live_counts.move(album.state, AlbumState.COMPLETE)
        # Reflect the move in one render: refresh the snapshot synchronously and opt
        # out of the async post-mutation rescan, whose "scanning" status dims the
        # inbox — the #11 flicker. (When not engaged, the render re-scans fresh, so
        # nothing to refresh and skip_rescan is a harmless no-op.)
        runner = request.app.state.scan_runner
        if runner.is_engaged():
            runner.refresh_now()
        request.state.skip_rescan = True
        return _flash_response("Moved to Library", album=album)

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
        return _flash_response("URL updated", "run Sync to confirm", album=album)

    @app.post("/manual/{album_id}/search", response_class=HTMLResponse)
    def manual_search(
        request: Request,
        album_id: str,
        artist: str = Form(""),
        title: str = Form(""),
        # Which page asked (#150). The search form is rendered on both the inbox
        # card and the album page, and the rows it comes back with carry an
        # action whose right answer differs between the two.
        on_album_page: bool = Form(False),
    ) -> Response:
        # Validate album exists; a 404 is the right signal for a stale UI.
        album = _find_album(request, album_id)
        try:
            # Cap to a handful — beyond this, MB's own search is the better
            # tool. Each row links out to the release for closer inspection.
            results = mb_search.search_releases(artist, title, limit=5)
        except mb_search.MBSearchError as e:
            return _flash_response(
                "MB search failed",
                str(e),
                level=Level.ERROR,
                tasks_changed=False,
                album=album,
            )
        return _render_release_picker(
            request,
            album,
            results,
            len(results),
            heading="MusicBrainz search results",
            on_album_page=on_album_page,
        )

    @app.post("/manual/{album_id}/candidates", response_class=HTMLResponse)
    def manual_candidates(
        request: Request, album_id: str, on_album_page: bool = Form(False)
    ) -> Response:
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
            return _flash_response(
                "MB lookup failed",
                str(e),
                level=Level.ERROR,
                tasks_changed=False,
                album=album,
            )
        return _render_release_picker(
            request,
            album,
            results,
            total,
            heading="Releases linked to this store URL",
            on_album_page=on_album_page,
        )

    @app.post("/manual/{album_id}/assign", response_class=HTMLResponse)
    def manual_assign(request: Request, album_id: str, mbid: str = Form(...)) -> Response:
        album = _find_album(request, album_id)
        extracted = _extract_mbid(mbid)
        if not extracted:
            return _flash_response(
                "Could not parse",
                "Paste a full MB release URL or the 36-char MBID",
                level=Level.ERROR,
                tasks_changed=False,
                album=album,
            )
        try:
            status_str, msg = _apply_best_match(
                album.path, [extracted], request.app.state.cfg, request.app.state.tagger
            )
        except mb_lookup.MBError as e:
            return _flash_response(
                "MB lookup failed",
                str(e),
                level=Level.ERROR,
                tasks_changed=False,
                album=album,
            )
        except Exception as e:
            log.exception("manual assign failed", extra=_LOG_ONLY)
            return _flash_response(
                "Assignment failed",
                str(e),
                level=Level.ERROR,
                tasks_changed=False,
                album=album,
            )
        # status_str is 'tagged' or 'needs_confirmation' — use the friendlier
        # verb from msg's first clause.
        verb = "Tagged" if status_str == "tagged" else "Needs review"
        return _flash_response(verb, msg, album=album)

    @app.post("/unconfirmed/{album_id}/manual", response_class=HTMLResponse)
    def mark_unconfirmed_manual(request: Request, album_id: str) -> Response:
        """Drop the store URL + Bandcamp block. Album becomes "manually
        sourced" (store_url is None, store_name() returns None)."""
        album = _find_album(request, album_id)
        sc = album.sidecar
        if sc is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "no sidecar")
        # `replace`, not a fresh `Sidecar(...)` (#263).
        new_sc = replace(sc, store_url=None, bandcamp=None, notes="marked as purchased elsewhere")
        sidecar_mod.write(album.path, new_sc)
        return _flash_response("Marked manual", "purchased elsewhere", album=album)


def _replace_url(sc: Sidecar, new_url: str, new_bandcamp: BandcampInfo | None) -> Sidecar:
    """Build a new Sidecar with store_url and bandcamp block replaced."""
    # `replace`, not a fresh `Sidecar(...)` (#263).
    return replace(
        sc,
        store_url=new_url,
        bandcamp=new_bandcamp,
    )


def _is_writable(path: Path) -> bool:
    try:
        return path.exists() and (path.is_dir() or path.parent.is_dir())
    except OSError:
        return False


def _process_rss_bytes() -> int | None:
    """Resident set size of this process, in bytes — None if unavailable.

    Reads /proc/self/status on Linux (where Harmonist runs in Docker); falls
    back to getrusage for dev on macOS (ru_maxrss is bytes there, kB on Linux)."""
    try:
        with open("/proc/self/status", encoding="ascii") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024  # value is in kB
    except OSError:
        pass
    try:
        import resource

        maxrss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return maxrss if sys.platform == "darwin" else maxrss * 1024
    except (ImportError, OSError):
        return None


def _flash(message: str, *, level: str) -> str:
    """Render a small flash message fragment for HTMX swap-or-replace.

    `message` is escaped: it routinely carries `str(e)` from a failed MB call,
    and album/track text read off disk. Plain text only — no caller passes
    markup, and any that wanted to would have to render it elsewhere. The
    client half of this path already escapes (the `esc()` helper behind the
    `harmonist-status` trigger in index.html); this keeps the server-rendered
    fragment consistent with it.
    """
    classes = {
        "info": "bg-bc-teal/10 text-bc-teal border-bc-teal/30",
        "warning": "bg-amber-500/10 text-amber-300 border-amber-500/30",
        "error": "bg-red-500/10 text-red-300 border-red-500/30",
    }.get(level, "bg-slate-700/30 text-slate-200 border-slate-600")
    return (
        f'<div class="px-4 py-2 border rounded {classes} text-sm font-bold">'
        f"{html.escape(message)}</div>"
    )


def _live_album_ref(album: Album) -> tuple[str | None, str]:
    """`(current_id, label)` for an album an action just changed.

    The id is re-derived from the album's sidecar ON DISK rather than taken from
    the passed-in `Album`, because that object was resolved BEFORE the mutation
    and its `id` is frequently already dead by the time we record the event:
    tagging drops the sidecar's `temp_uid` in favour of the MBID (models.py), and
    unlink reverses that. The old id isn't merely un-indexed, it's erased — the
    registry fallback in `_find_album` can't recover it, so an entry recorded
    with it links nowhere forever (#65).

    Reading the sidecar here is one small file read on an action path that just
    wrote that same file, so it's warm.
    """
    label = f"{album.artist} — {album.title}".strip(" —")
    current = sidecar_mod.album_id_for(album.path)
    if current is not None:
        return current, label
    # No sidecar (still NEW, or it was just erased): the registry id the caller
    # already holds is the best available handle, and it IS resolvable.
    return album.id, label


def _flash_response(
    verb: str,
    details: str | None = None,
    *,
    level: Level = Level.INFO,
    tasks_changed: bool = True,
    status_code: int = 200,
    extra_triggers: dict[str, Any] | None = None,
    album: Album | None = None,
    record_activity: bool = True,
    oob: str = "",
) -> HTMLResponse:
    """Standard action response: flash HTML body + HX-Trigger events.

    The status bar renders the message as a level-coloured pill around
    `verb` followed by `details` in plain text. Splitting the two keeps
    the status bar visually light when the details run long.

    Emits:
      - `harmonist-status` — picked up by the status-bar JS in index.html.
      - `tasks-changed` (when `tasks_changed=True`) — inbox + library refresh.
      - any `extra_triggers` — endpoint-specific client events (e.g. an open
        modal reloading itself). Merged last; don't shadow the two above.

    Use for every endpoint that mutates album state. For pure-display
    failures (e.g. MB lookup error with no state change), pass
    `tasks_changed=False` to avoid spurious refreshes.

    `oob` is markup carrying `hx-swap-oob`, appended to the flash. Every caller
    of this helper posts with `hx-swap="none"`, so the flash body itself is
    discarded — but htmx still processes out-of-band elements in it, which is how
    a mutation can re-state the one region it changed without the page reloading.
    Pass it on *every* return path of an endpoint that uses it, including the
    no-op ones: a response without the fragment leaves the region as it was.

    Pass `album` whenever the action concerns one album, so the entry joins that
    album's history (#33) and the feed can link + name it (#65). The album's id
    is re-derived from disk here — see `_live_album_ref` for why the caller's
    `album.id` is not safe to record.
    """
    message = verb if not details else f"{verb} — {details}"
    album_id, album_label = (None, None) if album is None else _live_album_ref(album)
    # Every action outcome is also an activity-log entry (the Activity tab).
    # `record_activity=False` is for the rare case where a background worker
    # writes the authoritative entry itself — starting a sync, where only the
    # runner knows link-only vs full — and this would just duplicate it (#101).
    if record_activity:
        activity.record(message, level, album_id=album_id, album_label=album_label)
    triggers: dict[str, Any] = {
        "harmonist-status": {
            "verb": verb,
            "details": details,
            "level": level,
            # Carried SEPARATELY rather than folded into `details`, because the two
            # surfaces compose it differently: the feed puts the name in its own
            # (linked) position, while the status bar has no such column and must
            # inline it. Baking it into details would double it up in the feed —
            # the #65 mistake — and dropping it left the bar saying just "Tagged".
            "album": album_label,
        }
    }
    if tasks_changed:
        triggers["tasks-changed"] = True
    if extra_triggers:
        triggers.update(extra_triggers)
    return HTMLResponse(
        _flash(message, level=level) + oob,
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
    on_album_page: bool = False,
) -> Response:
    """Render the shared candidate-release list (store-URL picker or name
    search). `retarget` rewrites the swap to the card's preserved results box —
    needed when the trigger (e.g. the Recheck button) posts with hx-swap=none.

    `on_album_page` is what the *rows* need (#150). Every other action block is
    an include, so it inherits that flag from the template around it; this one
    is a response, rendered with no such surroundings, and its **Use** button
    assigns a release exactly as the paste box does. Without the flag travelling
    with the request, picking a release from a search made on the album page
    would tag the album and leave the page describing the album it used to be.
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
            "on_album_page": on_album_page,
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
