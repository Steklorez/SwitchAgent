"""A port whose forwarder gives nothing away -- no [TITLE_ID] in its name,
its NCAs encrypted -- is still one card, installed together, when its
release folder says what it is. Laid out like the real Need for Speed: Most
Wanted port (2026-09-28)."""

from __future__ import annotations

import shutil

from switchagent import config, db, queue_worker, scanner, sd_files
from switchagent.model import ContentType
from switchagent.mtp import MockMtpBackend
from switchagent.web import services

from .test_loose_nro import make_nro
from .test_web_api import client, web_ctx  # noqa: F401 -- fixtures

DEVICE = "mock-switch"
FORWARDER = b"PFS0" + bytes(4000)  # encrypted NCAs: nothing readable inside


def _release(root, name="Need-For-Speed-Most-Wanted", nsp="Need-for-Speed-Most-Wanted.nsp"):
    release = root / name
    app = release / "switch" / "nfsmw-nx"
    (app / "game_root").mkdir(parents=True)
    (release / nsp).write_bytes(FORWARDER)
    (app / "nfsmw-nx.nro").write_bytes(make_nro("nfsmw-nx"))
    (app / "nfsmw.toml").write_bytes(b"[game]")
    (app / "game_root" / "default.xex").write_bytes(b"xex")
    return release


def _scan(conn, monkeypatch):
    monkeypatch.setattr(scanner, "is_file_stable", lambda _p: True)
    scanner.scan_library_once(conn)


def _row(conn, path):
    return db.get_library_item(conn, str(path))


def test_the_forwarder_is_installable_and_its_files_are_its_own(isolated_db, monkeypatch):
    conn, _ = isolated_db
    release = _release(config.LIBRARY_DIR)
    _scan(conn, monkeypatch)

    nsp = _row(conn, release / "Need-for-Speed-Most-Wanted.nsp")
    files = _row(conn, release / "switch")
    assert nsp["status"] == "AVAILABLE"
    assert nsp["title_id"] is None
    assert nsp["title_id_source"] == sd_files.SEALED_FORWARDER
    assert files["title_id_source"] == sd_files.PORT_PART


def test_it_is_one_card_not_two(isolated_db, monkeypatch):
    conn, _ = isolated_db
    _release(config.LIBRARY_DIR)
    _scan(conn, monkeypatch)

    games = services.list_library_view(conn)["games"]

    assert len(games) == 1
    game = games[0]
    assert game["name"] == "Need-for-Speed-Most-Wanted"
    assert game["base"]["can_install"]
    assert [e["content_type"] for e in game["sd_files"]] == [ContentType.SD_FILES.value]


def test_installing_the_card_puts_everything_where_it_belongs(isolated_db, monkeypatch):
    conn, _ = isolated_db
    release = _release(config.LIBRARY_DIR)
    _scan(conn, monkeypatch)
    ids = [_row(conn, release / "Need-for-Speed-Most-Wanted.nsp")["id"], _row(conn, release / "switch")["id"]]

    result = services.create_and_confirm_jobs(conn, ids, DEVICE)
    assert not result["errors"], result
    backend = MockMtpBackend(device_id=DEVICE)
    backend.add_storage("SD_CARD")
    backend.add_storage("SD_INSTALL")
    registry = queue_worker.DeviceRegistry()
    registry.register(DEVICE, backend)
    while queue_worker.run_worker_once(conn, registry) is not None:
        pass

    assert backend.storage_tree("SD_INSTALL").list_files() == ["Need-for-Speed-Most-Wanted.nsp"]
    assert sorted(backend.storage_tree("SD_CARD").list_files()) == [
        "switch/nfsmw-nx/game_root/default.xex",
        "switch/nfsmw-nx/nfsmw-nx.nro",
        "switch/nfsmw-nx/nfsmw.toml",
    ]


def test_two_such_packages_in_one_folder_are_not_guessed_between(isolated_db, monkeypatch):
    conn, _ = isolated_db
    release = _release(config.LIBRARY_DIR)
    (release / "Another.nsp").write_bytes(FORWARDER + b"x")
    _scan(conn, monkeypatch)

    assert _row(conn, release / "Need-for-Speed-Most-Wanted.nsp")["status"] == "NEEDS_REVIEW"
    assert _row(conn, release / "Another.nsp")["status"] == "NEEDS_REVIEW"


def test_a_real_game_in_the_folder_keeps_its_files(isolated_db, monkeypatch):
    conn, _ = isolated_db
    release = _release(config.LIBRARY_DIR)
    (release / "Game [0100000000010000][v0].nsp").write_bytes(b"game")
    _scan(conn, monkeypatch)

    assert _row(conn, release / "Need-for-Speed-Most-Wanted.nsp")["status"] == "NEEDS_REVIEW"
    assert _row(conn, release / "switch")["title_id"] == "0100000000010000"


def test_a_big_package_is_not_a_forwarder(isolated_db, monkeypatch):
    conn, _ = isolated_db
    monkeypatch.setattr(sd_files, "FORWARDER_MAX_BYTES", 100)
    release = _release(config.LIBRARY_DIR)
    _scan(conn, monkeypatch)

    assert _row(conn, release / "Need-for-Speed-Most-Wanted.nsp")["status"] == "NEEDS_REVIEW"


def test_without_its_switch_folder_it_goes_back_to_needing_review(isolated_db, monkeypatch):
    conn, _ = isolated_db
    release = _release(config.LIBRARY_DIR)
    _scan(conn, monkeypatch)
    shutil.rmtree(release / "switch")
    _scan(conn, monkeypatch)

    nsp = _row(conn, release / "Need-for-Speed-Most-Wanted.nsp")
    assert nsp["status"] == "NEEDS_REVIEW"
    assert nsp["title_id_source"] is None


def test_the_rule_applies_at_start_without_a_scan(tmp_path, monkeypatch):
    """After an update that learned the rule, the Library groups the port
    as soon as the app starts -- nothing scans at start."""
    import time

    from switchagent import known_folders
    from switchagent.web.context import build_mock_context

    library = tmp_path / "library"
    library.mkdir()
    monkeypatch.setattr(config, "LIBRARY_DIR", library)
    monkeypatch.setattr(config, "WORK_DIR", tmp_path / "work")
    monkeypatch.setattr(config, "INBOX_DIR", tmp_path / "inbox")
    monkeypatch.setattr(config, "CONFIG_YAML_PATH", tmp_path / "config.yaml")
    monkeypatch.setattr(known_folders, "downloads_dir", lambda: None)
    release = _release(library)
    db_path = tmp_path / "test.db"
    with db.open_db(db_path) as conn:
        _scan(conn, monkeypatch)
        nsp = _row(conn, release / "Need-for-Speed-Most-Wanted.nsp")
        # What an index written by a version without the rule looks like.
        db.set_library_item_sealed_forwarder(conn, nsp["id"], False)
        db.set_library_item_title_id(conn, _row(conn, release / "switch")["id"], title_id=None, source=None)

    ctx = build_mock_context(db_path)
    ctx.start_worker()
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            with db.open_db(db_path) as conn:
                if _row(conn, release / "Need-for-Speed-Most-Wanted.nsp")["status"] == "AVAILABLE":
                    break
            time.sleep(0.1)
    finally:
        ctx.stop_worker()
    with db.open_db(db_path) as conn:
        assert _row(conn, release / "Need-for-Speed-Most-Wanted.nsp")["status"] == "AVAILABLE"
        assert _row(conn, release / "switch")["title_id_source"] == sd_files.PORT_PART


# -- install order: a port's files first, its forwarder last ------------------

def test_a_sealed_ports_files_go_before_its_forwarder_in_one_chain(isolated_db, monkeypatch):
    from switchagent.web.preparation import _group_by_title

    conn, _ = isolated_db
    release = _release(config.LIBRARY_DIR)
    _scan(conn, monkeypatch)
    nsp = _row(conn, release / "Need-for-Speed-Most-Wanted.nsp")["id"]
    files = _row(conn, release / "switch")["id"]

    chains = _group_by_title(conn, [nsp, files])

    assert list(chains.values()) == [[files, nsp]]


def test_a_titled_ports_files_go_before_its_forwarder(isolated_db, monkeypatch):
    from switchagent.web.preparation import _group_by_title

    conn, _ = isolated_db
    release = _release(config.LIBRARY_DIR, nsp="Port [0100000000010000][v0].nsp")
    _scan(conn, monkeypatch)
    nsp = _row(conn, release / "Port [0100000000010000][v0].nsp")["id"]
    files = _row(conn, release / "switch")["id"]

    assert _group_by_title(conn, [nsp, files]) == {"0100000000010000": [files, nsp]}


def test_a_companion_app_that_came_with_the_game_still_goes_after_it(isolated_db, monkeypatch):
    from switchagent.web.preparation import _variant_rank

    conn, _ = isolated_db
    release = _release(config.LIBRARY_DIR, nsp="Port [0100000000010000][v0].nsp")
    _scan(conn, monkeypatch)
    row = _row(conn, release / "switch")
    db.set_library_item_title_id(conn, row["id"], title_id="0100000000010000", source=sd_files.RELEASE_PART)

    assert _variant_rank(_row(conn, release / "switch")) > _variant_rank(
        _row(conn, release / "Port [0100000000010000][v0].nsp"))


def test_the_card_says_the_size_of_the_whole_game(web_ctx, client, monkeypatch):
    """A port's card is its 4 KB forwarder, but installing it is the whole
    switch/ folder too: the card shows what the game weighs, not its base."""
    import re

    release = _release(config.LIBRARY_DIR)
    with db.open_db(web_ctx.db_path) as conn:
        _scan(conn, monkeypatch)
        expected = sum(
            db.get_library_item(conn, str(p))["size"]
            for p in (release / "Need-for-Speed-Most-Wanted.nsp", release / "switch")
        )

    page = client.get("/").text

    sizes = re.findall(r'class="card-size"\s+data-card-family="port-\d+" data-total="(\d+)"', page)
    assert sizes == [str(expected)]


def test_queue_lists_a_port_in_install_order_not_click_order(isolated_db, monkeypatch, tmp_path):
    """Ticked forwarder-first, the port was listed forwarder-first -- on top,
    "Ready to install", while its files copied below it: it looked as if the
    first item were being skipped (field report, 2026-09-28)."""
    from switchagent.web.preparation import PreparationQueue

    conn, _ = isolated_db
    release = _release(config.LIBRARY_DIR)
    _scan(conn, monkeypatch)
    nsp = _row(conn, release / "Need-for-Speed-Most-Wanted.nsp")["id"]
    files = _row(conn, release / "switch")["id"]
    monkeypatch.setattr(PreparationQueue, "_install_sequentially",
                        lambda *_a, **_k: {"created": [], "errors": [], "batch_id": None, "blocked": []})
    queue = PreparationQueue(tmp_path / "test.db")

    queue.submit([nsp, files], DEVICE)

    items = next(iter(queue.states.values()))["items"]
    assert items[str(files)]["order"] < items[str(nsp)]["order"]
