# PyInstaller spec -- onedir build of the packaged desktop app.
#
# Build locally with:
#   pyinstaller --noconfirm packaging/SwitchAgent.spec
# (run from the project root, with the project installed in the active
# environment -- `pip install -e .` -- so collect_data_files/copy_metadata
# below can find switchagent's installed package data and metadata).
#
# Produces dist/SwitchAgent/ (onedir bundle) with SwitchAgent.exe at its
# root -- see docs/PACKAGING.md for the full local build + release process.
#
# Deliberately NOT --onefile at this stage (see the packaging task this
# was written for): onedir is the production format; onefile can be
# explored later, once onedir is proven by the packaged smoke test.

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, copy_metadata
from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo,
    StringFileInfo,
    StringStruct,
    StringTable,
    VarFileInfo,
    VarStruct,
    VSVersionInfo,
)

PACKAGING_DIR = Path(SPECPATH).resolve()
PROJECT_ROOT = PACKAGING_DIR.parent

sys.path.insert(0, str(PROJECT_ROOT))
from importlib.metadata import version as _pkg_version  # noqa: E402

APP_VERSION = _pkg_version("switchagent")
ICON_PATH = PROJECT_ROOT / "switchagent" / "web" / "static" / "app.ico"


def _version_tuple(version_str: str) -> tuple[int, int, int, int]:
    parts = []
    for chunk in version_str.split("+")[0].split(".")[:4]:
        digits = "".join(c for c in chunk if c.isdigit())
        parts.append(int(digits) if digits else 0)
    while len(parts) < 4:
        parts.append(0)
    return tuple(parts[:4])


_VERSION_TUPLE = _version_tuple(APP_VERSION)

_VERSION_INFO = VSVersionInfo(
    ffi=FixedFileInfo(
        filevers=_VERSION_TUPLE,
        prodvers=_VERSION_TUPLE,
        mask=0x3F,
        flags=0x0,
        OS=0x40004,       # VOS_NT_WINDOWS32
        fileType=0x1,     # VFT_APP
        subtype=0x0,
        date=(0, 0),
    ),
    kids=[
        StringFileInfo([
            StringTable("040904B0", [
                StringStruct("CompanyName", "SwitchAgent project"),
                StringStruct("FileDescription", "SwitchAgent -- local Switch MTP file manager for DBI"),
                StringStruct("FileVersion", APP_VERSION),
                StringStruct("InternalName", "SwitchAgent"),
                StringStruct("LegalCopyright", "SwitchAgent project"),
                StringStruct("OriginalFilename", "SwitchAgent.exe"),
                StringStruct("ProductName", "SwitchAgent"),
                StringStruct("ProductVersion", APP_VERSION),
            ]),
        ]),
        VarFileInfo([VarStruct("Translation", [1033, 1200])]),
    ],
)

# -- data files --------------------------------------------------------------
#
# Preserves switchagent/web/{templates,static}'s package-relative layout
# inside the bundle -- switchagent/web/app.py's `_WEB_DIR =
# Path(__file__).resolve().parent` lookup then just works unmodified
# inside the frozen bundle too (see switchagent/paths.py's module
# docstring): no application code needs to know about sys._MEIPASS.
datas = collect_data_files("switchagent.web", includes=["templates/*", "static/*", "static/addon-icons/*", "addons/*"])

# copy_metadata is what makes importlib.metadata.version("switchagent")
# (switchagent/__init__.py's single source of truth for __version__)
# keep working inside the frozen build -- without this, the dist-info
# directory a normal `pip install` creates simply isn't there, and the
# lookup would silently fall back to the "0.0.0+unknown" placeholder.
datas += copy_metadata("switchagent")

# Which source this build is: branch, commit, uncommitted changes, when
# (switchagent/build_info.py). Written under build/, never into the source
# tree, and shipped next to build_info.py, where it looks for it -- a local
# test build then says what it is instead of passing for the release.
import json as _json  # noqa: E402
from switchagent import build_info as _build_info  # noqa: E402

_BUILD_FILE = PROJECT_ROOT / "build" / "_build.json"
_BUILD_FILE.parent.mkdir(parents=True, exist_ok=True)
_BUILD_FILE.write_text(_json.dumps(_build_info.collect(PROJECT_ROOT)), encoding="utf-8")
datas += [(str(_BUILD_FILE), "switchagent")]

# Proven-necessary hidden imports only (see this file's own history / the
# packaging report for what was actually observed missing during a real
# packaged run -- not a speculative list). uvicorn resolves its event
# loop / HTTP protocol implementations dynamically by name at runtime,
# which PyInstaller's static import analysis cannot see on its own.
hiddenimports = [
    "uvicorn.loops.auto",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
    "win32timezone",
]

a = Analysis(
    [str(PACKAGING_DIR / "run_desktop.py")],
    pathex=[str(PROJECT_ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="SwitchAgent",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # windowed -- no console window on double-click
    icon=str(ICON_PATH),
    version=_VERSION_INFO,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="SwitchAgent",
)
