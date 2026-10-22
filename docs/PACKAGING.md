# Packaging SwitchAgent as a Windows application

This documents how SwitchAgent is packaged for distribution via GitHub
Releases as a normal Windows app -- no Python/pip/pywin32/FastAPI install
required by the end user. It does **not** change any transfer/queue/MTP
semantics; it is a packaging-facing layer on top of the existing,
unmodified core (`switchagent/mtp/`, `queue_worker.py`, `manifest.py`,
`db.py`, `web/`).

## Architecture

| Concern | Module |
|---|---|
| dev / installed / portable runtime-mode detection, resource root vs. writable app-data root | `switchagent/paths.py` |
| Windows Downloads Known Folder resolution | `switchagent/known_folders.py` |
| first-run `config.yaml` bootstrap | `switchagent/firstrun.py` |
| desktop entry point (what `SwitchAgent.exe` actually runs) | `switchagent/desktop.py` |
| Windows named-mutex single-instance guard | `switchagent/single_instance.py` |
| system tray icon (Open / Exit) | `switchagent/tray.py` |
| rotating file logging for the packaged app | `switchagent/logging_setup.py` |
| version (single source of truth) | `switchagent/__init__.py` (reads `importlib.metadata`, ultimately `pyproject.toml`) |

`switchagent/config.py` builds its path constants (`DATA_DIR`, `WORK_DIR`,
`INBOX_DIR`, `LIBRARY_DIR`, `DB_PATH`, `CONFIG_YAML_PATH`, ...) on top of
`paths.py` -- in dev mode every one of them resolves identically to
before `paths.py` existed, so `switch-agent` (the CLI) and existing tests
are unaffected.

### Runtime modes

- **dev**: a source checkout / `pip install -e .`. `sys.frozen` is unset.
  App data lives in the project root, exactly as before this packaging
  work started.
- **installed**: a PyInstaller-frozen build with no `portable.flag` next
  to the EXE (i.e. installed via `SwitchAgent-Setup-x64.exe`). App data
  lives in `%LOCALAPPDATA%\SwitchAgent\` (`data\switchagent.db`, `work\`,
  `logs\`, `config.yaml`).
- **portable**: a PyInstaller-frozen build **with** `portable.flag`
  present next to the EXE (i.e. extracted from
  `SwitchAgent-Portable-x64.zip`). App data lives right next to the EXE,
  so the whole folder stays self-contained and movable.

## Building locally

Prerequisites: Python 3.11+ (tested with 3.14.5) on Windows, and Inno
Setup 6 if you want to build the installer (`winget install
JRSoftware.InnoSetup`, or download from jrsoftware.org).

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install --require-hashes -r requirements-lock.txt
pip install --require-hashes -r requirements-build-lock.txt
pip install -e . --no-deps

# 1. Run the full test suite
pytest -q

# 2. Build the PyInstaller onedir bundle -> dist\SwitchAgent\
pyinstaller --noconfirm packaging\SwitchAgent.spec

# 3. Packaged smoke test -- runs the ACTUAL built EXE, mock mode only
python packaging\smoke_test.py dist\SwitchAgent\SwitchAgent.exe

# 4. Portable ZIP -> dist\SwitchAgent-Portable-x64.zip
python packaging\build_portable_zip.py

# 5. Installer -> dist\installer\SwitchAgent-Setup-x64.exe
#    (auto-detects ISCC.exe; pass --iscc "C:\path\to\ISCC.exe" if needed)
python packaging\build_installer.py
```

`build_installer.py` computes the version passed to Inno Setup the same
way everywhere else does (`importlib.metadata` -> `pyproject.toml`) --
**do not** invoke `ISCC.exe` directly with a hand-typed
`/DMyAppVersion=X.Y.Z`. A release-readiness audit found that this exact
pattern is a real manual-sync risk: a stale copy-pasted version string
would silently build an installer whose `AppVersion` doesn't match the
EXE's own embedded version resource.

All of the above was run and verified end-to-end during development,
including a real install -> launch -> uninstall cycle (confirming
`%LOCALAPPDATA%\SwitchAgent`'s data survives uninstall) and a real
extract -> launch cycle for the portable ZIP (confirming it never
touches `%LOCALAPPDATA%\SwitchAgent`).

## Reproducible dependencies (Stage 10)

The project keeps using plain `pip` + `requirements.txt` (no switch to
Poetry/uv/pipenv -- unnecessary for what this needed). `requirements.txt`
stays the human-edited, floor-pinned (`>=`) file it always was, for
development. Two new files add a minimal lock layer for release builds,
generated with **pip-tools** (`pip-compile`), the standard, minimal
companion for exactly this pip-native use case:

- `requirements-lock.txt` -- exact pinned versions + SHA-256 hashes for
  every runtime dependency (direct and transitive), compiled from
  `requirements.txt`.
- `requirements-build.txt` / `requirements-build-lock.txt` -- the same,
  for build-only tooling (currently just PyInstaller) that is never a
  runtime dependency of `switchagent` itself.

To regenerate after bumping a version in `requirements.txt` /
`requirements-build.txt`:

```powershell
pip install pip-tools
pip-compile --generate-hashes --allow-unsafe --output-file=requirements-lock.txt requirements.txt
pip-compile --generate-hashes --allow-unsafe --output-file=requirements-build-lock.txt requirements-build.txt
```

CI installs with `pip install --require-hashes -r requirements-lock.txt`
-- pip refuses to install anything not present with a matching hash,
which is the actual reproducibility guarantee (not just "a version
number in a file").

## RAR extraction

`rarfile` (already a dependency) parses RAR headers in pure Python, but
decompressing needs an external tool. The build therefore ships 7-Zip's
`7z.exe` + `7z.dll` (and its `License.txt`) under `vendor/7zip/` in the
bundle (source: `packaging/vendor/7zip/`, added in `SwitchAgent.spec`).
`extractor._use_bundled_7zip()` points `rarfile.SEVENZIP_TOOL` at it, so
`.rar` works with nothing installed on the machine.

Redistribution: 7-Zip is LGPL; `7z.dll` also carries the "unRAR license
restriction" (the RAR decoder may not be used to re-create the RAR
compression algorithm -- SwitchAgent only decompresses). Binary
redistribution must reproduce 7-Zip's license text, which is why
`License.txt` travels next to the binaries. See `THIRD_PARTY_NOTICES.md`.

In a source checkout the same files are used from `packaging/vendor/7zip/`.
If they are missing, `rarfile` falls back to `unrar`/`7z`/`bsdtar` on
`PATH`; if none exists, extraction raises
`extractor.ExtractionBackendMissingError`, reported as a per-item error,
and `extractor.rar_backend_available()` (shown on Settings) says so.

To update 7-Zip: replace the three files in `packaging/vendor/7zip/` with
those of a newer 7-Zip install.

## Packaged smoke test (`packaging/smoke_test.py`)

Runs the **actual built** `SwitchAgent.exe` (never the source Python
package) in `--mock --no-browser` mode, in an isolated temp
`%LOCALAPPDATA%`:

1. waits for a real `/api/health` 200 response (bounded retry, fails fast
   if the process exits early instead of just timing out uninformatively);
2. checks one more API endpoint (`/api/queue`);
3. requests a **graceful** shutdown by posting a real `WM_CLOSE` to the
   app's hidden tray window -- the exact same message a user's tray-icon
   "Exit" click sends (see `switchagent/tray.py`), not a hard kill;
4. confirms the process exits with code 0 and its log contains no
   fatal error/traceback.

This is also how a real, easy-to-miss bug was caught during development:
uvicorn's *default* logging setup builds a formatter that touches
`sys.stdout.isatty()`. A **windowed** (`console=False`) PyInstaller build
launched without an inherited console -- i.e. exactly how a real user
double-clicks the EXE or opens it from the Start Menu -- runs with
`sys.stdout`/`sys.stderr` set to `None`, and that crashed the app before
it ever bound a socket. Earlier ad-hoc testing had always redirected
stdout/stderr to a file (`> log.txt`), which happens to keep them
non-`None` and hid the bug completely. The fix
(`switchagent/desktop.py` passes `log_config=None` to `uvicorn.Config`,
and `switchagent/logging_setup.py` attaches its rotating file handler to
the **root** logger so uvicorn's own log records land somewhere safe
too) is covered by regression tests in `tests/test_desktop.py` and
`tests/test_logging_setup.py`, and was re-verified against a real
no-redirection launch of the rebuilt EXE.

## Release tagging (version validation + safe publishing)

A commit + push to `main` **is** a release: `.github/workflows/
release-windows.yml`'s "Determine release tag" step derives the release
tag as `v<pyproject.toml's [project] version>` and, after the build,
publishes a GitHub Release under that tag -- creating the underlying git
tag too, pointed at the exact commit that was built, if it doesn't
already exist. There is no separate manual `git tag && git push --tags`
step; bumping `pyproject.toml`'s version before pushing to `main` is what
determines the next release's version. Pushing an explicit `v*` tag still
works (e.g. to re-publish an older commit) -- in that case the tag is
taken as-is and instead validated, before installing any dependency,
against `pyproject.toml`'s `[project] version` -- `v0.1.0` and
`v0.1.0-rc1` both match a package version of `0.1.0`; a tag whose numbers
actually differ (`v0.2.0` against a package version still at `0.1.0`)
fails the workflow immediately, before any build work happens. A push to
`main` that lands without a version bump since the last release simply
re-publishes the same tag (see the idempotent delete-and-recreate below).

The GitHub Release itself is created as a **draft**, has its three assets
uploaded, and is only then flipped to published. This is deliberate: a
single `gh release create <tag> <assets...>` call uploads assets as
separate steps internally, and a failure partway through would leave a
live, published release missing an asset -- exactly the "partially
published release" a release-readiness audit specifically checked for.
Staying a draft until every asset is confirmed uploaded means a failure
here leaves, at worst, an invisible draft. The same step also deletes any
pre-existing release for the same tag first (never the underlying git
tag), so re-running the workflow after a previous partial failure is
idempotent instead of hard-failing on "release already exists". A tag
with a pre-release suffix (`-rc1`, `-beta1`, ...) is automatically
published as a GitHub pre-release.

## Code signing (Stage 12)

Not required for a build to succeed. If the repository's secrets
`WINDOWS_CERTIFICATE_BASE64` (a base64-encoded `.pfx`) and
`WINDOWS_CERTIFICATE_PASSWORD` are both set, `.github/workflows/
release-windows.yml` signs `SwitchAgent.exe` and the installer with
`signtool.exe` (timestamped against DigiCert's TSA), verifies the
signature, and rebuilds the portable ZIP from the now-signed EXE.
Without those secrets, the workflow builds a normal unsigned release --
it never fails just because signing isn't configured.

**An unsigned download will trigger a Windows SmartScreen warning.**
Signing does not automatically remove that: a brand-new certificate has
no reputation with Microsoft yet, and SmartScreen's reputation system
builds up over time/download volume regardless of whether the binary is
signed. Do not promise users a signature alone fixes SmartScreen.

## Known limitations / what still needs real-hardware or manual verification

- The system tray icon's actual `_TrayWindow` class (window creation,
  `PumpMessages()`, popup menu) has its pure message-routing logic covered
  by automated tests (`tests/test_tray.py`); a human should still click
  through Open/right-click-menu/Exit at least once on a machine that
  hasn't run it before.
- `SwitchAgent.exe`'s real (non-mock) MTP backend has been exercised
  against a physically connected Switch: packaged real-mode startup,
  device discovery, the Devices/Diagnostics/Queue/History pages, and a
  graceful shutdown with the same device rediscovered on restart all
  confirmed working.
- Authenticode signing itself requires a certificate (`WINDOWS_CERTIFICATE_BASE64`/
  `WINDOWS_CERTIFICATE_PASSWORD` repo secrets); without them the workflow
  builds a normal unsigned release.
- The Game/Device Details and update-check pages behave
  correctly in both installed and portable packaged modes -- portable
  mode keeps `config.yaml`/`data/`/`logs/`/`runtime.json`/`portable.flag`
  next to the EXE (never under `%LOCALAPPDATA%`), confirming
  `switchagent/paths.py`'s dev/installed/portable app-data-root resolution
  works correctly rather than hardcoding an installed-mode path.
