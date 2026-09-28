"""Small preparation queue, independent of the single MTP worker.

Only job manifests/payload references need to survive restart. An interrupted
preparation is not automatically re-extracted or confirmed on startup.
"""
from __future__ import annotations

import copy
import shutil
import threading
import time
import uuid
from pathlib import Path

from .. import config, db, extractor
from .. import title_id as title_id_mod
from ..model import ContentType

RESERVE_BYTES = 512 * 1024 * 1024

# Queue badge vocabulary -- deliberately the same four role names
# web/services.py's _job_variant_role() and library.js's CONFIRM_ROLE_LABELS
# use, so one item keeps the same badge as it moves from "waiting to be
# prepared" (here, no manifest yet) to a real job (there, read off the frozen
# manifest).
_TITLE_VARIANT_TO_ROLE = {"BASE": "base", "UPDATE": "update", "DLC": "dlc"}


# An item the user took out of the queue (PreparationQueue.remove_items()).
REMOVED_PHASE = 'Removed'
# Phases an item can still be removed in. "Installing" means its jobs are
# with the worker: services.remove_from_queue() cancels those first and
# refuses outright if one is already sending.
_REMOVABLE_PHASES = ('Waiting', 'Analyzing', 'Extracting', 'Preparing', 'Prepared', 'Installing')


def _library_item_role(row):
    """'base'/'update'/'dlc'/'mod' for a library item that has no job -- and
    therefore no frozen manifest -- yet. None when it cannot be told (no
    title_id, or one that will not parse, e.g. an archive whose contents are
    only known after extraction): the row then shows no badge at all, which
    is the honest answer rather than a guessed one."""
    if row is None:
        return None
    if row["content_type"] == ContentType.SD_FILES.value:
        return "sd"
    if row["content_type"] == ContentType.AMIIBO.value:
        return "amiibo"
    if row["content_type"] == ContentType.EMUIIBO.value:
        return "emuiibo"
    if row["content_type"] == ContentType.ADDON.value:
        return "addon"
    if row["item_type"] == "MOD_FOLDER" or row["content_type"] == ContentType.ATMOSPHERE_MOD.value:
        return "mod"
    if not row["title_id"]:
        return None
    try:
        variant, _base_id = title_id_mod.classify_title_variant(row["title_id"])
    except (ValueError, TypeError):
        return None
    return _TITLE_VARIANT_TO_ROLE.get(variant)

# How long a resolved (Overridden-and-now-DONE, or Skipped) item stays
# visible in a "Failed" batch's panel before PreparationQueue.snapshot()
# stops including it -- same grace period submit() already gives a fully
# successful batch (see its own comment), applied per-item here instead of
# to the whole batch, since sibling "Not started" items in the same batch
# may still need the user's own attention indefinitely.
RESOLVED_ITEM_GRACE_SECONDS = 10.0


def _resolved_long_enough(row) -> bool:
    if not (row['abandoned'] or row['status'] in ('DONE', 'DONE_UNVERIFIED')):
        return False
    finished_at = row['finished_at']
    if not finished_at:
        return False
    from datetime import datetime, timezone
    elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(finished_at)).total_seconds()
    return elapsed >= RESOLVED_ITEM_GRACE_SECONDS


def remove_extraction(path):
    if path is None:
        return
    path = Path(path)
    root = config.WORK_DIR.resolve()
    # safe_extract creates a random directory immediately under work.
    # Never accept the work root itself, a link, or a Library directory.
    if path.is_symlink() or path.resolve().parent != root:
        raise ValueError("Extraction directory is outside the controlled work root")
    if path.exists():
        shutil.rmtree(path)


_VARIANT_RANK = {'SD_FILES': -1, 'BASE': 0, 'UPDATE': 1, 'DLC': 1, 'MOD': 2}


def _variant_rank(row) -> int:
    """Where an item belongs within its own chain -- Base first, then
    Update/DLC, then Mods -- regardless of what order the user happened
    to select/submit them in. The Library page's selection order is
    click order, not install order (Array.from(selected.keys()) in
    library.js), so this must never be assumed to already be correct;
    _group_by_title() below always re-sorts by this before returning."""
    if row is None:
        return 1
    from ..model import ContentType
    from .. import sd_files
    if row['content_type'] == ContentType.SD_FILES.value and row['title_id_source'] != sd_files.RELEASE_PART:
        # A port's switch/ folder goes BEFORE its forwarder (by explicit
        # request, 2026-09-28): the forwarder is what puts the icon on the
        # home menu, so it comes last -- the icon appears once what it
        # launches is all on the card, and never if that did not arrive
        # (a stopped item stops the rest of its chain). A companion app that
        # merely came with the release ("release") is not needed by the
        # game and keeps its place after it.
        return _VARIANT_RANK['SD_FILES']
    if row['content_type'] in (ContentType.ATMOSPHERE_MOD.value, ContentType.SD_FILES.value,
                               ContentType.AMIIBO.value):
        # A game's switch/ folder and its amiibo go after the game itself,
        # like a mod.
        return _VARIANT_RANK['MOD']
    from .. import title_id as title_id_mod
    try:
        variant = title_id_mod.classify_title_variant(row['title_id']).variant
    except (ValueError, TypeError, AttributeError):
        return 1
    return _VARIANT_RANK.get(variant, 1)


def _port_chain_key(row):
    """A port with a sealed forwarder has no TITLE_ID to chain by; its
    forwarder and its switch/ files share a release folder instead (see
    sd_files.SEALED_FORWARDER), and that is what keeps them one chain."""
    from .. import sd_files
    if row is None or row['title_id_source'] not in (sd_files.SEALED_FORWARDER, sd_files.PORT_PART):
        return None
    return f"port-{Path(row['absolute_path']).parent}"


def _group_by_title(conn, item_ids):
    """Groups item_ids into per-title "chains". Items sharing a
    base_title_id (base/update/DLC/mods for the same game -- see
    services.family_base_title_id) form one chain and are attempted
    strictly Base, then Update/DLC, then Mods (see _variant_rank) --
    NEVER just the order they happened to be submitted in -- a later
    item in the SAME chain is never even attempted while an earlier one
    hasn't resolved: by explicit request, an Update must never install
    ahead of its own Base. Ties (e.g. two mods) keep their original
    relative submission order (stable sort). An item with no
    determinable title_id gets its own singleton chain, fully
    independent of everything else. Chains themselves are independent of
    each other: one game stuck waiting for a decision never blocks a
    different game in the same batch (see _install_sequentially)."""
    from .services import family_base_title_id
    chains: dict = {}
    rows_by_id = {}
    for item_id in item_ids:
        row = db.get_library_item_by_id(conn, item_id)
        rows_by_id[item_id] = row
        family = family_base_title_id(row['title_id']) if row else None
        port = _port_chain_key(row)
        key = family if family is not None else port if port is not None else f'item-{item_id}'
        chains.setdefault(key, []).append(item_id)
    for key, ids in chains.items():
        ids.sort(key=lambda item_id: _variant_rank(rows_by_id[item_id]))
    return chains


def preflight(conn, item_ids, progress=None):
    errors = []
    required = 0
    config.WORK_DIR.mkdir(parents=True, exist_ok=True)
    for item_id in item_ids:
        row = db.get_library_item_by_id(conn, item_id)
        try:
            if row is None:
                raise ValueError("library item not found")
            if row["status"] != "AVAILABLE":
                raise ValueError(f"not installable in its current state: {row['status']}")
            path = Path(row["absolute_path"])
            if progress:
                progress(item_id=item_id, phase="Analyzing", name=path.name)
            if not path.exists():
                raise ValueError("source file is missing")
            if path.suffix.lower() in (".zip", ".7z", ".rar"):
                entries = extractor.list_archive_entries(path)
                required += sum(max(0, entry.size) for entry in entries if not entry.is_dir)
            if progress:
                progress(item_id=item_id, phase="Waiting")
        except (OSError, ValueError, extractor.ArchiveError) as exc:
            errors.append({"library_item_id": item_id, "error": str(exc)})
            if progress:
                progress(item_id=item_id, phase="Failed", error=str(exc))
    free = shutil.disk_usage(config.WORK_DIR).free
    if progress:
        progress(workspace=str(config.WORK_DIR.resolve()), free_bytes=free,
                 required_bytes=required, reserve_bytes=RESERVE_BYTES)
    if required + RESERVE_BYTES > free:
        errors.append({"library_item_id": None, "error":
                       f"Insufficient workspace space: need {required + RESERVE_BYTES} bytes, free {free}"})
    return errors


class PreparationQueue:
    def __init__(self, db_path):
        self.db_path = db_path
        self.lock = threading.Lock()
        self.serial = threading.Lock()
        self.states = {}
        self.stop = threading.Event()
        # task_id -> why its run was stopped from outside (abort_all(),
        # clear_for_device()) -- what that run then reports as its reason.
        self._stopped_reasons = {}

    def submit(self, item_ids, target, *, amiibo_selection=None):
        task_id = uuid.uuid4().hex
        item_ids = list(dict.fromkeys(item_ids))
        from .. import queue_worker
        from .services import cover_id_for_title
        items = {}
        with db.open_db(self.db_path) as conn:
            chains = _group_by_title(conn, item_ids)
            # Queue lists a run's items in the order they will INSTALL, not
            # the order they were ticked in: a port's forwarder was ticked
            # first but installs last, and listed first it sat at the top
            # reading "Ready to install" while its files copied below it --
            # which looked like the first item being skipped (2026-09-28).
            install_order = {member: n for n, member in enumerate(
                member for members in chains.values() for member in members)}
            for item_id in item_ids:
                position = install_order[item_id]
                row = db.get_library_item_by_id(conn, item_id)
                # The SAME resolver display_name_for_job() uses once this
                # item becomes a real job (full filename, extension and
                # bracket tags included) -- not services._resolve_entry_name
                # (the Library page's own .stem-based, extension-dropping
                # convention), so a queued item's name never changes the
                # moment it's confirmed into a job.
                name = queue_worker.resolve_library_item_display_name(conn, row) if row else 'Missing library file'
                # No ' — Mod' suffix any more: the row now carries a [Mod]
                # badge, and saying it twice on one line reads as a bug.
                items[str(item_id)] = {'name': name, 'phase': 'Waiting', 'order': position,
                                       'job_ids': [], 'role': _library_item_role(row),
                                       'cover_id': cover_id_for_title(row['title_id']) if row else None}
        with self.lock:
            # A finished ("Ready") preparation is meant to clear itself the
            # instant it completes (see the `run()` closure below) -- this
            # is a backstop for any that didn't (e.g. the process was
            # killed mid-run) plus every "Failed" one, which stays visible
            # until the user starts something new rather than vanishing on
            # its own (it's explaining why sibling items never started).
            # Unconditional now: this used to only fire once 50+ states had
            # piled up, so in normal use ("install a batch, wait, install
            # another") it never ran at all -- completed batches just sat
            # in the panel forever, looking like nothing ever finished.
            for key in list(self.states):
                if self.states[key]["phase"] in ("Ready", "Failed"):
                    del self.states[key]
            self.states[task_id] = {"id": task_id, "phase": "Waiting", "started": time.time(),
                                    "items": items, "target": target, "chains": chains,
                                    "amiibo_selection": dict(amiibo_selection or {})}

        def update(item_id=None, **fields):
            with self.lock:
                # None once clear_for_device() has removed this run's own
                # state out from under it (device connection changed
                # mid-run, see that method's docstring) -- a silent no-op,
                # never a KeyError, since the background thread calling
                # this has no other way to learn that's happened except
                # by checking self.stop-style at its own next loop point
                # (see _install_sequentially's own check).
                state = self.states.get(task_id)
                if state is None:
                    return
                if item_id is not None:
                    fields.pop('name', None)  # Keep the resolved family name, especially for mods.
                    item = state["items"][str(item_id)]
                    # Removed from the queue (remove_items()): whatever the
                    # run is still doing with it, nothing brings it back.
                    if item.get('phase') == REMOVED_PHASE:
                        return
                    item.update(fields)
                else:
                    state.update(fields)

        def clear_if_still_ready():
            # By explicit request: Queue must not keep showing a batch once
            # it's fully installed -- a short grace period (not instant)
            # so a poll landing right after completion still gets to render
            # "N / N finished" at least once, rather than the row just
            # disappearing out from under a mid-render page. Re-checks the
            # phase is still "Ready" (not overwritten by a newer submit())
            # before deleting, so this can never remove the wrong state.
            with self.lock:
                state = self.states.get(task_id)
                if state is not None and state.get("phase") == "Ready":
                    del self.states[task_id]

        def run():
            with self.serial:
                update(phase="Preparing")
                try:
                    with db.open_db(self.db_path) as conn:
                        result = self._install_sequentially(conn, item_ids, target, update, task_id, chains)
                    # 'blocked': items whose own chain hit a job that
                    # needs the user's decision (see _install_sequentially)
                    # -- those items are NOT failures in the "something
                    # broke" sense, but the batch still isn't fully done,
                    # so it must not report "Ready" (and must not
                    # auto-clear itself below) while any remain.
                    still_needs_attention = bool(result["errors"] or result["blocked"])
                    update(phase="Failed" if still_needs_attention else "Ready", result=result, finished=time.time())
                    if not still_needs_attention:
                        timer = threading.Timer(10.0, clear_if_still_ready)
                        timer.daemon = True  # trivial in-memory cleanup -- never delay app shutdown
                        timer.start()
                except Exception as exc:
                    update(phase="Failed", error=str(exc), finished=time.time())
                finally:
                    with self.lock:
                        self._stopped_reasons.pop(task_id, None)

        # A preparation must finish writing manifests before normal process exit.
        threading.Thread(target=run, name="switchagent-preparation", daemon=False).start()
        return task_id

    def _install_sequentially(self, conn, item_ids, target, update, task_id, chains):
        """Processes `chains` (see _group_by_title) -- one game's items at
        a time, strictly in order within each chain, but chains
        themselves are independent: a job that needs the user's decision
        (DESTINATION_CONFLICT and the same set below) stops ONLY its own
        chain and moves straight on to the next, different game, rather
        than the entire batch dying on one conflict the way it used to.
        Deliberately does NOT poll/wait for that decision to actually
        happen -- self.serial (held for this whole call, see submit())
        would otherwise stay locked for as long as the user takes to
        click Override, blocking every other preparation in the
        meantime. Once the user DOES resolve it (via the ordinary
        Override/Retry/Skip job actions, a completely separate code
        path), PreparationQueue.continue_chain() is what picks the rest
        of that chain back up, as a brand new run.

        Preparing one item AHEAD (2026-09-18): extracting an archive,
        staging its payload and hashing it are disk/CPU work that has
        nothing to do with the USB cable, yet this loop used to do all of
        it only after the previous item had finished transferring -- so a
        multi-game batch spent the console's idle time doing nothing and
        the PC's idle time doing nothing, alternately. The next item is now
        prepared on a background thread while the current one transfers.

        What that deliberately does NOT change: a prepared-ahead item's
        jobs are created PENDING_CONFIRM and are confirmed -- i.e. become
        visible to the worker at all -- only when its turn actually
        arrives, in the same order as before. So "an Update never installs
        ahead of its own Base" still holds by construction, and an item
        whose chain turns out to be blocked is abandoned with its staged
        payload released (_abandon_prepared below), never quietly
        installed because it happened to be ready early."""
        from .services import create_and_confirm_jobs
        from ..work_cleanup import cleanup_batch_if_all_done
        from ..manifest import batch_work_dir
        import json
        # One bounded-history log per Install click; never one per archive/job.
        log_dir = config.LOGS_DIR / 'installs'
        log_dir.mkdir(parents=True, exist_ok=True)
        logs = sorted(log_dir.glob('install-*.log'), key=lambda p: p.stat().st_mtime)
        for old in logs[:-9]:
            if old.is_file() and not old.is_symlink():
                old.unlink()
        result = {'created': [], 'errors': [], 'batch_id': None, 'blocked': []}
        item_id = None
        order = [(chain_key, member_id) for chain_key, ids in chains.items() for member_id in ids]
        blocked_chains = set()
        lookahead = None
        with (log_dir / f'install-{task_id}.log').open('w', encoding='utf-8') as log:
            def record(**fields):
                log.write(json.dumps({'time': db.now_iso(), **fields}, ensure_ascii=False) + '\n')
                log.flush()

            def check_still_running():
                if self.stop.is_set():
                    raise RuntimeError('Application stopped; remaining items were not prepared')
                reason = stopped_reason()
                if reason:
                    # clear_for_device()/abort_all() removed this run's own
                    # state -- update() would already silently no-op from
                    # here on, but stop doing any further real work
                    # (creating jobs, polling) too, not just the UI updates.
                    raise RuntimeError(reason)

            def stopped_reason():
                with self.lock:
                    return _stopped_reason_locked()

            def _stopped_reason_locked():
                if task_id in self.states:
                    return None
                return self._stopped_reasons.get(
                    task_id, 'Preparation cleared (device connection changed); remaining items were not prepared',
                )

            def removed(member_id):
                with self.lock:
                    return _removed_locked(member_id)

            def _removed_locked(member_id):
                state = self.states.get(task_id)
                item = state['items'].get(str(member_id)) if state else None
                return bool(item) and item.get('phase') == REMOVED_PHASE

            def progress_for(member_id):
                def preparing(item_id=None, **fields):
                    if fields.get('phase') == 'Ready':
                        fields['phase'] = 'Prepared'
                    update(item_id=item_id if item_id is not None else member_id, **fields)
                return preparing

            def prepare(member_id, own_conn):
                """Everything slow about an item: extraction, staging and the
                manifest hash. Never confirms -- see this method's docstring."""
                with self.lock:
                    selection = (self.states.get(task_id) or {}).get("amiibo_selection") or {}
                # Only an item the user narrowed down to some of its amiibo
                # carries a selection; everything else is prepared as before.
                narrowed = {str(member_id): selection[str(member_id)]} if str(member_id) in selection else None
                return create_and_confirm_jobs(
                    own_conn, [member_id], target, progress=progress_for(member_id), confirm=False,
                    **({"amiibo_selection": narrowed} if narrowed else {}),
                )

            def start_lookahead(next_index):
                """Prepares order[next_index] on its own thread, with its own
                DB connection (a sqlite3.Connection is used by one caller at a
                time by convention here -- see db.get_connection).

                Returns None -- i.e. keeps the old strictly-sequential
                behaviour -- for an item that has to be EXTRACTED first. Two
                archives' payloads must never be unpacked into work/ at the
                same time: an archive is allowed to produce up to
                config.extraction.max_extracted_size_bytes (~37 GiB by
                default), and "the previous archive's payload is gone before
                the next one is extracted" is a deliberate, separately-tested
                guarantee (test_hotfix_reconciliation.py, plus the cleanup
                assertion at the bottom of this loop). Nothing else needs
                workspace at all: a bare package file or mod folder under a
                scanned root is never copied, only hashed in place (see
                manifest._build_package_files), which is exactly the work
                worth overlapping."""
                if next_index >= len(order):
                    return None
                next_chain, next_item = order[next_index]
                if next_chain in blocked_chains or removed(next_item):
                    return None
                next_row = db.get_library_item_by_id(conn, next_item)
                if next_row is None:
                    return None
                if Path(next_row['absolute_path']).suffix.lower() in config.ARCHIVE_EXTENSIONS:
                    record(event='Not prepared ahead: needs extraction, and only one archive is '
                                 'unpacked at a time', item=next_item, chain=next_chain)
                    return None
                state = {'index': next_index, 'item_id': next_item, 'chain_key': next_chain,
                         'part': None, 'error': None}
                record(event='Preparing', item=next_item, chain=next_chain, ahead=True)

                def run_ahead():
                    try:
                        with db.open_db(self.db_path) as ahead_conn:
                            state['part'] = prepare(next_item, ahead_conn)
                    except Exception as exc:  # noqa: BLE001 -- re-raised on the main thread below
                        state['error'] = exc

                state['thread'] = threading.Thread(
                    target=run_ahead, name='switchagent-preparation-ahead', daemon=False,
                )
                state['thread'].start()
                return state

            def collect(state):
                state['thread'].join()
                if state['error'] is not None:
                    raise state['error']
                return state['part']

            def release_unconfirmed(part, reason):
                """Jobs that were prepared but never confirmed: nothing has
                been sent and nothing can be. Marked abandoned for the
                record, their staged payload released."""
                batch = part.get('batch_id') if part else None
                if batch is None:
                    return False
                conn.execute(
                    "UPDATE jobs SET status='FAILED', abandoned=1, error=? WHERE batch_id=? AND status='PENDING_CONFIRM'",
                    (reason, batch),
                )
                conn.commit()
                cleanup_batch_if_all_done(conn, batch)
                return True

            def abandon_prepared(state, reason):
                """A prepared-ahead item whose turn never came (its chain got
                blocked, or the run stopped)."""
                try:
                    part = collect(state)
                except Exception:  # noqa: BLE001 -- it never became a job; nothing to unwind
                    record(event='Discarded a prepared-ahead item that had failed to prepare',
                           item=state['item_id'], chain=state['chain_key'])
                    return
                if not release_unconfirmed(part, reason):
                    return
                if removed(state['item_id']):
                    record(event='Discarded a prepared-ahead item removed from the queue',
                           item=state['item_id'], chain=state['chain_key'])
                    return
                # Back to "Waiting", never "Failed": preparing this item early
                # was OUR optimisation, and undoing it must leave exactly the
                # state the strictly-sequential loop would have left -- an
                # item that simply never started. For the common reason
                # (an earlier item of the same game hit a conflict) the item
                # genuinely is still waiting: resolving that conflict resumes
                # this chain through continue_chain(), which prepares it
                # again from scratch.
                update(item_id=state['item_id'], phase='Waiting', error=None)
                record(event='Abandoned a prepared-ahead item', item=state['item_id'],
                       chain=state['chain_key'], reason=reason)

            record(event='Install started', items=item_ids, chains=chains)
            try:
                for index, (chain_key, item_id) in enumerate(order):
                    if chain_key in blocked_chains:
                        continue
                    check_still_running()
                    if removed(item_id):
                        if lookahead is not None and lookahead['index'] == index:
                            abandon_prepared(lookahead, 'Removed from the queue')
                            lookahead = None
                        record(event='Skipped: removed from the queue', item=item_id, chain=chain_key)
                        continue

                    if lookahead is not None and lookahead['index'] == index:
                        part = collect(lookahead)
                        lookahead = None
                    else:
                        if lookahead is not None:
                            abandon_prepared(lookahead, 'Its game was already resolved another way')
                            lookahead = None
                        record(event='Preparing', item=item_id, chain=chain_key)
                        part = prepare(item_id, conn)

                    result['created'].extend(part['created'])
                    result['errors'].extend(part['errors'])
                    result['batch_id'] = part['batch_id']
                    update(item_id=item_id, job_ids=[j['job_id'] for j in part['created']])
                    record(event='Prepared', result=part, chain=chain_key)
                    if part['errors']:
                        # This ONE item's own preparation failed (bad
                        # archive, out of space, ...) -- there's no job here
                        # to Override/Skip, so unlike a stuck job below, this
                        # chain simply moves on to its own next item rather
                        # than deferring.
                        update(item_id=item_id, phase='Failed',
                               error='; '.join(e['error'] for e in part['errors']))
                        continue
                    batch = part['batch_id']
                    if batch is None:
                        continue

                    # Only now does the worker get to see this item at all --
                    # unless the run was stopped while this item was being
                    # prepared (extracting an archive can take minutes, and
                    # Abort or a reconnect may land in the middle of it).
                    # Checked and confirmed under self.lock, the same lock
                    # abort_all() removes states under: once abort_all() has
                    # returned, no job of a run it stopped can still become
                    # visible to the worker.
                    with self.lock:
                        reason = _stopped_reason_locked()
                        if reason is None and self.stop.is_set():
                            reason = 'Application stopped; remaining items were not prepared'
                        # Removed from the queue while it was being prepared
                        # (an archive can take minutes to unpack): decided
                        # under the same lock remove_items() marks it under,
                        # so it is either confirmed or dropped, never both.
                        dropped = reason is None and _removed_locked(item_id)
                        if reason is None and not dropped:
                            for entry in part['created']:
                                db.confirm_job(conn, entry['job_id'])
                    if dropped:
                        release_unconfirmed(part, 'Removed from the queue')
                        record(event='Dropped after preparing: removed from the queue',
                               item=item_id, chain=chain_key)
                        continue
                    if reason is not None:
                        release_unconfirmed(part, f'Not started: {reason}')
                        raise RuntimeError(reason)
                    update(item_id=item_id, phase='Installing')

                    # The console is busy from here on; use that time to get
                    # the next game's bytes ready on disk.
                    if lookahead is None:
                        lookahead = start_lookahead(index + 1)

                    previous = None
                    blocked_here = False
                    while True:
                        if removed(item_id):
                            # Its not-yet-running jobs were cancelled by
                            # services.remove_from_queue(): not a failure to
                            # stop the chain on, just nothing left to wait for.
                            record(event='Removed from the queue before it started', item=item_id, chain=chain_key)
                            break
                        jobs = db.list_jobs_by_batch(conn, batch)
                        statuses = [(j['id'], j['status'], j['error']) for j in jobs]
                        if statuses != previous:
                            record(event='Jobs', jobs=statuses, chain=chain_key)
                            previous = statuses
                        # Every status a job can get stuck in without ever
                        # reaching DONE/DONE_UNVERIFIED on its own -- kept in
                        # sync with services._RETRYABLE_JOB_STATUSES (the
                        # canonical "needs a user action" set) plus CANCELLED
                        # (dead status string, never actually set, kept here
                        # defensively) and WAITING_FOR_BASE (this run's own
                        # item racing a DIFFERENT outstanding base job).
                        if any(j['status'] in (
                            'FAILED', 'INTERRUPTED', 'CANCELLED', 'WAITING_FOR_BASE',
                            'DESTINATION_CONFLICT', 'SOURCE_CHANGED', 'DEVICE_UNAVAILABLE', 'BLOCKED_BY_DEPENDENCY',
                        ) for j in jobs):
                            blocked_here = True
                            break
                        if jobs and all(j['status'] in ('DONE', 'DONE_UNVERIFIED') for j in jobs):
                            break
                        if self.stop.wait(.5):
                            raise RuntimeError('Application stopped; remaining items were not prepared')
                        reason = stopped_reason()
                        if reason:
                            raise RuntimeError(reason)
                    if blocked_here:
                        update(item_id=item_id, phase='Failed',
                               error='Needs your action -- see the job above (Override/Retry/Skip)')
                        result['blocked'].append(item_id)
                        blocked_chains.add(chain_key)
                        record(event='Blocked -- moving to the next independent game', item=item_id, chain=chain_key)
                        if lookahead is not None and lookahead['chain_key'] == chain_key:
                            # Prepared while this item was still transferring,
                            # and its own turn can no longer come: an Update
                            # must never install ahead of the Base that just
                            # stopped.
                            abandon_prepared(
                                lookahead, 'Not started: an earlier item of the same game needs your action',
                            )
                            lookahead = None
                        continue  # stop just THIS chain; the outer loop moves to the next one
                    if removed(item_id):
                        # Nothing was installed; its cancelled jobs' staging
                        # is released like any other finished batch's.
                        cleanup_batch_if_all_done(conn, batch)
                        record(event='Released after removal', item=item_id, chain=chain_key)
                        continue
                    update(item_id=item_id, phase='Cleaning')
                    cleanup_batch_if_all_done(conn, batch)
                    if batch_work_dir(batch).exists():
                        raise RuntimeError('Temporary payload cleanup failed; next archive was not extracted')
                    update(item_id=item_id, phase='Ready')
                    record(event='Installed and cleaned', item=item_id, chain=chain_key)
            except Exception as exc:
                if lookahead is not None:
                    abandon_prepared(lookahead, f'Not started: {exc}')
                    lookahead = None
                if item_id is not None:
                    update(item_id=item_id, phase='Failed', error=str(exc))
                record(event='Stopped', error=str(exc))
                raise
            finally:
                if lookahead is not None:
                    abandon_prepared(lookahead, 'Not started: nothing left in this batch to install it after')
                    lookahead = None
            record(event='Install finished', errors=result['errors'], blocked=result['blocked'])
        return result

    def clear_for_device(self, device_id: str) -> None:
        """By explicit request: a device's connection-state change, in
        EITHER direction, invalidates every preparation batch targeting
        it -- called from WebContext.refresh_devices() alongside
        services.abandon_all_jobs_for_device() (see its own docstring for
        why nothing survives a connection-state change except the
        permanent History record). Removing the state entry here is what
        makes a currently-running _install_sequentially() actually stop
        doing further work, not just stop being displayed -- see its own
        `task_id not in self.states` checks, and update()'s matching
        silent-no-op for the same reason."""
        with self.lock:
            for task_id in [tid for tid, state in self.states.items() if state.get('target') == device_id]:
                del self.states[task_id]

    def abort_all(self) -> int:
        """Queue page "Abort": every preparation run still in progress stops
        creating jobs. Its panel goes away at once (like clear_for_device());
        the run itself unwinds at its next check -- at the latest when the
        item it is preparing right now is ready, at which point that item's
        jobs are released unconfirmed instead of handed to the worker (see
        _install_sequentially). Finished panels ("Ready"/"Failed") are left
        alone: they describe runs that are already over. Returns how many
        runs were stopped."""
        with self.lock:
            running = [task_id for task_id, state in self.states.items()
                       if state.get('phase') not in ('Ready', 'Failed')]
            for task_id in running:
                self._stopped_reasons[task_id] = 'Aborted by user; remaining items were not prepared'
                del self.states[task_id]
        return len(running)

    def remove_items(self, item_ids) -> list:
        """Queue page "Remove from queue": items of a run still in progress
        are dropped from it -- the run skips them when their turn comes, one
        being prepared right now is released instead of confirmed, and one
        whose jobs were already handed to the worker stops being waited on
        (see _install_sequentially). Cancelling those jobs, and refusing when
        one is already sending, is services.remove_from_queue()'s part.
        Returns the ids actually removed."""
        wanted = {str(item_id) for item_id in item_ids}
        removed = []
        with self.lock:
            for state in self.states.values():
                if state.get('phase') in ('Ready', 'Failed'):
                    continue
                for key, item in state['items'].items():
                    if key in wanted and item.get('phase') in _REMOVABLE_PHASES:
                        item['phase'] = REMOVED_PHASE
                        removed.append(int(key))
        return removed

    def has_running(self) -> bool:
        """Is any preparation run still going (i.e. would abort_all() stop
        anything)? The Queue page shows Abort only while there is something
        to abort."""
        with self.lock:
            return any(state.get('phase') not in ('Ready', 'Failed') for state in self.states.values())

    def continue_chain(self, resolved_job_id):
        """Called right after a job is successfully Overridden/Retried/
        Skipped (see app.py's override/cancel routes) -- if that job
        belongs to a preparation batch's chain (see _group_by_title) and
        that chain has more items after it, submits THEM as a fresh,
        independent preparation run for the same target device. This is
        what makes "Base, Update, Mod" actually continue with Update and
        Mod once a conflict on Base is resolved, instead of leaving them
        stuck at "Not started" forever. A brand new panel entry, not a
        mutation of the old (already-terminal) one -- see
        _install_sequentially's own docstring for why the original run
        never waits around for this to happen on its own."""
        remaining, target = None, None
        with self.lock:
            for state in self.states.values():
                chains = state.get('chains')
                if not chains:
                    continue
                found_item_id = next(
                    (int(k) for k, item in state['items'].items() if resolved_job_id in item.get('job_ids', ())),
                    None,
                )
                if found_item_id is None:
                    continue
                for chain_ids in chains.values():
                    if found_item_id in chain_ids:
                        position = chain_ids.index(found_item_id)
                        remaining = chain_ids[position + 1:]
                        target = state.get('target')
                        break
                break
        if remaining and target:
            self.submit(remaining, target)

    def dock_items(self):
        """What the queue dock needs from preparation, and nothing more:
        one entry per library item still on its way to the console, in
        submit order. A cheap copy under the lock -- unlike snapshot(), no
        database reads, since the dock polls this on every page."""
        with self.lock:
            return [
                {"library_item_id": int(item_id), "phase": item.get("phase"), "error": item.get("error"),
                 "job_ids": list(item.get("job_ids", ())), "name": item.get("name"),
                 "role": item.get("role"), "started": state["started"], "order": item.get("order", 0)}
                for state in sorted(self.states.values(), key=lambda s: s["started"])
                for item_id, item in state["items"].items()
                if item.get("phase") != REMOVED_PHASE
            ]

    def snapshot(self):
        with self.lock:
            result = copy.deepcopy(list(self.states.values()))
        for state in result:
            state["items"] = {k: v for k, v in state["items"].items() if v.get("phase") != REMOVED_PHASE}
            state["elapsed"] = int(state.get("finished", time.time()) - state["started"])
        from .services import _job_view, _resolve_latest_retry
        with db.open_db(self.db_path) as conn:
            for state in result:
                resolved_item_ids = []
                for item_id, item in state['items'].items():
                    # A stored job_id is frozen at creation time -- if that
                    # job was since Overridden/Retried (see
                    # _resolve_latest_retry's own docstring), show the
                    # current attempt, not the superseded original.
                    rows = [_resolve_latest_retry(conn, row) for job_id in item.get('job_ids', [])
                            if (row := conn.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone())]
                    # By explicit request: once THIS item's own conflict has
                    # been resolved (Override succeeded, or Skip was
                    # clicked) and stayed that way for 10s -- the same
                    # grace period submit()'s own auto-clear already gives
                    # a fully successful batch -- its row is no longer
                    # useful clutter, even while sibling items in the same
                    # (already-dead, never-resuming) batch are still stuck
                    # "Not started" and genuinely still need the user's own
                    # action elsewhere. `rows and ...`: an item that never
                    # got a job at all (job_ids empty) must never be swept
                    # up here -- all() on an empty list is vacuously True.
                    if rows and all(_resolved_long_enough(row) for row in rows):
                        resolved_item_ids.append(item_id)
                        continue
                    item['jobs'] = [_job_view(conn, row) for row in rows]
                for item_id in resolved_item_ids:
                    del state['items'][item_id]
        return result
