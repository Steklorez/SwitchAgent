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
