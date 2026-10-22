"""Settings -> Start with Windows (switchagent/autostart.py), against a
fake registry: the Run value is the whole truth of the setting."""

from __future__ import annotations

from pathlib import Path

import pytest

from switchagent import autostart


class FakeReg:
    HKEY_CURRENT_USER = "HKCU"
    REG_SZ = 1

    def __init__(self, value=None):
        self.values = {} if value is None else {autostart.VALUE_NAME: value}

    class _Key:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def OpenKey(self, root, path):
        return self._Key()

    CreateKey = OpenKey

    def QueryValueEx(self, key, name):
        if name not in self.values:
            raise FileNotFoundError(name)
        return self.values[name], self.REG_SZ

    def SetValueEx(self, key, name, _reserved, _kind, value):
        self.values[name] = value

    def DeleteValue(self, key, name):
        del self.values[name]


@pytest.fixture
def exe(tmp_path):
    path = tmp_path / "SwitchAgent.exe"
    path.write_bytes(b"MZ")
    return path


def test_off_by_default(exe):
    assert autostart.status(exe=exe, reg=FakeReg()) == {"available": True, "enabled": False, "other_copy": None}


def test_on_registers_this_exe_without_a_browser_tab(exe):
    reg = FakeReg()
    assert autostart.set_enabled(True, exe=exe, reg=reg)["enabled"] is True
    assert reg.values[autostart.VALUE_NAME] == f'"{exe}" --no-browser'


def test_off_removes_it(exe):
    reg = FakeReg()
    autostart.set_enabled(True, exe=exe, reg=reg)
    assert autostart.set_enabled(False, exe=exe, reg=reg)["enabled"] is False
    assert autostart.VALUE_NAME not in reg.values


def test_another_copy_is_named_and_left_alone_by_off(exe, tmp_path):
    other = str(tmp_path / "old" / "SwitchAgent.exe")
    reg = FakeReg(f'"{other}" --no-browser')
    state = autostart.status(exe=exe, reg=reg)
    assert state["enabled"] is False and state["other_copy"] == other
    autostart.set_enabled(False, exe=exe, reg=reg)
    assert reg.values[autostart.VALUE_NAME] == f'"{other}" --no-browser'


def test_on_replaces_another_copy(exe, tmp_path):
    reg = FakeReg('"C:\old\SwitchAgent.exe" --no-browser')
    state = autostart.set_enabled(True, exe=exe, reg=reg)
    assert state == {"available": True, "enabled": True, "other_copy": None}


def test_a_dev_run_cannot_register(monkeypatch):
    monkeypatch.setattr(autostart.paths, "is_frozen", lambda: False)
    assert autostart.status()["available"] is False
    with pytest.raises(RuntimeError):
        autostart.set_enabled(True)
