"""The Add-ons tab and installing its utilities (switchagent/addons.py,
web/addons/catalog.yaml, web/addons_service.py, web/addons_views.py).

The catalog is edited by hand; these tests are what stops an entry that
could not be shown truthfully -- or that could write anywhere near the
boot chain -- from reaching the page. The release layouts below are the
real ones (2026-09-26): Ultrahand's sdout.zip carries nx-ovlloader,
SaltyNX and Fizeau ship settings files a person edits, FPSLocker and JKSV
are one file each, named by the NACP inside.
"""

from __future__ import annotations

import copy
import hashlib
import json
import struct
import time

import pytest
import yaml
from fastapi.testclient import TestClient

from switchagent import addons, config, db, github_release, known_folders, manifest as manifest_mod, nro, scanner
from switchagent.model import ContentType
from switchagent.mtp import MockMtpBackend
from switchagent.web import addons_views, services
from switchagent.web.addons_service import AddonInstaller
from switchagent.web.app import create_app
from switchagent.web.context import build_mock_context

from .conftest import build_zip


def make_nro(name: str, version: str = "", author: str = "") -> bytes:
    body = bytearray(0x100)
    body[0x10:0x14] = b"NRO0"
    struct.pack_into("<I", body, 0x18, len(body))
    nacp = bytearray(0x4000)
    for i in range(16):
        nacp[i * 0x300:i * 0x300 + len(name)] = name.encode()
        nacp[i * 0x300 + 0x200:i * 0x300 + 0x200 + len(author)] = author.encode()
    nacp[0x3060:0x3060 + len(version)] = version.encode()
    aset = bytearray(0x38)
    aset[0:4] = b"ASET"
    struct.pack_into("<QQ", aset, 0x18, 0x38, len(nacp))
    return bytes(body) + bytes(aset) + bytes(nacp)


def ultrahand_sdout(version="2.5.3") -> dict[str, bytes]:
    return {
        "switch/.overlays/ovlmenu.ovl": make_nro("Ultrahand", version, "ppkantorski"),
        "switch/Ultrahand-Reload/Ultrahand-Reload.nro": make_nro("Ultrahand-Reload", "1.0"),
        "atmosphere/contents/420000000007E51A/exefs.nsp": b"loader",
        "atmosphere/contents/420000000007E51A/toolbox.json": b"{}",
        "atmosphere/contents/420000000007E51A/flags/boot2.flag": b"",
        "atmosphere/contents/420000000007E51B/exefs.nsp": b"loader2",
        "atmosphere/exefs_patches/audio_mastervolume/3a75b4a6.ips": b"IPS",
        "config/ultrahand/lang/ru.json": b"{}",
        "config/ultrahand/themes/ultra.ini": b"[theme]",
    }


def saltynx_zip() -> dict[str, bytes]:
    return {
        "SaltySD/exceptions.txt": b"X0100000000010000\n",
        "SaltySD/saltynx_core.elf": b"core",
        "atmosphere/contents/0000000000534C56/exefs.nsp": b"salty",
        "atmosphere/contents/0000000000534C56/flags/boot2.flag": b"",
        "atmosphere/exefs_patches/SaltyNX_Fixes/IDs.txt": b"ids",
    }


def fizeau_zip(version="2.8.3") -> dict[str, bytes]:
    return {
        "atmosphere/contents/0100000000000F12/exefs.nsp": b"fizeau",
        "atmosphere/contents/0100000000000F12/flags/boot2.flag": b"",
        "atmosphere/exefs_patches/nvnflinger_cmu/86837cae.ips": b"IPS",
        "config/Fizeau/config.ini": b"[profile1]\ntemperature = 6500\n",
        "switch/Fizeau/Fizeau.nro": make_nro("Fizeau", version),
        "switch/.overlays/Fizeau.ovl": make_nro("Fizeau", version),
    }


def files_of(entries):
    return [(name, len(data)) for name, data in entries.items()]


# ---------------------------------------------------------------------------
# The catalog
# ---------------------------------------------------------------------------

def _doc():
    return yaml.safe_load(addons.CATALOG_PATH.read_text(encoding="utf-8"))


def test_the_shipped_catalog_keeps_every_rule():
    catalog = addons.load_catalog()
    ids = [a.id for a in catalog]
    assert ids[:10] == ["ultrahand", "emuiibo", "status-monitor", "fpslocker", "saltynx", "reversenx-rt",
                        "fizeau", "quickntp", "jksv", "moonlight"]
    assert len(ids) == 26 and {"amiigo", "nxthemes", "themezer", "nxmp", "noods", "turnips"} <= set(ids)
    by_id = {a.id: a for a in catalog}
    assert by_id["emuiibo"].install == "emuiibo" and by_id["emuiibo"].requires == ("ultrahand",)
    assert all(a.spec is not None for a in catalog if a.id != "emuiibo")
    assert by_id["ultrahand"].spec.kefir
    assert [a.id for a in addons.install_order(["fpslocker"])] == ["ultrahand", "saltynx", "fpslocker"]
    assert [a.id for a in addons.install_order(["reversenx-rt", "fpslocker"])] == \
        ["ultrahand", "saltynx", "reversenx-rt", "fpslocker"]


def _ultrahand(d):
    return d["addons"][0]["install"]


def _entry(d, addon_id):
    return next(e for e in d["addons"] if e["id"] == addon_id)


@pytest.mark.parametrize("breakage,message", [
    (lambda d: d["addons"][1].update(requires=["nope"]), "does not have"),
    (lambda d: d["addons"][2].update(id="emuiibo"), "used twice"),
    (lambda d: d["addons"][1].update(status="dbi"), "unknown `status`"),
    (lambda d: d["addons"][1].update(install="tesla"), "unknown `install`"),
    (lambda d: d["addons"][1].update(preview="missing.png"), "not a file"),
    (lambda d: d["addons"][1].update(source="http://example.com"), "https"),
    (lambda d: d["addons"][1].pop("usage"), "`usage`"),
    (lambda d: d["addons"][1].update(related={"label": "x", "href": "https://elsewhere"}), "in-app"),
    (lambda d: d["addons"][1].update(id="Emu iibo"), "lowercase"),
    # What an install block may never name.
    (lambda d: _ultrahand(d)["places"].append("bootloader/payloads/"), "not a place an add-on may write"),
    (lambda d: _ultrahand(d)["places"].append("atmosphere/package3"), "not a place an add-on may write"),
    (lambda d: _ultrahand(d)["places"].append("atmosphere/config/system_settings.ini"), "not a place"),
    (lambda d: _ultrahand(d)["places"].append("switch/"), "too broad"),
    (lambda d: _ultrahand(d)["places"].append("atmosphere/contents/"), "too broad"),
    (lambda d: _ultrahand(d)["places"].append("payload.bin"), "root of the card"),
    (lambda d: _ultrahand(d)["places"].append("config/../bootloader/"), "plain"),
    (lambda d: _ultrahand(d)["sign"].append("switch/elsewhere.ovl"), "not a file inside its `places`"),
    (lambda d: _ultrahand(d).update(repo="not a repo"), "owner/name"),
    (lambda d: d["addons"][2]["install"].update(to="switch/.overlays/x.nro"), "its own extension"),
    (lambda d: d["addons"][0].update(requires=["fpslocker"]), "requires itself"),
    (lambda d: _entry(d, "nxmp")["install"]["layout"].update({"nxmp/": "bootloader/payloads/"}), "not a place"),
    (lambda d: _entry(d, "nxmp")["install"]["layout"].update({"../x/": "switch/x/"}), "plain folder"),
    (lambda d: _entry(d, "nxmp")["install"].update(layout={"nxmp/": "switch/nxmp/nxmp.nro"}), "folder ending in /"),
])
def test_an_entry_the_tab_could_not_show_truthfully_is_refused(breakage, message):
    doc = copy.deepcopy(_doc())
    breakage(doc)
    with pytest.raises(addons.CatalogError, match=message):
        addons.parse_catalog(doc)


def test_catalog_text_is_escaped_and_backticks_become_code():
    assert str(addons.rich("copy `SdOut` to <root> & restart")) == \
        "copy <code>SdOut</code> to &lt;root&gt; &amp; restart"


# ---------------------------------------------------------------------------
# A release, recognised by what is in it
# ---------------------------------------------------------------------------

def test_real_release_layouts_are_recognised_and_nothing_else():
    match = addons.match_archive(files_of(ultrahand_sdout()))
    assert match.addon_id == "ultrahand" and match.version_source == "switch/.overlays/ovlmenu.ovl"
    assert {dest for _src, dest, _size in match.files} == set(ultrahand_sdout())
    assert addons.match_archive(files_of(saltynx_zip())).keep == ("SaltySD/exceptions.txt",)
    fizeau = addons.match_archive(files_of(fizeau_zip()))
    assert fizeau.addon_id == "fizeau" and fizeau.keep == ("config/Fizeau/config.ini",)
    # Re-packed inside one folder: still Fizeau, laid out from inside it.
    wrapped = addons.match_archive([(f"Fizeau-2.8.3/{n}", s) for n, s in files_of(fizeau_zip())])
    assert wrapped.addon_id == "fizeau"
    assert ("Fizeau-2.8.3/config/Fizeau/config.ini", "config/Fizeau/config.ini") in \
        {(src, dest) for src, dest, _ in wrapped.files}
    # nx-ovlloader on its own is not Ultrahand (no menu in it).
    loader_only = {k: v for k, v in ultrahand_sdout().items() if "ovlmenu" not in k}
    assert addons.match_archive(files_of(loader_only)) is None
    # One file outside its places and it is not the release the catalog knows.
    assert addons.match_archive(files_of({**ultrahand_sdout(), "bootloader/hekate_ipl.ini": b"x"})) is None
    assert addons.match_archive(files_of({**saltynx_zip(), "README.md": b"x"})) is None


def test_one_overlay_or_app_is_known_by_the_name_inside_it():
    fps = make_nro("FPSLocker", "3.3.2", "masagrator")
    match = addons.match_program("FPSLocker-3.3.2-final.ovl", nro.nro_info_from_bytes(fps), len(fps))
    assert match.addon_id == "fpslocker"
    assert match.files == (("FPSLocker-3.3.2-final.ovl", "switch/.overlays/FPSLocker.ovl", len(fps)),)
    jksv = make_nro("JKSV", "12.02.2025")
    assert addons.match_program("JKSV.nro", nro.nro_info_from_bytes(jksv), 1).files[0][1] == "switch/JKSV/JKSV.nro"
    # The name decides, not the file name; and an .nro is never an overlay.
    tesla = make_nro("Tesla Menu", "1.2.3")
    assert addons.match_program("ovlmenu.ovl", nro.nro_info_from_bytes(tesla), 1) is None
    assert addons.match_program("FPSLocker.nro", nro.nro_info_from_bytes(fps), 1) is None
    assert addons.match_program("FPSLocker.ovl", None, 1) is None


# ---------------------------------------------------------------------------
# In the Library, and through the queue
# ---------------------------------------------------------------------------

def _rows(conn):
    return {row["absolute_path"]: row for row in db.list_library_items(conn)}


def _console():
    backend = MockMtpBackend(device_id="mock-switch-parent", device_name="Parent's Switch")
    backend.add_storage("SD_CARD")
    backend.add_storage("SD_INSTALL")
    backend.connect()
    sd = backend.storage_tree("SD_CARD")

    def put(path, data=b""):
        sd.ensure_directory(path.rpartition("/")[0])
        sd.write_file(path, data)

    return backend, put


def _run_worker(conn, backend):
    from switchagent import queue_worker

    registry = queue_worker.DeviceRegistry()
    registry.register(backend.device_id, backend)
    for _ in range(20):
        if queue_worker.run_worker_once(conn, registry) is None:
            return


def test_add_ons_in_the_library_are_add_ons_not_games(isolated_db):
    conn, _ = isolated_db
    build_zip(config.LIBRARY_DIR / "sdout.zip", ultrahand_sdout())
    (config.LIBRARY_DIR / "FPSLocker.ovl").write_bytes(make_nro("FPSLocker", "3.3.2"))
    (config.LIBRARY_DIR / "some other overlay.ovl").write_bytes(make_nro("Something", "1.0"))
    build_zip(config.LIBRARY_DIR / "SaltyNX.zip", saltynx_zip())
    scanner.scan_library_once(conn)
    rows = _rows(conn)
    ultrahand = rows[str(config.LIBRARY_DIR / "sdout.zip")]
    assert ultrahand["content_type"] == ContentType.ADDON.value and ultrahand["status"] == "AVAILABLE"
    details = json.loads(ultrahand["details_json"])["addon"]
    assert (details["id"], details["version"]) == ("ultrahand", "2.5.3")
    assert json.loads(rows[str(config.LIBRARY_DIR / "FPSLocker.ovl")]["details_json"])["addon"]["id"] == "fpslocker"
    # SaltyNX is not "a mod" of program 0000000000534C56.
    assert rows[str(config.LIBRARY_DIR / "SaltyNX.zip")]["content_type"] == ContentType.ADDON.value
    # An overlay that is not the catalog's has nowhere known to go: not indexed.
    assert str(config.LIBRARY_DIR / "some other overlay.ovl") not in rows
    view = services.list_library_view(conn, kind="games")
    assert view["games"] == [] and view["on_addons_tab"] == 3


def test_installing_keeps_a_persons_settings_and_replaces_everything_else(isolated_db):
    conn, _ = isolated_db
    zip_path = build_zip(config.LIBRARY_DIR / "Fizeau 2.8.3.zip", fizeau_zip())
    scanner.scan_library_once(conn)
    row = _rows(conn)[str(zip_path)]
    backend, put = _console()
    put("config/Fizeau/config.ini", b"[profile1]\ntemperature = 3200\n")        # the owner's own night filter
    put("switch/.overlays/Fizeau.ovl", make_nro("Fizeau", "2.8.1"))              # an older release
    put("switch/.overlays/Status-Monitor-Overlay.ovl", b"somebody else's")
    result = services.create_and_confirm_jobs(conn, [row["id"]], backend.device_id)
    assert result["errors"] == []
    _run_worker(conn, backend)
    job = db.get_job(conn, result["created"][0]["job_id"])
    assert job["status"] == "DONE" and "your own settings kept" in job["error"]
    sd = backend.storage_tree("SD_CARD")
    assert sd.read_file("config/Fizeau/config.ini") == b"[profile1]\ntemperature = 3200\n"
    assert nro.nro_info_from_bytes(sd.read_file("switch/.overlays/Fizeau.ovl")).version == "2.8.3"
    assert sd.read_file("switch/.overlays/Status-Monitor-Overlay.ovl") == b"somebody else's"
    assert sd.exists("atmosphere/contents/0100000000000F12/flags/boot2.flag")
    manifest = manifest_mod.load_manifest(job["id"])
    assert manifest.addon_id == "fizeau" and manifest.keep == ("config/Fizeau/config.ini",)
    assert services._job_view(conn, job)["variant_role"] == "addon"


def test_a_console_without_the_settings_file_gets_the_shipped_one(isolated_db):
    conn, _ = isolated_db
    zip_path = build_zip(config.LIBRARY_DIR / "Fizeau 2.8.3.zip", fizeau_zip())
    scanner.scan_library_once(conn)
    backend, _put = _console()
    result = services.create_and_confirm_jobs(conn, [_rows(conn)[str(zip_path)]["id"]], backend.device_id)
    _run_worker(conn, backend)
    assert db.get_job(conn, result["created"][0]["job_id"])["error"] is None
    assert backend.storage_tree("SD_CARD").read_file("config/Fizeau/config.ini").startswith(b"[profile1]")


def test_a_manifest_never_writes_outside_the_add_ons_places(tmp_path):
    from switchagent.preview import PreviewReport

    (tmp_path / "x.ovl").write_bytes(b"x")
    report = PreviewReport(content_type=ContentType.ADDON, source="x", addon_id="fpslocker",
                           copy_root=tmp_path, copy_plan=[("x.ovl", "bootloader/payloads/x.bin")])
    with pytest.raises(manifest_mod.ManifestError, match="may not be written"):
        manifest_mod._build_copy_plan_files(report, tmp_path)
    report.addon_id = "not-in-the-catalog"
    with pytest.raises(manifest_mod.ManifestError, match="not an add-on of the catalog"):
        manifest_mod._build_copy_plan_files(report, tmp_path)


# ---------------------------------------------------------------------------
# On a console
# ---------------------------------------------------------------------------

def _read(backend, known=None):
    state = None
    for state in addons.read_device(backend, "SD_CARD", addons.load_catalog(), known=known):
        pass
    return state


def test_reading_a_console_says_what_it_has_with_versions():
    backend, put = _console()
    for path, data in ultrahand_sdout().items():
        put(path, data)
    put("switch/.overlays/FPSLocker.ovl", make_nro("FPSLocker", "3.3.2"))
    put("atmosphere/contents/0000000000534C56/exefs.nsp", b"salty")
    state = _read(backend)
    assert state.complete and not state.kefir
    by_id = {a.id: a for a in addons.load_catalog()}
    assert addons.status(by_id["ultrahand"], state)["label"] == "installed — 2.5.3"
    assert addons.status(by_id["fpslocker"], state)["version"] == "3.3.2"
    assert addons.status(by_id["saltynx"], state)["state"] == "installed"
    assert addons.status(by_id["fizeau"], state)["state"] == "missing"
    assert addons.status(by_id["ultrahand"], None)["state"] == "unknown"


def test_tesla_menu_in_ultrahands_place_is_said_and_kefir_is_noticed():
    backend, put = _console()
    put("switch/.overlays/ovlmenu.ovl", make_nro("Tesla Menu", "1.2.3", "WerWolv"))
    put("atmosphere/contents/420000000007E51A/exefs.nsp", b"loader")
    put(addons.KEFIR_SIGN, make_nro("kefir-updater", "1.0"))
    state = _read(backend)
    found = addons.status(addons.by_id("ultrahand"), state)
    assert found["state"] == "other" and found["label"] == "Tesla Menu 1.2.3 is in its place"
    assert state.kefir


def test_a_program_is_read_back_only_when_it_changed():
    backend, put = _console()
    put("switch/.overlays/FPSLocker.ovl", make_nro("FPSLocker", "3.3.2"))
    first = _read(backend)
    reads = []
    original = backend.read_file

    def counting(storage, path, **kw):
        reads.append(path)
        return original(storage, path, **kw)

    backend.read_file = counting
    again = _read(backend, known=addons.ConsoleAddons.from_dict(first.to_dict()))
    assert reads == [] and again.programs == first.programs
    put("switch/.overlays/FPSLocker.ovl", make_nro("FPSLocker", "3.3.10-longer") + b"more")
    _read(backend, known=again)
    assert reads == ["switch/.overlays/FPSLocker.ovl"]


def test_a_program_is_known_by_its_nacp_wherever_a_person_put_it():
    # A real kefir card: sphaira in the Homebrew Menu's place, folders
    # spelled otherwise than the catalog spells them.
    backend, put = _console()
    put("hbmenu.nro", make_nro("sphaira", "1.0.7", "TotalJustice"))
    put("switch/NxThemesInstaller/NxThemesInstaller.nro", make_nro("NXThemes Installer", "2.8.0"))
    put("switch/ovlEdiZon.ovl", make_nro("Tesla Menu", "1.0"))
    put("switch/.overlays/ovlEdiZon.ovl", make_nro("EdiZon", "v1.0.15-a12ce33"))
    put("switch/retroarch_switch.nro", make_nro("RetroArch", "1.19"))
    state = _read(backend)
    by_id = {a.id: a for a in addons.load_catalog()}
    sphaira = addons.status(by_id["sphaira"], state)
    assert sphaira["state"] == "elsewhere" and sphaira["label"] == "installed — 1.0.7 as hbmenu.nro"
    assert addons.is_installed(by_id["sphaira"], state)
    themes = addons.status(by_id["nxthemes"], state)
    assert themes["state"] == "installed" and themes["version"] == "2.8.0"
    assert addons.status(by_id["edizon"], state)["state"] == "installed"
    assert addons.status(by_id["checkpoint"], state)["state"] == "missing"
    # Named once, then remembered while its size stays the same.
    reads = []
    original = backend.read_file
    backend.read_file = lambda storage, path, **kw: reads.append(path) or original(storage, path, **kw)
    again = _read(backend, known=addons.ConsoleAddons.from_dict(state.to_dict()))
    assert reads == [] and addons.status(by_id["sphaira"], again)["state"] == "elsewhere"


# ---------------------------------------------------------------------------
# Install: from GitHub, requirements first, all or nothing
# ---------------------------------------------------------------------------

class _Response:
    def __init__(self, data: bytes, url: str):
        self._data, self._pos, self._url = data, 0, url

    def read(self, n=-1):
        chunk = self._data[self._pos:] if n is None or n < 0 else self._data[self._pos:self._pos + n]
        self._pos += len(chunk)
        return chunk

    def geturl(self):
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _zip_bytes(entries):
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


class FakeGitHub:
    """GitHub's release API and asset downloads for several repositories,
    each served after a redirect to release-assets.githubusercontent.com."""

    def __init__(self, releases: dict[str, tuple[str, str, bytes]], *, bad_digest=(), stars=None):
        self.releases = releases
        self.bad_digest = set(bad_digest)
        self.stars = stars or {}
        self.requests = []

    def __call__(self, request, timeout=None):
        import urllib.error

        url = request.full_url
        self.requests.append(url)
        for repo, (tag, asset, data) in self.releases.items():
            if url == f"{github_release.API_ROOT}/{repo}":
                return _Response(json.dumps({"stargazers_count": self.stars.get(repo, 100)}).encode(), url)
            if url == github_release.latest_release_api(repo):
                digest = "0" * 64 if repo in self.bad_digest else hashlib.sha256(data).hexdigest()
                return _Response(json.dumps({
                    "tag_name": tag, "draft": False, "prerelease": False,
                    "html_url": f"https://github.com/{repo}/releases/tag/{tag}",
                    "assets": [
                        {"name": "Debug.zip", "size": 3, "browser_download_url": f"https://github.com/{repo}/d.zip"},
                        {"name": asset, "size": len(data), "digest": "sha256:" + digest,
                         "browser_download_url": f"https://github.com/{repo}/releases/download/{tag}/{asset}"},
                    ],
                }).encode(), url)
            if url.startswith(f"https://github.com/{repo}/releases/download/"):
                return _Response(data, "https://release-assets.githubusercontent.com/x")
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)


def _fake_github(**kw):
    return FakeGitHub({
        "ppkantorski/Ultrahand-Overlay": ("v2.5.3", "sdout.zip", _zip_bytes(ultrahand_sdout())),
        "masagrator/SaltyNX": ("1.9.3", "SaltyNX.zip", _zip_bytes(saltynx_zip())),
        "masagrator/FPSLocker": ("3.3.2", "FPSLocker.ovl", make_nro("FPSLocker", "3.3.2")),
        "J-D-K/JKSV": ("12/02/2025", "JKSV.nro", make_nro("JKSV", "12.02.2025")),
    }, **kw)


class _RecordingPreparations:
    def __init__(self):
        self.submitted = []

    def submit(self, item_ids, target, **_kwargs):
        self.submitted.append((list(item_ids), target))


def _db_path(conn):
    return conn.execute("PRAGMA database_list").fetchone()["file"]


def _run(installer, device, ids):
    installer.start(device, ids)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        state = installer.snapshot()
        if state["state"] in ("done", "failed"):
            return state
        time.sleep(0.02)
    raise AssertionError("the install never finished")


def test_an_install_brings_its_requirements_first_and_queues_them_together(isolated_db):
    conn, _ = isolated_db
    db_path = _db_path(conn)
    preparations = _RecordingPreparations()
    installer = AddonInstaller(db_path, preparations, opener=_fake_github())
    state = _run(installer, "mock-switch-parent", ["ultrahand", "saltynx", "fpslocker"])
    assert state["state"] == "done", state["error"]
    assert [s["state"] for s in state["steps"]] == ["queued"] * 3
    assert [s["version"] for s in state["steps"]] == ["2.5.3", "1.9.3", "3.3.2"]
    downloads = config.LIBRARY_DIR / github_release.DOWNLOADS_FOLDER
    assert sorted(p.name for p in downloads.iterdir()) == \
        ["FPSLocker 3.3.2.ovl", "SaltyNX 1.9.3.zip", "Ultrahand Overlay 2.5.3.zip"]
    rows = _rows(conn)
    ids = [rows[str(downloads / n)]["id"] for n in
           ("Ultrahand Overlay 2.5.3.zip", "SaltyNX 1.9.3.zip", "FPSLocker 3.3.2.ovl")]
    assert preparations.submitted == [(ids, "mock-switch-parent")]
    # Already there with GitHub's checksum: used as it is, not fetched again.
    github = _fake_github()
    installer._opener = github
    _run(installer, "mock-switch-parent", ["fpslocker"])
    assert not any("/releases/download/" in u for u in github.requests)


def test_a_version_tag_with_slashes_still_makes_a_plain_file_name(isolated_db):
    conn, _ = isolated_db
    db_path = _db_path(conn)
    state = _run(AddonInstaller(db_path, _RecordingPreparations(), opener=_fake_github()), "x", ["jksv"])
    assert state["state"] == "done", state["error"]
    assert (config.LIBRARY_DIR / github_release.DOWNLOADS_FOLDER / "JKSV 12-02-2025.nro").is_file()


def test_one_failed_download_queues_nothing(isolated_db):
    conn, _ = isolated_db
    db_path = _db_path(conn)
    preparations = _RecordingPreparations()
    installer = AddonInstaller(db_path, preparations, opener=_fake_github(bad_digest={"masagrator/SaltyNX"}))
    state = _run(installer, "mock-switch-parent", ["ultrahand", "saltynx", "fpslocker"])
    assert state["state"] == "failed" and preparations.submitted == []
    assert [s["state"] for s in state["steps"]] == ["ready", "failed", "skipped"]
    assert "checksum" in state["steps"][1]["error"]


def test_a_download_that_is_not_the_add_on_is_refused(isolated_db):
    conn, _ = isolated_db
    db_path = _db_path(conn)
    github = FakeGitHub({"masagrator/FPSLocker": ("3.3.2", "FPSLocker.ovl", make_nro("Something Else", "1"))})
    state = _run(AddonInstaller(db_path, _RecordingPreparations(), opener=github), "x", ["fpslocker"])
    assert state["state"] == "failed" and "is not FPSLocker" in state["error"]
    assert not list((config.LIBRARY_DIR / github_release.DOWNLOADS_FOLDER).glob("*"))


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------

@pytest.fixture
def bare_client(tmp_path, monkeypatch):
    (tmp_path / "library").mkdir()
    (tmp_path / "inbox").mkdir()
    monkeypatch.setattr(config, "INBOX_DIR", tmp_path / "inbox")
    monkeypatch.setattr(config, "WORK_DIR", tmp_path / "work")
    monkeypatch.setattr(config, "LIBRARY_DIR", tmp_path / "library")
    monkeypatch.setattr(config, "CONFIG_YAML_PATH", tmp_path / "config.yaml")
    monkeypatch.setattr(known_folders, "downloads_dir", lambda: None)
    db_path = tmp_path / "test.db"
    ctx = build_mock_context(db_path, seed_emuiibo=True)
    with db.open_db(db_path) as conn:
        scanner.scan_library_once(conn)
        ctx.refresh_devices(conn)
        while ctx.emuiibo.step(conn, ctx.registry):
            pass
    test_client = TestClient(create_app(ctx))
    test_client.ctx = ctx
    return test_client


@pytest.fixture
def client(bare_client):
    """The Add-ons tab is a beta feature: these tests have it turned on."""
    from switchagent import preferences

    preferences.set_beta(True)
    return bare_client


def _fingerprint(client, name):
    return next(d["device_fingerprint"] for d in client.get("/api/devices").json() if name in d["display_name"])


def test_the_list_is_short_and_each_add_on_has_a_page_of_its_own(client):
    html = client.get("/addons").text
    assert 'href="/addons" class="active"' in html
    for addon in addons.load_catalog():
        assert f'id="addon-{addon.id}"' in html and f'href="/addons/{addon.id}?device=' in html
        assert addon.icon and f"/static/{addon.icon}" in html
    # The instructions are on the add-on's own page, not the list.
    assert "Using it" not in html and "<kbd>" not in html
    page = client.get("/addons/emuiibo").text
    assert "Using it" in page and "When something is wrong" in page
    assert "<kbd>R3</kbd>" in page
    assert "<code>atmosphere/contents/0100000000000352</code>" in page
    assert "addon-emuiibo-overlay.svg" in page
    assert 'href="/addons/ultrahand' in page                      # what it needs links to its page
    assert client.get("/addons/nope").status_code == 404


def test_every_add_on_has_an_icon_of_its_own():
    for addon in addons.load_catalog():
        assert addon.icon and (addons.STATIC_DIR / addon.icon).is_file(), addon.id


def test_a_switch_with_old_emuiibo_is_offered_the_update(client):
    parent = _fingerprint(client, "Parent")
    html = client.get(f"/addons?device={parent}").text
    assert "installed — 0.6.3 (outdated, 1.1.3 is current)" in html
    assert f'data-device="{parent}"' in html and ">Update<" in html


def test_a_bare_switch_is_offered_each_add_on_with_what_it_needs(client):
    child = _fingerprint(client, "Child")
    with db.open_db(client.ctx.db_path) as conn:
        view = addons_views.page(conn, client.ctx, child)
    entries = {e["addon"].id: e for e in view["entries"]}
    assert entries["fpslocker"]["with"] == ["Ultrahand Overlay", "SaltyNX"]
    assert entries["emuiibo"]["with"] == ["Ultrahand Overlay"]
    assert entries["jksv"]["with"] == []
    assert all(e["can_install"] and e["button"] == "Install" for e in entries.values())
    html = client.get(f"/addons?device={child}").text
    assert "with Ultrahand Overlay, SaltyNX" in html and 'data-addon="moonlight"' in html


def test_what_kefir_keeps_updated_is_left_to_it(client):
    parent = _fingerprint(client, "Parent")
    with db.open_db(client.ctx.db_path) as conn:
        device_id = services.resolve_target_device_id(conn, parent)
        db.set_device_addons(conn, device_id, {
            "files": {"switch/.overlays/ovlmenu.ovl": 10, "atmosphere/contents/420000000007e51a/exefs.nsp": 5,
                      addons.KEFIR_SIGN.lower(): 3},
            "programs": {"switch/.overlays/ovlmenu.ovl": {"name": "Ultrahand", "version": "2.5.0", "size": 10}},
            "kefir": True, "checked": [p.lower() for p in addons.console_paths(addons.load_catalog())],
        })
        view = addons_views.page(conn, client.ctx, parent)
    ultrahand = next(e for e in view["entries"] if e["addon"].id == "ultrahand")
    assert ultrahand["managed"] and not ultrahand["can_install"]
    assert ultrahand["status"]["label"] == "installed — 2.5.0 (kefir keeps it up to date)"
    assert "runs kefir" in client.get(f"/addons?device={parent}").text


def test_install_over_the_api_plans_the_chain(client, monkeypatch):
    started = []
    monkeypatch.setattr(client.ctx.emuiibo_downloads, "start",
                        lambda device_id, ids: started.append((device_id, list(ids))) or {"state": "checking"})
    child = _fingerprint(client, "Child")
    assert client.post("/api/addons/install", json={"device": child, "addon": "reversenx-rt"}).status_code == 202
    assert started[-1][1] == ["ultrahand", "saltynx", "reversenx-rt"]
    assert client.post("/api/addons/install", json={"device": child, "addon": "nope"}).status_code == 404
    assert client.get("/api/addons/activity").json() == {"download": None}


# ---------------------------------------------------------------------------
# Updates: GitHub's latest release, asked at start and once a day
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("latest,installed,newer", [
    ("1.4.1+r4", "1.4.0", True),          # Status Monitor on the real OLED
    ("v2.5.3", "2.4.4", True),            # Ultrahand: kefir's is older
    ("3.3.2", "3.3.2", False),
    ("1.4.1+r4", "1.4.1+r4", False),
    ("1.4.1+r4", "1.4.1", True),          # a rebuild of 1.4.1 is newer
    ("12/02/2025", "12.02.2025", False),  # JKSV: the tag and its NACP, one date
    ("01/15/2026", "12.02.2025", True),   # month/day/year, compared as a date
    ("1.10.0", "1.9.3", True),
    ("nightly", "1.0", False),
    ("1.0", None, False),
])
def test_versions_compare_the_way_releases_are_named(latest, installed, newer):
    assert addons.is_newer(latest, installed) is newer


def _fake_github_with_emuiibo():
    github = _fake_github()
    github.releases.update({
        "XorTroll/emuiibo": ("1.1.3", "emuiibo.zip", b"zip"),
        "ppkantorski/Status-Monitor-Overlay": ("v1.4.1+r4", "Status-Monitor-Overlay.ovl", b"ovl"),
    })
    return github


def test_the_latest_releases_are_asked_of_github_and_remembered(tmp_path):
    from switchagent.web.addons_service import ReleaseChecker

    cache = tmp_path / "addon_releases.json"
    github = _fake_github_with_emuiibo()
    checker = ReleaseChecker(cache, opener=github)
    snapshot = checker.check_now()
    releases = snapshot["releases"]
    assert releases["ultrahand"]["version"] == "2.5.3"
    assert releases["status-monitor"]["version"] == "1.4.1+r4"
    assert releases["emuiibo"]["version"] == "1.1.3"
    assert releases["jksv"]["version"] == "12/02/2025"
    assert "fizeau" not in releases           # GitHub (here) does not answer for it: not guessed
    # Only the API (releases, and each repo's stars) -- nothing is downloaded by a check.
    assert all(u.startswith(github_release.API_ROOT) for u in github.requests)
    assert not any("/releases/download/" in u for u in github.requests)
    assert snapshot["stars"]["ultrahand"] == 100
    # Stars already known are asked again only after a week; releases every check.
    github.requests.clear()
    checker.check_now()
    known = {f"{github_release.API_ROOT}/{repo}" for repo in github.releases}
    assert github.requests and not known & set(github.requests)
    assert snapshot["checked_at"] and not snapshot["checking"]
    # After a restart the page has them before the next check.
    again = ReleaseChecker(cache, opener=FakeGitHub({}))
    assert again.latest("ultrahand")["version"] == "2.5.3"


def test_the_checker_runs_at_start_and_then_waits_a_day(tmp_path):
    from switchagent.web.addons_service import ReleaseChecker

    github = _fake_github_with_emuiibo()
    checker = ReleaseChecker(tmp_path / "c.json", opener=github, interval_seconds=3600, enabled=lambda: True)
    checker.start()
    deadline = time.monotonic() + 10
    while checker.snapshot()["checked_at"] is None and time.monotonic() < deadline:
        time.sleep(0.02)
    checker.stop()
    assert checker.latest("fpslocker")["version"] == "3.3.2"
    assert len([u for u in github.requests if "FPSLocker/releases/latest" in u]) == 1


def _set_console(client, name, files, programs, *, kefir=False):
    fingerprint = _fingerprint(client, name)
    with db.open_db(client.ctx.db_path) as conn:
        device_id = services.resolve_target_device_id(conn, fingerprint)
        db.set_device_addons(conn, device_id, {
            "files": {**{f.lower(): 1 for f in files}, **({addons.KEFIR_SIGN.lower(): 1} if kefir else {})},
            "programs": {p.lower(): v for p, v in programs.items()}, "kefir": kefir,
            "checked": [p.lower() for p in addons.console_paths(addons.load_catalog())],
        })
    return fingerprint


def test_an_older_add_on_on_the_console_gets_an_update_button(client):
    client.ctx.addon_releases._releases = {
        "status-monitor": {"version": "1.4.1+r4"}, "fpslocker": {"version": "3.3.2"},
        "jksv": {"version": "12/02/2025"}, "ultrahand": {"version": "2.5.3"},
    }
    child = _set_console(client, "Child", [
        "switch/.overlays/Status-Monitor-Overlay.ovl", "switch/.overlays/FPSLocker.ovl", "switch/JKSV/JKSV.nro",
    ], {
        "switch/.overlays/Status-Monitor-Overlay.ovl": {"name": "Status Monitor", "version": "1.4.0", "size": 1},
        "switch/.overlays/FPSLocker.ovl": {"name": "FPSLocker", "version": "3.3.2", "size": 1},
        "switch/JKSV/JKSV.nro": {"name": None, "version": None, "size": 10_000_000},
    })
    with db.open_db(client.ctx.db_path) as conn:
        entries = {e["addon"].id: e for e in addons_views.page(conn, client.ctx, child)["entries"]}
    monitor = entries["status-monitor"]
    assert monitor["button"] == "Update to 1.4.1+r4" and monitor["can_install"]
    assert monitor["status"]["label"] == "installed — 1.4.0 · 1.4.1+r4 is available"
    assert entries["fpslocker"]["button"] == "Installed" and not entries["fpslocker"]["can_install"]
    # Too big to read back, and not installed by SwitchAgent: nobody knows.
    assert entries["jksv"]["button"] == "Reinstall latest" and entries["jksv"]["can_install"]
    html = client.get(f"/addons?device={child}").text
    assert ">Update to 1.4.1+r4<" in html


def test_what_kefir_updates_gets_no_update_button(client):
    client.ctx.addon_releases._releases = {"ultrahand": {"version": "2.5.3"}}
    parent = _set_console(client, "Parent", [
        "switch/.overlays/ovlmenu.ovl", "atmosphere/contents/420000000007E51A/exefs.nsp",
    ], {"switch/.overlays/ovlmenu.ovl": {"name": "Ultrahand", "version": "2.4.4", "size": 1}}, kefir=True)
    with db.open_db(client.ctx.db_path) as conn:
        ultrahand = next(e for e in addons_views.page(conn, client.ctx, parent)["entries"] if e["addon"].id == "ultrahand")
    assert ultrahand["update"] is None and not ultrahand["can_install"]


def test_emuiibo_is_outdated_against_githubs_release_when_known(client):
    parent = _fingerprint(client, "Parent")        # emuiibo 0.6.3 on it
    client.ctx.addon_releases._releases = {"emuiibo": {"version": "1.2.0"}}
    html = client.get(f"/addons?device={parent}").text
    assert "installed — 0.6.3 (outdated, 1.2.0 is current)" in html and ">Update to 1.2.0<" in html


def test_what_switchagent_installed_itself_knows_its_version(isolated_db):
    conn, _ = isolated_db
    (config.LIBRARY_DIR / "JKSV.nro").write_bytes(make_nro("JKSV", "12.02.2025"))
    scanner.scan_library_once(conn)
    backend, _put = _console()
    row = _rows(conn)[str(config.LIBRARY_DIR / "JKSV.nro")]
    services.create_and_confirm_jobs(conn, [row["id"]], backend.device_id)
    _run_worker(conn, backend)
    assert addons_views.installed_by_us(conn, backend.device_id, addons.by_id("jksv")) == "12.02.2025"
    assert addons_views.installed_by_us(conn, backend.device_id, addons.by_id("moonlight")) is None


# ---------------------------------------------------------------------------
# A beta feature: off by default, turned on in Settings after a warning
# ---------------------------------------------------------------------------

def test_the_add_ons_tab_is_off_until_the_beta_features_are_turned_on(bare_client):
    from switchagent import preferences

    client = bare_client
    assert preferences.load()["beta"] is False
    assert 'href="/addons"' not in client.get("/").text
    moved = client.get("/addons", follow_redirects=False)
    assert (moved.status_code, moved.headers["location"]) == (303, "/settings#beta")
    child = _fingerprint(client, "Child")
    refused = client.post("/api/addons/install", json={"device": child, "addon": "fpslocker"})
    assert refused.status_code == 403 and "beta" in refused.json()["detail"]
    settings = client.get("/settings").text
    assert 'id="beta-toggle"' in settings and "checked" not in settings.split('id="beta-toggle"')[1][:20]
    assert "Turn on experimental features?" in settings

    assert client.post("/api/preferences/beta", json={"enabled": True}).json() == {"beta": True}
    assert 'href="/addons"' in client.get("/").text
    assert client.get("/addons", follow_redirects=False).status_code == 200
    # Saving the other preferences keeps it.
    preferences.save(True, 60, True)
    assert preferences.beta_enabled()


def test_emuiibo_and_its_menu_are_not_beta(bare_client, monkeypatch):
    client = bare_client
    started = []
    monkeypatch.setattr(client.ctx.emuiibo_downloads, "start",
                        lambda device_id, ids: started.append(list(ids)) or {"state": "checking"})
    child = _fingerprint(client, "Child")
    assert client.post("/api/addons/install", json={"device": child, "addon": "ultrahand"}).status_code == 202
    assert client.post("/api/amiibo/emuiibo/download", json={"device": child}).status_code == 202
    assert started == [["ultrahand"], ["ultrahand", "emuiibo"]]


# ---------------------------------------------------------------------------
# Releases not laid out like the SD card, and what archives carry along
# ---------------------------------------------------------------------------

def test_a_release_laid_out_its_own_way_is_moved_where_the_catalog_says():
    nxmp = {"nxmp/nxmp.nro": 66_688_940, "nxmp/config.ini": 769, "nxmp/eqpresets.ini": 608,
            "nxmp/langs/nxmp-ru.json": 3653, "nxmp/mpv/subfont.ttf": 6_244_952}
    match = addons.match_archive(list(nxmp.items()))
    assert match.addon_id == "nxmp"
    assert ("nxmp/langs/nxmp-ru.json", "switch/nxmp/langs/nxmp-ru.json", 3653) in match.files
    assert match.keep == ("switch/nxmp/config.ini", "switch/nxmp/eqpresets.ini")
    assert match.version_source == "nxmp/nxmp.nro"
    # NooDS: a bare noods.nro at the root of its zip. Turnips: inside out/.
    assert addons.match_archive([("noods.nro", 10)]).files == (("noods.nro", "switch/noods/noods.nro", 10),)
    assert addons.match_archive([("out/Turnips.nro", 10)]).files ==         (("out/Turnips.nro", "switch/Turnips/Turnips.nro", 10),)
    # A file its layout does not cover: not that release.
    assert addons.match_archive([*nxmp.items(), ("README.md", 1)]) is None


def test_what_a_mac_or_explorer_adds_to_an_archive_is_ignored():
    files = [*files_of(saltynx_zip()), ("__MACOSX/SaltySD/._exceptions.txt", 120), ("SaltySD/.DS_Store", 6),
             ("Thumbs.db", 9)]
    match = addons.match_archive(files)
    assert match.addon_id == "saltynx"
    assert not any("__MACOSX" in src or src.endswith((".DS_Store", "Thumbs.db")) for src, _d, _s in match.files)


@pytest.mark.parametrize("version,key", [
    ("v1.0.15-a12ce33", (1, 0, 15)),      # EdiZon Overlay's NACP
    ("1.7.7-6c8fda4", (1, 7, 7)),         # Turnips'
    ("1.4.1+r4", (1, 4, 1, 4)),
    ("2.0-1234567", (2, 0, 1234567)),     # all digits: a number, not a hash
])
def test_a_builds_commit_hash_is_not_part_of_its_version(version, key):
    assert addons.version_key(version) == key


def test_an_entry_the_last_read_did_not_look_for_is_not_called_missing(bare_client):
    """A read made before an entry joined the catalog never looked for its
    files: "not checked yet", never "not installed"."""
    from switchagent import preferences

    preferences.set_beta(True)
    parent = _fingerprint(bare_client, "Parent")
    with db.open_db(bare_client.ctx.db_path) as conn:
        device_id = services.resolve_target_device_id(conn, parent)
        stored = db.get_device_addons(conn, device_id)[0]
        stored["checked"] = [p for p in stored["checked"] if "fpslocker" not in p]
        db.set_device_addons(conn, device_id, stored)
        entries = {e["addon"].id: e for e in addons_views.page(conn, bare_client.ctx, parent)["entries"]}
    assert entries["fpslocker"]["status"]["state"] == "unknown"
    assert entries["fpslocker"]["status"]["label"].startswith("not checked yet")
    assert entries["fizeau"]["status"]["state"] == "missing"


def _run_loop_once(checker):
    checker.start()
    time.sleep(0.3)
    checker.stop()


def test_with_the_beta_features_off_nothing_goes_to_github(tmp_path):
    from switchagent.web.addons_service import ReleaseChecker

    github = _fake_github_with_emuiibo()
    _run_loop_once(ReleaseChecker(tmp_path / "c.json", opener=github, interval_seconds=3600))  # preferences: off
    assert github.requests == []


def test_a_start_soon_after_the_last_check_does_not_ask_again(tmp_path):
    from switchagent.web.addons_service import ReleaseChecker

    cache = tmp_path / "c.json"
    ReleaseChecker(cache, opener=_fake_github_with_emuiibo(), enabled=lambda: True).check_now()
    github = _fake_github_with_emuiibo()
    _run_loop_once(ReleaseChecker(cache, opener=github, interval_seconds=3600, enabled=lambda: True))
    assert github.requests == []
