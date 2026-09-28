"""Sequential queue worker: processes CONFIRMED jobs one at a time, each
strictly against its own already-assigned target device AND its own frozen
content snapshot.

Two guarantees this module exists to enforce, both driven by findings from
a pre-Stage-5 architecture audit (see docs/STAGE4.1.md):

  1. Multi-device (stage 4): a job's target_device_id is fixed at creation
     and NEVER substituted. If that specific device isn't reachable right
     now, the job waits -- it is not sent to some other, currently-
     available device just because one happens to be plugged in.

  2. Content pinning (stage 4.1): a job's target CONTENT is equally fixed
     at creation, as a manifest (switchagent/manifest.py) built from the
     exact PreviewReport the user was shown. The worker never re-scans
     inbox/ to decide what to send -- it re-verifies the frozen manifest
     against whatever is currently on disk, and refuses (does not silently
     send something different) if they've drifted apart. It also never
     assumes an existing destination file is safe to skip just because it
     exists -- only a file THIS job's own prior send_file call is recorded
     (in manifest.py's progress.json) as having delivered gets skipped;
     anything else found already present is treated as an unresolved
     conflict, not as proof of a prior successful send -- unless reading it
     back proves it holds exactly the bytes being sent (small files on a
     real filesystem only; see manifest.py's docstring). A matching name or
     size is never taken as proof.

One worker, one job, one device at a time (the explicitly allowed minimal
shape for this stage) -- but nothing here assumes there is only ever one
device in the system. DeviceRegistry maps many device_id's to their own
MtpBackend instances; run_worker_once() scans CONFIRMED jobs in creation
order and processes the FIRST one whose target device is currently
reachable, skipping (not redirecting) any it passes over.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from . import db
from . import emuiibo
from . import manifest as manifest_mod
from . import sd_files
from . import title_id as title_id_mod
from . import work_cleanup
from .model import ContentType
from .mtp.base import MtpBackend, TransferStatus
from .mtp.errors import (
    DeviceDisconnectedError, DeviceNotFoundError, FileAlreadyExistsError, MtpError, TransferAborted,
)
from .preview import PreviewReport
from .transfer import STORAGE_SD_CARD, STORAGE_SD_INSTALL

log = logging.getLogger("switchagent.queue_worker")


class DeviceRegistry:
    """Maps a stable device_id to the MtpBackend instance that can reach it.
    Registering a backend here does NOT mean the device is currently
    plugged in -- run_worker_once() finds that out the same way it always
    would for a single device, by calling backend.connect() and handling
    DeviceNotFoundError. This is deliberately just a dict wrapper: device
    identity/enumeration is the backend's concern (see mtp/base.py), not
    this registry's -- it only remembers which backend instance to ask for
    a given device_id."""

    def __init__(self):
        self._backends: dict[str, MtpBackend] = {}

    def register(self, device_id: str, backend: MtpBackend) -> None:
        self._backends[device_id] = backend

    def get(self, device_id: str) -> Optional[MtpBackend]:
        return self._backends.get(device_id)

    def known_device_ids(self) -> list[str]:
        return list(self._backends)


@dataclass
class JobRunOutcome:
    job_id: int
    status: str
    error: Optional[str] = None
    # False only for a job the user's Abort cancelled before a single byte
    # moved (see _run_job_transfer): exactly like services.cancel_job() on a
    # queued job, that leaves no History row -- nothing was attempted.
    record_history: bool = True


# Answers True once the user has pressed Abort on the Queue page (see
# WebContext.request_abort()). Polled, never pushed: at every point below
# where a transfer can stop without lying about what reached the device.
ShouldAbort = Callable[[], bool]


def _never_abort() -> bool:
    return False


def _target_storage_for(report: PreviewReport) -> str:
    if report.content_type is ContentType.GAME_PACKAGE:
        return STORAGE_SD_INSTALL
    if report.content_type in (ContentType.ATMOSPHERE_MOD, ContentType.SD_FILES,
                               ContentType.EMUIIBO, ContentType.AMIIBO, ContentType.ADDON):
        return STORAGE_SD_CARD
    raise manifest_mod.ManifestError(
        f"content type {report.content_type.value} is not eligible for transfer"
    )


def create_job_from_report(
    conn: sqlite3.Connection,
    report: PreviewReport,
    *,
    action: str,
    target_device_id: str,
    inbox_item_id: Optional[int] = None,
    library_item_id: Optional[int] = None,
    batch_id: Optional[int] = None,
    force_overwrite: bool = False,
    retry_of_job_id: Optional[int] = None,
) -> int:
    """The one correct way to create a job that can actually be confirmed
    and run. Builds and freezes a content manifest from `report` -- the
    exact object the user was shown during preview, never re-derived from a
    fresh scan -- then creates the PENDING_CONFIRM row and attaches the
    manifest to it before returning.

    Exactly one of inbox_item_id (Stage 2's inbox/ pipeline) or
    library_item_id (Web UI's library_items) must be given -- see
    db.create_job() for the enforced invariant.

    force_overwrite (W3-006 Override): threaded straight to db.create_job()
    -- see that function's own comment on the column. Only
    services.override_job() ever passes True.

    retry_of_job_id: threaded straight to db.create_job() -- see that
    column's own comment. Only services.retry_job()'s direct-source path
    passes this (the staged/frozen-payload path sets it separately, after
    this returns, alongside manifest_path/payload_batch_id).

    Raises manifest.ManifestError if `report` has nothing locally
    stageable yet (MIXED/UNKNOWN content, or an archive that was never
    extracted with preview_path(path, extract=True)). If manifest building
    fails AFTER the job row was inserted (staging error), the row is left
    behind as FAILED rather than stuck, unconfirmable, in PENDING_CONFIRM
    forever -- but the exception still propagates so the caller knows
    creation did not succeed."""
    target_storage = _target_storage_for(report)  # raises before any DB row exists, for MIXED/UNKNOWN
    job_id = db.create_job(
        conn, inbox_item_id=inbox_item_id, library_item_id=library_item_id, action=action,
        target_storage=target_storage, target_device_id=target_device_id, batch_id=batch_id,
        force_overwrite=force_overwrite, retry_of_job_id=retry_of_job_id,
    )
    try:
        manifest = manifest_mod.build_manifest_and_stage(
            report, job_id, target_storage=target_storage, batch_id=batch_id,
        )
    except manifest_mod.ManifestError as exc:
        db.update_job_status(conn, job_id, "FAILED", error=f"could not build manifest: {exc}")
        db.log_job_event(conn, job_id, f"manifest build failed: {exc}")
        raise

    db.set_job_manifest_path(conn, job_id, str(manifest_mod.manifest_path_for(job_id)))
    db.log_job_event(conn, job_id, f"manifest frozen: {len(manifest.files)} file(s)")
    return job_id


# Content whose manifest title_id IS its game's family id rather than an id
# of its own that the variant arithmetic would have to decode.
_TAGGED_WITH_FAMILY_ID = (ContentType.ATMOSPHERE_MOD.value, ContentType.SD_FILES.value)


def _base_title_id_for_dependency_check(manifest) -> Optional[str]:
    """The family/base TITLE_ID a job's manifest actually depends on for
    install ordering -- mods are tagged with the base's own TITLE_ID
    directly; everything else recovers it via
    title_id.classify_title_variant(). None if the manifest has no
    title_id at all. Shared with _dependency_status below (same
    computation) so run_worker_once's "waiting for base game (X)" message
    always names the ACTUAL base being waited on -- BUG: it used to show
    manifest.title_id (the waiting job's OWN id, e.g. the Update's or
    DLC's own TITLE_ID), not the base's, which is exactly backwards."""
    if not manifest.title_id:
        return None
    if manifest.content_type in _TAGGED_WITH_FAMILY_ID:
        return manifest.title_id
    return title_id_mod.classify_title_variant(manifest.title_id).base_title_id


def _dependency_status(conn: sqlite3.Connection, manifest, target_device_id: str) -> Optional[str]:
    """Enforces Base Game -> Update/DLC/Mod install order (see
    docs/STATE.md's Quake II investigation: an Update was allowed to
    install while its Base Game job was still outstanding, and the base
    game itself silently never completed -- Horizon showed an icon for a
    title whose actual application data was never delivered).

    Returns None if this job has no unmet dependency -- either it IS a
    base-variant job itself (bases have no dependency), its base is
    already confirmed installed per OUR OWN install_history (never a live
    device read -- this hardware has no MTP-visible installed-games list,
    same limitation as everywhere else in this project), or there simply
    is no competing Base Game job for this title on this device right now
    (nothing in the SAME batch/session to wait behind -- see the
    deliberate scoping note below). Otherwise returns "WAITING_FOR_BASE"
    (a Base Game job for this title exists and hasn't succeeded yet --
    try again next pass) or "BLOCKED_BY_DEPENDENCY" (install_history shows
    the base variant itself failed/was interrupted/conflicted -- this job
    will never auto-run).

    Deliberately scoped to an ACTUAL outstanding base job, not "no
    install_history exists for this title at all": this hardware cannot
    prove what's already installed on the console (no MTP-visible
    installed-games list -- see docs/STAGE5A-MTP-RESEARCH.md), so treating
    "no history" as "must wait forever" would make it impossible to ever
    install an update/DLC/mod for a game the user already owns/installed
    by some other means, which was never broken and is not what the real
    incident (see docs/STATE.md's Quake II investigation) was about -- that
    was specifically a base and its update racing within the SAME
    confirmed batch. What's actually enforced: an update/DLC/mod job
    never overtakes a base job for the SAME title that is genuinely still
    in flight or already known to have failed.

    Cannot classify without a title_id -- returns None (no dependency
    enforced) rather than blocking on an unknown; this mirrors every
    other "cannot determine, so don't guess" rule in this codebase."""
    if not manifest.title_id:
        return None
    if manifest.content_type == ContentType.SD_FILES.value:
        # A game's switch/ folder is copied BEFORE the game now, not after
        # it (web/preparation._variant_rank: a port's forwarder comes last,
        # so its icon appears only once what it launches is on the card).
        # Waiting here for a base job would deadlock against the forwarder
        # job the preparation already created behind it.
        return None

    if manifest.content_type in _TAGGED_WITH_FAMILY_ID:
        # mods are tagged with the base's own TITLE_ID directly -- never a
        # base game itself, so it waits behind one that is in flight.
        base_title_id = manifest.title_id
    else:
        variant, base_title_id = title_id_mod.classify_title_variant(manifest.title_id)
        if variant == "BASE":
            return None

    # A currently-outstanding (non-terminal) Base job for this exact
    # title+device always outranks install_history -- checked FIRST, before
    # any success/failure history lookup. This prevents stale History from
    # letting a new Update/DLC overtake a brand new Base: re-installing a
    # deleted game (a fresh Base+Update+DLC batch) must never let the new
    # Update start just because an OLDER Base install once succeeded on
    # this device -- the new Base, still in flight, must reach a terminal
    # outcome first. DONE_UNVERIFIED still counts as a sufficient transport
    # outcome the moment the new Base finishes (see
    # has_successful_base_install) -- only a base job that is genuinely
    # still non-terminal is prioritized over history here.
    if _has_outstanding_base_job(conn, target_device_id, base_title_id):
        return "WAITING_FOR_BASE"
    if db.has_successful_base_install(conn, target_device_id, base_title_id):
        return None
    if db.has_failed_base_install(conn, target_device_id, base_title_id):
        return "BLOCKED_BY_DEPENDENCY"
    return None


_NON_TERMINAL_JOB_STATUSES = ("PENDING_CONFIRM", "CONFIRMED", "DEVICE_UNAVAILABLE", "RUNNING", "VERIFYING")


def _has_outstanding_base_job(conn: sqlite3.Connection, target_device_id: str, base_title_id: str) -> bool:
    """Is there a Base Game job for this exact title, on this device, that
    hasn't reached a terminal state yet? Deliberately NOT "did a base job
    ever exist" -- a base job that already finished is covered by
    install_history (checked before this is ever called). jobs.title_id
    isn't a column (title_id only lives in each job's frozen manifest
    file), so this loads the manifest for every non-terminal job on this
    device -- fine at this project's scale (a personal library's worth of
    concurrent jobs, not thousands)."""
    placeholders = ",".join("?" * len(_NON_TERMINAL_JOB_STATUSES))
    rows = conn.execute(
        f"SELECT id FROM jobs WHERE target_device_id = ? AND status IN ({placeholders})",
        (target_device_id, *_NON_TERMINAL_JOB_STATUSES),
    ).fetchall()
    for row in rows:
        try:
            candidate = manifest_mod.load_manifest(row["id"])
        except (FileNotFoundError, ValueError, KeyError, TypeError):
            # FAULT-001: a malformed (not just missing) manifest on some
            # OTHER non-terminal job on this device must never crash the
            # dependency check for the job actually being evaluated right
            # now -- that OTHER job's own manifest corruption (if any) is
            # handled separately, in run_worker_once() itself, the next
            # time IT is the one being considered.
            continue
        if candidate.content_type != ContentType.GAME_PACKAGE.value or not candidate.title_id:
            continue
        variant, candidate_base_id = title_id_mod.classify_title_variant(candidate.title_id)
        if variant == "BASE" and candidate_base_id == base_title_id:
            return True
    return False


def _decide_device_absence_status(job_row: sqlite3.Row) -> str:
    """UI-005: distinguishes a job that has NEVER started a transfer
    (attempt_count == 0, no delivered files recorded in progress.json) --
    eligible for the gentler, self-resolving WAITING_FOR_DEVICE -- from one
    that already had at least one attempt or partial delivery, which keeps
    the existing, more conservative DEVICE_UNAVAILABLE (requires an
    explicit manual retry; db.retry_job() deliberately never auto-retries
    an already-attempted job, see its own docstring). A job already
    WAITING_FOR_DEVICE is re-evaluated by this same check on every pass
    (see list_confirmed_jobs()) -- that's what makes it self-resolving the
    moment its SAME target_device_id reappears, no manual action needed.
    Deliberately narrow: a job that already touched the device in any way
    keeps the older, more cautious status -- this never widens what
    DEVICE_UNAVAILABLE used to mean, only carves out the genuinely-never-
    started case."""
    if job_row["auto_resume"]:
        # Its connection dropped mid-job (see _wait_for_reconnect): waiting
        # for the console to come back is exactly what it is doing.
        return "WAITING_FOR_DEVICE"
    if job_row["attempt_count"] > 0:
        return "DEVICE_UNAVAILABLE"
    if manifest_mod.load_progress(job_row["id"]):
        return "DEVICE_UNAVAILABLE"
    return "WAITING_FOR_DEVICE"


# How long a job whose console stopped answering waits before trying again
# on its own while that console stays connected. Seeing the console go away
# and come back -- a replugged cable, MTP restarted in DBI -- skips the wait.
RECONNECT_RETRY_SECONDS = 300.0


def _wait_for_reconnect(
    conn: sqlite3.Connection, job_id: int, bytes_done: int, reason: str, *,
    in_flight: Optional[str] = None, storage: Optional[str] = None,
) -> JobRunOutcome:
    """The connection to the console dropped mid-job. Not a failure of the
    job: every file it delivered is in progress.json and is not sent again,
    so it waits as WAITING_FOR_DEVICE and continues by itself (by the
    user's explicit choice, 2026-09-26, after a console that had stopped
    answering held a job at 0% until the cable was replugged). Errors,
    conflicts and changed sources still stop a job for good -- only a lost
    connection resumes. No History row: nothing has ended."""
    from datetime import datetime, timedelta, timezone

    if in_flight and storage != STORAGE_SD_INSTALL:
        # May be half written on the card now; resuming replaces it. (An
        # install node keeps nothing half-sent to replace.)
        manifest_mod.mark_replaceable(job_id, in_flight)
    error = (
        f"{reason}. Reconnect the Switch, or restart MTP in DBI -- the install continues from here "
        f"by itself (without that, it tries again in {int(RECONNECT_RETRY_SECONDS // 60)} min)"
    )
    resume_after = (datetime.now(timezone.utc) + timedelta(seconds=RECONNECT_RETRY_SECONDS)).isoformat(timespec="seconds")
    db.update_job_status(conn, job_id, "WAITING_FOR_DEVICE", bytes_done=bytes_done, error=error)
    db.set_job_auto_resume(conn, job_id, resume_after)
    db.log_job_event(conn, job_id, f"connection lost ({reason}) -> WAITING_FOR_DEVICE, resumes on reconnect")
    return JobRunOutcome(job_id=job_id, status="WAITING_FOR_DEVICE", error=error, record_history=False)


def _resume_not_due(job_row: sqlite3.Row) -> bool:
    """A job waiting after a lost connection whose console never went away:
    too early to knock again (see RECONNECT_RETRY_SECONDS)."""
    if not job_row["auto_resume"] or not job_row["resume_after"]:
        return False
    from datetime import datetime, timezone

    return datetime.now(timezone.utc) < datetime.fromisoformat(job_row["resume_after"])


def _note_device_gone(conn: sqlite3.Connection, job_row: sqlite3.Row) -> None:
    """The console of a job waiting after a lost connection has been seen
    gone: when it is back it is a fresh connection, so no need to wait."""
    if job_row["auto_resume"] and job_row["resume_after"]:
        db.set_job_auto_resume(conn, job_row["id"], None)


def run_worker_once(
    conn: sqlite3.Connection, registry: DeviceRegistry, *, should_abort: Optional[ShouldAbort] = None,
) -> Optional[JobRunOutcome]:
    """Does at most one unit of work: finds the oldest CONFIRMED (or
    WAITING_FOR_BASE/WAITING_FOR_DEVICE) job whose target device is
    currently reachable AND whose install-order dependency (Base Game ->
    Update/DLC/Mod, see _dependency_status above) is satisfied, fully
    processes it (to DONE, FAILED, INTERRUPTED, SOURCE_CHANGED,
    DESTINATION_CONFLICT, or BLOCKED_BY_DEPENDENCY), and returns its
    outcome. Jobs it passes over because their device isn't reachable get
    one of two statuses (see _decide_device_absence_status above):
    WAITING_FOR_DEVICE for a job that never started a transfer at all
    (non-terminal, automatically re-checked every pass, no manual action
    needed -- self-resolving the moment its target device reappears), or
    the existing, unchanged DEVICE_UNAVAILABLE for one that already had an
    attempt or partial delivery (requires an explicit db.retry_job() call;
    never retried automatically on a later pass -- see docs/STAGE4.1.md).
    Jobs waiting on their base game are marked WAITING_FOR_BASE and
    re-checked every pass automatically (no explicit action needed, unlike
    DEVICE_UNAVAILABLE) -- this is also what guarantees an Update/DLC/Mod
    can never reach RUNNING while its Base Game job is still outstanding:
    it simply never gets past this check to reach _process_job() at all.

    Returns None if there was nothing processable right now -- either the
    queue is empty, or every remaining job's target device is unreachable
    or dependency-blocked. Never picks a different device than the one a
    job was created with.

    should_abort: see ShouldAbort. Once it answers True, this pass starts
    nothing new (the job list it read may predate the Abort that cancelled
    those jobs), and a job already transferring stops at its next safe
    point -- see _run_job_transfer."""
    from .mtp.windows import device_fingerprint

    should_abort = should_abort or _never_abort

    for job in db.list_confirmed_jobs(conn):
        device_id = job["target_device_id"]
        backend = registry.get(device_id)

        if backend is None:
            status = _decide_device_absence_status(job)
            # Fingerprint, never the raw device_id -- it embeds the
            # device's real USB serial number (see mtp/windows.py's
            # mask_device_id docstring), and this message is stored in
            # jobs.error/job_log, which the Web UI renders directly to a
            # human (queue.html's {{ j.error }}) -- found while adding
            # this status: the pre-existing DEVICE_UNAVAILABLE message
            # below had the same gap, fixed here alongside it since both
            # now share this exact code path.
            error = f"device '{device_fingerprint(device_id)}' is not registered with this worker"
            _note_device_gone(conn, job)
            if job["auto_resume"]:
                continue  # keeps its own "reconnect the Switch" message
            if status != job["status"]:  # avoid rewriting/re-logging every pass while still absent
                db.update_job_status(conn, job["id"], status, error=error)
                db.log_job_event(conn, job["id"], f"{error} -> {status}")
            continue

        try:
            backend.connect()
        except DeviceNotFoundError as exc:
            status = _decide_device_absence_status(job)
            error = f"device '{device_fingerprint(device_id)}' not currently reachable: {exc}"
            _note_device_gone(conn, job)
            if job["auto_resume"]:
                continue  # keeps its own "reconnect the Switch" message
            if status != job["status"]:
                db.update_job_status(conn, job["id"], status, error=error)
                db.log_job_event(conn, job["id"], f"{error} -> {status}")
            continue

        if _resume_not_due(job):
            continue

        try:
            manifest = manifest_mod.load_manifest(job["id"])
        except (FileNotFoundError, ValueError, KeyError, TypeError) as exc:
            # FAULT-001: a job's manifest.json can only ever go missing or
            # become unreadable through something outside this application's
            # own control (manual deletion, disk corruption, a future bug) --
            # never guess what it would have said. Fail this ONE job
            # outright (a corrupted content snapshot is not a transient
            # condition a later pass could resolve on its own) and keep
            # scanning for other, still-processable work in this same call.
            # Previously this call was unguarded and raised straight out of
            # this for loop -- uncaught here or anywhere before
            # _worker_loop's own broad handler -- which meant the SAME
            # broken job (still first in creation order) would raise again
            # on every subsequent pass, silently blocking every job behind
            # it, for every device, forever, not just failing this one job.
            error = f"job manifest is missing or unreadable: {type(exc).__name__}: {exc}"
            db.update_job_status(conn, job["id"], "FAILED", error=error, finished_at=db.now_iso())
            db.log_job_event(conn, job["id"], error)
            db.record_install_history(
                conn, job_id=job["id"], title_id=None,
                display_name=display_name_for_job(conn, job),
                target_device_id=device_id, target_storage=job["target_storage"],
                outcome="FAILED", error=error, bytes_total=None,
            )
            continue

        dependency = _dependency_status(conn, manifest, device_id)

        if dependency == "WAITING_FOR_BASE":
            if job["status"] != "WAITING_FOR_BASE":  # write once, not every pass -- avoid spamming job_log
                error = (
                    f"waiting for base game ({_base_title_id_for_dependency_check(manifest)}) "
                    "to install successfully first"
                )
                db.update_job_status(conn, job["id"], "WAITING_FOR_BASE", error=error)
                db.log_job_event(conn, job["id"], "base game not yet installed -> WAITING_FOR_BASE")
            continue

        if dependency == "BLOCKED_BY_DEPENDENCY":
            error = (
                f"base game install for {_base_title_id_for_dependency_check(manifest)} "
                "did not succeed -- see History"
            )
            db.update_job_status(conn, job["id"], "BLOCKED_BY_DEPENDENCY", error=error, finished_at=db.now_iso())
            db.log_job_event(conn, job["id"], f"blocked: {error}")
            db.record_install_history(
                conn, job_id=job["id"], title_id=manifest.title_id,
                display_name=display_name_for_job(conn, job),
                target_device_id=device_id, target_storage=job["target_storage"],
                outcome="BLOCKED_BY_DEPENDENCY", error=error,
                bytes_total=sum(f.size for f in manifest.files),
            )
            continue

        # Found a job whose target device is reachable right now AND whose
        # dependency (if any) is satisfied -- process exactly this one and
        # stop (sequential: one worker, one job at a time, per this
        # stage's explicitly allowed minimal shape).
        if should_abort():
            return None
        return _process_job(conn, backend, job, should_abort=should_abort)

    return None


def find_family_base_name_source(
    conn: sqlite3.Connection, family_title_id: str, *, library_items: Optional[list] = None,
) -> Optional[sqlite3.Row]:
    """For a family TITLE_ID (a mod's own title_id IS its family id,
    atmosphere/contents/<TITLE_ID>/ convention; an update/DLC's family id
    is title_id.classify_title_variant()'s recovered base_title_id), finds
    the best available library_items row to borrow a display name from --
    preferring an actual Base Game, falling back to an Update or DLC if no
    base is currently indexed. Returns None if nothing in the library is
    known for that family at all -- callers keep showing the raw TITLE_ID
    rather than inventing a name (title_id.py's "never guess" principle,
    inherited here). Shared by both the Web UI's Library grouping
    (switchagent/web/services.py) and Queue/History (below) so a mod is
    never named one way in one place and differently in another -- found
    via a real UI bug report: a Bread and Fred mod showed as
    "Mod -- 0100AF401B6A4000" (its own TITLE_ID) instead of "Bread and
    Fred", in both places, until this existed.

    PERF-001: `library_items` lets a caller that is about to call this once
    PER ROW of a whole listing (e.g. Library grouping/Queue/History
    rendering) fetch+pass the full `db.list_library_items(conn)` list ONCE
    instead of this function silently re-querying and re-classifying the
    ENTIRE table on every single call -- confirmed via profiling to turn an
    O(mods x library size) full-table rescan into the dominant cost of
    rendering the Library page at real-world scale (measured: ~12s of a
    ~12.2s call for 5,340 rows / 200 mods, ~98% of it inside this function
    alone). Passing nothing preserves the exact old (correct, just
    potentially slower at scale) behavior for any caller that only ever
    needs a single one-off lookup."""
    if library_items is None:
        library_items = db.list_library_items(conn)
    candidates = []
    for row in library_items:
        if row["content_type"] != ContentType.GAME_PACKAGE.value or not row["title_id"]:
            continue
        variant, base_id = title_id_mod.classify_title_variant(row["title_id"])
        if base_id != family_title_id:
            continue
        candidates.append((0 if variant == "BASE" else 1, row))
    if not candidates:
        return None
    candidates.sort(key=lambda vc: vc[0])
    return candidates[0][1]


def resolve_library_item_display_name(
    conn: sqlite3.Connection, row: sqlite3.Row, *, library_items: Optional[list] = None,
) -> str:
    """The full filename/folder-name-based label (see display_name_for_job
    below for the .name-not-.stem convention Queue/History use) -- with
    the mod-parent-name fallback from find_family_base_name_source above
    applied for MOD_FOLDER rows, whose own folder name is ALWAYS just a
    bare TITLE_ID (never a product name at all, unlike a package
    filename). See find_family_base_name_source's own docstring for why
    `library_items` matters at scale."""
    raw_name = Path(row["absolute_path"]).name
    if row["content_type"] == ContentType.ADDON.value:
        # "sdout.zip" names nothing; the catalog's name and the version in
        # the release itself do ("Ultrahand Overlay 2.5.3").
        from . import addons

        found = sd_files.row_details(row).get("addon") or {}
        addon = addons.by_id(found.get("id") or "")
        if addon is not None:
            return f"{addon.name} {found['version']}" if found.get("version") else addon.name
        return raw_name
    if row["content_type"] == ContentType.SD_FILES.value:
        # "switch.7z" or a folder called "switch" names the SD layout, not
        # the game: borrow the game's name when it belongs to one (exactly
        # as a mod does), the release folder's otherwise.
        if row["title_id"]:
            source = find_family_base_name_source(conn, row["title_id"], library_items=library_items)
            if source is not None:
                return Path(source["absolute_path"]).name
        return sd_files.row_display_name(row)
    if row["item_type"] != "MOD_FOLDER" or not row["title_id"]:
        return raw_name
    source = find_family_base_name_source(conn, row["title_id"], library_items=library_items)
    return Path(source["absolute_path"]).name if source is not None else raw_name


def display_name_for_job(
    conn: sqlite3.Connection, job_row: sqlite3.Row, *, library_items: Optional[list] = None,
    mod_suffix: bool = True,
) -> str:
    """Best-effort human-readable label for install_history -- derived from
    whichever source row this job came from (library_items or inbox_items,
    see db.py). Falls back to a generic label if that row is gone by the
    time this runs (e.g. deleted from the library after the job already
    ran) -- history must still be recordable, never blocked on the source
    still existing. See find_family_base_name_source's own docstring for
    why `library_items` matters at scale.

    A mod's own label is often borrowed from its base game (see
    resolve_library_item_display_name's mod-parent-name fallback), which
    can be confusing in Queue/History: it looks identical to the base
    game's OWN job, with nothing to say "this one is a mod, not the game".
    mods always target SD_CARD (base/update/DLC
    installs always target SD_INSTALL, see _target_storage_for) -- an
    unambiguous, already-available signal to append " -- Mod" with,
    baked into install_history.display_name at recording time (this same
    function), not just a Queue-only display trick.

    mod_suffix: pass False from a surface that already says "Mod" some
    other way -- Queue rows carry a [Mod] badge now (see web/services.py's
    _job_variant_role), and a row reading "Russian Language Mod — Mod
    [Mod]" says it three times. History has no badge, so it keeps the
    suffix and is the reason this is a parameter rather than a deletion."""
    if job_row["library_item_id"] is not None:
        row = db.get_library_item_by_id(conn, job_row["library_item_id"])
        if row is not None:
            name = resolve_library_item_display_name(conn, row, library_items=library_items)
            return _with_mod_suffix(name, job_row, row["content_type"]) if mod_suffix else name
    if job_row["inbox_item_id"] is not None:
        row = db.get_inbox_item_by_id(conn, job_row["inbox_item_id"])
        if row is not None:
            name = Path(row["relative_path"]).name
            return _with_mod_suffix(name, job_row, row["content_type"]) if mod_suffix else name
    return f"job {job_row['id']}"


def _with_mod_suffix(name: str, job_row: sqlite3.Row, content_type: Optional[str] = None) -> str:
    # A game's switch/ folder also goes to SD_CARD, and calling it a mod
    # would be wrong: it is half of the game, not a change to one.
    if content_type == ContentType.SD_FILES.value:
        return f"{name} — SD files"
    if content_type == ContentType.AMIIBO.value:
        return f"{name} — Amiibo"
    if content_type in (ContentType.EMUIIBO.value, ContentType.ADDON.value):
        return name  # "emuiibo-v1.1.3.zip", "Ultrahand Overlay 2.5.3.zip" already say what it is
    if job_row["target_storage"] == STORAGE_SD_CARD:
        return f"{name} — Mod"
    return name


def _process_job(
    conn: sqlite3.Connection, backend: MtpBackend, job_row: sqlite3.Row, *,
    should_abort: ShouldAbort = _never_abort,
) -> JobRunOutcome:
    """Thin wrapper around _run_job_transfer(): every outcome it can
    possibly return is already terminal for THIS attempt (there is no
    "still running" return value -- RUNNING is only ever an intermediate
    status written to the DB mid-function, never the function's own
    result), so every call here gets exactly one install_history row (see
    docs/WEB-UI.md, point 12 -- History must show failures/interruptions
    too, not just successes).

    The try/except here is a deliberate safety net, found necessary by a
    real incident (see docs/STATE.md's Quake II investigation): an
    unexpected, non-MtpError exception raised deep inside a transfer (that
    one was a win32com AttributeError from a COM object invalidated by an
    unrelated thread -- see WebContext's class-level note in
    switchagent/web/context.py for the actual root cause and its fix) used
    to propagate all the way up to _worker_loop's own broad handler, which
    only logs and moves on -- leaving the job stuck at RUNNING forever
    (never terminal, never in install_history, invisible as an error to
    the user) while the worker went on to process the NEXT confirmed job
    as if nothing had happened. Catching it HERE, where job_id is known,
    means ANY unexpected failure -- whatever future cause -- still leaves
    an honest INTERRUPTED record instead of a silent gap. INTERRUPTED
    (not FAILED) because we genuinely don't know how much, if anything,
    reached the device -- same honesty standard already used for a
    detected mid-transfer disconnect."""
    try:
        outcome = _run_job_transfer(conn, backend, job_row, should_abort=should_abort)
    except Exception as exc:  # noqa: BLE001 -- see docstring: must never leave a job silently stuck
        error = f"unexpected error during transfer: {type(exc).__name__}: {exc}"
        db.update_job_status(conn, job_row["id"], "INTERRUPTED", error=error)
        db.log_job_event(conn, job_row["id"], f"unexpected exception, marked INTERRUPTED: {exc!r}")
        log.exception("unexpected exception while processing job %s", job_row["id"])
        outcome = JobRunOutcome(job_id=job_row["id"], status="INTERRUPTED", error=error)

    # FAULT-001: this job's own manifest could become unreadable somewhere
    # between _run_job_transfer() having already handled it (or having
    # itself failed for the identical reason, already caught above) and
    # this history-recording step -- never let recording history be the
    # reason a terminal outcome silently loses its permanent record. The
    # job's own status was already durably set above regardless of what
    # happens here.
    try:
        manifest = manifest_mod.load_manifest(job_row["id"])
        title_id = manifest.title_id
        bytes_total = sum(f.size for f in manifest.files)
    except (FileNotFoundError, ValueError, KeyError, TypeError) as exc:
        title_id = None
        bytes_total = None
        db.log_job_event(
            conn, job_row["id"],
            f"could not reload manifest for history recording: {type(exc).__name__}: {exc}",
        )

    if outcome.record_history:
        db.record_install_history(
            conn, job_id=job_row["id"], title_id=title_id,
            display_name=display_name_for_job(conn, job_row),
            target_device_id=job_row["target_device_id"], target_storage=job_row["target_storage"],
            outcome=outcome.status, error=outcome.error,
            bytes_total=bytes_total,
        )
    # The instant this job's own outcome is durable, see whether its whole
    # batch (if any) is now fully done and can have its shared staging
    # removed -- see work_cleanup.cleanup_batch_if_all_done's own docstring
    # for why this is safe (a still-retryable sibling keeps everything
    # alive) and why it's additive to the existing manual sweep.
    work_cleanup.cleanup_batch_if_all_done(conn, job_row["batch_id"])
    if job_row["payload_batch_id"]:
        work_cleanup.cleanup_batch_if_all_done(conn, job_row["payload_batch_id"])
    return outcome


ABORTED_BEFORE_START_ERROR = "cancelled by user (Abort) before it started"


@dataclass
class AmiiboPlan:
    """What an AMIIBO job decided, amiibo by amiibo, before sending anything
    (see _plan_amiibo)."""
    skip: set                 # destinations not to send
    already: list             # amiibo already on the console, left exactly as they are
    conflicts: list           # a different amiibo already has that place -- left untouched
    replace: list             # a different amiibo has that place and the job may replace it
    aborted: bool = False

    def note(self, total: int) -> Optional[str]:
        """One sentence for History when not every amiibo was simply added."""
        if not (self.already or self.conflicts or self.replace):
            return None
        parts = [f"{total - len(self.already) - len(self.conflicts)} of {total} amiibo added"]
        if self.already:
            parts.append(f"{len(self.already)} already on the console, left as they are")
        if self.replace:
            parts.append(f"{len(self.replace)} replaced a different amiibo in the same folder")
        if self.conflicts:
            names = ", ".join(u.split("/", 2)[-1] for u in self.conflicts[:3])
            more = f" and {len(self.conflicts) - 3} more" if len(self.conflicts) > 3 else ""
            parts.append(f"{len(self.conflicts)} not copied because a different amiibo already uses "
                         f"that folder ({names}{more})")
        return "; ".join(parts)


def _read_amiibo_json(path) -> Optional["emuiibo.AmiiboInfo"]:
    try:
        return emuiibo.parse_amiibo_json(path.read_bytes())
    except OSError:
        return None


def _plan_amiibo(
    conn: sqlite3.Connection, backend: MtpBackend, job_row: sqlite3.Row, manifest, *,
    delivered: set, replaceable: set, should_abort: ShouldAbort,
) -> AmiiboPlan:
    """An AMIIBO job decides per AMIIBO, never per file. A virtual amiibo is
    its folder, and emuiibo rewrites that folder as soon as the amiibo is
    used (areas.json, a mii, a UUID, save data under areas/), so the same
    amiibo installed yesterday no longer matches its source byte for byte --
    the file-level "already there, not ours, stop the job" rule would fail
    a 766-amiibo collection on its first amiibo. Instead, for every amiibo
    not already delivered by this job (or the attempt it retries):

      - its folder is free: sent as usual;
      - it holds the SAME amiibo (figure and UUID, read back off the
        console): already there, left exactly as it is -- its save data too;
      - it holds a DIFFERENT amiibo: not touched under Settings' default
        "skip" policy; under "override" (or an Override job) that folder is
        removed and the new amiibo written in its place.

    One listing per destination folder, one small read per amiibo that is
    already there -- nothing is read for a folder that does not exist."""
    storage = job_row["target_storage"]
    force = bool(job_row["force_overwrite"])
    job_id = job_row["id"]
    by_dest = {f.dest_relative_path: f for f in manifest.files}
    plan = AmiiboPlan(skip=set(), already=[], conflicts=[], replace=[])
    listings: dict = {}

    def names_in(folder: str):
        if folder not in listings:
            entries = backend.list_directory(storage, folder)
            listings[folder] = None if entries is None else {e.name.lower() for e in entries}
        return listings[folder]

    for index, (unit, dests) in enumerate(emuiibo.manifest_units(by_dest).items()):
        if should_abort():
            plan.aborted = True
            return plan
        if index % 25 == 0:
            db.update_job_status(conn, job_id, "RUNNING", last_progress_at=db.now_iso())
        if any(d in delivered or d in replaceable for d in dests):
            continue  # this job's own amiibo, part way through: carried on as usual
        parent, _, name = unit.rpartition("/")
        present = names_in(parent)
        if present is None or name.lower() not in present:
            continue
        if unit in by_dest:
            # A raw dump: the same bytes already there is the ordinary
            # already-present case the file loop handles on its own.
            data = backend.read_file(storage, unit, max_bytes=by_dest[unit].size)
            if data is not None and hashlib.sha256(data).hexdigest() == by_dest[unit].sha256:
                continue
        else:
            # Its own amiibo.json -- not the v2/amiibo.json a converted amiibo
            # keeps inside itself.
            own_json = f"{unit}/{emuiibo.AMIIBO_JSON}".lower()
            json_dest = next((d for d in dests if d.lower() == own_json), None)
            local = remote = None
            if json_dest is not None:
                local = _read_amiibo_json(manifest_mod.resolve_source_path(
                    by_dest[json_dest], job_id, batch_id=manifest.batch_id))
                data = backend.read_file(storage, f"{unit}/{emuiibo.AMIIBO_JSON}",
                                         max_bytes=emuiibo.AMIIBO_JSON_MAX_BYTES)
                remote = emuiibo.parse_amiibo_json(data) if data else None
            if local is not None and remote is not None and local.same_amiibo(remote):
                plan.already.append(unit)
                plan.skip.update(dests)
                continue
        if force:
            plan.replace.append(unit)
        else:
            plan.conflicts.append(unit)
            plan.skip.update(dests)
    return plan


def _replace_amiibo_folders(backend: MtpBackend, storage: str, units: list, conn, job_id: int) -> None:
    """Removes what a replaced amiibo's folder held -- files first, then the
    folders, one object at a time -- so nothing of the old amiibo (its save
    data least of all) ends up mixed into the new one."""
    for unit in units:
        if not emuiibo.is_inside_amiibo_dir(unit):
            continue
        entries = backend.list_directory(storage, unit)
        if entries is None:
            backend.delete(storage, unit)  # a file (raw dump) -- overwritten anyway
            continue
        for path in emuiibo.removal_order(backend, storage, unit):
            backend.delete(storage, path)
        db.log_job_event(conn, job_id, f"removed '{unit}' to replace it with a different amiibo")


def _aborted_between_files_error(done_count: int, total_count: int) -> str:
    return (
        f"stopped by Abort after {done_count} of {total_count} file(s) -- nothing was left half-written; "
        "Retry continues from here"
    )


def _aborted_mid_file_error(dest_relative_path: str, storage: str) -> str:
    if storage == STORAGE_SD_INSTALL:
        return (
            f"stopped by Abort while sending '{dest_relative_path}' -- DBI received only part of it, "
            "so this install did not complete on the console; Retry sends it again from the start"
        )
    return (
        f"stopped by Abort while sending '{dest_relative_path}' -- that file was not finished; "
        "Retry replaces it and continues from there"
    )


def _run_job_transfer(
    conn: sqlite3.Connection, backend: MtpBackend, job_row: sqlite3.Row, *,
    should_abort: ShouldAbort = _never_abort,
) -> JobRunOutcome:
    """Abort (should_abort) takes effect at exactly three kinds of point,
    each one where the job can stop without guessing what reached the
    device:

      - before the job goes RUNNING at all: nothing was sent, so it is
        cancelled the same way a queued job is (FAILED, abandoned, no
        History row);
      - between two files: INTERRUPTED, every earlier file recorded as
        delivered, nothing half-written -- Retry continues from there;
      - mid-file, from inside the backend's own progress callback, which
        raises TransferAborted: INTERRUPTED, and the file in flight is left
        unfinished (jobs.current_file still names it, which is what lets a
        Retry replace it -- see manifest.inherit_progress).

    Mid-file only works on a backend that calls `progress` at all: the WPD
    transport does, about once a second while bytes move; the Shell
    fallback cannot, so there the job stops after the file being copied has
    finished. Nor is the device's own finalising wait interrupted (the
    commit after the last byte, up to ~80s for a big .nsz on DBI): the
    callback's last call reports every byte, and aborting there would throw
    away a file the console already has in full."""
    job_id = job_row["id"]
    storage = job_row["target_storage"]
    manifest = manifest_mod.load_manifest(job_id)

    # TOCTOU guard (stage 4.1): verify EVERY manifest file still matches its
    # live source BEFORE touching the device at all -- not interleaved with
    # sending, so a source that drifted only partway through isn't left in
    # a confusing in-between state on the device.
    try:
        mismatch = manifest_mod.verify_manifest_against_source(manifest, job_id)
    except manifest_mod.ManifestError as exc:
        # FAULT-001: a manifest declaring an invalid/escaping relative path
        # or an unrecognized source_kind is corrupted data, not a
        # legitimate content-drift case -- fail outright, with a precise
        # reason, rather than let it fall through to the generic
        # unexpected-exception safety net (which would still catch this
        # safely as INTERRUPTED, but "we never even resolved a source path"
        # is a FAILED, not an ambiguous-mid-flight, situation).
        error = f"manifest is invalid: {exc}"
        db.update_job_status(conn, job_id, "FAILED", error=error, finished_at=db.now_iso())
        db.log_job_event(conn, job_id, error)
        return JobRunOutcome(job_id=job_id, status="FAILED", error=error)
    if mismatch is not None:
        if mismatch.kind == "missing":
            error = f"source no longer exists: {mismatch.dest_relative_path}"
            db.update_job_status(conn, job_id, "FAILED", error=error, finished_at=db.now_iso())
            db.log_job_event(conn, job_id, error)
            return JobRunOutcome(job_id=job_id, status="FAILED", error=error)

        error = f"source changed since confirmation: {mismatch.dest_relative_path}"
        db.update_job_status(conn, job_id, "SOURCE_CHANGED", error=error)
        db.log_job_event(conn, job_id, error + " -- create a new job from a fresh preview/confirmation")
        return JobRunOutcome(job_id=job_id, status="SOURCE_CHANGED", error=error)

    if should_abort():
        # Checking the sources can take a while (a changed mtime means
        # re-hashing); an Abort pressed meanwhile still finds this job not
        # yet started. Idempotent with the cancel services.abort_all()
        # already applied to it while it was CONFIRMED.
        db.update_job_status(conn, job_id, "FAILED", error=ABORTED_BEFORE_START_ERROR, finished_at=db.now_iso())
        conn.execute("UPDATE jobs SET abandoned=1 WHERE id=?", (job_id,))
        conn.commit()
        db.log_job_event(conn, job_id, "cancelled by Abort before it started running")
        return JobRunOutcome(job_id=job_id, status="FAILED", error=ABORTED_BEFORE_START_ERROR, record_history=False)

    delivered = manifest_mod.load_progress(job_id)
    # Files a previous attempt of this same transfer left half written (see
    # manifest.inherit_progress) -- the only ones replaced without asking.
    replaceable = manifest_mod.load_replaceable(job_id)
    bytes_done = sum(f.size for f in manifest.files if f.dest_relative_path in delivered)
    # UI-006 follow-up: bytes_total/bytes_done were previously only ever
    # written at a TERMINAL status (see the DESTINATION_CONFLICT/FAILED/
    # DONE writes below and in _process_job's history recording) -- during
    # the whole RUNNING phase the jobs row kept showing bytes_total=NULL,
    # bytes_done=0 no matter how much had actually transferred. queue.js's
    # own progress-bar fraction already reads these two columns for
    # SD_CARD jobs; the frontend was ready, the backend just never fed it
    # real numbers, so a real, multi-minute, multi-file transfer looked
    # like a bare spinner with nothing else changing on screen.
    now = db.now_iso()
    db.update_job_status(
        conn, job_id, "RUNNING", started_at=now, last_progress_at=now, increment_attempt=True,
        bytes_done=bytes_done, bytes_total=sum(f.size for f in manifest.files),
    )
    db.log_job_event(conn, job_id, f"running on device '{job_row['target_device_id']}'")

    # Set if ANY file in THIS run completes with TransferStatus.UNVERIFIED
    # (see switchagent/mtp/windows.py -- reached for install-like storages
    # such as SD_INSTALL, where the backend can prove transport was
    # accepted but not prove DBI's own install completion). Downgrades the
    # job's final terminal status from DONE to DONE_UNVERIFIED below the
    # loop. A resumed job can never carry hidden UNVERIFIED history from an
    # earlier run: reaching UNVERIFIED for any file always ends that run's
    # job in the terminal DONE_UNVERIFIED status (see below), and
    # DONE_UNVERIFIED is deliberately not in retry_job()'s retryable set
    # (db.py) -- so _process_job() can never be re-entered for a job that
    # already delivered a file as UNVERIFIED in a prior run.
    any_unverified = False
    # Files found already on the card with exactly these bytes (read back
    # and hashed by the backend -- see send_file's expected_sha256): done,
    # without sending them again. Counted for one log line at the end rather
    # than one per file: a re-run over a 4,625-file folder would otherwise
    # write 4,625 of them.
    already_present = 0

    # AMIIBO: decided amiibo by amiibo first (see _plan_amiibo); EMUIIBO:
    # emuiibo's own program files are replaced whatever is there -- they
    # belong to emuiibo, not to anybody's data, and installing a release
    # over another one is exactly what updating emuiibo means.
    # ADDON: the same for an Add-ons utility's own files -- except the ones a
    # person edits (manifest.keep: a config.ini), which are written only
    # where missing and never replaced.
    amiibo_plan: Optional[AmiiboPlan] = None
    always_overwrite = manifest.content_type in (ContentType.EMUIIBO.value, ContentType.ADDON.value)
    kept_paths = {k.lower() for k in manifest.keep}
    kept = 0
    if manifest.content_type == ContentType.AMIIBO.value:
        try:
            amiibo_plan = _plan_amiibo(conn, backend, job_row, manifest, delivered=delivered,
                                       replaceable=replaceable, should_abort=should_abort)
            if not amiibo_plan.aborted:
                _replace_amiibo_folders(backend, storage, amiibo_plan.replace, conn, job_id)
        except MtpError as exc:
            error = f"could not check what is already on the console: {exc}"
            db.update_job_status(conn, job_id, "FAILED", bytes_done=bytes_done, error=error, finished_at=db.now_iso())
            db.log_job_event(conn, job_id, f"failed: {error}")
            return JobRunOutcome(job_id=job_id, status="FAILED", error=error)
        if amiibo_plan.aborted:
            error = _aborted_between_files_error(len(delivered), len(manifest.files))
            db.update_job_status(conn, job_id, "INTERRUPTED", bytes_done=bytes_done, error=error)
            db.log_job_event(conn, job_id, f"aborted by user while checking the console -> INTERRUPTED ({error})")
            return JobRunOutcome(job_id=job_id, status="INTERRUPTED", error=error)
        if amiibo_plan.already or amiibo_plan.conflicts or amiibo_plan.replace:
            db.log_job_event(
                conn, job_id,
                f"amiibo on the console: {len(amiibo_plan.already)} already there, "
                f"{len(amiibo_plan.conflicts)} held by a different amiibo (skipped), "
                f"{len(amiibo_plan.replace)} replaced",
            )
    skipped = amiibo_plan.skip if amiibo_plan else set()
    replace_units = tuple(u + "/" for u in amiibo_plan.replace) if amiibo_plan else ()

    for file in manifest.files:
        if file.dest_relative_path in delivered or file.dest_relative_path in skipped:
            continue

        if should_abort():
            done_count = len(manifest_mod.load_progress(job_id))
            error = _aborted_between_files_error(done_count, len(manifest.files))
            db.update_job_status(conn, job_id, "INTERRUPTED", bytes_done=bytes_done, error=error)
            db.log_job_event(conn, job_id, f"aborted by user between files -> INTERRUPTED ({error})")
            return JobRunOutcome(job_id=job_id, status="INTERRUPTED", error=error)

        # UI-006 (stall detection): honest per-file "still actively
        # working" touch-point, in addition to the post-completion one
        # below -- for a single large file whose one send_file() call
        # itself takes a long time (no way to see progress DURING it,
        # this architecture has no such signal, see this function's own
        # docstring), this is what keeps last_progress_at reflecting "just
        # started sending this file" rather than staying frozen at
        # whenever the job first went RUNNING.
        db.update_job_status(
            conn, job_id, "RUNNING", last_progress_at=db.now_iso(), current_file=file.dest_relative_path,
        )

        parent = "/".join(file.dest_relative_path.split("/")[:-1])
        source_path = manifest_mod.resolve_source_path(file, job_id, batch_id=manifest.batch_id)

        try:
            backend.ensure_directory(storage, parent)

            # Live byte counter for THIS file, on top of whatever earlier
            # files of the same job already delivered. The WPD transport
            # (switchagent/mtp/wpd.py) calls this about once a second while
            # the bytes are actually moving; the Shell fallback cannot report
            # anything mid-copy and simply never calls it, which is exactly
            # the old behaviour. Runs on this same worker thread, inside
            # send_file, so it shares this function's own DB connection
            # safely.
            def report_progress(sent: int, total: int, _already: int = bytes_done) -> None:
                db.update_job_status(
                    conn, job_id, "RUNNING", last_progress_at=db.now_iso(), bytes_done=_already + sent,
                )
                # Never on the last call (sent == total): every byte is
                # already across and only the device's commit is left --
                # see this function's docstring.
                if sent < total and should_abort():
                    raise TransferAborted("aborted by user mid-transfer")

            # W3-006 Override: force_overwrite is only ever true on a job
            # created by services.override_job(), the user's explicit
            # "Override" click on a DESTINATION_CONFLICT card -- for that
            # one job, and that job only, send_file's own overwrite=True
            # replaces whatever is there, no existence check at all. Every
            # other job still refuses blindly -- via send_file's OWN
            # existence check (it already does the identical any(i.Name ==
            # ... for i in parent.GetFolder.Items()) scan internally before
            # copying) rather than a separate backend.exists() call first --
            # that used to re-navigate and re-scan the same destination
            # directory a second time for every single file, pure wasted
            # MTP round-trips this loop doesn't need.
            result = backend.send_file(
                storage, file.dest_relative_path, source_path,
                overwrite=(bool(job_row["force_overwrite"])
                           or (always_overwrite and file.dest_relative_path.lower() not in kept_paths)
                           or file.dest_relative_path in replaceable
                           or file.dest_relative_path.startswith(replace_units)),
                progress=report_progress,
                # Only a real filesystem can be read back meaningfully -- DBI's
                # install node is not one (see mtp/windows.py).
                expected_sha256=file.sha256 if storage == STORAGE_SD_CARD else None,
            )
        except TransferAborted:
            error = _aborted_mid_file_error(file.dest_relative_path, storage)
            db.update_job_status(conn, job_id, "INTERRUPTED", bytes_done=bytes_done, error=error)
            db.log_job_event(conn, job_id, f"aborted by user mid-file -> INTERRUPTED ({error})")
            return JobRunOutcome(job_id=job_id, status="INTERRUPTED", error=error)
        except FileAlreadyExistsError:
            if file.dest_relative_path.lower() in kept_paths:
                # A person's own settings file: already there, so it stays.
                kept += 1
                db.log_job_event(conn, job_id, f"kept as it is on the console: {file.dest_relative_path}")
                continue
            # Exists on the device, but WE have no record (progress.json) of
            # having put it there ourselves during this job -- cannot prove
            # it's the same content, so refuse rather than guess. This is
            # idempotent-but-not-blind: a file THIS job already delivered
            # was already skipped above via `delivered`; anything else
            # present is genuinely unresolved.
            #
            # Settings' conflict policy decides what happens next, not a
            # person: "override" jobs already sent overwrite=True above and
            # never reach this branch at all, so getting here only ever
            # means the policy in effect was "skip" -- resolved immediately,
            # the same way a manual Skip click always worked (abandoned=1),
            # with no one needing to look at it.
            error = (
                f"'{file.dest_relative_path}' already exists on '{storage}' "
                "and was not sent by this job -- skipped automatically "
                "(Settings: existing files on the Switch are skipped)"
            )
            db.update_job_status(
                conn, job_id, "DESTINATION_CONFLICT", bytes_done=bytes_done, error=error, finished_at=db.now_iso(),
            )
            conn.execute("UPDATE jobs SET abandoned=1 WHERE id=?", (job_id,))
            conn.commit()
            db.log_job_event(conn, job_id, error)
            return JobRunOutcome(job_id=job_id, status="DESTINATION_CONFLICT", error=error)
        except DeviceDisconnectedError as exc:
            return _wait_for_reconnect(
                conn, job_id, bytes_done, str(exc), in_flight=file.dest_relative_path, storage=storage,
            )
        except MtpError as exc:
            db.update_job_status(
                conn, job_id, "FAILED", bytes_done=bytes_done, error=str(exc), finished_at=db.now_iso(),
            )
            db.log_job_event(conn, job_id, f"failed: {exc}")
            return JobRunOutcome(job_id=job_id, status="FAILED", error=str(exc))

        if result.status is TransferStatus.COMPLETED:
            manifest_mod.mark_delivered(job_id, file.dest_relative_path)
            if result.already_present:
                already_present += 1
                bytes_done += file.size
            else:
                bytes_done += result.bytes_sent
            db.update_job_status(conn, job_id, "RUNNING", last_progress_at=db.now_iso(), bytes_done=bytes_done)
            continue

        if result.status is TransferStatus.UNVERIFIED:
            # Transport accepted, DBI-side result unprovable (see
            # switchagent/mtp/windows.py). Recorded delivered so a
            # (currently unreachable, see `any_unverified` above)
            # re-entry would never redundantly resend this file --
            # resending a real, already-accepted install is worse than
            # leaving it alone. bytes_done advances by the manifest's own
            # known size, not result.bytes_sent: for an install-like node
            # that figure is diagnostic-only and can legitimately read 0
            # even on a real, physically confirmed success (see
            # docs/STAGE5B-REAL-MTP.md) -- using it here would make a
            # correctly-accepted transfer look like it sent nothing.
            manifest_mod.mark_delivered(job_id, file.dest_relative_path)
            bytes_done += file.size
            any_unverified = True
            db.update_job_status(conn, job_id, "RUNNING", last_progress_at=db.now_iso(), bytes_done=bytes_done)
            db.log_job_event(conn, job_id, f"'{file.dest_relative_path}': {result.error or 'unverified'}")
            continue

        if result.status is TransferStatus.DEVICE_DISCONNECTED:
            return _wait_for_reconnect(
                conn, job_id, bytes_done, result.error or "The Switch disconnected mid-transfer",
                in_flight=file.dest_relative_path, storage=storage,
            )

        error = result.error or f"transfer of '{file.dest_relative_path}' did not complete ({result.status.value})"
        db.update_job_status(conn, job_id, "FAILED", bytes_done=bytes_done, error=error, finished_at=db.now_iso())
        db.log_job_event(conn, job_id, f"failed: {error}")
        return JobRunOutcome(job_id=job_id, status="FAILED", error=error)

    if any_unverified:
        db.update_job_status(conn, job_id, "DONE_UNVERIFIED", bytes_done=bytes_done, finished_at=db.now_iso())
        db.log_job_event(
            conn, job_id,
            f"done_unverified, {len(manifest.files)} file(s) -- transport accepted, "
            "installation result not confirmed via MTP",
        )
        return JobRunOutcome(job_id=job_id, status="DONE_UNVERIFIED")

    if already_present:
        db.log_job_event(
            conn, job_id,
            f"{already_present} file(s) were already on the device with exactly these contents -- not sent again",
        )
    note = None
    if amiibo_plan is not None:
        note = amiibo_plan.note(len(emuiibo.manifest_units(f.dest_relative_path for f in manifest.files)))
    elif kept:
        note = f"your own settings kept ({kept} file{'s' if kept != 1 else ''} already on the console)"
    db.update_job_status(conn, job_id, "DONE", bytes_done=bytes_done, finished_at=db.now_iso(), error=note)
    db.log_job_event(conn, job_id, f"done, {len(manifest.files) - len(skipped)} file(s)"
                     + (f" -- {note}" if note else ""))
    return JobRunOutcome(job_id=job_id, status="DONE", error=note)


def run_worker_loop(
    conn: sqlite3.Connection, registry: DeviceRegistry, *, poll_interval_seconds: float = 5.0,
) -> None:
    """Manual/CLI convenience: repeatedly calls run_worker_once(), sleeping
    between empty passes. Not itself tested (mirrors cli.py's _run_watch,
    which is the same kind of thin infinite-loop wrapper around a
    fully-tested single-pass function) -- run_worker_once is where the
    actual logic and its test coverage live."""
    import time

    from .mtp.windows import device_fingerprint

    fingerprints = [device_fingerprint(d) for d in registry.known_device_ids()]
    print(f"Queue worker started. Known devices: {fingerprints} (Ctrl+C to stop)")
    while True:
        outcome = run_worker_once(conn, registry)
        if outcome is not None:
            print(f"job {outcome.job_id}: {outcome.status}" + (f" ({outcome.error})" if outcome.error else ""))
        else:
            time.sleep(poll_interval_seconds)
