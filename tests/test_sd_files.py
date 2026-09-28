"""SD_FILES: a homebrew port's switch/ folder belongs to its game.

The layouts below are the real library's, file for file (2026-09-23):

  Zuma Deluxe (homebrew port) for Nintendo Switch [NSP]/
    Zuma Deluxe (port) [01333de6fb400000].nsp   forwarder -> sdmc:/switch/zumaportable/dbc17o.nro
    switch.7z                                    switch/zumaportable/... (the .nro and its data)

  Mega Man X Regenesis (homebrew port) for Nintendo Switch [NSP]/
    Mega Man X Regenesis [01693dfa9bb80000].nsp  forwarder -> sdmc:/switch/mmxregenesis_nx/mmxregenesis_nx.nro
    switch.7z                                    switch/mmxregenesis_nx/assets/... -- data only, no .nro
    Homebrew (1.0.0)/switch/mmxregenesis_nx/mmxregenesis_nx.nro

Before this, Library showed each switch.7z as its own NEEDS REVIEW card
named "switch", never offered to install it, and did not see the unpacked
Homebrew (1.0.0) folder at all -- so Mega Man could not have worked even
with both archives installed by hand. These tests are what keeps that from
coming back: if one fails, a switch/ folder has come loose from its game.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from switchagent import config, db, extractor, known_folders, manifest as manifest_mod, preview
from switchagent import queue_worker, scanner, sd_files, title_id
from switchagent.model import ArchiveEntry, ContentType
from switchagent.mtp import MockMtpBackend
from switchagent.web import services
from switchagent.web.app import create_app
from switchagent.web.context import build_mock_context

from .conftest import build_7z

ZUMA_DIR = "Zuma Deluxe (homebrew port) for Nintendo Switch [NSP]"
ZUMA_NSP = "Zuma Deluxe (port) [01333de6fb400000].nsp"
ZUMA_ID = "01333DE6FB400000"
MEGAMAN_DIR = "Mega Man X Regenesis (homebrew port) for Nintendo Switch [NSP]"
MEGAMAN_NSP = "Mega Man X Regenesis [01693dfa9bb80000].nsp"
MEGAMAN_ID = "01693DFA9BB80000"


def forwarder_bytes(launches: str) -> bytes:
    """What a real forwarder carries in plain text: nx-hbloader's own
    fallback, then nextNroPath and nextArgv, each naming the same .nro."""
    return (b"PFS0" + b"\x00" * 64 + b"sdmc:/hbmenu.nro\x00" + b"\x13" * 32
            + f"sdmc:/{launches}".encode() * 2 + b"\x00" * 16)


@pytest.fixture(autouse=True)
def _no_stability_wait(monkeypatch):
    # An archive is otherwise watched for 2s to be sure nothing is still
    # writing it -- correct in real use, dead time in a test.
    monkeypatch.setattr(scanner, "is_file_stable", lambda *_a, **_k: True)


def _zuma(library, *, with_launch=True):
    folder = library / ZUMA_DIR
    folder.mkdir(parents=True, exist_ok=True)
    nsp = folder / ZUMA_NSP
    nsp.write_bytes(forwarder_bytes("switch/zumaportable/dbc17o.nro") if with_launch else b"PFS0 no strings")
    build_7z(folder / "switch.7z", {
        "switch/zumaportable/dbc17o.nro": b"nro",
        "switch/zumaportable/images/advback.jpg": b"jpg",
        "switch/zumaportable/levels/spiral/spiral.dat": b"level",
    })
    return folder


def _megaman(library, *, with_unpacked_nro=True):
    folder = library / MEGAMAN_DIR
    folder.mkdir(parents=True, exist_ok=True)
    (folder / MEGAMAN_NSP).write_bytes(forwarder_bytes("switch/mmxregenesis_nx/mmxregenesis_nx.nro"))
    build_7z(folder / "switch.7z", {
        "switch/mmxregenesis_nx/assets/.godot/imported/a.ctex": b"a",
        "switch/mmxregenesis_nx/assets/scripts/x.gd": b"x",
    })
    if with_unpacked_nro:
        unpacked = folder / "Homebrew (1.0.0)" / "switch" / "mmxregenesis_nx"
        unpacked.mkdir(parents=True)
        (unpacked / "mmxregenesis_nx.nro").write_bytes(b"the executable")
    return folder


def _rows(conn):
    return {row["absolute_path"]: row for row in db.list_library_items(conn)}


def _game(view, base_title_id):
    return next(g for g in view["games"] if g["base_title_id"] == base_title_id)


# ---------------------------------------------------------------------------
# Classification: from the listing / the folder, never by extracting
# ---------------------------------------------------------------------------

def _entries(*names):
    return [ArchiveEntry(name=n, size=len(n), is_dir=False, is_symlink=False) for n in names]


def test_an_archive_holding_a_switch_folder_is_sd_files():
    cls = extractor.classify_entries(_entries(
        "switch/zumaportable/dbc17o.nro", "switch/zumaportable/fonts/Cancun10.txt",
    ))
    assert cls.content_type is ContentType.SD_FILES
    assert cls.sd_root == ("switch",)
    assert cls.sd_summary.apps == ("switch/zumaportable",)
    assert cls.sd_summary.nro == ("switch/zumaportable/dbc17o.nro",)
    assert cls.sd_summary.files == 2


def test_a_wrapped_switch_folder_is_found_one_level_down():
    cls = extractor.classify_entries(_entries("Zuma v1.2/switch/zumaportable/dbc17o.nro", "Zuma v1.2/README.txt"))
    assert cls.content_type is ContentType.SD_FILES
    assert cls.sd_root == ("Zuma v1.2", "switch")
    assert cls.sd_summary.files == 1  # the README is not SD card content


def test_a_download_folder_called_switch_is_not_an_sd_card():
    cls = extractor.classify_entries(_entries("Switch/Game [0100000000010000][v0].nsp"))
    assert cls.content_type is ContentType.GAME_PACKAGE
    assert cls.sd_root is None


def test_a_mods_own_switch_named_folder_stays_part_of_the_mod():
    cls = extractor.classify_entries(_entries("atmosphere/contents/0100000000010000/romfs/Data/Switch/tex.bin"))
    assert cls.content_type is ContentType.ATMOSPHERE_MOD
    assert cls.sd_root is None


def test_a_switch_folder_buried_in_game_data_is_not_an_sd_card():
    cls = extractor.classify_entries(_entries("Game/Data/Switch/textures/a.bin"))
    assert cls.content_type is ContentType.UNKNOWN


def test_two_different_switch_folders_are_not_guessed_between():
    cls = extractor.classify_entries(_entries("A/switch/a/a.nro", "B/switch/b/b.nro"))
    assert cls.content_type is ContentType.UNKNOWN


def test_a_package_archive_that_also_ships_a_switch_folder_keeps_both():
    cls = extractor.classify_entries(_entries(f"{ZUMA_NSP}", "switch/zumaportable/dbc17o.nro"))
    assert cls.content_type is ContentType.GAME_PACKAGE
    assert cls.sd_root == ("switch",)


def test_every_destination_is_under_switch_whatever_the_source_spelling():
    assert sd_files.destination_for("zumaportable/dbc17o.nro") == "switch/zumaportable/dbc17o.nro"
    assert sd_files.destination_for("./a\\b.dat") == "switch/a/b.dat"


# ---------------------------------------------------------------------------
# Forwarders
# ---------------------------------------------------------------------------

def test_the_forwarders_own_launch_path_is_read_and_hbmenu_is_not_it(tmp_path):
    path = tmp_path / ZUMA_NSP
    path.write_bytes(forwarder_bytes("switch/zumaportable/dbc17o.nro"))
    assert sd_files.forwarder_launch_path(path) == "switch/zumaportable/dbc17o.nro"


def test_concatenated_launch_strings_are_split_at_the_first_nro(tmp_path):
    # Mega Man's forwarder stores nextNroPath and nextArgv back to back.
    path = tmp_path / MEGAMAN_NSP
    path.write_bytes(b"x" + b"sdmc:/switch/mmxregenesis_nx/mmxregenesis_nx.nro" * 2)
    assert sd_files.forwarder_launch_path(path) == "switch/mmxregenesis_nx/mmxregenesis_nx.nro"


def test_a_real_sized_game_is_never_read_for_a_launch_path(tmp_path, monkeypatch):
    path = tmp_path / "Game [0100000000010000][v0].nsp"
    path.write_bytes(forwarder_bytes("switch/x/x.nro"))
    monkeypatch.setattr(sd_files, "FORWARDER_MAX_BYTES", 10)
    assert sd_files.forwarder_launch_path(path) is None


# ---------------------------------------------------------------------------
# The library: one game, not a game plus a card called "switch"
# ---------------------------------------------------------------------------

def test_zuma_is_one_game_with_its_switch_folder_inside_it(isolated_db):
    conn, _ = isolated_db
    folder = _zuma(config.LIBRARY_DIR)
    scanner.scan_library_once(conn)

    sd_row = _rows(conn)[str(folder / "switch.7z")]
    assert sd_row["content_type"] == ContentType.SD_FILES.value
    assert sd_row["status"] == "AVAILABLE"
    assert sd_row["suggested_target"] == "SD_CARD"
    assert (sd_row["title_id"], sd_row["title_id_source"]) == (ZUMA_ID, "forwarder")

    view = services.list_library_view(conn, kind="games")
    assert len(view["games"]) == 1, [g["name"] for g in view["games"]]
    game = _game(view, ZUMA_ID)
    assert game["base"]["absolute_path"] == str(folder / ZUMA_NSP)
    assert [e["sd_label"] for e in game["sd_files"]] == ["switch.7z"]
    assert game["sd_files"][0]["sd_destinations"] == ["sdmc:/switch/zumaportable"]
    assert game["launch_checks"] == [{
        "launches": "sdmc:/switch/zumaportable/dbc17o.nro", "provided_by": "switch.7z",
    }]
    assert game["launch_missing"] == []


def test_megaman_needs_both_its_archive_and_its_unpacked_folder(isolated_db):
    conn, _ = isolated_db
    folder = _megaman(config.LIBRARY_DIR)
    scanner.scan_library_once(conn)

    unpacked = folder / "Homebrew (1.0.0)" / "switch"
    rows = _rows(conn)
    assert rows[str(unpacked)]["content_type"] == ContentType.SD_FILES.value
    assert rows[str(unpacked)]["item_type"] == "MOD_FOLDER"  # the schema's folder kind

    game = _game(services.list_library_view(conn, kind="games"), MEGAMAN_ID)
    assert sorted(e["sd_label"] for e in game["sd_files"]) == ["Homebrew (1.0.0)/switch", "switch.7z"]
    assert game["launch_checks"][0]["provided_by"] == "Homebrew (1.0.0)/switch"


def test_a_forwarder_whose_nro_nothing_provides_says_so(isolated_db):
    conn, _ = isolated_db
    _megaman(config.LIBRARY_DIR, with_unpacked_nro=False)
    scanner.scan_library_once(conn)

    game = _game(services.list_library_view(conn, kind="games"), MEGAMAN_ID)
    assert game["launch_missing"] == ["sdmc:/switch/mmxregenesis_nx/mmxregenesis_nx.nro"]
    assert len(game["sd_files"]) == 1  # the data archive still belongs to it


def test_without_a_readable_forwarder_the_folder_decides(isolated_db):
    conn, _ = isolated_db
    folder = _zuma(config.LIBRARY_DIR, with_launch=False)
    scanner.scan_library_once(conn)
    row = _rows(conn)[str(folder / "switch.7z")]
    assert (row["title_id"], row["title_id_source"]) == (ZUMA_ID, "folder")


def test_a_forwarder_claims_its_folder_even_from_somewhere_else(isolated_db):
    conn, _ = isolated_db
    games = config.LIBRARY_DIR / "Forwarders"
    games.mkdir()
    (games / ZUMA_NSP).write_bytes(forwarder_bytes("switch/zumaportable/dbc17o.nro"))
    (games / "Other [0100000000010000][v0].nsp").write_bytes(b"x")
    data = config.LIBRARY_DIR / "Homebrew data"
    data.mkdir()
    build_7z(data / "switch.7z", {"switch/zumaportable/dbc17o.nro": b"nro"})
    scanner.scan_library_once(conn)
    row = _rows(conn)[str(data / "switch.7z")]
    assert (row["title_id"], row["title_id_source"]) == (ZUMA_ID, "forwarder")


def test_a_folder_holding_two_games_does_not_hand_its_switch_folder_to_either(isolated_db):
    conn, _ = isolated_db
    pack = config.LIBRARY_DIR / "Two games"
    pack.mkdir()
    (pack / "A [0100000000010000][v0].nsp").write_bytes(b"a")
    (pack / "B [0100000000020000][v0].nsp").write_bytes(b"b")
    build_7z(pack / "switch.7z", {"switch/tool/tool.nro": b"nro"})
    scanner.scan_library_once(conn)

    row = _rows(conn)[str(pack / "switch.7z")]
    assert row["title_id"] is None
    view = services.list_library_view(conn, kind="games")
    standalone = [g for g in view["games"] if g["base"]["content_type"] == ContentType.SD_FILES.value]
    assert len(standalone) == 1
    # Named after the folder, not "switch", and installable on its own.
    assert standalone[0]["name"] == "Two games"
    assert standalone[0]["base"]["can_install"] is True
    assert standalone[0]["has_detail_page"] is False


def test_a_releases_switch_folder_goes_to_the_game_at_its_top_ticked_by_hand(isolated_db, tmp_path, monkeypatch):
    """Animal Crossing's real release: the game, its update and a DLC at
    the top, the Island Transfer Tool (a game of its own) in a sub-folder,
    and "Events Unlock (1.11.1a)/switch/" -- DBI, JKSV, switch-time and a
    dbi.config for the game's events. It is Animal Crossing's, but the game
    does not need it: it is not ticked along with the game."""
    conn, _ = isolated_db
    ac = config.LIBRARY_DIR / "Animal Crossing New Horizons [NSP]"
    (ac / "Official Transfer Tool").mkdir(parents=True)
    (ac / "Events Unlock (1.11.1a)" / "switch").mkdir(parents=True)
    (ac / "Animal Crossing New Horizons [01006F8002326000][v0].nsp").write_bytes(b"base")
    (ac / "Animal Crossing New Horizons [01006F8002326800][v2228224].nsp").write_bytes(b"upd")
    (ac / "Official Transfer Tool" / "Island Transfer Tool [0100F38011CFE000][v0].nsp").write_bytes(b"tool")
    for name in ("DBI.nro", "JKSV.nro", "switch-time.nro", "dbi.config"):
        (ac / "Events Unlock (1.11.1a)" / "switch" / name).write_bytes(b"x")
    scanner.scan_library_once(conn)

    events = next(r for r in db.list_library_items(conn) if r["content_type"] == ContentType.SD_FILES.value)
    assert (events["title_id"], events["title_id_source"]) == ("01006F8002326000", "release")
    view = services.list_library_view(conn, kind="games")
    by_base = {g["base_title_id"]: g for g in view["games"]}
    assert [e["id"] for e in by_base["01006F8002326000"]["sd_files"]] == [events["id"]]
    assert by_base["01006F8002326000"]["sd_files"][0]["sd_optional"] is True
    # The Transfer Tool stays a card of its own: it is a different program.
    assert by_base["0100F38011CFE000"]["sd_files"] == []
    assert not any(g["base"]["content_type"] == ContentType.SD_FILES.value for g in view["games"])

    monkeypatch.setattr(config, "CONFIG_YAML_PATH", tmp_path / "config.yaml")
    monkeypatch.setattr(known_folders, "downloads_dir", lambda: None)
    db_path = conn.execute("PRAGMA database_list").fetchone()["file"]
    from pathlib import Path
    html = TestClient(create_app(build_mock_context(Path(db_path)))).get("/").text
    marker = f'class="select-box" value="{events["id"]}"'
    assert marker in html
    row = html[html.index(marker):][:900]
    assert 'data-optional="1"' in row and 'data-role="sd"' in row


def test_a_download_folder_called_switch_is_left_to_its_packages(isolated_db):
    conn, _ = isolated_db
    category = config.LIBRARY_DIR / "Switch"
    category.mkdir()
    (category / "Game [0100000000010000][v0].nsp").write_bytes(b"nsp")
    scanner.scan_library_once(conn)
    assert [r["content_type"] for r in db.list_library_items(conn)] == [ContentType.GAME_PACKAGE.value]


def test_owners_follow_the_library_when_the_forwarder_goes(isolated_db):
    conn, _ = isolated_db
    folder = _zuma(config.LIBRARY_DIR)
    scanner.scan_library_once(conn)
    (folder / ZUMA_NSP).unlink()
    scanner.scan_library_once(conn)
    row = _rows(conn)[str(folder / "switch.7z")]
    assert row["title_id"] is None
    assert row["status"] == "AVAILABLE"


def test_a_plain_game_family_is_untouched(isolated_db):
    """The same library also holds ordinary games. Nothing about them may
    change: no SD files, no launch checks, same grouping."""
    conn, _ = isolated_db
    stardew = config.LIBRARY_DIR / "Stardew Valley [NSZ]"
    stardew.mkdir()
    (stardew / "Stardew Valley [0100E65002BB8000][v0] (0.87 GB).nsz").write_bytes(b"base")
    (stardew / "Stardew Valley [0100E65002BB8800][v1310720] (0.67 GB).nsz").write_bytes(b"update")
    _zuma(config.LIBRARY_DIR)
    scanner.scan_library_once(conn)

    game = _game(services.list_library_view(conn, kind="games"), "0100E65002BB8000")
    assert len(game["updates"]) == 1
    assert game["sd_files"] == [] and game["launch_checks"] == []
    assert game["base"]["launches"] is None and game["base"]["sd"] is None


# ---------------------------------------------------------------------------
# A homebrew port's own made-up TITLE_ID is a game, not a DLC of nothing
# ---------------------------------------------------------------------------

def test_undertale_yellows_id_is_its_own_game():
    assert title_id.classify_title_variant("018DDBF896CAE6E0") == ("BASE", "018DDBF896CAE6E0")


def test_real_dlc_ids_still_resolve_to_their_base():
    assert title_id.classify_title_variant("010030B0289BD003") == ("DLC", "010030B0289BC000")
    assert title_id.classify_title_variant("0100304027593001") == ("DLC", "0100304027592000")


# ---------------------------------------------------------------------------
# Installing: forwarder to DBI, switch/ folder verbatim onto the SD card
# ---------------------------------------------------------------------------

def _backend():
    parent = MockMtpBackend(device_id="mock-switch-parent", device_name="Parent's Switch")
    parent.add_storage("SD_CARD")
    parent.add_storage("SD_INSTALL")
    registry = queue_worker.DeviceRegistry()
    registry.register("mock-switch-parent", parent)
    return parent, registry


def _run_worker(conn, registry, limit=20):
    for _ in range(limit):
        if queue_worker.run_worker_once(conn, registry) is None:
            return


def test_installing_zuma_puts_the_forwarder_in_dbi_and_the_folder_on_the_card(isolated_db):
    conn, _ = isolated_db
    folder = _megaman(config.LIBRARY_DIR)
    scanner.scan_library_once(conn)
    game = _game(services.list_library_view(conn, kind="games"), MEGAMAN_ID)
    # Selecting the game selects every part of it (library.js). Which goes
    # first is the preparation's call (switch/ folder, then the forwarder --
    # see test_a_ports_files_go_before_its_forwarder); at the worker, an SD
    # part no longer waits for a game job.
    ids = [e["id"] for e in game["sd_files"]] + [game["base"]["id"]]

    parent, registry = _backend()
    result = services.create_and_confirm_jobs(conn, ids, "mock-switch-parent")
    assert result["errors"] == []
    jobs = [db.get_job(conn, c["job_id"]) for c in result["created"]]
    assert sorted(j["target_storage"] for j in jobs) == ["SD_CARD", "SD_CARD", "SD_INSTALL"]
    sd_jobs = [j for j in jobs if j["target_storage"] == "SD_CARD"]
    for job in sd_jobs:
        manifest = manifest_mod.load_manifest(job["id"])
        assert manifest.content_type == ContentType.SD_FILES.value
        assert manifest.title_id == MEGAMAN_ID
        assert all(f.dest_relative_path.startswith("switch/mmxregenesis_nx/") for f in manifest.files)

    queue_worker.run_worker_once(conn, registry)
    assert "WAITING_FOR_BASE" not in {db.get_job(conn, j["id"])["status"] for j in sd_jobs}

    _run_worker(conn, registry)
    assert {db.get_job(conn, j["id"])["status"] for j in jobs} <= {"DONE", "DONE_UNVERIFIED"}
    on_card = set(parent.storage_tree("SD_CARD").list_files())
    assert {
        "switch/mmxregenesis_nx/mmxregenesis_nx.nro",
        "switch/mmxregenesis_nx/assets/.godot/imported/a.ctex",
        "switch/mmxregenesis_nx/assets/scripts/x.gd",
    } <= on_card
    assert all(path.startswith("switch/") for path in on_card)
    assert parent.storage_tree("SD_INSTALL").list_files() == [MEGAMAN_NSP]
    # The bare folder was verified and sent in place, never copied or moved.
    assert (folder / "Homebrew (1.0.0)" / "switch" / "mmxregenesis_nx" / "mmxregenesis_nx.nro").is_file()


def test_queue_and_history_call_it_sd_files_not_a_mod(isolated_db):
    conn, _ = isolated_db
    _zuma(config.LIBRARY_DIR)
    scanner.scan_library_once(conn)
    game = _game(services.list_library_view(conn, kind="games"), ZUMA_ID)
    parent, registry = _backend()
    result = services.create_and_confirm_jobs(conn, [game["base"]["id"], game["sd_files"][0]["id"]],
                                              "mock-switch-parent")
    sd_job = next(db.get_job(conn, c["job_id"]) for c in result["created"]
                  if db.get_job(conn, c["job_id"])["target_storage"] == "SD_CARD")
    view = services._job_view(conn, sd_job)
    assert view["variant_role"] == "sd" and view["variant_label"] == "SD files"
    assert view["display_name"] == ZUMA_NSP  # the game's name, the badge says the rest

    _run_worker(conn, registry)
    history = {h["display_name"]: h for h in db.list_install_history(conn)}
    assert f"{ZUMA_NSP} — SD files" in history
    rows = services.list_history_entries(conn)["rows"]
    # The same name Library shows for the game, and a badge saying which
    # half of it this row was -- never "Mod".
    assert sorted((r["name"], r["role"]) for r in rows) == [
        ("Zuma Deluxe (port)", "base"), ("Zuma Deluxe (port)", "sd"),
    ]


def test_a_package_archive_with_its_own_switch_folder_installs_both(isolated_db):
    conn, _ = isolated_db
    path = build_7z(config.LIBRARY_DIR / "Zuma bundle.7z", {
        ZUMA_NSP: forwarder_bytes("switch/zumaportable/dbc17o.nro"),
        "switch/zumaportable/dbc17o.nro": b"nro",
    })
    scanner.scan_library_once(conn)
    row = _rows(conn)[str(path)]
    assert row["content_type"] == ContentType.GAME_PACKAGE.value
    assert sd_files.sd_summary_of(row).nro == ("switch/zumaportable/dbc17o.nro",)

    parent, registry = _backend()
    result = services.create_and_confirm_jobs(conn, [row["id"]], "mock-switch-parent")
    assert result["errors"] == []
    assert sorted(db.get_job(conn, c["job_id"])["target_storage"] for c in result["created"]) == \
        ["SD_CARD", "SD_INSTALL"]
    _run_worker(conn, registry)
    assert parent.storage_tree("SD_CARD").list_files() == ["switch/zumaportable/dbc17o.nro"]


def test_preview_of_a_bare_switch_folder_describes_what_goes_where(tmp_path):
    folder = tmp_path / "Homebrew (1.0.0)" / "switch" / "app"
    folder.mkdir(parents=True)
    (folder / "app.nro").write_bytes(b"nro")
    report = preview.preview_path(tmp_path / "Homebrew (1.0.0)")
    assert report.content_type is ContentType.SD_FILES
    assert report.sd_source_dir == (tmp_path / "Homebrew (1.0.0)" / "switch").resolve()
    assert report.sd_summary.nro == ("switch/app/app.nro",)


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------

@pytest.fixture
def client(tmp_path, monkeypatch):
    library_dir = tmp_path / "library"
    library_dir.mkdir()
    (tmp_path / "inbox").mkdir()
    monkeypatch.setattr(config, "INBOX_DIR", tmp_path / "inbox")
    monkeypatch.setattr(config, "WORK_DIR", tmp_path / "work")
    monkeypatch.setattr(config, "LIBRARY_DIR", library_dir)
    monkeypatch.setattr(config, "CONFIG_YAML_PATH", tmp_path / "config.yaml")
    monkeypatch.setattr(known_folders, "downloads_dir", lambda: None)
    db_path = tmp_path / "test.db"
    _zuma(library_dir)
    with db.open_db(db_path) as conn:
        scanner.scan_library_once(conn)
    ctx = build_mock_context(db_path)
    return TestClient(create_app(ctx))


def test_the_library_page_shows_zuma_once_with_its_sd_files(client):
    html = client.get("/").text
    assert html.count('class="card-name"') == 1
    assert ">switch<" not in html
    assert "1 SD files" in html
    assert "SD files — switch.7z" in html
    assert "→ sdmc:/switch/zumaportable" in html
    assert 'data-role="sd"' in html


def test_the_game_page_lists_the_sd_files_and_what_the_icon_starts(client):
    html = client.get(f"/games/{ZUMA_ID}").text
    assert "SD card files" in html
    assert "sdmc:/switch/zumaportable/dbc17o.nro" in html
    assert "comes from <strong>switch.7z</strong>" in html


# ---------------------------------------------------------------------------
# Upgrading: a library indexed before any of this must not keep its
# "switch" cards. Its files have not changed, so without a reason to look
# again the scanner would skip them forever.
# ---------------------------------------------------------------------------

def test_rows_indexed_by_the_old_classifier_are_looked_at_again_once(isolated_db, monkeypatch):
    conn, _ = isolated_db
    folder = _zuma(config.LIBRARY_DIR)
    for path, fields in (
        (folder / "switch.7z", dict(file_type="7Z", content_type="UNKNOWN", status="NEEDS_REVIEW",
                                    suggested_action=None, suggested_target=None, title_id=None,
                                    title_id_source=None)),
        (folder / ZUMA_NSP, dict(file_type="NSP", content_type="GAME_PACKAGE", status="AVAILABLE",
                                 suggested_action="INSTALL_VIA_DBI", suggested_target="SD_INSTALL",
                                 title_id=ZUMA_ID, title_id_source="filename")),
    ):
        st = path.stat()
        db.upsert_library_item(conn, absolute_path=str(path), item_type="FILE", size=st.st_size,
                               mtime=st.st_mtime, content_hash=None, details_json=None, **fields)

    def no_wait(*_a, **_k):
        raise AssertionError("an unchanged file needs no stability wait")
    monkeypatch.setattr(scanner, "is_file_stable", no_wait)

    first = scanner.scan_library_once(conn)
    assert first["updated"] == 2 and first["unchanged"] == 0
    rows = _rows(conn)
    assert rows[str(folder / "switch.7z")]["content_type"] == ContentType.SD_FILES.value
    assert rows[str(folder / "switch.7z")]["title_id"] == ZUMA_ID
    assert sd_files.launches_of(rows[str(folder / ZUMA_NSP)]) == "switch/zumaportable/dbc17o.nro"

    second = scanner.scan_library_once(conn)
    assert second["unchanged"] == 2 and second["updated"] == 0


def test_a_missing_nro_is_said_on_the_card_itself(tmp_path, monkeypatch):
    """Inside the card, not beside it: the tile's three rows (card, tags,
    link) are aligned across the whole shelf, and a fourth, optional row
    would break that for every neighbour."""
    library_dir = tmp_path / "library"
    library_dir.mkdir()
    (tmp_path / "inbox").mkdir()
    monkeypatch.setattr(config, "INBOX_DIR", tmp_path / "inbox")
    monkeypatch.setattr(config, "WORK_DIR", tmp_path / "work")
    monkeypatch.setattr(config, "LIBRARY_DIR", library_dir)
    monkeypatch.setattr(config, "CONFIG_YAML_PATH", tmp_path / "config.yaml")
    monkeypatch.setattr(known_folders, "downloads_dir", lambda: None)
    db_path = tmp_path / "test.db"
    _megaman(library_dir, with_unpacked_nro=False)
    with db.open_db(db_path) as conn:
        scanner.scan_library_once(conn)
    from pathlib import Path
    html = TestClient(create_app(build_mock_context(Path(db_path)))).get("/").text
    card = html.split('class="game-group"', 1)[1].split("game-variants", 1)[0]
    assert "Needs sdmc:/switch/mmxregenesis_nx/mmxregenesis_nx.nro" in card



# ---------------------------------------------------------------------------
# Retry continues its transfer (2026-09-23): Mega Man's switch/ folder
# stopped after 613 files; a retry that knew none of them met file #1 on the
# card and, under "skip", stopped there. A file already there with exactly
# the same bytes is done either way.
# ---------------------------------------------------------------------------

def _zuma_sd_job(conn, parent):
    _zuma(config.LIBRARY_DIR)
    scanner.scan_library_once(conn)
    game = _game(services.list_library_view(conn, kind="games"), ZUMA_ID)
    result = services.create_and_confirm_jobs(conn, [game["sd_files"][0]["id"]], "mock-switch-parent")
    return result["created"][0]["job_id"]


def test_a_retry_carries_on_after_what_its_attempt_delivered(isolated_db):
    conn, _ = isolated_db
    parent, registry = _backend()
    job_id = _zuma_sd_job(conn, parent)
    files = [f.dest_relative_path for f in manifest_mod.load_manifest(job_id).files]
    # File 2 of 3 breaks off half-way: the card now holds file 1 and a
    # half-written file 2 of this transfer's own.
    parent.arm_failure("partial", storage="SD_CARD", dest_path=files[1])
    queue_worker.run_worker_once(conn, registry)
    old = db.get_job(conn, job_id)
    assert old["status"] in ("FAILED", "INTERRUPTED") and old["current_file"] == files[1]

    retried = services.retry_job(conn, job_id)["new_job_id"]
    assert manifest_mod.load_progress(retried) == {files[0]}
    assert manifest_mod.load_replaceable(retried) == {files[1]}
    _run_worker(conn, registry)
    assert db.get_job(conn, retried)["status"] == "DONE"
    tree = parent.storage_tree("SD_CARD")
    assert tree.read_file("switch/zumaportable/dbc17o.nro") == b"nro"
    assert tree.read_file("switch/zumaportable/images/advback.jpg") == b"jpg"
    assert tree.read_file("switch/zumaportable/levels/spiral/spiral.dat") == b"level"


def test_files_already_on_the_card_with_the_same_bytes_are_not_a_conflict(isolated_db):
    conn, _ = isolated_db
    parent, registry = _backend()
    job_id = _zuma_sd_job(conn, parent)
    manifest = manifest_mod.load_manifest(job_id)
    tree = parent.storage_tree("SD_CARD")
    first = manifest.files[0]
    tree.ensure_directory(first.dest_relative_path.rsplit("/", 1)[0])
    tree.write_file(first.dest_relative_path,
                    manifest_mod.resolve_source_path(first, job_id, batch_id=manifest.batch_id).read_bytes())
    _run_worker(conn, registry)
    assert db.get_job(conn, job_id)["status"] == "DONE"
    assert any("already on the device" in e["message"] for e in db.list_job_log(conn, job_id))


def test_a_different_file_in_the_way_is_still_skipped_as_a_conflict(isolated_db):
    conn, _ = isolated_db
    parent, registry = _backend()
    job_id = _zuma_sd_job(conn, parent)
    first = manifest_mod.load_manifest(job_id).files[0]
    tree = parent.storage_tree("SD_CARD")
    tree.ensure_directory(first.dest_relative_path.rsplit("/", 1)[0])
    tree.write_file(first.dest_relative_path, b"somebody else's file")
    _run_worker(conn, registry)
    assert db.get_job(conn, job_id)["status"] == "DESTINATION_CONFLICT"
    assert tree.read_file(first.dest_relative_path) == b"somebody else's file"
