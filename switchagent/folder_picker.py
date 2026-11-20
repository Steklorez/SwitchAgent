"""Native Windows folder selection, isolated from the MTP COM thread."""
import os
import threading

_lock = threading.Lock()


def _bring_dialog_to_front(hwnd, msg, lp, data) -> int:
    """SHBrowseForFolder callback (BrowseCallbackProc) -- forces the dialog
    to the foreground the instant it's created (BFFM_INITIALIZED).

    Without this, the dialog opens with no owner window (hwndOwner=0 below
    -- this process has no window of its own to use as one) and Windows'
    foreground-lock timeout then leaves it buried behind whatever window
    currently has input focus (typically the browser tab the "Choose and
    add a folder..." click came from) -- invisible, not merely inactive,
    since nothing raised it above that window either. A plain
    SetForegroundWindow() from a background process is refused outright by
    that same foreground-lock mechanism; toggling HWND_TOPMOST on and then
    immediately back off first is the standard workaround -- it only
    reorders Z-order (no special permission needed for a window your own
    process owns) and is enough to raise this dialog above the currently
    focused window, at which point SetForegroundWindow succeeds too."""
    import win32con
    import win32gui
    from win32com.shell import shellcon
    if msg == shellcon.BFFM_INITIALIZED:
        win32gui.SetWindowPos(hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0,
                              win32con.SWP_NOMOVE | win32con.SWP_NOSIZE)
        win32gui.SetWindowPos(hwnd, win32con.HWND_NOTOPMOST, 0, 0, 0, 0,
                              win32con.SWP_NOMOVE | win32con.SWP_NOSIZE)
        win32gui.SetForegroundWindow(hwnd)
    return 0


# A folder with more sub-folders than this is shown cut short (and says so);
# the path can still be typed.
BROWSE_LIMIT = 2000


def list_drives() -> list[str]:
    if os.name != "nt":
        return ["/"]
    drives = getattr(os, "listdrives", None)
    if drives is not None:
        return list(drives())
    return [f"{letter}:\\" for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ" if os.path.exists(f"{letter}:\\")]


def browse_folders(path: str | None) -> dict:
    """The sub-folders of one folder on the computer running SwitchAgent --
    the folder picker for a browser on another machine, which the native
    dialog (pick_folder) cannot reach: it would open on the server's own
    screen. Folders only, never files; nothing here writes. An empty path
    lists the drives."""
    if not path or not path.strip():
        return {"path": "", "parent": None, "folders": [{"name": d, "path": d} for d in list_drives()],
                "truncated": False}
    folder = os.path.abspath(os.path.expanduser(path.strip()))
    if not os.path.isdir(folder):
        raise FileNotFoundError(f"Not a folder: {folder}")
    names = []
    with os.scandir(folder) as entries:
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=True):
                    names.append(entry.name)
            except OSError:
                continue
    names.sort(key=str.casefold)
    parent = os.path.dirname(folder.rstrip("\\/")) if folder.rstrip("\\/") else None
    if parent == folder or not parent or (os.name == "nt" and folder.rstrip("\\/").endswith(":")):
        parent = ""  # a drive root goes back to the drive list
    return {
        "path": folder,
        "parent": parent,
        "folders": [{"name": n, "path": os.path.join(folder, n)} for n in names[:BROWSE_LIMIT]],
        "truncated": len(names) > BROWSE_LIMIT,
    }


def pick_folder() -> str | None:
    if os.name != "nt":
        raise RuntimeError("Folder picking is only available on Windows. Enter the path manually.")
    if not _lock.acquire(blocking=False):
        raise RuntimeError("A folder picker dialog is already open")
    try:
        import pythoncom
        from win32com.shell import shell, shellcon
        pythoncom.CoInitialize()
        try:
            result = shell.SHBrowseForFolder(
                0, None, "Choose a folder with games or mods",
                shellcon.BIF_RETURNONLYFSDIRS | 0x0040,  # BIF_NEWDIALOGSTYLE is absent in pywin32 shellcon
                _bring_dialog_to_front, None,
            )
            if not result or not result[0]:
                return None
            return shell.SHGetPathFromIDListW(result[0])
        finally:
            pythoncom.CoUninitialize()
    finally:
        _lock.release()
