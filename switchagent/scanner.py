"""Inbox scanner: classifies files/mod-folders under inbox/.

Read-only with respect to inbox/ contents. Archives are only peeked via
their own directory listing (extractor.list_archive_entries) -- nothing is
ever extracted here (extraction into work/<job-id>/ only happens through
extractor.safe_extract, invoked explicitly, e.g. from the `preview`
CLI command). Nothing in this module writes to the Switch.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Callable, Optional

from . import addons, config, db, emuiibo, extractor, nro, sd_files, title_id
from .model import ContentType

TITLE_ID_DIR_RE = re.compile(rf"^{config.TITLE_ID_RE}$")

# Which version of classify_file() indexed a row, stored in its details_json.
# Bump it whenever classification learns to recognise something it used to
# miss: a file whose size and mtime have not changed is otherwise never
# looked at again, so without this an archive indexed as UNKNOWN before
# switch/ folders were understood would stay a NEEDS REVIEW card called
# "switch" for good -- on exactly the libraries the change was made for.
#   1  (no marker) -- before SD_FILES
#   2  switch/ folders, forwarder launch paths
#   3  archives holding a loose .nro
#   4  emuiibo releases and virtual amiibo; an .nsp inside atmosphere/contents/
#      (a sysmodule's exefs.nsp) is no longer taken for an installable package
#   5  Add-ons catalog releases (switchagent/addons.py): Ultrahand's
#      sdout.zip, SaltyNX, Fizeau, a catalog overlay or app
CLASSIFICATION_REVISION = 5

# A library item that is a program for this PC, not something for a Switch
# (emuiibo's emutool / emuiigen): shown for what it is, never installable,
# and not a question left open the way NEEDS_REVIEW is.
NOT_FOR_SWITCH = "NOT_FOR_SWITCH"


def _details_json(*, extra: Optional[dict] = None, **parts) -> str:
    return json.dumps(
        {"classified": CLASSIFICATION_REVISION, **sd_files.details(**parts), **(extra or {})},
        ensure_ascii=False,
    )


def emuiibo_details(plan: emuiibo.ReleasePlan, version: Optional[str]) -> dict:
    return {"emuiibo": {**plan.to_dict(), "version": version}}


def amiibo_details(collection: emuiibo.Collection) -> dict:
    return {"amiibo": collection.summary()}


def addon_details(match: addons.ReleaseMatch, version: Optional[str]) -> dict:
    return {"addon": match.to_dict(version)}


# A Tesla overlay. Indexed only when it is an Add-ons catalog utility's
# (addons.match_program): any other .ovl has nowhere SwitchAgent knows to put it.
OVL_EXTENSION = ".ovl"


def _addon_program(path: Path) -> Optional[addons.ReleaseMatch]:
    try:
        size = path.stat().st_size
    except OSError:
        return None
    return addons.match_program(path.name, nro.read_nro_info(path), size)


# What the library indexes as single files. .nro is deliberately not in
# config.ALL_TRACKED_EXTENSIONS: that set also means "installed some other
# way" to sd_files.folder_summary, which would then stop treating every
# switch/ folder holding an .nro as SD card files.
_LIBRARY_FILE_EXTENSIONS = config.ALL_TRACKED_EXTENSIONS | {sd_files.NRO_EXTENSION}


def classified_revision(row) -> int:
    value = sd_files.row_details(row).get("classified")
    return value if isinstance(value, int) else 1


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            h.update(chunk)
    return h.hexdigest()


def is_file_stable(path: Path, wait_seconds: float = config.STABLE_CHECK_INTERVAL_SECONDS) -> bool:
    """True if the file isn't being written right now: same size across a
    short wait, and openable exclusively for read+write (nothing else has it
    open for writing). Mirrors Test-FileLocked from the proven old
    sync-to-switch.ps1 -- a file mid-download must never be hashed/classified
    yet, or we'd cache a half-written hash and never look at it again."""
    try:
        size_before = path.stat().st_size
    except OSError:
        return False
    time.sleep(wait_seconds)
    try:
        size_after = path.stat().st_size
    except OSError:
        return False
    if size_before != size_after:
        return False
    try:
        with path.open("r+b"):
            pass
    except OSError:
        return False
    return True


# ---------------------------------------------------------------------------
# Classification of a single inbox entry
# ---------------------------------------------------------------------------

def classify_file(abs_path: Path) -> dict:
    """Analyze a single file already confirmed stable. Returns the fields
    db.upsert_inbox_item expects, minus relative_path/size/mtime (the caller
    already has those from os.stat)."""
    ext = abs_path.suffix.lower()

    if ext in config.PACKAGE_EXTENSIONS:
        return _classify_package_file(abs_path, ext)

    if ext in config.ARCHIVE_EXTENSIONS:
        return _classify_archive_file(abs_path, ext)

    if ext in (sd_files.NRO_EXTENSION, OVL_EXTENSION):
        match = _addon_program(abs_path)
        if match is not None:
            return _addon_fields(abs_path, match, file_type=ext.lstrip(".").upper(),
                                 content_hash=sha256_file(abs_path),
                                 version=(nro.read_nro_info(abs_path) or nro.NroInfo(None, None, None)).version)
        if ext == sd_files.NRO_EXTENSION:
            return _classify_nro_file(abs_path)
        return dict(
            item_type="FILE", file_type="OVL", content_type=ContentType.UNKNOWN.value, package_format=None,
            content_hash=sha256_file(abs_path), title_id=None, title_id_source=None, title_id_confident=False,
            status="NEEDS_REVIEW", suggested_action=None, suggested_target=None,
            note="a Tesla overlay that is not one of the Add-ons catalog's", error=None, details_json=None,
        )

    raise ValueError(f"unsupported extension for classify_file: {ext}")


def _addon_fields(abs_path: Path, match: addons.ReleaseMatch, *, file_type: str,
                  content_hash: Optional[str], version: Optional[str]) -> dict:
    addon = addons.by_id(match.addon_id)
    label = f"{addon.name if addon else match.addon_id}{' ' + version if version else ''}"
    return dict(
        item_type="FILE", file_type=file_type, content_type=ContentType.ADDON.value,
        package_format=None, content_hash=content_hash,
        title_id=None, title_id_source=None, title_id_confident=False,
        status="ANALYZED", suggested_action="COPY_MERGE", suggested_target="SD_CARD",
        note=f"{label} -- an Add-ons utility, copied only to the places its catalog entry names",
        error=None, details_json=_details_json(extra=addon_details(match, version)),
    )


def _classify_nro_file(abs_path: Path) -> dict:
    """A lone .nro: an SD_FILES item of one file (see sd_files.py). Not
    trusted on its extension -- without an NRO0 header it is not an app."""
    info = nro.read_nro_info(abs_path)
    common = dict(
        item_type="FILE", file_type="NRO", package_format=None, content_hash=sha256_file(abs_path),
        title_id=None, title_id_source=None, title_id_confident=False, error=None,
    )
    if info is None:
        return dict(
            **common, content_type=ContentType.UNKNOWN.value,
            status="NEEDS_REVIEW", suggested_action=None, suggested_target=None,
            note="not a Switch homebrew application (no NRO0 header)", details_json=None,
        )
    name = " ".join(part for part in (info.name, info.version) if part) or None
    summary = sd_files.summarize([(sd_files.loose_nro_relative(abs_path.name), abs_path.stat().st_size)])
    return dict(
        **common, content_type=ContentType.SD_FILES.value,
        status="ANALYZED", suggested_action="COPY_MERGE", suggested_target="SD_CARD",
        note=None, details_json=_details_json(sd=summary, app_name=name),
    )


def _classify_package_file(abs_path: Path, ext: str, *, hash_content: bool = True) -> dict:
    fmt = ext.lstrip(".").upper()
    guess = title_id.from_filename(abs_path.name)
    content_hash = sha256_file(abs_path) if hash_content else None
    # A homebrew forwarder names the .nro it starts; that is how its switch/
    # folder is found and checked (see sd_files.py). Only packages small
    # enough to be one are read at all.
    details_json = _details_json(launches=sd_files.forwarder_launch_path(abs_path))

    if guess.title_id is None:
        # Never trust the filename alone -- if TITLE_ID cannot be
        # determined unambiguously: NEEDS_REVIEW, never guess. A bare
        # package file with no usable bracketed TITLE_ID in its name gives
        # us nothing else to go on at this stage (no NCA header parsing),
        # so we refuse to guess and stop here instead of picking one.
        return dict(
            item_type="FILE", file_type=fmt, content_type=ContentType.GAME_PACKAGE.value,
            package_format=fmt, content_hash=content_hash,
            title_id=None, title_id_source=None, title_id_confident=False,
            status="NEEDS_REVIEW", suggested_action=None, suggested_target=None,
            note="could not unambiguously determine TITLE_ID from the filename -- needs human review",
            error=None, details_json=details_json,
        )

    return dict(
        item_type="FILE", file_type=fmt, content_type=ContentType.GAME_PACKAGE.value,
        package_format=fmt, content_hash=content_hash,
        title_id=guess.title_id, title_id_source=guess.source, title_id_confident=guess.confident,
        status="ANALYZED", suggested_action="INSTALL_VIA_DBI", suggested_target="SD_INSTALL",
        note="TITLE_ID taken from the filename -- a release label, not a confirmed value from the package",
        error=None, details_json=details_json,
    )


def _classify_archive_file(abs_path: Path, ext: str) -> dict:
    archive_format = ext.lstrip(".").upper()
    try:
        entries = extractor.list_archive_entries(abs_path)
    except extractor.CorruptArchiveError as exc:
        return dict(
            item_type="FILE", file_type=archive_format, content_type=ContentType.UNKNOWN.value,
            package_format=None, content_hash=None,
            title_id=None, title_id_source=None, title_id_confident=False,
            status="ERROR", suggested_action=None, suggested_target=None,
            note=None, error=f"could not open {archive_format} (corrupt or still being written): {exc}",
            details_json=None,
        )

    content_hash = extractor.hash_entries(entries)
    cls = extractor.classify_entries(entries, archive_name=abs_path.name)
    # A package or mod archive can carry a switch/ folder as well; it is
    # installed alongside (see services.create_and_confirm_jobs), and the
    # row says so rather than keeping it out of sight.
    sd_details = _details_json(sd=cls.sd_summary)

    common = dict(
        item_type="FILE", file_type=archive_format, content_hash=content_hash,
        content_type=cls.content_type.value, package_format=cls.package_format,
        error=None,
    )

    if cls.content_type is ContentType.MIXED:
        title = cls.package_title_id_guess.title_id or cls.atmosphere_title_id_guess.title_id
        return dict(
            **common,
            title_id=title, title_id_source="archive", title_id_confident=False,
            status="NEEDS_REVIEW", suggested_action=None, suggested_target=None,
            note="archive contains both an installable package and an atmosphere mod -- MIXED, needs a human decision",
            details_json=sd_details,
        )

    if cls.content_type is ContentType.GAME_PACKAGE:
        return dict(
            **common,
            title_id=cls.package_title_id_guess.title_id,
            title_id_source=cls.package_title_id_guess.source,
            title_id_confident=cls.package_title_id_guess.confident,
            status="ANALYZED", suggested_action="INSTALL_VIA_DBI", suggested_target="SD_INSTALL",
            note="archive contains an installable package, will be safely extracted before sending",
            details_json=sd_details,
        )

    if cls.content_type is ContentType.ATMOSPHERE_MOD:
        return dict(
            **common,
            title_id=cls.atmosphere_title_id_guess.title_id,
            title_id_source=cls.atmosphere_title_id_guess.source,
            title_id_confident=cls.atmosphere_title_id_guess.confident,
            status="ANALYZED", suggested_action="COPY_MERGE", suggested_target="SD_CARD",
            note="archive contains an atmosphere mod, will be safely extracted before sending",
            details_json=sd_details,
        )

    if cls.content_type is ContentType.EMUIIBO:
        plan = cls.emuiibo_release
        version = None
        if plan.overlay_source:
            data = extractor.read_archive_member(abs_path, plan.overlay_source, max_bytes=emuiibo.OVERLAY_MAX_BYTES)
            version = emuiibo.overlay_version(data) if data else None
        version = version or emuiibo.version_from_name(abs_path.name)
        return dict(
            **common,
            title_id=None, title_id_source=None, title_id_confident=False,
            status="ANALYZED", suggested_action="COPY_MERGE", suggested_target="SD_CARD",
            note="emuiibo (virtual amiibo sysmodule and overlay), copied to where its release lays it out",
            details_json=_details_json(extra=emuiibo_details(plan, version)),
        )

    if cls.content_type is ContentType.ADDON:
        version = None
        if cls.addon.version_source:
            data = extractor.read_archive_member(abs_path, cls.addon.version_source,
                                                 max_bytes=addons.CONSOLE_READ_MAX_BYTES)
            info = nro.nro_info_from_bytes(data) if data else None
            version = info.version if info else None
        return _addon_fields(abs_path, cls.addon, file_type=archive_format, content_hash=content_hash,
                             version=version)

    if cls.content_type is ContentType.AMIIBO:
        return dict(
            **common,
            title_id=None, title_id_source=None, title_id_confident=False,
            status="ANALYZED", suggested_action="COPY_MERGE", suggested_target="SD_CARD",
            note="virtual amiibo for emuiibo, copied into emuiibo/amiibo/ on the SD card",
            details_json=_details_json(sd=cls.sd_summary, extra=amiibo_details(cls.amiibo)),
        )

    if cls.content_type is ContentType.SD_FILES:
        # No TITLE_ID inside a switch/ folder. A bracketed one in the
        # archive's own name is taken as the label it is; otherwise which
        # game this belongs to is decided once the whole library is known
        # (sd_files.assign_owners, run at the end of scan_library_once).
        guess = title_id.from_filename(abs_path.name)
        return dict(
            **common,
            title_id=guess.title_id, title_id_source=guess.source, title_id_confident=False,
            status="ANALYZED", suggested_action="COPY_MERGE", suggested_target="SD_CARD",
            note=("archive contains a homebrew app (.nro) for switch/ on the SD card, will be safely extracted before sending"
                  if cls.loose_nro else
                  "archive contains a switch/ folder for the SD card, will be safely extracted before sending"),
            details_json=sd_details,
        )

    tool = emuiibo.pc_tool(extractor.file_list(entries))
    if tool is not None:
        # Nothing to review and nothing to install: a program for this PC.
        return dict(
            **common,
            title_id=None, title_id_source=None, title_id_confident=False,
            status=NOT_FOR_SWITCH, suggested_action=None, suggested_target=None,
            note=(f"{tool}: emuiibo's PC app for making virtual amiibo"
                  + (" (emuiibo 1.1 replaced it with emuiigen)" if tool == "emutool" else "")
                  + " -- it runs on a PC, nothing of it goes to the Switch"),
            details_json=sd_details,
        )

    return dict(
        **common,
        title_id=None, title_id_source=None, title_id_confident=False,
        status="NEEDS_REVIEW", suggested_action=None, suggested_target=None,
        note="archive contains neither an nsp/nsz/xci/xcz package, an atmosphere structure nor a switch/ folder",
        details_json=sd_details,
    )


def hash_mod_folder(folder: Path) -> tuple[str, int, float]:
    """Cheap fingerprint for an on-disk atmosphere mod folder: sha256 over
    sorted (relative path, size, mtime_ns) of every contained file, not full
    file contents -- these folders can hold large romfs assets, and
    size+mtime is exactly what the proven old sync script already relies on
    for change detection. Also returns total size and max mtime."""
    h = hashlib.sha256()
    total_size = 0
    max_mtime = 0.0
    files = sorted((p for p in folder.rglob("*") if p.is_file()), key=lambda p: p.as_posix())
    for f in files:
        st = f.stat()
        rel = f.relative_to(folder).as_posix()
        h.update(rel.encode("utf-8", "surrogateescape"))
        h.update(str(st.st_size).encode())
        h.update(str(st.st_mtime_ns).encode())
        total_size += st.st_size
        max_mtime = max(max_mtime, st.st_mtime)
    return h.hexdigest(), total_size, max_mtime


def find_mod_folders(inbox_dir: Path, *, should_stop: Optional[Callable[[], bool]] = None) -> list[Path]:
    """Finds every atmosphere/contents/<TITLE_ID> directory placed directly
    in inbox/ (not inside an archive -- that case is handled by
    extractor.list_archive_entries + classify_entries). Matched by regex on
    the TITLE_ID directory name, not a fixed depth, so 'atmosphere' can be
    nested wherever the user happened to drop it.

    `should_stop`, if given, is polled while walking the tree and makes
    this return whatever it has found so far. A Library folder pointed at
    something like a real Downloads directory can take minutes just to
    enumerate, and a scan the user has asked to stop must not have to
    finish walking it first (see scan_library_once)."""
    result = []
    atmosphere_dirs = []
    for candidate in inbox_dir.rglob("atmosphere"):
        if should_stop is not None and should_stop():
            return result
        atmosphere_dirs.append(candidate)
    if inbox_dir.name.lower() == "atmosphere":
        atmosphere_dirs.insert(0, inbox_dir)
    for atmosphere_dir in atmosphere_dirs:
        if not atmosphere_dir.is_dir():
            continue
        for contents_dir in atmosphere_dir.iterdir():
            if contents_dir.name.lower() not in ("contents", "titles") or not contents_dir.is_dir():
                continue
            for child in contents_dir.iterdir():
                if child.is_dir() and TITLE_ID_DIR_RE.fullmatch(child.name):
                    result.append(child)
    return result


def emuiibo_release_folders(mod_folders: list[Path]) -> list[Path]:
    """The unpacked emuiibo releases among `mod_folders`: an
    atmosphere/contents/0100000000000352 holding exefs.nsp is emuiibo's
    sysmodule, and the folder atmosphere/ sits in is the release (SdOut/, as
    both real releases unpack)."""
    roots = []
    for folder in mod_folders:
        if folder.name.upper() == emuiibo.PROGRAM_ID and (folder / "exefs.nsp").is_file():
            roots.append(folder.parents[2])
    return list(dict.fromkeys(roots))


def _child_ci(folder: Path, name: str) -> Optional[Path]:
    try:
        for entry in folder.iterdir():
            if entry.name.lower() == name.lower():
                return entry
    except OSError:
        return None
    return None


def release_folder_files(root: Path) -> list[tuple[str, int]]:
    """(path relative to `root`, size) for the files of an unpacked release
    that are emuiibo's -- and only those, so a release unpacked straight
    into a busy folder is never fingerprinted or copied as a whole."""
    wanted: list[Path] = []
    contents = _child_ci(root, "atmosphere")
    contents = _child_ci(contents, "contents") if contents else None
    program = _child_ci(contents, emuiibo.PROGRAM_ID) if contents else None
    if program is not None:
        wanted += [p for p in program.rglob("*") if p.is_file()]
    overlays = _child_ci(root, "switch")
    overlays = _child_ci(overlays, ".overlays") if overlays else None
    ovl = _child_ci(overlays, "emuiibo.ovl") if overlays else None
    if ovl is not None and ovl.is_file():
        wanted.append(ovl)
    data = _child_ci(root, "emuiibo")
    data = _child_ci(data, "overlay") if data else None
    if data is not None:
        wanted += [p for p in data.rglob("*") if p.is_file()]
    return sorted((p.relative_to(root).as_posix(), p.stat().st_size) for p in wanted)


def hash_file_list(root: Path, files: list[tuple[str, int]]) -> tuple[str, int, float]:
    """hash_mod_folder's fingerprint over a chosen set of files."""
    h = hashlib.sha256()
    total_size = 0
    max_mtime = 0.0
    for rel, _size in sorted(files):
        st = (root / rel).stat()
        h.update(rel.encode("utf-8", "surrogateescape"))
        h.update(str(st.st_size).encode())
        h.update(str(st.st_mtime_ns).encode())
        total_size += st.st_size
        max_mtime = max(max_mtime, st.st_mtime)
    return h.hexdigest(), total_size, max_mtime


def _looks_like_dump_file(path: Path) -> bool:
    try:
        size = path.stat().st_size
        if size not in emuiibo.RAW_DUMP_SIZES:
            return False
        with path.open("rb") as handle:
            head = handle.read(32)
    except OSError:
        return False
    return emuiibo.looks_like_raw_dump(path.name, size, head)


def find_amiibo_collections(amiibo_jsons: list[Path], bin_files: list[Path], roots: list[Path]) -> list[Path]:
    """The folders in the library that are virtual amiibo collections: for
    every folder holding an amiibo.json (and every raw amiibo dump), the
    topmost folder above it that holds nothing but amiibo -- "ALL amiibo
    (flag_json)" for the real pack, not the release folder it sits in next
    to two archives. Never above a Library folder."""
    amiibo_dirs: list[Path] = []
    for folder in sorted({p.parent for p in amiibo_jsons}, key=lambda p: (len(p.parts), str(p))):
        if not any(folder.is_relative_to(done) for done in amiibo_dirs):
            amiibo_dirs.append(folder)
    amiibo_set = set(amiibo_dirs)
    dumps = {p for p in bin_files
             if not any(p.is_relative_to(d) for d in amiibo_dirs) and _looks_like_dump_file(p)}

    memo: dict[Path, bool] = {}

    def pure(folder: Path) -> bool:
        """Nothing in `folder` but amiibo (and files that say nothing, like
        Thumbs.db)."""
        if folder in amiibo_set:
            return True
        if folder in memo:
            return memo[folder]
        memo[folder] = False  # a link loop reads as "not pure", never as a hang
        result = True
        try:
            with os.scandir(folder) as it:
                for entry in it:
                    path = Path(entry.path)
                    if entry.is_dir(follow_symlinks=False):
                        if not pure(path):
                            result = False
                            break
                    elif path not in dumps and entry.name.lower() not in emuiibo._IGNORABLE_FILES:
                        result = False
                        break
        except OSError:
            result = False
        memo[folder] = result
        return result

    def library_root_of(path: Path) -> Optional[Path]:
        return next((r for r in roots if path.is_relative_to(r)), None)

    found: list[Path] = []
    for item in [*amiibo_dirs, *sorted(dumps)]:
        root = library_root_of(item)
        if root is None:
            continue
        candidate = item if item in amiibo_set else item.parent
        if candidate not in amiibo_set and not pure(candidate):
            continue  # a lone dump among other files is nobody's collection
        while candidate != root and pure(candidate.parent):
            candidate = candidate.parent
        found.append(candidate)
    outermost: list[Path] = []
    for folder in sorted(set(found), key=lambda p: (len(p.parts), str(p))):
        if not any(folder.is_relative_to(done) for done in outermost):
            outermost.append(folder)
    return outermost


def folder_collection(folder: Path) -> Optional[emuiibo.Collection]:
    files = [(p.relative_to(folder).as_posix(), p.stat().st_size) for p in folder.rglob("*") if p.is_file()]
    return emuiibo.find_collection(files, root_name=folder.name)


def path_is_inside_any(path: Path, roots: list[Path]) -> bool:
    for root in roots:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def scan_once(conn) -> dict:
    """One full pass over inbox/. Safe to call repeatedly (e.g. from a
    watchdog debounce or a timer) -- items unchanged since the last pass are
    recognized via size+mtime (files) or the folder fingerprint (mod
    folders) and skipped without re-hashing. Never writes to the Switch."""
    inbox_dir = config.INBOX_DIR
    inbox_dir.mkdir(parents=True, exist_ok=True)

    seen_relative_paths: set[str] = set()
    new_count = 0
    updated_count = 0
    unchanged_count = 0
    skipped_unstable = 0
    duplicate_count = 0
    errors = 0

    mod_folders = find_mod_folders(inbox_dir)
    for folder in mod_folders:
        rel_path = folder.relative_to(inbox_dir).as_posix()
        seen_relative_paths.add(rel_path)

        existing = db.get_inbox_item(conn, rel_path)
        content_hash, total_size, max_mtime = hash_mod_folder(folder)

        if existing is not None and existing["content_hash"] == content_hash:
            db.touch_inbox_item_scanned(conn, existing["id"])
            unchanged_count += 1
            continue

        db.upsert_inbox_item(
            conn,
            relative_path=rel_path,
            item_type="MOD_FOLDER",
            file_type="ATMOSPHERE_MOD",
            size=total_size,
            mtime=max_mtime,
            content_hash=content_hash,
            content_type=ContentType.ATMOSPHERE_MOD.value,
            package_format=None,
            title_id=folder.name.upper(),
            title_id_source="atmosphere_path",
            title_id_confident=True,
            status="ANALYZED",
            suggested_action="COPY_MERGE",
            suggested_target="SD_CARD",
            note=None,
            error=None,
            details_json=None,
        )
        if existing is None:
            new_count += 1
        else:
            updated_count += 1

    candidate_files = [
        p for p in inbox_dir.rglob("*")
        if p.is_file()
        and p.suffix.lower() in config.ALL_TRACKED_EXTENSIONS
        and not path_is_inside_any(p, mod_folders)
    ]

    for abs_path in candidate_files:
        rel_path = abs_path.relative_to(inbox_dir).as_posix()
        seen_relative_paths.add(rel_path)

        try:
            st = abs_path.stat()
        except OSError:
            errors += 1
            continue

        existing = db.get_inbox_item(conn, rel_path)
        if existing is not None and existing["size"] == st.st_size and existing["mtime"] == st.st_mtime:
            db.touch_inbox_item_scanned(conn, existing["id"])
            unchanged_count += 1
            continue

        if not is_file_stable(abs_path):
            skipped_unstable += 1
            continue

        try:
            fields = classify_file(abs_path)
        except Exception as exc:  # noqa: BLE001 -- one bad file must not kill the whole scan pass
            db.upsert_inbox_item(
                conn, relative_path=rel_path, item_type="FILE",
                file_type=abs_path.suffix.lstrip(".").upper(), size=st.st_size,
                mtime=st.st_mtime, content_hash=None, title_id=None,
                title_id_source=None, status="ERROR", suggested_action=None,
                suggested_target=None, note=None, error=str(exc),
            )
            errors += 1
            continue

        # SKIP_DUPLICATE: same content already analyzed under a different
        # name/path elsewhere in inbox/. Content equality wins over whatever
        # this item's OWN filename-based classification concluded -- e.g. a
        # byte-identical copy with no bracketed TITLE_ID in its name would
        # otherwise land on NEEDS_REVIEW, even though we already know exactly
        # what it is from its twin. We flag it, we do NOT touch the original
        # and we do NOT delete this copy -- the user decides.
        if fields.get("content_hash") and fields.get("status") != "ERROR":
            dup = db.find_other_analyzed_item_with_hash(conn, fields["content_hash"], rel_path)
            if dup is not None:
                fields = dict(fields)
                fields["status"] = "SKIP_DUPLICATE"
                fields["suggested_action"] = None
                fields["suggested_target"] = None
                fields["note"] = f"the same file was already found in inbox as '{dup['relative_path']}'"
                duplicate_count += 1
            else:
                # ARCH-005: same content may already sit in the library
                # (config.LIBRARY_DIR) instead of inbox/ -- a separate root,
                # scanned by a separate pass (scan_library_once()), that the
                # check above can never see since it only ever looks at
                # inbox_items. Never deletes/changes either file; only flags.
                lib_dup = db.find_library_item_with_hash(conn, fields["content_hash"])
                if lib_dup is not None:
                    fields = dict(fields)
                    fields["status"] = "SKIP_DUPLICATE"
                    fields["suggested_action"] = None
                    fields["suggested_target"] = None
                    fields["note"] = f"the same file is already in the library as '{lib_dup['absolute_path']}'"
                    duplicate_count += 1

        db.upsert_inbox_item(
            conn, relative_path=rel_path, size=st.st_size, mtime=st.st_mtime, **fields,
        )
        if existing is None:
            new_count += 1
        else:
            updated_count += 1

    removed_count = db.delete_inbox_items_missing_from(conn, seen_relative_paths)

    return dict(
        new=new_count, updated=updated_count, unchanged=unchanged_count,
        skipped_unstable=skipped_unstable, duplicates=duplicate_count,
        errors=errors, removed=removed_count,
    )


# ---------------------------------------------------------------------------
# Web UI: library scan -- same classification logic as scan_once() above,
# applied to config.LIBRARY_DIR (an arbitrary, permanent, user-configured
# directory, e.g. D:\shared\Download) instead of INBOX_DIR, and indexed
# into library_items (keyed by absolute path) instead of inbox_items (keyed
# by inbox-relative path). Deliberately a separate function, not a
# root-parameterized scan_once(): the two tables have different identity
# columns (relative_path vs absolute_path) and different status vocabularies
# (inbox's NEW/ANALYZED/... vs library's AVAILABLE/...), so sharing one
# function would need a branch at nearly every db.* call anyway -- see
# docs/WEB-UI.md for the "separate table, minimal overlap" rationale.
# ---------------------------------------------------------------------------

def scan_library_once(
    conn, *, on_file: Optional[Callable[[str], None]] = None,
    should_stop: Optional[Callable[[], bool]] = None,
    on_progress: Optional[Callable[[str, int, Optional[int]], None]] = None,
) -> dict:
    """One full pass over config.LIBRARY_DIR. Safe to call repeatedly --
    items unchanged since the last pass (by size+mtime for files, or the
    folder fingerprint for mod folders) are recognized and skipped without
    re-hashing, exactly like scan_once(). Never writes to the Switch, never
    creates a job -- indexing a file here only ever produces an AVAILABLE
    (or NEEDS_REVIEW/ERROR/SKIP_DUPLICATE) library_items row, nothing more
    (see queue_worker.py for the only code that ever creates a job, always
    from an explicit, separate user action).

    `on_file`, if given, is called with the absolute path of each item
    (mod folder or file) right as it starts being looked at -- including
    already-unchanged ones, so it's an honest "currently looking at"
    progress signal, not just "currently indexing new content". Purely a
    UI progress hook (see switchagent/web/context.py's
    run_scan_in_background); scan_library_once() itself has no other use
    for it and works identically with or without one.

    `on_progress(phase, done, total)`, if given, says where the pass is,
    for a progress bar (web/context.py): "walking" while the folders are
    still being enumerated -- done counts entries seen, total is None, since
    nobody knows yet -- then "indexing" over every item found (done of
    total), then "finishing" for the whole-library passes at the end.

    `should_stop`, if given, is polled at every step -- while enumerating
    the tree as well as per item -- and makes this return early with
    cancelled=True. Everything already indexed in this pass is kept (each
    item is committed as it is read), but the two whole-library passes at
    the bottom are deliberately SKIPPED: both reason from
    seen_absolute_paths as if it were the complete picture, so running
    either on a half-finished walk would merge or delete rows for files
    this pass simply never got to. A cancelled scan therefore only ever
    adds knowledge, never removes it."""
    library_dirs = config.library_dirs()
    if not library_dirs:
        # The user removed their last Library folder. Not a failure and not
        # "the drive is unplugged" (the branch below): nothing is part of
        # the collection any more, so nothing should still be indexed as
        # if it were. delete_library_items_missing_from() is the same pass
        # that already retires a single deleted file, with the same rule
        # for a row some job still references -- flagged ERROR and kept,
        # never hard-deleted, so Queue/History keep their display names.
        removed_count = db.delete_library_items_missing_from(conn, set())
        return dict(
            new=0, updated=0, unchanged=0, skipped_unstable=0, duplicates=0,
            errors=0, removed=removed_count, relocated=0,
        )

    missing = [p for p in library_dirs if not p.is_dir()]
    if len(missing) == len(library_dirs):
        return dict(
            new=0, updated=0, unchanged=0, skipped_unstable=0, duplicates=0, errors=0, removed=0,
            relocated=0, error=f"library directories do not exist: {missing}",
        )

    def _stopped() -> bool:
        return should_stop is not None and should_stop()

    def _progress(phase: str, done: int, total: Optional[int]) -> None:
        if on_progress is not None:
            on_progress(phase, done, total)

    _progress("walking", 0, None)

    seen_absolute_paths: set[str] = set()
    # Which of those were inserted, not just refreshed -- see the
    # relocation pass at the bottom for why that has to be tracked by path
    # and not just counted.
    newly_indexed_paths: set[str] = set()
    new_count = 0
    updated_count = 0
    unchanged_count = 0
    skipped_unstable = 0
    duplicate_count = 0
    errors = 0

    def _cancelled() -> dict:
        return dict(
            new=new_count, updated=updated_count, unchanged=unchanged_count,
            skipped_unstable=skipped_unstable, duplicates=duplicate_count,
            errors=errors, removed=0, relocated=0, cancelled=True,
        )

    mod_folders = list(dict.fromkeys(
        folder for root in library_dirs if root.is_dir()
        for folder in find_mod_folders(root, should_stop=should_stop)
    ))
    if _stopped():
        return _cancelled()
    # An unpacked emuiibo release is one item of its own, not "a mod" for a
    # program id that is no game (see emuiibo.py).
    release_roots = emuiibo_release_folders(mod_folders)
    # Its sysmodule folder belongs to the release item -- and its exefs.nsp
    # is no package to install either (see the file walk below).
    release_programs = [m for m in mod_folders
                        if m.name.upper() == emuiibo.PROGRAM_ID and m.parents[2] in release_roots]
    mod_folders = [m for m in mod_folders if m not in release_programs]
    for root in release_roots:
        if _stopped():
            return _cancelled()
        abs_path = str(root)
        seen_absolute_paths.add(abs_path)
        if on_file is not None:
            on_file(abs_path)
        files = release_folder_files(root)
        plan = emuiibo.plan_release(files)
        existing = db.get_library_item(conn, abs_path)
        content_hash, total_size, max_mtime = hash_file_list(root, files)
        if (existing is not None and existing["status"] != db.LIBRARY_ITEM_RETIRED
                and existing["content_hash"] == content_hash
                and classified_revision(existing) >= CLASSIFICATION_REVISION):
            db.touch_library_item_scanned(conn, existing["id"])
            unchanged_count += 1
            continue
        version = None
        if plan is not None and plan.overlay_source:
            try:
                version = emuiibo.overlay_version((root / plan.overlay_source).read_bytes())
            except OSError:
                version = None
        db.upsert_library_item(
            conn, absolute_path=abs_path, item_type="MOD_FOLDER", file_type=ContentType.EMUIIBO.value,
            size=total_size, mtime=max_mtime, content_hash=content_hash,
            content_type=ContentType.EMUIIBO.value, package_format=None,
            title_id=None, title_id_source=None, title_id_confident=False,
            status="AVAILABLE", suggested_action="COPY_MERGE", suggested_target="SD_CARD",
            note="emuiibo (virtual amiibo sysmodule and overlay), copied to where its release lays it out",
            error=None,
            details_json=_details_json(extra=emuiibo_details(
                plan, version or emuiibo.version_from_name(root.name))) if plan else None,
        )
        if existing is None:
            new_count += 1
            newly_indexed_paths.add(abs_path)
        else:
            updated_count += 1

    for folder in mod_folders:
        if _stopped():
            return _cancelled()
        abs_path = str(folder)
        seen_absolute_paths.add(abs_path)
        if on_file is not None:
            on_file(abs_path)

        existing = db.get_library_item(conn, abs_path)
        content_hash, total_size, max_mtime = hash_mod_folder(folder)

        # "Unchanged" has to mean "its CONTENT is unchanged", never "its
        # membership is unchanged". A row this pass has just walked to is, by
        # definition, in the library again -- and a retired one (see
        # db.LIBRARY_ITEM_RETIRED) says the opposite, so it must not take the
        # shortcut past re-indexing. Remove a Library folder and add it straight
        # back and every file is byte-identical, which is exactly when this
        # fires: 19 of one real library's 48 rows stayed retired, and therefore
        # invisible, through any number of rescans. Re-classifying them costs
        # one pass over files that have just rejoined the library, and only
        # ever on that pass.
        if (existing is not None and existing["status"] != db.LIBRARY_ITEM_RETIRED
                and existing["content_hash"] == content_hash):
            db.touch_library_item_scanned(conn, existing["id"])
            unchanged_count += 1
            continue

        db.upsert_library_item(
            conn,
            absolute_path=abs_path,
            item_type="MOD_FOLDER",
            file_type="ATMOSPHERE_MOD",
            size=total_size,
            mtime=max_mtime,
            content_hash=content_hash,
            content_type=ContentType.ATMOSPHERE_MOD.value,
            package_format=None,
            title_id=folder.name.upper(),
            title_id_source="atmosphere_path",
            title_id_confident=True,
            status="AVAILABLE",
            suggested_action="COPY_MERGE",
            suggested_target="SD_CARD",
            note=None,
            error=None,
            details_json=None,
        )
        if existing is None:
            new_count += 1
            newly_indexed_paths.add(abs_path)
        else:
            updated_count += 1

    # Materialised with an explicit loop rather than a comprehension purely
    # so the walk itself can be interrupted: on a folder the size of a real
    # Downloads directory this enumeration alone is most of the wait, and a
    # cancel that only took effect afterwards would not feel like a cancel.
    candidate_files: list[Path] = []
    # Folders called "switch" are noted during the SAME walk rather than a
    # second one: on a network share this enumeration is most of a scan.
    switch_dirs: list[Path] = []
    # Virtual amiibo (their amiibo.json) and possible raw amiibo dumps, from
    # the same walk -- see find_amiibo_collections.
    amiibo_jsons: list[Path] = []
    bin_files: list[Path] = []
    walked = 0
    for root in library_dirs:
        if not root.is_dir():
            continue
        for candidate in root.rglob("*"):
            if _stopped():
                return _cancelled()
            walked += 1
            if walked % 25 == 0:
                _progress("walking", walked, None)
            lowered_name = candidate.name.lower()
            if lowered_name == sd_files.SD_ROOT_DIR:
                if candidate.is_dir():
                    switch_dirs.append(candidate)
                continue
            if lowered_name == emuiibo.AMIIBO_JSON:
                amiibo_jsons.append(candidate)
                continue
            if lowered_name.endswith(".bin"):
                bin_files.append(candidate)
                continue
            if (candidate.suffix.lower() == OVL_EXTENSION and candidate.is_file()
                    and not path_is_inside_any(candidate, mod_folders)
                    and _addon_program(candidate) is not None):
                candidate_files.append(candidate)
                continue
            if (candidate.suffix.lower() in _LIBRARY_FILE_EXTENSIONS
                    and candidate.is_file()
                    and not path_is_inside_any(candidate, mod_folders)
                    and not path_is_inside_any(candidate, release_programs)):
                candidate_files.append(candidate)
    candidate_files = list(dict.fromkeys(candidate_files))

    # switch/ folders already unpacked in the library (a homebrew port's
    # .nro often ships that way, next to an archive of its data): each is
    # one SD_FILES item, fingerprinted exactly like a mod folder.
    amiibo_folders = [
        f for f in find_amiibo_collections(amiibo_jsons, bin_files, [r for r in library_dirs if r.is_dir()])
        if not path_is_inside_any(f, mod_folders)
    ]
    # A release's own switch/ holds only its overlay, which the release item
    # already copies; anything more in it is somebody's homebrew as usual.
    release_switch_dirs = [
        d for d in switch_dirs
        if d.parent in release_roots and all(
            p.relative_to(d).as_posix().lower() == ".overlays/emuiibo.ovl"
            for p in d.rglob("*") if p.is_file())
    ]
    sd_folders = sd_files.find_sd_folders(
        switch_dirs, exclude=[*mod_folders, *amiibo_folders, *release_switch_dirs],
    )
    # An .nro inside one of those is part of it, not an app of its own.
    candidate_files = [
        c for c in candidate_files
        if not path_is_inside_any(c, amiibo_folders)
        and (c.suffix.lower() != sd_files.NRO_EXTENSION or not path_is_inside_any(c, sd_folders))
    ]
    to_index = len(amiibo_folders) + len(sd_folders) + len(candidate_files)
    indexed = 0
    _progress("indexing", 0, to_index)
    for folder in amiibo_folders:
        if _stopped():
            return _cancelled()
        indexed += 1
        _progress("indexing", indexed, to_index)
        abs_path = str(folder)
        seen_absolute_paths.add(abs_path)
        if on_file is not None:
            on_file(abs_path)
        existing = db.get_library_item(conn, abs_path)
        content_hash, total_size, max_mtime = hash_mod_folder(folder)
        if (existing is not None and existing["status"] != db.LIBRARY_ITEM_RETIRED
                and existing["content_hash"] == content_hash
                and classified_revision(existing) >= CLASSIFICATION_REVISION):
            db.touch_library_item_scanned(conn, existing["id"])
            unchanged_count += 1
            continue
        collection = folder_collection(folder)
        db.upsert_library_item(
            conn, absolute_path=abs_path, item_type="MOD_FOLDER", file_type=ContentType.AMIIBO.value,
            size=total_size, mtime=max_mtime, content_hash=content_hash,
            content_type=ContentType.AMIIBO.value, package_format=None,
            title_id=None, title_id_source=None, title_id_confident=False,
            status="AVAILABLE" if collection else "NEEDS_REVIEW",
            suggested_action="COPY_MERGE" if collection else None,
            suggested_target="SD_CARD" if collection else None,
            note="virtual amiibo for emuiibo, copied into emuiibo/amiibo/ on the SD card",
            error=None,
            details_json=_details_json(extra=amiibo_details(collection) if collection else None),
        )
        if existing is None:
            new_count += 1
            newly_indexed_paths.add(abs_path)
        else:
            updated_count += 1

    for folder in sd_folders:
        if _stopped():
            return _cancelled()
        indexed += 1
        _progress("indexing", indexed, to_index)
        abs_path = str(folder)
        seen_absolute_paths.add(abs_path)
        if on_file is not None:
            on_file(abs_path)

        existing = db.get_library_item(conn, abs_path)
        content_hash, total_size, max_mtime = hash_mod_folder(folder)
        if (existing is not None and existing["status"] != db.LIBRARY_ITEM_RETIRED
                and existing["content_hash"] == content_hash):
            db.touch_library_item_scanned(conn, existing["id"])
            unchanged_count += 1
            continue

        # item_type is the folder kind the schema has (a CHECK constraint
        # allows FILE and MOD_FOLDER only); content_type is what says this
        # folder is SD card files rather than a mod.
        db.upsert_library_item(
            conn,
            absolute_path=abs_path,
            item_type="MOD_FOLDER",
            file_type=ContentType.SD_FILES.value,
            size=total_size,
            mtime=max_mtime,
            content_hash=content_hash,
            content_type=ContentType.SD_FILES.value,
            package_format=None,
            title_id=None,
            title_id_source=None,
            title_id_confident=False,
            status="AVAILABLE",
            suggested_action="COPY_MERGE",
            suggested_target="SD_CARD",
            note=None,
            error=None,
            details_json=_details_json(sd=sd_files.folder_summary(folder)),
        )
        if existing is None:
            new_count += 1
            newly_indexed_paths.add(abs_path)
        else:
            updated_count += 1

    for abs_path_obj in candidate_files:
        if _stopped():
            return _cancelled()
        indexed += 1
        _progress("indexing", indexed, to_index)
        abs_path = str(abs_path_obj)
        seen_absolute_paths.add(abs_path)
        if on_file is not None:
            on_file(abs_path)

        try:
            st = abs_path_obj.stat()
        except OSError:
            errors += 1
            continue

        existing = db.get_library_item(conn, abs_path)
        # Same rule as the mod-folder loop above: a retired row must be
        # re-indexed rather than recognised as unchanged.
        same_file = (existing is not None and existing["status"] != db.LIBRARY_ITEM_RETIRED
                     and existing["size"] == st.st_size and existing["mtime"] == st.st_mtime)
        if same_file and classified_revision(existing) >= CLASSIFICATION_REVISION:
            db.touch_library_item_scanned(conn, existing["id"])
            unchanged_count += 1
            continue

        is_package = abs_path_obj.suffix.lower() in config.PACKAGE_EXTENSIONS
        # A file only being classified again by a newer revision has not
        # changed since it was last read whole, so it is not mid-download
        # and needs no stability wait (2s a file, on every archive at once).
        if not is_package and not same_file and not is_file_stable(abs_path_obj):
            skipped_unstable += 1
            continue

        try:
            # Index package names and metadata only. Installation preparation
            # hashes the selected payload; indexing must not read gigabytes.
            fields = (_classify_package_file(abs_path_obj, abs_path_obj.suffix.lower(), hash_content=False)
                      if is_package else classify_file(abs_path_obj))
        except Exception as exc:  # noqa: BLE001 -- one bad file must not kill the whole scan pass
            db.upsert_library_item(
                conn, absolute_path=abs_path, item_type="FILE",
                file_type=abs_path_obj.suffix.lstrip(".").upper(), size=st.st_size,
                mtime=st.st_mtime, content_hash=None, title_id=None,
                title_id_source=None, status="ERROR", suggested_action=None,
                suggested_target=None, note=None, error=str(exc),
            )
            errors += 1
            continue

        # library's status vocabulary uses AVAILABLE where inbox uses
        # ANALYZED -- translate classify_file()'s output the same way
        # scan_once() consumes it, just with a different terminal label for
        # the "ready to act on" case.
        fields = dict(fields)
        if fields.get("status") == "ANALYZED":
            fields["status"] = "AVAILABLE"

        # SKIP_DUPLICATE: same content already indexed under a different
        # path elsewhere in the library -- mirrors scan_once()'s dedup
        # rule exactly (see find_other_library_item_with_hash's docstring).
        if fields.get("content_hash") and fields.get("status") != "ERROR":
            dup = db.find_other_library_item_with_hash(conn, fields["content_hash"], abs_path)
            if dup is not None:
                fields["status"] = "SKIP_DUPLICATE"
                fields["suggested_action"] = None
                fields["suggested_target"] = None
                fields["note"] = f"the same file was already found in the library as '{dup['absolute_path']}'"
                duplicate_count += 1
            else:
                # ARCH-005: mirror-image of scan_once()'s cross-root check --
                # same content may already sit in inbox/ instead of the
                # library. Never deletes/changes either file; only flags.
                inbox_dup = db.find_inbox_item_with_hash(conn, fields["content_hash"])
                if inbox_dup is not None:
                    fields["status"] = "SKIP_DUPLICATE"
                    fields["suggested_action"] = None
                    fields["suggested_target"] = None
                    fields["note"] = f"the same file was already found in inbox as '{inbox_dup['relative_path']}'"
                    duplicate_count += 1

        db.upsert_library_item(
            conn, absolute_path=abs_path, size=st.st_size, mtime=st.st_mtime, **fields,
        )
        if existing is None:
            new_count += 1
            newly_indexed_paths.add(abs_path)
        else:
            updated_count += 1

    _progress("finishing", to_index, to_index)

    # Before anything is called stale: a row this pass never visited may be
    # the very file it DID visit under another path spelling (a Library
    # folder re-pointed at the same directory over UNC, a drive letter
    # change, a renamed parent). Fold those together rather than leaving
    # one file counted as two. Runs against the paths genuinely walked
    # above -- deliberately BEFORE the unplugged-drive backfill below, so
    # an offline root's rows can never become a merge target.
    relocated_paths = db.merge_relocated_library_items(
        conn, seen_absolute_paths, skip_roots=missing,
    )
    # A row that turns out to be an already-known file under a new path was
    # never new, however it looked a moment ago -- reporting "1 new" for a
    # file the library has had all along is exactly the confusion this pass
    # exists to remove. (The keeper may equally be a row from an EARLIER
    # scan, e.g. a library already carrying both spellings before this
    # existed; then there is no `new` to take back and the sets simply do
    # not intersect.)
    new_count -= len(newly_indexed_paths & set(relocated_paths))
    relocated_count = len(relocated_paths)

    # An unplugged drive must not erase its existing index.
    for row in db.list_library_items(conn):
        if any(Path(row["absolute_path"]).is_relative_to(root) for root in missing):
            seen_absolute_paths.add(row["absolute_path"])
    removed_count = db.delete_library_items_missing_from(conn, seen_absolute_paths)

    # Last, with the whole library known: which game each switch/ folder
    # belongs to. Needs every package's final row, which is why it cannot
    # happen while the files are still being read.
    assign_sd_file_owners(conn)

    return dict(
        new=new_count, updated=updated_count, unchanged=unchanged_count,
        skipped_unstable=skipped_unstable, duplicates=duplicate_count,
        errors=errors, removed=removed_count, relocated=relocated_count,
    )


def index_library_file(conn, path: Path):
    """Indexes ONE file of a Library folder right away -- exactly what the
    next full scan would record for it (classify_file, AVAILABLE for
    ANALYZED) -- and returns its row. For a file SwitchAgent itself just put
    there (an emuiibo release downloaded from GitHub), which is meant to be
    installable at once rather than after the next scan. The full scan then
    finds it unchanged."""
    st = path.stat()
    fields = dict(classify_file(path))
    if fields.get("status") == "ANALYZED":
        fields["status"] = "AVAILABLE"
    db.upsert_library_item(conn, absolute_path=str(path), size=st.st_size, mtime=st.st_mtime, **fields)
    return db.get_library_item(conn, str(path))


def amiibo_owners(rows) -> dict[int, tuple[Optional[str], Optional[str]]]:
    """{row id: (family TITLE_ID or None, how it was decided)} for every
    AMIIBO row: the game a virtual amiibo collection came with, so its card
    can say so and installing the game installs them too.

      1. a [TITLE_ID] in the collection's own name;
      2. otherwise its release folder: the nearest folder above it holding
         any game. One game there owns it. Several: the one whose own
         package lies directly in that folder -- the release's game, where
         the others sit in sub-folders (Animal Crossing's release keeps the
         game, its update and a DLC at the top and the Island Transfer Tool
         in "Official Transfer Tool/"). Still more than one: nobody --
         never a guess.

    Unlike a switch/ folder, a collection is useful without its game, so an
    unowned one is not a problem, just shown on the Amiibo tab alone."""
    families_below: dict[str, set[str]] = {}
    families_direct: dict[str, set[str]] = {}
    for row in rows:
        if row["content_type"] != ContentType.GAME_PACKAGE.value or not row["title_id"]:
            continue
        try:
            family = title_id.classify_title_variant(row["title_id"]).base_title_id
        except (ValueError, TypeError):
            continue
        path = Path(row["absolute_path"])
        families_direct.setdefault(str(path.parent), set()).add(family)
        for parent in path.parents:
            families_below.setdefault(str(parent), set()).add(family)

    owners: dict[int, tuple[Optional[str], Optional[str]]] = {}
    for row in rows:
        if row["content_type"] != ContentType.AMIIBO.value:
            continue
        guess = title_id.from_filename(Path(row["absolute_path"]).name)
        if guess.title_id is not None:
            owners[row["id"]] = (title_id.classify_title_variant(guess.title_id).base_title_id, "filename")
            continue
        owner = None
        for parent in Path(row["absolute_path"]).parents:
            found = families_below.get(str(parent))
            if not found:
                continue
            if len(found) == 1:
                owner = next(iter(found))
            else:
                direct = families_direct.get(str(parent), set())
                owner = next(iter(direct)) if len(direct) == 1 else None
            break
        owners[row["id"]] = (owner, "folder" if owner else None)
    return owners


def assign_sd_file_owners(conn) -> int:
    """Records, on every SD_FILES and AMIIBO row, the game it belongs to
    (sd_files.assign_owners and amiibo_owners for the rules) as that row's
    title_id. Rows whose answer did not change are not written. Returns how
    many changed.

    Stored rather than worked out on every read because everything
    downstream already keys off library_items.title_id -- Library's grouping,
    the install order a job waits in, Queue and History's names -- and the
    answer only changes when the library does, i.e. on a scan."""
    rows = [row for row in db.list_library_items(conn) if row["status"] != db.LIBRARY_ITEM_RETIRED]
    by_id = {row["id"]: row for row in rows}
    changed = 0
    sd_owners = sd_files.assign_owners(rows)
    forwarders, port_parts = sd_files.sealed_forwarders(rows, sd_owners)
    for row in rows:
        is_forwarder = row["title_id_source"] == sd_files.SEALED_FORWARDER
        if (row["id"] in forwarders) != is_forwarder:
            db.set_library_item_sealed_forwarder(conn, row["id"], row["id"] in forwarders)
            changed += 1
    for row_id in port_parts:
        sd_owners[row_id] = (None, sd_files.PORT_PART)
    owners = {**sd_owners, **amiibo_owners(rows)}
    for row_id, (owner, source) in owners.items():
        row = by_id[row_id]
        if row["title_id"] == owner and row["title_id_source"] == source:
            continue
        db.set_library_item_title_id(conn, row_id, title_id=owner, source=source)
        changed += 1
    return changed
