"""SQLite storage for SwitchAgent.

Single-file database living at data/switchagent.db inside the project folder.
Two jobs:
  1. Remember what we already analyzed in inbox/, so unchanged files are not
     re-hashed on every scan.
  2. Remember what was already sent to the Switch (by content hash, not by
     filename), so the same content is never queued twice.

Kept deliberately dumb: no ORM, plain sqlite3 + row factory. Schema is
created with CREATE TABLE IF NOT EXISTS, safe to call init_db() every start.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS inbox_items (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    relative_path   TEXT NOT NULL UNIQUE,
    item_type       TEXT NOT NULL CHECK(item_type IN ('FILE', 'MOD_FOLDER')),
    file_type       TEXT NOT NULL,
    size            INTEGER NOT NULL,
    mtime           REAL NOT NULL,
    content_hash    TEXT,
    title_id        TEXT,
    title_id_source TEXT,
    status          TEXT NOT NULL DEFAULT 'NEW',
    suggested_action TEXT,
    suggested_target TEXT,
    note            TEXT,
    error           TEXT,
    first_seen_at   TEXT NOT NULL,
    last_scanned_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_inbox_items_hash ON inbox_items(content_hash);

CREATE TABLE IF NOT EXISTS jobs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    inbox_item_id   INTEGER NOT NULL REFERENCES inbox_items(id),
    action          TEXT NOT NULL,
    target_storage  TEXT,
    status          TEXT NOT NULL DEFAULT 'PENDING_CONFIRM',
    bytes_total     INTEGER,
    bytes_done      INTEGER DEFAULT 0,
    attempt_count   INTEGER DEFAULT 0,
    error           TEXT,
    created_at      TEXT NOT NULL,
    started_at      TEXT,
    finished_at     TEXT
);

CREATE INDEX IF NOT EXISTS idx_jobs_item ON jobs(inbox_item_id);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);

CREATE TABLE IF NOT EXISTS job_log (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id  INTEGER NOT NULL REFERENCES jobs(id),
    ts      TEXT NOT NULL,
    message TEXT NOT NULL
);

-- Web UI (see docs/WEB-UI.md). Deliberately separate from inbox_items,
-- not a rename/reuse of it: inbox_items indexes inbox/ (Stage 2's
-- drop-a-file workflow), library_items indexes an arbitrary configured
-- directory (config.LIBRARY_DIR, default D:/shared/Download) that the
-- user never has to move files into. Same classification vocabulary
-- (content_type/package_format/title_id/...) and the same underlying
-- scanner.classify_file()/hash_mod_folder() logic, applied to a different
-- root and keyed by absolute path instead of inbox-relative path, because
-- the two roots must never collide or be confused with each other (see
-- switchagent/db.py's "source file / logical title / device / job /
-- installation history" separation, point 35 of the Web UI spec).
CREATE TABLE IF NOT EXISTS library_items (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    absolute_path       TEXT NOT NULL UNIQUE,
    item_type           TEXT NOT NULL CHECK(item_type IN ('FILE', 'MOD_FOLDER')),
    file_type           TEXT NOT NULL,
    content_type        TEXT,
    package_format      TEXT,
    size                INTEGER NOT NULL,
    mtime               REAL NOT NULL,
    content_hash        TEXT,
    title_id            TEXT,
    title_id_source     TEXT,
    title_id_confident  INTEGER NOT NULL DEFAULT 0,
    status              TEXT NOT NULL DEFAULT 'AVAILABLE',
    suggested_action    TEXT,
    suggested_target    TEXT,
    note                TEXT,
    error               TEXT,
    details_json        TEXT,
    first_seen_at       TEXT NOT NULL,
    last_scanned_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_library_items_hash ON library_items(content_hash);
CREATE INDEX IF NOT EXISTS idx_library_items_title ON library_items(title_id);

-- Known MTP devices (see mtp/base.py's DeviceInfo.device_id -- this table
-- only ever stores that same stable identity, never a raw serial by
-- itself). friendly_name is purely cosmetic and user-editable; device_id
-- is not and never changes when friendly_name does (see docs/WEB-UI.md).
CREATE TABLE IF NOT EXISTS devices (
    device_id                TEXT PRIMARY KEY,
    friendly_name            TEXT,
    last_known_display_name  TEXT,
    first_seen_at            TEXT NOT NULL,
    last_seen_at             TEXT NOT NULL
);

-- Confirmed-by-us install/copy history, independent of both current device
-- state (which we mostly cannot query -- no "Installed games" node on the
-- hardware this project was developed against) and of whether the source
-- file still exists locally. One row per job reaching a terminal status
-- (DONE / DONE_UNVERIFIED / FAILED / INTERRUPTED / SOURCE_CHANGED /
-- DESTINATION_CONFLICT) -- see queue_worker.py. This is what "Installed by
-- SwitchAgent" (point 13 of the Web UI spec) is grounded in -- a claim
-- about OUR OWN records, never a claim about current on-device truth.
CREATE TABLE IF NOT EXISTS install_history (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id             INTEGER NOT NULL REFERENCES jobs(id),
    title_id           TEXT,
    display_name       TEXT NOT NULL,
    target_device_id   TEXT NOT NULL,
    target_storage     TEXT,
    outcome            TEXT NOT NULL,
    error              TEXT,
    bytes_total        INTEGER,
    created_at         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_install_history_job ON install_history(job_id);
CREATE INDEX IF NOT EXISTS idx_install_history_device ON install_history(target_device_id);

-- UI-002 (batch installations): one row per "Confirm Install" click, even
-- if only one item was selected -- see queue_worker.create_and_confirm_jobs
-- equivalent (web/services.py). jobs.batch_id (nullable, see
-- _JOBS_MIGRATIONS below) links back here. Historical jobs created before
-- this feature existed keep batch_id = NULL forever -- they are never
-- retroactively assigned an invented batch; the UI shows them ungrouped.
CREATE TABLE IF NOT EXISTS installation_batches (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    target_device_id  TEXT NOT NULL,
    created_at        TEXT NOT NULL
);

-- UI-007 (device storage mapping): a user-entered MANUAL override of the
-- AUTO logical-name mapping (see switchagent/mtp/windows.py's
-- logical_storage_name()), keyed by device + the raw DBI-reported storage
-- name. No MTP-visible stable storage id exists on the hardware this
-- project targets (see mtp/windows.py's module docstring), so raw display
-- name is the best available key -- one row = one explicit user decision;
-- AUTO mapping is never itself persisted here, only a deviation from it.
CREATE TABLE IF NOT EXISTS device_storage_mappings (
    device_id         TEXT NOT NULL,
    raw_storage_name  TEXT NOT NULL,
    logical_name      TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    PRIMARY KEY (device_id, raw_storage_name)
);

-- DBI's own live "InstalledApplications.csv" confirmation (see
-- mtp.windows.list_installed_title_ids()), persisted per device so it
-- survives that device disconnecting or the app restarting. Before this
-- table, WebContext kept this ONLY in an in-memory cache that was pruned
-- the moment a device dropped out of refresh_devices()'s `live` list --
-- so "confirmed on this Switch" silently reverted to "unknown" on every
-- unplug, even though nothing about the console itself had changed. One
-- row per (device, base_title_id) DBI confirms as of the most recent
-- successful CSV read; wholesale-replaced for that device on every such
-- read (see set_device_installed_base_title_ids), never merged -- a
-- title genuinely removed from the console must stop being claimed here
-- too, the next time that device is actually reachable and rescanned.
CREATE TABLE IF NOT EXISTS device_installed_titles (
    device_id       TEXT NOT NULL,
    base_title_id   TEXT NOT NULL,
    confirmed_at    TEXT NOT NULL,
    PRIMARY KEY (device_id, base_title_id)
);

-- emuiibo on a console, as last READ off it over MTP (switchagent/emuiibo.py
-- read_device): which of its parts are there, the overlay's version, and
-- every virtual amiibo in emuiibo/amiibo/. What the console held when it was
-- last looked at, never what SwitchAgent believes it sent -- replaced as a
-- whole by every complete read, kept across disconnects and restarts so the
-- Amiibo page still says something true about an unplugged console (with
-- the time it was read).
CREATE TABLE IF NOT EXISTS device_emuiibo (
    device_id   TEXT PRIMARY KEY,
    state_json  TEXT NOT NULL,
    read_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS device_amiibo (
    device_id   TEXT NOT NULL,
    path        TEXT NOT NULL,
    data_json   TEXT NOT NULL,
    PRIMARY KEY (device_id, path)
);

-- The Add-ons catalog's utilities on a console, as last READ off it
-- (switchagent/addons.py read_device): which of their files are there, the
-- version inside each overlay/app read back, whether it runs kefir. Like
-- device_emuiibo: what the console held, never what SwitchAgent sent.
CREATE TABLE IF NOT EXISTS device_addons (
    device_id   TEXT PRIMARY KEY,
    state_json  TEXT NOT NULL,
    read_at     TEXT NOT NULL
);
"""

# Full job status vocabulary. DEVICE_UNAVAILABLE and FAILED were added in
# stage 4; SOURCE_CHANGED and DESTINATION_CONFLICT were added in stage 4.1
# (see docs/STAGE4.1.md for exactly what each means and when it's reached).
# DONE_UNVERIFIED was added alongside the SD_INSTALL semantics fix (see
# docs/STAGE5B-REAL-MTP.md): reached when every manifest file transferred
# with TransferStatus.UNVERIFIED (the backend proved the device accepted
# the transfer but could not prove -- or disprove -- DBI's own install
# completion, e.g. SD_INSTALL). Deliberately distinct from DONE, which
# still means "verified" wherever verification is actually possible (e.g.
# SD_CARD) -- DONE_UNVERIFIED must never be silently treated as equivalent
# to a verified DONE by callers/UI.
#
# WAITING_FOR_BASE and BLOCKED_BY_DEPENDENCY were added after a real
# install-order incident (see docs/STATE.md's Quake II investigation --
# an Update was allowed to install while its Base Game job was still
# outstanding, and the base game itself never completed). Enforced by
# queue_worker.run_worker_once()'s dependency check, never by
# RealMtpBackend or the transfer layer. WAITING_FOR_BASE: this job's base
# game has not (yet) completed successfully -- may still resolve itself,
# the worker just skips it and re-checks next pass, exactly like
# DEVICE_UNAVAILABLE. BLOCKED_BY_DEPENDENCY: the base game definitively
# failed/was interrupted/conflicted -- this job will never auto-run; a
# fresh job (new preview + confirmation) is required after the base issue
# is resolved, same "no automatic retry of a definitively bad situation"
# principle as SOURCE_CHANGED/DESTINATION_CONFLICT.
#
# WAITING_FOR_DEVICE (UI-005 requirement): distinct from DEVICE_UNAVAILABLE
# on purpose -- see queue_worker.py's _decide_device_absence_status() for
# the exact eligibility rule and docs/PACKAGING.md-adjacent design note.
# Short version: a job that has NEVER started a transfer (attempt_count==0,
# nothing in progress.json) and whose target device is merely not plugged
# in yet is WAITING_FOR_DEVICE -- non-terminal, automatically re-checked
# every worker pass, no manual retry needed, self-resolving the moment its
# SAME target_device_id reappears. A job that already had an attempt or
# partial delivery keeps the existing, more conservative
# DEVICE_UNAVAILABLE (manual retry required) -- this is a deliberate,
# narrower carve-out, not a semantic replacement of DEVICE_UNAVAILABLE.
#
# ARCH-002: VERIFYING is RESERVED, not dead-and-forgotten -- confirmed by
# audit (2026-09-12) that no code anywhere in this project ever assigns it
# to a job (grep for `"VERIFYING"` as an update_job_status() argument
# finds none; it only appears in *display*-mapping dicts like
# web/services.py's _JOB_STATUS_TO_DISPLAY/_STATUS_BUCKETS, which must
# stay total over JOB_STATUSES but don't imply the value is ever reached).
# It is intentionally KEPT (not removed) for two reasons: (1) removing a
# value from this tuple is a compatibility risk for zero benefit -- a
# future real backend that needs a genuine multi-step "sent, now
# confirming" phase (this project's current real backend verifies
# synchronously inside send_file() itself, see mtp/windows.py, so it has
# never needed an intermediate persisted status) has an obvious, already-
# wired-through slot to use instead of inventing a new one; (2) an
# existing/upgraded database could theoretically already contain a row
# with this status from some earlier experiment -- removing the value
# would make such a row fail every status-vocabulary assumption
# throughout the codebase for no reason. Every consumer of JOB_STATUSES
# (display maps, bucket maps, cancelable/retryable-status tuples) already
# tolerates an entry with no special-case handling gracefully (falls back
# to a generic "waiting" bucket / the raw status string for display) --
# see tests/test_web_db.py's migration-safety coverage.
JOB_STATUSES = (
    "PENDING_CONFIRM", "CONFIRMED", "DEVICE_UNAVAILABLE", "WAITING_FOR_DEVICE", "RUNNING",
    "VERIFYING", "INTERRUPTED", "FAILED", "SOURCE_CHANGED",
    "DESTINATION_CONFLICT", "DONE", "DONE_UNVERIFIED",
    "WAITING_FOR_BASE", "BLOCKED_BY_DEPENDENCY",
)

# Job statuses that mean "this content is already spoken for" -- a duplicate
# job for the same content_hash should not be created while one of these is
# active, and DONE/DONE_UNVERIFIED mean it was already (verifiably or not)
# delivered. FAILED is deliberately excluded: a definitively failed attempt
# should not block re-queueing the same content. SOURCE_CHANGED,
# DESTINATION_CONFLICT, WAITING_FOR_BASE and BLOCKED_BY_DEPENDENCY are
# included: all still represent unresolved, unfinished business for this
# content, not a clean "try again from scratch" case.
ACTIVE_OR_DONE_JOB_STATUSES = (
    "PENDING_CONFIRM", "CONFIRMED", "DEVICE_UNAVAILABLE", "WAITING_FOR_DEVICE", "RUNNING",
    "VERIFYING", "INTERRUPTED", "SOURCE_CHANGED", "DESTINATION_CONFLICT",
    "DONE", "DONE_UNVERIFIED", "WAITING_FOR_BASE", "BLOCKED_BY_DEPENDENCY",
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_connection(db_path: Optional[Path] = None) -> sqlite3.Connection:
    # Resolved at call time, not baked in as a parameter default -- a
    # parameter default is evaluated once at import time, which would
    # silently ignore a later monkeypatch/override of config.DB_PATH.
    if db_path is None:
        db_path = config.DB_PATH
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # check_same_thread=False: the Web UI's per-request connection
    # (switchagent/web/app.py's get_conn dependency) is created inside a
    # sync FastAPI dependency and then used inside the sync route handler
    # -- two separate run_in_threadpool() hops that anyio's thread pool
    # does NOT guarantee land on the same OS thread. Found via a real
    # crash: `sqlite3.ProgrammingError: SQLite objects created in a
    # thread can only be used in that same thread` on /api/devices and
    # /api/queue once client-side polling got frequent enough (see
    # docs/STATE.md's Quake II investigation -- the Queue live-polling
    # fix raised request frequency enough to make this latent issue
    # visible in practice). Safe to relax here: each connection this
    # function returns is still only ever used by ONE logical
    # request/caller at a time, sequentially -- never concurrently shared
    # across two simultaneously-running threads -- which is the actual
    # unsafe case this check exists to catch.
    conn = sqlite3.connect(db_path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# Columns added after the initial stage-3 schema. SQLite has no
# "ADD COLUMN IF NOT EXISTS", so this checks PRAGMA table_info() first --
# safe to run on every startup, whether the DB is brand new (columns already
# present via SCHEMA... no, see note below) or pre-existing (stage-3 DBs on
# disk that predate this stage).
#
# NOTE: these columns are deliberately NOT in SCHEMA's CREATE TABLE above.
# Keeping SCHEMA as the stage-3 original and layering new columns via
# migration -- rather than rewriting the CREATE TABLE -- means an existing
# data/switchagent.db from before this stage upgrades in place instead of
# silently needing a fresh DB.
_INBOX_ITEMS_MIGRATIONS = {
    "content_type": "TEXT",
    "package_format": "TEXT",
    "title_id_confident": "INTEGER NOT NULL DEFAULT 0",
    "details_json": "TEXT",
}


def _migrate_inbox_items_columns(conn: sqlite3.Connection) -> None:
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(inbox_items)")}
    for column, declaration in _INBOX_ITEMS_MIGRATIONS.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE inbox_items ADD COLUMN {column} {declaration}")
    conn.commit()


# Stage 4: a job must always target one specific device (see
# docs/STAGE4.md -- "never pick the first available Switch"). Nullable at
# the SQL level on purpose: SQLite can't ALTER TABLE ADD a NOT NULL column
# without a default on a table that might already have rows, and a fixed
# default (e.g. '') would be worse than nullable -- it would silently look
# like a valid target instead of loudly failing. The real "never create a
# job without a target device" guarantee is enforced by create_job() below,
# in Python, at the only code path that inserts a row -- not by the schema.
_JOBS_MIGRATIONS = {
    "target_device_id": "TEXT",
    # Stage 4.1: path to work/job-<id>/manifest.json -- the frozen content
    # snapshot this job was created against (see switchagent/manifest.py).
    # Nullable for the same reason target_device_id is: enforced as
    # mandatory in Python (confirm_job() below), not at the SQL level.
    "manifest_path": "TEXT",
    # UI-002: nullable FK to installation_batches (see SCHEMA above).
    # NULL for every job created before this feature existed, and for any
    # future job created outside the Web UI's batch-creating path (there
    # is none today, but nothing requires a batch_id to exist).
    "batch_id": "INTEGER REFERENCES installation_batches(id)",
    # UI-006 (stall detection): last time this job showed REAL, honest
    # progress (see queue_worker.py's _run_job_transfer -- touched at
    # RUNNING start, per-file loop start, and per-file completion; never a
    # fake periodic keepalive). NULL until the job's first real progress
    # event; also NULL for every job that finished before this column
    # existed -- never backfilled with a guess.
    "last_progress_at": "TEXT",
}


def _migrate_jobs_columns(conn: sqlite3.Connection) -> None:
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
    for column, declaration in _JOBS_MIGRATIONS.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {column} {declaration}")
    conn.commit()


# UI-003: a user's own manual confirmation of DBI's on-console result,
# layered ON TOP of the immutable transport `outcome` column -- never
# overwrites it. NULL/'SUCCESS'/'FAILED' (validated in Python, see
# set_user_verified_outcome() below; not a SQL CHECK constraint, matching
# this file's existing convention of enforcing invariants in Python at the
# single writing function, not at the schema level -- see target_device_id
# in create_job()). Only ever meaningful for outcome == 'DONE_UNVERIFIED'
# (enforced by set_user_verified_outcome(), not by the schema either).
_INSTALL_HISTORY_MIGRATIONS = {
    "user_verified_outcome": "TEXT",
    "user_verified_at": "TEXT",
}


def _migrate_install_history_columns(conn: sqlite3.Connection) -> None:
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(install_history)")}
    for column, declaration in _INSTALL_HISTORY_MIGRATIONS.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE install_history ADD COLUMN {column} {declaration}")
    conn.commit()


def _migrate_jobs_table_for_library_support(conn: sqlite3.Connection) -> None:
    """Web UI: a job must be creatable from EITHER an inbox_items row OR a
    library_items row (see library_items' own comment in SCHEMA above).
    `inbox_item_id` was declared NOT NULL back in stage 2's original
    CREATE TABLE -- SQLite cannot relax a NOT NULL constraint (or add a new
    CHECK constraint) via ALTER TABLE, so this does the standard SQLite
    rebuild: create the new shape, copy rows across, drop, rename. Same
    "layer new shape on top, never rewrite SCHEMA's original CREATE TABLE"
    convention as _migrate_inbox_items_columns/_migrate_jobs_columns above
    -- an existing data/switchagent.db upgrades in place. Idempotent --
    skipped once library_item_id already exists. PRAGMA foreign_keys is
    suspended only for the duration of the swap (the moment `jobs` briefly
    doesn't exist under that name); job_log's own FK reference survives
    because the rebuilt table ends up with the same name."""
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
    if "library_item_id" in existing:
        return

    select_target_device_id = "target_device_id" if "target_device_id" in existing else "NULL"
    select_manifest_path = "manifest_path" if "manifest_path" in existing else "NULL"
    # UI-002/UI-006: this rebuild predates batch_id/last_progress_at, so it
    # must carry them across explicitly too, the same defensive way as
    # target_device_id/manifest_path above -- a hardcoded column list here
    # that omitted them would silently DROP whatever _migrate_jobs_columns()
    # had just added (a real regression caught by this project's own test
    # suite while building UI-002/UI-006: every job's freshly-added
    # batch_id/last_progress_at columns vanished the instant this rebuild
    # ran on a brand-new database, since it always runs immediately after
    # _migrate_jobs_columns() for a DB that predates library_item_id).
    select_batch_id = "batch_id" if "batch_id" in existing else "NULL"
    select_last_progress_at = "last_progress_at" if "last_progress_at" in existing else "NULL"

    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.execute("""
            CREATE TABLE jobs_new (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                inbox_item_id    INTEGER REFERENCES inbox_items(id),
                library_item_id  INTEGER REFERENCES library_items(id),
                action           TEXT NOT NULL,
                target_storage   TEXT,
                status           TEXT NOT NULL DEFAULT 'PENDING_CONFIRM',
                bytes_total      INTEGER,
                bytes_done       INTEGER DEFAULT 0,
                attempt_count    INTEGER DEFAULT 0,
                error            TEXT,
                created_at       TEXT NOT NULL,
                started_at       TEXT,
                finished_at      TEXT,
                target_device_id TEXT,
                manifest_path    TEXT,
                batch_id         INTEGER REFERENCES installation_batches(id),
                last_progress_at TEXT,
                CHECK ((inbox_item_id IS NOT NULL) + (library_item_id IS NOT NULL) = 1)
            )
        """)
        conn.execute(f"""
            INSERT INTO jobs_new (
                id, inbox_item_id, library_item_id, action, target_storage, status,
                bytes_total, bytes_done, attempt_count, error, created_at, started_at,
                finished_at, target_device_id, manifest_path, batch_id, last_progress_at
            )
            SELECT
                id, inbox_item_id, NULL, action, target_storage, status,
                bytes_total, bytes_done, attempt_count, error, created_at, started_at,
                finished_at, {select_target_device_id}, {select_manifest_path},
                {select_batch_id}, {select_last_progress_at}
            FROM jobs
        """)
        conn.execute("DROP TABLE jobs")
        conn.execute("ALTER TABLE jobs_new RENAME TO jobs")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_inbox_item ON jobs(inbox_item_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_library_item ON jobs(library_item_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status)")
        conn.commit()
    finally:
        conn.execute("PRAGMA foreign_keys=ON")


def _drop_duplicate_jobs_indexes(conn: sqlite3.Connection) -> None:
    """ARCH-003: `idx_jobs_item` (from the original stage-3 SCHEMA string's
    `CREATE INDEX IF NOT EXISTS idx_jobs_item ON jobs(inbox_item_id)`
    above) is an exact duplicate of `idx_jobs_inbox_item` (created by
    `_migrate_jobs_table_for_library_support`'s rebuild) -- both index
    `jobs(inbox_item_id)`. Because `executescript(SCHEMA)` re-runs on
    EVERY `init_db()` call, not just the first, and `CREATE INDEX IF NOT
    EXISTS` is a separate, independent statement from `CREATE TABLE IF NOT
    EXISTS` (SQLite does not skip it just because the enclosing table
    already existed), `idx_jobs_item` silently gets recreated the moment
    the rebuild has already dropped it once -- confirmed via direct
    `PRAGMA index_list`/`sqlite_master` inspection, reproduced with two
    successive `init_db()` calls and confirmed present on the real
    production `data/switchagent.db`. Dropping it here, unconditionally,
    on every `init_db()` call, is cheap and idempotent (`DROP INDEX IF
    EXISTS` is a no-op once it's already gone) -- simpler and more robust
    than trying to stop the schema script from recreating it.

    STAB-002 (migration stress) surfaced a related latent gap: the ONLY
    place `idx_jobs_inbox_item`/`idx_jobs_library_item` are ever created is
    inside `_migrate_jobs_table_for_library_support`'s rebuild branch,
    which is skipped entirely (early `return`) the moment `library_item_id`
    already exists. Every DB shape actually reachable via this
    application's OWN `init_db()` calls goes through that rebuild exactly
    once on its way to having `library_item_id` at all, so this has never
    been observed to miss the index in practice -- but that made
    `idx_jobs_inbox_item` continuing to exist an IMPLICIT invariant (true
    only because nothing else ever drops it), not a guaranteed one. Since
    dropping `idx_jobs_item` above now happens unconditionally on every
    call, this recreates both canonical indexes unconditionally too
    (`CREATE INDEX IF NOT EXISTS` is just as free/idempotent as the drop
    above), turning "should already be there" into "guaranteed to be
    there" for the same cost."""
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_inbox_item ON jobs(inbox_item_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_library_item ON jobs(library_item_id)")
    conn.execute("DROP INDEX IF EXISTS idx_jobs_item")
    conn.commit()


_INIT_DB_LOCK = threading.RLock()


def init_db(conn: sqlite3.Connection) -> None:
    # Worker, preparation and HTTP connections may all initialize at startup.
    # Serialize the entire check/add/rebuild sequence, including its commits;
    # locking a single ALTER would leave the preceding schema snapshot stale.
    # The desktop application's existing mutex excludes a second app process.
    with _INIT_DB_LOCK:
        _init_db_serial(conn)


def _init_db_serial(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()
    _migrate_inbox_items_columns(conn)
    _migrate_jobs_columns(conn)
    _migrate_jobs_table_for_library_support(conn)
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
    for column, declaration in {
        "retry_of_job_id": "INTEGER REFERENCES jobs(id)",
        "payload_batch_id": "INTEGER REFERENCES installation_batches(id)",
        "abandoned": "INTEGER NOT NULL DEFAULT 0",
        # W3-006 Override: set only by services.override_job() when the user
        # explicitly chooses "Override" on a DESTINATION_CONFLICT card -- the
        # worker (_run_job_transfer) skips its pre-send existence check and
        # passes overwrite=True to backend.send_file() for every file in
        # THIS job only. Every other job creation path leaves this at the
        # default 0/false; the "never overwrite" guard stays the default.
        "force_overwrite": "INTEGER NOT NULL DEFAULT 0",
        # The destination path the job was sending when it last touched the
        # device (queue_worker._run_job_transfer, set as each file starts).
        # A job that stopped mid-file may have left that one file half
        # written; a retry is allowed to replace exactly that file, because
        # it is this transfer's own -- see manifest.inherit_progress.
        "current_file": "TEXT",
        # Set when the connection to the console dropped mid-job -- the console
        # stopped answering, or went away -- rather than the job failing: it
        # waits as WAITING_FOR_DEVICE and continues on its own, from its own
        # progress.json (see queue_worker._wait_for_reconnect). resume_after is
        # the earliest time it tries again while the console stays connected;
        # NULL once the console has been seen gone, which means "as soon as it
        # is back".
        "auto_resume": "INTEGER NOT NULL DEFAULT 0",
        "resume_after": "TEXT",
    }.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {column} {declaration}")
    conn.commit()
    _migrate_install_history_columns(conn)
    _drop_duplicate_jobs_indexes(conn)


@contextmanager
def open_db(db_path: Optional[Path] = None) -> Iterator[sqlite3.Connection]:
    conn = get_connection(db_path)
    try:
        init_db(conn)
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# inbox_items
# ---------------------------------------------------------------------------

def get_inbox_item(conn: sqlite3.Connection, relative_path: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM inbox_items WHERE relative_path = ?", (relative_path,)
    ).fetchone()


def upsert_inbox_item(
    conn: sqlite3.Connection,
    *,
    relative_path: str,
    item_type: str,
    file_type: str,
    size: int,
    mtime: float,
    content_hash: Optional[str],
    title_id: Optional[str],
    title_id_source: Optional[str],
    status: str,
    suggested_action: Optional[str],
    suggested_target: Optional[str],
    note: Optional[str] = None,
    error: Optional[str] = None,
    content_type: Optional[str] = None,
    package_format: Optional[str] = None,
    title_id_confident: bool = False,
    details_json: Optional[str] = None,
) -> int:
    ts = now_iso()
    existing = get_inbox_item(conn, relative_path)
    if existing is None:
        cur = conn.execute(
            """
            INSERT INTO inbox_items (
                relative_path, item_type, file_type, size, mtime, content_hash,
                title_id, title_id_source, status, suggested_action,
                suggested_target, note, error, first_seen_at, last_scanned_at,
                content_type, package_format, title_id_confident, details_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                relative_path, item_type, file_type, size, mtime, content_hash,
                title_id, title_id_source, status, suggested_action,
                suggested_target, note, error, ts, ts,
                content_type, package_format, int(title_id_confident), details_json,
            ),
        )
        conn.commit()
        return cur.lastrowid

    conn.execute(
        """
        UPDATE inbox_items SET
            item_type = ?, file_type = ?, size = ?, mtime = ?, content_hash = ?,
            title_id = ?, title_id_source = ?, status = ?, suggested_action = ?,
            suggested_target = ?, note = ?, error = ?, last_scanned_at = ?,
            content_type = ?, package_format = ?, title_id_confident = ?, details_json = ?
        WHERE id = ?
        """,
        (
            item_type, file_type, size, mtime, content_hash,
            title_id, title_id_source, status, suggested_action,
            suggested_target, note, error, ts,
            content_type, package_format, int(title_id_confident), details_json,
            existing["id"],
        ),
    )
    conn.commit()
    return existing["id"]


def touch_inbox_item_scanned(conn: sqlite3.Connection, item_id: int) -> None:
    conn.execute(
        "UPDATE inbox_items SET last_scanned_at = ? WHERE id = ?",
        (now_iso(), item_id),
    )
    conn.commit()


def list_inbox_items(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM inbox_items ORDER BY first_seen_at DESC"
    ).fetchall()


def delete_inbox_items_missing_from(conn: sqlite3.Connection, present_relative_paths: set[str]) -> int:
    """Drop DB rows for inbox items that no longer exist on disk (user deleted
    them from inbox/ manually). Does not touch jobs history -- jobs keep their
    inbox_item_id even if the source row's underlying file is gone, so past
    transfers remain auditable."""
    rows = conn.execute("SELECT id, relative_path FROM inbox_items").fetchall()
    stale_ids = [r["id"] for r in rows if r["relative_path"] not in present_relative_paths]
    if stale_ids:
        conn.executemany("DELETE FROM inbox_items WHERE id = ?", [(i,) for i in stale_ids])
        conn.commit()
    return len(stale_ids)


def find_other_analyzed_item_with_hash(
    conn: sqlite3.Connection, content_hash: str, exclude_relative_path: str,
) -> Optional[sqlite3.Row]:
    """Within inbox/ itself: is there ALREADY another item (different path)
    with this exact content that is considered the canonical, analyzed copy?
    Used to flag a second copy of the same content (e.g. the same NSP
    dropped under two different filenames) as SKIP_DUPLICATE instead of
    presenting it as a second independent thing to install."""
    if not content_hash:
        return None
    return conn.execute(
        """
        SELECT * FROM inbox_items
        WHERE content_hash = ? AND relative_path != ? AND status = 'ANALYZED'
        ORDER BY first_seen_at ASC
        LIMIT 1
        """,
        (content_hash, exclude_relative_path),
    ).fetchone()


def find_library_item_with_hash(conn: sqlite3.Connection, content_hash: str) -> Optional[sqlite3.Row]:
    """ARCH-005: cross-root counterpart to find_other_analyzed_item_with_hash()
    -- is this exact content ALREADY sitting in the library (config.LIBRARY_DIR)
    as an available item? inbox/ and library/ are two independent roots
    scanned by two independent passes (scan_once() / scan_library_once()),
    so neither one's own same-root check (which only ever looks at its own
    table) could ever notice a copy that lives in the OTHER root. No
    exclude-path parameter is needed here (unlike the same-root sibling):
    any match in the other table is necessarily a different physical file
    tree, never "itself"."""
    if not content_hash:
        return None
    return conn.execute(
        """
        SELECT * FROM library_items
        WHERE content_hash = ? AND status = 'AVAILABLE'
        ORDER BY first_seen_at ASC
        LIMIT 1
        """,
        (content_hash,),
    ).fetchone()


def get_inbox_item_by_id(conn: sqlite3.Connection, item_id: int) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM inbox_items WHERE id = ?", (item_id,)).fetchone()


# ---------------------------------------------------------------------------
# jobs / dedup
# ---------------------------------------------------------------------------

def create_job(
    conn: sqlite3.Connection,
    *,
    action: str,
    target_storage: str,
    target_device_id: str,
    inbox_item_id: Optional[int] = None,
    library_item_id: Optional[int] = None,
    batch_id: Optional[int] = None,
    force_overwrite: bool = False,
    retry_of_job_id: Optional[int] = None,
) -> int:
    """Creates a job in PENDING_CONFIRM. target_device_id is mandatory and
    fixed for the job's entire lifetime -- this is the single enforcement
    point for "a job must always have one unambiguous target device"; there
    is no other way to insert a row into `jobs`.

    Exactly one of inbox_item_id (Stage 2's inbox/ pipeline) or
    library_item_id (Web UI's library_items, see SCHEMA) must be given --
    mirrors the CHECK constraint on the `jobs` table itself
    (_migrate_jobs_table_for_library_support), enforced here too so the
    error is a clear Python exception, not a raw sqlite3.IntegrityError.

    batch_id (UI-002) is optional and purely informational -- a job is
    fully valid and processable with batch_id=NULL (every job created
    before this feature existed, and any caller that doesn't group its
    jobs into a batch).

    retry_of_job_id: set when this job was created BY a Retry/Override
    click on an older job (see services.retry_job/override_job). Besides
    its original use in the staged/frozen-payload retry path (preventing
    two concurrent retries of the same attempt), list_queue()/
    list_queue_grouped() now also use it to stop showing the OLD job the
    instant a retry exists for it -- the old row is still a permanent,
    untouched record (still in History), just no longer active work, and
    no longer sitting there with live Override/Skip/Retry buttons a user
    could click again after already having retried it once."""
    if not target_device_id:
        raise ValueError(
            "target_device_id is required -- a job must always target one "
            "specific device, never 'whichever Switch happens to be plugged in'"
        )
    if (inbox_item_id is None) == (library_item_id is None):
        raise ValueError(
            "exactly one of inbox_item_id or library_item_id is required -- "
            f"got inbox_item_id={inbox_item_id!r}, library_item_id={library_item_id!r}"
        )
    ts = now_iso()
    cur = conn.execute(
        """
        INSERT INTO jobs (inbox_item_id, library_item_id, action, target_storage, target_device_id, status, created_at, batch_id, force_overwrite, retry_of_job_id)
        VALUES (?, ?, ?, ?, ?, 'PENDING_CONFIRM', ?, ?, ?, ?)
        """,
        (inbox_item_id, library_item_id, action, target_storage, target_device_id, ts, batch_id, int(force_overwrite), retry_of_job_id),
    )
    conn.commit()
    log_job_event(conn, cur.lastrowid, f"created, target_device_id={target_device_id!r}")
    return cur.lastrowid


# ---------------------------------------------------------------------------
# installation_batches (UI-002) -- one row per "Confirm Install" click.
# ---------------------------------------------------------------------------

def create_installation_batch(conn: sqlite3.Connection, *, target_device_id: str) -> int:
    """Creates a batch row. Callers (web/services.py's
    create_and_confirm_jobs) create this FIRST, then pass its id to every
    job created from the same confirmation click -- if zero jobs end up
    created (every selected item failed), the caller is responsible for
    not leaving a pointless empty batch behind (see delete_installation_batch)."""
    ts = now_iso()
    cur = conn.execute(
        "INSERT INTO installation_batches (target_device_id, created_at) VALUES (?, ?)",
        (target_device_id, ts),
    )
    conn.commit()
    return cur.lastrowid


def delete_installation_batch(conn: sqlite3.Connection, batch_id: int) -> None:
    """Used only to clean up a batch that ended up with zero jobs (e.g.
    every selected item failed preview/manifest staging) -- never called
    on a batch that has any job referencing it. No FK ON DELETE CASCADE
    is relied on here on purpose: this must never be reachable for a
    batch that already has jobs."""
    conn.execute("DELETE FROM installation_batches WHERE id = ?", (batch_id,))
    conn.commit()


def get_installation_batch(conn: sqlite3.Connection, batch_id: int) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM installation_batches WHERE id = ?", (batch_id,)).fetchone()


def list_jobs_by_batch(conn: sqlite3.Connection, batch_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM jobs WHERE batch_id = ? ORDER BY id ASC", (batch_id,)
    ).fetchall()


def set_job_manifest_path(conn: sqlite3.Connection, job_id: int, manifest_path: str) -> None:
    conn.execute("UPDATE jobs SET manifest_path = ? WHERE id = ?", (manifest_path, job_id))
    conn.commit()


def get_job(conn: sqlite3.Connection, job_id: int) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()


def list_jobs(conn: sqlite3.Connection, *, status: Optional[str] = None) -> list[sqlite3.Row]:
    if status is None:
        return conn.execute("SELECT * FROM jobs ORDER BY created_at ASC").fetchall()
    return conn.execute(
        "SELECT * FROM jobs WHERE status = ? ORDER BY created_at ASC", (status,)
    ).fetchall()


def set_job_auto_resume(conn: sqlite3.Connection, job_id: int, resume_after: Optional[str]) -> None:
    conn.execute("UPDATE jobs SET auto_resume = 1, resume_after = ? WHERE id = ?", (resume_after, job_id))
    conn.commit()


def list_confirmed_jobs(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Oldest first -- the order queue_worker.run_worker_once() considers
    jobs in, skipping over ones whose target device isn't currently
    reachable rather than ever redirecting them. Also includes
    WAITING_FOR_BASE jobs (see docs/STATE.md's Quake II investigation) and
    WAITING_FOR_DEVICE jobs (UI-005 -- a job that never started a transfer
    whose target device is merely not plugged in yet) -- all three must be
    re-evaluated every pass, exactly like a fresh CONFIRMED job, since the
    condition each is waiting on may have resolved since the last pass.
    Unlike DEVICE_UNAVAILABLE, none of the three is reconsidered via a
    separate explicit action -- all are naturally revisited here on every
    call."""
    # id ASC as an explicit tiebreak, not just created_at (whose second-
    # resolution timestamp -- see now_iso() -- ties trivially for every job
    # in the same multi-package-archive batch): a batch's jobs are always
    # created Base -> Update -> DLC in that order (see
    # web/services.create_and_confirm_jobs), so this keeps that same
    # relative order here rather than depending on SQLite's unspecified
    # tie-breaking. Not itself a correctness requirement (a job whose
    # dependency isn't satisfied yet is skipped over either way, see
    # _dependency_status), but it is what makes iteration order actually
    # match the deterministic install order the hotfix mandate requires.
    return conn.execute(
        "SELECT * FROM jobs WHERE status IN ('CONFIRMED', 'WAITING_FOR_BASE', 'WAITING_FOR_DEVICE') "
        "ORDER BY created_at ASC, id ASC"
    ).fetchall()


def update_job_status(
    conn: sqlite3.Connection,
    job_id: int,
    status: str,
    *,
    error: Optional[str] = None,
    bytes_done: Optional[int] = None,
    bytes_total: Optional[int] = None,
    started_at: Optional[str] = None,
    finished_at: Optional[str] = None,
    increment_attempt: bool = False,
    last_progress_at: Optional[str] = None,
    current_file: Optional[str] = None,
) -> None:
    sets = ["status = ?"]
    params: list = [status]
    if error is not None:
        sets.append("error = ?")
        params.append(error)
    if bytes_done is not None:
        sets.append("bytes_done = ?")
        params.append(bytes_done)
    if bytes_total is not None:
        sets.append("bytes_total = ?")
        params.append(bytes_total)
    if started_at is not None:
        sets.append("started_at = ?")
        params.append(started_at)
    if finished_at is not None:
        sets.append("finished_at = ?")
        params.append(finished_at)
    if increment_attempt:
        sets.append("attempt_count = attempt_count + 1")
    if last_progress_at is not None:
        # UI-006: only ever set from queue_worker.py's own honest progress
        # points (see _JOBS_MIGRATIONS' comment on last_progress_at) --
        # this function itself does not decide when progress "really"
        # happened, it just persists the caller's timestamp.
        sets.append("last_progress_at = ?")
        params.append(last_progress_at)
    if current_file is not None:
        sets.append("current_file = ?")
        params.append(current_file)
    params.append(job_id)
    conn.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE id = ?", params)
    conn.commit()


def confirm_job(conn: sqlite3.Connection, job_id: int) -> None:
    """PENDING_CONFIRM -> CONFIRMED. The explicit human confirmation step --
    nothing before this point may cause a transfer attempt. Requires a
    manifest to already be attached (see switchagent/manifest.py and
    switchagent/queue_worker.py::create_job_from_report) -- a job with no
    frozen content snapshot has nothing safe for the worker to send, so it
    cannot be confirmed."""
    job = get_job(conn, job_id)
    if job is None:
        raise ValueError(f"no such job: {job_id}")
    if job["status"] != "PENDING_CONFIRM":
        raise ValueError(f"job {job_id} is '{job['status']}', not PENDING_CONFIRM -- cannot confirm")
    if not job["manifest_path"]:
        raise ValueError(
            f"job {job_id} has no manifest -- create it via "
            "queue_worker.create_job_from_report(), not a bare db.create_job() call"
        )
    update_job_status(conn, job_id, "CONFIRMED")
    log_job_event(conn, job_id, "confirmed")


def retry_job(conn: sqlite3.Connection, job_id: int) -> None:
    """INTERRUPTED/FAILED/DEVICE_UNAVAILABLE -> CONFIRMED. The explicit
    human "try again" action -- run_worker_once() never does this on its
    own for any of these three statuses (DEVICE_UNAVAILABLE included: it
    only leaves a job DEVICE_UNAVAILABLE once per attempt and never
    revisits it without an explicit retry_job() call -- see
    docs/STAGE4.1.md, this corrects an inaccurate claim in the original
    stage-4 writeup that DEVICE_UNAVAILABLE jobs retried themselves).
    target_device_id and the job's manifest are both untouched -- retrying
    a job never changes its target device or what content it will send;
    the worker re-verifies the manifest against the live source before
    doing anything (see queue_worker.py), so a stale manifest is refused,
    not silently honored.

    SOURCE_CHANGED, DESTINATION_CONFLICT and BLOCKED_BY_DEPENDENCY are
    deliberately NOT retryable here: retrying with the same frozen
    manifest would just re-detect the identical problem. SOURCE_CHANGED
    needs a brand new job (fresh preview + fresh confirmation) against the
    current content; DESTINATION_CONFLICT needs the conflicting file
    resolved on the device first (out of scope for this stage -- no
    conflict-resolution UI exists yet); BLOCKED_BY_DEPENDENCY needs its
    base game's install issue resolved first, then a brand new job for
    this content (see docs/STATE.md's Quake II investigation).
    WAITING_FOR_BASE is also not in the retryable set below -- there is
    nothing to retry, the worker already re-checks it every pass on its
    own."""
    job = get_job(conn, job_id)
    if job is None:
        raise ValueError(f"no such job: {job_id}")
    if job["status"] in ("SOURCE_CHANGED", "DESTINATION_CONFLICT", "BLOCKED_BY_DEPENDENCY"):
        raise ValueError(
            f"job {job_id} is '{job['status']}' -- not retryable as-is: "
            + {
                "SOURCE_CHANGED": "create a new job from a fresh preview/confirmation instead",
                "DESTINATION_CONFLICT": "resolve the conflicting file on the device first",
                "BLOCKED_BY_DEPENDENCY": "resolve the base game's install issue, then create a new job",
            }[job["status"]]
        )
    if job["status"] not in ("INTERRUPTED", "FAILED", "DEVICE_UNAVAILABLE"):
        raise ValueError(
            f"job {job_id} is '{job['status']}' -- retry only applies to "
            "INTERRUPTED/FAILED/DEVICE_UNAVAILABLE"
        )
    update_job_status(conn, job_id, "CONFIRMED")
    log_job_event(conn, job_id, f"retried (was {job['status']})")


def recover_stale_running_jobs(conn: sqlite3.Connection) -> int:
    """Called once at process/worker startup, before any normal processing.
    Any job still RUNNING means the process died mid-transfer without a
    clean status update -- conservatively treated as INTERRUPTED, never as
    DONE. Recovery does NOT attempt to reconnect or pick a different device
    here; it only marks the fact. The next time this job is retried (see
    retry_job), run_worker_once() will look for its ORIGINAL
    target_device_id specifically, exactly as it would for any other
    INTERRUPTED job -- there is no separate "recovery path" that could
    substitute a different device, because recovery and normal processing
    share the same target_device_id lookup."""
    rows = conn.execute("SELECT id FROM jobs WHERE status = 'RUNNING'").fetchall()
    for row in rows:
        update_job_status(
            conn, row["id"], "INTERRUPTED",
            error="process restarted while this job was running -- not confirmed complete",
        )
        log_job_event(conn, row["id"], "recovered at startup: RUNNING -> INTERRUPTED")
    return len(rows)


def abandon_unconfirmed_jobs(conn: sqlite3.Connection) -> int:
    """Called once at process/worker startup, alongside
    recover_stale_running_jobs(). A job still PENDING_CONFIRM means the
    process died between staging it and confirming it -- and nothing can
    ever confirm it now: the only caller of confirm_job() for these is
    web/preparation.py's sequential run, whose state lives in memory
    (PreparationQueue.states) and does not survive a restart.

    Left alone, such a job is not merely untidy: it holds its staged
    payload in work/ forever, and counts as unfinished work against its
    target device, so forget_device() refuses that Switch for good. Marked
    abandoned (not just FAILED) because there is genuinely nothing to do
    with it -- retry_job()/override_job() both refuse an abandoned job and
    tell the user to select the source again in Library, which is exactly
    the right answer here. Nothing was ever sent to the device, so this
    can never discard real progress."""
    rows = conn.execute("SELECT id FROM jobs WHERE status = 'PENDING_CONFIRM'").fetchall()
    for row in rows:
        update_job_status(
            conn, row["id"], "FAILED",
            error="process restarted before this job was confirmed -- nothing was installed",
        )
        conn.execute("UPDATE jobs SET abandoned=1 WHERE id=?", (row["id"],))
        log_job_event(conn, row["id"], "recovered at startup: PENDING_CONFIRM -> FAILED (abandoned)")
    conn.commit()
    return len(rows)


def log_job_event(conn: sqlite3.Connection, job_id: int, message: str) -> None:
    conn.execute(
        "INSERT INTO job_log (job_id, ts, message) VALUES (?, ?, ?)",
        (job_id, now_iso(), message),
    )
    conn.commit()


def list_job_log(conn: sqlite3.Connection, job_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM job_log WHERE job_id = ? ORDER BY id ASC", (job_id,)
    ).fetchall()


def find_existing_job_for_hash(conn: sqlite3.Connection, content_hash: str) -> Optional[sqlite3.Row]:
    """Used to answer 'did we already send this content?' before letting the
    user confirm a new job -- matched by content hash, not filename, so a
    renamed-but-identical file is still recognized as a duplicate. Checks
    BOTH possible sources a job can be created from (inbox_items or
    library_items, see SCHEMA) -- a job's own row never says which table's
    content_hash matched, only that jobs.inbox_item_id XOR
    jobs.library_item_id led here."""
    if not content_hash:
        return None
    placeholders = ",".join("?" * len(ACTIVE_OR_DONE_JOB_STATUSES))
    return conn.execute(
        f"""
        SELECT jobs.*
        FROM jobs
        LEFT JOIN inbox_items ON inbox_items.id = jobs.inbox_item_id
        LEFT JOIN library_items ON library_items.id = jobs.library_item_id
        WHERE COALESCE(inbox_items.content_hash, library_items.content_hash) = ?
          AND jobs.status IN ({placeholders})
        ORDER BY jobs.created_at DESC
        LIMIT 1
        """,
        (content_hash, *ACTIVE_OR_DONE_JOB_STATUSES),
    ).fetchone()


# ---------------------------------------------------------------------------
# library_items -- Web UI's index of config.LIBRARY_DIR (see SCHEMA above
# and docs/WEB-UI.md). Deliberately mirrors inbox_items' shape/behavior
# (same classify_file()/hash_mod_folder() outputs, same upsert-by-identity/
# skip-if-unchanged pattern) but keyed by absolute_path, not a path relative
# to a single fixed root -- library_items' root is user-configurable.
# ---------------------------------------------------------------------------

def get_library_item(conn: sqlite3.Connection, absolute_path: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM library_items WHERE absolute_path = ?", (absolute_path,)
    ).fetchone()


def get_library_item_by_id(conn: sqlite3.Connection, item_id: int) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM library_items WHERE id = ?", (item_id,)).fetchone()


def upsert_library_item(
    conn: sqlite3.Connection,
    *,
    absolute_path: str,
    item_type: str,
    file_type: str,
    size: int,
    mtime: float,
    content_hash: Optional[str],
    title_id: Optional[str],
    title_id_source: Optional[str],
    status: str,
    suggested_action: Optional[str],
    suggested_target: Optional[str],
    note: Optional[str] = None,
    error: Optional[str] = None,
    content_type: Optional[str] = None,
    package_format: Optional[str] = None,
    title_id_confident: bool = False,
    details_json: Optional[str] = None,
) -> int:
    ts = now_iso()
    existing = get_library_item(conn, absolute_path)
    if existing is None:
        cur = conn.execute(
            """
            INSERT INTO library_items (
                absolute_path, item_type, file_type, size, mtime, content_hash,
                title_id, title_id_source, status, suggested_action,
                suggested_target, note, error, first_seen_at, last_scanned_at,
                content_type, package_format, title_id_confident, details_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                absolute_path, item_type, file_type, size, mtime, content_hash,
                title_id, title_id_source, status, suggested_action,
                suggested_target, note, error, ts, ts,
                content_type, package_format, int(title_id_confident), details_json,
            ),
        )
        conn.commit()
        return cur.lastrowid

    conn.execute(
        """
        UPDATE library_items SET
            item_type = ?, file_type = ?, size = ?, mtime = ?, content_hash = ?,
            title_id = ?, title_id_source = ?, status = ?, suggested_action = ?,
            suggested_target = ?, note = ?, error = ?, last_scanned_at = ?,
            content_type = ?, package_format = ?, title_id_confident = ?, details_json = ?
        WHERE id = ?
        """,
        (
            item_type, file_type, size, mtime, content_hash,
            title_id, title_id_source, status, suggested_action,
            suggested_target, note, error, ts,
            content_type, package_format, int(title_id_confident), details_json,
            existing["id"],
        ),
    )
    conn.commit()
    return existing["id"]


def set_library_item_sealed_forwarder(conn: sqlite3.Connection, item_id: int, sealed: bool) -> None:
    """A package whose TITLE_ID cannot be read, recognised (or no longer)
    as a port's forwarder by the folder it sits in -- see
    sd_files.SEALED_FORWARDER. Installable while it is one; back to
    NEEDS_REVIEW, exactly as the scanner left it, once it is not."""
    if sealed:
        conn.execute(
            "UPDATE library_items SET status = 'AVAILABLE', title_id_source = ?, "
            "suggested_action = 'INSTALL_VIA_DBI', suggested_target = 'SD_INSTALL', note = ? WHERE id = ?",
            ("sealed_forwarder",
             "a port's forwarder: installed together with the switch/ folder beside it "
             "(its TITLE_ID is sealed inside the package)", item_id),
        )
    else:
        conn.execute(
            "UPDATE library_items SET status = 'NEEDS_REVIEW', title_id_source = NULL, "
            "suggested_action = NULL, suggested_target = NULL, note = ? WHERE id = ?",
            ("could not unambiguously determine TITLE_ID from the filename -- needs human review", item_id),
        )
    conn.commit()


def set_library_item_title_id(
    conn: sqlite3.Connection, item_id: int, *, title_id: Optional[str], source: Optional[str],
) -> None:
    """Which game an SD_FILES row belongs to, decided after a scan has seen
    the whole library (scanner.assign_sd_file_owners). Never confident: it
    is inferred from where the files sit, not read from them."""
    conn.execute(
        "UPDATE library_items SET title_id = ?, title_id_source = ?, title_id_confident = 0 WHERE id = ?",
        (title_id, source, item_id),
    )
    conn.commit()


def touch_library_item_scanned(conn: sqlite3.Connection, item_id: int) -> None:
    conn.execute(
        "UPDATE library_items SET last_scanned_at = ? WHERE id = ?", (now_iso(), item_id),
    )
    conn.commit()


def list_library_items(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM library_items ORDER BY first_seen_at DESC").fetchall()


# A library_items row whose file has left the library, kept ONLY as a
# record: jobs.library_item_id has no ON DELETE clause, and Queue/History
# still resolve their display names through it. Its own status rather than
# ERROR, because the two mean different things to a reader and to the UI --
# ERROR is "look at this, something is wrong with it", and these need no
# attention at all: the file is simply not in the library any more, usually
# because the user removed the folder on purpose. Every "what is in my
# library" read skips them (see web/services.library_rows_in_scope), while
# every display-name lookup still finds them.
LIBRARY_ITEM_RETIRED = "RETIRED"


def delete_library_items_missing_from(conn: sqlite3.Connection, present_absolute_paths: set[str]) -> int:
    """Mirrors delete_inbox_items_missing_from() -- drops rows for files no
    longer present under config.LIBRARY_DIR. Does not touch jobs/history:
    a job keeps its library_item_id even if the row's file was later
    deleted from disk, so past transfers remain auditable.

    A row that any job has ever referenced cannot be hard-deleted --
    jobs.library_item_id has no ON DELETE clause, and PRAGMA
    foreign_keys=ON is set on every normal connection, so attempting to
    would raise sqlite3.IntegrityError (a real, previously-latent
    regression: every Rescan after a previously-installed game's file was
    removed from the Library folder used to crash here, and kept crashing
    on every subsequent Rescan since the stale row could never be cleaned
    up -- see tests/test_web_db.py). Such rows are marked RETIRED instead --
    visibly flagged (they already surface under the Library page's
    existing NEEDS_REVIEW/ERROR filter) and no longer retryable/
    installable (retry_job() and create_and_confirm_jobs() both already
    require status == 'AVAILABLE'), while keeping their name/title_id
    available for Queue/History's display-name lookups. Only rows with
    zero referencing jobs are actually removed; the return value counts
    only those true removals, same contract as before this fix."""
    rows = conn.execute("SELECT id, absolute_path FROM library_items").fetchall()
    stale_ids = [r["id"] for r in rows if r["absolute_path"] not in present_absolute_paths]
    if not stale_ids:
        return 0

    placeholders = ",".join("?" * len(stale_ids))
    referenced_ids = {
        row["library_item_id"] for row in conn.execute(
            f"SELECT DISTINCT library_item_id FROM jobs WHERE library_item_id IN ({placeholders})",
            stale_ids,
        ).fetchall()
    }
    deletable_ids = [i for i in stale_ids if i not in referenced_ids]
    orphaned_but_referenced_ids = [i for i in stale_ids if i in referenced_ids]

    if deletable_ids:
        conn.executemany("DELETE FROM library_items WHERE id = ?", [(i,) for i in deletable_ids])
    if orphaned_but_referenced_ids:
        ts = now_iso()
        conn.executemany(
            f"UPDATE library_items SET status = '{LIBRARY_ITEM_RETIRED}', error = ?, "
            "last_scanned_at = ? WHERE id = ?",
            [
                ("source file no longer found on disk (kept: referenced by an existing job/history entry)", ts, i)
                for i in orphaned_but_referenced_ids
            ],
        )
    conn.commit()
    return len(deletable_ids)


def _relocation_identity(row: sqlite3.Row):
    """What makes two library_items rows the SAME physical thing seen under
    two different path spellings. Deliberately cheap: indexing a package
    never hashes it (scanner._classify_package_file(hash_content=False) --
    reading gigabytes per scan pass is exactly what that avoids), so
    content_hash is None for the files this has to work on and cannot be
    the identity here.

    size + mtime + filename instead. mtime is a float carrying sub-
    microsecond precision and survives an SMB round trip byte-for-byte
    (verified against the real duplicated rows this fixes), so a collision
    between two genuinely unrelated files is not a practical concern. A
    mod folder has no single file to name and IS always hashed
    (scanner.hash_mod_folder), so it uses that hash directly.

    None means "no usable identity" -- never merged, in either direction."""
    if row["item_type"] == "MOD_FOLDER":
        return ("MOD_FOLDER", row["content_hash"]) if row["content_hash"] else None
    return ("FILE", row["size"], row["mtime"], Path(row["absolute_path"]).name.lower())


def merge_relocated_library_items(
    conn: sqlite3.Connection, present_absolute_paths: set[str], *, skip_roots=(),
) -> list[str]:
    """Folds a row whose path this scan did NOT visit into the row for the
    same physical file that it DID -- the file did not disappear, it is
    simply reached by a different path now.

    Why this exists: absolute_path is the library's identity key, so
    pointing a Library folder at the same directory by another spelling
    (D:\\shared\\Download -> \\\\192.168.50.2\\shared\\Download, a drive
    letter change, a renamed parent folder) re-indexed every file in it as
    a brand new item, while the old rows stayed behind marked ERROR
    "source file no longer found on disk" -- they cannot be deleted once a
    job references them (see delete_library_items_missing_from). One file
    then counted as two everywhere that counts library rows: Game Details
    said "Base game: present (2 copies)" and "Updates: 2" for a game with
    one of each.

    The old row is merged INTO the new one rather than the other way
    round, so the surviving row is the one describing where the file
    actually is now; jobs.library_item_id (the only FK into this table --
    install_history hangs off jobs(id) and keeps its own display_name
    snapshot regardless) is repointed first, so nothing loses its history.
    The earlier first_seen_at wins: the library did not learn about this
    file today just because the path changed.

    Strictly conservative -- merges only when exactly ONE visited row
    matches. Two live copies of the same file in two watched folders are a
    genuine duplicate, not a relocation (SKIP_DUPLICATE's job), and rows
    under `skip_roots` -- library folders that are currently unreachable,
    e.g. an unplugged drive -- are never merged away on the strength of
    something that only LOOKS like them.

    Returns the absolute_path of each surviving row, one entry per merge,
    so the caller can tell a genuinely new file from one it had already
    indexed under its old name (scan_library_once reports them
    separately -- "1 new" for a file that was only ever moved is a lie)."""
    rows = conn.execute("SELECT * FROM library_items").fetchall()
    live: dict = {}
    stale = []
    for row in rows:
        if row["absolute_path"] in present_absolute_paths:
            identity = _relocation_identity(row)
            if identity is not None:
                live.setdefault(identity, []).append(row)
        elif not any(Path(row["absolute_path"]).is_relative_to(root) for root in skip_roots):
            stale.append(row)

    merged: list[str] = []
    for row in stale:
        twins = live.get(_relocation_identity(row), [])
        if len(twins) != 1:
            continue
        keeper = twins[0]
        conn.execute(
            "UPDATE jobs SET library_item_id = ? WHERE library_item_id = ?", (keeper["id"], row["id"]),
        )
        conn.execute(
            "UPDATE library_items SET first_seen_at = ? WHERE id = ? AND first_seen_at > ?",
            (row["first_seen_at"], keeper["id"], row["first_seen_at"]),
        )
        conn.execute("DELETE FROM library_items WHERE id = ?", (row["id"],))
        merged.append(keeper["absolute_path"])
    if merged:
        conn.commit()
    return merged


def find_other_library_item_with_hash(
    conn: sqlite3.Connection, content_hash: str, exclude_absolute_path: str,
) -> Optional[sqlite3.Row]:
    """Mirrors find_other_analyzed_item_with_hash() -- flags a second copy
    of the same content found elsewhere under config.LIBRARY_DIR (e.g. the
    same NSZ re-downloaded under a different folder name) as SKIP_DUPLICATE
    instead of presenting it as an independent, separately-installable
    thing."""
    if not content_hash:
        return None
    return conn.execute(
        """
        SELECT * FROM library_items
        WHERE content_hash = ? AND absolute_path != ? AND status = 'AVAILABLE'
        ORDER BY first_seen_at ASC
        LIMIT 1
        """,
        (content_hash, exclude_absolute_path),
    ).fetchone()


def find_inbox_item_with_hash(conn: sqlite3.Connection, content_hash: str) -> Optional[sqlite3.Row]:
    """ARCH-005: cross-root counterpart to find_other_library_item_with_hash()
    -- is this exact content ALREADY sitting in inbox/ as an analyzed item?
    See find_library_item_with_hash()'s docstring (the mirror-image
    direction) for why this is a separate check from the same-root one."""
    if not content_hash:
        return None
    return conn.execute(
        """
        SELECT * FROM inbox_items
        WHERE content_hash = ? AND status = 'ANALYZED'
        ORDER BY first_seen_at ASC
        LIMIT 1
        """,
        (content_hash,),
    ).fetchone()


# ---------------------------------------------------------------------------
# devices -- known MTP device identities + user-editable friendly names.
# device_id itself (see mtp/base.py's DeviceInfo.device_id) is never
# editable here or anywhere -- only observed and stored as-is.
# ---------------------------------------------------------------------------

def upsert_device_seen(conn: sqlite3.Connection, device_id: str, display_name: Optional[str]) -> None:
    """Called whenever a device is observed live (connected or merely
    enumerated) -- records/refreshes last_seen_at and the last display name
    DBI reported. Never touches friendly_name -- that is exclusively set by
    set_device_friendly_name(), a separate, explicit user action."""
    ts = now_iso()
    existing = get_device(conn, device_id)
    if existing is None:
        conn.execute(
            """
            INSERT INTO devices (device_id, friendly_name, last_known_display_name, first_seen_at, last_seen_at)
            VALUES (?, NULL, ?, ?, ?)
            """,
            (device_id, display_name, ts, ts),
        )
    else:
        conn.execute(
            "UPDATE devices SET last_known_display_name = ?, last_seen_at = ? WHERE device_id = ?",
            (display_name, ts, device_id),
        )
    conn.commit()


def set_device_friendly_name(conn: sqlite3.Connection, device_id: str, friendly_name: Optional[str]) -> None:
    """friendly_name != identity (Web UI spec point 33): this never touches
    device_id, and does nothing if the device has never been seen (no
    device_id may be invented just to hold a name)."""
    if get_device(conn, device_id) is None:
        from .mtp.windows import mask_device_id  # ARCH-001: never echo a raw, serial-bearing device_id into an exception message that can reach an HTTP response body

        raise ValueError(f"unknown device_id -- never seen before, cannot name it: {mask_device_id(device_id)}")
    conn.execute("UPDATE devices SET friendly_name = ? WHERE device_id = ?", (friendly_name, device_id))
    conn.commit()


def get_device(conn: sqlite3.Connection, device_id: str) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM devices WHERE device_id = ?", (device_id,)).fetchone()


def list_devices(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM devices ORDER BY last_seen_at DESC").fetchall()


def forget_device(conn: sqlite3.Connection, device_id: str) -> bool:
    """Erase SwitchAgent's own memory of one device identity: its `devices`
    row, its MANUAL storage mappings (device_storage_mappings) and its
    cached DBI installed-title list (device_installed_titles). Returns
    False if no such row existed -- never invents one, same rule as
    set_device_friendly_name().

    Deliberately does NOT touch jobs / install_history /
    installation_batches. Those record what actually happened, and
    rewriting them so a Devices row disappears cleanly would be a lie
    about this app's own past. Nothing breaks by leaving them: an
    orphaned target_device_id is exactly the case web/services.py's
    device_label() already handles, falling back to the safe fingerprint.

    "Forget" is not "blocklist": if that same device is ever plugged in
    again, refresh_devices() -> upsert_device_seen() records it anew, with
    a fresh first_seen_at and no friendly_name. Callers are responsible
    for refusing to forget something still in use (a connected device,
    a device with unfinished queue work) -- see services.forget_device().
    """
    if get_device(conn, device_id) is None:
        return False
    conn.execute("DELETE FROM device_storage_mappings WHERE device_id = ?", (device_id,))
    conn.execute("DELETE FROM device_installed_titles WHERE device_id = ?", (device_id,))
    conn.execute("DELETE FROM device_emuiibo WHERE device_id = ?", (device_id,))
    conn.execute("DELETE FROM device_addons WHERE device_id = ?", (device_id,))
    conn.execute("DELETE FROM device_amiibo WHERE device_id = ?", (device_id,))
    conn.execute("DELETE FROM devices WHERE device_id = ?", (device_id,))
    conn.commit()
    return True


# ---------------------------------------------------------------------------
# install_history -- append-only record of terminal job outcomes. See
# queue_worker.py::_process_job() for the (only) writer.
# ---------------------------------------------------------------------------

def record_install_history(
    conn: sqlite3.Connection,
    *,
    job_id: int,
    title_id: Optional[str],
    display_name: str,
    target_device_id: str,
    target_storage: Optional[str],
    outcome: str,
    error: Optional[str] = None,
    bytes_total: Optional[int] = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO install_history (
            job_id, title_id, display_name, target_device_id, target_storage,
            outcome, error, bytes_total, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (job_id, title_id, display_name, target_device_id, target_storage, outcome, error, bytes_total, now_iso()),
    )
    conn.commit()
    return cur.lastrowid


def list_install_history(conn: sqlite3.Connection, *, limit: int = 200) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM install_history ORDER BY created_at DESC LIMIT ?", (limit,)
    ).fetchall()


def get_install_history_by_id(conn: sqlite3.Connection, history_id: int) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM install_history WHERE id = ?", (history_id,)).fetchone()


def list_all_install_history(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """W3-001/W3-003/W3-004: every recorded attempt, no LIMIT. Deliberately
    separate from list_install_history() rather than "just pass a huge
    limit": that function's 200-row default is the History PAGE's own
    pagination, and silently truncating there is harmless (the user sees
    the most recent page). The activity aggregation these three features do
    is a FILTER/count ("has this family ever had an unverified attempt?",
    "what has SwitchAgent ever attempted on this device?") -- a truncated
    read would produce a quietly WRONG answer, not just a shorter list.
    Read ONCE per request and bucketed in Python by the caller (never once
    per row -- same PERF-001 rule as queue_worker.find_family_base_name_source)."""
    return conn.execute("SELECT * FROM install_history ORDER BY created_at ASC").fetchall()


_USER_VERIFIED_OUTCOMES = (None, "SUCCESS", "FAILED")


def set_user_verified_outcome(conn: sqlite3.Connection, history_id: int, outcome: Optional[str]) -> None:
    """UI-003: the user's own annotation of DBI's on-console result, for a
    row whose TRANSPORT `outcome` is (and forever remains) DONE_UNVERIFIED.
    Never touches the `outcome` column itself -- that stays the honest,
    immutable transport result this project has always recorded (see
    docs/STATE.md's SD_INSTALL semantics fix). `outcome` here is
    deliberately named the same as the parameter, not the column, to be
    unambiguous in the caller: this sets user_verified_outcome, not
    install_history.outcome.

    outcome must be None (clears a previous user confirmation), 'SUCCESS',
    or 'FAILED' -- enforced here, in Python, matching this file's existing
    convention (see target_device_id in create_job()) rather than a SQL
    CHECK constraint. Only meaningful for a DONE_UNVERIFIED row -- the
    caller (web/services.py) is responsible for that restriction; this
    function itself only refuses a row that doesn't exist and an invalid
    outcome value, so it stays a plain, reusable persistence primitive."""
    if outcome not in _USER_VERIFIED_OUTCOMES:
        raise ValueError(f"invalid user-verified outcome: {outcome!r} -- expected one of {_USER_VERIFIED_OUTCOMES}")
    row = get_install_history_by_id(conn, history_id)
    if row is None:
        raise ValueError(f"no such install_history row: {history_id}")
    verified_at = now_iso() if outcome is not None else None
    conn.execute(
        "UPDATE install_history SET user_verified_outcome = ?, user_verified_at = ? WHERE id = ?",
        (outcome, verified_at, history_id),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# device_storage_mappings (UI-007) -- manual overrides of the AUTO logical
# storage-name mapping (see switchagent/mtp/windows.py's
# logical_storage_name()). Only ever written by an explicit user action
# (Devices page); the AUTO mapping itself is never a row here.
# ---------------------------------------------------------------------------

def list_device_storage_mappings(conn: sqlite3.Connection, device_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM device_storage_mappings WHERE device_id = ? ORDER BY raw_storage_name ASC",
        (device_id,),
    ).fetchall()


def get_device_storage_mapping(conn: sqlite3.Connection, device_id: str, raw_storage_name: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM device_storage_mappings WHERE device_id = ? AND raw_storage_name = ?",
        (device_id, raw_storage_name),
    ).fetchone()


def set_device_storage_mapping(
    conn: sqlite3.Connection, device_id: str, raw_storage_name: str, logical_name: str,
) -> None:
    """Plain persistence primitive -- the "only SD_CARD/SD_INSTALL, never
    NAND_*/SAVES/ALBUM" safety restriction and the "no two raw names
    mapped to the same logical name on one device" uniqueness rule are
    business rules enforced by the caller (web/services.py), not here --
    mirrors this module's existing split (e.g. db.retry_job() enforces
    status-transition rules; db.create_job() enforces only the one
    invariant that truly cannot be bypassed from anywhere)."""
    ts = now_iso()
    conn.execute(
        """
        INSERT INTO device_storage_mappings (device_id, raw_storage_name, logical_name, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT (device_id, raw_storage_name) DO UPDATE SET logical_name = excluded.logical_name, updated_at = excluded.updated_at
        """,
        (device_id, raw_storage_name, logical_name, ts),
    )
    conn.commit()


def clear_device_storage_mapping(conn: sqlite3.Connection, device_id: str, raw_storage_name: str) -> None:
    """Reverts a storage back to AUTO mapping -- deletes the override row,
    if any (a no-op, not an error, if none existed)."""
    conn.execute(
        "DELETE FROM device_storage_mappings WHERE device_id = ? AND raw_storage_name = ?",
        (device_id, raw_storage_name),
    )
    conn.commit()


def set_device_installed_base_title_ids(conn: sqlite3.Connection, device_id: str, base_title_ids: set[str]) -> None:
    """Replaces this device's ENTIRE confirmed set with base_title_ids --
    called once per successful DBI "InstalledApplications.csv" read
    (WebContext.refresh_devices()), never a partial add. A title no longer
    in the fresh read is a title DBI no longer confirms, and must stop
    being claimed here the moment we actually know that -- see this
    table's own schema comment (SCHEMA above) for why this exists at all."""
    now = now_iso()
    conn.execute("DELETE FROM device_installed_titles WHERE device_id = ?", (device_id,))
    conn.executemany(
        "INSERT INTO device_installed_titles (device_id, base_title_id, confirmed_at) VALUES (?, ?, ?)",
        [(device_id, title_id, now) for title_id in base_title_ids],
    )
    conn.commit()


def set_device_emuiibo(
    conn: sqlite3.Connection, device_id: str, state: dict, amiibo: list[dict], *, read_at: Optional[str] = None,
) -> None:
    """Replaces everything known about emuiibo on this device with one
    complete read (see the device_emuiibo schema comment). `read_at` keeps
    an earlier read's time when the change is not a new read (amiibo just
    removed and proven gone -- see web/emuiibo_service._forget_removed)."""
    import json

    conn.execute(
        "INSERT INTO device_emuiibo (device_id, state_json, read_at) VALUES (?, ?, ?) "
        "ON CONFLICT(device_id) DO UPDATE SET state_json = excluded.state_json, read_at = excluded.read_at",
        (device_id, json.dumps(state, ensure_ascii=False), read_at or now_iso()),
    )
    conn.execute("DELETE FROM device_amiibo WHERE device_id = ?", (device_id,))
    conn.executemany(
        "INSERT OR REPLACE INTO device_amiibo (device_id, path, data_json) VALUES (?, ?, ?)",
        [(device_id, a["path"], json.dumps(a, ensure_ascii=False)) for a in amiibo],
    )
    conn.commit()


def set_device_addons(conn: sqlite3.Connection, device_id: str, state: dict, *, read_at: Optional[str] = None) -> None:
    import json

    conn.execute(
        "INSERT INTO device_addons (device_id, state_json, read_at) VALUES (?, ?, ?) "
        "ON CONFLICT(device_id) DO UPDATE SET state_json = excluded.state_json, read_at = excluded.read_at",
        (device_id, json.dumps(state, ensure_ascii=False), read_at or now_iso()),
    )
    conn.commit()


def get_device_addons(conn: sqlite3.Connection, device_id: str) -> Optional[tuple[dict, str]]:
    """(state, read_at) of the last complete read, or None if never read."""
    import json

    row = conn.execute("SELECT state_json, read_at FROM device_addons WHERE device_id = ?", (device_id,)).fetchone()
    return (json.loads(row["state_json"]), row["read_at"]) if row else None


def get_device_emuiibo(conn: sqlite3.Connection, device_id: str) -> Optional[tuple[dict, list[dict], str]]:
    """(state, amiibo, read_at) of the last complete read, or None if this
    device was never read."""
    import json

    row = conn.execute("SELECT state_json, read_at FROM device_emuiibo WHERE device_id = ?",
                       (device_id,)).fetchone()
    if row is None:
        return None
    amiibo = [json.loads(r["data_json"]) for r in conn.execute(
        "SELECT data_json FROM device_amiibo WHERE device_id = ? ORDER BY path COLLATE NOCASE", (device_id,))]
    return json.loads(row["state_json"]), amiibo, row["read_at"]


def list_devices_with_emuiibo(conn: sqlite3.Connection) -> list[str]:
    return [row["device_id"] for row in conn.execute("SELECT device_id FROM device_emuiibo")]


def get_all_confirmed_installed_base_title_ids(conn: sqlite3.Connection) -> set[str]:
    """Union across every device this project has EVER successfully read
    an "Installed games" CSV from -- deliberately NOT limited to devices
    currently connected. That's the whole point of this table over
    WebContext's in-memory cache: unplugging a console, or restarting
    SwitchAgent, must not make it "forget" what DBI already confirmed
    there. Empty set if no device has ever had a successful CSV read."""
    return {
        row["base_title_id"]
        for row in conn.execute("SELECT DISTINCT base_title_id FROM device_installed_titles")
    }


# Used by queue_worker.py's install-order dependency check (Base Game ->
# Update/DLC/Mod, see docs/STATE.md's Quake II investigation). Both query
# install_history.title_id, which is always the JOB'S OWN content's
# title_id (see record_install_history's caller) -- so querying by the
# recovered base_title_id naturally, correctly scopes to "was the base
# variant itself ever installed", with no separate variant tag needed --
# EXCEPT for a mod checking its own dependency: a mod's base_title_id IS
# its own title_id (atmosphere/contents/<TITLE_ID>/ convention, no
# variant arithmetic), so without the target_storage filter below, a
# mod's OWN prior FAILED/INTERRUPTED attempt would be mistaken for "the
# base game failed" on its very next retry (found via a real regression
# in tests/test_stage41_recovery.py while building this feature). Base/
# update/DLC installs always target SD_INSTALL; mods always target
# SD_CARD -- filtering to SD_INSTALL rows means only a genuine Base Game
# GAME_PACKAGE install can ever answer "is the base installed", never a
# mod's own history about itself.
def has_successful_base_install(conn: sqlite3.Connection, target_device_id: str, title_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM install_history WHERE target_device_id = ? AND title_id = ? "
        "AND target_storage = 'SD_INSTALL' AND outcome IN ('DONE', 'DONE_UNVERIFIED') LIMIT 1",
        (target_device_id, title_id),
    ).fetchone()
    return row is not None


def has_failed_base_install(conn: sqlite3.Connection, target_device_id: str, title_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM install_history WHERE target_device_id = ? AND title_id = ? "
        "AND target_storage = 'SD_INSTALL' "
        "AND outcome IN ('FAILED', 'INTERRUPTED', 'SOURCE_CHANGED', 'DESTINATION_CONFLICT') LIMIT 1",
        (target_device_id, title_id),
    ).fetchone()
    return row is not None
