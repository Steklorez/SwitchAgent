"""Application/service layer between FastAPI routes and switchagent's
existing modules (db.py, scanner.py, preview.py, queue_worker.py, manifest.py).
Routes stay thin: parse the request, call one of these, serialize the
result. Nothing here is FastAPI-specific -- these functions take a plain
sqlite3.Connection (+ a WebContext where device/worker state is needed),
same style as the rest of the project.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone
import re
from pathlib import Path
from typing import Optional

from .. import db, emuiibo, extractor, manifest as manifest_mod, preview, queue_worker, sd_files
from .. import scanner as scanner_mod
from .. import title_id as title_id_mod
from ..model import ContentType
from .context import WebContext

# ---------------------------------------------------------------------------
# Library
# ---------------------------------------------------------------------------

# Maps a job's raw status (switchagent/db.py's JOB_STATUSES) to the
# UI-facing vocabulary from docs/WEB-UI.md point 6. A library item with no
# job yet keeps its own library_items.status (AVAILABLE/NEEDS_REVIEW/
# ERROR/SKIP_DUPLICATE) unchanged -- this map only applies once a job
# exists, and then takes precedence over the raw scan classification (the
# most recent job outcome is more relevant to the user than the original
# scan verdict).
_JOB_STATUS_TO_DISPLAY = {
    "PENDING_CONFIRM": "QUEUED",
    "CONFIRMED": "QUEUED",
    "DEVICE_UNAVAILABLE": "QUEUED",
    "WAITING_FOR_BASE": "WAITING_FOR_BASE",
    "WAITING_FOR_DEVICE": "WAITING_FOR_DEVICE",
    "RUNNING": "INSTALLING",
    "VERIFYING": "INSTALLING",
    "DONE": "INSTALLED",
    "DONE_UNVERIFIED": "INSTALLED_UNVERIFIED",
    "FAILED": "FAILED",
    "INTERRUPTED": "INTERRUPTED",
    "SOURCE_CHANGED": "SOURCE_CHANGED",
    "DESTINATION_CONFLICT": "DESTINATION_CONFLICT",
    "BLOCKED_BY_DEPENDENCY": "BLOCKED_BY_DEPENDENCY",
}


def _display_name_for_path(absolute_path: str, item_type: str) -> str:
    p = Path(absolute_path)
    return p.name if item_type == "MOD_FOLDER" else p.stem


def _resolve_entry_name(conn, row, *, library_items=None) -> str:
    """Library page's own formatting convention (.stem for files, no
    extension) applied on top of the shared family-name lookup (see
    queue_worker.find_family_base_name_source) -- a mod whose own folder
    name is just a bare TITLE_ID borrows its Base/Update/DLC sibling's
    name instead, same resolution Queue/History use
    (queue_worker.display_name_for_job), never a second, independently
    maintained naming system. See find_family_base_name_source's own
    docstring (PERF-001) for why `library_items` matters at scale."""
    if row["content_type"] == ContentType.SD_FILES.value:
        return sd_files.row_display_name(row)
    if row["item_type"] == "MOD_FOLDER" and row["title_id"]:
        source = queue_worker.find_family_base_name_source(conn, row["title_id"], library_items=library_items)
        if source is not None:
            return _display_name_for_path(source["absolute_path"], source["item_type"])
    return _display_name_for_path(row["absolute_path"], row["item_type"])


def _mod_name_for_row(row) -> Optional[str]:
    """W3-003: a mod's OWN distribution name, for search only.

    A mod folder's indexed path always ends in the literal
    `.../atmosphere/contents/<TITLE_ID>` (title_id.py's
    from_atmosphere_path convention), so its own last path segment is just
    the TITLE_ID and carries no product name at all -- which is exactly why
    _resolve_entry_name() borrows the family's Base/Update/DLC name for
    DISPLAY (see the Bread and Fred bug report in
    tests/test_library_grouping.py). The human-chosen name the user
    actually downloaded it under ("Russian Language Mod") still exists, one
    or more segments further up. This walks back past the structural
    segments to recover it, so searching for the mod's own name finds it --
    without ever changing what is DISPLAYED (that stays the borrowed family
    name, unchanged). Returns None when nothing meaningful is left (e.g. a
    bare `atmosphere/contents/<ID>` at the filesystem root)."""
    if row["content_type"] != ContentType.ATMOSPHERE_MOD.value:
        return None
    parts = [p for p in Path(row["absolute_path"]).parts if p not in ("\\", "/")]
    structural = {"atmosphere", "contents"}
    index = len(parts) - 1
    while index >= 0:
        segment = parts[index]
        if segment.lower() in structural or (row["title_id"] and segment.upper() == row["title_id"].upper()):
            index -= 1
            continue
        break
    if index < 0:
        return None
    candidate = parts[index]
    # A drive root ("D:\\") or a lone separator is not a name.
    return candidate if candidate.strip(":\\/ ") else None


def _latest_job_by_library_item(conn) -> dict[int, "object"]:
    rows = conn.execute(
        "SELECT * FROM jobs WHERE library_item_id IS NOT NULL ORDER BY created_at ASC"
    ).fetchall()
    result = {}
    for row in rows:
        result[row["library_item_id"]] = row  # ASC + overwrite -> last write wins -> latest
    return result


def _library_entry_view(
    conn, row, latest_job, *, library_items=None, installed_on_device_base_ids=None,
    connected_device_ids=None, sd_presence=None,
) -> dict:
    display_status = row["status"]
    job_view = None
    recent_until = None
    if latest_job is not None:
        if latest_job["status"] in ("DONE", "DONE_UNVERIFIED") and latest_job["finished_at"]:
            finished = datetime.fromisoformat(latest_job["finished_at"])
            if finished.tzinfo is None:
                finished = finished.replace(tzinfo=timezone.utc)
            expires = finished + timedelta(minutes=20)
            if expires > datetime.now(timezone.utc):
                recent_until = expires.isoformat()
        display_status = _JOB_STATUS_TO_DISPLAY.get(latest_job["status"], latest_job["status"])
        job_view = {
            "id": latest_job["id"],
            "status": latest_job["status"],
            "target_device_id": latest_job["target_device_id"],
            "target_device_label": device_label(conn, latest_job["target_device_id"]),
            "error": latest_job["error"],
            "created_at": latest_job["created_at"],
        }
    name = _resolve_entry_name(conn, row, library_items=library_items)
    base_title_id = family_base_title_id(row["title_id"])
    # A mod is never "installed" via DBI's NCA install pipeline (it's just
    # files merged onto the SD card, see title_id.py's atmosphere_mod_root)
    # -- matching it against DBI's own installed-titles report would claim
    # something this codebase has no way to actually confirm: never
    # computed for a mod.
    confirmed_on_device = (
        installed_on_device_base_ids is not None
        and row["content_type"] not in (ContentType.ATMOSPHERE_MOD.value, ContentType.SD_FILES.value)
        and base_title_id in installed_on_device_base_ids
    )
    if confirmed_on_device and display_status == "INSTALLED_UNVERIFIED":
        # DBI's own "Installed games" CSV (the same live console read that
        # drives confirmed_on_device/"On Switch" below) is independent,
        # machine-read proof -- at least as strong as UI-003's manual
        # "user looked at the console" confirmation, which already
        # upgrades this same status. Showing "unverified" right next to a
        # green "On Switch" badge reads as this app contradicting itself
        # over something it can already prove. The underlying transport
        # fact (job_view["status"], still built from latest_job below)
        # stays DONE_UNVERIFIED, completely untouched -- only this
        # top-level summary status is promoted, same "layer a stronger
        # signal on top, never overwrite the raw one" rule the rest of
        # this module already follows for UI-003.
        display_status = "INSTALLED"
    # By explicit request (corrected from an earlier, weaker version of this
    # rule): INSTALLED_UNVERIFIED -- "transport accepted, DBI hasn't
    # confirmed" -- is a HOLDING state, not a claim that survives an actual
    # check. installed_on_device_base_ids not being None means a live DBI
    # read genuinely happened this session; if THIS title isn't in it, that
    # is itself the answer -- "checked, not there" -- not "still unverified,
    # might still be fine". Badge hides in both cases a live check could
    # have happened and didn't confirm it: no device connected at all
    # (installed_on_device_base_ids empty/unpopulated) or connected and
    # checked but absent (confirmed_on_device False). None (never passed at
    # all -- a caller with literally no DBI information, e.g. no `ctx`)
    # is the one case this still shows raw, honest "no information exists
    # to judge by" rather than a guess either way. Every OTHER status
    # (FAILED, QUEUED, INSTALLED, ...) is a historical/current-job fact
    # that doesn't depend on any live connection, and keeps showing.
    # SD_FILES (a switch/ folder, a lone .nro): whether its files are on a
    # connected card right now is checked directly (context.check_sd_files)
    # -- the "On Switch" of something DBI's Installed Games never lists.
    sd_checked = (
        row["content_type"] == ContentType.SD_FILES.value
        and sd_presence is not None and row["id"] in sd_presence
    )
    if sd_checked and sd_presence[row["id"]]:
        confirmed_on_device = True
    hide_unverified_badge = (
        display_status == "INSTALLED_UNVERIFIED"
        and installed_on_device_base_ids is not None
        and not confirmed_on_device
    )
    # By explicit request (2026-09-26, after a card stayed green with the
    # console unplugged): "installed" is only said while the console it was
    # installed to is connected -- with it gone the card says nothing at
    # all, not even that it once was. And for SD files, a live check that
    # finds them gone from the card wins over our own record of copying
    # them, exactly as the rule above lets a live DBI read win.
    if display_status in ("INSTALLED", "INSTALLED_UNVERIFIED") and not confirmed_on_device:
        if (connected_device_ids is not None and latest_job is not None
                and latest_job["target_device_id"] not in connected_device_ids):
            hide_unverified_badge = True
            # The "just installed" glow goes with it: nothing to glow about
            # on a console that is not there. (Only then -- right after a
            # game install DBI's own list is routinely stale, and the glow
            # is exactly what says "it went through" meanwhile.)
            recent_until = None
        if sd_checked:
            hide_unverified_badge = True
    return {
        "id": row["id"],
        "name": name,
        # W3-003: searchable identity fields, additive -- `name` above stays
        # exactly what it always was (the borrowed-family display name for a
        # mod), these are never substituted for it.
        "filename": Path(row["absolute_path"]).name,
        "mod_name": _mod_name_for_row(row),
        "base_title_id": base_title_id,
        "absolute_path": row["absolute_path"],
        "item_type": row["item_type"],
        "file_type": row["file_type"],
        "content_type": row["content_type"],
        "package_format": row["package_format"],
        "size": row["size"],
        "mtime": row["mtime"],
        "content_hash": row["content_hash"],
        "title_id": row["title_id"],
        "title_id_source": row["title_id_source"],
        "title_id_confident": bool(row["title_id_confident"]),
        "status": display_status,
        "library_status": row["status"],
        # Found alongside W3-006 Override: cancel_job() (the "Skip"/"Cancel"
        # button) actually sets status='FAILED', abandoned=1 -- 'CANCELLED'
        # is never a real status anywhere in this codebase (dead string,
        # left over from an earlier naming). Without the `abandoned` check
        # below, a skipped/cancelled item's checkbox stayed disabled here
        # FOREVER (latest_job stays that same FAILED row, `status` never
        # becomes DONE/DONE_UNVERIFIED on its own) -- with no way back
        # except retry_job(), which itself explicitly refuses an abandoned
        # job and tells the user to "select the source again in Library"
        # (see that function's own error message above) -- a real dead end.
        "can_install": row["status"] == "AVAILABLE" and (
            latest_job is None
            or latest_job["status"] in ("DONE", "DONE_UNVERIFIED", "CANCELLED")
            or bool(latest_job["abandoned"])
        ),
        "recent_transfer_until": recent_until,
        "suggested_action": row["suggested_action"],
        "suggested_target": row["suggested_target"],
        "note": row["note"],
        "error": row["error"],
        "first_seen_at": row["first_seen_at"],
        "last_scanned_at": row["last_scanned_at"],
        "job": job_view,
        "confirmed_on_device": confirmed_on_device,
        "hide_unverified_badge": hide_unverified_badge,
        **_sd_fields(row),
        **_emuiibo_fields(row),
    }


def _emuiibo_fields(row) -> dict:
    """An emuiibo release's version (and whether a newer one exists), or a
    virtual amiibo collection's size -- what its card says instead of a
    game's format (see switchagent/emuiibo.py)."""
    # Only these two kinds carry anything to read -- the other thousands of
    # rows of a big library are not parsed for nothing.
    kind = row["content_type"]
    details = sd_files.row_details(row) if kind in (ContentType.EMUIIBO.value, ContentType.AMIIBO.value) else {}
    release = details.get("emuiibo") if kind == ContentType.EMUIIBO.value else None
    amiibo = details.get("amiibo") if kind == ContentType.AMIIBO.value else None
    version = release.get("version") if isinstance(release, dict) else None
    return {
        "emuiibo_version": version,
        "emuiibo_outdated": emuiibo.is_outdated(version),
        "emuiibo_latest": emuiibo.LATEST_KNOWN_VERSION,
        "amiibo": amiibo if isinstance(amiibo, dict) else None,
    }


def _sd_fields(row) -> dict:
    """What an entry puts on the SD card, and what a forwarder launches from
    it (see sd_files.py). `sd` is present on an SD_FILES item, and on a
    package or mod archive that carries a switch/ folder of its own."""
    summary = sd_files.sd_summary_of(row)
    launches = sd_files.launches_of(row)
    return {
        "sd": summary.to_dict() if summary else None,
        "sd_destinations": [f"sdmc:/{app}" for app in summary.apps] if summary else [],
        "sd_label": sd_files.part_label(row["absolute_path"]),
        "launches": f"sdmc:/{launches}" if launches else None,
        # Part of the game's release, not needed by the game (sd_files.
        # assign_owners' "release" rule): ticked by hand, not with the game.
        "sd_optional": row["content_type"] == ContentType.SD_FILES.value and row["title_id_source"] == "release",
    }


# ---------------------------------------------------------------------------
# W3-001 / W3-003 / W3-004: SwitchAgent's OWN recorded activity, classified
# honestly.
#
# Two independent axes that this project has always kept separate and that
# nothing here may ever conflate (docs/ARCHITECTURE.md §"job status
# transitions", mtp/base.py's TransferStatus.UNVERIFIED docstring):
#
#   1. TRANSPORT outcome (install_history.outcome) -- what SwitchAgent
#      itself observed. `DONE` means every file of the job completed with
#      TransferStatus.COMPLETED, i.e. a size-verifiable storage where the
#      destination object was actually checked (SD_CARD and friends).
#      `DONE_UNVERIFIED` means at least one file completed only with
#      TransferStatus.UNVERIFIED -- DBI's install-like virtual node
#      (SD_INSTALL), where System.Size reads 0 for the whole transfer and
#      there is NO reliable way to confirm the receiving side finished.
#      These are genuinely different facts, never folded together.
#
#   2. CONSOLE confirmation (install_history.user_verified_outcome, UI-003)
#      -- the user's own report of what DBI showed on the console. Only
#      ever set for a DONE_UNVERIFIED row (services.set_history_verification
#      enforces that), and it never rewrites `outcome`.
#
# The hard rule: a bare DONE_UNVERIFIED row with no user confirmation must
# never render as "installed". SwitchAgent cannot back that claim up, and
# claiming it anyway is exactly the class of false certainty this project
# refuses everywhere else.
# ---------------------------------------------------------------------------

def cover_id_for_title(title_id_value: Optional[str]) -> Optional[str]:
    """The id /api/covers/{id} knows a game's cover by: its family's base
    TITLE_ID, the same one Library's cards use -- so an update, a DLC and a
    mod all show their game's cover. None when there is no valid one."""
    family = family_base_title_id(title_id_value)
    return family if family and title_id_mod.is_valid_title_id(family) else None


def family_base_title_id(title_id_value: Optional[str]) -> Optional[str]:
    """The family a TITLE_ID belongs to, via the SHARED arithmetic in
    title_id.classify_title_variant() -- never a second implementation.
    A base game's family id is itself; an update/DLC's is its recovered
    base; a mod's own title_id already IS the family id (the
    atmosphere/contents/<TITLE_ID>/ convention), which classify_title_variant
    reproduces for free since that value has low-12-bits == 0x000.
    Returns None for a missing or non-conforming TITLE_ID -- such an item
    is simply never grouped with anything, exactly like list_library_view()
    already treats it (never guessed at)."""
    if not title_id_value:
        return None
    try:
        return title_id_mod.classify_title_variant(title_id_value).base_title_id
    except (ValueError, TypeError):
        return None


# Transport outcomes that mean "this attempt did not deliver".
FAILED_ACTIVITY_OUTCOMES = ("FAILED", "INTERRUPTED", "SOURCE_CHANGED", "DESTINATION_CONFLICT")

_ACTIVITY_LABELS = {
    # The ONLY two kinds allowed to state installation as a fact.
    "TRANSPORT_VERIFIED": "Installed on Switch — transfer verified by SwitchAgent",
    "CONSOLE_CONFIRMED_SUCCESS": "Installed on Switch — you confirmed this on the console",
    # Everything below deliberately never claims it.
    "CONSOLE_CONFIRMED_FAILED": "Transfer completed, but you reported it did not work on the console",
    "TRANSPORT_UNVERIFIED": "Transfer accepted by the Switch — console result not confirmed",
    "FAILED": "Transfer failed",
    "INTERRUPTED": "Transfer interrupted",
    "DESTINATION_CONFLICT": "Destination conflict — nothing was overwritten",
    "SOURCE_CHANGED": "Source file changed before the transfer ran",
}


def classify_activity(row) -> dict:
    """One install_history row -> the exact claim SwitchAgent is entitled
    to make about it. `outcome`/`user_verified_outcome` are passed through
    completely untouched alongside the derived fields -- callers that want
    the raw facts never have to trust this function's wording.

    `implies_installed` is True for exactly two cases and no others:
    a genuine `DONE` (transport-verified), and a `DONE_UNVERIFIED` the user
    explicitly confirmed SUCCESS for. A bare `DONE_UNVERIFIED` is False --
    see this section's header."""
    outcome = row["outcome"]
    user_verified = row["user_verified_outcome"]

    if outcome == "DONE":
        kind = "TRANSPORT_VERIFIED"
    elif outcome == "DONE_UNVERIFIED":
        if user_verified == "SUCCESS":
            kind = "CONSOLE_CONFIRMED_SUCCESS"
        elif user_verified == "FAILED":
            kind = "CONSOLE_CONFIRMED_FAILED"
        else:
            kind = "TRANSPORT_UNVERIFIED"
    elif outcome in _ACTIVITY_LABELS:
        kind = outcome
    else:
        kind = "OTHER"

    return {
        "kind": kind,
        "label": _ACTIVITY_LABELS.get(kind, (outcome or "").replace("_", " ")),
        "outcome": outcome,
        "user_verified_outcome": user_verified,
        "implies_installed": kind in ("TRANSPORT_VERIFIED", "CONSOLE_CONFIRMED_SUCCESS"),
        "is_failed": kind in ("CONSOLE_CONFIRMED_FAILED", "FAILED", "INTERRUPTED",
                              "DESTINATION_CONFLICT", "SOURCE_CHANGED"),
        "is_unconfirmed": kind == "TRANSPORT_UNVERIFIED",
    }


_FILTER_PREDICATES = {
    "all": lambda e: True,
    "games": lambda e: e["content_type"] == ContentType.GAME_PACKAGE.value,
    "mods": lambda e: e["content_type"] == ContentType.ATMOSPHERE_MOD.value,
    "new": lambda e: e["job"] is None and e["status"] == "AVAILABLE",
    "installed": lambda e: e["status"] in ("INSTALLED", "INSTALLED_UNVERIFIED"),
    "queued": lambda e: e["status"] in ("QUEUED", "INSTALLING", "WAITING_FOR_BASE", "WAITING_FOR_DEVICE"),
    "failed": lambda e: e["status"] in (
        "FAILED", "INTERRUPTED", "SOURCE_CHANGED", "DESTINATION_CONFLICT", "BLOCKED_BY_DEPENDENCY",
    ),
    "needs_review": lambda e: e["status"] in ("NEEDS_REVIEW", "ERROR"),
}

_SORT_KEYS = {
    "name": lambda e: e["name"].lower(),
    "date_added": lambda e: e["first_seen_at"],
    "date_modified": lambda e: e["mtime"],
    "size": lambda e: e["size"],
    # W3-003: "Last scanned" -- the scanner's own last_scanned_at stamp
    # (when SwitchAgent last re-examined this file), deliberately distinct
    # from "date_modified" (the file's own filesystem mtime) and from
    # "date_added" (first_seen_at). All three already existed as columns;
    # only this one had no sort exposed.
    "last_scanned": lambda e: e["last_scanned_at"] or "",
}


def library_rows_in_scope(conn) -> tuple[list, list]:
    """(every library_items row, the subset that is actually part of the
    library right now).

    Not the same question. A row is KEPT after its file leaves the library
    -- marked db.LIBRARY_ITEM_RETIRED rather than deleted, because
    jobs.library_item_id has no ON DELETE and Queue/History still resolve
    their display names through it (see
    db.delete_library_items_missing_from). Those rows are records, not
    content, and every "what is in my library" read has to say so: after
    removing the last Library folder the index was correctly retired down
    to exactly these rows, and the Library page went on rendering all 19 of
    them as games, which is not what "I removed that folder" looks like.

    Retired-ness is a status and not, as a first cut had it, "the path sits
    under no configured folder". Both are true in production, but the
    second also quietly answers "no" for any row whose path is not a real
    location -- which is most of the test corpus, and reasonably so, since
    a test about sorting or filtering has no business owning a filesystem.
    A status says the thing directly and cannot be wrong about it.

    Callers that resolve DISPLAY NAMES (queue_worker, device detail) keep
    reading db.list_library_items() directly -- for them the records are
    the entire point. Which is also why the full list is still handed to
    _library_entry_view below: a family's name can legitimately come from a
    sibling that is itself retired."""
    all_items = db.list_library_items(conn)
    in_scope = [row for row in all_items if row["status"] != db.LIBRARY_ITEM_RETIRED]
    return all_items, in_scope


def list_library(
    conn, *, search: Optional[str] = None, status_filter: str = "all",
    format_filter: Optional[str] = None, sort: str = "date_added", reverse: bool = True,
    installed_on_device_base_ids: Optional[set[str]] = None,
    connected_device_ids: Optional[set[str]] = None, sd_presence: Optional[dict[int, bool]] = None,
) -> list[dict]:
    """Reads already-indexed library_items -- never rescans (point 16: a
    full rescan is a separate, explicit POST /api/scan). Filtering/sorting
    happens in Python, not SQL: library sizes here are a personal
    collection (hundreds to low thousands of entries), not a scale where
    that matters, and it keeps this function simple to read/test.

    installed_on_device_base_ids: optional set of BASE title ids (see
    WebContext.get_known_installed_title_ids()) powering each entry's
    "confirmed_on_device" field -- None (the default) means "no
    information available", not "nothing is installed" (see
    _library_entry_view's own docstring on that distinction)."""
    latest_jobs = _latest_job_by_library_item(conn)
    all_items, in_scope = library_rows_in_scope(conn)
    entries = [
        _library_entry_view(
            conn, row, latest_jobs.get(row["id"]), library_items=all_items,
            installed_on_device_base_ids=installed_on_device_base_ids,
            connected_device_ids=connected_device_ids, sd_presence=sd_presence,
        )
        for row in in_scope
    ]

    predicate = _FILTER_PREDICATES.get(status_filter, _FILTER_PREDICATES["all"])
    entries = [e for e in entries if predicate(e)]

    if format_filter:
        entries = [e for e in entries if (e["package_format"] or "").upper() == format_filter.upper()]

    if search:
        needle = search.strip().lower()
        entries = [e for e in entries if needle in e["name"].lower() or needle in (e["title_id"] or "").lower()]

    key_fn = _SORT_KEYS.get(sort, _SORT_KEYS["date_added"])
    entries.sort(key=key_fn, reverse=reverse)
    return entries


# ---------------------------------------------------------------------------
# Library grouping (base game / update / DLC / mod) -- pure arithmetic on
# the well-documented, public Nintendo Switch TITLE_ID convention (the same
# convention scene-group filenames' own "[vN]" version tags already rely
# on -- see title_id.py's module docstring for the general "filename
# metadata is a label, not proof" caveat, which this inherits unchanged):
#   base application:  low 12 bits == 0x000
#   update/patch:       low 12 bits == 0x800, same upper bits as its base
#   DLC/AddOnContent:   low 12 bits in the "DLC family" range, i.e.
#                       (id & ~0xFFF) - 0x1000 recovers the base id
# The arithmetic itself lives in title_id.classify_title_variant() --
# shared with queue_worker.py's install-order dependency enforcement, see
# that module and docs/STATE.md's Quake II investigation. This module only
# does the grouping/presentation on top of it.
# ---------------------------------------------------------------------------

# W3-003: family-level filters, applied to the GROUPED ("games") view on
# top of the existing per-entry `kind`/`format`/`search` controls -- these
# are genuinely new capability, not a rename of what was already there.
# Each predicate takes one assembled family dict (see below).
_GROUP_FILTER_PREDICATES = {
    "all": lambda g: True,
    "base": lambda g: g["base"] is not None,
    "updates": lambda g: bool(g["updates"]),
    "dlc": lambda g: bool(g["dlc"]),
    "mods": lambda g: bool(g["mods"]),
    "amiibo": lambda g: bool(g.get("amiibo")),
    "duplicates": lambda g: bool(g["duplicates"]),
}

# W3-003 sorting, at the FAMILY level. Locked-in, documented semantics
# (tests/test_library_filters.py asserts each of these directly):
#   name          -- the family's display name, case-insensitive
#   date_added    -- MAX(first_seen_at) over every variant: a family counts
#                    as "recently added" as soon as ANY of its variants is
#                    (downloading a DLC for an old game does surface it)
#   size          -- SUM(size) over every variant (base + updates + DLC +
#                    mods + duplicate copies): "how much disk this whole
#                    game costs", not just its base package
#   last_scanned  -- MAX(last_scanned_at) over every variant: the most
#                    recent time the scanner looked at ANY part of it
#   date_modified -- MAX(mtime) over every variant (kept for compatibility
#                    with the pre-W3-003 toolbar option)
_FAMILY_SORT_KEYS = {
    "name": lambda g: g["name"].lower(),
    "date_added": lambda g: g["first_seen_at"] or "",
    "date_modified": lambda g: g["mtime"] or 0,
    "size": lambda g: g["total_size"],
    "last_scanned": lambda g: g["last_scanned_at"] or "",
}


def _family_entries(game: dict) -> list[dict]:
    """Every library entry belonging to one assembled family, in one flat
    list -- the single definition of "what is in this family" that the
    family-level filters and the family-level aggregates below both use,
    so they can never drift apart."""
    return [
        e for e in ([game["base"]] + game["updates"] + game["dlc"] + game["mods"]
                    + game.get("sd_files", []) + game.get("amiibo", []) + game["duplicates"])
        if e is not None
    ]


def _search_haystack(entry: dict) -> str:
    """W3-003: everything a search term is allowed to match on one entry --
    display name, the real filename on disk, its TITLE_ID, its family's
    base TITLE_ID (so searching a base id finds its updates/DLC too), and a
    mod's own distribution name where one is recoverable (see
    _mod_name_for_row)."""
    return "\n".join(
        part.lower() for part in (
            entry["name"], entry["filename"], entry["title_id"] or "",
            entry["base_title_id"] or "", entry["mod_name"] or "",
        )
    )


def list_library_view(
    conn, *, kind: str = "games", search: Optional[str] = None,
    format_filter: Optional[str] = None, sort: str = "date_added", reverse: bool = True,
    group_filter: str = "all", not_installed: bool = False,
    installed_on_device_base_ids: Optional[set[str]] = None,
    connected_device_ids: Optional[set[str]] = None, sd_presence: Optional[dict[int, bool]] = None,
) -> dict:
    """The Library page's primary read: groups GAME_PACKAGE entries by
    (derived) base TITLE_ID -- base game + nested updates/DLC/matching
    mods. `kind` selects what's returned:
      "games"   (default) -- grouped: [{base, updates, dlc, mods, duplicates}]
      "updates" -- flat list of every UPDATE-variant entry across all games
      "dlc"     -- flat list of every DLC-variant entry across all games
      "mods"    -- flat list of every atmosphere mod (its own dedicated
                   view -- mods are never a top-level "games" card by
                   themselves, but ARE also cross-referenced into their
                   matching game's nested variants list under "games" so
                   selecting a whole game selects its mod too; see
                   docs/STATE.md's "select whole game" UX note)
    Entries with no usable TITLE_ID (NEEDS_REVIEW/ERROR bare packages)
    become their own ungrouped singleton "family" under "games" -- nothing
    is ever silently hidden just because it couldn't be classified.

    W3-003 adds, on top of all of the above (and only for kind="games",
    which is what the Library page's grouped card view actually renders):

      group_filter -- one of _GROUP_FILTER_PREDICATES: All / Base Games /
        Updates / DLC / Mods / Duplicates.

      not_installed -- an independent, additive checkbox (combines with
        group_filter rather than being one more mutually-exclusive option
        in it): keeps only families with NEITHER a successful SwitchAgent
        copy NOR a DBI confirmed_on_device match (see _finish_family's
        "installed" field, an OR of both -- either signal alone is wrong
        in one direction, see that field's own comment for the real-
        hardware finding that proved it).

    Sorting for kind="games" is family-level (see _FAMILY_SORT_KEYS);
    the flat kinds keep the per-entry _SORT_KEYS they always used. `search`
    likewise matches a whole FAMILY under kind="games" (any of its entries
    matching surfaces the whole card) and individual entries under the flat
    kinds -- see the comment at the filtering site for why.

    installed_on_device_base_ids: a set of BASE
    title ids parsed straight from DBI's own "InstalledApplications.csv"
    (see WebContext.get_known_installed_title_ids()), the one signal in
    this module that genuinely can say "this is on that console right
    now". Powers each entry's/family's "confirmed_on_device" field; None
    (the default) means "no information available", never "nothing is
    installed"."""
    latest_jobs = _latest_job_by_library_item(conn)
    all_items, in_scope = library_rows_in_scope(conn)
    all_entries = [
        _library_entry_view(
            conn, row, latest_jobs.get(row["id"]), library_items=all_items,
            installed_on_device_base_ids=installed_on_device_base_ids,
            connected_device_ids=connected_device_ids, sd_presence=sd_presence,
        )
        for row in in_scope
    ]

    if format_filter:
        all_entries = [e for e in all_entries if (e["package_format"] or "").upper() == format_filter.upper()]

    # W3-003: search is applied at the FAMILY level for kind="games" (below,
    # once families are assembled), not by pruning entries first. Pruning
    # first is what the flat kinds want, but for the grouped card view it
    # silently LOSES matches: a mod's family is built from its GAME_PACKAGE
    # siblings, so searching a mod's own name pruned away every package and
    # the game disappeared entirely (found in a live run -- searching
    # "Russian Language Mod" returned nothing at all). Matching whole
    # families also matches how a user reads the page: they searched for a
    # game, they want that game's card, updates and DLC included.
    needle = search.strip().lower() if search else None
    if needle and kind != "games":
        all_entries = [e for e in all_entries if needle in _search_haystack(e)]

    key_fn = _SORT_KEYS.get(sort, _SORT_KEYS["date_added"])
    mods = [e for e in all_entries if e["content_type"] == ContentType.ATMOSPHERE_MOD.value]
    mods.sort(key=key_fn, reverse=reverse)

    if kind == "mods":
        return {"kind": "mods", "entries": mods}

    packages = [e for e in all_entries if e["content_type"] == ContentType.GAME_PACKAGE.value]
    sd_parts = [e for e in all_entries if e["content_type"] == ContentType.SD_FILES.value]
    # Not games, so not cards in a grid of games: emuiibo, virtual amiibo
    # collections and the PC tools that come with them live on the Amiibo tab
    # (web/amiibo_views.py), which has everything to do with them. Library
    # only says how many are over there, so nothing seems to vanish.
    elsewhere = [e for e in all_entries if _lives_on_amiibo_tab(e)]
    # Add-ons catalog releases (Ultrahand, FPSLocker...) are the Add-ons
    # tab's, the same way.
    addon_items = [e for e in all_entries if e["content_type"] == ContentType.ADDON.value]
    others = [e for e in all_entries
              if e not in mods and e not in packages and e not in sd_parts and e not in elsewhere
              and e not in addon_items]

    # Mods are tagged with the base game's own TITLE_ID directly (the
    # atmosphere/contents/<TITLE_ID>/ convention) -- no variant arithmetic
    # needed, unlike updates/DLC.
    mods_by_base: dict[str, list] = {}
    for m in mods:
        if m["title_id"]:
            mods_by_base.setdefault(m["title_id"], []).append(m)

    families: dict[str, dict] = {}
    # A port whose forwarder keeps its TITLE_ID sealed (sd_files.
    # SEALED_FORWARDER) is still one card: keyed by its release folder,
    # which is how its switch/ files were matched to it at scan time.
    port_family_by_folder: dict[str, str] = {}
    for e in packages:
        if e["title_id"]:
            variant, base_id = title_id_mod.classify_title_variant(e["title_id"])
        elif e["title_id_source"] == sd_files.SEALED_FORWARDER:
            variant, base_id = "BASE", f"port-{e['id']}"
            port_family_by_folder[str(Path(e["absolute_path"]).parent)] = base_id
        else:
            variant, base_id = "BASE", f"unknown-{e['id']}"  # never grouped with anything else
        fam = families.setdefault(base_id, {"base_title_id": base_id, "bases": [], "updates": [], "dlc": []})
        if variant == "BASE":
            fam["bases"].append(e)
        elif variant == "UPDATE":
            fam["updates"].append(e)
        else:
            fam["dlc"].append(e)

    if kind == "updates":
        entries = sorted((e for fam in families.values() for e in fam["updates"]), key=key_fn, reverse=reverse)
        return {"kind": "updates", "entries": entries}
    if kind == "dlc":
        entries = sorted((e for fam in families.values() for e in fam["dlc"]), key=key_fn, reverse=reverse)
        return {"kind": "dlc", "entries": entries}

    # A game's switch/ folder is part of that game's card, the same way its
    # updates are -- which game it belongs to was settled at scan time
    # (sd_files.assign_owners, stored as its title_id). One that belongs to
    # no game in the library is shown on its own card rather than hidden:
    # a standalone homebrew app is a perfectly good thing to install.
    # A virtual amiibo collection that came with a game (scanner.amiibo_owners,
    # stored as its title_id) is part of that game's card, with a tag of its
    # own, and is installed together with it. One without a game stays off
    # the grid -- on the Amiibo tab, like emuiibo itself.
    amiibo_by_base: dict[str, list] = {}
    for e in elsewhere:
        if e["content_type"] != ContentType.AMIIBO.value:
            continue
        family = family_base_title_id(e["title_id"])
        if family is not None and family in families:
            amiibo_by_base.setdefault(family, []).append(e)
    attached = {e["id"] for parts in amiibo_by_base.values() for e in parts}
    elsewhere = [e for e in elsewhere if e["id"] not in attached]

    sd_by_base: dict[str, list] = {}
    standalone_sd = []
    for e in sd_parts:
        family = family_base_title_id(e["title_id"])
        if family is None and e["title_id_source"] == sd_files.PORT_PART:
            family = port_family_by_folder.get(str(Path(e["absolute_path"]).parent))
        if family is not None and family in families:
            sd_by_base.setdefault(family, []).append(e)
        else:
            standalone_sd.append(e)

    games = []
    for fam in families.values():
        # Prefer an AVAILABLE base as the card's primary entry; any
        # additional base-variant copies (e.g. a true duplicate download)
        # are kept, not dropped -- shown alongside updates/dlc rather than
        # silently overwritten.
        bases_sorted = sorted(fam["bases"], key=lambda x: (x["status"] != "AVAILABLE", x["name"]))
        primary = bases_sorted[0] if bases_sorted else None
        duplicates = bases_sorted[1:]
        fam_mods = sorted(mods_by_base.get(fam["base_title_id"], []), key=lambda x: x["name"])
        fam_sd = sorted(sd_by_base.get(fam["base_title_id"], []), key=lambda x: x["sd_label"])
        fam_amiibo = sorted(amiibo_by_base.get(fam["base_title_id"], []), key=lambda x: x["name"])
        name_source = primary or (fam["updates"] + fam["dlc"] + duplicates + fam_mods)[0]
        games.append(_finish_family({
            "base_title_id": fam["base_title_id"],
            "name": name_source["name"],
            "base": primary,
            "updates": sorted(fam["updates"], key=lambda x: x["name"]),
            "dlc": sorted(fam["dlc"], key=lambda x: x["name"]),
            "mods": fam_mods,
            "sd_files": fam_sd,
            "amiibo": fam_amiibo,
            "amiibo_count": sum((e.get("amiibo") or {}).get("count", 0) for e in fam_amiibo),
            "duplicates": sorted(duplicates, key=lambda x: x["name"]),
            "variant_count": (
                len(fam["updates"]) + len(fam["dlc"]) + len(fam_mods) + len(fam_sd) + len(fam_amiibo)
                + len(duplicates) + (1 if primary else 0)
            ),
        }))
    for e in standalone_sd:
        games.append(_finish_family({
            "base_title_id": f"sd-{e['id']}", "name": e["name"], "base": e,
            "updates": [], "dlc": [], "mods": [], "sd_files": [], "amiibo": [], "amiibo_count": 0,
            "duplicates": [], "variant_count": 1,
        }))
    for e in others:  # MIXED/UNKNOWN content_type -- shown, never dropped
        games.append(_finish_family({
            "base_title_id": f"other-{e['id']}", "name": e["name"], "base": e,
            "updates": [], "dlc": [], "mods": [], "sd_files": [], "amiibo": [], "amiibo_count": 0,
            "duplicates": [], "variant_count": 1,
        }))

    if needle:
        games = [g for g in games if any(needle in _search_haystack(e) for e in _family_entries(g))]

    # W3-003: the family-level filters.
    predicate = _GROUP_FILTER_PREDICATES.get(group_filter, _GROUP_FILTER_PREDICATES["all"])
    games = [g for g in games if predicate(g)]
    if not_installed:
        games = [g for g in games if not g["installed"]]

    family_key_fn = _FAMILY_SORT_KEYS.get(sort, _FAMILY_SORT_KEYS["date_added"])
    games.sort(key=family_key_fn, reverse=reverse)

    # Only what a person looks for there: amiibo that came with no game.
    # emuiibo's own releases and its PC tools are files SwitchAgent uses,
    # not something to show anyone.
    amiibo_left = sum(1 for e in elsewhere if e["content_type"] == ContentType.AMIIBO.value)
    return {"kind": "games", "games": games, "on_amiibo_tab": amiibo_left, "on_addons_tab": len(addon_items)}


def _lives_on_amiibo_tab(entry: dict) -> bool:
    return (entry["content_type"] in (ContentType.EMUIIBO.value, ContentType.AMIIBO.value)
            or entry["library_status"] == scanner_mod.NOT_FOR_SWITCH)


def _finish_family(game: dict) -> dict:
    """Adds W3-003's family-level aggregates to an assembled family. See
    _FAMILY_SORT_KEYS for the locked-in definition of each one; computing
    them here (once, at assembly) rather than inside a sort key means the
    template can display exactly the same numbers the sort ordered by."""
    entries = _family_entries(game)
    # W3-001: only a real, 16-hex family id addresses a Game Details page.
    # The synthetic "unknown-<id>"/"other-<id>" ids this function also sees
    # (an unclassifiable NEEDS_REVIEW/ERROR package that became its own
    # singleton family) deliberately get no link rather than a broken one.
    game["has_detail_page"] = title_id_mod.is_valid_title_id(game["base_title_id"])
    game["total_size"] = sum(e["size"] or 0 for e in entries)
    game["first_seen_at"] = max((e["first_seen_at"] for e in entries if e["first_seen_at"]), default=None)
    game["last_scanned_at"] = max((e["last_scanned_at"] for e in entries if e["last_scanned_at"]), default=None)
    game["mtime"] = max((e["mtime"] for e in entries if e["mtime"]), default=0)
    # True as soon as ANY variant matched DBI's live Installed Games report
    # (see _library_entry_view) -- in practice always the base (a mod's own
    # entry never sets this, see that function's docstring), but checking
    # every variant costs nothing and never depends on which one happens to
    # be present in this family.
    game["confirmed_on_device"] = any(e["confirmed_on_device"] for e in entries)
    # "Not installed" filter's signal: EITHER SwitchAgent's own record of a
    # successful copy (base status INSTALLED/INSTALLED_UNVERIFIED) OR DBI's
    # confirmed_on_device above -- deliberately an OR of both, not either
    # one alone:
    #   - SwitchAgent-only would be wrong the other direction: real-hardware
    #     confirmed (2026-09-17) that plenty of games already on the
    #     console -- installed by any means other than this app, including
    #     before the user ever started using it -- show status AVAILABLE
    #     (SwitchAgent has no job for them) while confirmed_on_device is
    #     True. SwitchAgent-only would flag every one of those as "not
    #     installed" despite them demonstrably being on the console right
    #     now -- the exact bug a live check against a real library caught.
    #   - confirmed_on_device-only would be wrong the other direction: it
    #     only refreshes when the user manually reopens DBI on-console
    #     (confirmed directly against real hardware -- a title installed
    #     20+ minutes earlier, console connected the whole time, still
    #     wasn't in DBI's list), so it would keep claiming something
    #     not-yet-installed long after SwitchAgent itself successfully
    #     copied it.
    #
    # ...with one correction (field report, 2026-09-19): SwitchAgent's own
    # record loses to a live check that says otherwise. A game installed by
    # this app, then DELETED on the console, kept showing as installed after
    # the console was reconnected and re-read -- our own INSTALLED_UNVERIFIED
    # outlived the evidence it was a placeholder for. _library_entry_view
    # already decided exactly this question one level down (see its
    # hide_unverified_badge: "INSTALLED_UNVERIFIED is a HOLDING state, not a
    # claim that survives an actual check"), so this reuses that answer
    # rather than inventing a second, contradictory one -- the badge and the
    # "Not installed" filter must never disagree about the same title.
    # INSTALLED proper is untouched: that one is either user-confirmed or
    # DBI-confirmed, never a holding state.
    # A forwarder only starts an .nro that some part of the game puts on the
    # SD card; say so when none does (Mega Man X Regenesis ships its .nro
    # outside switch.7z -- install only the archive and the icon on the
    # home menu opens nothing). Checked against every part of this family
    # that carries a switch/ folder, case-insensitively like the card.
    parts = [(e["sd_label"], sd_files.SdSummary.from_dict(e["sd"])) for e in entries if e.get("sd")]
    checks = []
    for e in entries:
        target = e.get("launches")
        if not target or e["content_type"] != ContentType.GAME_PACKAGE.value:
            continue
        if any(c["launches"] == target for c in checks):
            continue
        checks.append({"launches": target, "provided_by": sd_files.provider_of(target[len("sdmc:/"):], parts)})
    game["launch_checks"] = checks
    game["launch_missing"] = [c["launches"] for c in checks if c["provided_by"] is None]

    base = game["base"]
    base_claims_installed = base is not None and base["status"] in ("INSTALLED", "INSTALLED_UNVERIFIED")
    if base is not None and base.get("hide_unverified_badge"):
        base_claims_installed = False
    game["installed"] = game["confirmed_on_device"] or base_claims_installed
    return game


def get_library_item(conn, item_id: int) -> Optional[dict]:
    row = db.get_library_item_by_id(conn, item_id)
    if row is None:
        return None
    latest_jobs = _latest_job_by_library_item(conn)
    return _library_entry_view(conn, row, latest_jobs.get(row["id"]))


# ---------------------------------------------------------------------------
# Devices
# ---------------------------------------------------------------------------

def list_devices(conn, ctx: WebContext) -> list[dict]:
    """Devices table (friendly names/history) merged with the worker
    thread's last-observed liveness cache (WebContext.get_known_devices())
    -- deliberately NOT a live COM probe from this (request) thread, see
    WebContext's class-level note on why RealMtpBackend must only ever be
    touched from the single worker thread. A device seen before but not
    currently reachable is still listed, marked disconnected, never
    silently dropped (point 7/23: "Switch B Disconnected", not gone)."""
    from ..mtp.windows import device_fingerprint

    live_ids = {info.device_id for info in ctx.get_known_devices()}
    result = []
    for row in db.list_devices(conn):
        result.append({
            "device_id": row["device_id"],
            "device_fingerprint": device_fingerprint(row["device_id"]),
            "friendly_name": row["friendly_name"],
            "display_name": row["friendly_name"] or row["last_known_display_name"]
            or f"Switch ({device_fingerprint(row['device_id'])})",
            "last_known_display_name": row["last_known_display_name"],
            "connected": row["device_id"] in live_ids,
            "first_seen_at": row["first_seen_at"],
            "last_seen_at": row["last_seen_at"],
        })
    result.sort(key=lambda d: (not d["connected"], d["display_name"].lower()))
    return result


def rename_device(conn, device_id: str, friendly_name: Optional[str]) -> None:
    db.set_device_friendly_name(conn, device_id, friendly_name or None)


def forget_device(conn, ctx: WebContext, device_id: str) -> None:
    """Remove a device SwitchAgent has seen from the Devices page for good
    (db.forget_device() -- see it for exactly what is and is not deleted).

    Exists because "has ever been seen by this server" is not always the
    same as "is one of this user's Switches": a mock-mode run against the
    real database seeded two fixture devices into one (config.MOCK_DB_PATH
    now makes that specific accident impossible), and a console can also
    enumerate under a second identity -- e.g. the stock firmware's own MTP
    with a blanked USB serial before DBI is started -- which is a real
    device row the user has no use for. Until this existed there was no
    way to remove either without editing SQLite by hand.

    Two refusals, both ValueError (409 at the route), because forgetting
    in these states would be either pointless or destructive:
      * the device is connected RIGHT NOW -- the next refresh_devices()
        would re-record it within seconds, so the row would visibly come
        back and look like a bug;
      * unfinished jobs still target it (_not_settled -- the same
        predicate the Queue view itself uses), which would leave the
        Queue holding work for a Switch the app no longer knows.
    """
    if db.get_device(conn, device_id) is None:
        from ..mtp.windows import mask_device_id  # ARCH-001: never echo a raw, serial-bearing device_id into an HTTP response body

        raise ValueError(f"unknown device_id -- never seen before: {mask_device_id(device_id)}")
    if any(info.device_id == device_id for info in ctx.get_known_devices()):
        raise ValueError(
            "this Switch is connected right now -- disconnect it first, "
            "otherwise it is recorded again the moment it is seen"
        )
    all_rows = db.list_jobs(conn)
    unfinished = [r for r in all_rows if r["target_device_id"] == device_id and _not_settled(all_rows, r)]
    if unfinished:
        raise ValueError(
            f"{len(unfinished)} unfinished job(s) in the Queue still target this Switch -- "
            "finish or cancel them first"
        )
    db.forget_device(conn, device_id)


def device_label(conn, device_id: Optional[str]) -> str:
    """Human-facing stand-in for a device_id -- friendly_name if the user
    set one, else the last display name DBI reported, else a safe
    fingerprint (see mtp.windows.device_fingerprint). NEVER the raw
    device_id itself: that string embeds the device's real USB serial
    number (see mtp/windows.py's mask_device_id docstring on why this is
    masked in every log/doc this project writes) -- pages that show a job
    or history row to a human (Queue/History) must not put that serial in
    plain, readable text. The raw device_id is still used everywhere it
    functionally needs to be (API payloads, HTML form values, matching
    logic) -- this helper is display-only."""
    if not device_id:
        return "—"
    row = db.get_device(conn, device_id)
    if row is not None and row["friendly_name"]:
        return row["friendly_name"]
    if row is not None and row["last_known_display_name"]:
        return row["last_known_display_name"]
    from ..mtp.windows import device_fingerprint
    return f"Switch ({device_fingerprint(device_id)})"


# ---------------------------------------------------------------------------
# Device storage mapping (UI-007) -- worker-owned discovery
# (WebContext.refresh_devices/get_known_storages, see that module), manual
# override persisted in device_storage_mappings (db.py). Restricted to
# AUTO/SD_CARD/SD_INSTALL only from the Web UI -- never NAND_USER/
# NAND_SYSTEM/NAND_INSTALL/SAVES/ALBUM, preserving the existing "this
# project never writes to NAND" safety model untouched.
# ---------------------------------------------------------------------------

ALLOWED_MANUAL_STORAGE_MAPPINGS = ("SD_CARD", "SD_INSTALL")


def list_device_storages(conn, ctx: WebContext, device_id: str) -> list[dict]:
    """One row per storage this device reported at its last worker-owned
    refresh (WebContext.get_known_storages() -- empty if never refreshed,
    e.g. the device has never actually been seen connected; never a live
    COM call from this, an HTTP request thread). Each row shows both the
    AUTO-resolved name (what mtp.windows.logical_storage_name() alone
    would produce from the raw name) and the EFFECTIVE name actually in
    use (StorageInfo.name -- post manual override if one applies, as of
    the worker's last refresh), plus whether a manual mapping is what's
    driving it -- checked against device_storage_mappings directly, not
    inferred from whether the two names happen to differ."""
    from ..mtp.windows import logical_storage_name

    manually_mapped_raw_names = {
        row["raw_storage_name"] for row in db.list_device_storage_mappings(conn, device_id)
    }
    result = []
    for info in ctx.get_known_storages(device_id):
        raw_name = info.raw_name or info.name
        result.append({
            "raw_name": raw_name,
            "auto_logical_name": logical_storage_name(raw_name),
            "effective_logical_name": info.name,
            "mapping_source": "MANUAL" if raw_name in manually_mapped_raw_names else "AUTO",
            "writable": info.writable,
            "free_bytes": info.free_bytes,
            "total_bytes": info.total_bytes,
        })
    return result


def set_device_storage_mapping(conn, ctx: WebContext, device_id: str, raw_storage_name: str, logical_name: str) -> None:
    """Validates before persisting:
    - logical_name must be one of ALLOWED_MANUAL_STORAGE_MAPPINGS -- never
      NAND_*/SAVES/ALBUM/anything else via the Web UI.
    - raw_storage_name must be a storage this device is CURRENTLY known to
      actually report (WebContext.get_known_storages()) -- never an
      arbitrary browser-supplied string with no relation to real hardware
      state (point 14: never accept an unvalidated identifier from the
      browser and act on it).
    - no second raw_storage_name on the SAME device may already be
      manually mapped to the SAME logical_name -- mtp/windows.py's
      _get_storage_item() (and MockMtpBackend's equivalent) picks whichever
      matching child comes first, so two manual entries mapped to the same
      logical name would silently make one of them unreachable; refusing
      this keeps the mapping unambiguous."""
    if logical_name not in ALLOWED_MANUAL_STORAGE_MAPPINGS:
        raise ValueError(
            f"'{logical_name}' is not an allowed manual mapping target -- only "
            f"{', '.join(ALLOWED_MANUAL_STORAGE_MAPPINGS)} may be set from the Web UI"
        )
    known_raw_names = {info.raw_name or info.name for info in ctx.get_known_storages(device_id)}
    if raw_storage_name not in known_raw_names:
        raise ValueError(f"'{raw_storage_name}' is not a currently known storage on this device")
    for row in db.list_device_storage_mappings(conn, device_id):
        if row["logical_name"] == logical_name and row["raw_storage_name"] != raw_storage_name:
            raise ValueError(
                f"'{logical_name}' is already manually mapped to a different storage on this device "
                "-- clear that mapping first"
            )
    db.set_device_storage_mapping(conn, device_id, raw_storage_name, logical_name)


def clear_device_storage_mapping(conn, device_id: str, raw_storage_name: str) -> None:
    db.clear_device_storage_mapping(conn, device_id, raw_storage_name)


# ---------------------------------------------------------------------------
# Queue / Jobs
# ---------------------------------------------------------------------------

_DESTINATION_CONFLICT_PATH_RE = re.compile(r"^'(.+)' already exists on '.+' and was not sent")


def _destination_conflict_path(row) -> Optional[str]:
    """W3-006: queue_worker.py's DESTINATION_CONFLICT error is free text
    (`f"'{file.dest_relative_path}' already exists on '{storage}' and was
    not sent by this job -- refusing to overwrite"`) -- extracts just the
    relative path for the Conflict UX card, without adding a new DB
    column for something already fully recoverable from the existing
    field. Returns None for any other status, or if the text doesn't
    match the expected shape (never guesses)."""
    if row["status"] != "DESTINATION_CONFLICT" or not row["error"]:
        return None
    match = _DESTINATION_CONFLICT_PATH_RE.match(row["error"])
    return match.group(1) if match else None


def _resolve_latest_retry(conn, row):
    """Follows `row`'s retry chain forward to its latest non-abandoned
    retry, if any -- for a caller that wants "the current state of this
    piece of work", not this exact job's own permanent record (get_job()/
    GET /api/jobs/{id} deliberately must NOT do this: a job's own row is a
    permanent, unaltered record of that one attempt -- see
    test_retry_leaves_old_install_history_entry_completely_untouched).
    PreparationQueue.snapshot() is the one caller that wants this: a
    preparation item stores the job_id it was given at creation and never
    updates it, so without following forward, an item whose job was
    Overridden/Retried kept showing the stale original's card forever --
    with fully live Override/Skip buttons, since the old row's status
    never itself changes when it's retried (see _not_settled()'s
    docstring for the matching fix already applied to the plain Queue
    list). retry_of_job_id always points to a strictly earlier job id
    (see retry_job()), so this can only terminate."""
    while True:
        newer = conn.execute(
            "SELECT * FROM jobs WHERE retry_of_job_id=? AND abandoned=0 ORDER BY id DESC LIMIT 1",
            (row["id"],),
        ).fetchone()
        if newer is None:
            return row
        row = newer


# What a queued item actually IS, as a badge on its Queue row -- the same
# four kinds the install-confirmation dialog already tags (library.js's
# CONFIRM_ROLE_LABELS), reusing its role names so both surfaces can share one
# vocabulary and one colour per kind. "base" is the one the dialog leaves
# untagged (there, everything hangs under a base-game header that names it);
# Queue is a flat list with no such header, so a plain game needs saying too.
_VARIANT_ROLE_LABEL = {"base": "Game", "update": "Update", "dlc": "DLC", "mod": "Mod", "sd": "SD files",
                       "amiibo": "Amiibo", "emuiibo": "emuiibo", "addon": "Add-on"}

_TITLE_VARIANT_TO_ROLE = {"BASE": "base", "UPDATE": "update", "DLC": "dlc"}


def _job_variant_role(job_row) -> Optional[str]:
    """'base'/'update'/'dlc'/'mod' for one job, or None when this job's own
    frozen manifest cannot say (missing/unreadable -- FAULT-001's defensive
    load pattern, same as _package_variant_label below -- or a package with
    no classifiable title_id). None means the row simply gets no badge:
    guessing a kind would be worse than showing none."""
    try:
        manifest = manifest_mod.load_manifest(job_row["id"])
    except (FileNotFoundError, ValueError, KeyError, TypeError):
        return None
    if manifest.content_type == ContentType.ATMOSPHERE_MOD.value:
        return "mod"
    if manifest.content_type == ContentType.SD_FILES.value:
        return "sd"
    if manifest.content_type == ContentType.AMIIBO.value:
        return "amiibo"
    if manifest.content_type == ContentType.EMUIIBO.value:
        return "emuiibo"
    if manifest.content_type == ContentType.ADDON.value:
        return "addon"
    if manifest.content_type != ContentType.GAME_PACKAGE.value or not manifest.title_id:
        return None
    variant, _base_id = title_id_mod.classify_title_variant(manifest.title_id)
    return _TITLE_VARIANT_TO_ROLE.get(variant)


# What a job's status pill says on the Queue page: words, not the stored
# enum ("CONFIRMED" told nobody anything). RUNNING is worded by queue.js from
# the target storage, and a CONFIRMED job reads "Paused" while the worker is.
JOB_STATUS_LABELS = {
    "PENDING_CONFIRM": "Preparing",
    "CONFIRMED": "Up next",
    "RUNNING": "Installing",
    "VERIFYING": "Verifying",
    "WAITING_FOR_BASE": "Waiting for the game",
    "WAITING_FOR_DEVICE": "Waiting for the Switch",
    "DEVICE_UNAVAILABLE": "Switch not connected",
    "FAILED": "Failed",
    "INTERRUPTED": "Interrupted",
    "SOURCE_CHANGED": "File changed",
    "BLOCKED_BY_DEPENDENCY": "Blocked",
    "DESTINATION_CONFLICT": "Already on the Switch",
    "DONE": "Installed",
    "DONE_UNVERIFIED": "Sent",
}


def _job_view(conn, row) -> dict:
    from ..mtp.windows import device_fingerprint

    variant_role = _job_variant_role(row)
    item = db.get_library_item_by_id(conn, row["library_item_id"]) if row["library_item_id"] is not None else None
    # Same helper the worker's history uses -- minus its " — Mod" suffix,
    # which the [Mod] badge below now says instead (History keeps it: no
    # badge there).
    display_name = queue_worker.display_name_for_job(
        conn, row, mod_suffix=variant_role not in ("mod", "sd", "amiibo", "emuiibo", "addon"))
    stall_seconds = _stall_seconds(row)
    return {
        # Queue badge: what this job installs (see _job_variant_role).
        "variant_role": variant_role,
        "variant_label": _VARIANT_ROLE_LABEL.get(variant_role),
        # Queue's row icon: the game's cover, whichever part this job is.
        "cover_id": cover_id_for_title(item["title_id"]) if item is not None else None,
        "abandoned": bool(row["abandoned"]),
        "id": row["id"],
        "display_name": display_name,
        "status": row["status"],
        "status_label": JOB_STATUS_LABELS.get(row["status"], row["status"].replace("_", " ").capitalize()),
        "target_device_id": row["target_device_id"],
        "target_device_label": device_label(conn, row["target_device_id"]),
        # W3-004: the safe, non-serial-bearing URL stand-in for this job's
        # target device -- lets Queue link straight at that device's own
        # detail page (/devices/{fingerprint}) instead of the generic list,
        # without ever putting the raw device_id in a user-visible URL.
        # jobs.target_device_id is NOT NULL (db.create_job enforces it), so
        # this is always computable.
        "target_device_fingerprint": device_fingerprint(row["target_device_id"]),
        "target_storage": row["target_storage"],
        "bytes_total": row["bytes_total"],
        "bytes_done": row["bytes_done"],
        "attempt_count": row["attempt_count"],
        "error": row["error"],
        "auto_resume": bool(row["auto_resume"]),
        "created_at": row["created_at"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "batch_id": row["batch_id"],
        "possibly_stalled": stall_seconds is not None,
        "stall_seconds": stall_seconds,
        "destination_conflict_path": _destination_conflict_path(row),
    }


# UI-006: stall detection is a DERIVED UI condition computed at read time
# from jobs.last_progress_at (touched by queue_worker.py only at real,
# honest progress points -- RUNNING start, the start of each file, and
# each file's completion -- never a fake periodic keepalive; see that
# module's own comments) -- never a stored status, never a new terminal
# job state. There is no reliable byte-level MTP progress signal to build
# a real ETA or percentage from (see MtpBackend's own module docstring);
# this only ever answers "how long since this job last showed ANY
# observable progress", which is architecturally the most honest signal
# available -- the UI must present it with the same disclaimer every time
# (see queue.html): large transfers may legitimately take a long time,
# this is not a failure and never auto-fails the job.
STALL_THRESHOLD_SECONDS = 300.0


def _stall_seconds(row) -> Optional[float]:
    if row["status"] != "RUNNING":
        return None
    reference = row["last_progress_at"] or row["started_at"]
    if not reference:
        return None
    from datetime import datetime, timezone
    reference_dt = datetime.fromisoformat(reference)
    elapsed = (datetime.now(timezone.utc) - reference_dt).total_seconds()
    return elapsed if elapsed >= STALL_THRESHOLD_SECONDS else None


# Buckets job statuses into exactly the four aggregate counters UI-002
# asks the grouped Queue view to show: total/active/successful
# transport/failed/waiting. DONE_UNVERIFIED counts as "successful" here on
# purpose -- this is transport-outcome bucketing, matching the mandate's
# own wording ("successful transport"), NOT user-verification (UI-003) --
# a job the user later marks Console: Confirmed failed still counts as
# successful transport here.
_STATUS_BUCKETS = {
    "RUNNING": "active", "VERIFYING": "active",
    "DONE": "successful", "DONE_UNVERIFIED": "successful",
    "FAILED": "failed", "INTERRUPTED": "failed", "SOURCE_CHANGED": "failed",
    "DESTINATION_CONFLICT": "failed", "BLOCKED_BY_DEPENDENCY": "failed",
}


def _status_bucket(status: str) -> str:
    return _STATUS_BUCKETS.get(status, "waiting")


# Statuses that mean "still in line, nothing physically happening on the
# device yet" -- point 26: cancel is only offered here. RUNNING is
# deliberately excluded: a physical MTP operation may be in flight and
# this backend has no atomic "stop now" (see MtpBackend's module docstring
# on real MTP's own limitations) -- pretending otherwise would violate the
# project's core "never claim what isn't proven" rule. WAITING_FOR_BASE is
# included -- a job stuck waiting on a base game the user has no intention
# of installing needs a way out too (see docs/STATE.md's Quake II
# investigation for why this status exists). WAITING_FOR_DEVICE (UI-005) is
# included for the same reason -- self-resolving if the Switch reappears,
# but the user may simply not intend to reconnect that device at all.
_CANCELABLE_JOB_STATUSES = (
    "PENDING_CONFIRM", "CONFIRMED", "DEVICE_UNAVAILABLE", "WAITING_FOR_BASE", "WAITING_FOR_DEVICE",
    "FAILED", "INTERRUPTED", "SOURCE_CHANGED", "BLOCKED_BY_DEPENDENCY",
    # DESTINATION_CONFLICT is deliberately absent: queue_worker.py already
    # resolves it automatically (Settings' conflict policy) the instant it
    # happens -- abandoned=1 is set right there, not by a later cancel.
)


def _not_settled(rows: list, row) -> bool:
    """True if `row` still belongs in the active Queue view. Excludes:
    - DONE/DONE_UNVERIFIED (History's job, see list_history_grouped()).
    - abandoned (Cancel, or an automatic DESTINATION_CONFLICT resolution --
      see cancel_job() and queue_worker.py's FileAlreadyExistsError
      handling): nothing is left to DO with this attempt; retry_job()
      explicitly refuses to touch an abandoned job ("select the source
      again in Library").
    - superseded (some OTHER job's retry_of_job_id points at this one --
      see db.create_job()'s own comment on the column): clicking Retry on
      a FAILED card used to leave the OLD card sitting there completely
      unchanged, showing the exact same live Retry button as before --
      nothing stopped a second, third, Nth click creating a pile of
      parallel duplicate attempts at the SAME file. The instant a retry
      exists, the old row is superseded and drops out of Queue -- still a
      permanent, untouched record (still in History), just not active
      work, and no longer clickable."""
    if row["status"] in ("DONE", "DONE_UNVERIFIED") or row["abandoned"]:
        return False
    return not any(r["retry_of_job_id"] == row["id"] for r in rows)


def _not_yet_queued(row) -> bool:
    """A job the worker cannot see yet, and which therefore has no business
    being drawn as a Queue row: PENDING_CONFIRM means "created and staged,
    but deliberately not confirmed" (see create_and_confirm_jobs' own
    confirm=False path).

    Why this is separate from _not_settled() rather than folded into it:
    _not_settled() also answers "is there unfinished work against this
    device" for forget_device(), where a staged-but-unconfirmed job very
    much still counts. This predicate is display-only.

    What it fixes: web/preparation.py prepares the NEXT item while the
    current one is still transferring, and records that item's job_ids
    only when its own turn actually arrives (_install_sequentially). In
    between, those PENDING_CONFIRM rows belonged to no preparation item,
    so queue.js's `represented` filter could not suppress them and the
    same file was drawn TWICE -- once as a "Waiting" preparation row and
    again as a separate batch group below it, for the whole duration of
    the previous file's transfer. The same window exists, more briefly,
    on the ordinary non-lookahead path (jobs are created before the
    batch is confirmed) and permanently for any job orphaned in
    PENDING_CONFIRM by a process that died mid-preparation -- the
    in-memory PreparationQueue that owned it does not survive a restart
    (db.abandon_unconfirmed_jobs cleans those up at startup)."""
    return row["status"] == "PENDING_CONFIRM"


def list_queue(conn) -> list[dict]:
    all_rows = db.list_jobs(conn)
    rows = [r for r in all_rows if not _not_yet_queued(r) and _not_settled(all_rows, r)]
    return [_job_view(conn, r) for r in rows]


# EMERGENCY HOTFIX: Base/Update/DLC display labels for jobs that fan out
# from the same multi-package archive -- see _batch_group_view below.
# Deliberately not .capitalize() ("DLC" -> "Dlc" is wrong); an explicit map
# instead.
_VARIANT_DISPLAY_LABEL = {"BASE": "Base", "UPDATE": "Update", "DLC": "DLC"}


def _package_variant_label(job_row) -> Optional[str]:
    """'Base'/'Update'/'DLC' for a GAME_PACKAGE job whose frozen manifest
    still carries a classifiable title_id -- None if the manifest is
    missing/unreadable (FAULT-001: same defensive load pattern used
    elsewhere in this file) or the content isn't classifiable this way
    (mods, or a NEEDS_REVIEW package with no title_id)."""
    try:
        manifest = manifest_mod.load_manifest(job_row["id"])
    except (FileNotFoundError, ValueError, KeyError, TypeError):
        return None
    if manifest.content_type != ContentType.GAME_PACKAGE.value or not manifest.title_id:
        return None
    variant, _base_id = title_id_mod.classify_title_variant(manifest.title_id)
    return _VARIANT_DISPLAY_LABEL.get(variant)


def group_jobs_by_game(jobs: list) -> list[dict]:
    """Queue's cards: one per game, in the order its first job appears, each
    holding every job that installs a part of it (base, update, DLCs, mods).
    The key is the game's cover id -- its family's base TITLE_ID; a job with
    none (unclassifiable file, inbox item) is a card of its own. queue.js's
    renderGameGroups() applies the same rule to what it polls."""
    games: dict = {}
    for job in jobs:
        key = job["cover_id"] or f"job-{job['id']}"
        game = games.setdefault(key, {"cover_id": job["cover_id"], "name": job["display_name"], "jobs": []})
        game["jobs"].append(job)
    return list(games.values())


def _batch_group_view(conn, batch_id: Optional[int], rows: list) -> dict:
    jobs = [_job_view(conn, r) for r in rows]
    # Multi-package-archive fan-out: several jobs in this SAME batch can
    # come from the SAME library item (one archive, several install jobs --
    # see web/services.create_and_confirm_jobs). Queue must show those
    # distinctly, not as N identically-named rows -- append each one's own
    # Base/Update/DLC label. Purely a display refinement: does not change
    # job identity, ordering, or dependency logic, and never fires for the
    # ordinary single-job-per-item case (group size < 2).
    by_item: dict[Optional[int], list] = {}
    for row, job in zip(rows, jobs):
        by_item.setdefault(row["library_item_id"], []).append((row, job))
    for item_id, group in by_item.items():
        if item_id is None or len(group) < 2:
            continue
        for row, job in group:
            label = _package_variant_label(row)
            if label:
                job["display_name"] = f"{job['display_name']} — {label}"

    counts = {"active": 0, "successful": 0, "failed": 0, "waiting": 0}
    for r in rows:
        counts[_status_bucket(r["status"])] += 1

    if batch_id is None:
        created_at = rows[0]["created_at"]
        target_device_id = rows[0]["target_device_id"]
    else:
        batch_row = db.get_installation_batch(conn, batch_id)
        created_at = batch_row["created_at"] if batch_row is not None else min(r["created_at"] for r in rows)
        target_device_id = batch_row["target_device_id"] if batch_row is not None else rows[0]["target_device_id"]

    display_name = jobs[0]["display_name"] if len(jobs) == 1 else f"{len(jobs)} items"
    return {
        "games": group_jobs_by_game(jobs),
        "batch_id": batch_id,
        "display_name": display_name,
        "created_at": created_at,
        "target_device_id": target_device_id,
        "target_device_label": device_label(conn, target_device_id),
        "jobs": jobs,
        "total": len(jobs),
        "finished": counts["successful"] + counts["failed"],
        "active": counts["active"],
        "successful": counts["successful"],
        "failed": counts["failed"],
        "waiting": counts["waiting"],
    }


def list_queue_grouped(conn) -> list[dict]:
    """UI-002: the same jobs list_queue() would show, grouped by
    installation batch. Unlike list_queue() (which excludes DONE/
    DONE_UNVERIFIED jobs entirely), a batch that still has at least one
    unfinished job shows ALL of its jobs, including any already-
    DONE_UNVERIFIED members -- e.g. a base game that finished while its
    DLC is still WAITING_FOR_BASE stays visible as part of that batch's
    progress. A batch is dropped from Queue entirely once EVERY one of its
    jobs is settled -- see _not_settled()'s own docstring for the full
    definition (DONE/DONE_UNVERIFIED, abandoned via Cancel/Skip, or
    superseded by a later Retry/Override). Fully represented in grouped
    History instead, see list_history_grouped(). Legacy batch_id=NULL jobs
    (created before this feature existed, or anything that predates it in
    an upgraded DB) each render as their own single-job group, exactly
    matching list_queue()'s own filtering. Staged-but-unconfirmed jobs are
    excluded from both, per job and then per batch -- see _not_yet_queued()
    for the duplicated-row bug that is about."""
    all_rows = db.list_jobs(conn)
    by_batch: dict[Optional[int], list] = {}
    for row in all_rows:
        by_batch.setdefault(row["batch_id"], []).append(row)

    groups = []
    for row in by_batch.pop(None, []):
        if _not_yet_queued(row) or not _not_settled(all_rows, row):
            continue
        groups.append(_batch_group_view(conn, None, [row]))

    for batch_id, rows in by_batch.items():
        # Unconfirmed rows are dropped from the group BEFORE the keep/drop
        # decision below, so a batch that is nothing but staged-ahead work
        # disappears wholesale rather than rendering as a phantom duplicate
        # of a file the preparation panel is already showing. Settled rows
        # are deliberately NOT dropped here -- a DONE base still belongs in
        # its batch's "N / M finished" while its DLC waits (see this
        # function's own docstring).
        rows = [r for r in rows if not _not_yet_queued(r)]
        if not any(_not_settled(all_rows, r) for r in rows):
            continue
        groups.append(_batch_group_view(conn, batch_id, rows))

    groups.sort(key=lambda g: g["created_at"])
    return groups


# Queue dock: what one game's parts are doing, strongest first -- the game's
# own row shows the first of these any part is in.
_DOCK_STATE_ORDER = ("installing", "preparing", "attention", "waiting", "done")
_DOCK_PREP_PHASE_STATE = {
    "Waiting": "waiting", "Prepared": "waiting",
    "Analyzing": "preparing", "Preparing": "preparing", "Installing": "preparing", "Cleaning": "preparing",
    "Failed": "attention",
}
_DOCK_WAITING_FOR_SWITCH = ("WAITING_FOR_DEVICE", "DEVICE_UNAVAILABLE")


def _dock_job_state(row) -> str:
    if row["status"] in ("DONE", "DONE_UNVERIFIED"):
        return "done"
    bucket = _status_bucket(row["status"])
    return {"active": "installing", "failed": "attention"}.get(bucket, "waiting")


def queue_dock(conn, prep_items: list, *, paused: bool = False) -> dict:
    """The dock along the bottom of every page: one row per GAME, not per
    file -- its base, update, DLC and mods are parts of one row, with one
    bar over all of their bytes. Built from the same two sources Queue
    draws from (confirmed jobs, and preparation items that have no job
    yet), grouped by the same family key Library uses."""
    all_rows = db.list_jobs(conn)
    # _not_settled(), with the "superseded by a retry" scan done once rather
    # than per row -- this runs on every page, every couple of seconds.
    superseded = {r["retry_of_job_id"] for r in all_rows if r["retry_of_job_id"] is not None}
    visible = [
        r for r in all_rows
        if not _not_yet_queued(r) and r["status"] not in ("DONE", "DONE_UNVERIFIED")
        and not r["abandoned"] and r["id"] not in superseded
    ]
    # A finished part still counts towards its game's bar while the rest
    # of the batch it came in is on its way.
    open_batches = {r["batch_id"] for r in visible if r["batch_id"] is not None}
    rows = visible + [
        r for r in all_rows
        if r["batch_id"] in open_batches and r["status"] in ("DONE", "DONE_UNVERIFIED") and not r["abandoned"]
    ]
    rows.sort(key=lambda r: r["id"])

    items: dict = {}

    def library_item(item_id):
        if item_id is None:
            return None
        if item_id not in items:
            items[item_id] = db.get_library_item_by_id(conn, item_id)
        return items[item_id]

    games: dict[str, dict] = {}

    def add_part(key, *, title_id, name, role, state, status, done, total):
        game = games.get(key)
        if game is None:
            game = games[key] = {"key": key, "cover_id": title_id, "name": name, "parts": [], "seq": len(games)}
        if role == "base" or not game["name"]:
            game["name"] = name
        game["parts"].append({"role": role, "state": state, "status": status, "done": done, "total": total})

    def family_key(item, fallback):
        cover_id = cover_id_for_title(item["title_id"]) if item is not None else None
        return (cover_id, cover_id) if cover_id else (fallback, None)

    for row in rows:
        item = library_item(row["library_item_id"])
        key, title_id = family_key(item, f"job-{row['id']}")
        state = _dock_job_state(row)
        total = row["bytes_total"] or (item["size"] if item is not None else 0) or 0
        done = total if state == "done" else min(row["bytes_done"] or 0, total)
        name = queue_worker.display_name_for_job(conn, row, mod_suffix=False)
        add_part(key, title_id=title_id, name=history_title_name(name), role=_job_variant_role(row),
                 state=state, status=row["status"], done=done, total=total)

    job_ids = {r["id"] for r in all_rows if not _not_yet_queued(r)}
    for prep in prep_items:
        state = _DOCK_PREP_PHASE_STATE.get(prep["phase"])
        # Ready, or already a confirmed job drawn above.
        if state is None or any(job_id in job_ids for job_id in prep["job_ids"]):
            continue
        item = library_item(prep["library_item_id"])
        key, title_id = family_key(item, f"item-{prep['library_item_id']}")
        add_part(key, title_id=title_id, name=history_title_name(prep["name"] or ""), role=prep["role"],
                 state=state, status=prep["phase"], done=0, total=(item["size"] if item is not None else 0) or 0)

    result = []
    for game in games.values():
        parts = game["parts"]
        states = {p["state"] for p in parts}
        if states == {"done"}:
            continue
        state = next(s for s in _DOCK_STATE_ORDER if s in states)
        current = next(p for p in parts if p["state"] == state)
        total = sum(p["total"] for p in parts)
        done = sum(p["done"] for p in parts)
        result.append({
            "key": game["key"],
            "name": game["name"],
            "cover_id": game["cover_id"],
            "state": state,
            "status_label": "Paused" if paused and state == "waiting" else _dock_status_label(state, current),
            "parts_label": _dock_parts_label(parts),
            "parts_done": sum(1 for p in parts if p["state"] == "done"),
            "parts_total": len(parts),
            "bytes_done": done,
            "bytes_total": total,
            "_seq": game["seq"],
        })
    # The game being installed first, then everything else in the order it
    # was asked for.
    result.sort(key=lambda g: (g["state"] != "installing", g["_seq"]))
    for game in result:
        del game["_seq"]
    return {"games": result, "paused": paused}


def _dock_status_label(state: str, part: dict) -> str:
    if state == "installing":
        what = _VARIANT_ROLE_LABEL.get(part["role"])
        return f"Installing {what}" if what and part["role"] != "base" else "Installing"
    if state == "preparing":
        return "Preparing"
    if state == "attention":
        return "Needs a decision in Queue"
    if part["status"] in _DOCK_WAITING_FOR_SWITCH:
        return "Waiting for the Switch"
    return "Queued"


def _dock_parts_label(parts: list) -> str:
    """"Game · Update · 2 DLC · Mod" -- what this one row stands for."""
    counts: dict[str, int] = {}
    for part in parts:
        role = part["role"] or "other"
        counts[role] = counts.get(role, 0) + 1
    labels = []
    for role in ("base", "update", "dlc", "mod", "sd", "emuiibo", "amiibo", "addon", "other"):
        n = counts.get(role)
        if not n:
            continue
        label = _VARIANT_ROLE_LABEL.get(role, "File")
        labels.append(label if n == 1 else f"{n} {label}{'' if label in ('DLC', 'SD files', 'emuiibo') else 's'}")
    return " · ".join(labels)


def get_job(conn, job_id: int) -> Optional[dict]:
    row = db.get_job(conn, job_id)
    return _job_view(conn, row) if row is not None else None


class JobCreationError(Exception):
    def __init__(self, library_item_id: int, message: str):
        super().__init__(message)
        self.library_item_id = library_item_id
        self.message = message


def resolve_target_device_id(conn, value: str) -> str:
    """Accepts EITHER a raw device_id or that device's safe fingerprint
    (mtp.windows.device_fingerprint) and always returns the raw device_id.

    Why this exists (W3-001): the install target <select> is rendered into
    HTML on both the Library page and the new Game Details page. Putting
    the raw device_id in its option values would mean every one of those
    pages' HTML carries the device's real USB serial (see mask_device_id's
    docstring) -- harmless as long as nobody ever shares the page source,
    which is not a guarantee worth relying on. Emitting the fingerprint
    instead and resolving it back HERE keeps the serial server-side without
    inventing a second job-creation endpoint or changing this function's
    own contract: an unrecognized value is passed straight through
    unchanged, so every existing caller that already had a raw device_id
    (the JSON API, retry, the CLI, every existing test) behaves exactly as
    before. Fingerprints are 16 lowercase hex characters and a real
    device_id is a long WPD path string, so the two can never be confused
    for one another."""
    if not value:
        return value
    from ..mtp.windows import device_fingerprint

    for row in db.list_devices(conn):
        if device_fingerprint(row["device_id"]) == value:
            return row["device_id"]
    return value


# Deterministic Base -> Update -> DLC install order for a multi-package
# archive (EMERGENCY HOTFIX: an archive with Base+Update+DLC used to only
# ever install the Base -- see create_and_confirm_jobs below). Multiple
# entries of the SAME variant (e.g. two DLCs) sort by their own
# archive-relative path next -- never by a guessed "latest version", which
# title_id.classify_title_variant's own docstring already says not to trust
# for anything beyond family grouping.
_PACKAGE_VARIANT_ORDER = {"BASE": 0, "UPDATE": 1, "DLC": 2}


def _package_entry_sort_key(entry) -> tuple:
    return (_PACKAGE_VARIANT_ORDER.get(entry.variant, 3), entry.relative_path)


def _sub_report_for_entry(report, entry):
    """A single-entry view of a multi-package archive's PreviewReport, as if
    `entry` had been the archive's only package -- reused as-is by
    queue_worker.create_job_from_report()/manifest.build_manifest_and_stage(),
    which only ever look at these singular fields, so every existing
    manifest/staging/security invariant applies identically per entry.
    package_entries is cleared on the copy so this sub-report never itself
    reads as "another multi-package archive"."""
    return dataclasses.replace(
        report,
        title_id=entry.title_id, title_id_confident=entry.title_id_confident,
        package_format=entry.package_format, package_relative_path=entry.relative_path,
        size=entry.size, package_entries=[],
    )


def _apply_library_row(report, row) -> None:
    _apply_sealed_forwarder(report, row)
    _apply_library_row_owner(report, row)


def _apply_sealed_forwarder(report, row) -> None:
    """A port's forwarder (sd_files.SEALED_FORWARDER): the package itself
    is known and is sent as it is -- only its TITLE_ID is not, and nothing
    about sending it to DBI needs one."""
    if (row["title_id_source"] == sd_files.SEALED_FORWARDER
            and report.content_type is ContentType.GAME_PACKAGE and report.package_relative_path is None):
        report.package_relative_path = Path(report.source).name
        report.destination = "SD install"
        report.mode = "INSTALL"


def _apply_library_row_owner(report, row) -> None:
    """A switch/ folder's own files carry no TITLE_ID; the game it belongs
    to is what the scan decided and what Library grouped it under
    (library_items.title_id, see sd_files.assign_owners). The job inherits
    exactly that, so it waits behind its game's install and is recorded
    under that game."""
    if report.content_type in (ContentType.SD_FILES, ContentType.AMIIBO):
        report.title_id = row["title_id"]
        report.title_id_confident = False


def _sd_sub_report(report):
    """The switch/ folder of a package or mod archive (see
    preview._attach_sd_part) as an SD_FILES report of its own, tagged with
    the game the archive installs. None when there is none."""
    if report.sd_source_dir is None or report.content_type not in (
        ContentType.GAME_PACKAGE, ContentType.ATMOSPHERE_MOD, ContentType.AMIIBO,
    ):
        return None
    family = report.title_id if report.content_type is ContentType.ATMOSPHERE_MOD         else family_base_title_id(report.title_id)
    return dataclasses.replace(
        report, content_type=ContentType.SD_FILES, title_id=family, title_id_confident=False,
        package_format=None, package_relative_path=None, package_entries=[], mod_source_dir=None,
        size=report.sd_summary.size if report.sd_summary else 0,
        file_count=report.sd_summary.files if report.sd_summary else None,
    )


def _apply_amiibo_selection(report, wanted) -> None:
    """Narrows an AMIIBO report to the amiibo the user picked (their
    destinations, or whole folders of them -- emuiibo.select). None keeps
    the whole collection. Raises ValueError when the pick names nothing in
    this collection: installing nothing must be said, not done silently."""
    if report.content_type is not ContentType.AMIIBO or wanted is None or report.amiibo_collection is None:
        return
    chosen = emuiibo.select(report.amiibo_collection, wanted)
    if not chosen:
        raise ValueError("none of the selected amiibo are in this collection")
    report.copy_plan = preview.amiibo_copy_plan(report.amiibo_collection, chosen)
    report.file_count = len(report.copy_plan)
    # The collection's switch/ folder (if any) belongs to the whole
    # collection, not to a few of its amiibo.
    report.sd_source_dir = None


def create_and_confirm_jobs(conn, library_item_ids: list[int], target_device_id: str, *, progress=None,
                            confirm: bool = True, amiibo_selection: Optional[dict] = None) -> dict:
    """The ONLY path that creates jobs from the Web UI (point 9/19/25): the
    caller (the bulk-install confirmation dialog) already gathered
    explicit human confirmation before this is ever called -- so every job
    is still confirmed within this same call (PENDING_CONFIRM -> CONFIRMED),
    matching "after Confirm Install, an immutable manifest/job is created"
    from the spec -- but only AFTER every selected item has been fully
    prepared (extracted + staged), never one at a time as each item finishes
    (EMERGENCY HOTFIX 2: item 1's job must never be CONFIRMED, and so
    visible to the worker, while item 4 is still being extracted). The
    background worker (see WebContext._worker_loop) picks confirmed jobs up
    independently, later -- this function never transfers anything itself.

    Preparation errors are reported per-item. Any error blocks transfer
    for the entire selection; no successful sibling is silently installed.

    EMERGENCY HOTFIX (multi-package archive): a selected library item whose
    archive contains more than one installable package (Base+Update+DLC,
    etc. -- see preview.PreviewReport.package_entries) gets ONE job PER
    entry here, in deterministic Base -> Update -> DLC order, all sharing
    this call's batch_id -- never just the first entry
    (extractor.classify_entries() used to silently keep only that one; this
    is the fix). A single-package archive or bare package file
    (package_entries empty/single) takes the original, unchanged one-job
    path.

    confirm=False stops one step short: every job is created and staged
    exactly as above but left PENDING_CONFIRM, i.e. invisible to the worker,
    for the caller to confirm later with db.confirm_job(). That is what lets
    web/preparation.py prepare the NEXT game while the current one is still
    transferring without ever letting it start early (see
    _install_sequentially's docstring). The "prepare everything before
    confirming anything" rule below is unchanged -- confirm=False simply
    hands that final step to the caller instead of doing it here.

    UI-002: one call to this function is one installation batch -- even
    when only a single item is selected. Every job successfully created
    here shares the same new `batch_id`; if NONE were created (every item
    failed), the just-created, now-unreferenced batch row is deleted
    rather than left behind as clutter (safe: nothing can reference it yet
    -- see db.create_installation_batch()'s docstring)."""
    if not target_device_id:
        raise ValueError("target_device_id is required")
    target_device_id = resolve_target_device_id(conn, target_device_id)

    from .. import config as config_mod
    from .preparation import preflight, remove_extraction
    library_item_ids = list(dict.fromkeys(library_item_ids))
    preflight_errors = preflight(conn, library_item_ids, progress)
    if preflight_errors:
        return {"created": [], "errors": preflight_errors, "batch_id": None}

    batch_id = db.create_installation_batch(conn, target_device_id=target_device_id)
    # Settings' conflict policy, read once for the whole batch: "override"
    # sends every job with overwrite=True from the start (a pre-existing
    # file it doesn't recognize is simply replaced, no FileAlreadyExistsError
    # ever raised); "skip" (default) leaves force_overwrite off, so
    # queue_worker.py's FileAlreadyExistsError handling is what applies and
    # resolves the job on its own -- no per-item prompt either way.
    force_overwrite = config_mod.load_conflict_policy() == "override"

    created = []
    errors = []
    for item_id in library_item_ids:
        error_count = len(errors)
        if progress:
            progress(item_id=item_id, phase="Extracting")
        row = db.get_library_item_by_id(conn, item_id)
        if row is None:
            errors.append({"library_item_id": item_id, "error": "library item not found"})
            continue
        if row["status"] not in ("AVAILABLE",):
            errors.append({
                "library_item_id": item_id,
                "error": f"not installable in its current state: {row['status']}",
            })
            continue

        try:
            report = preview.preview_path(Path(row["absolute_path"]), extract=True)
        except extractor.ExtractionBackendMissingError as exc:
            # RAR decompression needs an external unrar/7z/bsdtar on PATH
            # that this packaged build does not bundle (see
            # docs/PACKAGING.md's "RAR extraction" section) -- report
            # clearly per item rather than letting this propagate into an
            # unhandled 500. A .7z only gets here when py7zr cannot decode
            # its method and no external tool is there to; its own message
            # says exactly that.
            is_rar = Path(row["absolute_path"]).suffix.lower() == ".rar"
            errors.append({
                "library_item_id": item_id,
                "error": ("RAR extraction is unavailable on this machine (no unrar/7z/bsdtar found on PATH) "
                          "-- see Settings for details.") if is_rar else str(exc),
            })
            if progress:
                progress(item_id=item_id, phase="Failed", error=errors[-1]["error"])
            continue
        except (OSError, manifest_mod.ManifestError, extractor.ArchiveError, ValueError) as exc:
            errors.append({"library_item_id": item_id, "error": str(exc)})
            if progress:
                progress(item_id=item_id, phase="Failed", error=str(exc))
            continue

        _apply_library_row(report, row)
        try:
            _apply_amiibo_selection(report, (amiibo_selection or {}).get(str(item_id)))
        except ValueError as exc:
            errors.append({"library_item_id": item_id, "error": str(exc)})
            remove_extraction(report.work_dir)
            if progress:
                progress(item_id=item_id, phase="Failed", error=str(exc))
            continue

        if report.content_type is ContentType.GAME_PACKAGE and len(report.package_entries) > 1:
            entries = sorted(report.package_entries, key=_package_entry_sort_key)
            sub_reports = [(e.relative_path, _sub_report_for_entry(report, e)) for e in entries]
        else:
            sub_reports = [(None, report)]
        # A package or mod archive that also carries a switch/ folder gets
        # one more job for it, after its packages -- nothing the archive
        # ships may silently stay behind (see _sd_sub_report).
        sd_part = _sd_sub_report(report)
        if sd_part is not None:
            sub_reports.append(("switch/", sd_part))

        # Each entry gets its OWN try/except: one entry's manifest/staging
        # failure (e.g. a corrupt DLC payload) must not silently prevent
        # the sibling entries of the SAME archive from becoming jobs too --
        # "no installable entry may silently disappear" applies per entry,
        # not just per selected library item.
        #
        # EMERGENCY HOTFIX 2 (prepare-all-before-install): job rows are
        # created here (PENDING_CONFIRM, not yet visible to the worker) but
        # NOT confirmed yet -- confirming happens in a SEPARATE pass below,
        # only once every selected item has been fully prepared. Without
        # this, item 1's job could already be CONFIRMED (and picked up by
        # the worker, mid-transfer) while item 4 is still being extracted
        # in this same request -- exactly the "archive 1 installing while
        # archive 4 still extracting" bug this fixes.
        for label, sub_report in sub_reports:
            action = "INSTALL_VIA_DBI" if sub_report.content_type is ContentType.GAME_PACKAGE else "COPY_MERGE"
            try:
                job_id = queue_worker.create_job_from_report(
                    conn, sub_report, library_item_id=item_id, action=action,
                    target_device_id=target_device_id, batch_id=batch_id,
                    force_overwrite=force_overwrite,
                )
                created.append({"library_item_id": item_id, "job_id": job_id})
            except (OSError, manifest_mod.ManifestError, extractor.ArchiveError, ValueError) as exc:
                errors.append({
                    "library_item_id": item_id,
                    "error": f"{label}: {exc}" if label else str(exc),
                })

        remove_extraction(report.work_dir)
        if progress:
            progress(item_id=item_id, phase="Failed" if len(errors) > error_count else "Ready")

    # Only now, with every selected item fully prepared (extracted,
    # manifest built and staged), confirm every successfully-created job --
    # see the loop above's own comment.
    if errors:
        # None of these jobs has been exposed to the worker. Abandon this
        # failed preparation, retain its DB record, release owned payload.
        conn.execute("UPDATE jobs SET status='FAILED', abandoned=1, error=? WHERE batch_id=?",
                     ("Batch preparation failed; no installation started", batch_id))
        conn.commit()
        from ..work_cleanup import cleanup_batch_if_all_done
        cleanup_batch_if_all_done(conn, batch_id)
        created = []
    elif confirm:
        for c in created:
            db.confirm_job(conn, c["job_id"])

    # "No job created" means literally zero job ROWS exist under this
    # batch -- not merely zero successes. A manifest-build failure can
    # still leave a permanent FAILED job row behind (see
    # queue_worker.create_job_from_report()'s own docstring: that row is
    # kept as an honest record even though the exception it re-raises
    # means this item never reaches `created`). Deleting the batch despite
    # such a row still referencing it via `jobs.batch_id` would violate
    # the FK (PRAGMA foreign_keys=ON) and 500 the whole request -- this is
    # exactly the failure a live `switch-agent web --mock` run surfaced.
    if not db.list_jobs_by_batch(conn, batch_id):
        db.delete_installation_batch(conn, batch_id)
        batch_id = None

    return {"created": created, "errors": errors, "batch_id": batch_id}


# Statuses the Web UI's "Retry installation" action accepts. Broader than
# db.retry_job()'s own allow-list (INTERRUPTED/FAILED/DEVICE_UNAVAILABLE)
# because retry here NEVER reuses the old frozen manifest -- it always
# builds a fresh one from a fresh preview, so the old "SOURCE_CHANGED/
# DESTINATION_CONFLICT/BLOCKED_BY_DEPENDENCY need a brand new job, not a
# resume" restriction (see db.retry_job's own docstring) is no longer a
# reason to refuse; it's exactly what this function already does for
# every status.
_RETRYABLE_JOB_STATUSES = (
    "INTERRUPTED", "FAILED", "DEVICE_UNAVAILABLE",
    "SOURCE_CHANGED", "BLOCKED_BY_DEPENDENCY",
    # DESTINATION_CONFLICT is deliberately absent: it is already resolved
    # (Settings' conflict policy applies automatically), so there is
    # nothing left for a retry to do -- re-running it would just reach the
    # exact same conflict again under a "skip" policy, or never have
    # reached it at all under "override".
)


def retry_job(conn, job_id: int, *, force_overwrite: bool = False) -> dict:
    """The Web UI's "Retry installation" action. Batch-staged archives
    create a new attempt referencing the exact retained manifest/payload,
    verified against its frozen hash, without extracting again. Legacy
    per-job staging and direct sources use the fresh-preview path below.

    The direct-source retry path is deliberately NOT a
    resume. Creates a brand NEW job from a fresh preview/manifest of the
    same source and the same target device as the old job; the old job's
    row and its install_history entry are left completely untouched, a
    permanent record of that attempt (rule: never delete/rewrite History
    for convenience). This is NOT "safely continue an interrupted
    transfer" -- after an interruption the destination may be in an
    unknown state, and DBI's SD_INSTALL virtual node has no
    partial-transfer/resume semantics at the protocol level regardless, so
    a retry always means sending the whole file again, exactly like a
    first attempt (see docs/STATE.md's Quake II investigation).

    db.retry_job() (in-place resume via the frozen manifest +
    progress.json, built for Stage 4.1's multi-file atmosphere-mod partial
    delivery case) is untouched and still exists as a lower-level
    primitive -- this is a separate, additive mechanism specifically for
    this button, not a replacement.

    Re-validates everything a first install would: a fresh
    preview.preview_path() call (source exists/size/SHA-256 -- the same
    validation create_and_confirm_jobs() already does for a brand new
    job), the ORIGINAL target_device_id (immutable -- retry never targets
    a different device than the attempt it's retrying), and destination
    conflict detection happens the normal way once the worker actually
    picks the new job up.

    force_overwrite (W3-006 Override): internal-only, set by override_job()
    below -- callers reachable from the plain "Retry installation" button
    never pass this. Threaded straight to the new job's own
    db.create_job(force_overwrite=...); see that column's own comment.

    Returns {"old_job_id", "new_job_id"}. Raises ValueError (surfaced by
    the API as 409) if the job isn't in a retryable status, its source no
    longer exists/isn't installable, or it has no recognizable source at
    all."""
    old = db.get_job(conn, job_id)
    if old is None:
        raise ValueError(f"no such job: {job_id}")
    if old["status"] not in _RETRYABLE_JOB_STATUSES:
        raise ValueError(
            f"job {job_id} is '{old['status']}' -- not retryable "
            f"(retry applies to: {', '.join(_RETRYABLE_JOB_STATUSES)})"
        )

    if old["abandoned"]:
        raise ValueError("This attempt was abandoned; select the source again in Library")
    # A job can have at most one live retry at a time -- without this, a
    # DESTINATION_CONFLICT/FAILED card whose old row is still displayed
    # somewhere (e.g. a preparation-batch item, which looks its job up by
    # the id it recorded at creation and doesn't re-check _not_settled())
    # keeps its Override/Skip/Retry buttons fully functional even after
    # they've already been used once -- old["status"] never changes on
    # this row, so a second click passes every check above and creates
    # ANOTHER new job, unboundedly. _retry_staged_job() below already had
    # this guard; the direct-source path (library_item_id/inbox_item_id --
    # what a MOD_FOLDER retry/override uses) did not.
    if conn.execute("SELECT 1 FROM jobs WHERE retry_of_job_id=? AND abandoned=0", (job_id,)).fetchone():
        raise ValueError("This attempt already has a retry; use the latest attempt")
    # Archive retries use the EXACT frozen entry, not the archive's first
    # package. Keep a new attempt/history row without extracting again.
    if old["manifest_path"]:
        frozen = manifest_mod.load_manifest(job_id)
        if frozen.batch_id is not None and any(f.source_kind == "frozen" for f in frozen.files):
            return _retry_staged_job(conn, old, frozen, force_overwrite=force_overwrite)

    if old["library_item_id"] is not None:
        source_row = db.get_library_item_by_id(conn, old["library_item_id"])
        if source_row is None:
            raise ValueError("original source item no longer exists in the library")
        if source_row["status"] != "AVAILABLE":
            raise ValueError(f"source item is not currently installable: {source_row['status']}")
        source_path = Path(source_row["absolute_path"])
    elif old["inbox_item_id"] is not None:
        source_row = db.get_inbox_item_by_id(conn, old["inbox_item_id"])
        if source_row is None:
            raise ValueError("original source item no longer exists in inbox/")
        from .. import config as config_mod
        source_path = config_mod.INBOX_DIR / source_row["relative_path"]
    else:
        raise ValueError(f"job {job_id} has no recognizable source -- cannot retry")

    # UI-002: Retry is a new user action, not a continuation of the old
    # batch -- creates its own single-job batch rather than reusing or
    # retroactively touching the original job's batch_id (which, along
    # with the rest of that old job's row and its History entry, stays
    # exactly as it was).
    batch_id = db.create_installation_batch(conn, target_device_id=old["target_device_id"])

    try:
        report = preview.preview_path(source_path, extract=True)
        _apply_library_row(report, source_row)
        new_job_id = queue_worker.create_job_from_report(
            conn, report,
            library_item_id=old["library_item_id"], inbox_item_id=old["inbox_item_id"],
            action=old["action"], target_device_id=old["target_device_id"], batch_id=batch_id,
            force_overwrite=force_overwrite, retry_of_job_id=job_id,
        )
    except (FileNotFoundError, manifest_mod.ManifestError) as exc:
        # Same "only delete if truly nothing references it" rule as
        # create_and_confirm_jobs() above -- a ManifestError can still
        # leave a FAILED job row behind, already pointing at this batch_id.
        if not db.list_jobs_by_batch(conn, batch_id):
            db.delete_installation_batch(conn, batch_id)
        raise ValueError(f"could not re-validate source for retry: {exc}") from exc

    carried = manifest_mod.inherit_progress(job_id, new_job_id, in_flight=_in_flight_file(old))
    db.confirm_job(conn, new_job_id)
    db.log_job_event(conn, new_job_id, f"retry of job {job_id} (was {old['status']})"
                     + (f", continuing after {carried} file(s) it had already delivered" if carried else ""))
    db.log_job_event(conn, job_id, f"retried as new job {new_job_id}")
    return {"old_job_id": job_id, "new_job_id": new_job_id, "batch_id": batch_id}


def _in_flight_file(old) -> Optional[str]:
    """The file `old` was sending when it stopped, if it stopped mid-file.
    Only a transfer that broke off -- a failed send, a lost device -- can
    have left one half written; a job that ended any other way has nothing
    of its own to replace."""
    if old["status"] not in ("FAILED", "INTERRUPTED", "DEVICE_UNAVAILABLE"):
        return None
    return old["current_file"]


def _retry_staged_job(conn, old, frozen, *, force_overwrite: bool = False) -> dict:
    import json
    mismatch = manifest_mod.verify_manifest_against_source(frozen, old["id"])
    if mismatch:
        raise ValueError(f"Prepared source {mismatch.kind}: {mismatch.dest_relative_path}; select Library source again")
    # The "already has a live retry" guard now lives in retry_job() itself
    # (the only caller of this function) so it applies uniformly to both
    # the staged and direct-source paths -- see its own comment.
    batch_id = db.create_installation_batch(conn, target_device_id=old["target_device_id"])
    new_id = db.create_job(conn, action=old["action"], target_storage=old["target_storage"],
                           target_device_id=old["target_device_id"], library_item_id=old["library_item_id"],
                           inbox_item_id=old["inbox_item_id"], batch_id=batch_id,
                           force_overwrite=force_overwrite)
    try:
        manifest_mod.job_work_dir(new_id).mkdir(parents=True, exist_ok=True)
        path = manifest_mod.manifest_path_for(new_id)
        path.write_text(json.dumps(frozen.to_dict()), encoding="utf-8")
        conn.execute("UPDATE jobs SET manifest_path=?, retry_of_job_id=?, payload_batch_id=? WHERE id=?",
                     (str(path), old["id"], frozen.batch_id, new_id))
        conn.commit()
        carried = manifest_mod.inherit_progress(old["id"], new_id, in_flight=_in_flight_file(old))
        if carried:
            db.log_job_event(conn, new_id, f"continuing after {carried} file(s) job {old['id']} had already delivered")
        db.confirm_job(conn, new_id)
    except Exception:
        db.update_job_status(conn, new_id, "FAILED", error="Could not create retry manifest")
        raise
    db.log_job_event(conn, new_id, f"retry of job {old['id']} using retained payload")
    return {"old_job_id": old["id"], "new_job_id": new_id, "batch_id": batch_id}


def abandon_all_jobs_for_device(conn, device_id: str, reason: str) -> list[int]:
    """By explicit request: a device's connection-state change, in
    EITHER direction (disconnect OR a fresh connect -- including the
    very first refresh_devices() tick after app startup, which looks
    identical to a fresh connect), invalidates whatever was queued
    against its previous session. Every one of this device's jobs that
    hasn't reached DONE/DONE_UNVERIFIED yet is abandoned -- no attempt
    to judge case by case whether a given job "should" survive; nothing
    does except the permanent History record, exactly as cancel_job()
    already treats a single abandoned attempt.

    Unlike cancel_job(), this also covers RUNNING (and any other
    non-terminal status) -- cancel_job() refuses those because a live
    transfer can't be atomically stopped, but that reasoning doesn't
    apply here: the device is already gone (or was never confirmed
    reachable this session) by the time this runs, so there is nothing
    left to stop.

    _process_job() (queue_worker.py) is otherwise the ONLY writer of
    install_history, and only ever runs once per job the first time it
    reaches a recorded outcome -- a job already in one of those states
    (DESTINATION_CONFLICT, FAILED, ...) already has its entry. A job
    that never got that far (PENDING_CONFIRM/CONFIRMED/RUNNING/
    WAITING_FOR_DEVICE/VERIFYING) never will now that this function is
    abandoning it out from under _process_job -- so this records it
    directly, the same shape _process_job itself would have."""
    from ..work_cleanup import cleanup_batch_if_all_done

    _ALREADY_RECORDED_STATUSES = (
        "DESTINATION_CONFLICT", "FAILED", "INTERRUPTED", "SOURCE_CHANGED",
        "DEVICE_UNAVAILABLE", "BLOCKED_BY_DEPENDENCY", "WAITING_FOR_BASE",
    )
    rows = conn.execute(
        "SELECT * FROM jobs WHERE target_device_id=? AND status NOT IN ('DONE','DONE_UNVERIFIED') AND abandoned=0",
        (device_id,),
    ).fetchall()
    abandoned_ids = []
    for row in rows:
        job_id = row["id"]
        needs_history = row["status"] not in _ALREADY_RECORDED_STATUSES
        db.update_job_status(conn, job_id, "FAILED", error=reason, finished_at=db.now_iso())
        conn.execute("UPDATE jobs SET abandoned=1 WHERE id=?", (job_id,))
        conn.commit()
        db.log_job_event(conn, job_id, reason)
        if needs_history:
            try:
                frozen = manifest_mod.load_manifest(job_id)
                title_id, bytes_total = frozen.title_id, sum(f.size for f in frozen.files)
            except (FileNotFoundError, ValueError, KeyError, TypeError):
                title_id, bytes_total = None, None
            db.record_install_history(
                conn, job_id=job_id, title_id=title_id,
                display_name=queue_worker.display_name_for_job(conn, row),
                target_device_id=device_id, target_storage=row["target_storage"],
                outcome="FAILED", error=reason, bytes_total=bytes_total,
            )
        cleanup_batch_if_all_done(conn, row["batch_id"])
        if row["payload_batch_id"]:
            cleanup_batch_if_all_done(conn, row["payload_batch_id"])
        abandoned_ids.append(job_id)
    return abandoned_ids


class RemoveRefused(Exception):
    """remove_from_queue() found a part already being sent."""


def remove_from_queue(conn, ctx: WebContext, job_ids, library_item_ids) -> dict:
    """Queue page "Remove from queue" on a game's card: every part of it
    that has not started sending goes -- its jobs are cancelled (the same
    cancel as a row's own, see cancel_job()), and preparation items still on
    their way (waiting, unpacking, prepared) are dropped from their run (see
    PreparationQueue.remove_items()). Refused as a whole, before anything
    changes, while a part is already sending: that is Abort's to stop,
    at a point where it can be stopped cleanly."""
    rows = [row for job_id in job_ids if (row := db.get_job(conn, job_id)) is not None]
    if any(row["status"] in ("RUNNING", "VERIFYING") for row in rows):
        raise RemoveRefused("Part of this game is being sent right now -- use Abort to stop it.")
    cancelled = []
    for row in rows:
        if row["abandoned"] or row["status"] not in _CANCELABLE_JOB_STATUSES:
            continue
        cancel_job(conn, row["id"])
        cancelled.append(row["id"])
    removed_items = ctx.preparations.remove_items(library_item_ids)
    return {"cancelled_job_ids": cancelled, "removed_item_ids": removed_items}


def cancel_job(conn, job_id: int) -> None:
    row = db.get_job(conn, job_id)
    if row is None:
        raise ValueError(f"no such job: {job_id}")
    if row["status"] not in _CANCELABLE_JOB_STATUSES:
        raise ValueError(
            f"job {job_id} is '{row['status']}' -- cannot cancel (only queued-but-not-running jobs can be); "
            "a RUNNING transfer cannot be atomically stopped, see docs/WEB-UI.md"
        )
    db.update_job_status(conn, job_id, "FAILED", error="cancelled by user", finished_at=db.now_iso())
    conn.execute("UPDATE jobs SET abandoned=1 WHERE id=?", (job_id,))
    conn.commit()
    db.log_job_event(conn, job_id, "cancelled by user before it started running")
    from ..work_cleanup import cleanup_batch_if_all_done
    cleanup_batch_if_all_done(conn, row["payload_batch_id"] or row["batch_id"])


# What the Queue page's "Abort" cancels: every job that would still start on
# its own -- a subset of _CANCELABLE_JOB_STATUSES. The rest of that set
# (FAILED, INTERRUPTED, DEVICE_UNAVAILABLE, SOURCE_CHANGED,
# BLOCKED_BY_DEPENDENCY) has already stopped and only moves again if the
# user clicks Retry on it; Abort leaves those rows, and their Retry, alone.
_ABORT_CANCELS_JOB_STATUSES = (
    "PENDING_CONFIRM", "CONFIRMED", "WAITING_FOR_BASE", "WAITING_FOR_DEVICE",
)

ABORT_CANCEL_ERROR = "cancelled by user (Abort)"


def _abortable_rows(conn) -> list:
    placeholders = ",".join("?" * len(_ABORT_CANCELS_JOB_STATUSES))
    return conn.execute(
        f"SELECT * FROM jobs WHERE abandoned=0 AND status IN ({placeholders}) ORDER BY id",
        _ABORT_CANCELS_JOB_STATUSES,
    ).fetchall()


def _running_job_ids(conn) -> list[int]:
    return [row["id"] for row in conn.execute("SELECT id FROM jobs WHERE status='RUNNING' ORDER BY id")]


def abort_all(conn, ctx: WebContext) -> dict:
    """The Queue page's "Abort": stop everything, now.

    In this order, each step closing a door the next one relies on:
      1. preparation runs stop creating jobs (PreparationQueue.abort_all --
         once it returns, none of their jobs can still be confirmed);
      2. every job that would still start on its own is cancelled exactly
         as a per-row Cancel would (FAILED, abandoned, staging released);
      3. the transfer in flight, if any, is asked to stop at its next safe
         point (WebContext.request_abort -- see queue_worker.
         _run_job_transfer for what "safe" means and how soon that is).

    Never pauses the worker: once the running job has stopped, the next
    install the user starts runs normally. Returns what was done, including
    the id of a job that is still stopping -- the caller must not claim it
    has already stopped."""
    from ..work_cleanup import cleanup_batch_if_all_done

    preparations_stopped = ctx.preparations.abort_all()
    cancelled = []
    batches = set()
    for row in _abortable_rows(conn):
        db.update_job_status(conn, row["id"], "FAILED", error=ABORT_CANCEL_ERROR, finished_at=db.now_iso())
        conn.execute("UPDATE jobs SET abandoned=1 WHERE id=?", (row["id"],))
        conn.commit()
        db.log_job_event(conn, row["id"], "cancelled by Abort before it started running")
        cancelled.append(row["id"])
        batches.add(row["payload_batch_id"] or row["batch_id"])
    for batch_id in batches:
        cleanup_batch_if_all_done(conn, batch_id)
    running = _running_job_ids(conn)
    ctx.request_abort()
    return {
        "cancelled_job_ids": cancelled,
        "preparations_stopped": preparations_stopped,
        "stopping_job_ids": running,
    }


def abort_status(conn, ctx: WebContext) -> dict:
    """What the Queue page needs to draw its worker controls: whether Abort
    has anything to act on (a transfer running, a job that would still
    start, a preparation run still creating jobs), and whether an earlier
    Abort is still waiting for the running transfer to stop."""
    queued = len(_abortable_rows(conn))
    running = _running_job_ids(conn)
    preparing = ctx.preparations.has_running()
    stopping_seconds = ctx.abort_stopping_seconds
    return {
        "worker_paused": ctx.worker_paused.is_set(),
        "queued": queued,
        "running_job_ids": running,
        "preparing": preparing,
        "abortable": bool(queued or running or preparing),
        "stopping": stopping_seconds is not None and bool(running),
        "stopping_seconds": stopping_seconds if running else None,
    }


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# History as a JOURNAL. Deliberately read-only: nothing on that page changes
# anything, by explicit product decision -- see CLAUDE.md.
#
# It is the SAME list Queue shows, after the fact: one row per transfer, in
# the order they happened, carrying the same Game/Update/DLC/Mod badge and a
# marker of how each one ended. Deliberately NOT aggregated per title -- how
# many times something was sent is not the question; what was sent, in what
# order, is, and that stays true when the same thing was sent twice.
# ---------------------------------------------------------------------------

_STORAGE_LABELS = {"SD_CARD": "SD card", "SD_INSTALL": "SD install"}

# The SAME claims as _ACTIVITY_LABELS, said shorter. Not a second vocabulary:
# the kinds are still classify_activity()'s and nothing here states more than
# the long form does. History can afford the short one because it explains the
# caveat once, in its own footnote, instead of on every row -- where the long
# form wrapped to two lines and buried the name it belonged to.
_ACTIVITY_LABELS_SHORT = {
    "TRANSPORT_VERIFIED": "Installed",
    "CONSOLE_CONFIRMED_SUCCESS": "Installed",
    "CONSOLE_CONFIRMED_FAILED": "Did not work",
    "TRANSPORT_UNVERIFIED": "Sent",
    "FAILED": "Failed",
    "INTERRUPTED": "Interrupted",
    "DESTINATION_CONFLICT": "Blocked",
    "SOURCE_CHANGED": "Skipped",
}

# Which colour a row flies. "soft" is the honest middle: the Switch took the
# bytes and DBI's result is unknowable over MTP, so it is neither a success
# claim nor a warning.
_ACTIVITY_FLAGS = {
    "TRANSPORT_VERIFIED": "ok",
    "CONSOLE_CONFIRMED_SUCCESS": "ok",
    "TRANSPORT_UNVERIFIED": "soft",
    "CONSOLE_CONFIRMED_FAILED": "failed",
    "FAILED": "failed",
    "INTERRUPTED": "failed",
    "SOURCE_CHANGED": "failed",
    "DESTINATION_CONFLICT": "conflict",
}

_ROLE_LABELS = {"base": "Game", "update": "Update", "dlc": "DLC", "mod": "Mod", "sd": "SD files",
                "amiibo": "Amiibo", "emuiibo": "emuiibo", "addon": "Add-on"}


def _local(created_at: str) -> Optional[datetime]:
    """UTC ISO out of the database -> this machine's local time. History used
    to print the stored string as-is, so every time on the page was wrong by
    the viewer's own UTC offset -- three hours, for the person this was
    built for."""
    try:
        parsed = datetime.fromisoformat(created_at)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone()


def _day_label(moment: datetime, today) -> str:
    days = (today - moment.date()).days
    if days == 0:
        return "Today"
    if days == 1:
        return "Yesterday"
    return moment.strftime("%d %b").lstrip("0")


def history_title_name(display_name: str) -> str:
    """The same name Library shows, reached the same way: drop the extension
    (Library's names come from Path.stem) then the bracketed release tags
    (the global `game_name` filter). History rendered the raw filename
    instead, so one object read as two different things depending on which
    page you were on. Deliberately NOT cleaner than Library -- a "(0.41 GB)"
    the two pages disagree about would be the same bug again. A mod's
    " -- Mod" suffix is dropped here because the row carries a [Mod] badge,
    exactly as Queue does."""
    from .. import config as config_mod

    base = display_name.partition(" \u2014 ")[0]
    stem = Path(base).stem if Path(base).suffix.lower() in config_mod.ALL_TRACKED_EXTENSIONS else base
    return title_id_mod.strip_release_tags(stem)


def _history_role(conn, row) -> Optional[str]:
    """The same Game/Update/DLC/Mod badge Queue puts on the very same item.
    Reads the job's frozen manifest first (_job_variant_role -- identical
    answer to Queue's by construction), and falls back to the title_id the
    history row stored itself: manifests are cleaned up when a batch
    finishes, and history outlives them by design."""
    job_row = db.get_job(conn, row["job_id"])
    if job_row is not None:
        role = _job_variant_role(job_row)
        if role is not None:
            return role
    if " \u2014 Mod" in (row["display_name"] or ""):
        return "mod"
    if " \u2014 SD files" in (row["display_name"] or ""):
        return "sd"
    if " \u2014 Amiibo" in (row["display_name"] or ""):
        return "amiibo"
    if not row["title_id"]:
        return None
    try:
        variant, _base = title_id_mod.classify_title_variant(row["title_id"])
    except (ValueError, TypeError):
        return None
    return _TITLE_VARIANT_TO_ROLE.get(variant)


def list_history_entries(conn, *, limit: int = 200) -> dict:
    """The History page's whole payload: one entry per transfer, newest
    first. No aggregation and no summary -- the page is a list of what was
    sent, in the order it was sent, and says nothing it did not observe."""
    entries = list_history(conn, limit=limit)
    unsettled = _unsettled_job_ids(conn)
    today = datetime.now().astimezone().date()

    rows = []
    previous_day = None
    for entry in entries:
        activity = classify_activity(entry)
        moment = _local(entry["created_at"])
        day = _day_label(moment, today) if moment else ""
        role = _history_role(conn, entry)
        rows.append({
            "id": entry["id"],
            "name": history_title_name(entry["display_name"]),
            "raw_name": entry["display_name"],
            "role": role,
            "role_label": _ROLE_LABELS.get(role),
            "kind": activity["kind"],
            "label": _ACTIVITY_LABELS_SHORT.get(activity["kind"], activity["label"]),
            "long_label": activity["label"],
            "flag": _ACTIVITY_FLAGS.get(activity["kind"], "soft"),
            "device_label": entry["target_device_label"],
            "storage_label": _STORAGE_LABELS.get(entry["target_storage"], entry["target_storage"]),
            "bytes_total": entry["bytes_total"],
            "error": entry["error"],
            "day_label": day,
            # Only the first row of each day carries its heading, so a run of
            # transfers from one sitting reads as one block.
            "day_heading": day if day != previous_day else None,
            "time_label": moment.strftime("%H:%M") if moment else "",
            "in_queue": entry["job_id"] in unsettled,
        })
        previous_day = day

    return {"rows": rows, "truncated": len(entries) >= limit, "limit": limit}


def _unsettled_job_ids(conn) -> set:
    """job_ids still awaiting a decision in Queue. Drives the one text link a
    journal is allowed: an address for a stuck row, never a control."""
    all_rows = db.list_jobs(conn)
    return {r["id"] for r in all_rows if _not_settled(all_rows, r)}


def list_history(conn, *, limit: int = 200) -> list[dict]:
    result = []
    for row in db.list_install_history(conn, limit=limit):
        entry = dict(row)
        entry["target_device_label"] = device_label(conn, row["target_device_id"])
        result.append(entry)
    return result


def list_history_grouped(conn, *, limit: int = 200) -> list[dict]:
    """UI-002: list_history()'s entries grouped by the batch their
    originating job belonged to. A batch_id=NULL entry (pre-UI-002
    history, or anything not created through create_and_confirm_jobs()/
    retry_job()) renders as its own single-entry group, same as today.
    Newest group first, ordered by that group's most recent entry --
    matches list_install_history()'s own `ORDER BY created_at DESC`."""
    entries = list_history(conn, limit=limit)
    by_batch: dict[Optional[int], list[dict]] = {}
    for entry in entries:
        job_row = db.get_job(conn, entry["job_id"])
        batch_id = job_row["batch_id"] if job_row is not None else None
        by_batch.setdefault(batch_id, []).append(entry)

    groups = []
    for entry in by_batch.pop(None, []):
        groups.append({"batch_id": None, "entries": [entry], "created_at": entry["created_at"]})
    for batch_id, group_entries in by_batch.items():
        groups.append({
            "batch_id": batch_id,
            "entries": group_entries,
            "created_at": max(e["created_at"] for e in group_entries),
        })

    groups.sort(key=lambda g: g["created_at"], reverse=True)
    return groups


# UI-003: the user's own confirmation of DBI's on-console result for a
# DONE_UNVERIFIED transport outcome. Restricted here, in the service layer,
# to rows whose TRANSPORT outcome is actually DONE_UNVERIFIED --
# db.set_user_verified_outcome() itself is a plain, reusable persistence
# primitive with no opinion on that restriction (see its own docstring),
# matching this project's existing split between "business rule" (services.py)
# and "storage primitive" (db.py).
def set_history_verification(conn, history_id: int, outcome: Optional[str]) -> dict:
    row = db.get_install_history_by_id(conn, history_id)
    if row is None:
        raise ValueError(f"no such history entry: {history_id}")
    if row["outcome"] != "DONE_UNVERIFIED":
        raise ValueError(
            f"history entry {history_id} has transport outcome '{row['outcome']}' -- "
            "user verification only applies to DONE_UNVERIFIED entries"
        )
    db.set_user_verified_outcome(conn, history_id, outcome)
    updated = db.get_install_history_by_id(conn, history_id)
    return {
        "history_id": history_id,
        "outcome": updated["outcome"],
        "user_verified_outcome": updated["user_verified_outcome"],
        "user_verified_at": updated["user_verified_at"],
    }


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def get_settings(conn, ctx: WebContext) -> dict:
    from .. import config as config_mod
    import socket
    from .network import is_home_address

    try:
        lan_addresses = sorted({item[4][0] for item in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
                                if is_home_address(item[4][0]) and not item[4][0].startswith("127.")})
    except OSError:
        lan_addresses = []

    limits = config_mod.load_extraction_limits(config_mod.CONFIG_YAML_PATH)
    library_info = config_mod.library_dir_info(config_mod.CONFIG_YAML_PATH)
    return {
        "library_dir": str(config_mod.LIBRARY_DIR),
        "library_dirs": [str(p) for p in config_mod.library_dirs()],
        "conflict_policy": config_mod.load_conflict_policy(),
        "lan_addresses": lan_addresses,
        "library_dir_configured": library_info.configured,
        "library_dir_exists": library_info.exists,
        "worker_paused": ctx.worker_paused.is_set(),
        "worker_running": ctx.worker_running,
        "worker_last_heartbeat_at": ctx.worker_last_heartbeat_at,
        "worker_restart_pending": ctx.is_worker_restart_pending,
        "extraction_limits": {
            "max_extracted_size_bytes": limits.max_extracted_size_bytes,
            "max_file_count": limits.max_file_count,
            "max_directory_depth": limits.max_directory_depth,
        },
        "app_version": _app_version(),
        "runtime_mode": config_mod.RUNTIME_MODE,
        "rar_extraction_available": extractor.rar_backend_available(),
    }


def set_conflict_policy(policy: str) -> dict:
    """"skip" or "override" -- see config.load_conflict_policy(). Applied
    to every job created from here on (create_and_confirm_jobs reads it at
    creation time); jobs already in flight keep whatever was in effect
    when they were created."""
    from .. import config as config_mod

    config_mod.set_conflict_policy(policy)
    return {"conflict_policy": policy}


def _validate_library_dir_candidate(raw_path: str) -> Path:
    """W3-002: shared by validate-only and save. Never a Windows COM
    folder picker (explicitly out of scope -- this is a plain, validated
    text-path input, safe to call from an HTTP request thread since it
    only touches the local filesystem, never COM). Raises ValueError with
    a human-readable reason on any failure; never guesses/auto-corrects a
    bad path."""
    import os

    raw = (raw_path or "").strip()
    if not raw:
        raise ValueError("a folder path is required")

    candidate = Path(raw).expanduser()
    try:
        candidate = candidate.resolve()
    except OSError as exc:
        raise ValueError(f"could not resolve this path: {exc}") from exc

    if not candidate.exists():
        raise ValueError(f"'{candidate}' does not exist")
    if not candidate.is_dir():
        raise ValueError(f"'{candidate}' is not a directory")
    if not os.access(candidate, os.R_OK):
        raise ValueError(f"'{candidate}' is not readable")
    return candidate


def validate_library_dir(raw_path: str) -> dict:
    """W3-002: read-only check (exists / is a directory / readable /
    canonical path) -- no persistence, no watcher/rescan side effects.
    Powers the Settings page's "Validate" button, distinct from "Save"."""
    try:
        candidate = _validate_library_dir_candidate(raw_path)
    except ValueError as exc:
        return {"valid": False, "reason": str(exc), "canonical_path": None}
    return {"valid": True, "reason": None, "canonical_path": str(candidate)}


def set_library_dir(conn, ctx: WebContext, raw_path: str, raw_paths: list[str] | None = None) -> dict:
    """W3-002: validates, persists into config.yaml (in place, preserving
    every other line -- see config.set_library_source_dir()'s own
    docstring), and takes effect immediately: updates the live
    config.LIBRARY_DIR module attribute, restarts the filesystem watcher
    so it observes the new directory instead of the old one, and triggers
    a background rescan (the exact existing run_scan_in_background() the
    manual Rescan button already uses -- never a second scan mechanism,
    and "scanning never creates a job" already holds here for free).
    Raises ValueError (surfaced by the caller as a 4xx, never a 500) on
    any validation failure -- the old config/watcher state is left
    completely untouched in that case. Validation happens BEFORE the
    running scan is cancelled, so a rejected path costs nothing.

    An empty `raw_paths` removes every folder -- allowed, and the scan it
    triggers is what clears the now-orphaned index. `raw_paths=None` with
    an empty `raw_path` is still an error ("a folder path is required"):
    "I chose nothing" and "I sent nothing" are different requests."""
    from .. import config as config_mod

    # `paths: []` is a real request -- "remove my last Library folder" --
    # and used to be refused outright ("At least one folder is required"),
    # which left no way to stop SwitchAgent looking at a folder without
    # first finding some other folder to offer it. An empty list is now a
    # legitimate configured state; see config.library_dirs() for how it is
    # told apart from a config that simply never named one.
    candidates = list(dict.fromkeys(
        _validate_library_dir_candidate(p)
        for p in (raw_paths if raw_paths is not None else [raw_path])
    ))

    # A running scan used to make this fail outright ("Wait for the current
    # scan to finish, then save the folders again"), which turned the one
    # thing a user needs in order to STOP an unwanted scan into the one
    # thing they could not do while it ran -- and, because every save below
    # kicks off a scan of its own, adding a second folder immediately
    # re-armed that refusal against removing the first. Nothing about the
    # old scan is worth protecting here: it is walking folders the user is
    # in the middle of replacing. Stop it and wait for it to notice, so the
    # rescan started below is the only one running against the new list.
    ctx.cancel_scan(wait_seconds=10.0)

    config_mod.set_library_source_dirs(candidates)
    if candidates:
        config_mod.LIBRARY_DIR = candidates[0]

    ctx.stop_library_watcher()  # safe no-op if it wasn't running yet
    # A no-op while no folder is configured -- which, after removing the
    # last one, is exactly the point: nothing left to watch.
    ctx.start_library_watcher()  # re-reads config.LIBRARY_DIR (just updated above)
    # Still a scan even with no folders left: that pass is what retires the
    # rows for files that are no longer part of the collection (see
    # scan_library_once's own zero-folder branch).
    ctx.run_scan_in_background()

    return get_settings(conn, ctx)


def _app_version() -> str:
    from .. import __version__

    return __version__


# ---------------------------------------------------------------------------
# W3-008: update availability notification (never an auto-updater -- see
# switchagent/update_check.py's own module docstring for the full privacy/
# safety scope).
# ---------------------------------------------------------------------------

def get_update_check_status(*, force: bool = False) -> dict:
    from .. import config as config_mod
    from .. import update_check

    result = update_check.check_for_update(
        _app_version(), app_data_root=config_mod.APP_DATA_ROOT, force=force,
    )
    return result.to_dict()


# ---------------------------------------------------------------------------
# Work directory cleanup (UI-004) -- thin wrappers, all real logic lives in
# switchagent/work_cleanup.py (see its own module docstring for the safety
# model: only DONE/DONE_UNVERIFIED jobs past a retention window, manifest/
# progress.json always kept, no automatic background task).
# ---------------------------------------------------------------------------

def get_work_cleanup_preview(conn) -> dict:
    from .. import work_cleanup

    return work_cleanup.preview_cleanup(conn)


def execute_work_cleanup(conn) -> dict:
    from .. import work_cleanup

    return work_cleanup.execute_cleanup(conn)


# ---------------------------------------------------------------------------
# Diagnostics (UI-001 / UI-008) -- HTTP-safe: builds every input from the
# DB, the filesystem, and WebContext's already-cached state. Never makes a
# live COM call on the request thread (see diagnostics.py's module
# docstring and WebContext's device_cache/heartbeat notes).
# ---------------------------------------------------------------------------

def get_diagnostics_report(conn, ctx: WebContext):
    from .. import config as config_mod
    from .. import diagnostics
    from ..mtp.windows import device_fingerprint

    library_info = config_mod.library_dir_info(config_mod.CONFIG_YAML_PATH)
    known_devices = db.list_devices(conn)
    connected_ids = {info.device_id for info in ctx.get_known_devices()}

    return diagnostics.build_report(
        version=_app_version(),
        runtime_mode=config_mod.RUNTIME_MODE,
        resource_root=config_mod.PROJECT_ROOT,
        app_data_root=config_mod.APP_DATA_ROOT,
        config_path=config_mod.CONFIG_YAML_PATH,
        db_path=ctx.db_path,
        library_dir=config_mod.LIBRARY_DIR,
        library_dir_configured=library_info.configured,
        work_dir=config_mod.WORK_DIR,
        rar_backend_available=extractor.rar_backend_available(),
        watcher_running=ctx.library_watcher_running,
        worker_running=ctx.worker_running,
        worker_paused=ctx.worker_paused.is_set(),
        worker_last_heartbeat_at=ctx.worker_last_heartbeat_at,
        probe_com=False,  # HTTP request thread -- never a live COM call here
        known_device_count=len(known_devices),
        connected_device_count=len(connected_ids),
        device_fingerprints=[device_fingerprint(row["device_id"]) for row in known_devices],
    )


def recent_job_errors(conn, *, limit: int = 10, target_device_id: Optional[str] = None) -> list[dict]:
    """UI-008 / FIX-001: most recent jobs that recorded an error, newest
    first. Builds an EXPLICIT allowlist of fields safe to paste into a
    public bug report -- never reuses _job_view()'s full dict via `**row`
    or similar, because that dict's own `target_device_id` field is the
    device's raw, serial-bearing identity string (fine as an internal API
    payload field elsewhere, e.g. /api/queue, per ARCH-001's established
    "attribute value/API payload, never visible text" carve-out -- but a
    diagnostics EXPORT is explicitly meant to be shareable externally,
    which that carve-out never covered). This was a real, confirmed
    regression (independent audit, 2026-09-12): recent_job_errors()
    previously returned _job_view(conn, r) unchanged, and
    diagnostics.build_export_dict()'s `{**e, ...}` spread carried the raw
    target_device_id straight into GET /api/diagnostics/export's JSON
    body. Only device_label() (friendly name / last-seen name / safe
    fingerprint stand-in, see its own docstring) is exported here --
    never row["target_device_id"] itself, under any key.

    W3-004: `target_device_id` narrows this to one device's own errors, for
    the Device Details page's "Export diagnostics for this device" action.
    It is a FILTER INPUT only -- the raw id still never appears anywhere in
    the returned dicts, exactly as above."""
    rows = [r for r in db.list_jobs(conn) if r["error"]]
    if target_device_id is not None:
        rows = [r for r in rows if r["target_device_id"] == target_device_id]
    rows.sort(key=lambda r: r["created_at"], reverse=True)
    return [
        {
            "id": row["id"],
            "display_name": queue_worker.display_name_for_job(conn, row),
            "status": row["status"],
            "target_device_label": device_label(conn, row["target_device_id"]),
            "error": row["error"],
            "created_at": row["created_at"],
        }
        for row in rows[:limit]
    ]


def get_diagnostics_export(conn, ctx: WebContext) -> dict:
    """UI-008: the full export payload -- UI-001's DiagnosticsReport plus
    recent job errors and (if present) a sanitized application log tail.
    Built the same HTTP-safe way as get_diagnostics_report() -- no live
    COM call from this thread, and never a second diagnostics engine (see
    diagnostics.py's module docstring)."""
    from .. import config as config_mod
    from .. import diagnostics

    report = get_diagnostics_report(conn, ctx)
    log_tail = diagnostics.read_sanitized_log_tail(config_mod.LOGS_DIR / "switchagent.log")
    return diagnostics.build_export_dict(report, recent_job_errors=recent_job_errors(conn), log_tail=log_tail)
