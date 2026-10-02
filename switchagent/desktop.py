"""Windows desktop entry point for the packaged SwitchAgent.exe.

This is NOT the CLI (`switch-agent`, switchagent/cli.py) -- that stays
completely untouched and keeps working exactly as before
(`switch-agent web`, `scan`, `preview`, etc.). This module is what a
double-clicked SwitchAgent.exe actually runs:

  1. configure rotating file logging (switchagent/logging_setup.py)
  2. single-instance check (switchagent/single_instance.py) -- a second
     launch opens the first instance's Web UI and exits, never starting
     a second worker/server against the same device/DB
  3. first-run config bootstrap + start the existing WebContext
     (switchagent.web.context) exactly as `switch-agent web` does
  4. start uvicorn in a background thread, wait for a REAL readiness
     signal (uvicorn.Server.started -- not a fixed sleep) before doing
     anything else
  5. open the default browser
  6. run a system tray icon (switchagent/tray.py) until Exit
  7. shut everything down gracefully: HTTP server -> library watcher ->
     worker thread -> runtime-state file -> mutex

Nothing here changes transfer/queue/MTP semantics -- this is purely a
packaging-facing orchestration layer on top of the existing, unmodified
switchagent.web.context.WebContext / switchagent.web.app.create_app.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import time
import webbrowser
from typing import Optional

from . import config, firstrun, single_instance
from .logging_setup import configure_logging
from .web.app import create_app
from .web.context import WebContext, build_mock_context, build_real_context

log = logging.getLogger("switchagent.desktop")

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8765
READY_TIMEOUT_SECONDS = 30.0
READY_POLL_INTERVAL_SECONDS = 0.1
SHUTDOWN_JOIN_TIMEOUT_SECONDS = 15.0


def _runtime_info_path():
    return config.APP_DATA_ROOT / "runtime.json"


def _open_browser(port: int) -> None:
    try:
        webbrowser.open(f"http://127.0.0.1:{port}/")
    except Exception:
        log.exception("failed to open the default browser")


def _wait_until_ready(server, *, timeout_seconds: float = READY_TIMEOUT_SECONDS,
                       poll_interval_seconds: float = READY_POLL_INTERVAL_SECONDS,
                       sleep_fn=time.sleep, clock_fn=time.monotonic) -> bool:
    """A real readiness check -- polls uvicorn.Server's own `started` flag
    (set True only once the ASGI app's startup is complete and the socket
    is actually accepting connections), never a fixed `sleep(N)` guess."""
    deadline = clock_fn() + timeout_seconds
    while clock_fn() < deadline:
        if server.started:
            return True
        sleep_fn(poll_interval_seconds)
    return server.started


def build_parser() -> argparse.ArgumentParser:
    from . import __version__

    parser = argparse.ArgumentParser(prog="SwitchAgent")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--mock", action="store_true", help="use MockMtpBackend instead of real hardware")
    parser.add_argument("--no-browser", action="store_true", help="do not auto-open the default browser")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--host", default=DEFAULT_HOST, help="bind address (default: all interfaces for LAN access)")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    configure_logging()
    log.info("SwitchAgent desktop launcher starting (pid=%s)", os.getpid())

    try:
        mutex_handle, already_running = single_instance.acquire_mutex()
    except Exception:
        log.exception("could not acquire the single-instance mutex -- continuing without one")
        mutex_handle, already_running = None, False

    try:
        if already_running:
            return _handle_second_instance(args)
        return _run_primary_instance(args)
    except Exception:
        log.exception("fatal startup error")
        return 1
    finally:
        single_instance.release_mutex(mutex_handle)


def _handle_second_instance(args: argparse.Namespace) -> int:
    log.info("another SwitchAgent instance's mutex is already held -- checking it is actually alive")
    info = single_instance.read_runtime_info(_runtime_info_path())
    port = info.port if info is not None else args.port

    if single_instance.wait_for_existing_instance(port):
        log.info("existing instance confirmed alive on port %d -- opening its Web UI, not starting a second one", port)
        if not args.no_browser:
            _open_browser(port)
        return 0

    log.warning(
        "the single-instance mutex is held, but no SwitchAgent answered /api/health on port %d -- "
        "refusing to start a second instance regardless (a live OS mutex is a strong signal on its own)",
        port,
    )
    return 1


def _run_primary_instance(args: argparse.Namespace) -> int:
    import uvicorn

    use_mock = args.mock or os.environ.get("MOCK_MTP", "").lower() in ("1", "true", "yes")
    if use_mock:
        # Before anything opens a connection -- see config.use_mock_database()
        # for the incident this exists to make impossible. Logged, not silent:
        # the whole point is that a mock run started by hand from dist/ is
        # identifiable as such in the same log file the real app writes to.
        config.use_mock_database()
        log.info("mock mode -- using %s, never the real database", config.DB_PATH)
    firstrun.ensure_app_config(config.CONFIG_YAML_PATH)
    config.LIBRARY_DIR = config.load_library_dir()

    ctx: WebContext = build_mock_context(config.DB_PATH) if use_mock else build_real_context(config.DB_PATH)
    ctx.start_worker()
    log.info("worker thread started")
    ctx.start_library_watcher()
    log.info("library watcher started (watching %s)", config.LIBRARY_DIR)
    ctx.addon_releases.start()
    log.info("add-on release checks started (now, then once a day)")

    app = create_app(ctx)
    from .web.network import home_network_only
    app.middleware("http")(home_network_only)
    # log_config=None is required, not cosmetic: uvicorn's default logging
    # setup builds a formatter that inspects sys.stdout.isatty() -- in a
    # windowed (console=False) PyInstaller build launched normally (no
    # inherited console), sys.stdout/sys.stderr are None, and that call
    # crashes with AttributeError before the server ever starts. Our own
    # logging_setup.configure_logging() (root logger, called above by
    # main()) is what actually captures uvicorn's log records instead.
    uvicorn_config = uvicorn.Config(app, host=args.host, port=args.port, log_config=None, proxy_headers=False)
    server = uvicorn.Server(uvicorn_config)

    single_instance.write_runtime_info(_runtime_info_path(), port=args.port)

    server_thread = threading.Thread(target=server.run, name="switchagent-http", daemon=True)
    server_thread.start()
    log.info("HTTP server starting on http://%s:%d", args.host, args.port)

    def _shutdown() -> None:
        log.info("shutdown: stopping HTTP server")
        server.should_exit = True
        server_thread.join(timeout=SHUTDOWN_JOIN_TIMEOUT_SECONDS)
        log.info("shutdown: stopping library watcher")
        ctx.stop_library_watcher()
        log.info("shutdown: stopping worker thread")
        ctx.stop_worker()
        ctx.addon_releases.stop()
        single_instance.clear_runtime_info(_runtime_info_path())
        log.info("shutdown complete")

    def _exit_for_update() -> None:
        # Ends the tray loop (or, without a tray, the server thread the
        # fallback loop waits on); _shutdown() below then runs as usual.
        from . import tray

        server.should_exit = True
        tray.close_active()

    ctx.app_updater.request_exit = _exit_for_update

    if not _wait_until_ready(server):
        log.error("HTTP server did not become ready within %.0fs -- aborting startup", READY_TIMEOUT_SECONDS)
        _shutdown()
        return 1

    log.info("HTTP server ready")
    if not args.no_browser:
        _open_browser(args.port)

    try:
        from . import tray

        from . import build_info

        # Hovering the tray icon says which build this is (a Windows tray
        # tooltip holds at most 127 characters).
        tray.run_tray_icon(
            on_open=lambda: _open_browser(args.port),
            tooltip=f"SwitchAgent {build_info.describe()}"[:127],
        )
    except Exception:
        log.exception("tray icon unavailable -- falling back to running until the HTTP server stops")
        try:
            while server_thread.is_alive():
                server_thread.join(timeout=1.0)
        except KeyboardInterrupt:
            pass

    _shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
