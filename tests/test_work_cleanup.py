"""Tests for UI-004's work/job-<id>/ staging cleanup
(switchagent/work_cleanup.py). Covers: active/retryable jobs are never
touched, only DONE/DONE_UNVERIFIED past the retention window are eligible,
manifest.json/progress.json always survive, a bare-file job (no frozen
payload at all) never falsely counts as eligible, cleanup is idempotent,
and preview's numbers match what execute_cleanup() actually does.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from switchagent import config, db, manifest as manifest_mod, work_cleanup

_NOW = datetime.now(timezone.utc)
_OLD = _NOW - timedelta(days=work_cleanup.DEFAULT_RETENTION_DAYS + 1)
_FRESH = _NOW - timedelta(days=1)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _make_job(conn, *, status: str, finished_at=None) -> int:
    item_id = db.upsert_library_item(
        conn, absolute_path=f"D:\\shared\\Download\\Game-{status}-{finished_at}.nsp",
        item_type="FILE", file_type="NSP", size=100, mtime=0.0,
        content_hash=f"hash-{status}-{finished_at}", title_id="0100000000010000",
        title_id_source="filename", status="AVAILABLE",
        suggested_action="INSTALL_VIA_DBI", suggested_target="SD_INSTALL",
    )
    job_id = db.create_job(
        conn, action="INSTALL_VIA_DBI", target_storage="SD_INSTALL",
        target_device_id="dev-a", library_item_id=item_id,
    )
    db.update_job_status(conn, job_id, status, finished_at=finished_at)
    return job_id


def _write_frozen_payload(job_id: int, *, size: int = 1000, with_progress: bool = True) -> None:
    """Mimics manifest.build_manifest_and_stage()'s output for an
    archive-sourced job: manifest.json + progress.json (small) plus a
    "frozen" payload file (the heavy part cleanup should remove)."""
    work_dir = manifest_mod.job_work_dir(job_id)
    work_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / "manifest.json").write_text('{"content_type": "GAME_PACKAGE"}', encoding="utf-8")
    if with_progress:
        (work_dir / "progress.json").write_text('{"delivered": ["Game.nsp"]}', encoding="utf-8")
    (work_dir / "Game.nsp").write_bytes(b"x" * size)


def _write_bare_metadata_only(job_id: int) -> None:
    """Mimics the common real case: a bare-file job (source directly under
    inbox/library, never copied) -- only manifest.json ever exists."""
    work_dir = manifest_mod.job_work_dir(job_id)
    work_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / "manifest.json").write_text('{"content_type": "GAME_PACKAGE"}', encoding="utf-8")


# ---------------------------------------------------------------------------
# eligibility / safety
# ---------------------------------------------------------------------------

def test_active_job_never_eligible(isolated_db):
    conn, _inbox_dir = isolated_db
    job_id = _make_job(conn, status="RUNNING")
    _write_frozen_payload(job_id)

    preview = work_cleanup.preview_cleanup(conn)
    assert preview["eligible_job_count"] == 0
    assert job_id not in preview["eligible_job_ids"]


def test_interrupted_job_never_eligible(isolated_db):
    """INTERRUPTED must stay retryable -- its staged content (including any
    frozen payload low-level db.retry_job() would need) must survive,
    however old it gets."""
    conn, _inbox_dir = isolated_db
    job_id = _make_job(conn, status="INTERRUPTED", finished_at=_iso(_OLD))
    _write_frozen_payload(job_id)

    preview = work_cleanup.preview_cleanup(conn)
    assert preview["eligible_job_count"] == 0

    result = work_cleanup.execute_cleanup(conn)
    assert result["cleaned_job_count"] == 0
    assert (manifest_mod.job_work_dir(job_id) / "Game.nsp").exists()


def test_confirmed_and_waiting_and_device_unavailable_jobs_never_eligible(isolated_db):
    conn, _inbox_dir = isolated_db
    for status in ("CONFIRMED", "WAITING_FOR_BASE", "WAITING_FOR_DEVICE", "DEVICE_UNAVAILABLE",
                   "FAILED", "SOURCE_CHANGED", "DESTINATION_CONFLICT", "BLOCKED_BY_DEPENDENCY"):
        job_id = _make_job(conn, status=status, finished_at=_iso(_OLD))
        _write_frozen_payload(job_id)

    preview = work_cleanup.preview_cleanup(conn)
    assert preview["eligible_job_count"] == 0


def test_completed_but_fresh_job_under_retention_not_eligible(isolated_db):
    conn, _inbox_dir = isolated_db
    job_id = _make_job(conn, status="DONE", finished_at=_iso(_FRESH))
    _write_frozen_payload(job_id)

    preview = work_cleanup.preview_cleanup(conn)
    assert preview["eligible_job_count"] == 0

    result = work_cleanup.execute_cleanup(conn)
    assert result["cleaned_job_count"] == 0
    assert (manifest_mod.job_work_dir(job_id) / "Game.nsp").exists()


def test_bare_file_job_with_only_metadata_never_counted_as_eligible(isolated_db):
    """The common real case: nothing was ever copied into work/job-<id>/
    beyond manifest.json -- there is nothing to clean, so it must not
    inflate eligible_job_count with a job that would free zero bytes."""
    conn, _inbox_dir = isolated_db
    job_id = _make_job(conn, status="DONE_UNVERIFIED", finished_at=_iso(_OLD))
    _write_bare_metadata_only(job_id)

    preview = work_cleanup.preview_cleanup(conn)
    assert preview["eligible_job_count"] == 0
    assert preview["eligible_size_bytes"] == 0


# ---------------------------------------------------------------------------
# actual cleanup behavior
# ---------------------------------------------------------------------------

def test_eligible_old_completed_job_is_cleaned(isolated_db):
    conn, _inbox_dir = isolated_db
    job_id = _make_job(conn, status="DONE_UNVERIFIED", finished_at=_iso(_OLD))
    _write_frozen_payload(job_id, size=4096)

    preview = work_cleanup.preview_cleanup(conn)
    assert preview["eligible_job_count"] == 1
    assert preview["eligible_job_ids"] == [job_id]
    assert preview["eligible_size_bytes"] == 4096

    result = work_cleanup.execute_cleanup(conn)
    assert result["cleaned_job_count"] == 1
    assert result["cleaned_job_ids"] == [job_id]
    assert result["freed_bytes"] == 4096
    assert not (manifest_mod.job_work_dir(job_id) / "Game.nsp").exists()


def test_metadata_files_survive_cleanup(isolated_db):
    conn, _inbox_dir = isolated_db
    job_id = _make_job(conn, status="DONE", finished_at=_iso(_OLD))
    _write_frozen_payload(job_id)

    work_cleanup.execute_cleanup(conn)

    work_dir = manifest_mod.job_work_dir(job_id)
    assert (work_dir / "manifest.json").exists()
    assert (work_dir / "progress.json").exists()
    assert not (work_dir / "Game.nsp").exists()


def test_frozen_mod_directory_tree_is_removed(isolated_db):
    """A mod's frozen content is a whole directory (work/job-<id>/mod/...),
    not a single file -- must be recursively removed too."""
    conn, _inbox_dir = isolated_db
    job_id = _make_job(conn, status="DONE_UNVERIFIED", finished_at=_iso(_OLD))
    work_dir = manifest_mod.job_work_dir(job_id)
    work_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / "manifest.json").write_text("{}", encoding="utf-8")
    mod_dir = work_dir / "mod" / "romfs"
    mod_dir.mkdir(parents=True)
    (mod_dir / "file1.bin").write_bytes(b"x" * 2000)
    (mod_dir / "file2.bin").write_bytes(b"y" * 3000)

    result = work_cleanup.execute_cleanup(conn)
    assert result["cleaned_job_count"] == 1
    assert result["freed_bytes"] == 5000
    assert (work_dir / "manifest.json").exists()
    assert not (work_dir / "mod").exists()


def test_cleanup_is_idempotent(isolated_db):
    conn, _inbox_dir = isolated_db
    job_id = _make_job(conn, status="DONE", finished_at=_iso(_OLD))
    _write_frozen_payload(job_id, size=500)

    first = work_cleanup.execute_cleanup(conn)
    assert first["cleaned_job_count"] == 1
    assert first["freed_bytes"] == 500

    second = work_cleanup.execute_cleanup(conn)
    assert second["cleaned_job_count"] == 0
    assert second["freed_bytes"] == 0

    preview_after = work_cleanup.preview_cleanup(conn)
    assert preview_after["eligible_job_count"] == 0


def test_preview_matches_actual_cleanup(isolated_db):
    conn, _inbox_dir = isolated_db
    job_a = _make_job(conn, status="DONE", finished_at=_iso(_OLD))
    _write_frozen_payload(job_a, size=1000)
    job_b = _make_job(conn, status="DONE_UNVERIFIED", finished_at=_iso(_OLD))
    _write_frozen_payload(job_b, size=2500)
    fresh_job = _make_job(conn, status="DONE", finished_at=_iso(_FRESH))
    _write_frozen_payload(fresh_job, size=9999)  # must not count -- too recent

    preview = work_cleanup.preview_cleanup(conn)
    assert preview["eligible_job_count"] == 2
    assert preview["eligible_size_bytes"] == 3500
    assert set(preview["eligible_job_ids"]) == {job_a, job_b}

    result = work_cleanup.execute_cleanup(conn)
    assert result["cleaned_job_count"] == preview["eligible_job_count"]
    assert result["freed_bytes"] == preview["eligible_size_bytes"]
    assert set(result["cleaned_job_ids"]) == set(preview["eligible_job_ids"])
    # the fresh job's payload must still be there, untouched
    assert (manifest_mod.job_work_dir(fresh_job) / "Game.nsp").exists()


def test_total_work_size_includes_ineligible_jobs_too(isolated_db):
    """total_work_size_bytes (shown in Settings alongside the eligible
    figure) is the WHOLE work/ directory, not just what's cleanable --
    proves the two numbers are computed independently, not accidentally
    aliased."""
    conn, _inbox_dir = isolated_db
    active_job = _make_job(conn, status="RUNNING")
    _write_frozen_payload(active_job, size=7000)

    preview = work_cleanup.preview_cleanup(conn)
    assert preview["eligible_job_count"] == 0
    assert preview["total_work_size_bytes"] >= 7000


def test_two_threads_releasing_the_same_batch_never_see_it_half_removed(isolated_db, monkeypatch):
    """The worker (after a job) and the preparation queue (before the next
    archive) release a finished batch at the same moment. The one that loses
    the race must not return while the winner is still emptying the folder:
    its caller checks right afterwards that the folder is gone, and a
    1,532-file amiibo collection made that check fail every time
    (2026-09-25)."""
    import threading
    import time

    conn, _ = isolated_db
    job_id = _make_job(conn, status="DONE", finished_at=_iso(datetime.now(timezone.utc)))
    batch_id = db.create_installation_batch(conn, target_device_id="dev-a")
    conn.execute("UPDATE jobs SET batch_id=? WHERE id=?", (batch_id, job_id))
    conn.commit()
    batch_dir = manifest_mod.batch_work_dir(batch_id)
    for i in range(200):
        path = batch_dir / "copy" / f"amiibo-{i}" / "amiibo.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"{}")

    real_rmtree = work_cleanup.shutil.rmtree
    started = threading.Event()
    calls = []

    def slow_rmtree(path, *args, **kwargs):
        # The first caller takes its time; a second one arriving meanwhile
        # collides with it, as two rmtree()s over one folder do on Windows
        # (a file already gone, a folder not yet empty).
        calls.append(path)
        if len(calls) > 1:
            raise OSError("collided with the other thread's rmtree")
        started.set()
        time.sleep(0.3)
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(work_cleanup.shutil, "rmtree", slow_rmtree)
    db_path = Path(conn.execute("PRAGMA database_list").fetchone()["file"])
    outcome = {}

    def worker_side():
        with db.open_db(db_path) as own:
            outcome["worker"] = work_cleanup.cleanup_batch_if_all_done(own, batch_id)

    thread = threading.Thread(target=worker_side)
    thread.start()
    assert started.wait(5), "the worker's cleanup never reached rmtree"
    work_cleanup.cleanup_batch_if_all_done(conn, batch_id)
    assert not batch_dir.exists()
    thread.join(5)
    assert outcome["worker"] is True


# ---------------------------------------------------------------------------
# archive_finished_jobs: job-<id> folders folded into the job_archive table
# ---------------------------------------------------------------------------

def test_archive_folds_old_finished_job_into_db(isolated_db):
    import zlib

    conn, _inbox_dir = isolated_db
    job_id = _make_job(conn, status="DONE", finished_at=_iso(_OLD))
    work_dir = manifest_mod.job_work_dir(job_id)
    work_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / "manifest.json").write_text('{"a": 1}', encoding="utf-8")
    (work_dir / "progress.json").write_text('{"delivered": ["x"]}', encoding="utf-8")

    assert work_cleanup.archive_finished_jobs(conn) == 1
    assert not work_dir.exists()
    row = conn.execute("SELECT * FROM job_archive WHERE job_id=?", (job_id,)).fetchone()
    assert zlib.decompress(row["manifest"]) == b'{"a": 1}'
    assert zlib.decompress(row["progress"]) == b'{"delivered": ["x"]}'
    assert work_cleanup.archive_finished_jobs(conn) == 0  # idempotent


def test_archive_leaves_fresh_active_failed_and_payload_folders(isolated_db):
    conn, _inbox_dir = isolated_db
    cases = {
        "fresh": _make_job(conn, status="DONE", finished_at=_iso(_FRESH)),
        "running": _make_job(conn, status="RUNNING"),
        "failed": _make_job(conn, status="FAILED", finished_at=_iso(_OLD)),
        "payload": _make_job(conn, status="DONE", finished_at=_iso(_OLD)),
    }
    for job_id in cases.values():
        _write_bare_metadata_only(job_id)
    (manifest_mod.job_work_dir(cases["payload"]) / "Game.nsp").write_bytes(b"x")
    assert work_cleanup.archive_finished_jobs(conn) == 0
    for job_id in cases.values():
        assert manifest_mod.job_work_dir(job_id).is_dir()
    assert conn.execute("SELECT COUNT(*) FROM job_archive").fetchone()[0] == 0


def test_archive_folds_abandoned_failed_job_but_not_live_failed_one(isolated_db):
    conn, _inbox_dir = isolated_db
    dead = _make_job(conn, status="FAILED", finished_at=_iso(_OLD))
    live = _make_job(conn, status="FAILED", finished_at=_iso(_OLD))
    conn.execute("UPDATE jobs SET abandoned=1 WHERE id=?", (dead,))
    conn.commit()
    for job_id in (dead, live):
        _write_bare_metadata_only(job_id)
    assert work_cleanup.archive_finished_jobs(conn) == 1
    assert not manifest_mod.job_work_dir(dead).exists()
    assert manifest_mod.job_work_dir(live).is_dir()
