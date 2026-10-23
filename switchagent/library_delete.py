"""Deleting Library files from the PC, from the Library page.

The one place SwitchAgent removes anything from a Windows disk. Everything
here is about not removing the wrong thing:

- only what the Library index itself points at, and only inside a folder the
  user chose as a Library folder (never the folder itself, never a link);
- the plan the dialog shows and the deletion are the same code: a delete
  request is checked again from scratch, never trusted from the plan;
- the Recycle Bin is the default. It is used only where Windows really has
  one for the file (a fixed local drive) -- a network share, a USB stick or a
  very long path has none, and sending those "to the bin" would silently
  delete them for good. For such a file the Recycle Bin choice is refused,
  never quietly turned into a permanent delete;
- a file a queued or running install still needs is not deleted.

The index follows the disk: a row nobody ever referenced is dropped, one a
job or history entry points at is kept as RETIRED (db.drop_library_items).
"""

from __future__ import annotations

import ctypes
import os
import shutil
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

from . import config, db

MODE_RECYCLE = "recycle"
MODE_PERMANENT = "permanent"

# Jobs that still read the source file, or will when they start.
_JOB_STATUSES_NEEDING_THE_FILE = (
    "PENDING_CONFIRM", "CONFIRMED", "WAITING_FOR_DEVICE", "DEVICE_UNAVAILABLE", "RUNNING",
    "VERIFYING", "INTERRUPTED", "WAITING_FOR_BASE", "BLOCKED_BY_DEPENDENCY",
)

_MAX_BIN_PATH = 259   # SHFileOperation does not take longer paths


@dataclass
class Entry:
    id: int
    path: str
    kind: str                         # "file" | "folder"
    size: int
    exists: bool
    recoverable: bool                 # the Recycle Bin can take it
    blocked: Optional[str] = None     # why it will not be deleted at all
    also: list[str] = field(default_factory=list)   # other indexed items that go with it

    def public(self) -> dict:
        p = Path(self.path)
        return {"id": self.id, "path": self.path, "file": p.name, "folder": str(p.parent), "kind": self.kind,
                "size": self.size, "exists": self.exists, "recoverable": self.recoverable,
                "blocked": self.blocked, "also": self.also}


# -- what Windows can do ------------------------------------------------------

def can_recycle(path: str) -> bool:
    """True when Windows has a Recycle Bin for this path: a fixed local drive.
    Network shares (UNC or a mapped drive), removable drives and over-long
    paths have none."""
    if sys.platform != "win32" or len(path) > _MAX_BIN_PATH or path.startswith("\\\\"):
        return False
    drive = os.path.splitdrive(path)[0]
    if not drive:
        return False
    DRIVE_FIXED = 3
    return ctypes.windll.kernel32.GetDriveTypeW(drive + "\\") == DRIVE_FIXED


class _SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", ctypes.c_void_p), ("wFunc", ctypes.c_uint), ("pFrom", ctypes.c_wchar_p),
        ("pTo", ctypes.c_wchar_p), ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", ctypes.c_int),
        ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", ctypes.c_wchar_p),
    ]


def recycle(path: str) -> None:
    """Moves one file or folder to the Recycle Bin, with no dialog. Raises
    OSError when Windows did not do it."""
    FO_DELETE = 3
    FOF_SILENT, FOF_NOCONFIRMATION, FOF_ALLOWUNDO, FOF_NOERRORUI = 0x4, 0x10, 0x40, 0x400
    op = _SHFILEOPSTRUCTW()
    op.wFunc = FO_DELETE
    op.pFrom = path + "\0"            # the list is double-NUL terminated; ctypes adds the last one
    op.fFlags = FOF_SILENT | FOF_NOCONFIRMATION | FOF_ALLOWUNDO | FOF_NOERRORUI
    code = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if code != 0 or op.fAnyOperationsAborted or os.path.lexists(path):
        raise OSError(f"Windows could not move it to the Recycle Bin (code {code})")


def _remove_readonly(func, p, _exc):
    os.chmod(p, stat.S_IWRITE)
    func(p)


def delete_permanently(path: str) -> None:
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path, onerror=_remove_readonly)
    else:
        try:
            os.remove(path)
        except PermissionError:
            os.chmod(path, stat.S_IWRITE)
            os.remove(path)


# -- the plan -----------------------------------------------------------------

def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return path != root


def _is_link(path: str) -> bool:
    if os.path.islink(path):
        return True
    try:
        return bool(os.lstat(path).st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)   # a junction
    except (OSError, AttributeError):
        return False


def _tree_size(path: str) -> int:
    total = 0
    for base, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(base, name))
            except OSError:
                pass
    return total


def plan(conn, item_ids: Iterable[int], *, preparing_item_ids: Iterable[int] = ()) -> list[Entry]:
    """One Entry per selected library item, in the order given: what is on
    disk, whether the Recycle Bin can take it, and whether anything forbids
    deleting it."""
    roots = [Path(os.path.normpath(str(d))) for d in config.library_dirs()]
    preparing = {int(i) for i in preparing_item_ids}
    all_rows = None
    entries: list[Entry] = []
    seen_paths: set[str] = set()
    for item_id in dict.fromkeys(int(i) for i in item_ids):
        row = db.get_library_item_by_id(conn, item_id)
        if row is None:
            continue
        path = os.path.normpath(row["absolute_path"])
        key = os.path.normcase(path)
        is_dir = row["item_type"] == "MOD_FOLDER" or os.path.isdir(path)
        exists = os.path.lexists(path)
        entry = Entry(id=item_id, path=path, kind="folder" if is_dir else "file", size=0,
                      exists=exists, recoverable=False)
        if key in seen_paths:
            entry.blocked = "same file as another selected item"   # deleted once, with the first
        seen_paths.add(key)
        if not any(_inside(Path(path), root) for root in roots):
            entry.blocked = "not inside a Library folder"
        elif exists and _is_link(path):
            entry.blocked = "a link, not a file of its own"
        elif conn.execute(
            f"SELECT 1 FROM jobs WHERE library_item_id = ? AND abandoned = 0 AND status IN "
            f"({','.join('?' * len(_JOB_STATUSES_NEEDING_THE_FILE))}) LIMIT 1",
            (item_id, *_JOB_STATUSES_NEEDING_THE_FILE),
        ).fetchone() or item_id in preparing:
            entry.blocked = "waiting in Queue or being installed"
        if exists:
            try:
                entry.size = _tree_size(path) if is_dir else os.path.getsize(path)
            except OSError:
                entry.size = row["size"] or 0
            entry.recoverable = can_recycle(path)
        else:
            entry.size = 0
        # Other indexed items that live in this very file or folder go with it.
        if all_rows is None:
            all_rows = conn.execute("SELECT id, absolute_path FROM library_items").fetchall()
        prefix = os.path.normcase(path) + os.sep
        for other in all_rows:
            if other["id"] == item_id:
                continue
            other_key = os.path.normcase(os.path.normpath(other["absolute_path"]))
            if other_key == key or (is_dir and other_key.startswith(prefix)):
                entry.also.append(other["absolute_path"])
        entries.append(entry)
    return entries


# -- the deletion -------------------------------------------------------------

def delete(conn, item_ids: Iterable[int], mode: str, *, preparing_item_ids: Iterable[int] = (),
           recycler: Callable[[str], None] = None, remover: Callable[[str], None] = None) -> dict:
    """Deletes what plan() allows, one file at a time, and brings the index
    in line with what is really gone. A failure on one file never stops the
    others. Returns {"deleted": [ids], "failed": [{id, error}], "bytes": n}."""
    if mode not in (MODE_RECYCLE, MODE_PERMANENT):
        raise ValueError(f"unknown mode {mode!r}")
    recycler = recycler or recycle
    remover = remover or delete_permanently
    deleted: list[int] = []
    failed: list[dict] = []
    freed = 0
    gone_paths: list[str] = []
    for entry in plan(conn, item_ids, preparing_item_ids=preparing_item_ids):
        if entry.blocked == "same file as another selected item":
            continue
        if entry.blocked:
            failed.append({"id": entry.id, "error": entry.blocked})
            continue
        try:
            if entry.exists:
                if mode == MODE_RECYCLE:
                    if not entry.recoverable:
                        failed.append({"id": entry.id, "error": "no Recycle Bin for this location -- "
                                                                 "choose permanent deletion, or keep the file"})
                        continue
                    recycler(entry.path)
                else:
                    remover(entry.path)
                freed += entry.size
            gone_paths.append(entry.path)
            deleted.append(entry.id)
        except OSError as exc:
            failed.append({"id": entry.id, "error": str(exc) or exc.__class__.__name__})
    if gone_paths:
        _forget(conn, gone_paths)
    return {"deleted": deleted, "failed": failed, "bytes": freed}


def _forget(conn, gone_paths: list[str]) -> None:
    keys = {os.path.normcase(os.path.normpath(p)) for p in gone_paths}
    prefixes = tuple(k + os.sep for k in keys)
    stale = [
        r["id"] for r in conn.execute("SELECT id, absolute_path FROM library_items").fetchall()
        if (k := os.path.normcase(os.path.normpath(r["absolute_path"]))) in keys or k.startswith(prefixes)
    ]
    db.drop_library_items(conn, stale, reason="deleted from the Library by the user")
