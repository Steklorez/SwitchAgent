"""Updating SwitchAgent itself, on the user's click in the top banner.

Which file is fetched follows how this copy runs (paths.runtime_mode()):

  - "installed" -- SwitchAgent-Setup-x64.exe, the same per-user Inno Setup
    installer the user installed from. Run with /SILENT: a progress window,
    no questions, then SwitchAgent starts again (/RELAUNCH=1, see
    SwitchAgent.iss).
  - "portable"  -- SwitchAgent-Portable-x64.zip, unpacked over this folder:
    program files are replaced, the user's data next to them (config.yaml,
    data/, work/, logs/) is not in the zip and is never touched.
  - "dev"       -- nothing to install; the banner links to the release page.

Never a portable zip onto an installed copy or the other way round: the
asset name is chosen here and nowhere else.

The download goes through github_release (GitHub's hosts only, exact size,
GitHub's published SHA-256 -- required here, not optional: this file is
run). Program files cannot be replaced while SwitchAgent runs, so the last
step is a small PowerShell helper that waits for this process to exit,
then installs and starts the new version. An install already sending
files to a console is never cut off: the update waits for it to finish.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
import zipfile
from pathlib import Path, PurePosixPath
from typing import Callable, Optional

from . import github_release, paths, update_check

log = logging.getLogger(__name__)

INSTALLER_ASSET = "SwitchAgent-Setup-x64.exe"
PORTABLE_ASSET = "SwitchAgent-Portable-x64.zip"
MAX_BYTES = 400 * 1024 * 1024
UPDATES_FOLDER = "updates"
BUSY_POLL_SECONDS = 5.0

_HELPER = r"""
param([int]$WaitPid, [string]$Mode, [string]$Source, [string]$Target, [string]$Exe)
$ErrorActionPreference = 'Stop'
try { Wait-Process -Id $WaitPid -Timeout 120 -ErrorAction SilentlyContinue } catch {}
Start-Sleep -Seconds 1
if ($Mode -eq 'installed') {
  Start-Process -FilePath $Source -ArgumentList '/SILENT','/SUPPRESSMSGBOXES','/NORESTART','/RELAUNCH=1'
} else {
  Remove-Item -LiteralPath (Join-Path $Target '_internal') -Recurse -Force -ErrorAction SilentlyContinue
  Get-ChildItem -LiteralPath $Source -Force | ForEach-Object {
    Copy-Item -LiteralPath $_.FullName -Destination $Target -Recurse -Force
  }
  Remove-Item -LiteralPath $Source -Recurse -Force -ErrorAction SilentlyContinue
  Start-Process -FilePath $Exe -WorkingDirectory $Target
}
"""


def asset_for_mode(mode: str) -> Optional[str]:
    return {"installed": INSTALLER_ASSET, "portable": PORTABLE_ASSET}.get(mode)


def _check_installer(data: bytes) -> None:
    if data[:2] != b"MZ":
        raise github_release.DownloadError("the downloaded installer is not a Windows program")


def _check_portable(data: bytes) -> None:
    if data[:4] != b"PK\x03\x04":
        raise github_release.DownloadError("the downloaded portable build is not a zip")


def safe_extract(zip_path: Path, dest: Path) -> None:
    """Unpacks the portable zip; refuses any entry that would land outside
    `dest`, and a zip that is not a portable SwitchAgent."""
    with zipfile.ZipFile(zip_path) as archive:
        names = archive.namelist()
        for name in names:
            parts = PurePosixPath(name.replace("\\", "/")).parts
            if not parts or parts[0] in ("/", "") or ".." in parts or ":" in parts[0]:
                raise github_release.DownloadError(f"the portable zip holds an unsafe path: {name!r}")
        if "SwitchAgent.exe" not in names or "portable.flag" not in names:
            raise github_release.DownloadError("the portable zip is not a portable SwitchAgent")
        archive.extractall(dest)


class AppUpdater:
    """One update at a time. State is read by GET /api/app-update."""

    def __init__(
        self, *, app_data_root: Path, current_version: str,
        is_busy: Callable[[], bool], request_exit: Optional[Callable[[], None]] = None,
        mode: Optional[str] = None, opener=None, launch=None,
    ):
        self.app_data_root = app_data_root
        self.current_version = current_version
        self.is_busy = is_busy
        self.request_exit = request_exit
        self.mode = mode or paths.runtime_mode()
        self._opener = opener
        self._launch = launch or _launch_helper
        self._lock = threading.Lock()
        self._state: dict = {"phase": "idle"}
        self._thread: Optional[threading.Thread] = None

    def status(self) -> dict:
        with self._lock:
            state = dict(self._state)
        state["mode"] = self.mode
        state["can_install"] = asset_for_mode(self.mode) is not None
        return state

    def _set(self, **state) -> None:
        with self._lock:
            self._state = state

    def start(self) -> dict:
        with self._lock:
            if self._state.get("phase") in ("downloading", "waiting", "installing"):
                return dict(self._state)
            if asset_for_mode(self.mode) is None:
                raise github_release.DownloadError("this copy of SwitchAgent is not an installed or portable build")
            self._state = {"phase": "downloading", "done": 0, "total": 0}
        self._thread = threading.Thread(target=self._run, name="switchagent-app-update", daemon=True)
        self._thread.start()
        return self.status()

    def _run(self) -> None:
        try:
            self._download_and_install()
        except github_release.DownloadError as exc:
            log.warning("app update failed: %s", exc)
            self._set(phase="failed", error=str(exc))
        except Exception as exc:  # noqa: BLE001 -- shown on the banner, never a crash
            log.exception("app update failed")
            self._set(phase="failed", error=str(exc))

    def _download_and_install(self) -> None:
        asset = asset_for_mode(self.mode)
        kwargs = {"opener": self._opener} if self._opener else {}
        release = github_release.latest_release(
            update_check.GITHUB_REPO_SLUG, asset, max_bytes=MAX_BYTES, what="SwitchAgent", **kwargs)
        if not update_check._is_newer(release.version, self.current_version):
            raise github_release.DownloadError(f"SwitchAgent {self.current_version} is already the latest version")
        if release.sha256 is None:
            raise github_release.DownloadError("GitHub published no checksum for the update -- not installing it")
        updates = self.app_data_root / UPDATES_FOLDER
        check = _check_installer if self.mode == "installed" else _check_portable

        def progress(done: int, total: int) -> None:
            self._set(phase="downloading", version=release.version, done=done, total=total)

        file = github_release.download(
            release, updates, f"{release.version}-{asset}", max_bytes=MAX_BYTES,
            check=check, progress=progress, **kwargs)

        if self.mode == "portable":
            source = updates / f"SwitchAgent-{release.version}"
            if source.exists():
                import shutil
                shutil.rmtree(source)
            safe_extract(file, source)
        else:
            source = file

        while self.is_busy():
            self._set(phase="waiting", version=release.version)
            time.sleep(BUSY_POLL_SECONDS)

        self._set(phase="installing", version=release.version)
        script = updates / "install-update.ps1"
        script.write_text(_HELPER, encoding="utf-8")
        target = paths.executable_dir()
        self._launch(script, os.getpid(), self.mode, source, target, target / "SwitchAgent.exe")
        log.info("app update %s: helper started, exiting so it can replace the program files", release.version)
        if self.request_exit is not None:
            self.request_exit()


def _launch_helper(script: Path, pid: int, mode: str, source: Path, target: Path, exe: Path) -> None:
    # Never DETACHED_PROCESS: PowerShell started without a console exits at
    # once, status 0, having run nothing (seen on the first real update).
    # CREATE_NO_WINDOW gives it a console nobody sees; it outlives us anyway.
    flags = 0
    for name in ("CREATE_NEW_PROCESS_GROUP", "CREATE_NO_WINDOW"):
        flags |= getattr(subprocess, name, 0)
    subprocess.Popen(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden",
         "-File", str(script), "-WaitPid", str(pid), "-Mode", mode,
         "-Source", str(source), "-Target", str(target), "-Exe", str(exe)],
        creationflags=flags, close_fds=True,
    )
