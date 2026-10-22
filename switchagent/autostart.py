"""Start SwitchAgent with Windows -- Settings' "Start with Windows" switch.

One value under the current user's Run key (no admin rights, nothing
system-wide): `SwitchAgent` = `"<this exe>" --no-browser`, so a start at
sign-in sits in the tray instead of opening a browser tab every morning.
The registry is the only record of the setting; nothing is mirrored into
preferences, so what the page says is what Windows will actually do.

Only a built copy (installed or portable) can register itself: a dev run
has no exe to point at. Two copies on one PC share the value -- turning
it on in one replaces the other's, and the page says which copy the value
points at when it is not this one.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

from . import paths

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "SwitchAgent"
ARGS = "--no-browser"


def _winreg():
    import winreg

    return winreg


def command_for(exe: Path) -> str:
    return f'"{exe}" {ARGS}'


def exe_in_command(command: str) -> Optional[str]:
    command = command.strip()
    if command.startswith('"'):
        end = command.find('"', 1)
        return command[1:end] if end > 0 else None
    return command.split(" ", 1)[0] or None


def _read(reg=None) -> Optional[str]:
    reg = reg or _winreg()
    try:
        with reg.OpenKey(reg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _kind = reg.QueryValueEx(key, VALUE_NAME)
            return str(value)
    except OSError:
        return None


def status(*, exe: Optional[Path] = None, reg=None) -> dict:
    if exe is None:
        if not paths.is_frozen() or sys.platform != "win32":
            return {"available": False, "enabled": False, "other_copy": None}
        exe = Path(sys.executable)
    current = _read(reg)
    target = exe_in_command(current) if current else None
    ours = target is not None and Path(target).resolve() == exe.resolve()
    return {
        "available": True,
        "enabled": ours,
        "other_copy": target if target and not ours else None,
    }


def set_enabled(enabled: bool, *, exe: Optional[Path] = None, reg=None) -> dict:
    if exe is None and not paths.is_frozen():
        raise RuntimeError("only an installed or portable SwitchAgent can start with Windows")
    exe = exe or Path(sys.executable)
    reg = reg or _winreg()
    with reg.CreateKey(reg.HKEY_CURRENT_USER, RUN_KEY) as key:
        if enabled:
            reg.SetValueEx(key, VALUE_NAME, 0, reg.REG_SZ, command_for(exe))
        else:
            # Only our own entry: another copy's is that copy's switch.
            if status(exe=exe, reg=reg)["enabled"]:
                reg.DeleteValue(key, VALUE_NAME)
    return status(exe=exe, reg=reg)
