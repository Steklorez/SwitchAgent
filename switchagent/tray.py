"""Minimal Windows system tray icon for the desktop launcher
(switchagent/desktop.py) -- pywin32's win32gui/Shell_NotifyIcon, no extra
GUI dependency (wx/PyQt/tkinter). Menu: "Open SwitchAgent", one "Open ...
folder" item per distinct folder the app lives in (see _folder_entries),
and "Exit".

Runs its own Win32 message loop (PumpMessages()) on whichever thread calls
run() -- desktop.py calls it from the main thread, which then blocks for
the application's entire lifetime until Exit is chosen. This module never
touches MTP/COM objects at all (it only opens the browser and returns
control to its caller), so it cannot violate the project's
RealMtpBackend COM thread-affinity rule regardless of which thread it
runs on.

The message-routing DECISIONS (which mouse/menu event means "open" vs
"exit") are pure functions (_classify_tray_click/_classify_menu_command)
kept independent of any real win32gui/win32con object so they are
unit-testable without a Windows GUI session. The window/message-loop
plumbing around them (_TrayWindow) is not independently unit-tested here
-- it requires a real interactive Windows desktop session, which this
environment does not have; see the packaging report for what still needs
manual verification.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger("switchagent.desktop")

MENU_OPEN_ID = 1023
MENU_EXIT_ID = 1024
# Folder items get MENU_FOLDER_BASE_ID + their index in _folder_entries().
MENU_FOLDER_BASE_ID = 1030

# WM_APP + 20 -- an arbitrary, app-private window message id used as the
# Shell_NotifyIcon callback message (must not collide with any standard
# WM_* constant; WM_APP's whole range exists exactly for this).
WM_TRAYICON = 0x8000 + 20


def _classify_tray_click(lparam: int, *, wm_lbuttonup: int, wm_lbuttondblclk: int, wm_rbuttonup: int) -> str:
    """Pure decision logic for a Shell_NotifyIcon callback message's
    lParam (the actual mouse event) -- "open" / "menu" / "ignore"."""
    if lparam in (wm_lbuttonup, wm_lbuttondblclk):
        return "open"
    if lparam == wm_rbuttonup:
        return "menu"
    return "ignore"


def _classify_menu_command(menu_id: int, folder_count: int = 0) -> str:
    """Pure decision logic for a WM_COMMAND menu selection -- "open" /
    "exit" / "folder:<index>" / "ignore" (anything else, e.g. a stray
    message not from our own menu)."""
    if menu_id == MENU_OPEN_ID:
        return "open"
    if menu_id == MENU_EXIT_ID:
        return "exit"
    index = menu_id - MENU_FOLDER_BASE_ID
    if 0 <= index < folder_count:
        return f"folder:{index}"
    return "ignore"


def _folder_entries(*, program_dir: Path, data_dir: Path, logs_dir: Path) -> list[tuple[str, Path]]:
    """Pure: the "Open ... folder" menu items, in menu order. Where the
    program and its data are one directory (portable, dev) that is one
    item, not two pointing at the same place; the installed build keeps
    them apart (Program Files vs %LOCALAPPDATA%) and gets both."""
    entries: list[tuple[str, Path]] = []
    seen: set[str] = set()
    for label, path in (
        ("Open program folder", program_dir),
        ("Open data folder", data_dir),
        ("Open logs folder", logs_dir),
    ):
        key = os.path.normcase(os.path.abspath(path))
        if key in seen:
            continue
        seen.add(key)
        entries.append((label, path))
    return entries


def _current_folder_entries() -> list[tuple[str, Path]]:
    from . import config, paths

    program_dir = paths.executable_dir() if paths.is_frozen() else paths.resource_root()
    return _folder_entries(program_dir=program_dir, data_dir=paths.app_data_root(), logs_dir=config.LOGS_DIR)


def _default_icon_path():
    from . import paths

    # Bundled application icon (see packaging/, Stage 7) -- looked up
    # relative to the resource root so it resolves correctly both in a
    # source checkout and inside a frozen PyInstaller bundle.
    candidate = paths.resource_root() / "switchagent" / "web" / "static" / "app.ico"
    return candidate if candidate.is_file() else None


class _TrayWindow:
    """Thin wrapper around the real Win32 objects -- not unit-tested (see
    module docstring); kept as small as possible so the untested surface
    area is small too."""

    def __init__(self, *, on_open: Callable[[], None], tooltip: str = "SwitchAgent"):
        import win32api
        import win32con
        import win32gui

        self._win32api = win32api
        self._win32con = win32con
        self._win32gui = win32gui
        self._on_open = on_open
        self._tooltip = tooltip
        self._exiting = False
        self._folders: list[tuple[str, Path]] = []

        message_map = {
            WM_TRAYICON: self._on_tray_message,
            win32con.WM_COMMAND: self._on_command,
            win32con.WM_CLOSE: self._on_close,
            win32con.WM_DESTROY: self._on_destroy,
        }

        wc = win32gui.WNDCLASS()
        wc.hInstance = win32api.GetModuleHandle(None)
        wc.lpszClassName = "SwitchAgentTrayWindow"
        wc.lpfnWndProc = message_map
        class_atom = win32gui.RegisterClass(wc)

        self._hwnd = win32gui.CreateWindow(
            class_atom, "SwitchAgent", 0, 0, 0, 0, 0, 0, 0, wc.hInstance, None,
        )
        win32gui.UpdateWindow(self._hwnd)

        icon_path = _default_icon_path()
        if icon_path is not None:
            self._hicon = win32gui.LoadImage(
                0, str(icon_path), win32con.IMAGE_ICON, 0, 0,
                win32con.LR_LOADFROMFILE | win32con.LR_DEFAULTSIZE,
            )
        else:
            self._hicon = win32gui.LoadIcon(0, win32con.IDI_APPLICATION)

        win32gui.Shell_NotifyIcon(win32gui.NIM_ADD, (
            self._hwnd, 0, win32gui.NIF_ICON | win32gui.NIF_MESSAGE | win32gui.NIF_TIP,
            WM_TRAYICON, self._hicon, self._tooltip,
        ))

    def run(self) -> None:
        self._win32gui.PumpMessages()

    def close(self) -> None:
        """Programmatic close -- lets desktop.py tear the tray down even
        if the user never clicks Exit (e.g. the HTTP server thread died
        unexpectedly). Safe to call from any thread: PostMessage just
        queues a message for the window's own thread to process."""
        if self._exiting:
            return
        self._win32gui.PostMessage(self._hwnd, self._win32con.WM_CLOSE, 0, 0)

    # -- WNDPROC handlers -------------------------------------------------

    def _on_tray_message(self, hwnd, msg, wparam, lparam):
        action = _classify_tray_click(
            lparam,
            wm_lbuttonup=self._win32con.WM_LBUTTONUP,
            wm_lbuttondblclk=self._win32con.WM_LBUTTONDBLCLK,
            wm_rbuttonup=self._win32con.WM_RBUTTONUP,
        )
        if action == "open":
            self._safe_on_open()
        elif action == "menu":
            self._show_menu()
        return 1

    def _safe_on_open(self) -> None:
        try:
            self._on_open()
        except Exception:
            log.exception("tray 'Open SwitchAgent' handler failed")

    def _show_menu(self) -> None:
        win32gui, win32con = self._win32gui, self._win32con
        menu = win32gui.CreatePopupMenu()
        try:
            # AppendMenu requires the menu handle as its FIRST argument.
            # Omitting it raised TypeError before the popup could appear.
            win32gui.AppendMenu(menu, win32con.MF_STRING, MENU_OPEN_ID, "Open SwitchAgent")
            # Only folders that exist right now -- the logs folder, for
            # one, does not until something has been logged.
            self._folders = [(label, path) for label, path in _current_folder_entries() if path.is_dir()]
            if self._folders:
                # "" and not None: pywin32 raises TypeError on None, and
                # that exception used to abort the whole menu before it
                # could appear.
                win32gui.AppendMenu(menu, win32con.MF_SEPARATOR, 0, "")
                for index, (label, _path) in enumerate(self._folders):
                    win32gui.AppendMenu(menu, win32con.MF_STRING, MENU_FOLDER_BASE_ID + index, label)
            win32gui.AppendMenu(menu, win32con.MF_SEPARATOR, 0, "")
            win32gui.AppendMenu(menu, win32con.MF_STRING, MENU_EXIT_ID, "Exit")
            pos = win32gui.GetCursorPos()
            win32gui.SetForegroundWindow(self._hwnd)
            selected = win32gui.TrackPopupMenu(
                menu, win32con.TPM_LEFTALIGN | win32con.TPM_RIGHTBUTTON |
                win32con.TPM_RETURNCMD | win32con.TPM_NONOTIFY,
                pos[0], pos[1], 0, self._hwnd, None,
            )
            win32gui.PostMessage(self._hwnd, win32con.WM_NULL, 0, 0)
        finally:
            win32gui.DestroyMenu(menu)
        if selected:
            self._on_command(self._hwnd, win32con.WM_COMMAND, selected, 0)

    def _on_close(self, hwnd, msg, wparam, lparam):
        self._win32gui.DestroyWindow(hwnd)
        return 0

    def _on_command(self, hwnd, msg, wparam, lparam):
        menu_id = self._win32api.LOWORD(wparam)
        action = _classify_menu_command(menu_id, len(self._folders))
        if action == "open":
            self._safe_on_open()
        elif action == "exit":
            self.close()
        elif action.startswith("folder:"):
            self._open_folder(self._folders[int(action.split(":", 1)[1])][1])
        return 0

    def _open_folder(self, path: Path) -> None:
        try:
            os.startfile(str(path))
        except Exception:
            log.exception("tray: could not open folder %s", path)

    def _on_destroy(self, hwnd, msg, wparam, lparam):
        self._exiting = True
        try:
            self._win32gui.Shell_NotifyIcon(self._win32gui.NIM_DELETE, (self._hwnd, 0))
        except Exception:
            log.exception("failed to remove tray icon on shutdown")
        self._win32gui.PostQuitMessage(0)
        return 0


def run_tray_icon(*, on_open: Callable[[], None], tooltip: str = "SwitchAgent") -> None:
    """Blocks until the user selects Exit (or the tray is closed
    programmatically via the returned handle in a future extension) --
    see module docstring. Any failure to even create the tray (e.g. no
    interactive desktop session, which can happen when SwitchAgent is
    launched under a service account) is raised to the caller, which
    falls back to a non-tray shutdown path (see desktop.py)."""
    global _active_window
    window = _TrayWindow(on_open=on_open, tooltip=tooltip)
    _active_window = window
    try:
        window.run()
    finally:
        _active_window = None


_active_window: "Optional[_TrayWindow]" = None


def close_active() -> None:
    """Ends run_tray_icon() from any thread, as if Exit were clicked (an
    app update needs SwitchAgent gone before it can replace its files)."""
    if _active_window is not None:
        _active_window.close()
