"""Deleting Library files from the PC (library_delete.py): the Recycle Bin
is the default and is never silently replaced by a permanent delete; nothing
outside a Library folder, no link, and nothing an install still needs goes."""

from __future__ import annotations

import os
import shutil

import pytest

from switchagent import config, db, library_delete, scanner

from .conftest import choose_library_folder


@pytest.fixture
def lib(isolated_db):
    conn, _inbox = isolated_db
    choose_library_folder()
    return conn, config.LIBRARY_DIR


def _game(folder, name="Game [0100000000010000][v0].nsp", size=2048):
    path = folder / name
    path.write_bytes(b"x" * size)
    return path


def _index(conn):
    scanner.scan_library_once(conn)
    return {os.path.basename(r["absolute_path"]): r["id"]
            for r in conn.execute("SELECT id, absolute_path FROM library_items")}


def _bin(tmp_path):
    """A stand-in Recycle Bin: moves into a folder, so a test can look inside."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)

    def move(path):
        shutil.move(path, bin_dir / os.path.basename(path))
    return move, bin_dir


def test_plan_says_where_each_file_is_how_big_and_whether_the_bin_can_take_it(lib, monkeypatch):
    conn, folder = lib
    _game(folder)
    ids = _index(conn)
    monkeypatch.setattr(library_delete, "can_recycle", lambda p: True)
    [entry] = library_delete.plan(conn, ids.values())
    assert entry.size == 2048 and entry.exists and entry.recoverable and entry.blocked is None
    assert entry.public()["file"].endswith(".nsp")


def test_recycle_moves_the_file_and_the_row_goes(lib, tmp_path, monkeypatch):
    conn, folder = lib
    path = _game(folder)
    ids = _index(conn)
    monkeypatch.setattr(library_delete, "can_recycle", lambda p: True)
    move, bin_dir = _bin(tmp_path)
    result = library_delete.delete(conn, ids.values(), "recycle", recycler=move)
    assert result == {"deleted": list(ids.values()), "failed": [], "bytes": 2048}
    assert not path.exists() and (bin_dir / path.name).exists()
    assert conn.execute("SELECT COUNT(*) FROM library_items").fetchone()[0] == 0


def test_recycle_is_refused_where_windows_has_no_bin_never_turned_permanent(lib, monkeypatch):
    conn, folder = lib
    path = _game(folder)
    ids = _index(conn)
    monkeypatch.setattr(library_delete, "can_recycle", lambda p: False)    # a network share, say

    def must_not_run(_p):
        raise AssertionError("fell back to a permanent delete")
    result = library_delete.delete(conn, ids.values(), "recycle", recycler=must_not_run, remover=must_not_run)
    assert result["deleted"] == [] and "no Recycle Bin" in result["failed"][0]["error"]
    assert path.exists()
    assert conn.execute("SELECT COUNT(*) FROM library_items").fetchone()[0] == 1


def test_permanent_removes_a_file_and_a_whole_mod_folder(lib):
    conn, folder = lib
    single = _game(folder)
    mod = folder / "SomeMod" / "atmosphere" / "contents" / "0100000000010000" / "romfs"
    mod.mkdir(parents=True)
    (mod / "a.bin").write_bytes(b"1")
    (mod.parent / "ro.bin").write_bytes(b"22")
    ids = _index(conn)
    mod_item = conn.execute("SELECT id, absolute_path FROM library_items WHERE item_type = 'MOD_FOLDER'").fetchone()
    assert mod_item is not None
    library_delete.delete(conn, [ids[single.name], mod_item["id"]], "permanent")
    assert not single.exists() and not os.path.exists(mod_item["absolute_path"])
    assert conn.execute("SELECT COUNT(*) FROM library_items").fetchone()[0] == 0


def test_a_row_a_job_points_at_is_kept_as_retired(lib):
    conn, folder = lib
    path = _game(folder)
    item_id = _index(conn)[path.name]
    job = db.create_job(conn, action="install", target_storage="SD_CARD", target_device_id="dev",
                        library_item_id=item_id)
    db.update_job_status(conn, job, "DONE")
    library_delete.delete(conn, [item_id], "permanent")
    assert not path.exists()
    assert db.get_library_item_by_id(conn, item_id)["status"] == db.LIBRARY_ITEM_RETIRED


def test_nothing_waiting_in_queue_or_being_prepared_is_deleted(lib):
    conn, folder = lib
    queued, preparing = _game(folder), _game(folder, "Other [0100000000020000][v0].nsp")
    ids = _index(conn)
    db.create_job(conn, action="install", target_storage="SD_CARD", target_device_id="dev",
                  library_item_id=ids[queued.name])
    result = library_delete.delete(conn, ids.values(), "permanent", preparing_item_ids=[ids[preparing.name]])
    assert result["deleted"] == [] and len(result["failed"]) == 2
    assert queued.exists() and preparing.exists()


def test_nothing_outside_a_library_folder_and_never_the_folder_itself(lib, tmp_path):
    conn, folder = lib
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    stray = outside / "stray.nsp"
    stray.write_bytes(b"x")
    now = db.now_iso()
    for path, name in ((stray, "stray"), (folder, "root")):
        conn.execute(
            "INSERT INTO library_items (absolute_path, item_type, file_type, content_type, size, mtime, status, "
            "first_seen_at, last_scanned_at) VALUES (?, 'FILE', 'NSP', 'GAME_PACKAGE', 1, 0, 'AVAILABLE', ?, ?)",
            (str(path), now, now))
    conn.commit()
    ids = [r["id"] for r in conn.execute("SELECT id FROM library_items")]
    result = library_delete.delete(conn, ids, "permanent")
    assert result["deleted"] == [] and all("Library folder" in f["error"] for f in result["failed"])
    assert stray.exists() and folder.exists()


def test_a_file_already_gone_just_leaves_the_index(lib):
    conn, folder = lib
    path = _game(folder)
    item_id = _index(conn)[path.name]
    path.unlink()
    result = library_delete.delete(conn, [item_id], "permanent")
    assert result["deleted"] == [item_id] and result["bytes"] == 0


def test_the_api_plans_and_deletes_and_wants_a_mode(lib, tmp_path):
    from fastapi.testclient import TestClient
    from switchagent.web.app import create_app
    from switchagent.web.context import build_mock_context

    conn, folder = lib
    path = _game(folder)
    item_id = _index(conn)[path.name]
    client = TestClient(create_app(build_mock_context(tmp_path / "test.db")))
    plan = client.post("/api/library/delete/plan", json={"library_item_ids": [item_id]}).json()
    assert plan["items"][0]["id"] == item_id and plan["items"][0]["blocked"] is None
    assert client.post("/api/library/delete", json={"library_item_ids": [item_id]}).status_code == 422
    assert client.post("/api/library/delete", json={"library_item_ids": [item_id], "mode": "permanent"}
                       ).json()["deleted"] == [item_id]
    assert not path.exists()
