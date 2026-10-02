"""SwitchAgent's own update (switchagent/app_update.py): the file fetched
follows how this copy runs, nothing unverified is run, and a transfer in
progress is never cut off."""

from __future__ import annotations

import hashlib
import io
import json
import time
import zipfile

import pytest

from switchagent import app_update, github_release


def _zip(names) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for n in names:
            z.writestr(n, b"x")
    return buf.getvalue()


INSTALLER = b"MZ" + b"\0" * 100
PORTABLE = _zip(["SwitchAgent.exe", "portable.flag", "_internal/a.dll"])


class _Resp(io.BytesIO):
    def __init__(self, data, url):
        super().__init__(data)
        self._url = url

    def geturl(self):
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def _opener(tag="v9.0.0", digest=True, requested=None):
    assets = {app_update.INSTALLER_ASSET: INSTALLER, app_update.PORTABLE_ASSET: PORTABLE}

    def opener(request, timeout=None):
        url = request.full_url
        if url.endswith("/releases/latest"):
            return _Resp(json.dumps({
                "tag_name": tag, "html_url": "https://github.com/x/y/releases/tag/" + tag,
                "assets": [{
                    "name": n, "size": len(d),
                    "digest": ("sha256:" + hashlib.sha256(d).hexdigest()) if digest else None,
                    "browser_download_url": "https://github.com/x/y/releases/download/" + n,
                } for n, d in assets.items()],
            }).encode(), url)
        name = url.rsplit("/", 1)[1]
        if requested is not None:
            requested.append(name)
        return _Resp(assets[name], url)
    return opener


def _run(tmp_path, mode, *, busy=lambda: False, **opener_kw):
    launched, exited, requested = [], [], []
    updater = app_update.AppUpdater(
        app_data_root=tmp_path, current_version="1.0.0", is_busy=busy,
        request_exit=lambda: exited.append(True), mode=mode,
        opener=_opener(requested=requested, **opener_kw), launch=lambda *a: launched.append(a))
    updater.start()
    updater._thread.join(10)
    return updater, launched, exited, requested


def test_asset_follows_the_runtime_mode():
    assert app_update.asset_for_mode("installed") == "SwitchAgent-Setup-x64.exe"
    assert app_update.asset_for_mode("portable") == "SwitchAgent-Portable-x64.zip"
    assert app_update.asset_for_mode("dev") is None


def test_installed_copy_downloads_only_the_installer_and_restarts(tmp_path):
    updater, launched, exited, requested = _run(tmp_path, "installed")
    assert updater.status()["phase"] == "installing"
    assert requested == [app_update.INSTALLER_ASSET]
    (_script, _pid, mode, source, _target, _exe), = launched
    assert mode == "installed" and source.read_bytes() == INSTALLER
    assert exited == [True]


def test_portable_copy_downloads_only_the_zip_and_unpacks_it(tmp_path):
    updater, launched, exited, requested = _run(tmp_path, "portable")
    assert updater.status()["phase"] == "installing"
    assert requested == [app_update.PORTABLE_ASSET]
    (_script, _pid, mode, source, _target, _exe), = launched
    assert mode == "portable" and (source / "SwitchAgent.exe").is_file() and (source / "portable.flag").is_file()


def test_dev_run_installs_nothing(tmp_path):
    updater = app_update.AppUpdater(app_data_root=tmp_path, current_version="1.0.0",
                                    is_busy=lambda: False, mode="dev")
    assert updater.status()["can_install"] is False
    with pytest.raises(github_release.DownloadError):
        updater.start()


def test_no_published_checksum_means_nothing_is_run(tmp_path):
    updater, launched, exited, _ = _run(tmp_path, "installed", digest=False)
    assert updater.status()["phase"] == "failed"
    assert launched == [] and exited == []


def test_not_newer_is_not_installed(tmp_path):
    updater, launched, _, _ = _run(tmp_path, "installed", tag="v1.0.0")
    assert updater.status()["phase"] == "failed" and launched == []


def test_waits_for_a_running_transfer(tmp_path, monkeypatch):
    monkeypatch.setattr(app_update, "BUSY_POLL_SECONDS", 0.01)
    deadline = time.monotonic() + 0.2
    seen = []

    def busy():
        seen.append(True)
        return time.monotonic() < deadline

    updater, launched, _, _ = _run(tmp_path, "installed", busy=busy)
    assert len(seen) > 1 and len(launched) == 1


def test_portable_zip_with_an_escaping_path_is_refused(tmp_path):
    bad = tmp_path / "bad.zip"
    bad.write_bytes(_zip(["SwitchAgent.exe", "portable.flag", "../evil.txt"]))
    with pytest.raises(github_release.DownloadError):
        app_update.safe_extract(bad, tmp_path / "out")
    assert not (tmp_path / "evil.txt").exists()


def test_a_zip_that_is_not_portable_switchagent_is_refused(tmp_path):
    other = tmp_path / "other.zip"
    other.write_bytes(_zip(["SwitchAgent.exe"]))
    with pytest.raises(github_release.DownloadError):
        app_update.safe_extract(other, tmp_path / "out")


def test_helper_is_not_started_detached(monkeypatch, tmp_path):
    """PowerShell started with DETACHED_PROCESS exits at once having run
    nothing -- the update downloaded and then simply never happened."""
    import subprocess

    seen = {}
    monkeypatch.setattr(subprocess, "Popen", lambda args, **kw: seen.update(kw))
    app_update._launch_helper(tmp_path / "s.ps1", 1, "portable", tmp_path, tmp_path, tmp_path / "x.exe")
    assert not seen["creationflags"] & getattr(subprocess, "DETACHED_PROCESS", 0x8)
