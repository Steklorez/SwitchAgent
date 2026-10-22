"""Tests for the Web UI's FastAPI layer (switchagent/web/). Uses
MockMtpBackend throughout (via context.build_mock_context) -- no real
hardware, no pywin32 calls, consistent with the rest of the project's mock
mode. The background worker thread is deliberately never started in these
tests (ctx.start_worker() is not called) -- queue processing is driven
explicitly via queue_worker.run_worker_once() so outcomes are
deterministic, not timing-dependent. queue_worker.py's own extensive
transfer-semantics coverage (test_queue_worker.py) is not duplicated here;
these tests are about the HTTP/service layer's contracts and safety
invariants.
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from switchagent import config, db, known_folders, queue_worker
from switchagent.web.app import create_app
from switchagent.web.context import build_mock_context

from .conftest import build_zip, choose_library_folder


@pytest.fixture
def web_ctx(tmp_path, monkeypatch):
    inbox_dir = tmp_path / "inbox"
    work_dir = tmp_path / "work"
    library_dir = tmp_path / "library"
    inbox_dir.mkdir()
    library_dir.mkdir()
    monkeypatch.setattr(config, "INBOX_DIR", inbox_dir)
    monkeypatch.setattr(config, "WORK_DIR", work_dir)
    monkeypatch.setattr(config, "LIBRARY_DIR", library_dir)
    # Isolates config.library_dir_info()/get_settings() from this
    # machine's real config.yaml and real Windows Downloads folder --
    # without these, /api/settings would silently read the real project
    # config.yaml (CONFIG_YAML_PATH's stale default) and call the real
    # SHGetKnownFolderPath, neither of which belongs in a unit test.
    monkeypatch.setattr(config, "CONFIG_YAML_PATH", tmp_path / "config.yaml")
    monkeypatch.setattr(known_folders, "downloads_dir", lambda: None)

    db_path = tmp_path / "test.db"
    with db.open_db(db_path) as conn:
        pass  # just run schema/migrations once

    ctx = build_mock_context(db_path)
    # Seed the device cache once, standing in for the worker thread's first
    # tick (see WebContext.get_known_devices()/refresh_devices() -- request
    # threads only ever read the cache now, never touch COM directly, so
    # something has to populate it at least once for device-listing tests
    # to see anything; ctx.start_worker() is deliberately never started in
    # these tests for determinism, see the module docstring).
    with db.open_db(db_path) as conn:
        ctx.refresh_devices(conn)
    yield ctx


@pytest.fixture
def client(web_ctx):
    app = create_app(web_ctx)
    return TestClient(app)


def _add_game(library_dir, name="Game [0100000000010000][v0].nsp", content=b"payload"):
    path = library_dir / name
    path.write_bytes(content)
    return path


# ---------------------------------------------------------------------------
# devices
# ---------------------------------------------------------------------------

def test_list_devices_never_touches_com_from_a_request_thread(client, web_ctx):
    """Regression guard for a real-hardware incident (docs/STATE.md's
    Quake II investigation): a request thread calling refresh_devices()
    (which calls RealMtpBackend.connect(), touching live COM objects) at
    the same time the worker thread is mid-transfer on the SAME backend
    instance invalidated its cached COM references, crashing the job.
    Fixed by having request threads only ever read
    WebContext.get_known_devices() (no COM). Poisoning refresh_devices()
    here proves the request-handling path never calls it."""
    def _must_not_be_called(*a, **k):
        raise AssertionError("refresh_devices() must never be called from a request thread")
    web_ctx.refresh_devices = _must_not_be_called

    assert client.get("/api/devices").status_code == 200
    assert client.get("/").status_code == 200
    assert client.get("/queue").status_code == 200
    assert client.get("/devices").status_code == 200
    assert client.get("/settings").status_code == 200


def test_get_known_devices_starts_empty_until_a_refresh_happens(tmp_path):
    """A brand new context (before anything -- worker or test setup -- has
    ever called refresh_devices()) must not report devices it has never
    actually observed."""
    db_path = tmp_path / "fresh.db"
    with db.open_db(db_path):
        pass
    fresh_ctx = build_mock_context(db_path)
    assert fresh_ctx.get_known_devices() == []


def test_get_known_devices_reflects_last_worker_refresh(web_ctx):
    """get_known_devices() (what request threads read) only ever reflects
    what refresh_devices() (worker-thread-only) last observed -- proves
    the cache handoff itself works, independent of the FastAPI layer.
    (The web_ctx fixture already seeds it once, see its own comment --
    this re-refreshes explicitly to prove the mechanism itself, not just
    the fixture's one-time seed.)"""
    with db.open_db(web_ctx.db_path) as conn:
        web_ctx.refresh_devices(conn)
    ids = {d.device_id for d in web_ctx.get_known_devices()}
    assert ids == {"mock-switch-parent", "mock-switch-child"}


def test_list_devices_returns_mock_backends(client):
    res = client.get("/api/devices")
    assert res.status_code == 200
    devices = res.json()
    ids = {d["device_id"] for d in devices}
    assert ids == {"mock-switch-parent", "mock-switch-child"}
    assert all(d["connected"] for d in devices)


def test_device_label_never_exposes_the_raw_serial_bearing_device_id(client, web_ctx):
    """Regression guard found during real-hardware testing: device_id is a
    WPD path string containing the device's real USB serial number (see
    mtp/windows.py's mask_device_id) -- Queue/History/Library detail must
    show a friendly name or fingerprint on pages a human reads, never that
    raw string, even though the raw device_id is still fine (and needed)
    as a form value / API payload field."""
    from switchagent.web import services

    real_looking_id = r"::{GUID}\\?\usb#vid_057e&pid_201d#xtj10229424075#{6ac27878-a6fa-4155-ba85-f98f491d4f33}"
    with db.open_db(web_ctx.db_path) as conn:
        db.upsert_device_seen(conn, real_looking_id, "Switch")
        label = services.device_label(conn, real_looking_id)
    assert "xtj10229424075" not in label

    with db.open_db(web_ctx.db_path) as conn:
        db.set_device_friendly_name(conn, real_looking_id, "Моя Switch")
        label2 = services.device_label(conn, real_looking_id)
    assert label2 == "Моя Switch"


def test_devices_page_rename_placeholder_never_exposes_raw_device_id(client, web_ctx):
    """ARCH-001 regression: the Devices page's rename-form placeholder used
    to fall back to the raw device_id (`d.last_known_display_name or
    d.device_id`) whenever a device had never reported a display name --
    since device_id embeds the real USB serial on real hardware, that raw,
    serial-bearing string could render as VISIBLE placeholder text. It must
    fall back to the already-safe, fingerprint-based display_name field
    instead (services.list_devices()'s `friendly_name or
    last_known_display_name or f"Switch ({fingerprint})"`).

    Note: the raw device_id legitimately still appears elsewhere on this
    same page as HTML attribute VALUES (data-device-id, the rename
    data-url) -- that is the established, already-tested safe pattern
    (functionally required for devices.js, never rendered as visible text),
    so this test targets the placeholder attribute specifically rather than
    asserting the raw id is absent from the whole page."""
    import re

    from switchagent.mtp.windows import device_fingerprint

    real_looking_id = r"::{GUID}\\?\usb#vid_057e&pid_201d#xtj10229424075#{6ac27878-a6fa-4155-ba85-f98f491d4f33}"
    with db.open_db(web_ctx.db_path) as conn:
        # display_name=None -- reproduces the exact bug precondition: no
        # last_known_display_name has ever been recorded for this device.
        db.upsert_device_seen(conn, real_looking_id, None)

    html = client.get("/devices").text
    placeholders = re.findall(r'placeholder="([^"]*)"', html)
    assert f"Switch ({device_fingerprint(real_looking_id)})" in placeholders
    assert not any("xtj10229424075" in p for p in placeholders)


def test_queue_and_history_pages_never_render_raw_device_id(client, web_ctx):
    item_id = _seed_library_item(web_ctx)
    res = client.post("/api/jobs", json={"library_item_ids": [item_id], "target_device_id": "mock-switch-parent"})
    job_id = res.json()["created"][0]["job_id"]
    with db.open_db(web_ctx.db_path) as conn:
        queue_worker.run_worker_once(conn, web_ctx.registry)

    for path in ("/queue", "/history"):
        html = client.get(path).text
        assert "mock-switch-parent" not in html  # the raw id must not appear as visible text
    job = client.get(f"/api/jobs/{job_id}").json()
    assert job["target_device_label"] and job["target_device_label"] != "mock-switch-parent"


def test_rename_device_sets_friendly_name_not_identity(client):
    client.get("/api/devices")  # first sighting -- populates the devices table row
    res = client.post("/api/devices/mock-switch-parent/rename", json={"friendly_name": "Моя Switch"})
    assert res.status_code == 200

    devices = client.get("/api/devices").json()
    parent = next(d for d in devices if d["device_id"] == "mock-switch-parent")
    assert parent["friendly_name"] == "Моя Switch"
    assert parent["device_id"] == "mock-switch-parent"


def test_rename_unknown_device_404s(client):
    res = client.post("/api/devices/never-seen/rename", json={"friendly_name": "X"})
    assert res.status_code == 404


def test_rename_unknown_device_404_never_echoes_raw_serial_in_detail(client):
    """ARCH-001 regression: db.set_device_friendly_name()'s "unknown
    device_id" ValueError used to embed the raw device_id verbatim
    (f"...{device_id!r}"), and app.py's api_rename_device() forwards
    str(exc) straight into the HTTPException's `detail` field -- an API
    response body a real client could display verbatim (e.g. devices.js's
    storage-mapping handler already does `alert(data.detail)` for a sibling
    endpoint). It must be masked, same as every other device-identity
    string this API surface can emit."""
    real_looking_id = r"::{GUID}\\?\usb#vid_057e&pid_201d#xtj10229424075#{6ac27878-a6fa-4155-ba85-f98f491d4f33}"
    res = client.post(f"/api/devices/{real_looking_id}/rename", json={"friendly_name": "X"})
    assert res.status_code == 404
    assert "xtj10229424075" not in res.text


# ---------------------------------------------------------------------------
# UI-007: device storage mapping. Worker-owned discovery
# (WebContext.refresh_devices/get_known_storages) is exercised end-to-end
# here through the real web_ctx fixture (which already calls
# refresh_devices() once, see its own comment) -- no live COM involved,
# MockMtpBackend throughout.
# ---------------------------------------------------------------------------

def test_web_ctx_fixture_refresh_already_populated_the_storage_cache(web_ctx):
    """The fixture's one-time refresh_devices() call (see its own comment)
    must have populated get_known_storages() too, not just the device
    list -- proves refresh_devices()'s new storage-snapshot logic runs on
    the very first call regardless of the refresh interval."""
    storages = web_ctx.get_known_storages("mock-switch-parent")
    names = {s.name for s in storages}
    assert names == {"SD_CARD", "SD_INSTALL"}


def test_get_known_storages_is_empty_for_a_never_seen_device(web_ctx):
    assert web_ctx.get_known_storages("never-seen-device") == []


def test_api_list_device_storages_reports_auto_mapping_by_default(client, web_ctx):
    body = client.get("/api/devices/mock-switch-parent/storages").json()
    names = {s["effective_logical_name"] for s in body}
    assert names == {"SD_CARD", "SD_INSTALL"}
    assert all(s["mapping_source"] == "AUTO" for s in body)


def test_set_storage_mapping_rejects_nand_target(client, web_ctx):
    res = client.post(
        "/api/devices/mock-switch-parent/storages/mapping",
        json={"raw_storage_name": "SD_CARD", "logical_name": "NAND_USER"},
    )
    assert res.status_code == 409
    assert "not an allowed manual mapping target" in res.json()["detail"]


def test_set_storage_mapping_rejects_unknown_raw_storage_name(client, web_ctx):
    res = client.post(
        "/api/devices/mock-switch-parent/storages/mapping",
        json={"raw_storage_name": "NOT_A_REAL_STORAGE", "logical_name": "SD_CARD"},
    )
    assert res.status_code == 409
    assert "not a currently known storage" in res.json()["detail"]


def test_set_storage_mapping_rejects_duplicate_logical_name_on_same_device(client, web_ctx):
    """Two raw storages both manually mapped to SD_INSTALL on the same
    device would make one of them silently unreachable (the backend picks
    whichever matches first) -- must be refused."""
    web_ctx.storage_refresh_interval_seconds = 0  # force an immediate re-snapshot below
    parent = web_ctx.registry.get("mock-switch-parent")
    parent.add_storage("SD_INSTALL_2", raw_name="9: Weird Vendor Name")
    with db.open_db(web_ctx.db_path) as conn:
        web_ctx.refresh_devices(conn)  # re-snapshot so the new storage is known

    first = client.post(
        "/api/devices/mock-switch-parent/storages/mapping",
        json={"raw_storage_name": "9: Weird Vendor Name", "logical_name": "SD_INSTALL"},
    )
    assert first.status_code == 200

    second = client.post(
        "/api/devices/mock-switch-parent/storages/mapping",
        json={"raw_storage_name": "SD_CARD", "logical_name": "SD_INSTALL"},
    )
    assert second.status_code == 409
    assert "already manually mapped" in second.json()["detail"]


def test_set_and_clear_storage_mapping_round_trip(client, web_ctx):
    web_ctx.storage_refresh_interval_seconds = 0  # force immediate re-snapshots below
    parent = web_ctx.registry.get("mock-switch-parent")
    parent.add_storage("SD_INSTALL_2", raw_name="9: Weird Vendor Name")
    with db.open_db(web_ctx.db_path) as conn:
        web_ctx.refresh_devices(conn)

    set_res = client.post(
        "/api/devices/mock-switch-parent/storages/mapping",
        json={"raw_storage_name": "9: Weird Vendor Name", "logical_name": "SD_INSTALL"},
    )
    assert set_res.status_code == 200

    with db.open_db(web_ctx.db_path) as conn:
        web_ctx.refresh_devices(conn)  # picks the override back up (loaded every tick)
    body = client.get("/api/devices/mock-switch-parent/storages").json()
    mapped = next(s for s in body if s["raw_name"] == "9: Weird Vendor Name")
    # mapping_source is computed by services.py cross-referencing
    # device_storage_mappings directly, independent of what the backend
    # itself reports -- proven here. effective_logical_name (what the
    # BACKEND resolves the raw name to) is deliberately NOT asserted to
    # have changed: MockMtpBackend's own set_storage_overrides()
    # intentionally does not affect its routing/reporting (see its own
    # docstring) -- only RealMtpBackend actually resolves overrides,
    # which cannot be exercised without real hardware/COM.
    assert mapped["mapping_source"] == "MANUAL"

    clear_res = client.post(
        "/api/devices/mock-switch-parent/storages/mapping/clear",
        json={"raw_storage_name": "9: Weird Vendor Name"},
    )
    assert clear_res.status_code == 200
    with db.open_db(web_ctx.db_path) as conn:
        web_ctx.refresh_devices(conn)
    body2 = client.get("/api/devices/mock-switch-parent/storages").json()
    cleared = next(s for s in body2 if s["raw_name"] == "9: Weird Vendor Name")
    assert cleared["mapping_source"] == "AUTO"


def test_devices_page_renders_storage_table(client, web_ctx):
    html = client.get("/devices").text
    assert "storage-table" in html
    assert "SD_CARD" in html
    assert "SD_INSTALL" in html
    assert "storage-mapping-save" in html


def test_refresh_devices_loads_overrides_into_the_backend_every_tick(web_ctx):
    """Proves WebContext.refresh_devices() actually calls
    backend.set_storage_overrides() with the mapping loaded from
    device_storage_mappings -- inspected via MockMtpBackend's own
    operation_log (see its SET_STORAGE_OVERRIDES entry)."""
    with db.open_db(web_ctx.db_path) as conn:
        db.set_device_storage_mapping(conn, "mock-switch-parent", "9: Weird Vendor Name", "SD_INSTALL")
        web_ctx.refresh_devices(conn)

    parent = web_ctx.registry.get("mock-switch-parent")
    calls = [e for e in parent.operation_log if e.operation == "SET_STORAGE_OVERRIDES"]
    assert calls[-1].details["overrides"] == {"9: Weird Vendor Name": "SD_INSTALL"}


# ---------------------------------------------------------------------------
# library
# ---------------------------------------------------------------------------

def test_scan_then_list_library(client, web_ctx):
    _add_game(config.LIBRARY_DIR)
    res = client.post("/api/scan")
    assert res.status_code == 200
    assert res.json()["started"] is True

    import time
    for _ in range(50):
        status = client.get("/api/scan/status").json()
        if not status["running"]:
            break
        time.sleep(0.2)
    assert status["running"] is False
    assert status["error"] is None

    library = client.get("/api/library").json()
    assert len(library) == 1
    assert library[0]["title_id"] == "0100000000010000"
    assert library[0]["status"] == "AVAILABLE"


def test_scan_status_snapshot_is_idle_before_any_scan_ever_ran(web_ctx):
    assert web_ctx.scan_status_snapshot()["status"] == "idle"


def test_scan_status_snapshot_reports_running_with_elapsed_seconds(web_ctx):
    from switchagent.web.context import ScanState
    with web_ctx.scan_lock:
        web_ctx.scan_state = ScanState(running=True, started_at=db.now_iso(), current_filename="Game.nsp")
    snap = web_ctx.scan_status_snapshot()
    assert snap["status"] == "running"
    assert snap["current_filename"] == "Game.nsp"
    assert snap["elapsed_seconds"] is not None and snap["elapsed_seconds"] >= 0


def test_scan_status_snapshot_reports_completed_vs_failed(web_ctx):
    from switchagent.web.context import ScanState
    started = db.now_iso()
    with web_ctx.scan_lock:
        web_ctx.scan_state = ScanState(
            running=False, started_at=started, finished_at=db.now_iso(),
            summary={"new": 1, "updated": 0, "removed": 0},
        )
    ok = web_ctx.scan_status_snapshot()
    assert ok["status"] == "completed"
    assert ok["elapsed_seconds"] is not None

    with web_ctx.scan_lock:
        web_ctx.scan_state = ScanState(
            running=False, started_at=started, finished_at=db.now_iso(), error="disk full",
        )
    failed = web_ctx.scan_status_snapshot()
    assert failed["status"] == "failed"


def test_scan_reports_current_filename_progress_end_to_end(client, web_ctx):
    """Poll budget matches test_scan_then_list_library's own convention
    (50 x 0.2s = 10s) -- scanner.is_file_stable() does a real
    config.STABLE_CHECK_INTERVAL_SECONDS (2s) sleep per candidate file, so
    even this 2-file scan genuinely takes several seconds, not
    milliseconds."""
    _add_game(config.LIBRARY_DIR, name="Game1 [0100000000010000][v0].nsp")
    _add_game(config.LIBRARY_DIR, name="Game2 [0100000000020000][v0].nsp")
    res = client.post("/api/scan")
    assert res.status_code == 200
    assert res.json()["started"] is True

    import time
    status = None
    for _ in range(50):
        status = client.get("/api/scan/status").json()
        if not status["running"]:
            break
        time.sleep(0.2)
    assert status["running"] is False
    assert status["status"] == "completed"
    # Cleared once finished -- never left showing a stale filename after
    # the scan is actually done (a 2-file scan may well complete between
    # two polls faster than this test can ever observe it mid-flight --
    # same honest "fast work may not be caught mid-flight" reality as
    # Phase 4's batch-install tests -- so mid-scan current_filename isn't
    # asserted here, only that it's never stale once done).
    assert status["current_filename"] is None


def test_other_pages_remain_responsive_while_scan_runs_in_background(client, web_ctx):
    """Point 13/16: a Rescan running in its own thread must never block
    Library/Queue/History/Devices/Settings or device polling -- scanning
    touches only the filesystem + its own short-lived DB connection, never
    WebContext's device cache or any COM object. 3 files (each costing a
    real ~2s is_file_stable() sleep, see the test above) is enough to give
    a comfortably wide window where the scan is still definitely running
    when the immediate burst of requests below fires -- no need for more."""
    for i in range(3):
        _add_game(config.LIBRARY_DIR, name=f"Game{i}.nsp", content=f"payload-{i}".encode())
    res = client.post("/api/scan")
    assert res.json()["started"] is True

    for path in ("/", "/queue", "/history", "/devices", "/settings", "/api/devices", "/api/queue"):
        assert client.get(path).status_code == 200

    import time
    for _ in range(50):
        if not client.get("/api/scan/status").json()["running"]:
            break
        time.sleep(0.2)


# ---------------------------------------------------------------------------
# Library filesystem watcher (point 12): config.LIBRARY_DIR had NO automatic
# watcher before this -- only cli.py's separate `scan --watch` (a standalone
# CLI process watching the OLDER config.INBOX_DIR pipeline, left untouched).
# WebContext.start_library_watcher() reuses that same watchdog+debounce
# pattern AND (unlike the CLI path) Phase 8's run_scan_in_background(), so a
# debounced filesystem change ends up in exactly the same place a manual
# Rescan click does -- no second scanning code path to keep in sync.
# ---------------------------------------------------------------------------

def test_library_watcher_does_not_start_automatically(web_ctx):
    """Mirrors start_worker()'s own determinism guarantee -- constructing a
    WebContext (e.g. this fixture) must never have the side effect of
    watching the filesystem; every other test in this module relies on
    that (none of them expect a background rescan to ever fire)."""
    assert web_ctx.library_watcher_running is False


def test_watcher_does_not_run_against_a_folder_nobody_chose(web_ctx):
    """config.LIBRARY_DIR falls back to the Windows Downloads folder when
    config.yaml names none -- a fine suggestion to prefill Settings with,
    and a terrible thing to start walking and re-walking unasked: it is
    mostly junk, takes minutes per pass, and none of it is Switch content.
    The app already knows the difference (an unchosen folder is exactly
    what raises onboarding's "Choose Library Folder" banner), so the
    watcher waits for the answer instead of guessing."""
    web_ctx.start_library_watcher()
    try:
        assert web_ctx.library_watcher_running is False
    finally:
        web_ctx.stop_library_watcher()

    choose_library_folder()
    web_ctx.start_library_watcher()
    try:
        assert web_ctx.library_watcher_running is True
    finally:
        web_ctx.stop_library_watcher()


def test_cancelling_a_scan_reports_whether_there_was_one(client, web_ctx):
    assert client.post("/api/scan/cancel").json()["cancelled"] is False  # nothing running
    assert client.get("/api/scan/status").json()["cancel_requested"] is False


def test_library_folders_can_be_changed_while_a_scan_is_running(client, web_ctx, tmp_path):
    """The deadlock this replaces: saving the folder list used to fail
    outright with "Wait for the current scan to finish" -- so the one
    action that ends an unwanted scan was the one action that scan
    blocked, and since every save starts a scan of its own, adding a
    second folder immediately re-armed the refusal against removing the
    first."""
    import threading

    started, release = threading.Event(), threading.Event()

    def _slow_scan(conn, *, on_file=None, should_stop=None, on_progress=None):
        started.set()
        # Behaves like the real thing: polls for the stop flag rather than
        # running to completion, so "cancel" is what actually ends it.
        while not release.wait(0.02):
            if should_stop is not None and should_stop():
                return dict(new=0, updated=0, unchanged=0, skipped_unstable=0,
                            duplicates=0, errors=0, removed=0, relocated=0, cancelled=True)
        return dict(new=0, updated=0, unchanged=0, skipped_unstable=0,
                    duplicates=0, errors=0, removed=0, relocated=0)

    # _run() imports scanner lazily, so the module attribute is the seam.
    from switchagent import scanner as scanner_mod

    original = scanner_mod.scan_library_once
    scanner_mod.scan_library_once = _slow_scan
    try:
        assert web_ctx.run_scan_in_background() is True
        assert started.wait(5.0)

        second = tmp_path / "second-library"
        second.mkdir()
        res = client.post("/api/settings/library-dir", json={"path": str(second), "paths": [str(second)]})
        assert res.status_code == 200, res.text
        assert res.json()["library_dirs"] == [str(second)]
    finally:
        release.set()
        scanner_mod.scan_library_once = original
        web_ctx.stop_library_watcher()


def test_the_last_library_folder_can_be_removed(client, web_ctx, tmp_path):
    """Refused outright before ("At least one folder is required -- add a
    replacement before removing the last one"), so there was no way to stop
    SwitchAgent looking at a folder without first finding another folder to
    hand it. Removing everything is now a legitimate configured state, and
    the rescan it triggers is what retires the orphaned index."""
    folder = tmp_path / "some-library"
    folder.mkdir()
    _add_game(folder, name="Game [0100000000090000][v0].nsp")
    assert client.post(
        "/api/settings/library-dir", json={"path": str(folder), "paths": [str(folder)]},
    ).status_code == 200

    res = client.post("/api/settings/library-dir", json={"path": "", "paths": []})

    assert res.status_code == 200, res.text
    assert res.json()["library_dirs"] == []
    assert res.json()["library_dir_configured"] is False
    web_ctx.stop_library_watcher()


def test_sending_no_path_at_all_is_still_an_error(client, web_ctx):
    """"I chose nothing" (paths: []) and "I sent nothing" (no paths, empty
    path) are different requests -- only the first is a decision."""
    res = client.post("/api/settings/library-dir", json={"path": ""})

    assert res.status_code == 400
    assert "folder path is required" in res.json()["detail"]


def test_start_stop_library_watcher_is_idempotent_and_clean(web_ctx):
    choose_library_folder()
    web_ctx.start_library_watcher()
    try:
        assert web_ctx.library_watcher_running is True
        web_ctx.start_library_watcher()  # second call: no-op, no crash, no second Observer
        assert web_ctx.library_watcher_running is True
    finally:
        web_ctx.stop_library_watcher()
    assert web_ctx.library_watcher_running is False
    web_ctx.stop_library_watcher()  # second call: no-op, no crash


def test_library_watcher_triggers_a_scan_after_a_debounced_file_change(client, web_ctx):
    choose_library_folder()
    web_ctx.library_watch_debounce_seconds = 0.3  # fast enough for a test; production default is 5.0s
    web_ctx.start_library_watcher()
    try:
        _add_game(config.LIBRARY_DIR, name="WatchedGame [0100000000090000][v0].nsp")

        import time
        deadline = time.monotonic() + 10.0
        library = []
        while time.monotonic() < deadline:
            library = client.get("/api/library").json()
            if library:
                break
            time.sleep(0.2)
        assert len(library) == 1, "the watcher should have triggered a scan without a manual /api/scan call"
        assert library[0]["title_id"] == "0100000000090000"
    finally:
        web_ctx.stop_library_watcher()


def test_library_watcher_never_creates_a_job(client, web_ctx):
    """Point 16: a debounced watcher-triggered scan must be exactly as
    job-free as a manual one (already proven by
    test_scan_never_creates_a_job_via_api for the manual path -- this is
    the same invariant via the watcher's own trigger path)."""
    choose_library_folder()
    web_ctx.library_watch_debounce_seconds = 0.3
    web_ctx.start_library_watcher()
    try:
        _add_game(config.LIBRARY_DIR, name="WatchedGame2 [0100000000091000][v0].nsp")

        import time
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if client.get("/api/library").json():
                break
            time.sleep(0.2)

        assert client.get("/api/queue").json() == []
        assert client.get("/api/history").json() == []
    finally:
        web_ctx.stop_library_watcher()


def test_scan_never_creates_a_job_via_api(client, web_ctx):
    """Regression guard (point 25) at the HTTP layer, mirroring
    test_library_scanner.py's lower-level version."""
    _add_game(config.LIBRARY_DIR)
    client.post("/api/scan")
    import time
    for _ in range(50):
        if not client.get("/api/scan/status").json()["running"]:
            break
        time.sleep(0.2)

    queue = client.get("/api/queue").json()
    assert queue == []


def test_get_library_item_detail(client, web_ctx):
    with db.open_db(web_ctx.db_path) as conn:
        item_id = db.upsert_library_item(
            conn, absolute_path=str(config.LIBRARY_DIR / "Game.nsp"), item_type="FILE", file_type="NSP",
            size=1234, mtime=0.0, content_hash="abc", title_id="0100000000010000",
            title_id_source="filename", status="AVAILABLE", suggested_action="INSTALL_VIA_DBI",
            suggested_target="SD_INSTALL",
        )
    res = client.get(f"/api/library/{item_id}")
    assert res.status_code == 200
    body = res.json()
    assert body["title_id"] == "0100000000010000"
    assert body["size"] == 1234
    assert body["job"] is None


def test_get_library_item_404_for_unknown_id(client):
    res = client.get("/api/library/999999")
    assert res.status_code == 404


# ---------------------------------------------------------------------------
# jobs / queue -- creation always requires an explicit target device
# ---------------------------------------------------------------------------

def _seed_library_item(web_ctx, name="Game [0100000000010000][v0].nsp") -> int:
    path = _add_game(config.LIBRARY_DIR, name=name)
    with db.open_db(web_ctx.db_path) as conn:
        item_id = db.upsert_library_item(
            conn, absolute_path=str(path), item_type="FILE", file_type="NSP",
            size=path.stat().st_size, mtime=path.stat().st_mtime, content_hash="h1",
            title_id="0100000000010000", title_id_source="filename", status="AVAILABLE",
            suggested_action="INSTALL_VIA_DBI", suggested_target="SD_INSTALL",
        )
    return item_id


def test_create_jobs_requires_target_device(client, web_ctx):
    item_id = _seed_library_item(web_ctx)
    res = client.post("/api/jobs", json={"library_item_ids": [item_id], "target_device_id": ""})
    assert res.status_code == 422  # pydantic: min_length=1 on target_device_id


def test_create_jobs_requires_at_least_one_item(client):
    res = client.post("/api/jobs", json={"library_item_ids": [], "target_device_id": "mock-switch-parent"})
    assert res.status_code == 422


def test_create_jobs_reports_missing_rar_backend_as_a_clear_per_item_error(client, web_ctx, monkeypatch):
    """A .rar item whose extraction needs an external unrar/7z/bsdtar tool
    that isn't installed (Packaging: "RAR extraction" -- must never crash,
    must report clearly) surfaces as a normal per-item error, not an
    unhandled 500."""
    from switchagent import extractor as extractor_mod
    from switchagent.web import services as services_mod

    item_id = _seed_library_item(web_ctx, name="Mod [0100000000070000].rar")

    def _raise(*args, **kwargs):
        raise extractor_mod.ExtractionBackendMissingError("no unrar/7z/bsdtar on PATH")

    monkeypatch.setattr(services_mod.preview, "preview_path", _raise)

    res = client.post("/api/jobs", json={
        "library_item_ids": [item_id], "target_device_id": "mock-switch-parent",
    })

    assert res.status_code == 422
    body = res.json()
    assert body["created"] == []
    assert len(body["errors"]) == 1
    assert "RAR" in body["errors"][0]["error"]


def test_create_jobs_creates_confirmed_job_and_it_appears_in_queue(client, web_ctx):
    item_id = _seed_library_item(web_ctx)
    res = client.post("/api/jobs", json={
        "library_item_ids": [item_id], "target_device_id": "mock-switch-parent",
    })
    assert res.status_code == 201
    body = res.json()
    assert len(body["created"]) == 1
    assert body["errors"] == []
    job_id = body["created"][0]["job_id"]

    with db.open_db(web_ctx.db_path) as conn:
        row = db.get_job(conn, job_id)
        assert row["status"] == "CONFIRMED"  # already confirmed -- the modal WAS the confirmation
        assert row["target_device_id"] == "mock-switch-parent"

    queue = client.get("/api/queue").json()
    assert len(queue) == 1
    assert queue[0]["id"] == job_id
    assert queue[0]["status"] == "CONFIRMED"


# ---------------------------------------------------------------------------
# Batch install: 4 selected items must yield 4 persisted jobs, none lost --
# real report: after a 4-item batch (Bread and Fred Base/Update/Mod +
# Sacred 2), the user only saw the LAST (largest) job in Queue because the
# first three completed fast enough to already be gone by the time they
# looked. All four actually ran correctly; this section proves the backend
# genuinely never drops one, regardless of how fast any of them finish.
# ---------------------------------------------------------------------------

def _seed_four_items(web_ctx) -> list[int]:
    names_and_titles = [
        ("Bread and Fred [0100AF401B6A4000][v0].nsp", "0100AF401B6A4000"),
        ("Bread and Fred Update [0100AF401B6A4800][v131072].nsp", "0100AF401B6A4800"),
        ("Quake II [010048F0195E8000][v0].nsp", "010048F0195E8000"),
        ("Sacred 2 [010074402766A000][v65536].nsp", "010074402766A000"),
    ]
    ids = []
    with db.open_db(web_ctx.db_path) as conn:
        for i, (name, title) in enumerate(names_and_titles):
            path = _add_game(config.LIBRARY_DIR, name=name, content=f"payload-{i}".encode())
            item_id = db.upsert_library_item(
                conn, absolute_path=str(path), item_type="FILE", file_type="NSP",
                size=path.stat().st_size, mtime=path.stat().st_mtime, content_hash=f"hash-{i}",
                title_id=title, title_id_source="filename", status="AVAILABLE",
                suggested_action="INSTALL_VIA_DBI", suggested_target="SD_INSTALL",
            )
            ids.append(item_id)
    return ids


def test_batch_of_four_selected_items_creates_four_jobs(client, web_ctx):
    item_ids = _seed_four_items(web_ctx)
    res = client.post("/api/jobs", json={"library_item_ids": item_ids, "target_device_id": "mock-switch-parent"})
    assert res.status_code == 201
    body = res.json()
    assert len(body["created"]) == 4
    assert body["errors"] == []

    with db.open_db(web_ctx.db_path) as conn:
        all_jobs = sorted(db.list_jobs(conn), key=lambda j: j["id"])  # id, not created_at: created_at
        # has only 1s resolution and these 4 jobs are created within the
        # same test, possibly the same second -- id is the one column
        # with a real, strictly-monotonic guarantee matching insertion order.
    assert len(all_jobs) == 4
    assert {j["status"] for j in all_jobs} == {"CONFIRMED"}
    # order preserved: job creation followed the submitted library_item_ids order
    created_job_ids = [c["job_id"] for c in body["created"]]
    assert created_job_ids == sorted(created_job_ids)  # ascending ids, in submission order
    assert [j["library_item_id"] for j in all_jobs] == item_ids


def test_batch_jobs_execute_sequentially_and_none_are_lost(client, web_ctx):
    """Pumps the worker exactly 4 times (never touching /api/queue in
    between, replicating "the user only looked once, at the end") and
    proves all 4 reached a terminal state and all 4 are in History --
    nothing silently skipped or overwritten, even though the small ones
    finish "instantly" relative to a human looking at a page."""
    item_ids = _seed_four_items(web_ctx)
    res = client.post("/api/jobs", json={"library_item_ids": item_ids, "target_device_id": "mock-switch-parent"})
    created_job_ids = [c["job_id"] for c in res.json()["created"]]
    assert len(created_job_ids) == 4

    outcomes = []
    with db.open_db(web_ctx.db_path) as conn:
        for _ in range(4):
            outcome = queue_worker.run_worker_once(conn, web_ctx.registry)
            assert outcome is not None, "a job must be found and processed on each of the 4 passes"
            outcomes.append(outcome)

    # Sequential, not parallel: each pass processed exactly one job, and no
    # two outcomes name the same job_id (proves nothing was double-run or
    # silently repeated instead of moving on to the next one).
    assert len(outcomes) == 4
    assert {o.job_id for o in outcomes} == set(created_job_ids)
    assert all(o.status == "DONE" for o in outcomes)

    # A 5th pass finds nothing left -- confirms exactly 4 jobs existed, no
    # phantom extra/duplicate job was created anywhere along the way.
    with db.open_db(web_ctx.db_path) as conn:
        assert queue_worker.run_worker_once(conn, web_ctx.registry) is None

    history = client.get("/api/history").json()
    assert len(history) == 4
    assert {h["job_id"] for h in history} == set(created_job_ids)
    assert all(h["outcome"] == "DONE" for h in history)

    # All 4 completed -> Queue is correctly empty (DONE jobs belong in
    # History only) -- this is the CORRECT disappearance the user observed,
    # not a bug, as long as (proven above) every single one is in History.
    assert client.get("/api/queue").json() == []


def test_batch_jobs_never_run_in_parallel(client, web_ctx):
    """A second run_worker_once() call must never start a new job while an
    earlier one is still RUNNING -- exactly the invariant the Quake II
    incident was about. Simulated here by checking that after the FIRST
    pass, exactly one job is RUNNING/terminal and the rest are still
    untouched (CONFIRMED), never more than one touched per pass."""
    item_ids = _seed_four_items(web_ctx)
    client.post("/api/jobs", json={"library_item_ids": item_ids, "target_device_id": "mock-switch-parent"})

    with db.open_db(web_ctx.db_path) as conn:
        queue_worker.run_worker_once(conn, web_ctx.registry)
        statuses = [db.get_job(conn, jid)["status"] for jid in
                    [row["id"] for row in db.list_jobs(conn)]]
    touched = [s for s in statuses if s != "CONFIRMED"]
    assert len(touched) == 1, f"exactly one job should be touched after a single pass, got: {statuses}"
    assert touched[0] == "DONE"


def test_create_jobs_does_not_transfer_immediately(client, web_ctx):
    """job created != transfer immediately (point 25) -- nothing in the
    mock backend's storage until the worker explicitly runs."""
    item_id = _seed_library_item(web_ctx)
    client.post("/api/jobs", json={"library_item_ids": [item_id], "target_device_id": "mock-switch-parent"})
    parent = web_ctx.registry.get("mock-switch-parent")
    parent.connect()
    assert parent.storage_tree("SD_INSTALL").list_files() == []


def test_worker_pump_completes_the_job_end_to_end(client, web_ctx):
    item_id = _seed_library_item(web_ctx)
    res = client.post("/api/jobs", json={"library_item_ids": [item_id], "target_device_id": "mock-switch-parent"})
    job_id = res.json()["created"][0]["job_id"]

    with db.open_db(web_ctx.db_path) as conn:
        outcome = queue_worker.run_worker_once(conn, web_ctx.registry)
        assert outcome.job_id == job_id
        assert outcome.status == "DONE"

    job = client.get(f"/api/jobs/{job_id}").json()
    assert job["status"] == "DONE"

    history = client.get("/api/history").json()
    assert len(history) == 1
    assert history[0]["outcome"] == "DONE"

    # A completed job drops out of the "still in queue" view.
    assert client.get("/api/queue").json() == []


def test_background_worker_thread_picks_up_a_confirmed_job_promptly(client, web_ctx):
    """Regression guard for the real-hardware finding that the Queue page
    barely showed progress (docs/REAL-HARDWARE-TEST-*.md): the background
    worker thread (not just a manually-pumped run_worker_once() call) must
    notice and process a CONFIRMED job within roughly
    context.DEFAULT_WORKER_POLL_INTERVAL_SECONDS, not the old 5s default.
    Starts the REAL WebContext worker thread -- the same mechanism
    `switch-agent web` runs in production -- and polls /api/jobs/{id}
    (never sleeps blindly) for a bounded, generous timeout."""
    import time

    item_id = _seed_library_item(web_ctx)
    res = client.post("/api/jobs", json={"library_item_ids": [item_id], "target_device_id": "mock-switch-parent"})
    job_id = res.json()["created"][0]["job_id"]

    web_ctx.start_worker()
    try:
        deadline = time.monotonic() + 5.0
        status = None
        while time.monotonic() < deadline:
            status = client.get(f"/api/jobs/{job_id}").json()["status"]
            if status in ("DONE", "DONE_UNVERIFIED", "FAILED"):
                break
            time.sleep(0.1)
        assert status == "DONE", f"worker thread did not complete the job in time (last status: {status})"
    finally:
        web_ctx.stop_worker()


def test_create_jobs_partial_preparation_failure_blocks_entire_selection(client, web_ctx):
    good_id = _seed_library_item(web_ctx, name="Good [0100000000010000][v0].nsp")
    res = client.post("/api/jobs", json={
        "library_item_ids": [good_id, 999999], "target_device_id": "mock-switch-parent",
    })
    assert res.status_code == 422
    body = res.json()
    assert body["created"] == []
    assert len(body["errors"]) == 1
    assert body["errors"][0]["library_item_id"] == 999999


def test_retry_job_wrong_state_returns_409(client, web_ctx):
    item_id = _seed_library_item(web_ctx)
    res = client.post("/api/jobs", json={"library_item_ids": [item_id], "target_device_id": "mock-switch-parent"})
    job_id = res.json()["created"][0]["job_id"]  # CONFIRMED, not retryable yet
    retry_res = client.post(f"/api/jobs/{job_id}/retry")
    assert retry_res.status_code == 409


def test_cancel_queued_job(client, web_ctx):
    item_id = _seed_library_item(web_ctx)
    res = client.post("/api/jobs", json={"library_item_ids": [item_id], "target_device_id": "mock-switch-parent"})
    job_id = res.json()["created"][0]["job_id"]

    cancel_res = client.post(f"/api/jobs/{job_id}/cancel")
    assert cancel_res.status_code == 200
    job = client.get(f"/api/jobs/{job_id}").json()
    assert job["status"] == "FAILED"


def test_cancelled_job_lets_library_item_be_reselected(client, web_ctx):
    """A cancelled/skipped job (status='FAILED', abandoned=1 -- see
    cancel_job()) must not permanently disable its library item's
    checkbox. Before this fix, can_install only ever cleared for a
    'CANCELLED' status string cancel_job() never actually sets, so a
    cancelled item's checkbox stayed disabled forever -- with retry_job()
    itself refusing an abandoned job and pointing the user right back at
    Library ("select the source again"), that was a real dead end."""
    item_id = _seed_library_item(web_ctx)
    res = client.post("/api/jobs", json={"library_item_ids": [item_id], "target_device_id": "mock-switch-parent"})
    job_id = res.json()["created"][0]["job_id"]

    entries = client.get("/api/library").json()
    assert next(e for e in entries if e["id"] == item_id)["can_install"] is False

    cancel_res = client.post(f"/api/jobs/{job_id}/cancel")
    assert cancel_res.status_code == 200

    entries = client.get("/api/library").json()
    assert next(e for e in entries if e["id"] == item_id)["can_install"] is True


def test_cancel_running_job_rejected(client, web_ctx):
    item_id = _seed_library_item(web_ctx)
    res = client.post("/api/jobs", json={"library_item_ids": [item_id], "target_device_id": "mock-switch-parent"})
    job_id = res.json()["created"][0]["job_id"]
    with db.open_db(web_ctx.db_path) as conn:
        db.update_job_status(conn, job_id, "RUNNING", started_at=db.now_iso())

    cancel_res = client.post(f"/api/jobs/{job_id}/cancel")
    assert cancel_res.status_code == 409


# ---------------------------------------------------------------------------
# UI-005: WAITING_FOR_DEVICE -- Queue UI/cancel wiring. The worker-level
# eligibility rule itself (queue_worker._decide_device_absence_status) has
# its own dedicated tests in tests/test_queue_worker.py; these cover only
# the HTTP/HTML layer.
# ---------------------------------------------------------------------------

def test_waiting_for_device_job_can_be_cancelled(client, web_ctx):
    item_id = _seed_library_item(web_ctx)
    res = client.post("/api/jobs", json={"library_item_ids": [item_id], "target_device_id": "mock-switch-parent"})
    job_id = res.json()["created"][0]["job_id"]
    with db.open_db(web_ctx.db_path) as conn:
        db.update_job_status(conn, job_id, "WAITING_FOR_DEVICE", error="waiting")

    cancel_res = client.post(f"/api/jobs/{job_id}/cancel")
    assert cancel_res.status_code == 200
    assert client.get(f"/api/jobs/{job_id}").json()["status"] == "FAILED"


def test_queue_page_shows_friendly_waiting_for_device_message_no_retry_button(client, web_ctx):
    item_id = _seed_library_item(web_ctx)
    res = client.post("/api/jobs", json={"library_item_ids": [item_id], "target_device_id": "mock-switch-parent"})
    job_id = res.json()["created"][0]["job_id"]
    with db.open_db(web_ctx.db_path) as conn:
        db.update_job_status(
            conn, job_id, "WAITING_FOR_DEVICE",
            error="device 'abc123' is not registered with this worker",
        )

    html = client.get("/queue").text
    assert "Waiting for" in html
    assert "to reconnect" in html
    assert "resumes automatically" in html
    # the raw technical error string is suppressed for this self-resolving
    # status -- the friendly message above is shown instead
    assert "is not registered with this worker" not in html
    assert f'data-job-action="retry" data-job-id="{job_id}"' not in html  # self-resolving, no retry button
    assert f'data-remove-jobs="{job_id}"' in html  # taken out of the queue from its game card


def test_waiting_for_device_error_message_never_contains_raw_device_id(client, web_ctx):
    """Regression guard for a gap found while building UI-005: the
    device-absence error message used to embed the raw target_device_id
    directly (which, on real hardware, contains the device's USB serial --
    see mtp/windows.py's mask_device_id docstring) -- fixed to use
    device_fingerprint() instead, for both WAITING_FOR_DEVICE and the
    pre-existing DEVICE_UNAVAILABLE message that shares the same code."""
    item_id = _seed_library_item(web_ctx)
    res = client.post("/api/jobs", json={
        "library_item_ids": [item_id],
        "target_device_id": "usb#vid_057e&pid_201d#SERIALNUMBER5678#{guid}",  # never registered
    })
    job_id = res.json()["created"][0]["job_id"]
    with db.open_db(web_ctx.db_path) as conn:
        queue_worker.run_worker_once(conn, web_ctx.registry)
        row = db.get_job(conn, job_id)

    assert row["status"] == "WAITING_FOR_DEVICE"
    assert "SERIALNUMBER5678" not in (row["error"] or "")


# ---------------------------------------------------------------------------
# UI-006: stall detection -- a DERIVED UI condition from jobs.last_progress_at,
# never a stored status. services.STALL_THRESHOLD_SECONDS governs the cutoff.
# ---------------------------------------------------------------------------

def _make_running_job(web_ctx, client, *, last_progress_at=None, started_at=None) -> int:
    from datetime import datetime, timezone
    item_id = _seed_library_item(web_ctx)
    res = client.post("/api/jobs", json={"library_item_ids": [item_id], "target_device_id": "mock-switch-parent"})
    job_id = res.json()["created"][0]["job_id"]
    started_at = started_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    with db.open_db(web_ctx.db_path) as conn:
        db.update_job_status(conn, job_id, "RUNNING", started_at=started_at, last_progress_at=last_progress_at)
    return job_id


def test_stall_detection_flags_a_running_job_past_threshold(client, web_ctx):
    from datetime import datetime, timedelta, timezone
    from switchagent.web import services

    old = (
        datetime.now(timezone.utc) - timedelta(seconds=services.STALL_THRESHOLD_SECONDS + 30)
    ).isoformat(timespec="seconds")
    job_id = _make_running_job(web_ctx, client, last_progress_at=old)

    job = client.get(f"/api/jobs/{job_id}").json()
    assert job["possibly_stalled"] is True
    assert job["stall_seconds"] >= services.STALL_THRESHOLD_SECONDS


def test_stall_detection_does_not_flag_recent_progress(client, web_ctx):
    from datetime import datetime, timezone

    recent = datetime.now(timezone.utc).isoformat(timespec="seconds")
    job_id = _make_running_job(web_ctx, client, last_progress_at=recent)

    job = client.get(f"/api/jobs/{job_id}").json()
    assert job["possibly_stalled"] is False
    assert job["stall_seconds"] is None


def test_stall_detection_ignores_non_running_jobs(client, web_ctx):
    """An old timestamp on a job that isn't RUNNING (e.g. it already
    finished, or hasn't started at all) must never be flagged -- stall
    detection is only meaningful for a job actively in flight right now."""
    from datetime import datetime, timedelta, timezone

    item_id = _seed_library_item(web_ctx)
    res = client.post("/api/jobs", json={"library_item_ids": [item_id], "target_device_id": "mock-switch-parent"})
    job_id = res.json()["created"][0]["job_id"]
    very_old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(timespec="seconds")
    with db.open_db(web_ctx.db_path) as conn:
        db.update_job_status(conn, job_id, "DONE", finished_at=very_old, last_progress_at=very_old)

    job = client.get(f"/api/jobs/{job_id}").json()
    assert job["possibly_stalled"] is False


def test_stall_detection_falls_back_to_started_at_when_no_progress_recorded_yet(client, web_ctx):
    """A job that reached RUNNING but hasn't touched last_progress_at even
    once yet (shouldn't normally happen given the worker's own touch-point
    at RUNNING start, but this is the honest fallback if it somehow did)
    must still use started_at, not silently report 'never stalled'."""
    from datetime import datetime, timedelta, timezone
    from switchagent.web import services

    old_start = (
        datetime.now(timezone.utc) - timedelta(seconds=services.STALL_THRESHOLD_SECONDS + 30)
    ).isoformat(timespec="seconds")
    job_id = _make_running_job(web_ctx, client, last_progress_at=None, started_at=old_start)

    job = client.get(f"/api/jobs/{job_id}").json()
    assert job["possibly_stalled"] is True


def test_queue_page_renders_stall_warning_with_disclaimer(client, web_ctx):
    from datetime import datetime, timedelta, timezone
    from switchagent.web import services

    old = (
        datetime.now(timezone.utc) - timedelta(seconds=services.STALL_THRESHOLD_SECONDS + 60)
    ).isoformat(timespec="seconds")
    _make_running_job(web_ctx, client, last_progress_at=old)

    html = client.get("/queue").text
    assert "Possibly stalled" in html
    assert "no observable progress for" in html
    assert "may legitimately take a long time" in html


def test_api_worker_restart_returns_pending(client, web_ctx):
    res = client.post("/api/worker/restart")
    assert res.status_code == 200
    assert res.json()["restart_pending"] is True
    assert web_ctx.is_worker_restart_pending is True


def test_worker_restart_is_picked_up_by_the_real_worker_thread(client, web_ctx):
    """Uses the REAL WebContext worker thread (not a manually-pumped
    run_worker_once() call) -- proves request_worker_restart() is actually
    observed and cleared at the loop's own next safe point, not just that
    the flag can be set."""
    import time

    web_ctx.start_worker()
    try:
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and web_ctx.worker_last_heartbeat_at is None:
            time.sleep(0.05)
        assert web_ctx.worker_last_heartbeat_at is not None, "worker thread never ticked at all"

        web_ctx.request_worker_restart()
        assert web_ctx.is_worker_restart_pending is True

        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and web_ctx.is_worker_restart_pending:
            time.sleep(0.05)
        assert web_ctx.is_worker_restart_pending is False, "worker never picked up the restart request"
    finally:
        web_ctx.stop_worker()


def test_settings_page_shows_worker_heartbeat_and_restart_button(client, web_ctx):
    html = client.get("/settings").text
    assert 'id="restart-worker-btn"' in html
    assert "Last heartbeat:" in html


# ---------------------------------------------------------------------------
# Retry installation: creates a brand NEW job, never a resume (point 8 /
# services.retry_job's own docstring, docs/STATE.md's Quake II
# investigation) -- after an interruption the destination is in an unknown
# state and DBI's SD_INSTALL virtual node has no resume semantics at the
# protocol level regardless, so a retry always means re-sending the whole
# file, exactly like a first attempt. The old job row and its
# install_history entry must be left completely untouched -- a permanent
# record of that attempt, never rewritten for convenience.
# ---------------------------------------------------------------------------

_RETRYABLE_STATUSES_FOR_TEST = (
    "INTERRUPTED", "FAILED", "DEVICE_UNAVAILABLE",
    "SOURCE_CHANGED", "BLOCKED_BY_DEPENDENCY",
)


def test_retry_creates_a_new_job_and_leaves_the_old_one_untouched(client, web_ctx):
    item_id = _seed_library_item(web_ctx)
    res = client.post("/api/jobs", json={"library_item_ids": [item_id], "target_device_id": "mock-switch-parent"})
    old_job_id = res.json()["created"][0]["job_id"]
    with db.open_db(web_ctx.db_path) as conn:
        db.update_job_status(conn, old_job_id, "INTERRUPTED", error="device unplugged mid-transfer")

    retry_res = client.post(f"/api/jobs/{old_job_id}/retry")
    assert retry_res.status_code == 200
    body = retry_res.json()
    assert body["ok"] is True
    assert body["old_job_id"] == old_job_id
    new_job_id = body["new_job_id"]
    assert new_job_id != old_job_id

    with db.open_db(web_ctx.db_path) as conn:
        old_row = db.get_job(conn, old_job_id)
        new_row = db.get_job(conn, new_job_id)
    # The old job stays a permanent, unaltered record of that attempt.
    assert old_row["status"] == "INTERRUPTED"
    assert old_row["error"] == "device unplugged mid-transfer"
    # The new job is a fresh, independent attempt -- already confirmed,
    # same as any brand new job (see create_and_confirm_jobs).
    assert new_row["status"] == "CONFIRMED"
    assert new_row["library_item_id"] == old_row["library_item_id"]
    assert new_row["target_device_id"] == old_row["target_device_id"]
    assert new_row["attempt_count"] == 0  # a fresh job, not a resumed attempt counter


def test_retry_preserves_target_device_immutably_even_with_others_connected(client, web_ctx):
    """The mock context has two connected devices (parent/child) -- retry
    must never silently switch to a different one just because it's also
    reachable, mirroring the "never redirect a job between devices" rule."""
    item_id = _seed_library_item(web_ctx)
    res = client.post("/api/jobs", json={"library_item_ids": [item_id], "target_device_id": "mock-switch-child"})
    old_job_id = res.json()["created"][0]["job_id"]
    with db.open_db(web_ctx.db_path) as conn:
        db.update_job_status(conn, old_job_id, "FAILED", error="transfer failed")

    retry_res = client.post(f"/api/jobs/{old_job_id}/retry")
    new_job_id = retry_res.json()["new_job_id"]
    with db.open_db(web_ctx.db_path) as conn:
        new_row = db.get_job(conn, new_job_id)
    assert new_row["target_device_id"] == "mock-switch-child"


def test_retry_leaves_old_install_history_entry_completely_untouched(client, web_ctx):
    item_id = _seed_library_item(web_ctx)
    res = client.post("/api/jobs", json={"library_item_ids": [item_id], "target_device_id": "mock-switch-parent"})
    old_job_id = res.json()["created"][0]["job_id"]
    with db.open_db(web_ctx.db_path) as conn:
        db.update_job_status(conn, old_job_id, "INTERRUPTED", error="device unplugged mid-transfer")
        db.record_install_history(
            conn, job_id=old_job_id, title_id="0100000000010000", display_name="Game",
            target_device_id="mock-switch-parent", target_storage="SD_INSTALL",
            outcome="INTERRUPTED", error="device unplugged mid-transfer", bytes_total=1234,
        )

    client.post(f"/api/jobs/{old_job_id}/retry")

    with db.open_db(web_ctx.db_path) as conn:
        history = db.list_install_history(conn)
    # Retry itself never writes a history row -- only a job actually
    # reaching a terminal state via the worker does that. The old entry
    # must be the only one, byte-for-byte as it was written originally.
    assert len(history) == 1
    entry = history[0]
    assert entry["job_id"] == old_job_id
    assert entry["outcome"] == "INTERRUPTED"
    assert entry["error"] == "device unplugged mid-transfer"
    assert entry["bytes_total"] == 1234


@pytest.mark.parametrize("status", _RETRYABLE_STATUSES_FOR_TEST)
def test_retry_accepts_every_documented_retryable_status(client, web_ctx, status):
    item_id = _seed_library_item(web_ctx)
    res = client.post("/api/jobs", json={"library_item_ids": [item_id], "target_device_id": "mock-switch-parent"})
    old_job_id = res.json()["created"][0]["job_id"]
    with db.open_db(web_ctx.db_path) as conn:
        db.update_job_status(conn, old_job_id, status, error="simulated for test")

    retry_res = client.post(f"/api/jobs/{old_job_id}/retry")
    assert retry_res.status_code == 200, f"status {status} should be retryable but got {retry_res.json()}"
    with db.open_db(web_ctx.db_path) as conn:
        new_row = db.get_job(conn, retry_res.json()["new_job_id"])
    assert new_row["status"] == "CONFIRMED"


@pytest.mark.parametrize("status", [
    "PENDING_CONFIRM", "CONFIRMED", "RUNNING", "VERIFYING",
    "DONE", "DONE_UNVERIFIED", "WAITING_FOR_BASE", "DESTINATION_CONFLICT",
])
def test_retry_rejected_for_non_retryable_statuses(client, web_ctx, status):
    """DONE/DONE_UNVERIFIED especially: a verified-or-not SUCCESSFUL
    install must never be offered a "Retry installation" button -- that
    would invite an unwanted, unnecessary re-transfer of something that
    already worked."""
    item_id = _seed_library_item(web_ctx)
    res = client.post("/api/jobs", json={"library_item_ids": [item_id], "target_device_id": "mock-switch-parent"})
    old_job_id = res.json()["created"][0]["job_id"]
    with db.open_db(web_ctx.db_path) as conn:
        db.update_job_status(conn, old_job_id, status)

    retry_res = client.post(f"/api/jobs/{old_job_id}/retry")
    assert retry_res.status_code == 409


def test_retry_409_for_unknown_job_id(client):
    res = client.post("/api/jobs/999999/retry")
    assert res.status_code == 409  # services.retry_job raises ValueError -> 409, not a bare 404


def test_retry_rejected_when_source_library_item_no_longer_exists(client, web_ctx):
    """Simulates the library_items row having been removed out from under
    an existing job. NOTE: this cannot be produced via
    db.delete_library_items_missing_from() (what a real Rescan uses) --
    that function currently raises sqlite3.IntegrityError for any row a
    job still references, since jobs.library_item_id has no ON DELETE
    clause (found while writing this test; a real latent Rescan bug once a
    previously-installed game's file is removed from the Library folder,
    tracked in STATE.md for the Rescan phase, deliberately NOT fixed here
    -- out of scope for retry). Bypassing FK enforcement directly is the
    only way to isolate retry_job()'s OWN "source row is gone" branch from
    that unrelated bug."""
    item_id = _seed_library_item(web_ctx)
    res = client.post("/api/jobs", json={"library_item_ids": [item_id], "target_device_id": "mock-switch-parent"})
    old_job_id = res.json()["created"][0]["job_id"]
    with db.open_db(web_ctx.db_path) as conn:
        db.update_job_status(conn, old_job_id, "FAILED")
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("DELETE FROM library_items WHERE id = ?", (item_id,))
        conn.commit()

    retry_res = client.post(f"/api/jobs/{old_job_id}/retry")
    assert retry_res.status_code == 409
    assert "no longer exists" in retry_res.json()["detail"]


def test_retry_rejected_when_source_no_longer_available(client, web_ctx):
    """Re-validates, rather than blindly trusting the old job's frozen
    state -- e.g. a rescan since the original attempt marked the source
    NEEDS_REVIEW (or it's now claimed by another active job)."""
    item_id = _seed_library_item(web_ctx)
    res = client.post("/api/jobs", json={"library_item_ids": [item_id], "target_device_id": "mock-switch-parent"})
    old_job_id = res.json()["created"][0]["job_id"]
    with db.open_db(web_ctx.db_path) as conn:
        db.update_job_status(conn, old_job_id, "FAILED")
        row = db.get_library_item_by_id(conn, item_id)
        db.upsert_library_item(
            conn, absolute_path=row["absolute_path"], item_type=row["item_type"], file_type=row["file_type"],
            size=row["size"], mtime=row["mtime"], content_hash=row["content_hash"], title_id=row["title_id"],
            title_id_source=row["title_id_source"], status="NEEDS_REVIEW",
            suggested_action=row["suggested_action"], suggested_target=row["suggested_target"],
        )

    retry_res = client.post(f"/api/jobs/{old_job_id}/retry")
    assert retry_res.status_code == 409
    assert "not currently installable" in retry_res.json()["detail"]


def test_retry_button_appears_in_queue_html_for_all_retryable_statuses(client, web_ctx):
    """Structural guard mirroring test_queue_page_has_live_polling_hook_elements
    -- proves queue.html's Retry button condition actually covers every
    status services.py accepts."""
    item_id = _seed_library_item(web_ctx)
    res = client.post("/api/jobs", json={"library_item_ids": [item_id], "target_device_id": "mock-switch-parent"})
    job_id = res.json()["created"][0]["job_id"]

    for status in _RETRYABLE_STATUSES_FOR_TEST:
        with db.open_db(web_ctx.db_path) as conn:
            db.update_job_status(conn, job_id, status)
        html = client.get("/queue").text
        assert f'data-job-action="retry" data-job-id="{job_id}"' in html, (
            f"Retry installation button missing for status {status}"
        )


def test_no_action_buttons_for_an_auto_resolved_destination_conflict(client, web_ctx):
    """DESTINATION_CONFLICT is always abandoned=1 by the time it's set --
    queue_worker.py resolves it automatically per Settings' conflict
    policy, the instant it happens (see FileAlreadyExistsError handling).
    Nothing is left for a person to click: no Retry (retrying would just
    hit the same conflict again), no Cancel (already abandoned)."""
    item_id = _seed_library_item(web_ctx)
    res = client.post("/api/jobs", json={"library_item_ids": [item_id], "target_device_id": "mock-switch-parent"})
    job_id = res.json()["created"][0]["job_id"]
    with db.open_db(web_ctx.db_path) as conn:
        db.update_job_status(conn, job_id, "DESTINATION_CONFLICT", error="conflict")
        conn.execute("UPDATE jobs SET abandoned=1 WHERE id=?", (job_id,))
        conn.commit()

    html = client.get("/queue").text
    assert f'data-job-action="retry" data-job-id="{job_id}"' not in html
    assert f'data-job-action="cancel" data-job-id="{job_id}"' not in html


# ---------------------------------------------------------------------------
# UI-002: installation batches
# ---------------------------------------------------------------------------

def test_create_jobs_returns_one_batch_id_shared_by_every_created_job(client, web_ctx):
    item_ids = _seed_four_items(web_ctx)
    res = client.post("/api/jobs", json={"library_item_ids": item_ids, "target_device_id": "mock-switch-parent"})
    body = res.json()
    batch_id = body["batch_id"]
    assert batch_id is not None

    with db.open_db(web_ctx.db_path) as conn:
        for c in body["created"]:
            assert db.get_job(conn, c["job_id"])["batch_id"] == batch_id


def test_single_item_confirm_still_creates_a_batch(client, web_ctx):
    """Even a single selected item is one batch -- not just a bare job."""
    item_id = _seed_library_item(web_ctx)
    res = client.post("/api/jobs", json={"library_item_ids": [item_id], "target_device_id": "mock-switch-parent"})
    body = res.json()
    assert body["batch_id"] is not None
    with db.open_db(web_ctx.db_path) as conn:
        assert db.get_job(conn, body["created"][0]["job_id"])["batch_id"] == body["batch_id"]


def test_all_items_failing_leaves_no_dangling_batch(client, web_ctx):
    res = client.post("/api/jobs", json={"library_item_ids": [999999], "target_device_id": "mock-switch-parent"})
    body = res.json()
    assert body["created"] == []
    assert body["batch_id"] is None


def test_all_items_failing_after_a_job_row_was_already_inserted_does_not_500(client, web_ctx, monkeypatch):
    """Regression: unlike an item rejected before any DB row exists (see
    test_all_items_failing_leaves_no_dangling_batch above), a manifest-build
    failure happens AFTER queue_worker.create_job_from_report() already
    inserted the job row (which it deliberately keeps as a permanent FAILED
    record, not an orphan -- see that function's own docstring). That row
    still references this batch's batch_id, so the old "if not created:
    delete the batch" logic tried to delete a batch two FK-referencing rows
    still pointed to, raising sqlite3.IntegrityError and 500ing the whole
    request -- found via a live `switch-agent web --mock` run during UI-002
    development, not by any prior unit test."""
    from switchagent import queue_worker as queue_worker_mod
    from switchagent import manifest as manifest_mod

    item_ids = [_seed_library_item(web_ctx, name="A [0100000000010000][v0].nsp"),
                _seed_library_item(web_ctx, name="B [0100000000020000][v0].nsp")]

    def _raise(*args, **kwargs):
        raise manifest_mod.ManifestError("source is not under a known scanned root")

    monkeypatch.setattr(queue_worker_mod.manifest_mod, "build_manifest_and_stage", _raise)

    res = client.post("/api/jobs", json={"library_item_ids": item_ids, "target_device_id": "mock-switch-parent"})
    assert res.status_code == 422
    body = res.json()
    assert body["created"] == []
    assert len(body["errors"]) == 2
    # The batch is NOT deleted -- two real FAILED job rows exist under it.
    assert body["batch_id"] is not None
    with db.open_db(web_ctx.db_path) as conn:
        assert db.get_installation_batch(conn, body["batch_id"]) is not None
        jobs = db.list_jobs_by_batch(conn, body["batch_id"])
        assert len(jobs) == 2
        assert all(j["status"] == "FAILED" for j in jobs)


def test_queue_grouped_shows_a_multi_job_batch_together_with_aggregates(client, web_ctx):
    item_ids = _seed_four_items(web_ctx)
    res = client.post("/api/jobs", json={"library_item_ids": item_ids, "target_device_id": "mock-switch-parent"})
    batch_id = res.json()["batch_id"]

    groups = client.get("/api/queue/grouped").json()
    assert len(groups) == 1
    group = groups[0]
    assert group["batch_id"] == batch_id
    assert group["total"] == 4
    assert group["waiting"] == 4
    assert group["finished"] == 0


def test_create_jobs_fans_out_a_base_update_dlc_archive_through_the_real_endpoint(client, web_ctx):
    """Web-flow test: selecting ONE library item whose
    archive contains Base+Update+DLC must produce one user selection -> one
    batch -> multiple jobs through the actual POST /api/jobs endpoint (not
    just the service function directly), and Queue must show Base/Update/
    DLC distinctly, not one opaque archive-job."""
    base_title_id, update_title_id, dlc_title_id = (
        "0100AAAAAAAAA000", "0100AAAAAAAAA800", "0100AAAAAAAAB001",
    )
    base_name = f"Game [{base_title_id}][v0].nsz"
    update_name = f"Game Update [{update_title_id}][v65536].nsz"
    dlc_name = f"Game DLC [{dlc_title_id}][v0].nsz"
    archive_path = build_zip(config.LIBRARY_DIR / "bud.zip", {
        base_name: b"base", update_name: b"update", dlc_name: b"dlc",
    })
    st = archive_path.stat()
    with db.open_db(web_ctx.db_path) as conn:
        item_id = db.upsert_library_item(
            conn, absolute_path=str(archive_path), item_type="FILE", file_type="ZIP",
            size=st.st_size, mtime=st.st_mtime, content_hash="h-bud",
            title_id=None, title_id_source=None, status="AVAILABLE",
            suggested_action="INSTALL_VIA_DBI", suggested_target="SD_INSTALL",
            content_type="GAME_PACKAGE",
        )

    res = client.post("/api/jobs", json={"library_item_ids": [item_id], "target_device_id": "mock-switch-parent"})
    assert res.status_code == 201
    body = res.json()
    assert body["errors"] == []
    assert len(body["created"]) == 3  # one selection -> one batch -> three jobs
    assert len({c["library_item_id"] for c in body["created"]}) == 1  # all from the same selected item
    batch_id = body["batch_id"]
    assert batch_id is not None

    groups = client.get("/api/queue/grouped").json()
    assert len(groups) == 1
    group = groups[0]
    assert group["batch_id"] == batch_id
    assert group["total"] == 3
    names = [j["display_name"] for j in group["jobs"]]
    assert names == ["bud.zip — Base", "bud.zip — Update", "bud.zip — DLC"]

    html = client.get("/queue").text
    assert "bud.zip — Base" in html
    assert "bud.zip — Update" in html
    assert "bud.zip — DLC" in html


def test_queue_grouped_keeps_a_finished_member_visible_while_batch_is_incomplete(client, web_ctx):
    """Mirrors the mandate's own example: a base game that already finished
    (DONE_UNVERIFIED) must stay listed as part of its batch's progress as
    long as at least one sibling job hasn't reached a terminal state yet --
    Queue must not just drop it once IT individually finishes."""
    item_ids = _seed_four_items(web_ctx)
    res = client.post("/api/jobs", json={"library_item_ids": item_ids, "target_device_id": "mock-switch-parent"})
    created = res.json()["created"]
    first_job_id = created[0]["job_id"]

    with db.open_db(web_ctx.db_path) as conn:
        db.update_job_status(conn, first_job_id, "DONE_UNVERIFIED", finished_at=db.now_iso())

    groups = client.get("/api/queue/grouped").json()
    assert len(groups) == 1
    group = groups[0]
    assert group["total"] == 4
    assert group["successful"] == 1
    assert group["waiting"] == 3
    assert first_job_id in {j["id"] for j in group["jobs"]}  # still shown, not silently dropped


def test_queue_grouped_drops_a_batch_once_every_job_is_terminally_successful(client, web_ctx):
    item_ids = _seed_four_items(web_ctx)
    res = client.post("/api/jobs", json={"library_item_ids": item_ids, "target_device_id": "mock-switch-parent"})
    job_ids = [c["job_id"] for c in res.json()["created"]]

    with db.open_db(web_ctx.db_path) as conn:
        for job_id in job_ids:
            db.update_job_status(conn, job_id, "DONE_UNVERIFIED", finished_at=db.now_iso())

    assert client.get("/api/queue/grouped").json() == []


def test_queue_drops_a_single_abandoned_job(client, web_ctx):
    """A skipped/cancelled job (abandoned=1) is permanent History, but is
    never active work again -- retry_job() explicitly refuses to touch an
    abandoned job. Before this fix it sat in Queue forever (list_queue
    only ever excluded DONE/DONE_UNVERIFIED), showing action buttons that
    led nowhere."""
    item_id = _seed_library_item(web_ctx)
    res = client.post("/api/jobs", json={"library_item_ids": [item_id], "target_device_id": "mock-switch-parent"})
    job_id = res.json()["created"][0]["job_id"]

    assert client.post(f"/api/jobs/{job_id}/cancel").status_code == 200

    assert client.get("/api/queue/grouped").json() == []
    assert client.get("/api/queue").json() == []
    # Still a real, permanent record -- just not in the active Queue view.
    job = client.get(f"/api/jobs/{job_id}").json()
    assert job["abandoned"] is True


def test_queue_grouped_drops_a_batch_once_every_job_is_done_or_abandoned(client, web_ctx):
    """A mixed batch -- one job succeeded, its sibling was skipped -- must
    also disappear from Queue entirely, not linger because one member
    technically never reached DONE/DONE_UNVERIFIED."""
    item_ids = _seed_four_items(web_ctx)
    res = client.post("/api/jobs", json={"library_item_ids": item_ids, "target_device_id": "mock-switch-parent"})
    job_ids = [c["job_id"] for c in res.json()["created"]]

    with db.open_db(web_ctx.db_path) as conn:
        db.update_job_status(conn, job_ids[0], "DONE_UNVERIFIED", finished_at=db.now_iso())
        db.update_job_status(conn, job_ids[1], "DONE", finished_at=db.now_iso())
    assert client.post(f"/api/jobs/{job_ids[2]}/cancel").status_code == 200
    assert client.post(f"/api/jobs/{job_ids[3]}/cancel").status_code == 200

    assert client.get("/api/queue/grouped").json() == []


def test_queue_drops_a_job_once_it_has_been_retried(client, web_ctx):
    """Retry/Override used to leave the OLD conflicted/failed job sitting
    in Queue completely unchanged -- same live Override/Skip/Retry
    buttons as before, inviting a second, third, Nth click on the same
    card that piled up parallel duplicate attempts at the same file. The
    instant a retry exists (jobs.retry_of_job_id), the old row must
    disappear from Queue -- superseded, not "settled" by its own status,
    which never itself changes when it's retried."""
    item_id = _seed_library_item(web_ctx)
    res = client.post("/api/jobs", json={"library_item_ids": [item_id], "target_device_id": "mock-switch-parent"})
    old_job_id = res.json()["created"][0]["job_id"]
    with db.open_db(web_ctx.db_path) as conn:
        db.update_job_status(conn, old_job_id, "FAILED", error="network blip")

    retry_res = client.post(f"/api/jobs/{old_job_id}/retry")
    assert retry_res.status_code == 200
    new_job_id = retry_res.json()["new_job_id"]

    queue_ids = {j["id"] for j in client.get("/api/queue").json()}
    assert old_job_id not in queue_ids
    assert new_job_id in queue_ids
    grouped_ids = {j["id"] for g in client.get("/api/queue/grouped").json() for j in g["jobs"]}
    assert old_job_id not in grouped_ids
    assert new_job_id in grouped_ids

    # Still a real, permanent record -- just not in the active Queue view.
    old_job = client.get(f"/api/jobs/{old_job_id}").json()
    assert old_job["status"] == "FAILED"
    assert old_job["abandoned"] is False


def test_queue_html_renders_batch_header_only_for_multi_job_batches(client, web_ctx):
    item_ids = _seed_four_items(web_ctx)
    client.post("/api/jobs", json={"library_item_ids": item_ids, "target_device_id": "mock-switch-parent"})
    html = client.get("/queue").text
    assert "batch-header" in html
    assert "4 items" in html  # batch title fallback -- see services._batch_group_view


def test_queue_html_omits_batch_header_for_a_single_job_batch(client, web_ctx):
    item_id = _seed_library_item(web_ctx)
    client.post("/api/jobs", json={"library_item_ids": [item_id], "target_device_id": "mock-switch-parent"})
    html = client.get("/queue").text
    assert "batch-header" not in html


def test_legacy_batch_id_null_job_still_renders_as_its_own_group(client, web_ctx):
    """A job created before this feature existed (or through any path that
    doesn't set batch_id) must keep rendering exactly as before -- as its
    own individual entry, never crashing the grouping logic."""
    item_id = _seed_library_item(web_ctx)
    with db.open_db(web_ctx.db_path) as conn:
        job_id = db.create_job(
            conn, library_item_id=item_id, action="INSTALL_VIA_DBI",
            target_storage="SD_INSTALL", target_device_id="mock-switch-parent",
        )
        # create_job() leaves a job PENDING_CONFIRM, which Queue no longer
        # draws at all (services._not_yet_queued) -- incidental to what this
        # test is actually about, so move it on to real queued work.
        # update_job_status() rather than confirm_job(): the latter insists
        # on a frozen manifest, which a deliberately bare create_job() (the
        # whole point of this test) has none of.
        db.update_job_status(conn, job_id, "CONFIRMED")

    groups = client.get("/api/queue/grouped").json()
    assert any(g["batch_id"] is None and g["jobs"][0]["id"] == job_id for g in groups)
    html = client.get("/queue").text
    assert f'data-job-id="{job_id}"' in html


def test_a_staged_but_unconfirmed_job_is_not_drawn_as_a_queue_row(client, web_ctx):
    """web/preparation.py stages the NEXT item while the current one is
    still transferring, and only records that item's job_ids once its own
    turn arrives. In between, its PENDING_CONFIRM jobs belonged to no
    preparation item, so queue.js's `represented` filter could not
    suppress them and the very same file was drawn TWICE -- once as a
    "Waiting" preparation row, and again as a whole separate batch group
    underneath it, for as long as the previous file took to copy."""
    item_id = _seed_library_item(web_ctx)
    with db.open_db(web_ctx.db_path) as conn:
        batch_id = db.create_installation_batch(conn, target_device_id="mock-switch-parent")
        job_id = db.create_job(
            conn, library_item_id=item_id, action="INSTALL_VIA_DBI",
            target_storage="SD_INSTALL", target_device_id="mock-switch-parent", batch_id=batch_id,
        )
        assert db.get_job(conn, job_id)["status"] == "PENDING_CONFIRM"

    assert client.get("/api/queue/grouped").json() == []
    assert client.get("/api/queue").json() == []
    assert f'data-job-id="{job_id}"' not in client.get("/queue").text

    # ...and the instant its turn comes, it is ordinary queued work again.
    with db.open_db(web_ctx.db_path) as conn:
        db.update_job_status(conn, job_id, "CONFIRMED")
    assert f'data-job-id="{job_id}"' in client.get("/queue").text


def test_an_unconfirmed_job_still_counts_as_unfinished_work(client, web_ctx):
    """_not_yet_queued() is display-only and must never leak into
    _not_settled(): a staged job holds a real payload and a real claim on
    its Switch, so forget_device()'s "unfinished job(s) still target this
    device" guard has to keep counting it even while Queue draws nothing
    for it."""
    from switchagent.web import services

    item_id = _seed_library_item(web_ctx)
    with db.open_db(web_ctx.db_path) as conn:
        db.create_job(
            conn, library_item_id=item_id, action="INSTALL_VIA_DBI",
            target_storage="SD_INSTALL", target_device_id="mock-switch-parent",
        )
        rows = db.list_jobs(conn)

    assert [services._not_yet_queued(r) for r in rows] == [True]
    assert [services._not_settled(rows, r) for r in rows] == [True]


def test_retry_creates_its_own_new_single_job_batch_not_the_old_one(client, web_ctx):
    item_id = _seed_library_item(web_ctx)
    res = client.post("/api/jobs", json={"library_item_ids": [item_id], "target_device_id": "mock-switch-parent"})
    old_job_id = res.json()["created"][0]["job_id"]
    old_batch_id = res.json()["batch_id"]
    with db.open_db(web_ctx.db_path) as conn:
        db.update_job_status(conn, old_job_id, "FAILED", error="boom")

    retry_res = client.post(f"/api/jobs/{old_job_id}/retry")
    body = retry_res.json()
    assert body["batch_id"] is not None
    assert body["batch_id"] != old_batch_id

    with db.open_db(web_ctx.db_path) as conn:
        assert db.get_job(conn, old_job_id)["batch_id"] == old_batch_id  # untouched
        assert db.get_job(conn, body["new_job_id"])["batch_id"] == body["batch_id"]


def test_retry_failing_after_a_job_row_was_already_inserted_does_not_500(client, web_ctx, monkeypatch):
    """Same FK-on-delete regression as
    test_all_items_failing_after_a_job_row_was_already_inserted_does_not_500,
    but through retry_job()'s own single-job batch."""
    from switchagent import manifest as manifest_mod
    from switchagent import queue_worker as queue_worker_mod

    item_id = _seed_library_item(web_ctx)
    res = client.post("/api/jobs", json={"library_item_ids": [item_id], "target_device_id": "mock-switch-parent"})
    old_job_id = res.json()["created"][0]["job_id"]
    with db.open_db(web_ctx.db_path) as conn:
        db.update_job_status(conn, old_job_id, "FAILED", error="boom")

    def _raise(*args, **kwargs):
        raise manifest_mod.ManifestError("source is not under a known scanned root")

    monkeypatch.setattr(queue_worker_mod.manifest_mod, "build_manifest_and_stage", _raise)

    retry_res = client.post(f"/api/jobs/{old_job_id}/retry")
    assert retry_res.status_code == 409  # surfaced as a normal ValueError, not a 500
    with db.open_db(web_ctx.db_path) as conn:
        # A FAILED job row from the retry attempt exists and still points
        # at a real, un-deleted batch.
        rows = [r for r in db.list_jobs(conn) if r["id"] != old_job_id]
        assert len(rows) == 1
        assert rows[0]["status"] == "FAILED"
        assert db.get_installation_batch(conn, rows[0]["batch_id"]) is not None


def test_history_keeps_every_transfer_in_the_order_it_happened(client, web_ctx):
    """One row per transfer, not per title. How many times something was
    sent is not the question -- what was sent, and in what order, is, and
    that stays true when the same thing was sent three times."""
    item_id = _seed_library_item(web_ctx)
    res = client.post("/api/jobs", json={"library_item_ids": [item_id], "target_device_id": "mock-switch-parent"})
    job_id = res.json()["created"][0]["job_id"]
    with db.open_db(web_ctx.db_path) as conn:
        for _ in range(3):
            db.record_install_history(
                conn, job_id=job_id, title_id="0100000000010000", display_name="Some Game.nsp",
                target_device_id="mock-switch-parent", target_storage="SD_INSTALL",
                outcome="DONE_UNVERIFIED", bytes_total=1000,
            )

    html = client.get("/history").text
    assert html.count('class="job-row history-row') == 3
    assert "×3" not in html, "a repeat counter answers a question nobody asked"


def test_history_carries_the_same_badge_queue_puts_on_the_same_item(client, web_ctx):
    """It is the same list, after the fact. An Update reads as an Update in
    both places, from the same source (services._history_role)."""
    item_id = _seed_library_item(web_ctx, name="Some Game [0100000000010800][v65536].nsp")
    res = client.post("/api/jobs", json={"library_item_ids": [item_id], "target_device_id": "mock-switch-parent"})
    job_id = res.json()["created"][0]["job_id"]
    with db.open_db(web_ctx.db_path) as conn:
        db.record_install_history(
            conn, job_id=job_id, title_id="0100000000010800",
            display_name="Some Game [0100000000010800][v65536].nsp",
            target_device_id="mock-switch-parent", target_storage="SD_INSTALL",
            outcome="DONE_UNVERIFIED", bytes_total=1000,
        )

    html = client.get("/history").text
    assert 'class="job-tag job-tag-update">[Update]' in html


def test_history_says_nothing_it_did_not_observe(client, web_ctx):
    """No summary line. It counted transfers and totalled bytes, which is
    arithmetic about the log rather than anything a reader came for, and it
    led with what had NOT happened today."""
    _job_id, _history_id = _seed_done_unverified_history(web_ctx)
    html = client.get("/history").text

    assert "sent in total" not in html
    assert "Nothing was sent today" not in html
    assert "transfers ·" not in html


# ---------------------------------------------------------------------------
# UI-003: user verification of DONE_UNVERIFIED's on-console result. Never
# rewrites install_history.outcome (the immutable transport result) --
# user_verified_outcome/_at are a separate, additive annotation. Only
# meaningful for a DONE_UNVERIFIED row.
# ---------------------------------------------------------------------------

def _seed_done_unverified_history(web_ctx, *, outcome="DONE_UNVERIFIED") -> tuple[int, int]:
    """Returns (job_id, history_id) for a real job + matching install_history
    row with the given transport outcome (default DONE_UNVERIFIED)."""
    item_id = _seed_library_item(web_ctx)
    with db.open_db(web_ctx.db_path) as conn:
        job_id = db.create_job(
            conn, action="INSTALL_VIA_DBI", target_storage="SD_INSTALL",
            target_device_id="mock-switch-parent", library_item_id=item_id,
        )
        history_id = db.record_install_history(
            conn, job_id=job_id, title_id="0100000000010000", display_name="Some Game",
            target_device_id="mock-switch-parent", target_storage="SD_INSTALL",
            outcome=outcome, bytes_total=1000,
        )
    return job_id, history_id


def test_set_history_verification_success_then_clear(client, web_ctx):
    _job_id, history_id = _seed_done_unverified_history(web_ctx)

    res = client.post(f"/api/history/{history_id}/verification", json={"outcome": "SUCCESS"})
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["outcome"] == "DONE_UNVERIFIED"  # transport outcome untouched
    assert body["user_verified_outcome"] == "SUCCESS"
    assert body["user_verified_at"] is not None

    history = client.get("/api/history").json()
    entry = next(h for h in history if h["id"] == history_id)
    assert entry["outcome"] == "DONE_UNVERIFIED"
    assert entry["user_verified_outcome"] == "SUCCESS"

    clear_res = client.post(f"/api/history/{history_id}/verification", json={"outcome": None})
    assert clear_res.status_code == 200
    assert clear_res.json()["user_verified_outcome"] is None
    assert clear_res.json()["user_verified_at"] is None
    assert clear_res.json()["outcome"] == "DONE_UNVERIFIED"  # still untouched


def test_set_history_verification_failed(client, web_ctx):
    _job_id, history_id = _seed_done_unverified_history(web_ctx)
    res = client.post(f"/api/history/{history_id}/verification", json={"outcome": "FAILED"})
    assert res.status_code == 200
    assert res.json()["user_verified_outcome"] == "FAILED"
    assert res.json()["outcome"] == "DONE_UNVERIFIED"


@pytest.mark.parametrize("outcome", ["DONE", "FAILED", "INTERRUPTED", "BLOCKED_BY_DEPENDENCY"])
def test_history_verification_rejected_for_non_unverified_transport_outcome(client, web_ctx, outcome):
    """UI-003 is restricted to DONE_UNVERIFIED specifically -- e.g. a
    definitively FAILED install has no ambiguous console result to
    confirm, and a verified DONE needs no user confirmation at all."""
    _job_id, history_id = _seed_done_unverified_history(web_ctx, outcome=outcome)
    res = client.post(f"/api/history/{history_id}/verification", json={"outcome": "SUCCESS"})
    assert res.status_code == 409


def test_history_verification_409_for_unknown_history_id(client, web_ctx):
    res = client.post("/api/history/999999/verification", json={"outcome": "SUCCESS"})
    assert res.status_code == 409


def test_history_verification_never_touches_transport_outcome_at_the_db_layer(client, web_ctx):
    """Direct DB-level regression guard for UI-003's own critical
    constraint, on top of the API-level check above."""
    _job_id, history_id = _seed_done_unverified_history(web_ctx)
    client.post(f"/api/history/{history_id}/verification", json={"outcome": "FAILED"})
    with db.open_db(web_ctx.db_path) as conn:
        row = db.get_install_history_by_id(conn, history_id)
    assert row["outcome"] == "DONE_UNVERIFIED"
    assert row["user_verified_outcome"] == "FAILED"


def test_history_asks_for_nothing_and_changes_nothing(client, web_ctx):
    """By explicit product decision the page is a journal, not a pulpit.
    The prompt this replaces was shown 55 times in one real library and
    answered 0 times -- a question whose answer lives on another device,
    costs a walk to the console, and buys nothing once given."""
    _job_id, _history_id = _seed_done_unverified_history(web_ctx)
    html = client.get("/history").text

    assert "data-verify-action" not in html
    assert "Installed successfully" not in html and "Installation failed" not in html
    assert "<form" not in html
    assert 'class="btn' not in html, "a journal has no controls at all"
    assert ">Sent<" in html  # it still says what happened


def test_history_still_answers_where_a_stuck_thing_is_decided(client, web_ctx):
    """The one link a read-only journal is allowed: an address, not a
    control. Reporting a problem and staying silent about where it gets
    resolved would be worse than not reporting it."""
    item_id = _seed_library_item(web_ctx)
    res = client.post("/api/jobs", json={"library_item_ids": [item_id], "target_device_id": "mock-switch-parent"})
    job_id = res.json()["created"][0]["job_id"]
    with db.open_db(web_ctx.db_path) as conn:
        # FAILED, not DESTINATION_CONFLICT: the latter is always
        # abandoned=1 the instant it happens (Settings' conflict policy),
        # so it can never actually be "still waiting in Queue" -- FAILED
        # genuinely can, pending a Retry/Cancel decision.
        db.update_job_status(conn, job_id, "FAILED", error="already there")
        db.record_install_history(
            conn, job_id=job_id, title_id="0100000000010000", display_name="Some Game.nsp",
            target_device_id="mock-switch-parent", target_storage="SD_CARD",
            outcome="FAILED", bytes_total=1000,
        )

    html = client.get("/history").text
    assert 'href="/queue"' in html
    assert "Still waiting in Queue" in html
    assert 'class="btn' not in html, "an address, never a control"


def test_the_verification_api_is_untouched_by_the_page_dropping_it(client, web_ctx):
    """Removing the page's call to it was a UI decision, not a data one:
    the endpoint and its column stay correct and callable."""
    _job_id, history_id = _seed_done_unverified_history(web_ctx)
    res = client.post(f"/api/history/{history_id}/verification", json={"outcome": "SUCCESS"})
    assert res.status_code == 200 and res.json()["ok"] is True


def test_history_page_never_shows_verification_ui_for_verified_outcomes(client, web_ctx):
    """A DONE (already verified by the transport layer itself, e.g.
    SD_CARD copies) or a definitively FAILED row must never show the
    verification prompt -- it only applies to DONE_UNVERIFIED."""
    _job_id, _history_id = _seed_done_unverified_history(web_ctx, outcome="DONE")
    html = client.get("/history").text
    assert ">Sent<" not in html
    assert "data-verify-action" not in html  # nothing renders this any more


# ---------------------------------------------------------------------------
# Queue/History UX (point 9/10): every real status must be shown honestly --
# no fabricated progress percentage (SD_INSTALL gives no reliable byte-level
# signal, see DONE_UNVERIFIED's own rationale), DONE_UNVERIFIED must never
# read as plain "Installed", and History must capture every terminal
# outcome including BLOCKED_BY_DEPENDENCY, presented through the real
# HTTP/template layer (not just queue_worker/db directly -- see
# test_install_order.py for that lower level).
# ---------------------------------------------------------------------------

def test_queue_never_renders_a_fabricated_progress_percentage(client, web_ctx):
    """There is no reliable byte-level progress signal for an MTP transfer
    to DBI's SD_INSTALL node at all (System.Size reads 0 even on a real
    successful install) -- the UI must never invent one just because
    bytes_done happens to be a nonzero column value."""
    import re
    item_id = _seed_library_item(web_ctx)
    res = client.post("/api/jobs", json={"library_item_ids": [item_id], "target_device_id": "mock-switch-parent"})
    job_id = res.json()["created"][0]["job_id"]
    with db.open_db(web_ctx.db_path) as conn:
        db.update_job_status(conn, job_id, "RUNNING", bytes_done=3, started_at=db.now_iso())

    html = client.get("/queue").text
    assert not re.search(r"\d+\s*%", html)


def test_done_unverified_is_visually_and_textually_distinct_from_done_in_history(client, web_ctx):
    item_id = _seed_library_item(web_ctx)
    res = client.post("/api/jobs", json={"library_item_ids": [item_id], "target_device_id": "mock-switch-parent"})
    job_id = res.json()["created"][0]["job_id"]
    with db.open_db(web_ctx.db_path) as conn:
        db.record_install_history(
            conn, job_id=job_id, title_id="0100000000010000", display_name="Some Game",
            target_device_id="mock-switch-parent", target_storage="SD_INSTALL",
            outcome="DONE_UNVERIFIED", bytes_total=1000,
        )
    html = client.get("/history").text
    # No longer a raw enum pill: "DONE UNVERIFIED" told a reader nothing.
    # The claim is spelled out now, and the distinction it has to preserve
    # is that this row never reads as a completed install.
    assert ">Sent<" in html
    assert "DONE UNVERIFIED" not in html
    assert ">Installed<" not in html
    assert "history-soft" in html and "history-ok" not in html


def test_blocked_by_dependency_appears_in_history_page_via_http(client, web_ctx):
    """End-to-end through the real HTTP/service/template layer -- proves
    the Web UI's History page actually surfaces this outcome, not just
    that it's correctly persisted at the db/queue_worker level."""
    base_id = _seed_library_item(web_ctx, name="Base [0100000000010000][v0].nsp")
    dlc_path = _add_game(config.LIBRARY_DIR, name="DLC [0100000000011001][v0].nsp", content=b"dlc-payload")
    with db.open_db(web_ctx.db_path) as conn:
        dlc_id = db.upsert_library_item(
            conn, absolute_path=str(dlc_path), item_type="FILE", file_type="NSP",
            size=dlc_path.stat().st_size, mtime=0.0, content_hash="dlc-hash",
            title_id="0100000000011001", title_id_source="filename", status="AVAILABLE",
            suggested_action="INSTALL_VIA_DBI", suggested_target="SD_INSTALL",
            content_type="GAME_PACKAGE", package_format="NSP",
        )

    parent = web_ctx.registry.get("mock-switch-parent")
    parent.connect()
    parent.arm_failure("error", storage="SD_INSTALL", dest_path="Base [0100000000010000][v0].nsp")

    res = client.post("/api/jobs", json={
        "library_item_ids": [base_id, dlc_id], "target_device_id": "mock-switch-parent",
    })
    assert res.status_code == 201

    with db.open_db(web_ctx.db_path) as conn:
        queue_worker.run_worker_once(conn, web_ctx.registry)  # base -> FAILED
        queue_worker.run_worker_once(conn, web_ctx.registry)  # dlc -> BLOCKED_BY_DEPENDENCY

    api_history = client.get("/api/history").json()
    blocked = [h for h in api_history if h["outcome"] == "BLOCKED_BY_DEPENDENCY"]
    assert len(blocked) == 1
    assert blocked[0]["display_name"] == "DLC [0100000000011001][v0].nsp"

    html = client.get("/history").text
    assert "BLOCKED BY DEPENDENCY" in html
    assert "DLC [0100000000011001][v0].nsp" in html


def test_retry_then_worker_completion_creates_a_second_distinct_history_entry(client, web_ctx):
    """Combines Phase 6 (retry = brand new job) with the point 10
    requirement end-to-end: after a retry AND the new job actually
    completing, History must hold BOTH the original failed attempt
    (untouched) and a new, separate entry for the new job -- never one
    overwriting the other."""
    item_id = _seed_library_item(web_ctx)
    res = client.post("/api/jobs", json={"library_item_ids": [item_id], "target_device_id": "mock-switch-parent"})
    old_job_id = res.json()["created"][0]["job_id"]
    with db.open_db(web_ctx.db_path) as conn:
        db.update_job_status(conn, old_job_id, "INTERRUPTED", error="device unplugged mid-transfer")
        db.record_install_history(
            conn, job_id=old_job_id, title_id="0100000000010000", display_name="Game",
            target_device_id="mock-switch-parent", target_storage="SD_INSTALL",
            outcome="INTERRUPTED", error="device unplugged mid-transfer", bytes_total=7,
        )

    retry_res = client.post(f"/api/jobs/{old_job_id}/retry")
    new_job_id = retry_res.json()["new_job_id"]
    with db.open_db(web_ctx.db_path) as conn:
        outcome = queue_worker.run_worker_once(conn, web_ctx.registry)
    assert outcome.job_id == new_job_id
    assert outcome.status == "DONE"

    history = client.get("/api/history").json()
    assert len(history) == 2
    by_job = {h["job_id"]: h for h in history}
    assert by_job[old_job_id]["outcome"] == "INTERRUPTED"  # original entry, byte-for-byte unchanged
    assert by_job[old_job_id]["error"] == "device unplugged mid-transfer"
    assert by_job[new_job_id]["outcome"] == "DONE"
    assert by_job[new_job_id]["error"] is None


# ---------------------------------------------------------------------------
# settings / worker control
# ---------------------------------------------------------------------------

def test_worker_pause_resume(client, web_ctx):
    assert client.get("/api/settings").json()["worker_paused"] is False
    client.post("/api/worker/pause")
    assert client.get("/api/settings").json()["worker_paused"] is True
    client.post("/api/worker/resume")
    assert client.get("/api/settings").json()["worker_paused"] is False


def test_settings_reports_library_dir(client, web_ctx):
    body = client.get("/api/settings").json()
    assert body["library_dir"] == str(config.LIBRARY_DIR)


def test_settings_reports_unconfigured_library_dir_honestly(client, web_ctx):
    """web_ctx points CONFIG_YAML_PATH at a nonexistent file and stubs
    known_folders.downloads_dir() to None -- library_dir_info() must
    report "not configured", not silently claim a real folder exists."""
    body = client.get("/api/settings").json()
    assert body["library_dir_configured"] is False
    assert body["library_dir_exists"] is False


def test_settings_reports_rar_extraction_availability(client, web_ctx, monkeypatch):
    from switchagent import extractor as extractor_mod

    monkeypatch.setattr(extractor_mod, "rar_backend_available", lambda: False)
    assert client.get("/api/settings").json()["rar_extraction_available"] is False

    monkeypatch.setattr(extractor_mod, "rar_backend_available", lambda: True)
    assert client.get("/api/settings").json()["rar_extraction_available"] is True


def test_settings_reports_app_version_and_runtime_mode(client, web_ctx):
    from switchagent import __version__

    body = client.get("/api/settings").json()
    assert body["app_version"] == __version__
    assert body["runtime_mode"] == "dev"


# ---------------------------------------------------------------------------
# W3-002: Library Settings -- change the library folder from the Web UI
# ---------------------------------------------------------------------------

def _seed_real_destination_conflict(web_ctx, client) -> tuple[int, int]:
    """W3-006: a REAL DESTINATION_CONFLICT, produced by the actual worker
    refusing to overwrite (not a hand-crafted error string) -- mirrors
    test_queue_worker.py::test_destination_conflict_records_install_history,
    through the HTTP layer this time. Returns (job_id, batch_id)."""
    item_id = _seed_library_item(web_ctx)
    parent = web_ctx.registry.get("mock-switch-parent")
    parent.connect()
    parent.storage_tree("SD_INSTALL").write_file("Game [0100000000010000][v0].nsp", b"someone else's file")

    res = client.post("/api/jobs", json={"library_item_ids": [item_id], "target_device_id": "mock-switch-parent"})
    job_id = res.json()["created"][0]["job_id"]
    batch_id = res.json()["batch_id"]

    with db.open_db(web_ctx.db_path) as conn:
        queue_worker.run_worker_once(conn, web_ctx.registry)
        assert db.get_job(conn, job_id)["status"] == "DESTINATION_CONFLICT"

    return job_id, batch_id


def test_destination_conflict_drops_out_of_queue_immediately(client, web_ctx):
    """Auto-resolved (abandoned=1) the instant it happens -- see
    queue_worker.py's FileAlreadyExistsError handling -- so it never
    lingers in the active Queue view waiting for a decision nobody needs
    to make."""
    job_id, batch_id = _seed_real_destination_conflict(web_ctx, client)

    assert all(j["id"] != job_id for j in client.get("/api/queue").json())
    groups = client.get("/api/queue/grouped").json()
    assert all(g["batch_id"] != batch_id for g in groups)

    html = client.get("/queue").text
    assert f'data-job-id="{job_id}"' not in html


def test_destination_conflict_path_still_reachable_by_direct_job_lookup(client, web_ctx):
    """destination_conflict_path is still real, useful information (the
    Device Details page shows it) even though the Queue page no longer
    surfaces this job at all -- GET /api/jobs/{id} looks a job up
    directly, unfiltered by whether it's still "active"."""
    job_id, _batch_id = _seed_real_destination_conflict(web_ctx, client)

    job = client.get(f"/api/jobs/{job_id}").json()
    assert job["destination_conflict_path"] == "Game [0100000000010000][v0].nsp"
    assert job["status"] == "DESTINATION_CONFLICT"
    assert job["abandoned"] is True


# ---------------------------------------------------------------------------
# W3-008: update availability notification
# ---------------------------------------------------------------------------

def test_get_update_check_returns_a_fresh_result(client, web_ctx, tmp_path, monkeypatch):
    from switchagent import update_check as update_check_mod

    # get_update_check_status() reads config.APP_DATA_ROOT directly (not
    # monkeypatched by the shared web_ctx fixture, which only isolates
    # INBOX_DIR/WORK_DIR/LIBRARY_DIR/CONFIG_YAML_PATH individually) -- must
    # isolate it here too, or this test would write a real cache file into
    # this project's actual data directory.
    monkeypatch.setattr(config, "APP_DATA_ROOT", tmp_path / "app_data")
    monkeypatch.setattr(update_check_mod, "_fetch_latest_release", lambda repo_slug: ("99.0.0", "https://example.invalid/v99"))

    body = client.get("/api/update-check").json()
    assert body["update_available"] is True
    assert body["latest_version"] == "99.0.0"
    assert body["release_url"] == "https://example.invalid/v99"
    assert body["error"] is None


def test_post_update_check_forces_a_fresh_check(client, web_ctx, tmp_path, monkeypatch):
    from switchagent import update_check as update_check_mod

    monkeypatch.setattr(config, "APP_DATA_ROOT", tmp_path / "app_data")
    calls = []
    monkeypatch.setattr(
        update_check_mod, "_fetch_latest_release",
        lambda repo_slug: (calls.append(1), ("1.0.0", "url"))[1],
    )

    client.get("/api/update-check")
    client.get("/api/update-check")  # within the interval -- must not call again
    assert len(calls) == 1

    client.post("/api/update-check")  # force=True -- must call regardless
    assert len(calls) == 2


def test_update_check_never_shows_an_update_when_offline(client, web_ctx, tmp_path, monkeypatch):
    from switchagent import update_check as update_check_mod

    monkeypatch.setattr(config, "APP_DATA_ROOT", tmp_path / "app_data")

    def _raise(repo_slug):
        raise OSError("no network")

    monkeypatch.setattr(update_check_mod, "_fetch_latest_release", _raise)

    res = client.get("/api/update-check")
    assert res.status_code == 200  # silent, non-fatal -- never a 500
    body = res.json()
    assert body["update_available"] is False
    assert body["error"] == "no network"


def test_settings_page_renders_update_check_widgets(client, web_ctx):
    html = client.get("/settings").text
    assert 'id="update-check-status"' in html
    assert 'id="update-check-link"' in html
    assert 'id="update-check-now-btn"' in html


def test_settings_page_renders_library_dir_form(client, web_ctx):
    html = client.get("/settings").text
    assert 'id="library-dir-list"' in html
    assert 'id="library-dir-manual-input"' in html
    assert 'id="library-dir-manual-add-btn"' in html
    assert 'id="library-dir-pick-btn"' in html
    assert '/static/settings.js' in html


def test_validate_library_dir_accepts_a_real_existing_directory(client, web_ctx, tmp_path):
    new_dir = tmp_path / "AnotherRealFolder"
    new_dir.mkdir()

    res = client.post("/api/settings/library-dir/validate", json={"path": str(new_dir)})
    body = res.json()
    assert body["valid"] is True
    assert body["reason"] is None
    assert body["canonical_path"] == str(new_dir.resolve())


def test_validate_library_dir_rejects_a_nonexistent_path(client, web_ctx, tmp_path):
    res = client.post("/api/settings/library-dir/validate", json={"path": str(tmp_path / "Ghost")})
    body = res.json()
    assert body["valid"] is False
    assert "does not exist" in body["reason"]


def test_validate_library_dir_rejects_a_file_not_a_directory(client, web_ctx, tmp_path):
    a_file = tmp_path / "not_a_folder.txt"
    a_file.write_text("x")

    res = client.post("/api/settings/library-dir/validate", json={"path": str(a_file)})
    body = res.json()
    assert body["valid"] is False
    assert "not a directory" in body["reason"]


def test_validate_library_dir_never_persists_anything(client, web_ctx, tmp_path):
    before = client.get("/api/settings").json()["library_dir"]
    new_dir = tmp_path / "ShouldNotBeSaved"
    new_dir.mkdir()

    client.post("/api/settings/library-dir/validate", json={"path": str(new_dir)})

    after = client.get("/api/settings").json()["library_dir"]
    assert after == before  # Validate alone must never change anything


def test_set_library_dir_persists_and_updates_settings(client, web_ctx, tmp_path):
    new_dir = tmp_path / "MyNewSwitchLibrary"
    new_dir.mkdir()

    res = client.post("/api/settings/library-dir", json={"path": str(new_dir)})
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["library_dir"] == str(new_dir.resolve())
    assert body["library_dir_configured"] is True
    assert body["library_dir_exists"] is True

    # Reflected on a fresh GET too, not just the mutation's own response.
    after = client.get("/api/settings").json()
    assert after["library_dir"] == str(new_dir.resolve())

    # Persisted to config.yaml on disk, not just the in-memory module attribute.
    from switchagent import config as config_mod
    assert config_mod.load_library_dir(config_mod.CONFIG_YAML_PATH) == new_dir.resolve()


def test_set_library_dir_rejects_invalid_path_with_400_and_leaves_old_config_untouched(client, web_ctx, tmp_path):
    before = client.get("/api/settings").json()["library_dir"]

    res = client.post("/api/settings/library-dir", json={"path": str(tmp_path / "DoesNotExist")})
    assert res.status_code == 400
    assert "does not exist" in res.json()["detail"]

    after = client.get("/api/settings").json()["library_dir"]
    assert after == before


def test_set_library_dir_never_creates_a_job(client, web_ctx, tmp_path):
    new_dir = tmp_path / "JobFreeLibrary"
    new_dir.mkdir()
    (new_dir / "Game [0100000000010000][v0].nsp").write_bytes(b"x")

    client.post("/api/settings/library-dir", json={"path": str(new_dir)})

    with db.open_db(web_ctx.db_path) as conn:
        assert db.list_jobs(conn) == []


def test_set_library_dir_rescans_the_new_directory_automatically(client, web_ctx, tmp_path):
    new_dir = tmp_path / "PreloadedLibrary"
    new_dir.mkdir()
    (new_dir / "Preloaded [0100000000077777][v0].nsp").write_bytes(b"payload")

    res = client.post("/api/settings/library-dir", json={"path": str(new_dir)})
    assert res.status_code == 200

    import time
    library_items = []
    for _ in range(50):
        library_items = client.get("/api/library").json()
        if library_items:
            break
        time.sleep(0.2)

    assert any("Preloaded" in item["name"] for item in library_items)


# ---------------------------------------------------------------------------
# diagnostics (UI-001) -- GET /api/diagnostics and the Settings page section.
# The engine itself (switchagent/diagnostics.py) has its own dedicated unit
# tests in tests/test_diagnostics.py; these cover only the HTTP/HTML wiring
# and the "never a live COM call from a request thread" invariant.
# ---------------------------------------------------------------------------

def test_api_diagnostics_returns_expected_checks(client, web_ctx):
    body = client.get("/api/diagnostics").json()
    names = {c["name"] for c in body["checks"]}
    for expected in ("Database", "Library", "Work directory", "Free disk",
                     "Watcher", "Worker", "RAR extraction", "MTP/COM", "Devices"):
        assert expected in names
    assert body["overall_status"] in ("OK", "WARNING", "ERROR")


def test_api_diagnostics_reflects_worker_pause_state(client, web_ctx):
    client.post("/api/worker/pause")
    body = client.get("/api/diagnostics").json()
    worker_check = next(c for c in body["checks"] if c["name"] == "Worker")
    assert worker_check["status"] == "WARNING"
    assert "paused" in worker_check["detail"]
    client.post("/api/worker/resume")


def test_api_diagnostics_never_leaks_a_raw_device_id(client, web_ctx):
    """web_ctx pre-registers mock devices named e.g. "mock-switch-parent"
    (see build_mock_context) -- these are already safe strings, so this
    test seeds a device_id shaped like a REAL raw WPD path (embedding a
    fake USB serial) directly into the devices table and asserts it never
    reaches the diagnostics payload, only its sha256 fingerprint does."""
    from switchagent import db as db_mod
    from switchagent.mtp.windows import device_fingerprint

    raw_id = r"usb#vid_057e&pid_3000#SERIALNUMBER1234#{fingerprint-guid}"
    with db_mod.open_db(web_ctx.db_path) as conn:
        db_mod.upsert_device_seen(conn, raw_id, "Switch")

    body = client.get("/api/diagnostics").json()
    payload_text = str(body)
    assert raw_id not in payload_text
    assert "SERIALNUMBER1234" not in payload_text
    assert device_fingerprint(raw_id) in body["device_fingerprints"]


def test_api_diagnostics_does_not_touch_com_from_the_request_thread(client, web_ctx, monkeypatch):
    """Structural regression guard for the same class of incident as
    test_list_devices_never_touches_com_from_a_request_thread above:
    enumerate_devices()/RealMtpBackend.connect() must never be imported or
    called while building the diagnostics report -- only the already-
    cached WebContext state and DB/filesystem reads."""
    import switchagent.mtp.windows as windows_mod

    def _boom(*args, **kwargs):
        raise AssertionError("diagnostics must never call enumerate_devices() from an HTTP request thread")

    monkeypatch.setattr(windows_mod, "enumerate_devices", _boom)
    res = client.get("/api/diagnostics")
    assert res.status_code == 200


def test_settings_page_renders_diagnostics_section_and_copy_button(client, web_ctx):
    html = client.get("/settings").text
    assert "Diagnostics" in html
    assert 'id="copy-diagnostics-btn"' in html
    assert 'id="diagnostics-text"' in html
    assert "/static/settings.js" in html


# ---------------------------------------------------------------------------
# UI-004: work directory cleanup. The eligibility/safety logic itself has
# its own dedicated unit tests in tests/test_work_cleanup.py; these cover
# only the HTTP/HTML wiring.
# ---------------------------------------------------------------------------

def _make_old_job_with_frozen_payload(web_ctx, client, *, size: int = 5000) -> tuple[int, "Path"]:
    from datetime import datetime, timedelta, timezone
    from switchagent import manifest as manifest_mod, work_cleanup

    item_id = _seed_library_item(web_ctx)
    res = client.post("/api/jobs", json={"library_item_ids": [item_id], "target_device_id": "mock-switch-parent"})
    job_id = res.json()["created"][0]["job_id"]
    with db.open_db(web_ctx.db_path) as conn:
        queue_worker.run_worker_once(conn, web_ctx.registry)  # -> DONE (mock backend)

    work_dir = manifest_mod.job_work_dir(job_id)
    (work_dir / "frozen-payload.bin").write_bytes(b"x" * size)

    old_finished = (
        datetime.now(timezone.utc) - timedelta(days=work_cleanup.DEFAULT_RETENTION_DAYS + 1)
    ).isoformat(timespec="seconds")
    with db.open_db(web_ctx.db_path) as conn:
        current_status = db.get_job(conn, job_id)["status"]
        db.update_job_status(conn, job_id, current_status, finished_at=old_finished)
    return job_id, work_dir


def test_api_work_cleanup_preview_reports_zero_when_nothing_eligible(client, web_ctx):
    body = client.get("/api/work-cleanup/preview").json()
    assert body["eligible_job_count"] == 0
    assert body["eligible_size_bytes"] == 0


def test_api_work_cleanup_execute_cleans_an_eligible_old_job(client, web_ctx):
    job_id, work_dir = _make_old_job_with_frozen_payload(web_ctx, client, size=5000)

    preview = client.get("/api/work-cleanup/preview").json()
    assert preview["eligible_job_count"] == 1
    assert preview["eligible_size_bytes"] == 5000
    assert preview["eligible_job_ids"] == [job_id]

    result = client.post("/api/work-cleanup").json()
    assert result["cleaned_job_count"] == 1
    assert result["freed_bytes"] == 5000
    assert not (work_dir / "frozen-payload.bin").exists()
    assert (work_dir / "manifest.json").exists()  # metadata preserved


def test_settings_page_renders_work_cleanup_section_disabled_when_nothing_eligible(client, web_ctx):
    html = client.get("/settings").text
    assert "Work directory" in html
    assert 'id="cleanup-btn" class="btn" disabled>' in html
    assert "Nothing eligible for cleanup right now." in html


def test_settings_page_enables_cleanup_button_when_something_eligible(client, web_ctx):
    _make_old_job_with_frozen_payload(web_ctx, client, size=2048)

    html = client.get("/settings").text
    assert 'id="cleanup-btn" class="btn" >' in html  # not disabled
    assert "1 completed job(s) eligible for cleanup" in html


# ---------------------------------------------------------------------------
# UI-008: GET /api/diagnostics/export
# ---------------------------------------------------------------------------

def test_export_diagnostics_json_default_includes_recent_job_errors_key(client, web_ctx):
    body = client.get("/api/diagnostics/export").json()
    assert "recent_job_errors" in body
    assert "log_tail" in body
    assert body["version"]


def test_export_diagnostics_txt_format_is_plain_text_and_matches_json_version(client, web_ctx):
    json_body = client.get("/api/diagnostics/export").json()
    res = client.get("/api/diagnostics/export?format=txt")
    assert res.status_code == 200
    assert "text/plain" in res.headers["content-type"]
    assert f"Version {json_body['version']}" in res.text


def test_export_diagnostics_includes_a_real_failed_jobs_error(client, web_ctx):
    from switchagent import db as db_mod

    library_dir = config.LIBRARY_DIR
    library_dir.mkdir(parents=True, exist_ok=True)
    path = _add_game(library_dir, name="Broken [0100000000099999][v0].nsp")
    with db_mod.open_db(web_ctx.db_path) as conn:
        item_id = db_mod.upsert_library_item(
            conn, absolute_path=str(path), item_type="FILE", file_type="NSP", size=path.stat().st_size,
            mtime=path.stat().st_mtime, content_hash="h", title_id="0100000000099999",
            title_id_source="filename", status="AVAILABLE", suggested_action="INSTALL_VIA_DBI",
            suggested_target="SD_INSTALL",
        )
        job_id = db_mod.create_job(
            conn, library_item_id=item_id, action="INSTALL_VIA_DBI", target_storage="SD_INSTALL",
            target_device_id="mock-switch-parent",
        )
        db_mod.update_job_status(conn, job_id, "FAILED", error="disk full")

    body = client.get("/api/diagnostics/export").json()
    errors = body["recent_job_errors"]
    assert any(e["id"] == job_id and e["error"] == "disk full" for e in errors)


def test_export_diagnostics_never_leaks_a_raw_device_id(client, web_ctx):
    from switchagent import db as db_mod
    from switchagent.mtp.windows import device_fingerprint

    raw_id = r"usb#vid_057e&pid_3000#SERIALNUMBER1234#{fingerprint-guid}"
    with db_mod.open_db(web_ctx.db_path) as conn:
        db_mod.upsert_device_seen(conn, raw_id, "Switch")

    text_export = client.get("/api/diagnostics/export?format=txt").text
    json_export = client.get("/api/diagnostics/export").text
    assert "SERIALNUMBER1234" not in text_export
    assert "SERIALNUMBER1234" not in json_export
    assert device_fingerprint(raw_id) in json_export


def test_export_diagnostics_never_leaks_a_failed_jobs_raw_target_device_id(client, web_ctx):
    """FIX-001 regression (independent audit, 2026-09-12): a job whose
    target_device_id is itself a serial-shaped raw string, with a REAL
    recorded error, used to leak that exact raw string into
    GET /api/diagnostics/export's JSON body via
    services.recent_job_errors() -> _job_view()'s unfiltered
    "target_device_id" field -> diagnostics.build_export_dict()'s
    `{**e, ...}` spread -- confirmed live three independent ways during
    the audit (static code read, a synthetic-serial script, and a live
    true-HTTP reproduction). Neither pre-existing "leak" test above could
    have caught this: one seeds a raw id only into the `devices` table
    (never a job's target_device_id), the other creates a real failed job
    but targets the already-safe literal "mock-switch-parent" -- this
    test is the one that actually combines both conditions, and checks
    the FULL export payload recursively, not just one known field."""
    import json as json_mod

    from switchagent import db as db_mod

    raw_id = r"\\?\usb#vid_057e&pid_201d#SERIAL_TEST_123456#{fix001-fake-guid}"
    library_dir = config.LIBRARY_DIR
    library_dir.mkdir(parents=True, exist_ok=True)
    path = _add_game(library_dir, name="Broken2 [0100000000088888][v0].nsp")
    with db_mod.open_db(web_ctx.db_path) as conn:
        db_mod.upsert_device_seen(conn, raw_id, "Switch")
        item_id = db_mod.upsert_library_item(
            conn, absolute_path=str(path), item_type="FILE", file_type="NSP", size=path.stat().st_size,
            mtime=path.stat().st_mtime, content_hash="h2", title_id="0100000000088888",
            title_id_source="filename", status="AVAILABLE", suggested_action="INSTALL_VIA_DBI",
            suggested_target="SD_INSTALL",
        )
        job_id = db_mod.create_job(
            conn, library_item_id=item_id, action="INSTALL_VIA_DBI", target_storage="SD_INSTALL",
            target_device_id=raw_id,
        )
        db_mod.update_job_status(conn, job_id, "FAILED", error="disk full")

    json_res = client.get("/api/diagnostics/export")
    txt_res = client.get("/api/diagnostics/export?format=txt")

    # Recursively check the ENTIRE JSON payload, not just one known field --
    # the whole point of this test is not trusting an allowlist of "the
    # fields we already thought to check".
    raw_payload_text = json_mod.dumps(json_res.json())
    assert raw_id not in raw_payload_text
    assert "SERIAL_TEST_123456" not in raw_payload_text
    assert raw_id not in txt_res.text
    assert "SERIAL_TEST_123456" not in txt_res.text

    # The safe stand-in must still be present -- this must be a real fix,
    # not just a field deleted/blanked out.
    errors = json_res.json()["recent_job_errors"]
    assert any(e["id"] == job_id and e["error"] == "disk full" for e in errors)


def test_export_diagnostics_sanitizes_a_real_log_tail(client, web_ctx, monkeypatch, tmp_path):
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    monkeypatch.setattr(config, "LOGS_DIR", logs_dir)
    raw_id = r"usb#vid_057e&pid_3000#SERIALNUMBER1234#{fingerprint-guid}"
    (logs_dir / "switchagent.log").write_text(f"2026-01-01 INFO connected to {raw_id}\n", encoding="utf-8")

    body = client.get("/api/diagnostics/export").json()
    assert body["log_tail"] is not None
    assert "SERIALNUMBER1234" not in body["log_tail"]


def test_logs_endpoint_returns_the_sanitized_log_as_plain_text(client, monkeypatch, tmp_path):
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    monkeypatch.setattr(config, "LOGS_DIR", logs_dir)
    raw_id = r"usb#vid_057e&pid_3000#SERIALNUMBER1234#{fingerprint-guid}"
    (logs_dir / "switchagent.log").write_text(
        f"2026-01-01 INFO first line\n2026-01-01 INFO connected to {raw_id}\n", encoding="utf-8")

    res = client.get("/api/logs")
    assert res.status_code == 200
    assert "text/plain" in res.headers["content-type"]
    assert "first line" in res.text
    assert "SERIALNUMBER1234" not in res.text


def test_logs_endpoint_is_empty_without_a_log_file(client, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "LOGS_DIR", tmp_path / "missing")
    res = client.get("/api/logs")
    assert res.status_code == 200
    assert res.text == ""


def test_settings_page_has_copy_logs_button(client):
    assert 'id="copy-logs-btn"' in client.get("/settings").text


# ---------------------------------------------------------------------------
# health (readiness / single-instance detection / packaged smoke tests)
# ---------------------------------------------------------------------------

def test_health_endpoint_reports_app_name_version_and_ok_status(client):
    from switchagent import __version__

    body = client.get("/api/health").json()
    assert body == {"app": "SwitchAgent", "version": __version__, "status": "ok"}


# ---------------------------------------------------------------------------
# HTML pages render without error (smoke coverage for the Jinja templates)
# ---------------------------------------------------------------------------

def test_worker_poll_interval_is_responsive():
    """Guards against regressing back to the original 5s default that,
    combined with the old reload-based Queue polling, made a fast real
    install (~15s total, see docs/REAL-HARDWARE-TEST-*.md) nearly
    invisible in the UI."""
    from switchagent.web import context
    assert context.DEFAULT_WORKER_POLL_INTERVAL_SECONDS <= 2.0


def test_queue_page_has_live_polling_hook_elements(client, web_ctx):
    """Structural guard: queue.js's live-polling JS looks these elements
    up by id -- if a template edit ever renames/removes them, the page
    would silently fall back to being a static, one-time snapshot again."""
    html = client.get("/queue").text
    assert 'id="job-list"' in html
    assert 'id="empty-state"' in html
    assert 'id="toast-container"' in html
    assert '/static/queue.js' in html


@pytest.mark.parametrize("path", [
    "/", "/?kind=games", "/?kind=updates", "/?kind=dlc", "/?kind=mods",
    "/queue", "/history", "/devices", "/settings",
])
def test_html_pages_render(client, path):
    """Covers every Library `kind` value through the real Jinja template,
    not just the default -- a prior version of this had a bug
    (`view.items` colliding with dict.items() under Jinja2's dot-notation
    resolution) that only 500'd for kind=updates/dlc/mods, which the
    original version of this test (default path only) never exercised."""
    res = client.get(path)
    assert res.status_code == 200
    assert "text/html" in res.headers["content-type"]


def test_library_grouped_view_renders_real_variant_data(client, web_ctx):
    """End-to-end: seed a base+update+dlc+mod through the real DB layer and
    confirm each Library `kind` renders the right content, not just 200.
    Uses distinctive names (not the words "update"/"dlc"/"mod" alone,
    which also appear in the toolbar's own filter <option> labels
    regardless of data) so a passing assertion actually proves something."""
    with db.open_db(web_ctx.db_path) as conn:
        db.upsert_library_item(
            conn, absolute_path=str(config.LIBRARY_DIR / "ZzzQuest.nsp"), item_type="FILE", file_type="NSP",
            size=100, mtime=0.0, content_hash="b1", title_id="0100000000010000", title_id_source="filename",
            status="AVAILABLE", suggested_action="INSTALL_VIA_DBI", suggested_target="SD_INSTALL",
            content_type="GAME_PACKAGE", package_format="NSP",
        )
        db.upsert_library_item(
            conn, absolute_path=str(config.LIBRARY_DIR / "ZzzQuest Patch.nsp"), item_type="FILE", file_type="NSP",
            size=50, mtime=0.0, content_hash="u1", title_id="0100000000010800", title_id_source="filename",
            status="AVAILABLE", suggested_action="INSTALL_VIA_DBI", suggested_target="SD_INSTALL",
            content_type="GAME_PACKAGE", package_format="NSP",
        )
        db.upsert_library_item(
            conn, absolute_path=str(config.LIBRARY_DIR / "ZzzQuest Bonus Chapter.nsp"), item_type="FILE",
            file_type="NSP", size=10, mtime=0.0, content_hash="d1", title_id="0100000000011001",
            title_id_source="filename", status="AVAILABLE", suggested_action="INSTALL_VIA_DBI",
            suggested_target="SD_INSTALL", content_type="GAME_PACKAGE", package_format="NSP",
        )
        db.upsert_library_item(
            conn, absolute_path=str(config.LIBRARY_DIR / "ZzzQuest Translation Pack"), item_type="MOD_FOLDER",
            file_type="ATMOSPHERE_MOD", size=5, mtime=0.0, content_hash="m1", title_id="0100000000010000",
            title_id_source="atmosphere_path", title_id_confident=True, status="AVAILABLE",
            suggested_action="COPY_MERGE", suggested_target="SD_CARD", content_type="ATMOSPHERE_MOD",
        )

    games_html = client.get("/?kind=games").text
    assert "ZzzQuest" in games_html
    assert "1 update(s), 1 DLC, 1 mod(s)" in games_html
    # A matching mod IS cross-referenced into the game's own nested variant
    # list (so "select the whole game" can select it too) -- it's still
    # never a top-level "games" card by itself, only nested. Its OWN raw
    # folder name ("ZzzQuest Translation Pack") never appears anywhere --
    # it resolves to the base game's name instead (same fix as the real
    # Bread and Fred bug report, see tests/test_library_grouping.py), so
    # "Mod — <resolved name>" is what actually renders.
    assert "Mod — ZzzQuest" in games_html
    assert "ZzzQuest Translation Pack" not in games_html
    # TITLE_ID remains visible as technical info even though it's no
    # longer used as the display name.
    assert "0100000000010000" in games_html

    updates_html = client.get("/?kind=updates").text
    assert "ZzzQuest Patch" in updates_html
    assert "ZzzQuest Bonus Chapter" not in updates_html  # that's DLC, not an update

    dlc_html = client.get("/?kind=dlc").text
    assert "ZzzQuest Bonus Chapter" in dlc_html
    assert "ZzzQuest Patch" not in dlc_html

    mods_html = client.get("/?kind=mods").text
    assert "ZzzQuest" in mods_html
    assert "ZzzQuest Translation Pack" not in mods_html
    assert "0100000000010000" in mods_html


def test_games_view_marks_base_checkbox_and_shares_family_with_siblings(client, web_ctx):
    """Server-side half of the "select a game selects everything" UX:
    library.js cascades from data-role="base" to every checkbox sharing
    the same data-family. This proves the template actually emits those
    attributes, consistently, for base/update/dlc/mod -- without them the
    JS cascade silently does nothing."""
    with db.open_db(web_ctx.db_path) as conn:
        db.upsert_library_item(
            conn, absolute_path=str(config.LIBRARY_DIR / "Base.nsp"), item_type="FILE", file_type="NSP",
            size=100, mtime=0.0, content_hash="b1", title_id="0100000000030000", title_id_source="filename",
            status="AVAILABLE", suggested_action="INSTALL_VIA_DBI", suggested_target="SD_INSTALL",
            content_type="GAME_PACKAGE", package_format="NSP",
        )
        db.upsert_library_item(
            conn, absolute_path=str(config.LIBRARY_DIR / "Update.nsp"), item_type="FILE", file_type="NSP",
            size=50, mtime=0.0, content_hash="u1", title_id="0100000000030800", title_id_source="filename",
            status="AVAILABLE", suggested_action="INSTALL_VIA_DBI", suggested_target="SD_INSTALL",
            content_type="GAME_PACKAGE", package_format="NSP",
        )

    html = client.get("/?kind=games").text
    assert 'data-family="0100000000030000" data-role="base"' in html
    # the update's checkbox carries the SAME family id but never the base role
    assert "game-variants" in html
    variants_section = html.split("game-variants", 1)[1]
    assert 'data-family="0100000000030000"' in variants_section
    assert 'data-role="base"' not in variants_section


# ---------------------------------------------------------------------------
# STAB-001: integration regression across UI-001..008 combinations that the
# per-feature tests elsewhere in this file (and in test_queue_worker.py)
# each only exercise in isolation.
# ---------------------------------------------------------------------------

def test_stab001_batch_waiting_for_device_resumes_all_siblings_when_device_reappears(client, web_ctx):
    """Batch (UI-002) x WAITING_FOR_DEVICE (UI-005): every never-attempted
    job in a batch must reach WAITING_FOR_DEVICE while its shared target
    device is absent -- not get stuck, not silently redirect to a
    different device -- and each one must resume normally, still part of
    the SAME original batch, the moment that same device reappears."""
    # Quake II + Sacred 2 -- two unrelated titles, deliberately not the
    # Bread and Fred base+update pair, so this test's assertions are about
    # WAITING_FOR_DEVICE only, never entangled with Base/Update dependency
    # ordering (a separate concern, already covered by test_install_order.py).
    item_ids = _seed_four_items(web_ctx)[2:4]
    res = client.post("/api/jobs", json={"library_item_ids": item_ids, "target_device_id": "mock-switch-parent"})
    batch_id = res.json()["batch_id"]
    job_ids = [c["job_id"] for c in res.json()["created"]]

    parent = web_ctx.registry.get("mock-switch-parent")
    parent.set_device_present(False)
    with db.open_db(web_ctx.db_path) as conn:
        queue_worker.run_worker_once(conn, web_ctx.registry)  # one pass covers both -- same absent device

    with db.open_db(web_ctx.db_path) as conn:
        for job_id in job_ids:
            row = db.get_job(conn, job_id)
            assert row["status"] == "WAITING_FOR_DEVICE"
            assert row["batch_id"] == batch_id

    groups = client.get("/api/queue/grouped").json()
    assert len(groups) == 1
    assert groups[0]["batch_id"] == batch_id
    assert groups[0]["waiting"] == 2

    parent.set_device_present(True)
    with db.open_db(web_ctx.db_path) as conn:
        for _ in job_ids:
            queue_worker.run_worker_once(conn, web_ctx.registry)

    with db.open_db(web_ctx.db_path) as conn:
        for job_id in job_ids:
            row = db.get_job(conn, job_id)
            assert row["status"] == "DONE"
            assert row["batch_id"] == batch_id  # still the same original batch throughout


def test_stab001_verifying_one_batch_members_history_does_not_affect_sibling(client, web_ctx):
    """Batch (UI-002) x DONE_UNVERIFIED verification (UI-003): user
    verification of one batch member's install_history must never leak
    onto a sibling job's own history row, even though both share a
    batch_id and finished with the identical transport outcome."""
    item_ids = _seed_four_items(web_ctx)[2:4]
    res = client.post("/api/jobs", json={"library_item_ids": item_ids, "target_device_id": "mock-switch-parent"})
    job_a, job_b = (c["job_id"] for c in res.json()["created"])

    with db.open_db(web_ctx.db_path) as conn:
        db.update_job_status(conn, job_a, "DONE_UNVERIFIED", finished_at=db.now_iso())
        db.update_job_status(conn, job_b, "DONE_UNVERIFIED", finished_at=db.now_iso())
        history_a = db.record_install_history(
            conn, job_id=job_a, title_id="010048F0195E8000", display_name="Quake II",
            target_device_id="mock-switch-parent", target_storage="SD_INSTALL",
            outcome="DONE_UNVERIFIED", bytes_total=1000,
        )
        history_b = db.record_install_history(
            conn, job_id=job_b, title_id="010074402766A000", display_name="Sacred 2",
            target_device_id="mock-switch-parent", target_storage="SD_INSTALL",
            outcome="DONE_UNVERIFIED", bytes_total=1000,
        )

    res = client.post(f"/api/history/{history_a}/verification", json={"outcome": "SUCCESS"})
    assert res.status_code == 200

    with db.open_db(web_ctx.db_path) as conn:
        assert db.get_install_history_by_id(conn, history_a)["user_verified_outcome"] == "SUCCESS"
        assert db.get_install_history_by_id(conn, history_b)["user_verified_outcome"] is None


def test_stab001_cleanup_and_retry_eligible_statuses_never_overlap():
    """Cleanup (UI-004) only ever targets DONE/DONE_UNVERIFIED (terminal,
    successful-transport); retry only ever targets the failure-ish
    statuses. If these two sets ever overlapped, a job's staged manifest/
    payload could be cleaned up out from under a retry that still needed
    to read it -- confirmed disjoint by construction, not just by
    coincidence of today's two lists."""
    from switchagent import work_cleanup
    from switchagent.web import services

    assert set(work_cleanup.CLEANUP_ELIGIBLE_STATUSES).isdisjoint(services._RETRYABLE_JOB_STATUSES)


def test_stab001_stall_flag_never_applies_once_a_job_leaves_running(client, web_ctx):
    """Stall detection (UI-006) must stop applying the instant a job
    leaves RUNNING, for any reason, even if last_progress_at is left stale
    by that transition -- _stall_seconds() is unconditionally RUNNING-only
    by construction, never re-evaluates a terminal/waiting status's old
    timestamp as "still stalled". Uses the REAL crash-recovery path
    (db.recover_stale_running_jobs(), WebContext's own startup-only call)
    to end the RUNNING state -- run_worker_once() itself never revisits an
    already-RUNNING job at all (see queue_worker.py: it only ever picks up
    jobs from db.list_confirmed_jobs(), which does not include RUNNING),
    so that is not a path that could ever "resume" a stalled job here."""
    from datetime import datetime, timedelta, timezone

    item_id = _seed_library_item(web_ctx)
    res = client.post("/api/jobs", json={"library_item_ids": [item_id], "target_device_id": "mock-switch-parent"})
    job_id = res.json()["created"][0]["job_id"]

    stale = (datetime.now(timezone.utc) - timedelta(seconds=600)).isoformat()
    with db.open_db(web_ctx.db_path) as conn:
        db.update_job_status(
            conn, job_id, "RUNNING", started_at=stale, last_progress_at=stale, increment_attempt=True,
        )
    queue = {j["id"]: j for j in client.get("/api/queue").json()}
    assert queue[job_id]["possibly_stalled"] is True  # sanity: genuinely flagged while RUNNING

    with db.open_db(web_ctx.db_path) as conn:
        recovered = db.recover_stale_running_jobs(conn)
        assert recovered == 1
        assert db.get_job(conn, job_id)["status"] == "INTERRUPTED"

    queue_after = {j["id"]: j for j in client.get("/api/queue").json()}
    assert queue_after[job_id]["possibly_stalled"] is False
    assert queue_after[job_id]["stall_seconds"] is None


# ---------------------------------------------------------------------------
# Static asset cache-busting. Hand-maintained `?v=N` numbers cost this
# project two shipped-but-invisible changes in one sitting: app.js carried
# no version at all, and settings.js kept serving the previous build's
# "At least one folder is required" long after that rule was gone from it.
# ---------------------------------------------------------------------------

def _static_refs(html: str) -> list[str]:
    return re.findall(r'(?:src|href)="(/static/[^"]+)"', html)


@pytest.mark.parametrize("path", ["/", "/queue", "/history", "/devices", "/settings"])
def test_every_static_asset_is_cache_busted(client, path):
    refs = _static_refs(client.get(path).text)
    assert refs, f"{path} loads no static assets at all -- did the markup change?"
    unversioned = [r for r in refs if "?v=" not in r]
    assert not unversioned, f"{path} would serve these from a stale cache forever: {unversioned}"


def test_the_cache_key_changes_when_the_file_does(client, tmp_path, monkeypatch):
    """A version that does not move when the file moves is worse than
    none: it reads as protection while providing none."""
    from switchagent.web import app as app_mod

    before = _static_refs(client.get("/").text)
    assert before == _static_refs(client.get("/").text), "the same bytes must keep the same URL"

    static_dir = app_mod._WEB_DIR / "static"
    original = (static_dir / "app.js").read_bytes()
    try:
        (static_dir / "app.js").write_bytes(original + b"\n// touched\n")
        after = _static_refs(client.get("/").text)
    finally:
        (static_dir / "app.js").write_bytes(original)

    changed = set(after) - set(before)
    assert any("app.js" in ref for ref in changed), "editing app.js must change its URL"


def test_a_missing_static_file_does_not_break_the_page(client):
    """A 404 in the network tab beats a page that will not render."""
    from switchagent.web import app as app_mod

    assert app_mod._static_url("no-such-file.js") == "/static/no-such-file.js"


# ---------------------------------------------------------------------------
# Queue dock: one row per game along the bottom of every page
# ---------------------------------------------------------------------------

def test_queue_dock_is_empty_with_nothing_queued(client):
    assert client.get("/api/queue/dock").json() == {"games": [], "paused": False}


def test_queue_dock_shows_one_row_per_game_not_per_file(client, web_ctx):
    item_ids = _seed_four_items(web_ctx)
    client.post("/api/jobs", json={"library_item_ids": item_ids, "target_device_id": "mock-switch-parent"})

    games = client.get("/api/queue/dock").json()["games"]
    assert [g["name"] for g in games] == ["Bread and Fred", "Quake II", "Sacred 2"]
    bread = games[0]
    assert bread["cover_id"] == "0100AF401B6A4000"
    assert bread["parts_total"] == 2
    assert bread["parts_label"] == "Game · Update"
    assert bread["state"] == "waiting"
    assert bread["status_label"] == "Queued"


def test_queue_dock_puts_the_installing_game_first_with_its_bytes(client, web_ctx):
    item_ids = _seed_four_items(web_ctx)
    created = client.post("/api/jobs", json={
        "library_item_ids": item_ids, "target_device_id": "mock-switch-parent",
    }).json()["created"]
    by_item = {c["library_item_id"]: c["job_id"] for c in created}
    with db.open_db(web_ctx.db_path) as conn:
        db.update_job_status(conn, by_item[item_ids[3]], "RUNNING", bytes_total=1000, bytes_done=250)

    games = client.get("/api/queue/dock").json()["games"]
    assert games[0]["name"] == "Sacred 2"
    assert games[0]["state"] == "installing"
    assert (games[0]["bytes_done"], games[0]["bytes_total"]) == (250, 1000)


def test_queue_dock_counts_a_finished_part_and_drops_a_finished_game(client, web_ctx):
    item_ids = _seed_four_items(web_ctx)
    created = client.post("/api/jobs", json={
        "library_item_ids": item_ids, "target_device_id": "mock-switch-parent",
    }).json()["created"]
    by_item = {c["library_item_id"]: c["job_id"] for c in created}
    with db.open_db(web_ctx.db_path) as conn:
        db.update_job_status(conn, by_item[item_ids[0]], "DONE_UNVERIFIED", finished_at=db.now_iso())
        db.update_job_status(conn, by_item[item_ids[2]], "DONE_UNVERIFIED", finished_at=db.now_iso())

    games = {g["name"]: g for g in client.get("/api/queue/dock").json()["games"]}
    assert "Quake II" not in games
    assert games["Bread and Fred"]["parts_done"] == 1
    assert games["Bread and Fred"]["bytes_done"] > 0


def test_queue_dock_marks_a_failed_part_as_needing_a_decision(client, web_ctx):
    item_ids = _seed_four_items(web_ctx)
    created = client.post("/api/jobs", json={
        "library_item_ids": item_ids[2:3], "target_device_id": "mock-switch-parent",
    }).json()["created"]
    with db.open_db(web_ctx.db_path) as conn:
        db.update_job_status(conn, created[0]["job_id"], "FAILED", error="disk full")

    [game] = client.get("/api/queue/dock").json()["games"]
    assert game["state"] == "attention"
    assert game["status_label"] == "Needs a decision in Queue"


def test_queue_dock_shows_an_item_still_being_prepared(client, web_ctx, monkeypatch):
    item_ids = _seed_four_items(web_ctx)
    monkeypatch.setattr(web_ctx.preparations, "dock_items", lambda: [{
        "library_item_id": item_ids[2], "phase": "Analyzing", "error": None, "job_ids": [],
        "name": "Quake II [010048F0195E8000][v0].nsp", "role": "base", "started": 0.0, "order": 0,
    }])

    [game] = client.get("/api/queue/dock").json()["games"]
    assert game["name"] == "Quake II"
    assert game["state"] == "preparing"
    assert game["bytes_total"] > 0


def test_every_page_carries_the_queue_dock(client):
    for path in ("/", "/queue", "/history", "/devices", "/settings"):
        assert 'id="queue-dock"' in client.get(path).text, path


def test_queue_rows_carry_their_games_cover_id(client, web_ctx):
    """Queue draws each row's game cover: an update shows its base game's."""
    item_ids = _seed_four_items(web_ctx)
    client.post("/api/jobs", json={"library_item_ids": item_ids[:2], "target_device_id": "mock-switch-parent"})

    jobs = [j for g in client.get("/api/queue/grouped").json() for j in g["jobs"]]
    assert {j["cover_id"] for j in jobs} == {"0100AF401B6A4000"}
    assert 'class="game-thumb game-group-thumb"' in client.get("/queue").text


def test_queue_groups_one_card_per_game_with_a_row_per_package(client, web_ctx):
    item_ids = _seed_four_items(web_ctx)
    client.post("/api/jobs", json={"library_item_ids": item_ids, "target_device_id": "mock-switch-parent"})

    [batch] = client.get("/api/queue/grouped").json()
    cards = [(g["cover_id"], len(g["jobs"])) for g in batch["games"]]
    assert cards == [("0100AF401B6A4000", 2), ("010048F0195E8000", 1), ("010074402766A000", 1)]
    html = client.get("/queue").text
    assert html.count('class="game-group"') == 3
    assert html.count('class="seg-bar job-bar') == 4


def test_queue_page_switches_the_dock_off(client):
    assert 'id="queue-dock" class="queue-dock" aria-label="Queue" hidden data-off' in client.get("/queue").text
    assert "data-off" not in client.get("/history").text


def test_about_page_shows_version_and_links(client):
    from switchagent import __version__, about

    html = client.get("/about").text
    assert f"v{__version__}" in html
    assert about.REPO_URL in html and about.ISSUE_URL in html
    assert about.TELEGRAM_CHAT_URL in html
    assert 'href="/about"' in client.get("/settings").text
