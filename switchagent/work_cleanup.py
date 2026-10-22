"""UI-004: safe cleanup of work/job-<id>/ staging directories.

manifest.build_manifest_and_stage() (see manifest.py) only ever copies
real payload bytes into work/job-<id>/ for ARCHIVE-sourced content (a
single "frozen" package file, or a whole "frozen" mod tree) -- a job
whose source is a bare file directly under inbox/ or config.LIBRARY_DIR
(the common case for this project's real usage: plain NSP/NSZ files, never
copied anywhere) leaves nothing there beyond the small manifest.json/
progress.json bookkeeping files. Only the former can ever grow large
enough to be worth cleaning up.

Never `shutil.rmtree(WORK_DIR)` wholesale: a job's staged content is still
needed for as long as that job might legitimately be retried --
db.retry_job() (Stage 4.1's in-place resume) reads progress.json and any
"frozen" files directly, and manifest.verify_manifest_against_source()
re-checks them against the frozen manifest before ever touching the
device. Only jobs in a truly TERMINAL, non-retryable-in-the-conservative
sense state (DONE / DONE_UNVERIFIED) and older than a retention window are
ever eligible -- see CLEANUP_ELIGIBLE_STATUSES. manifest.json/
progress.json themselves are always kept (tiny, and still useful as an
audit trail of what a job actually contained) -- only heavier "frozen"
payload files/directories are ever removed.

No automatic background cleanup task exists or is planned here -- this
module is only ever invoked by an explicit user action (the Web UI's
Settings page "Clean completed staging" button).
"""

from __future__ import annotations

import shutil
import sqlite3
import threading
import zlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from . import config, db, manifest as manifest_mod

# Every other status (CONFIRMED/WAITING_FOR_BASE/WAITING_FOR_DEVICE/RUNNING/
# DEVICE_UNAVAILABLE/FAILED/INTERRUPTED/SOURCE_CHANGED/DESTINATION_CONFLICT/
# BLOCKED_BY_DEPENDENCY) either represents an active transfer or a job a
# user might still retry (low-level db.retry_job() reuses the frozen
# manifest/progress -- see this module's own docstring) -- deleting staged
# content out from under any of those would be silent, irreversible data
# loss, not a cosmetic issue. The Web UI's OWN "Retry installation" button
# (services.retry_job) never reuses old staged content either way (always
# builds a fresh manifest), but the lower-level primitive still can be
# called directly, and is still exercised by tests/test_stage41_recovery.py
# -- so this module treats FAILED/INTERRUPTED etc. as still-sensitive,
# exactly like every other non-terminal status.
CLEANUP_ELIGIBLE_STATUSES = ("DONE", "DONE_UNVERIFIED")

DEFAULT_RETENTION_DAYS = 7.0

_KEPT_FILENAMES = frozenset({"manifest.json", "progress.json"})


def _dir_size(path: Path, *, exclude_names: frozenset[str] = frozenset()) -> int:
    if not path.is_dir():
        return 0
    total = 0
    for p in path.rglob("*"):
        if p.is_file() and p.name not in exclude_names:
            total += p.stat().st_size
    return total


@dataclass(frozen=True)
class EligibleJob:
    job_id: int
    work_dir: Path
    size_bytes: int


def _eligible_jobs(
    conn: sqlite3.Connection, *, retention_days: float, now: Optional[datetime] = None,
) -> list[EligibleJob]:
    """Every DONE/DONE_UNVERIFIED job whose finished_at is at least
    retention_days old AND whose work dir actually has non-metadata payload
    to remove -- a job with nothing to clean (the common bare-file case, or
    one already cleaned by a previous call, see execute_cleanup()'s own
    idempotence note) is simply not listed, never an error."""
    if now is None:
        now = datetime.now(timezone.utc)
    retention = timedelta(days=retention_days)

    result = []
    for row in db.list_jobs(conn):
        if row["status"] not in CLEANUP_ELIGIBLE_STATUSES:
            continue
        finished_at = row["finished_at"]
        if not finished_at:
            continue
        finished_dt = datetime.fromisoformat(finished_at)
        if now - finished_dt < retention:
            continue
        work_dir = manifest_mod.job_work_dir(row["id"])
        size = _dir_size(work_dir, exclude_names=_KEPT_FILENAMES)
        if size <= 0:
            continue
        result.append(EligibleJob(job_id=row["id"], work_dir=work_dir, size_bytes=size))
    return result


def preview_cleanup(conn: sqlite3.Connection, *, retention_days: float = DEFAULT_RETENTION_DAYS) -> dict:
    """Read-only -- what execute_cleanup() would do, without doing it.
    Shown to the user before they confirm (mandate: preview-then-confirm,
    never a silent delete)."""
    eligible = _eligible_jobs(conn, retention_days=retention_days)
    return {
        "total_work_size_bytes": _dir_size(config.WORK_DIR),
        "eligible_size_bytes": sum(j.size_bytes for j in eligible),
        "eligible_job_count": len(eligible),
        "eligible_job_ids": [j.job_id for j in eligible],
        "retention_days": retention_days,
    }


# The worker (after a job) and the preparation queue (before extracting the
# next archive) both release a finished batch, often at the same moment. Two
# rmtree()s over the same folder race: one fails on files the other already
# removed, and the preparation's "is it gone?" check then saw the folder the
# other thread was still emptying -- "Temporary payload cleanup failed" on a
# batch that was being cleaned up just fine. Harmless with one .nsp; hit every
# time with a 1,532-file amiibo collection (2026-09-25). One at a time.
_BATCH_CLEANUP_LOCK = threading.Lock()


def cleanup_batch_if_all_done(conn: sqlite3.Connection, batch_id: Optional[int]) -> bool:
    """Immediate cleanup of a batch's shared staging
    (manifest.batch_work_dir(batch_id) -- see manifest.py) the moment every
    one of its jobs has reached a fully-successful terminal state
    (DONE/DONE_UNVERIFIED). A single still-FAILED/INTERRUPTED/etc. sibling
    keeps the whole batch's staging alive (it may still be retried in
    place, see db.retry_job()) -- this is purely additive to the existing
    manual, retention-based execute_cleanup() above, which stays as the
    slower fallback for exactly that harder case (e.g. a job superseded by
    a NEW batch via the Web UI's own retry, whose original batch can never
    reach "all done"). Never touches config.LIBRARY_DIR or the DB -- only
    the batch's own filesystem staging directory. Returns whether anything
    was actually removed."""
    if batch_id is None:
        return False
    jobs = conn.execute("SELECT * FROM jobs WHERE batch_id=? OR payload_batch_id=?", (batch_id, batch_id)).fetchall()
    released = {j["id"] for j in jobs if j["status"] in CLEANUP_ELIGIBLE_STATUSES or j["abandoned"]}
    # Retried attempts keep immutable history. Their payload is released
    # only when the successor (possibly a chain) has finished or been abandoned.
    while True:
        parents = {j["retry_of_job_id"] for j in jobs if j["id"] in released and j["retry_of_job_id"]}
        if parents <= released:
            break
        released.update(parents)
    if not jobs or any(j["id"] not in released for j in jobs):
        return False
    batch_dir = manifest_mod.batch_work_dir(batch_id)
    with _BATCH_CLEANUP_LOCK:
        if (not batch_dir.is_dir() or batch_dir.is_symlink()
                or batch_dir.resolve().parent != config.WORK_DIR.resolve()):
            return False
        try:
            shutil.rmtree(batch_dir)
        except OSError:
            return False
        return not batch_dir.exists()


def execute_cleanup(conn: sqlite3.Connection, *, retention_days: float = DEFAULT_RETENTION_DAYS) -> dict:
    """Removes every file/directory under an eligible job's work dir EXCEPT
    manifest.json/progress.json. Idempotent: a job with nothing left to
    remove (already cleaned, or never had payload) simply isn't in
    _eligible_jobs()'s result -- calling this twice in a row is a normal,
    successful no-op the second time, never an error."""
    eligible = _eligible_jobs(conn, retention_days=retention_days)
    freed_bytes = 0
    cleaned_job_ids = []
    for job in eligible:
        for entry in list(job.work_dir.iterdir()):
            if entry.name in _KEPT_FILENAMES:
                continue
            if entry.is_dir():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink(missing_ok=True)
        freed_bytes += job.size_bytes
        cleaned_job_ids.append(job.job_id)
    return {"cleaned_job_count": len(cleaned_job_ids), "cleaned_job_ids": cleaned_job_ids, "freed_bytes": freed_bytes}


def archive_finished_jobs(conn: sqlite3.Connection, *, retention_days: float = DEFAULT_RETENTION_DAYS) -> int:
    """Folds work/job-<id>/ of finished jobs into one compressed row of the
    job_archive table and removes the folder, so work/ does not collect a
    folder per transfer forever. Only DONE/DONE_UNVERIFIED or abandoned jobs past the
    retention window, and only folders holding nothing but manifest.json/
    progress.json (a folder still carrying payload waits for
    execute_cleanup()). Row first, folder second: a failure in between
    leaves both, never neither. Returns how many folders were folded."""
    now = datetime.now(timezone.utc)
    folded = 0
    for row in db.list_jobs(conn):
        # Abandoned attempts can never be retried either (retry_job refuses
        # them), so their folders are as finished as a DONE job's.
        finished = row["status"] in CLEANUP_ELIGIBLE_STATUSES or row["abandoned"]
        if not finished:
            continue
        if row["finished_at"]:
            if now - datetime.fromisoformat(row["finished_at"]) < timedelta(days=retention_days):
                continue
        elif not row["abandoned"]:
            continue
        job_dir = manifest_mod.job_work_dir(row["id"])
        if (not job_dir.is_dir() or job_dir.is_symlink()
                or job_dir.resolve().parent != config.WORK_DIR.resolve()):
            continue
        entries = list(job_dir.iterdir())
        if any(e.name not in _KEPT_FILENAMES or not e.is_file() for e in entries):
            continue
        try:
            blobs = {e.name: zlib.compress(e.read_bytes(), 9) for e in entries}
            conn.execute(
                "INSERT OR REPLACE INTO job_archive (job_id, archived_at, manifest, progress) VALUES (?, ?, ?, ?)",
                (row["id"], now.isoformat(timespec="seconds"), blobs.get("manifest.json"), blobs.get("progress.json")),
            )
            conn.commit()
            shutil.rmtree(job_dir)
        except OSError:
            continue
        folded += 1
    return folded
