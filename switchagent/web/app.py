"""FastAPI application factory + routes. See docs/WEB-UI.md for the full
architecture writeup. Routes are deliberately thin -- they parse the
request, call one function in services.py, and either render a template
or return the result as JSON. All mutating logic (job creation,
retry/cancel, device rename, scan triggering) lives in services.py or
switchagent's existing modules, never inline here.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import __version__, build_info, db
from .. import title_id as title_id_mod
from . import addons_views, amiibo_views, detail_views, error_reporting, onboarding, services
from .context import WebContext
from .schemas import (
    AmiiboDeviceRequest,
    AmiiboRemoveRequest,
    AddonInstallRequest,
    BetaRequest,
    EmuiiboDownloadRequest,
    ConflictPolicyRequest,
    CreateJobsRequest,
    DeviceStorageMappingClearRequest,
    DeviceStorageMappingRequest,
    HistoryVerificationRequest,
    LibraryDirRequest,
    PreferencesRequest,
    RemoveFromQueueRequest,
    RenameDeviceRequest,
)

log = logging.getLogger("switchagent.web")

_WEB_DIR = Path(__file__).resolve().parent
# "Copy logs" (Settings): the last ~500 KB of the application log -- a
# few days of normal use, still a comfortable clipboard paste.
LOG_COPY_MAX_BYTES = 500_000


def _nav_context(request: Request) -> dict:
    """What every page's navigation needs: whether there is an Amiibo tab
    (amiibo_views.amiibo_tab_visible). Never allowed to break a page -- an
    error page included -- so a failed read just leaves the tab out."""
    from .. import preferences

    beta = preferences.beta_enabled()
    # Where emuiibo is installed from: Add-ons when the beta features are
    # on, else the Amiibo page itself (emuiibo is not a beta feature).
    nav = {"beta": beta, "emuiibo_home": "/addons/emuiibo" if beta else "/amiibo"}
    ctx = getattr(request.app.state, "ctx", None)
    if ctx is None:
        return {**nav, "amiibo_tab": False}
    try:
        conn = db.get_connection(ctx.db_path)
        try:
            return {**nav, "amiibo_tab": amiibo_views.amiibo_tab_visible(conn)}
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        log.debug("could not tell whether emuiibo is installed anywhere", exc_info=True)
        return {**nav, "amiibo_tab": False}


_TEMPLATES = Jinja2Templates(directory=str(_WEB_DIR / "templates"), context_processors=[_nav_context])


def _format_size(num_bytes) -> str:
    """Display-only formatting for templates -- not the same function as
    preview._format_size() (kept separate on purpose: that one is
    CLI-output formatting, this is an HTML template filter; duplicating an
    8-line presentation helper is simpler than importing across that
    boundary)."""
    if num_bytes is None:
        return "—"
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _format_mtime(epoch_seconds) -> str:
    import datetime as _dt
    if not epoch_seconds:
        return "—"
    return _dt.datetime.fromtimestamp(epoch_seconds).strftime("%Y-%m-%d %H:%M")


def _static_url(name: str) -> str:
    """`/static/<name>?v=<token>`, where the token changes whenever the
    file does. Templates call this instead of writing the path by hand.

    Why it exists: the `?v=N` numbers were maintained by hand, one per
    `<script>`/`<link>` tag, and a browser holding a cached copy has no
    other reason to re-fetch. Two changes shipped in this session never
    reached the page for exactly that -- app.js carried no version at all,
    and settings.js still served the previous build's "At least one folder
    is required" long after that rule had been removed from it. library.js
    had it both ways at once: `?v=3` on Library and bare on Game Details,
    i.e. two cache entries for one file, either of which could be stale.
    Hand-maintained cache keys are a thing to stop having, not to keep
    remembering.

    The token is the file's size and mtime, not a content hash: this is
    a local, single-user app serving a handful of small files per page,
    and re-reading them all on every render to hash them would be work
    spent to tell apart cases that cannot occur here. A missing file
    yields no token rather than raising -- a 404 in the network tab is a
    far better failure than a page that will not render at all."""
    try:
        stat = (_WEB_DIR / "static" / name).stat()
    except OSError:
        return f"/static/{name}"
    return f"/static/{name}?v={int(stat.st_mtime)}-{stat.st_size}"


_TEMPLATES.env.filters["filesize"] = _format_size
_TEMPLATES.env.filters["mtime"] = _format_mtime
_TEMPLATES.env.globals["app_version"] = __version__
_TEMPLATES.env.globals["build_label"] = build_info.label()
_TEMPLATES.env.globals["build_description"] = build_info.describe()
_TEMPLATES.env.globals["static_url"] = _static_url
_TEMPLATES.env.filters["game_name"] = title_id_mod.strip_release_tags
_TEMPLATES.env.filters["rich"] = addons_views.rich


# ---------------------------------------------------------------------------
# W3-007: onboarding -- thin glue that reads already-available state (config,
# DB, WebContext -- exactly what page_library()/page_devices() below already
# read for their normal content) and hands it to onboarding.py's pure
# decision functions. Kept here rather than in services.py on purpose (see
# this ticket's own handoff notes): services.py stays untouched, and
# onboarding.py itself never touches config/db/WebContext directly, matching
# every other "routes call one function, shape the dict" convention already
# used throughout this file (e.g. page_settings() below).
# ---------------------------------------------------------------------------

def _library_onboarding_message(conn) -> Optional[onboarding.OnboardingMessage]:
    from .. import config as config_mod

    library_info = config_mod.library_dir_info(config_mod.CONFIG_YAML_PATH)
    # Rows retained only as a record for Queue/History do not make the
    # library non-empty -- otherwise removing every folder left onboarding
    # insisting there was content to look at (see library_rows_in_scope).
    item_count = len(services.library_rows_in_scope(conn)[1])
    return onboarding.compute_library_message(
        library_dir_configured=library_info.configured,
        library_dir_exists=library_info.exists,
        library_item_count=item_count,
    )


def _device_onboarding_message(devices: list[dict]) -> Optional[onboarding.OnboardingMessage]:
    return onboarding.compute_device_message(
        known_device_count=len(devices),
        any_device_connected=any(d["connected"] for d in devices),
    )


def get_ctx(request: Request) -> WebContext:
    return request.app.state.ctx


def get_conn(ctx: WebContext = Depends(get_ctx)):
    conn = db.get_connection(ctx.db_path)
    db.init_db(conn)
    try:
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# W3-009: public error-reporting UX -- a global handler for any UNHANDLED
# exception during a request. HTTPException / request-validation errors
# already have their own FastAPI/Starlette handlers upstream of this one
# (registered separately, at a different layer) and are completely
# untouched by this -- this only ever fires for a genuine, unexpected bug,
# never for an ordinary 404/400/409/422 this codebase already raises on
# purpose. Renders a calm, generic page instead of a raw traceback; the
# traceback itself still reaches the log file exactly as before --
# Starlette's ServerErrorMiddleware re-raises the original exception after
# this handler runs (so it can still propagate to the ASGI server's own
# exception logging, see switchagent/logging_setup.py / cli.py's
# `switch-agent web`), this handler only changes what the CLIENT sees.
# ---------------------------------------------------------------------------

_JOB_ID_IN_PATH_RE = re.compile(r"/jobs/(\d+)")


def _pick_known_device_fingerprint(conn, ctx: Optional[WebContext]) -> str:
    """Prefers a currently-connected device (most relevant to whatever the
    user was just doing when this error happened) over a merely-known one;
    NEVER the raw device_id -- see error_reporting.py's own module
    docstring and diagnostics.py's established convention."""
    from ..mtp.windows import device_fingerprint

    if ctx is not None:
        connected = ctx.get_known_devices()
        if connected:
            return device_fingerprint(connected[0].device_id)
    known = db.list_devices(conn)
    if known:
        return device_fingerprint(known[0]["device_id"])
    return "—"


def _build_error_page_context(request: Request, exc: Exception) -> dict:
    """Gathers everything the generic error page needs, as defensively as
    possible -- this function must never itself be the reason the error
    page fails to render, so every DB/WebContext read here is wrapped and
    falls back to a safe placeholder rather than propagating a second
    exception out of an already-failing request."""
    from .. import config as config_mod
    from .. import diagnostics

    page_ctx: Optional[WebContext] = getattr(request.app.state, "ctx", None)

    runtime_mode = "—"
    device_fp = "—"
    job_id = "—"
    job_status = "—"

    match = _JOB_ID_IN_PATH_RE.search(request.url.path)
    if match:
        job_id = match.group(1)

    conn = None
    try:
        if page_ctx is not None:
            runtime_mode = config_mod.RUNTIME_MODE
            conn = db.get_connection(page_ctx.db_path)
            db.init_db(conn)
            device_fp = _pick_known_device_fingerprint(conn, page_ctx)
            if match:
                job_row = db.get_job(conn, int(match.group(1)))
                if job_row is not None:
                    job_status = job_row["status"]
    except Exception:  # noqa: BLE001 -- the error page itself must never crash
        log.exception("error page: failed to gather diagnostics context (non-fatal, showing placeholders)")
    finally:
        if conn is not None:
            conn.close()

    summary_input = error_reporting.IssueSummaryInput(
        version=__version__,
        windows=error_reporting.windows_version_string(),
        runtime_mode=runtime_mode,
        page_action=f"{request.method} {request.url.path}",
        job_id=job_id,
        device_fingerprint=device_fp,
        job_status=job_status,
        description="",
    )
    summary_text = error_reporting.build_issue_summary_text(summary_input)

    # Never a raw traceback, even behind the "Show technical details"
    # disclosure -- just the exception's own type+message, sanitized the
    # same way every other free-text diagnostics field in this codebase
    # already is (masks a raw device_id / the current user's home
    # directory / username -- see diagnostics.sanitize_free_text's own
    # docstring).
    technical_details: Optional[str] = None
    try:
        technical_details = diagnostics.sanitize_free_text(f"{type(exc).__name__}: {exc}")
    except Exception:  # noqa: BLE001
        technical_details = None

    return {
        "summary_text": summary_text,
        "report_issue_url": error_reporting.build_report_issue_url(summary_text),
        "technical_details": technical_details,
    }


def _handle_unexpected_error(request: Request, exc: Exception):
    # NOTE: FastAPI/Starlette runs a SYNC exception handler (this one) in a
    # worker thread (run_in_threadpool) -- log.exception()'s usual
    # ambient-sys.exc_info() trick does not see across that thread boundary,
    # so `exc_info=exc` is passed explicitly here instead (works from any
    # thread, formats the same full traceback into the log file). Starlette's
    # ServerErrorMiddleware also re-raises the original exception after this
    # handler returns (back on the request's own task), which is what
    # uvicorn's own "Exception in ASGI application" logging (also full
    # traceback, also log-file-only) is triggered by -- this call is a
    # deliberate belt-and-suspenders duplicate, not the only path a
    # traceback reaches the log through.
    log.error("unhandled exception during %s %s", request.method, request.url.path, exc_info=exc)
    context = _build_error_page_context(request, exc)
    return _TEMPLATES.TemplateResponse(request, "error.html", context, status_code=500)


def create_app(ctx: WebContext) -> FastAPI:
    app = FastAPI(title="SwitchAgent")
    app.state.ctx = ctx
    app.mount("/static", StaticFiles(directory=str(_WEB_DIR / "static")), name="static")
    # W3-009: see _handle_unexpected_error's own docstring/comments above --
    # only genuinely unhandled exceptions reach this; existing
    # HTTPException-based 4xx responses everywhere else in this file are
    # completely unaffected.
    app.add_exception_handler(Exception, _handle_unexpected_error)

    # -- health (readiness / single-instance detection / packaged smoke
    # tests -- deliberately no device/job/library data, see docs) --------

    @app.get("/api/health")
    def api_health():
        return {"app": "SwitchAgent", "version": __version__, "status": "ok"}

    # -- HTML pages -----------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def page_library(
        request: Request, search: Optional[str] = None, kind: str = "games",
        format: Optional[str] = None, sort: str = "date_added",
        filter: str = "all", not_installed: bool = False,
        conn=Depends(get_conn), ctx: WebContext = Depends(get_ctx),
    ):
        # W3-003: `filter` is an additive query param -- omitting it
        # reproduces the exact pre-W3-003 view. `not_installed` is a
        # second, independent additive param (a checkbox, not one more
        # mutually-exclusive `filter` option) -- see list_library_view's
        # own docstring for why it's never confirmed_on_device-based.
        #
        # installed_on_device_base_ids: ctx.get_known_installed_title_ids()'s
        # in-memory, CONNECTED-ONLY cache -- by explicit request, "On
        # Switch" must go dark the instant that Switch disconnects (nothing
        # can vouch for it anymore) and only relight once it's reconnected
        # and re-read. db.get_all_confirmed_installed_base_title_ids() (the
        # persisted table) is still written on every successful read, just
        # no longer what the Library page displays -- see that function's
        # own docstring for the opposite tradeoff it was built for.
        view = services.list_library_view(
            conn, kind=kind, search=search, format_filter=format, sort=sort,
            group_filter=filter, not_installed=not_installed,
            installed_on_device_base_ids=ctx.get_known_installed_title_ids(),
            connected_device_ids=ctx.get_connected_device_ids(), sd_presence=ctx.get_sd_presence(),
        )
        devices = services.list_devices(conn, ctx)
        # Corner hint (Settings' #dbi-installed-games-hint explains the
        # actual steps): a Switch is connected RIGHT NOW, but no currently
        # live device is reporting DBI's "Installed games" MTP node --
        # most likely means the DBI setting is off, worth a nudge rather
        # than a silently absent "On Switch" badge the user has no way to
        # explain. Deliberately still the live, connected-only cache here
        # (not the persisted table above): once ANY device has ever had a
        # successful CSV read, the persisted set would never be empty
        # again, and this hint would stop firing even for a brand new
        # device that genuinely has the DBI setting off right now.
        show_installed_games_hint = bool(ctx.get_known_devices()) and not ctx.get_known_installed_title_ids()
        return _TEMPLATES.TemplateResponse(request, "library.html", {
            "view": view, "devices": devices, "search": search or "",
            "show_installed_games_hint": show_installed_games_hint,
            "kind": kind, "format_filter": format or "", "sort": sort,
            "group_filter": filter, "not_installed": not_installed,
            "active_page": "library",
            "library_onboarding": _library_onboarding_message(conn),
        })

    @app.get("/queue", response_class=HTMLResponse)
    def page_queue(request: Request, conn=Depends(get_conn), ctx: WebContext = Depends(get_ctx)):
        from .. import config as config_mod

        return _TEMPLATES.TemplateResponse(request, "queue.html", {
            "groups": services.list_queue_grouped(conn), "devices": services.list_devices(conn, ctx),
            "worker_paused": ctx.worker_paused.is_set(), "active_page": "queue",
            "conflict_policy": config_mod.load_conflict_policy(),
            "abort": services.abort_status(conn, ctx),
            "removable_statuses": services._CANCELABLE_JOB_STATUSES,
        })

    @app.get("/history", response_class=HTMLResponse)
    def page_history(request: Request, conn=Depends(get_conn), ctx: WebContext = Depends(get_ctx)):
        return _TEMPLATES.TemplateResponse(request, "history.html", {
            # One row per transfer, newest first -- the same list Queue
            # shows, after the fact (services.list_history_entries).
            "view": services.list_history_entries(conn),
            "devices": services.list_devices(conn, ctx),
            "active_page": "history",
        })

    @app.get("/devices", response_class=HTMLResponse)
    def page_devices(request: Request, conn=Depends(get_conn), ctx: WebContext = Depends(get_ctx)):
        devices = services.list_devices(conn, ctx)
        storages_by_device = {
            d["device_id"]: services.list_device_storages(conn, ctx, d["device_id"]) for d in devices
        }
        return _TEMPLATES.TemplateResponse(request, "devices.html", {
            "devices": devices, "storages_by_device": storages_by_device,
            "allowed_storage_mappings": services.ALLOWED_MANUAL_STORAGE_MAPPINGS, "active_page": "devices",
            "device_onboarding": _device_onboarding_message(devices),
        })

    @app.get("/settings", response_class=HTMLResponse)
    def page_settings(request: Request, conn=Depends(get_conn), ctx: WebContext = Depends(get_ctx)):
        from .. import diagnostics

        devices = services.list_devices(conn, ctx)
        report = services.get_diagnostics_report(conn, ctx)
        return _TEMPLATES.TemplateResponse(request, "settings.html", {
            "settings": services.get_settings(conn, ctx), "devices": devices, "active_page": "settings",
            "diagnostics": diagnostics.report_to_dict(report),
            "diagnostics_text": diagnostics.format_report_text(report),
            "cleanup": services.get_work_cleanup_preview(conn),
        })

    # -- Amiibo (emuiibo) ----------------------------------------------------

    @app.get("/amiibo", response_class=HTMLResponse)
    def page_amiibo(request: Request, device: Optional[str] = None, conn=Depends(get_conn)):
        # No tab before emuiibo is on a console (amiibo_tab_visible): an old
        # link lands where it gets installed.
        from .. import preferences

        # With the beta features on, emuiibo is installed from Add-ons;
        # without them this page is where it is installed from.
        if not amiibo_views.amiibo_tab_visible(conn) and preferences.beta_enabled():
            return RedirectResponse("/addons/emuiibo", status_code=303)
        # Everything on the page is rendered by amiibo.js from
        # GET /api/amiibo -- one source for the first paint and every
        # refresh after a read or a removal.
        return _TEMPLATES.TemplateResponse(request, "amiibo.html", {
            "active_page": "amiibo", "device": device or "",
        })

    @app.get("/addons", response_class=HTMLResponse)
    def page_addons(request: Request, device: Optional[str] = None, conn=Depends(get_conn),
                    ctx: WebContext = Depends(get_ctx)):
        from .. import preferences

        # A beta feature: off, there is no Add-ons tab (Settings turns it on).
        if not preferences.beta_enabled():
            return RedirectResponse("/settings#beta", status_code=303)
        return _TEMPLATES.TemplateResponse(request, "addons.html", {
            "active_page": "addons", "view": addons_views.page(conn, ctx, device),
        })

    @app.get("/addons/{addon_id}", response_class=HTMLResponse)
    def page_addon(request: Request, addon_id: str, device: Optional[str] = None, conn=Depends(get_conn),
                   ctx: WebContext = Depends(get_ctx)):
        """One add-on in full -- what the list leaves out."""
        from .. import preferences

        if not preferences.beta_enabled():
            return RedirectResponse("/settings#beta", status_code=303)
        view = addons_views.page(conn, ctx, device)
        entry = next((e for e in view["entries"] if e["addon"].id == addon_id), None)
        if entry is None:
            raise HTTPException(status_code=404, detail=f"no add-on {addon_id!r} in the catalog")
        return _TEMPLATES.TemplateResponse(request, "addon.html", {
            "active_page": "addons", "view": view, "e": entry,
        })

    @app.get("/api/amiibo")
    def api_amiibo(device: Optional[str] = None, conn=Depends(get_conn), ctx: WebContext = Depends(get_ctx)):
        return amiibo_views.page(conn, ctx, device)

    @app.get("/api/amiibo/activity")
    def api_amiibo_activity(device: str, conn=Depends(get_conn), ctx: WebContext = Depends(get_ctx)):
        """The cheap poll: what the worker is doing with this console's
        emuiibo right now, and when it was last read -- the page reloads its
        whole view only when that changes."""
        device_id = _resolve_fingerprint_or_404(conn, device)
        stored = db.get_device_emuiibo(conn, device_id)
        return {**ctx.emuiibo.snapshot(device_id), "read_at": stored[2] if stored else None,
                "download": ctx.emuiibo_downloads.snapshot()}

    @app.get("/api/amiibo/emuiibo/offer")
    def api_emuiibo_offer(device: str, conn=Depends(get_conn), ctx: WebContext = Depends(get_ctx)):
        """Installing amiibo onto this console: should emuiibo come too?"""
        return amiibo_views.emuiibo_offer(conn, ctx, _resolve_fingerprint_or_404(conn, device))

    @app.post("/api/amiibo/emuiibo/download", status_code=202)
    def api_emuiibo_download(body: EmuiiboDownloadRequest, conn=Depends(get_conn),
                             ctx: WebContext = Depends(get_ctx)):
        """Download emuiibo's current release from GitHub into the Library
        and, given a console, queue it for install there."""
        from .emuiibo_service import DownloadRefused

        device_id = _resolve_fingerprint_or_404(conn, body.device) if body.device else None
        # With a console: whatever emuiibo needs that it lacks (its overlay
        # menu, Ultrahand) comes first, in the same install.
        ids = [a.id for a in addons_views.plan_install(conn, device_id, "emuiibo")] if device_id else ["emuiibo"]
        try:
            return ctx.emuiibo_downloads.start(device_id, ids)
        except DownloadRefused as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/addons/install", status_code=202)
    def api_addon_install(body: AddonInstallRequest, conn=Depends(get_conn), ctx: WebContext = Depends(get_ctx)):
        """Install one Add-ons catalog entry -- and what it requires that
        this console lacks -- from GitHub, through the ordinary queue."""
        from .emuiibo_service import DownloadRefused

        from .. import preferences

        # emuiibo and the overlay menu it needs are not beta features.
        if not preferences.beta_enabled() and body.addon not in ("emuiibo", "ultrahand"):
            raise HTTPException(status_code=403, detail="Add-ons are a beta feature -- turn it on in Settings")
        device_id = _resolve_fingerprint_or_404(conn, body.device)
        try:
            ids = [a.id for a in addons_views.plan_install(conn, device_id, body.addon)]
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"no add-on {body.addon!r} in the catalog") from exc
        try:
            return ctx.emuiibo_downloads.start(device_id, ids)
        except DownloadRefused as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/addons/activity")
    def api_addon_activity(ctx: WebContext = Depends(get_ctx)):
        return {"download": ctx.emuiibo_downloads.snapshot()}

    @app.post("/api/amiibo/refresh")
    def api_amiibo_refresh(body: AmiiboDeviceRequest, conn=Depends(get_conn), ctx: WebContext = Depends(get_ctx)):
        device_id = _resolve_fingerprint_or_404(conn, body.device)
        ctx.emuiibo.request_read(device_id)
        return {"ok": True}

    @app.post("/api/amiibo/remove")
    def api_amiibo_remove(body: AmiiboRemoveRequest, conn=Depends(get_conn), ctx: WebContext = Depends(get_ctx)):
        from .emuiibo_service import RemovalRefused

        device_id = _resolve_fingerprint_or_404(conn, body.device)
        try:
            return ctx.emuiibo.request_removal(conn, device_id, body.paths,
                                               confirm_save_data=body.confirm_save_data)
        except RemovalRefused as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    # -- JSON API: devices ------------------------------------------------

    @app.get("/api/devices")
    def api_list_devices(conn=Depends(get_conn), ctx: WebContext = Depends(get_ctx)):
        return services.list_devices(conn, ctx)

    @app.post("/api/devices/{device_id}/rename")
    def api_rename_device(device_id: str, body: RenameDeviceRequest, conn=Depends(get_conn)):
        try:
            services.rename_device(conn, device_id, body.friendly_name)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"ok": True}

    # -- JSON API: device storage mapping (UI-007) -------------------------

    @app.get("/api/devices/{device_id}/storages")
    def api_list_device_storages(device_id: str, conn=Depends(get_conn), ctx: WebContext = Depends(get_ctx)):
        return services.list_device_storages(conn, ctx, device_id)

    @app.post("/api/devices/{device_id}/storages/mapping")
    def api_set_device_storage_mapping(
        device_id: str, body: DeviceStorageMappingRequest, conn=Depends(get_conn), ctx: WebContext = Depends(get_ctx),
    ):
        try:
            services.set_device_storage_mapping(conn, ctx, device_id, body.raw_storage_name, body.logical_name)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"ok": True}

    @app.post("/api/devices/{device_id}/storages/mapping/clear")
    def api_clear_device_storage_mapping(
        device_id: str, body: DeviceStorageMappingClearRequest, conn=Depends(get_conn),
    ):
        services.clear_device_storage_mapping(conn, device_id, body.raw_storage_name)
        return {"ok": True}

    # -- JSON API: library --------------------------------------------------

    @app.get("/api/library")
    def api_list_library(
        search: Optional[str] = None, status: str = "all", format: Optional[str] = None,
        sort: str = "date_added", conn=Depends(get_conn), ctx: WebContext = Depends(get_ctx),
    ):
        return services.list_library(
            conn, search=search, status_filter=status, format_filter=format, sort=sort,
            installed_on_device_base_ids=ctx.get_known_installed_title_ids(),
            connected_device_ids=ctx.get_connected_device_ids(), sd_presence=ctx.get_sd_presence(),
        )

    @app.get("/api/library/{item_id}")
    def api_get_library_item(item_id: int, conn=Depends(get_conn)):
        entry = services.get_library_item(conn, item_id)
        if entry is None:
            raise HTTPException(status_code=404, detail="library item not found")
        return entry

    # -- JSON API: scan (point 16: never blocks the request) ---------------

    @app.post("/api/scan")
    def api_trigger_scan(ctx: WebContext = Depends(get_ctx)):
        started = ctx.run_scan_in_background()
        return {"started": started}

    @app.post("/api/scan/cancel")
    def api_cancel_scan(ctx: WebContext = Depends(get_ctx)):
        """Cooperative stop -- see WebContext.cancel_scan(). Returns
        immediately with whether there was a scan to stop; the scan itself
        unwinds on its own thread, and /api/scan/status is what says when
        it actually has."""
        return {"cancelled": ctx.cancel_scan(), **ctx.scan_status_snapshot()}

    @app.get("/api/scan/status")
    def api_scan_status(ctx: WebContext = Depends(get_ctx)):
        return ctx.scan_status_snapshot()

    @app.get("/api/activity")
    def api_activity(conn=Depends(get_conn), ctx: WebContext = Depends(get_ctx)):
        """Everything the Library page's activity panel shows, in one read:
        the library scan, the cover download and what each connected
        console is being asked. Memory only -- nothing here touches a disk
        walk or a device."""
        from .. import preferences
        covers = ctx.covers.snapshot()
        devices = ctx.device_activity_snapshot()
        for device in devices:
            device["label"] = services.device_label(conn, device.pop("device_id"))
        return {
            "scan": ctx.scan_status_snapshot(),
            "covers": {
                "enabled": preferences.load()["covers"], "running": covers["running"],
                "phase": covers["phase"], "ready": len(covers["ready"]), "total": covers["total"],
                "not_found": covers["not_found"], "failed": covers["failed"],
                # Only a real failure is an error here: "TitleDB has no cover
                # for this homebrew port" is an answer, shown as a count.
                "error": covers["error"] if covers["failed"] else None,
            },
            "devices": devices,
        }

    # -- JSON API: queue / jobs (mutations are explicit, separate from reads) --

    @app.get("/api/queue")
    def api_list_queue(conn=Depends(get_conn)):
        return services.list_queue(conn)

    @app.get("/api/queue/grouped")
    def api_list_queue_grouped(conn=Depends(get_conn)):
        return services.list_queue_grouped(conn)

    @app.get("/api/queue/dock")
    def api_queue_dock(conn=Depends(get_conn), ctx: WebContext = Depends(get_ctx)):
        """The dock along the bottom of every page -- one row per game."""
        return services.queue_dock(conn, ctx.preparations.dock_items(), paused=ctx.worker_paused.is_set())

    @app.post("/api/queue/remove")
    def api_queue_remove(body: RemoveFromQueueRequest, ctx: WebContext = Depends(get_ctx), conn=Depends(get_conn)):
        """Queue page "Remove from queue" -- one game's card, every part of
        it that has not started sending (services.remove_from_queue)."""
        try:
            return services.remove_from_queue(conn, ctx, body.job_ids, body.library_item_ids)
        except services.RemoveRefused as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/preparations", status_code=202)
    def api_prepare_jobs(body: CreateJobsRequest, ctx: WebContext = Depends(get_ctx), conn=Depends(get_conn)):
        target = services.resolve_target_device_id(conn, body.target_device_id)
        try:
            return {"preparation_id": ctx.preparations.submit(
                body.library_item_ids, target, amiibo_selection=body.amiibo_selection)}
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/preparations")
    def api_preparations(ctx: WebContext = Depends(get_ctx)):
        return ctx.preparations.snapshot()

    @app.post("/api/jobs")
    def api_create_jobs(body: CreateJobsRequest, conn=Depends(get_conn)):
        result = services.create_and_confirm_jobs(
            conn, body.library_item_ids, body.target_device_id, amiibo_selection=body.amiibo_selection)
        if result["created"] and not result["errors"]:
            status_code = 201
        elif result["created"]:
            status_code = 207  # partial success
        else:
            status_code = 422
        return JSONResponse(status_code=status_code, content=result)

    @app.get("/api/jobs/{job_id}")
    def api_get_job(job_id: int, conn=Depends(get_conn)):
        job = services.get_job(conn, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        return job

    @app.post("/api/jobs/{job_id}/retry")
    def api_retry_job(job_id: int, ctx: WebContext = Depends(get_ctx), conn=Depends(get_conn)):
        try:
            result = services.retry_job(conn, job_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        ctx.preparations.continue_chain(job_id)
        return {"ok": True, **result}

    @app.post("/api/jobs/{job_id}/cancel")
    def api_cancel_job(job_id: int, ctx: WebContext = Depends(get_ctx), conn=Depends(get_conn)):
        try:
            services.cancel_job(conn, job_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        ctx.preparations.continue_chain(job_id)
        return {"ok": True}

    # -- JSON API: history ---------------------------------------------------

    @app.get("/api/history")
    def api_list_history(conn=Depends(get_conn)):
        return services.list_history(conn)

    @app.post("/api/history/{history_id}/verification")
    def api_set_history_verification(history_id: int, body: HistoryVerificationRequest, conn=Depends(get_conn)):
        try:
            result = services.set_history_verification(conn, history_id, body.outcome)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"ok": True, **result}

    # -- JSON API: settings / worker control ---------------------------------

    @app.get("/api/settings")
    def api_get_settings(conn=Depends(get_conn), ctx: WebContext = Depends(get_ctx)):
        return services.get_settings(conn, ctx)

    @app.post("/api/settings/conflict-policy")
    def api_set_conflict_policy(body: ConflictPolicyRequest):
        return services.set_conflict_policy(body.policy)

    # -- JSON API: update availability notification (W3-008) -------------
    # Deliberately its own endpoint, fetched asynchronously by settings.js
    # AFTER the page has already rendered -- never embedded in
    # GET /settings's own server-rendered data, so a slow/offline GitHub
    # check (up to ~10s on a stale cache) can never delay the Settings
    # page itself from loading.

    @app.get("/api/update-check")
    def api_get_update_check():
        return services.get_update_check_status(force=False)

    @app.post("/api/update-check")
    def api_force_update_check():
        return services.get_update_check_status(force=True)

    # The top banner's "Update": download and install SwitchAgent itself
    # (switchagent/app_update.py). GET is the banner's poll while it runs.
    @app.get("/api/app-update")
    def api_app_update_status(ctx: WebContext = Depends(get_ctx)):
        return {**services.get_update_check_status(force=False), **ctx.app_updater.status()}

    @app.post("/api/app-update")
    def api_app_update_start(ctx: WebContext = Depends(get_ctx)):
        from .. import github_release

        try:
            return ctx.app_updater.start()
        except github_release.DownloadError as exc:
            raise HTTPException(status_code=409, detail=str(exc))

    # -- JSON API: library folder (W3-002) -------------------------------

    @app.post("/api/preferences/beta")
    def api_set_beta(body: BetaRequest, ctx: WebContext = Depends(get_ctx)):
        from .. import preferences

        enabled = preferences.set_beta(body.enabled)
        # Only a running app asks GitHub (desktop.py / cli.py start it;
        # tests never do): the first check comes right after turning it on.
        if enabled and ctx.addon_releases.running:
            ctx.addon_releases.check_soon()
        return {"beta": enabled}

    @app.get("/api/preferences")
    def api_preferences(ctx: WebContext = Depends(get_ctx)):
        from .. import preferences
        return {**preferences.load(), "covers_running": ctx.covers.running, "covers_error": ctx.covers.error}

    @app.post("/api/preferences")
    def api_save_preferences(body: PreferencesRequest, ctx: WebContext = Depends(get_ctx)):
        from .. import preferences
        values = preferences.save(body.auto_scan, body.scan_interval, body.covers)
        ctx.stop_library_watcher()
        ctx.start_library_watcher()
        if body.auto_scan:
            ctx.run_scan_in_background()
        return values

    @app.post("/api/covers/refresh")
    def api_refresh_covers(retry: bool = False, conn=Depends(get_conn), ctx: WebContext = Depends(get_ctx)):
        view = services.list_library_view(conn)
        # Pass each family's display name alongside its TITLE_ID -- covers.py
        # falls back to a TitleDB name search when the TITLE_ID itself isn't
        # found there (a release filename's bracketed TITLE_ID is never more
        # than a low-confidence guess, see title_id.py's from_filename).
        # And where its files are: a homebrew port's cover is the icon
        # inside its own .nro.
        ctx.covers.submit([
            (g["base_title_id"], g["name"], [e["absolute_path"] for e in services._family_entries(g)])
            for g in view["games"]
        ], retry=retry)
        return {"queued": True}

    @app.get("/api/covers/status")
    def api_covers_status(ctx: WebContext = Depends(get_ctx)):
        from .. import preferences
        return {**ctx.covers.snapshot(), "enabled": preferences.load()["covers"]}

    @app.get("/api/covers/{title_id}")
    def api_cover(title_id: str):
        from ..covers import image_path
        try:
            path = image_path(title_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not path.is_file():
            raise HTTPException(status_code=404, detail="Cover not cached yet")
        with path.open("rb") as file:
            header = file.read(12)
        media = "image/png" if header.startswith(b"\x89PNG") else "image/webp" if header.startswith(b"RIFF") else "image/jpeg"
        return FileResponse(path, media_type=media, headers={"Cache-Control": "public, max-age=604800"})

    @app.post("/api/settings/library-dir/validate")
    def api_validate_library_dir(body: LibraryDirRequest):
        return services.validate_library_dir(body.path)

    @app.post("/api/settings/library-dir")
    def api_set_library_dir(body: LibraryDirRequest, conn=Depends(get_conn), ctx: WebContext = Depends(get_ctx)):
        try:
            result = services.set_library_dir(conn, ctx, body.path, body.paths)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"ok": True, **result}

    @app.post("/api/settings/library-dir/pick")
    def api_pick_library_dir(request: Request):
        from ipaddress import ip_address
        from ..folder_picker import pick_folder
        try:
            local = request.client is not None and ip_address(request.client.host).is_loopback
        except ValueError:
            local = False
        if not local:
            raise HTTPException(status_code=403, detail="Open Settings on the computer running SwitchAgent to pick a folder")
        try:
            return {"path": pick_folder()}
        except Exception as exc:
            logging.getLogger(__name__).exception("Folder picker failed")
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    # -- JSON API: work directory cleanup (UI-004) -----------------------

    @app.get("/api/work-cleanup/preview")
    def api_work_cleanup_preview(conn=Depends(get_conn)):
        return services.get_work_cleanup_preview(conn)

    @app.post("/api/work-cleanup")
    def api_work_cleanup_execute(conn=Depends(get_conn)):
        return services.execute_work_cleanup(conn)

    # -- JSON API: diagnostics (UI-001) ---------------------------------

    @app.get("/api/diagnostics")
    def api_get_diagnostics(conn=Depends(get_conn), ctx: WebContext = Depends(get_ctx)):
        from .. import diagnostics

        report = services.get_diagnostics_report(conn, ctx)
        return diagnostics.report_to_dict(report)

    # -- JSON/text API: diagnostics export (UI-008) ----------------------

    @app.get("/api/diagnostics/export")
    def api_export_diagnostics(format: str = "json", conn=Depends(get_conn), ctx: WebContext = Depends(get_ctx)):
        from .. import diagnostics

        payload = services.get_diagnostics_export(conn, ctx)
        if format == "txt":
            return PlainTextResponse(diagnostics.export_dict_to_text(payload))
        return payload

    @app.get("/api/logs")
    def api_logs():
        """Settings' "Copy logs" -- the application log's recent end,
        through the same redaction as the diagnostics export's log tail,
        just a longer stretch of it. Empty (not 404) when there is no log
        file yet, e.g. a dev `switch-agent web` run."""
        from .. import config as config_mod
        from .. import diagnostics

        tail = diagnostics.read_sanitized_log_tail(config_mod.LOGS_DIR / "switchagent.log", max_bytes=LOG_COPY_MAX_BYTES)
        return PlainTextResponse(tail or "")

    @app.post("/api/worker/pause")
    def api_worker_pause(ctx: WebContext = Depends(get_ctx)):
        ctx.worker_paused.set()
        return {"paused": True}

    @app.post("/api/worker/resume")
    def api_worker_resume(ctx: WebContext = Depends(get_ctx)):
        ctx.worker_paused.clear()
        return {"paused": False}

    @app.get("/api/worker/status")
    def api_worker_status(conn=Depends(get_conn), ctx: WebContext = Depends(get_ctx)):
        """The Queue page's header controls, polled with the job list:
        Pause/Resume state, whether Abort has anything to act on, and
        whether an earlier Abort is still waiting for a transfer to stop."""
        return services.abort_status(conn, ctx)

    @app.post("/api/queue/abort")
    def api_queue_abort(conn=Depends(get_conn), ctx: WebContext = Depends(get_ctx)):
        """Queue page "Abort" -- see services.abort_all(). The transfer in
        flight (stopping_job_ids) stops at its next safe point, not
        necessarily before this returns."""
        return {"ok": True, **services.abort_all(conn, ctx)}

    @app.post("/api/worker/restart")
    def api_worker_restart(ctx: WebContext = Depends(get_ctx)):
        """UI-006 'Restart worker when safe' -- see WebContext.
        request_worker_restart()'s own docstring for exactly what this can
        and cannot do (never interrupts an in-flight COM call)."""
        ctx.request_worker_restart()
        return {"restart_pending": True}

    # =====================================================================
    # W3-001 Game Details / W3-004 Device Details.
    #
    # Kept together as one clearly separated block of NEW routes (nothing
    # above is restructured), with all view assembly in web/detail_views.py.
    # =====================================================================

    @app.get("/games/{base_title_id}", response_class=HTMLResponse)
    def page_game_detail(
        request: Request, base_title_id: str,
        conn=Depends(get_conn), ctx: WebContext = Depends(get_ctx),
    ):
        """W3-001. `base_title_id` is the family's 16-hex base TITLE_ID.
        404 for a non-TITLE_ID-shaped segment or a family the library knows
        nothing about -- never a blank page implying the game exists."""
        view = detail_views.build_game_detail_view(conn, ctx, base_title_id)
        if view is None:
            raise HTTPException(status_code=404, detail="no such game family in the library")
        return _TEMPLATES.TemplateResponse(request, "game_detail.html", {
            "view": view, "devices": services.list_devices(conn, ctx), "active_page": "library",
        })

    @app.get("/devices/{fingerprint}", response_class=HTMLResponse)
    def page_device_detail(
        request: Request, fingerprint: str,
        conn=Depends(get_conn), ctx: WebContext = Depends(get_ctx),
    ):
        """W3-004. Addressed by mtp.windows.device_fingerprint(), never the
        raw serial-bearing device_id -- this is a URL a human sees."""
        view = detail_views.build_device_detail_view(conn, ctx, fingerprint)
        if view is None:
            raise HTTPException(status_code=404, detail="no such device")
        return _TEMPLATES.TemplateResponse(request, "device_detail.html", {
            "view": view, "allowed_storage_mappings": services.ALLOWED_MANUAL_STORAGE_MAPPINGS,
            "active_page": "devices",
        })

    @app.get("/api/devices/by-fingerprint/{fingerprint}")
    def api_get_device_by_fingerprint(
        fingerprint: str, conn=Depends(get_conn), ctx: WebContext = Depends(get_ctx),
    ):
        """W3-004: server-side fingerprint -> device resolution, for
        anything on the Device Details page that just needs the device's
        own fields (never used by rename/storage-mapping, which have their
        own fingerprint-addressed routes below -- see those for why).

        Returns the same dict shape as an entry of GET /api/devices,
        including `device_id`: an API payload field, which is the
        established carve-out (ARCH-001) -- what must never happen is that
        string appearing as visible text or in a page URL."""
        device_id = detail_views.resolve_device_id_by_fingerprint(conn, fingerprint)
        if device_id is None:
            raise HTTPException(status_code=404, detail="no such device")
        device = next((d for d in services.list_devices(conn, ctx) if d["device_id"] == device_id), None)
        if device is None:  # pragma: no cover -- resolve_* already proved the row exists
            raise HTTPException(status_code=404, detail="no such device")
        return device

    def _resolve_fingerprint_or_404(conn, fingerprint: str) -> str:
        device_id = detail_views.resolve_device_id_by_fingerprint(conn, fingerprint)
        if device_id is None:
            raise HTTPException(status_code=404, detail="no such device")
        return device_id

    @app.post("/api/devices/by-fingerprint/{fingerprint}/rename")
    def api_rename_device_by_fingerprint(
        fingerprint: str, body: RenameDeviceRequest, conn=Depends(get_conn),
    ):
        """W3-004 privacy finding (independent verification, 2026-09-12):
        the Device Details page is addressed by fingerprint, but its
        rename/storage-mapping actions still called the raw-device_id
        routes below with that id in the URL path -- harmless in the page's
        own HTML (never rendered there), but uvicorn's own access log
        records every request's path, so the raw, serial-bearing id was
        landing in the server's log on every rename/mapping click from
        this page. These fingerprint-addressed twins resolve server-side
        (same as api_get_device_by_fingerprint/api_export_device_diagnostics
        already do) and are what device_detail.js now calls instead -- the
        original raw-device_id routes are unchanged and still used by
        devices.js on the Devices list page, an internal API call that was
        never the leak."""
        device_id = _resolve_fingerprint_or_404(conn, fingerprint)
        try:
            services.rename_device(conn, device_id, body.friendly_name)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"ok": True}

    @app.post("/api/devices/by-fingerprint/{fingerprint}/forget")
    def api_forget_device_by_fingerprint(
        fingerprint: str, conn=Depends(get_conn), ctx: WebContext = Depends(get_ctx),
    ):
        """Drop a previously-seen device from the Devices page -- see
        services.forget_device() for what that does and the two states it
        refuses in. Fingerprint-addressed like its neighbours (W3-004: a
        raw, serial-bearing device_id must never appear in a request path,
        which lands verbatim in uvicorn's access log).

        404 vs 409 is a real distinction here, not decoration: 404 means
        "no device with that fingerprint" (a stale page), 409 means "that
        device exists and forgetting it right now would be wrong" -- the
        message is written to be shown to the user as-is."""
        device_id = _resolve_fingerprint_or_404(conn, fingerprint)
        try:
            services.forget_device(conn, ctx, device_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"ok": True}

    @app.get("/api/devices/by-fingerprint/{fingerprint}/storages")
    def api_list_device_storages_by_fingerprint(
        fingerprint: str, conn=Depends(get_conn), ctx: WebContext = Depends(get_ctx),
    ):
        """Fingerprint-addressed twin of GET /api/devices/{device_id}/storages
        (same W3-004 reasoning as the routes around it: the raw device_id
        never belongs in a request path, which lands in uvicorn's own
        access log). Used by the Library page's header space bar to read
        the connected Switch's SD_CARD free/total bytes without the page's
        own JS ever handling a raw device_id."""
        device_id = _resolve_fingerprint_or_404(conn, fingerprint)
        return services.list_device_storages(conn, ctx, device_id)

    @app.post("/api/devices/by-fingerprint/{fingerprint}/storages/mapping")
    def api_set_device_storage_mapping_by_fingerprint(
        fingerprint: str, body: DeviceStorageMappingRequest, conn=Depends(get_conn), ctx: WebContext = Depends(get_ctx),
    ):
        device_id = _resolve_fingerprint_or_404(conn, fingerprint)
        try:
            services.set_device_storage_mapping(conn, ctx, device_id, body.raw_storage_name, body.logical_name)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"ok": True}

    @app.post("/api/devices/by-fingerprint/{fingerprint}/storages/mapping/clear")
    def api_clear_device_storage_mapping_by_fingerprint(
        fingerprint: str, body: DeviceStorageMappingClearRequest, conn=Depends(get_conn),
    ):
        device_id = _resolve_fingerprint_or_404(conn, fingerprint)
        services.clear_device_storage_mapping(conn, device_id, body.raw_storage_name)
        return {"ok": True}

    @app.get("/api/devices/by-fingerprint/{fingerprint}/diagnostics/export")
    def api_export_device_diagnostics(
        fingerprint: str, format: str = "json",
        conn=Depends(get_conn), ctx: WebContext = Depends(get_ctx),
    ):
        """W3-004 'Export diagnostics for this device' -- the same
        diagnostics engine as GET /api/diagnostics/export, scoped to one
        device (see detail_views.build_device_diagnostics_export)."""
        device_id = detail_views.resolve_device_id_by_fingerprint(conn, fingerprint)
        if device_id is None:
            raise HTTPException(status_code=404, detail="no such device")
        payload = detail_views.build_device_diagnostics_export(conn, ctx, device_id, fingerprint)
        if format == "txt":
            return PlainTextResponse(detail_views.device_diagnostics_export_to_text(payload))
        return payload

    return app
