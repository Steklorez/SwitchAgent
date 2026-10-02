"""Runtime context shared by every route handler and the background worker
thread -- constructed once at process startup (see switchagent/cli.py's
`web` command) and stored on `app.state.ctx`.

Two backend "modes", chosen at construction time, never mixed at runtime:
  - real (default): devices are discovered live via
    switchagent.mtp.windows.enumerate_devices() and backed by
    RealMtpBackend instances, created on first sighting.
  - mock (--mock / MOCK_MTP=true, matching the original project-wide mock
    mode convention): a fixed, pre-registered set of MockMtpBackend
    instances -- no real hardware, no pywin32 calls, safe for development
    and for the automated test suite.

SQLite connections are not shared across threads: each request opens its
own short-lived connection (see switchagent/web/services.py's `with
db.open_db(ctx.db_path) as conn:` pattern), and the worker thread owns one
long-lived connection of its own. WAL mode (already enabled by
db.get_connection()) makes this safe for SQLite's usual
one-writer/many-readers model.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from .. import config, db, queue_worker
from ..mtp.base import DeviceInfo, MtpBackend
from ..mtp.errors import DeviceNotFoundError
from ..transfer import STORAGE_SD_INSTALL

log = logging.getLogger("switchagent.web")

# How long the worker thread sleeps between passes when it found nothing
# processable (see _worker_loop below) -- i.e. the worst-case delay before
# a freshly-CONFIRMED job is even noticed. Lowered from an initial 5.0s
# after a real-hardware install (docs/REAL-HARDWARE-TEST-*.md) measured a
# 4s gap between job creation and RUNNING, which combined with the Queue
# page's own polling made a genuinely fast (~15s total) install nearly
# invisible in the UI. This does not touch RealMtpBackend or any transfer/
# verification timing -- purely how often run_worker_once() gets a chance
# to notice new work.
DEFAULT_WORKER_POLL_INTERVAL_SECONDS = 2.0

# Same debounce value as cli.py's `scan --watch` (a separate, older,
# INBOX_DIR-watching CLI process -- see start_library_watcher()'s own
# docstring for why that one is untouched rather than shared code): a
# burst of filesystem events (e.g. a multi-file copy, or a downloader
# still writing) coalesces into exactly one rescan, fired this many
# seconds after the LAST observed event, not the first.
DEFAULT_LIBRARY_WATCH_DEBOUNCE_SECONDS = 5.0

# UI-007: how often refresh_devices() actually calls the (COM-expensive,
# on a real backend) list_storages() per connected device, as opposed to
# the plain connect()/overrides refresh it already does every single
# worker tick (~worker_poll_interval_seconds, cheap, no extra COM beyond
# what refresh_devices() already did anyway). Storage free/total space
# does not need second-by-second freshness -- this trades a little
# staleness for meaningfully less COM traffic on every tick.
DEFAULT_STORAGE_REFRESH_INTERVAL_SECONDS = 30.0


@dataclass
class ScanState:
    running: bool = False
    summary: Optional[dict] = None
    error: Optional[str] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    current_filename: Optional[str] = None
    # Set the moment a stop is asked for, and kept afterwards so the
    # finished state reads "Cancelled" rather than an ordinary
    # "Completed" with suspiciously small numbers.
    cancel_requested: bool = False
    # Where the pass is (scanner.scan_library_once's on_progress):
    # "walking" (done = entries seen, total unknown), "indexing" (done of
    # total), "finishing".
    phase: Optional[str] = None
    done: int = 0
    total: Optional[int] = None


class WebContext:
    def __init__(
        self,
        *,
        db_path: Path,
        registry: queue_worker.DeviceRegistry,
        discover_devices: Callable[[queue_worker.DeviceRegistry], None],
        worker_poll_interval_seconds: float = DEFAULT_WORKER_POLL_INTERVAL_SECONDS,
        library_watch_debounce_seconds: float = DEFAULT_LIBRARY_WATCH_DEBOUNCE_SECONDS,
        storage_refresh_interval_seconds: float = DEFAULT_STORAGE_REFRESH_INTERVAL_SECONDS,
    ):
        """`discover_devices(registry)` is the one mode-specific hook: it
        must register any newly-visible device_id's MtpBackend into
        `registry` (real mode: enumerate + RealMtpBackend(device_id=...);
        mock mode: a no-op, since mock backends are all pre-registered
        before the context is even constructed -- see build_real_context()/
        build_mock_context() below)."""
        self.db_path = db_path
        from .preparation import PreparationQueue
        self.preparations = PreparationQueue(db_path)
        from ..covers import CoverQueue
        self.covers = CoverQueue()
        # emuiibo on each console -- read and changed on the worker thread
        # only, in slices between jobs (see web/emuiibo_service.py).
        from .emuiibo_service import EmuiiboService, ReleaseDownloader
        self.emuiibo = EmuiiboService()
        # emuiibo's current release from GitHub, on request -- its own
        # thread, network only (see web/emuiibo_service.ReleaseDownloader).
        self.emuiibo_downloads = ReleaseDownloader(db_path, self.preparations)
        # Which release of each Add-ons catalog entry is current -- asked of
        # GitHub at start and once a day, only once start() is called (the
        # app does; tests never do). See web/addons_service.ReleaseChecker.
        from .addons_service import ReleaseChecker
        self.addon_releases = ReleaseChecker(Path(db_path).parent / "addon_releases.json")
        # SwitchAgent's own update, from the top banner (see app_update.py).
        # request_exit is set by desktop.py; without it (tests) nothing exits.
        from .. import __version__, app_update
        self.app_updater = app_update.AppUpdater(
            app_data_root=config.APP_DATA_ROOT, current_version=__version__,
            is_busy=self._is_sending)
        self.registry = registry
        self._discover_devices = discover_devices
        self.worker_poll_interval_seconds = worker_poll_interval_seconds
        self.library_watch_debounce_seconds = library_watch_debounce_seconds
        self.storage_refresh_interval_seconds = storage_refresh_interval_seconds

        self.worker_paused = threading.Event()
        self._stop_event = threading.Event()
        self._worker_thread: Optional[threading.Thread] = None
        # UI-006 "Restart worker when safe" -- see request_worker_restart()
        # below for exactly what this can and cannot do.
        self._worker_restart_requested = threading.Event()
        # Queue page "Abort" -- see request_abort(). A counter, not an
        # Event: nothing ever has to remember to clear it, so an Abort can
        # never leave the worker stuck refusing work. The worker pass that
        # was already under way when the counter moved is the one that
        # stops; the next pass reads the new value and runs normally.
        self._abort_lock = threading.Lock()
        self._abort_generation = 0
        self._abort_requested_monotonic: Optional[float] = None
        self._worker_pass_generation: Optional[int] = None

        self.scan_lock = threading.Lock()
        self.scan_state = ScanState()

        # See start_library_watcher()/stop_library_watcher() below.
        # Deliberately NOT started here -- exactly like _worker_thread
        # above, so constructing a WebContext (e.g. the test suite's
        # web_ctx fixture) never has a side effect of watching the
        # filesystem; only cli.py's `switch-agent web` command turns it on.
        # Set for the lifetime of one background scan; see cancel_scan().
        self._scan_cancel: Optional[threading.Event] = None

        self._watcher_observer = None
        self._watcher_stop_event: Optional[threading.Event] = None
        self._watcher_thread: Optional[threading.Thread] = None

        # See refresh_devices()/get_known_devices() below -- a small,
        # COM-free cache the worker thread writes to and every HTTP
        # request thread only ever reads from. Its own lock is deliberately
        # separate from (and much cheaper than) anything that could ever be
        # held for the duration of a real MTP transfer.
        self._device_cache: list[DeviceInfo] = []
        self._device_cache_lock = threading.Lock()

        # UI-007: worker-owned storage snapshot -- populated by
        # refresh_devices() (below), read by every HTTP request thread via
        # get_known_storages(), same COM-safety split as _device_cache
        # above. Refreshed only every storage_refresh_interval_seconds,
        # not every tick (see that constant's own comment).
        self._storage_cache: dict[str, list] = {}  # device_id -> list[StorageInfo]
        self._storage_cache_lock = threading.Lock()
        self._last_storage_refresh_monotonic: float = 0.0

        # Installed-games cross-reference (Library UI's "on this Switch"
        # highlight) -- same COM-safety split and refresh cadence as
        # _storage_cache above (see refresh_devices()'s should_refresh_storage
        # gate): populated worker-thread-only from
        # MtpBackend.list_installed_title_ids(), read by every HTTP request
        # thread via get_known_installed_title_ids(), never queried live
        # from a request thread. A device that stops reporting a set (DBI
        # setting turned off, transient hiccup) keeps its last-known
        # snapshot rather than being wiped -- identical "stale but honest"
        # convention as _storage_cache.
        self._installed_games_cache: dict[str, set[str]] = {}  # device_id -> base_title_ids
        self._installed_games_cache_lock = threading.Lock()
        # What each connected console is being asked right now, and what the
        # last successful "Installed games" read found -- for the Library
        # page's activity panel. That read can take a while on a console
        # with many titles, and until it finishes no card can say "On
        # Switch"; the page should say so instead of looking finished.
        self._device_activity_lock = threading.Lock()
        self._device_activity: dict[str, dict] = {}
        self._installed_read: dict[str, dict] = {}

        # DBI does not rewrite InstalledApplications.csv mid-session
        # (field-confirmed 2026-09-21: a title this app itself sends via
        # SD_INSTALL never appears in _installed_games_cache above until
        # the NEXT MTP session, no matter how many more times that same
        # stale file is re-read) -- so a freshly succeeded SD_INSTALL job
        # is the only signal available that a title just landed, until
        # reconnecting lets DBI regenerate the CSV for real. Purely an
        # additive union at read time (get_known_installed_title_ids), never
        # merged into _installed_games_cache itself -- that dict stays
        # exactly what the CSV says, so a real disagreement (e.g. the
        # install actually failed on the console) is still visible in the
        # two-axis sense this module already keeps elsewhere. Pruned to
        # live devices the same "gone the instant its device disconnects"
        # way as _installed_games_cache (see refresh_devices()) -- the next
        # connect's fresh CSV read is what's authoritative from then on.
        self._locally_confirmed_installs: dict[str, set[str]] = {}  # device_id -> base_title_ids

        # device_id -> {library item id: is it on that SD card right now} for
        # SD_FILES items (a switch/ folder, a lone .nro): the one kind whose
        # presence can be checked directly, file by file, since DBI's
        # Installed Games list only knows installed titles. Read when a
        # device connects and with every storage refresh (check_sd_files);
        # connected-only, pruned exactly like _installed_games_cache. Guarded
        # by _installed_games_cache_lock.
        self._sd_presence: dict[str, dict[int, bool]] = {}

        # Worker heartbeat (UI-001/UI-006): updated once per _worker_loop
        # iteration, whether or not that iteration found any work -- this
        # is "the worker thread is alive and ticking", a different signal
        # from any individual job's own last_progress_at. Read-only from
        # every HTTP request thread, exactly like _device_cache above.
        self._worker_heartbeat_lock = threading.Lock()
        self._worker_last_heartbeat_at: Optional[str] = None

    # -- devices --------------------------------------------------------
    #
    # IMPORTANT (found via a real-hardware incident, see docs/STATE.md's
    # Quake II investigation): refresh_devices() below calls
    # backend.connect() on live RealMtpBackend/win32com objects, which are
    # NOT thread-safe -- Shell.Application's COM objects are apartment-
    # threaded. This method must be called ONLY from the single worker
    # thread (_worker_loop). It used to also be called directly from every
    # HTTP request thread (via services.list_devices()) on every page load
    # and every /api/devices poll -- that let a request thread's connect()
    # call race the worker thread's own connect()/send_file() call on the
    # SAME RealMtpBackend instance mid-transfer, invalidating its cached
    # COM object references. Confirmed root cause: a `AttributeError:
    # <unknown>.Items` inside _get_storage_item() while a large real
    # transfer was in progress, with a concurrent /api/devices request in
    # the server log at the same moment. Fixed by having request threads
    # only ever read get_known_devices() (a plain, lock-protected list --
    # no COM, no cross-thread object access at all).

    def refresh_devices(self, conn) -> list[DeviceInfo]:
        """Discovery (mode-specific) + liveness probe (mode-agnostic: try
        connect() on every known backend, exactly the same reachability
        check queue_worker.run_worker_once() already relies on) + devices
        table bookkeeping. Called once per worker loop tick, from the
        worker thread only -- see the class-level note above. Also updates
        the cache get_known_devices() reads.

        UI-007: for every reachable device, also (a) loads its manual
        storage-name overrides from device_storage_mappings and hands them
        to the backend (cheap -- a dict of small strings, no COM involved
        in the load itself; see MtpBackend.set_storage_overrides()'s own
        docstring on why the BACKEND never queries SQLite directly) every
        single tick, and (b) refreshes the storage snapshot cache
        get_known_storages() reads, but only every
        storage_refresh_interval_seconds -- that part DOES call
        list_storages(), which on a real backend is a live, comparatively
        expensive COM round-trip per storage (System.FreeSpace/
        System.Capacity), so it is deliberately not done on every tick."""
        self._discover_devices(self.registry)
        with self._device_cache_lock:
            previously_live = {device.device_id for device in self._device_cache}
        live: list[DeviceInfo] = []
        should_refresh_storage = (
            time.monotonic() - self._last_storage_refresh_monotonic >= self.storage_refresh_interval_seconds
        )
        storage_snapshot: dict[str, list] = {}
        installed_games_snapshot: dict[str, set[str]] = {}
        sd_presence_snapshot: dict[str, dict[int, bool]] = {}
        for device_id in self.registry.known_device_ids():
            backend = self.registry.get(device_id)
            try:
                info = backend.connect()
            except DeviceNotFoundError:
                continue
            live.append(info)
            db.upsert_device_seen(conn, info.device_id, info.name)

            overrides = {
                row["raw_storage_name"]: row["logical_name"]
                for row in db.list_device_storage_mappings(conn, device_id)
            }
            backend.set_storage_overrides(overrides)

            if should_refresh_storage or device_id not in previously_live:
                self._note_device_activity(device_id, "Reading storage")
                try:
                    storage_snapshot[device_id] = backend.list_storages()
                except Exception:  # noqa: BLE001 -- one bad device must not skip the rest
                    log.exception("failed to refresh storage snapshot for a device")
                installed_games_snapshot[device_id] = set()
                self._note_device_activity(device_id, "Reading installed games")
                try:
                    ids = backend.list_installed_title_ids()
                    if ids is not None:
                        with self._device_activity_lock:
                            self._installed_read[device_id] = {"count": len(ids), "at": db.now_iso()}
                        installed_games_snapshot[device_id] = ids
                        # Persisted too (db.device_installed_titles), not just
                        # cached in memory -- so this device's confirmed set
                        # survives it disconnecting or SwitchAgent restarting
                        # (see db.set_device_installed_base_title_ids's own
                        # docstring). Only on an actual successful read: a
                        # failed/None read below must never wipe an
                        # already-persisted confirmation just because this
                        # tick couldn't reach the console.
                        db.set_device_installed_base_title_ids(conn, device_id, ids)
                except Exception:  # noqa: BLE001 -- one bad device must not skip the rest
                    log.exception("failed to refresh installed-games snapshot for a device")
                finally:
                    self._note_device_activity(device_id, None)
                sd_presence_snapshot[device_id] = check_sd_files(conn, backend)
        with self._device_cache_lock:
            self._device_cache = live
        with self._device_activity_lock:
            live_ids_now = {device.device_id for device in live}
            self._installed_read = {k: v for k, v in self._installed_read.items() if k in live_ids_now}
        if should_refresh_storage:
            self._last_storage_refresh_monotonic = time.monotonic()
        if storage_snapshot:
            with self._storage_cache_lock:
                # Only devices actually seen live this pass are updated --
                # a device that dropped out keeps its last-known snapshot
                # rather than being wiped, same "stale but honest, never
                # silently gone" convention as _device_cache/devices list
                # elsewhere in this class.
                self._storage_cache.update(storage_snapshot)
        with self._installed_games_cache_lock:
            live_ids = {device.device_id for device in live}
            self._installed_games_cache = {key: value for key, value in self._installed_games_cache.items() if key in live_ids}
            self._installed_games_cache.update(installed_games_snapshot)
            # Same prune as above, same reason: gone the instant its device
            # disconnects, never carried into a later, different session.
            self._locally_confirmed_installs = {
                key: value for key, value in self._locally_confirmed_installs.items() if key in live_ids
            }
            self._sd_presence = {key: value for key, value in self._sd_presence.items() if key in live_ids}
            self._sd_presence.update(sd_presence_snapshot)

        # By explicit request: a connection-state change in EITHER
        # direction -- a device dropping OR a fresh connect (this
        # includes the very first tick after app startup, indistinguishable
        # from a fresh connect since previously_live starts empty) --
        # invalidates whatever was queued against its previous session.
        # See services.abandon_all_jobs_for_device()'s own docstring for
        # why nothing survives except the permanent History record, and
        # PreparationQueue.clear_for_device() for the in-memory half.
        self.emuiibo.on_devices(live_ids, live_ids - previously_live)

        changed_ids = previously_live.symmetric_difference(live_ids)
        if changed_ids:
            from .services import abandon_all_jobs_for_device
            for device_id in changed_ids:
                reason = (
                    "device connection changed (disconnected) -- queue cleared" if device_id not in live_ids
                    else "device connection changed (connected) -- queue cleared"
                )
                abandon_all_jobs_for_device(conn, device_id, reason)
                self.preparations.clear_for_device(device_id)
        return live

    def _note_device_activity(self, device_id: str, phase: Optional[str]) -> None:
        with self._device_activity_lock:
            if phase is None:
                self._device_activity.pop(device_id, None)
            else:
                self._device_activity[device_id] = {"phase": phase, "since": time.monotonic()}

    def device_activity_snapshot(self) -> list[dict]:
        """One entry per console connected right now: what it is being asked
        (phase, for how long) and what its last "Installed games" read found.
        Read from memory only -- never a COM call, this runs on an HTTP
        thread (see the class-level note)."""
        with self._device_cache_lock:
            live = list(self._device_cache)
        with self._device_activity_lock:
            activity = dict(self._device_activity)
            reads = dict(self._installed_read)
        now = time.monotonic()
        result = []
        for device in live:
            current = activity.get(device.device_id)
            last = reads.get(device.device_id)
            result.append({
                "device_id": device.device_id,
                "phase": current["phase"] if current else None,
                "busy_seconds": round(now - current["since"], 1) if current else None,
                "installed_count": last["count"] if last else None,
                "installed_read_at": last["at"] if last else None,
            })
        # A console being read for the first time is not in _device_cache
        # yet (that is only replaced once the whole tick is done), but it is
        # exactly the one worth showing.
        for device_id, current in activity.items():
            if all(entry["device_id"] != device_id for entry in result):
                result.append({
                    "device_id": device_id, "phase": current["phase"],
                    "busy_seconds": round(now - current["since"], 1),
                    "installed_count": None, "installed_read_at": None,
                })
        return result

    def get_known_installed_title_ids(self) -> set[str]:
        """Union, across every currently-known device's most recent
        snapshot, of BASE title ids (title_id.classify_title_variant) DBI's
        own "InstalledApplications.csv" (see MtpBackend.
        list_installed_title_ids()) reports as installed right now -- exact
        TITLE_ID data, not a name guess. A plain cache read, no COM, safe
        from any HTTP request thread (see refresh_devices()'s own class-
        level threading note above it). Empty set if no connected device
        currently exposes that node/file (mock mode, a real device with the
        DBI setting off, or nothing connected at all) -- library rendering
        must treat that as "no information", never as "nothing is
        installed". Purely an ADDITIVE "confirmed present on a console
        right now" signal layered on top of the existing job-history-based
        INSTALLED/INSTALLED_UNVERIFIED status (services.py's
        _JOB_STATUS_TO_DISPLAY) -- never a replacement for it, the two
        answer genuinely different questions (what SwitchAgent itself once
        sent, vs. what DBI reports is there right now).

        Also unions in _locally_confirmed_installs -- see that field's own
        comment for why a title this session itself just installed must
        count here too, not just what the last CSV read happened to catch."""
        with self._installed_games_cache_lock:
            ids = {tid for ids in self._installed_games_cache.values() for tid in ids}
            ids |= {tid for ids in self._locally_confirmed_installs.values() for tid in ids}
            return ids

    def get_connected_device_ids(self) -> set[str]:
        """The devices connected right now, from the worker thread's last
        observation -- a plain cache read, like get_known_devices()."""
        with self._device_cache_lock:
            return {device.device_id for device in self._device_cache}

    def get_sd_presence(self) -> dict[int, bool]:
        """{library item id: on the SD card} across every connected device,
        for SD_FILES items that were checked (see check_sd_files). An item
        absent from the dict was not checked -- no information, never
        "not there"; present on any connected card counts as present."""
        merged: dict[int, bool] = {}
        with self._installed_games_cache_lock:
            for per_device in self._sd_presence.values():
                for item_id, present in per_device.items():
                    merged[item_id] = merged.get(item_id, False) or present
        return merged

    def note_install_job_outcome(self, conn, outcome: Optional["queue_worker.JobRunOutcome"]) -> None:
        """Called once per _worker_loop pass, right after run_worker_once()
        (see below) -- the only place that both knows a job just reached a
        terminal status AND has a `conn` to look up what it actually was.
        Folds a freshly succeeded SD_INSTALL job's base title id into
        _locally_confirmed_installs (see that field's own comment); a no-op
        for anything else (no outcome, a non-terminal-success status, a
        SD_CARD/mod job -- DBI never installs those, so there is nothing for
        its CSV to ever confirm), so calling this unconditionally on every
        pass is always safe and cheap."""
        if outcome is None or outcome.status not in ("DONE", "DONE_UNVERIFIED"):
            return
        job = db.get_job(conn, outcome.job_id)
        if job is None or job["library_item_id"] is None:
            return
        if job["target_storage"] != STORAGE_SD_INSTALL:
            # Its files were just written to that card, each one checked:
            # present until the next check_sd_files says otherwise.
            with self._installed_games_cache_lock:
                self._sd_presence.setdefault(job["target_device_id"], {})[job["library_item_id"]] = True
            return
        item = db.get_library_item_by_id(conn, job["library_item_id"])
        if item is None:
            return
        from .services import family_base_title_id
        base_title_id = family_base_title_id(item["title_id"])
        if base_title_id is None:
            return
        with self._installed_games_cache_lock:
            self._locally_confirmed_installs.setdefault(job["target_device_id"], set()).add(base_title_id)

    def get_known_storages(self, device_id: str) -> list:
        """What every HTTP request thread calls to read a device's
        storages -- a plain cache read, no COM involved, exactly like
        get_known_devices() below. At most
        ~storage_refresh_interval_seconds stale. Empty list if this device
        has never been successfully refreshed yet (never guesses)."""
        with self._storage_cache_lock:
            return list(self._storage_cache.get(device_id, []))

    def get_known_devices(self) -> list[DeviceInfo]:
        """What every HTTP request thread calls instead of
        refresh_devices() -- a plain read of the worker thread's last
        observation, no COM involved. At most ~worker_poll_interval_seconds
        stale; while a transfer is in progress the worker thread is busy
        and this simply keeps returning whatever it last saw (the device
        was reachable right before the transfer started) rather than
        blocking or guessing -- a real disconnect mid-transfer still shows
        up promptly and correctly via that job's own INTERRUPTED status,
        not via this cache."""
        with self._device_cache_lock:
            return list(self._device_cache)

    # -- background worker ------------------------------------------------

    def _is_sending(self) -> bool:
        """A job is copying to a console right now -- an app update waits
        for it rather than cutting it off."""
        with db.open_db(self.db_path) as conn:
            return conn.execute("SELECT 1 FROM jobs WHERE status='RUNNING' LIMIT 1").fetchone() is not None

    def start_worker(self) -> None:
        if self._worker_thread is not None:
            return
        self._stop_event.clear()
        self.preparations.stop.clear()
        self._worker_thread = threading.Thread(target=self._worker_loop, name="switchagent-worker", daemon=True)
        self._worker_thread.start()

    def stop_worker(self) -> None:
        self.preparations.stop.set()
        self._stop_event.set()
        if self._worker_thread is not None:
            self._worker_thread.join(timeout=10.0)
            self._worker_thread = None

    @property
    def worker_running(self) -> bool:
        return self._worker_thread is not None and self._worker_thread.is_alive()

    def _touch_worker_heartbeat(self) -> None:
        with self._worker_heartbeat_lock:
            self._worker_last_heartbeat_at = db.now_iso()

    @property
    def worker_last_heartbeat_at(self) -> Optional[str]:
        with self._worker_heartbeat_lock:
            return self._worker_last_heartbeat_at

    def request_worker_restart(self) -> None:
        """UI-006 'Restart worker when safe'. NEVER interrupts an in-flight
        COM/MTP call -- run_worker_once() always runs at most one job to
        one of its own terminal outcomes (or finds nothing to do) before
        returning control to _worker_loop, and this flag is only ever
        checked at the very TOP of the loop's NEXT iteration, after that
        return has already happened. No Thread.kill()/TerminateThread,
        nothing forcibly torn down -- just a clean, in-place reopen of the
        worker's own DB connection at its own next safe point.

        If the worker is genuinely wedged inside a single blocking COM
        call that never returns at all, this flag will simply never be
        reached -- there is no way to safely interrupt that (real MTP/Shell
        COM calls have no cancellation mechanism this project's backend
        uses, see mtp/windows.py's module docstring on PerformOperations()'s
        own blocking behavior). is_worker_restart_pending() lets the UI
        honestly show "queued, waiting for a safe point" rather than
        implying this always resolves quickly; a manual application
        restart may still be needed in that case, and the UI must say so,
        never claim this button fixes a truly hung call."""
        self._worker_restart_requested.set()

    @property
    def is_worker_restart_pending(self) -> bool:
        return self._worker_restart_requested.is_set()

    def request_abort(self) -> None:
        """Queue page "Abort", the running-transfer half (the queued half
        is services.abort_all(), which calls this LAST, after it has
        cancelled every job the worker could otherwise pick up next).

        Stops the worker pass under way right now at its next safe point
        (queue_worker._run_job_transfer's docstring lists them) -- never by
        force: no thread is killed and no COM call is interrupted. Worker
        passes that start afterwards are unaffected, so the worker is ready
        for new installs the moment the aborted one has stopped; Pause is a
        separate switch and is left exactly as it was."""
        with self._abort_lock:
            self._abort_generation += 1
            self._abort_requested_monotonic = time.monotonic()

    def _abort_checker(self, generation: int):
        return lambda: self._abort_generation != generation

    @property
    def abort_stopping_seconds(self) -> Optional[float]:
        """How long an Abort has been waiting for the worker pass it
        targets to stop; None when there is nothing left to stop (no Abort,
        or the aborted pass already returned). The Queue page shows the
        running row as "Stopping" for exactly this long."""
        with self._abort_lock:
            active = self._worker_pass_generation
            if active is None or active == self._abort_generation or self._abort_requested_monotonic is None:
                return None
            return round(time.monotonic() - self._abort_requested_monotonic, 1)

    def _worker_loop(self) -> None:
        conn = db.get_connection(self.db_path)
        db.init_db(conn)
        recovered = db.recover_stale_running_jobs(conn)
        if recovered:
            log.info("worker startup: recovered %d stale RUNNING job(s) -> INTERRUPTED", recovered)
        # Which game each switch/ folder, amiibo collection and sealed
        # forwarder belongs to is otherwise only decided at the end of a
        # scan -- and nothing scans at start. After an update that learned a
        # new rule, the Library went on showing the old grouping until
        # something happened to trigger one (a port showed as two cards for
        # ~20s, 2026-09-28). A pass over the index only, no disk walk.
        from .. import scanner as scanner_mod
        regrouped = scanner_mod.assign_sd_file_owners(conn)
        if regrouped:
            log.info("worker startup: regrouped %d library item(s)", regrouped)
        # Staged but never confirmed, and now unconfirmable -- the in-memory
        # preparation that owned these did not survive the restart. Their
        # batches' staging is released here the same way preparation.py's
        # own _abandon_prepared() does it.
        unconfirmed = conn.execute(
            "SELECT DISTINCT batch_id FROM jobs WHERE status = 'PENDING_CONFIRM'"
        ).fetchall()
        abandoned = db.abandon_unconfirmed_jobs(conn)
        if abandoned:
            from ..work_cleanup import cleanup_batch_if_all_done

            log.info("worker startup: abandoned %d unconfirmed job(s) -> FAILED", abandoned)
            for row in unconfirmed:
                cleanup_batch_if_all_done(conn, row["batch_id"])
        try:
            while not self._stop_event.is_set():
                if self._worker_restart_requested.is_set():
                    self._worker_restart_requested.clear()
                    log.info("worker restart requested -- reopening DB connection at a safe point")
                    conn.close()
                    conn = db.get_connection(self.db_path)
                    db.init_db(conn)
                self._touch_worker_heartbeat()
                try:
                    self.refresh_devices(conn)
                    if self.worker_paused.is_set():
                        self._stop_event.wait(1.0)
                        continue
                    # Read BEFORE run_worker_once lists the jobs: an Abort
                    # after this point may have cancelled rows that list
                    # still holds, and must stop this pass (see
                    # request_abort()).
                    with self._abort_lock:
                        generation = self._abort_generation
                        self._worker_pass_generation = generation
                    try:
                        outcome = queue_worker.run_worker_once(
                            conn, self.registry, should_abort=self._abort_checker(generation),
                        )
                    finally:
                        with self._abort_lock:
                            self._worker_pass_generation = None
                    self.note_install_job_outcome(conn, outcome)
                    self.emuiibo.note_job(conn, outcome.job_id if outcome else None)
                    # No job to run: time for emuiibo's slice (a read or a
                    # removal carries on from where it stopped). While one
                    # is under way the loop does not sleep, so it finishes
                    # at MTP speed -- and a job confirmed meanwhile still
                    # starts within one slice.
                    more_emuiibo_work = outcome is None and self.emuiibo.step(conn, self.registry)
                except Exception:  # noqa: BLE001 -- one bad iteration must not kill the worker thread
                    log.exception("worker loop iteration failed")
                    outcome = None
                    more_emuiibo_work = False
                if outcome is None and not more_emuiibo_work:
                    self._stop_event.wait(self.worker_poll_interval_seconds)
        finally:
            conn.close()

    # -- library scan (point 16: must not block the UI) --------------------

    def run_scan_in_background(self) -> bool:
        """Returns False without starting anything if a scan is already
        running -- callers (the /api/scan route) surface that as "scan
        already in progress", never queue up a second concurrent scan."""
        with self.scan_lock:
            if self.scan_state.running:
                return False
            self.scan_state = ScanState(running=True, started_at=db.now_iso())
            # One event per scan, never reused: a stop asked for during the
            # previous pass must not silently abort the next one.
            cancel = threading.Event()
            self._scan_cancel = cancel

        def _on_file(path: str) -> None:
            with self.scan_lock:
                self.scan_state.current_filename = path

        def _on_progress(phase: str, done: int, total: Optional[int]) -> None:
            with self.scan_lock:
                self.scan_state.phase = phase
                self.scan_state.done = done
                self.scan_state.total = total

        def _run():
            from .. import scanner
            try:
                conn = db.get_connection(self.db_path)
                db.init_db(conn)
                try:
                    summary = scanner.scan_library_once(
                        conn, on_file=_on_file, should_stop=cancel.is_set, on_progress=_on_progress,
                    )
                finally:
                    conn.close()
                with self.scan_lock:
                    self.scan_state.summary = summary
                    self.scan_state.error = summary.get("error")
            except Exception as exc:  # noqa: BLE001 -- must not crash the thread silently
                log.exception("library scan failed")
                with self.scan_lock:
                    self.scan_state.error = str(exc)
            finally:
                with self.scan_lock:
                    self.scan_state.running = False
                    self.scan_state.finished_at = db.now_iso()
                    self.scan_state.current_filename = None
                    self._scan_cancel = None

        threading.Thread(target=_run, name="switchagent-scan", daemon=True).start()
        return True

    def cancel_scan(self, *, wait_seconds: float = 0.0) -> bool:
        """Asks a running scan to stop and returns whether there was one.

        Cooperative, not a kill: the flag is polled by scan_library_once()
        at every step of its walk, so the scan unwinds at a point where
        the database is consistent and whatever it had already indexed is
        kept (see that function on why the whole-library merge/delete
        passes are skipped on a cancelled pass). `wait_seconds` blocks
        until the scan thread has actually noticed -- set_library_dir()
        uses it so "remove this folder" cannot race the very scan of that
        folder it is meant to stop.

        A stop can outlive one poll interval on a slow share: the flag is
        checked between filesystem calls, and a single stat()/rglob step
        over an unresponsive SMB mount can block for as long as the OS
        takes. It always stops; it is not always instant."""
        with self.scan_lock:
            if not self.scan_state.running or self._scan_cancel is None:
                return False
            cancel = self._scan_cancel
            self.scan_state.cancel_requested = True
        cancel.set()
        if wait_seconds:
            deadline = time.monotonic() + wait_seconds
            while time.monotonic() < deadline:
                with self.scan_lock:
                    if not self.scan_state.running:
                        break
                time.sleep(0.05)
        return True

    def scan_status_snapshot(self) -> dict:
        """The full GET /api/scan/status payload (point 11). Adds an
        explicit, derived `status` ("idle"/"running"/"completed"/"failed" --
        ScanState itself only tracks the raw booleans/timestamps a scan
        naturally produces, not a single summary label) and `elapsed_seconds`
        (computed against "now" while still running, fixed once finished) on
        top of the pre-existing raw fields, which stay unchanged for
        whatever already reads them directly (e.g. library.js's
        pollScanStatus, which only ever looked at `running`/`error`/
        `summary`)."""
        with self.scan_lock:
            state = self.scan_state
            running, error = state.running, state.error
            started_at, finished_at = state.started_at, state.finished_at
            summary, current_filename = state.summary, state.current_filename
            cancel_requested = state.cancel_requested
            phase, done, total = state.phase, state.done, state.total

        cancelled = bool(summary and summary.get("cancelled"))
        if started_at is None:
            status = "idle"
        elif running:
            status = "running"
        elif error:
            status = "failed"
        elif cancelled:
            # Not "completed": a cancelled pass stopped partway, and
            # reporting its smaller numbers as a finished scan would read
            # as "your library shrank" rather than "you stopped it".
            status = "cancelled"
        else:
            status = "completed"

        elapsed_seconds = None
        if started_at is not None:
            start_dt = datetime.fromisoformat(started_at)
            end_dt = datetime.fromisoformat(finished_at) if finished_at else datetime.now(timezone.utc)
            elapsed_seconds = (end_dt - start_dt).total_seconds()

        return {
            "status": status, "running": running, "summary": summary, "error": error,
            "started_at": started_at, "finished_at": finished_at,
            "current_filename": current_filename, "elapsed_seconds": elapsed_seconds,
            "cancel_requested": cancel_requested, "cancelled": cancelled,
            "phase": phase if running else None, "done": done, "total": total,
        }

    # -- library filesystem watcher (point 12) ------------------------------
    #
    # cli.py's `switch-agent scan --watch` already does exactly this same
    # watchdog + debounce trick, but for config.INBOX_DIR (the older,
    # separate Stage-2 CLI pipeline, via scanner.scan_once()) as a
    # standalone blocking process with its own DB connection -- it is
    # deliberately left untouched here (a working, independent feature;
    # nothing about it needs to change). The Web UI's own Library folder
    # (config.LIBRARY_DIR) had NO automatic watcher at all before this --
    # only the manual Rescan button (run_scan_in_background(), point 16) --
    # found while auditing this exact point. This reuses BOTH the proven
    # watchdog+debounce pattern AND (unlike the CLI path) Phase 8's actual
    # async scan machinery instead of a second, separate scan call: a
    # debounced filesystem change simply calls run_scan_in_background(),
    # the exact same method POST /api/scan calls -- so "scanning never
    # creates a job" and "never blocks other pages" (points 13/16) hold
    # here for free, already proven by that method's own tests, and if a
    # manual Rescan is already in flight when the debounce fires,
    # run_scan_in_background()'s own "already running -> False" guard
    # applies exactly as it would to a second concurrent manual click.

    def start_library_watcher(self) -> None:
        """No-op if already running (mirrors start_worker()'s own
        idempotence) or if config.LIBRARY_DIR doesn't currently exist
        (mirrors scan_library_once()'s own tolerance of a missing
        directory -- watchdog's Observer.schedule() itself has no such
        tolerance and would raise, so this must check first rather than
        let that surprise a server startup).

        Also a no-op while no Library folder has actually been CHOSEN.
        config.LIBRARY_DIR falls back to the Windows Downloads folder when
        config.yaml names none (config.load_library_dir), which is a fine
        suggestion to prefill Settings with and a terrible thing to start
        walking and re-walking unasked: a real Downloads directory is
        mostly junk, takes minutes per pass, and none of it is Switch
        content. The app already distinguishes the two cases -- an
        unconfigured folder is exactly what raises onboarding's "Choose
        Library Folder" banner -- so nothing here needs to guess; it just
        waits for the answer."""
        if self._watcher_observer is not None:
            return
        from .. import preferences
        if not preferences.load()["auto_scan"]:
            return
        if not config.library_dir_info(config.CONFIG_YAML_PATH).configured:
            log.info("library watcher: no Library folder chosen yet, not watching anything")
            return
        roots = [root for root in config.library_dirs() if root.is_dir()]
        if not roots:
            log.warning("library watcher: %s does not exist yet, not watching", config.LIBRARY_DIR)

        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer

        state = {"last_event": 0.0, "pending": False}

        class _Handler(FileSystemEventHandler):
            def on_any_event(self, event):  # noqa: ANN001 -- watchdog's own signature
                state["last_event"] = time.time()
                state["pending"] = True

        observer = Observer()
        for root in roots:
            observer.schedule(_Handler(), str(root), recursive=True)
        observer.start()
        self._watcher_observer = observer
        self._watcher_stop_event = threading.Event()

        def _debounce_loop():
            stop_event = self._watcher_stop_event
            last_scan = time.monotonic()
            while not stop_event.is_set():
                stop_event.wait(1.0)
                if stop_event.is_set():
                    break
                due = time.monotonic() - last_scan >= preferences.load()["scan_interval"]
                changed = state["pending"] and (time.time() - state["last_event"]) >= self.library_watch_debounce_seconds
                if due or changed:
                    if self.run_scan_in_background():
                        state["pending"] = False
                        last_scan = time.monotonic()

        self._watcher_thread = threading.Thread(target=_debounce_loop, name="switchagent-watcher", daemon=True)
        self._watcher_thread.start()

    def stop_library_watcher(self) -> None:
        if self._watcher_observer is not None:
            self._watcher_observer.stop()
            self._watcher_observer.join()
            # Set for the lifetime of one background scan; see cancel_scan().
        self._scan_cancel: Optional[threading.Event] = None

        self._watcher_observer = None
        if self._watcher_stop_event is not None:
            self._watcher_stop_event.set()
        if self._watcher_thread is not None:
            self._watcher_thread.join(timeout=5.0)
            self._watcher_thread = None
        self._watcher_stop_event = None

    @property
    def library_watcher_running(self) -> bool:
        return self._watcher_observer is not None


def _discover_real_devices(registry: queue_worker.DeviceRegistry) -> None:
    from ..mtp.windows import RealMtpBackend, enumerate_devices

    for info in enumerate_devices():
        if registry.get(info.device_id) is None:
            registry.register(info.device_id, RealMtpBackend(device_id=info.device_id))


def build_real_context(db_path: Path) -> WebContext:
    return WebContext(db_path=db_path, registry=queue_worker.DeviceRegistry(), discover_devices=_discover_real_devices)


def mock_overlay_bytes(version: str) -> bytes:
    """The smallest .nro emuiibo.overlay_version() reads a version from:
    header, asset section, and a NACP holding `version` at 0x3060."""
    import struct

    nro_size = 0x80
    data = bytearray(nro_size)
    data[0x10:0x14] = b"NRO0"
    struct.pack_into("<I", data, 0x18, nro_size)
    nacp = bytearray(0x4000)
    nacp[0:7] = b"emuiibo"
    nacp[0x3060:0x3060 + len(version)] = version.encode("ascii")
    asset = bytearray(b"ASET") + struct.pack("<IQQQQ", 0, 0, 0, 0x38, len(nacp))
    asset += bytes(0x38 - len(asset))
    return bytes(data + asset + nacp)


def _seed_mock_emuiibo(sd) -> None:
    """`web --mock`'s parent Switch as a real one with emuiibo 0.6.3 looks
    (see emuiibo.py): the sysmodule, its overlay, Tesla, a few amiibo --
    one with game save data, one a favorite -- so the Amiibo page shows
    something true to life without hardware. The child Switch stays bare."""
    import json as json_mod

    from .. import emuiibo

    def put(path: str, data: bytes = b"") -> None:
        sd.ensure_directory(path.rpartition("/")[0])
        sd.write_file(path, data)

    put(f"{emuiibo.SYSMODULE_DIR}/exefs.nsp", b"\0" * 64)
    put(f"{emuiibo.SYSMODULE_DIR}/flags/boot2.flag")
    put(emuiibo.OVERLAY_FILE, mock_overlay_bytes("0.6.3"))
    put(f"{emuiibo.OVLLOADER_DIR}/exefs.nsp", b"\0" * 64)
    put(f"{emuiibo.OVLLOADER_DIR}/flags/boot2.flag")
    put(emuiibo.TESLA_MENU_FILE, b"\0" * 64)
    put(emuiibo.STATUS_ON_FLAG)
    figures = {
        "Animal Crossing/Isabelle": ("Isabelle", [2, 1, 0, 1, 5], True),
        "Animal Crossing/Tom Nook": ("Tom Nook", [2, 0, 0, 1, 5], False),
        "Super Smash Bros/Mario": ("Mario", [0, 0, 0, 2, 0], False),
    }
    for path, (name, (gcid, variant, ftype, model, series), save) in figures.items():
        folder = f"{emuiibo.AMIIBO_DIR}/{path}"
        doc = {
            "name": name, "write_counter": 0, "version": 0, "mii_charinfo_file": "mii-charinfo.bin",
            "first_write_date": {"y": 2026, "m": 9, "d": 1}, "last_write_date": {"y": 2026, "m": 9, "d": 1},
            "id": {"game_character_id": gcid, "character_variant": variant, "figure_type": ftype,
                   "model_number": model, "series": series},
            "uuid": [1, 2, 3, 4, 5, 6, len(name), 0, 0, 0], "use_random_uuid": False,
        }
        put(f"{folder}/{emuiibo.AMIIBO_JSON}", json_mod.dumps(doc, indent=2).encode())
        put(f"{folder}/{emuiibo.AMIIBO_FLAG}")
        put(f"{folder}/areas.json", b'{"areas": [], "current_area_access_id": 0}')
        if save:
            put(f"{folder}/areas/0x38600500.bin", b"\0" * 216)
    put(emuiibo.FAVORITES_FILE, b"sdmc:/emuiibo/amiibo/Super Smash Bros/Mario\n")


def build_mock_context(
    db_path: Path, *, device_ids: Optional[list[str]] = None, seed_emuiibo: bool = False,
) -> WebContext:
    """Development/test mode -- no real hardware, no pywin32. Pre-registers
    a small fixed set of MockMtpBackend instances (default: two, named the
    same way tests/test_queue_worker.py already does, for a consistent
    "parent + child Switch" story across the codebase) so discover_devices
    has nothing to do at runtime -- the registry is already complete."""
    from ..mtp.mock import MockMtpBackend

    registry = queue_worker.DeviceRegistry()
    ids = device_ids or ["mock-switch-parent", "mock-switch-child"]
    names = {"mock-switch-parent": "Parent's Switch (mock)", "mock-switch-child": "Child's Switch (mock)"}
    # A plausible SD_CARD size/free-space pair -- only SD_CARD is a real
    # filesystem (see mtp/windows.py's SIZE_VERIFIABLE_STORAGES), so it's
    # the only storage worth giving believable numbers to here; SD_INSTALL
    # stays byte-less, same as real hardware reports it. Lets the Library
    # page's header space bar (used + about-to-be-selected) render
    # something in `switch-agent web --mock` without real hardware.
    sd_card_total_bytes = 256 * 1024**3
    sd_card_free_bytes = sd_card_total_bytes - 45 * 1024**3
    for device_id in ids:
        backend = MockMtpBackend(device_id=device_id, device_name=names.get(device_id, device_id))
        backend.add_storage("SD_CARD", free_bytes=sd_card_free_bytes, total_bytes=sd_card_total_bytes)
        backend.add_storage("SD_INSTALL")
        if seed_emuiibo and device_id == "mock-switch-parent":
            _seed_mock_emuiibo(backend.storage_tree("SD_CARD"))
        registry.register(device_id, backend)

    def _noop_discover(_registry: queue_worker.DeviceRegistry) -> None:
        return None

    return WebContext(db_path=db_path, registry=registry, discover_devices=_noop_discover)


def check_sd_files(conn, backend) -> dict[int, bool]:
    """{library item id: are its files on this device's SD card} for every
    SD_FILES item in the library -- what its .nro (or, with none, its
    switch/<app> folders) is checked by, since that is what the Homebrew
    Menu goes by. Stops at the first failure and returns what it has: a
    console that does not answer one lookup will not answer the next, and
    an item it never got to is simply unknown, never "not there"."""
    from .. import sd_files
    from ..model import ContentType
    from ..transfer import STORAGE_SD_CARD

    result: dict[int, bool] = {}
    for row in db.list_library_items(conn):
        if row["content_type"] != ContentType.SD_FILES.value or row["status"] == db.LIBRARY_ITEM_RETIRED:
            continue
        summary = sd_files.sd_summary_of(row)
        paths = (summary.nro or summary.apps) if summary else ()
        if not paths:
            continue
        try:
            result[row["id"]] = all(backend.exists(STORAGE_SD_CARD, path) for path in paths)
        except Exception:  # noqa: BLE001 -- a presence check must never break refresh_devices
            log.warning("SD presence check stopped (%d item(s) checked)", len(result), exc_info=True)
            break
    return result
