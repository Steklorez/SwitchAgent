# Third-Party Notices

SwitchAgent is distributed under the MIT License (see `LICENSE`). It uses
the following third-party packages. This list covers SwitchAgent's direct
runtime dependencies (`requirements.txt`) and the build tooling used to
produce the packaged Windows release; each package's own license text
governs its use, and nothing here modifies those licenses. This is a
good-faith summary, not legal advice -- if you plan to redistribute
SwitchAgent commercially or at scale, have it reviewed.

## Runtime dependencies

This list covers direct dependencies AND the transitive ones actually
bundled into the packaged EXE by PyInstaller (verified against a real
build's `dist/SwitchAgent/_internal/` -- not just `requirements.txt`'s
direct entries). Packages used only by the test suite (`pytest`,
`iniconfig`, `pluggy`, `pygments`) and only by `httpx`'s test-client role
(`httpx`, `httpcore`, `certifi`, `idna` -- SwitchAgent's own server code
never makes outbound HTTPS calls) are excluded on purpose; they are
present in `requirements-lock.txt` for reproducible testing but are not
shipped inside the packaged application.

| Package | License | Project |
|---|---|---|
| FastAPI | MIT | https://github.com/fastapi/fastapi |
| Starlette | BSD-3-Clause | https://github.com/Kludex/starlette |
| Uvicorn | BSD-3-Clause | https://uvicorn.dev/ |
| h11 (uvicorn's HTTP/1.1 implementation) | MIT | https://github.com/python-hyper/h11 |
| anyio | MIT | https://github.com/agronholm/anyio |
| Jinja2 | BSD-3-Clause | https://palletsprojects.com/p/jinja/ |
| MarkupSafe (Jinja2 dependency) | BSD-3-Clause | https://github.com/pallets/markupsafe |
| Pydantic + pydantic-core | MIT | https://github.com/pydantic/pydantic |
| annotated-types, typing-extensions, typing-inspection (Pydantic dependencies) | MIT | (various, see PyPI) |
| Click, colorama (uvicorn dependencies) | BSD-3-Clause / BSD-3-Clause | https://github.com/pallets/click, https://github.com/tartley/colorama |
| watchdog | Apache-2.0 | https://github.com/gorakhargosh/watchdog |
| py7zr | LGPL-2.1-or-later | https://py7zr.readthedocs.io/ |
| py7zr's codec dependencies: inflate64, multivolumefile, pybcj, pyppmd | LGPL-2.1-or-later | (various, see PyPI) |
| py7zr's other dependencies: pycryptodomex, texttable, psutil, brotli | BSD / Public Domain / MIT / MIT | (various, see PyPI) |
| rarfile | ISC | https://github.com/markokr/rarfile |
| PyYAML | MIT | https://pyyaml.org/ |
| python-multipart | Apache-2.0 | https://github.com/Kludex/python-multipart |
| pywin32 (+ pywin32-ctypes, used by PyInstaller itself) | PSF License | https://github.com/mhammond/pywin32 |

**py7zr and its codec dependencies (LGPL-2.1-or-later):** SwitchAgent
imports these as ordinary Python package dependencies; none are modified.
Under a PyInstaller build their compiled bytecode/extension modules are
bundled alongside the app rather than kept as separately-replaceable
shared libraries. If you need to exercise the LGPL's right to relink/
replace one of these with a modified version, you can do so in a source
checkout (`pip install -e .` with your own copy) or by replacing the
relevant files under a packaged build's `_internal/` directory --
SwitchAgent never statically compiles any of them in. Their own source
and licenses are unmodified upstream.

## Build tooling (not distributed inside the packaged app)

| Tool | License | Project |
|---|---|---|
| PyInstaller | GPLv2-or-later, **with an explicit exception** permitting use to build and distribute non-GPL (including closed-source or differently-licensed) programs | https://pyinstaller.org |
| Inno Setup | Freeware (custom license, source available) | https://jrsoftware.org/isinfo.php |

PyInstaller's bootloader is linked into the produced `SwitchAgent.exe`,
but PyInstaller's own license explicitly grants an exception for exactly
this case -- see PyInstaller's `COPYING.txt` for the full exception text.
Neither PyInstaller nor Inno Setup source code is distributed as part of
a SwitchAgent release; only their build output (the packaged EXE/installer)
is.

## RAR extraction

SwitchAgent uses the `rarfile` package (ISC license) to read RAR archives
and bundles **7-Zip** (`7z.exe`, `7z.dll`; Copyright Igor Pavlov,
https://www.7-zip.org/) to decompress them. 7-Zip is licensed under the
GNU LGPL; `7z.dll` additionally contains code under the LGPL with the
"unRAR license restriction" (the RAR decoder may not be used to re-create
the RAR compression algorithm) and BSD 2/3-clause code. SwitchAgent only
decompresses. 7-Zip's full license text is shipped as
`vendor/7zip/License.txt` next to the binaries.

## Nintendo Switch / game content

SwitchAgent does not include, bundle, or depend on any Nintendo
firmware, keys, copyrighted assets, or game content. Its application
icon is an original, generic graphic unrelated to Nintendo's branding.
