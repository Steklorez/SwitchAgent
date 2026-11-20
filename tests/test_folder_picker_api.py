from fastapi.testclient import TestClient
from switchagent import folder_picker
from switchagent.web.app import create_app
from .test_web_api import web_ctx


def test_picker_local_cancel_and_remote_rejection(web_ctx, monkeypatch):
    calls = []
    monkeypatch.setattr(folder_picker, "pick_folder", lambda: calls.append(True))
    app = create_app(web_ctx)
    with TestClient(app, client=("127.0.0.1", 12345)) as client:
        assert client.post("/api/settings/library-dir/pick").json() == {"path": None}
    with TestClient(app, client=("192.168.1.42", 12345)) as client:
        assert client.post("/api/settings/library-dir/pick").status_code == 403
    assert calls == [True]


def test_remote_browser_walks_the_servers_folders(web_ctx, tmp_path):
    """A browser on another machine cannot use the native dialog (it opens
    on the server's screen), so it lists the server's folders instead:
    drives first, then folders only -- never files."""
    tmp_path = tmp_path / "browse"
    tmp_path.mkdir()
    (tmp_path / "Игры").mkdir()
    (tmp_path / "b-folder").mkdir()
    (tmp_path / "game.nsp").write_bytes(b"x")
    with TestClient(create_app(web_ctx), client=("192.168.1.42", 12345)) as client:
        drives = client.get("/api/settings/library-dir/browse").json()
        assert drives["path"] == "" and drives["parent"] is None and drives["folders"]
        listing = client.get("/api/settings/library-dir/browse", params={"path": str(tmp_path)}).json()
        assert [f["name"] for f in listing["folders"]] == ["b-folder", "Игры"]
        assert listing["folders"][1]["path"] == str(tmp_path / "Игры")
        assert listing["parent"] == str(tmp_path.parent)
        missing = client.get("/api/settings/library-dir/browse", params={"path": str(tmp_path / "nope")})
        assert missing.status_code == 404


def test_browser_drive_root_goes_back_to_the_drive_list():
    import os
    import pytest
    if os.name != "nt":
        pytest.skip("Windows drive letters")
    root = os.environ.get("SystemDrive", "C:") + "\\"
    assert root in folder_picker.list_drives()
    assert folder_picker.browse_folders(root)["parent"] == ""


def test_pages_carry_the_app_icon(web_ctx):
    with TestClient(create_app(web_ctx)) as client:
        icon = client.get("/favicon.ico")
        assert icon.status_code == 200 and icon.content[:4] == b"\x00\x00\x01\x00"
        assert 'rel="icon"' in client.get("/settings").text


def test_picker_unexpected_failure_returns_json(web_ctx, monkeypatch):
    def fail():
        raise AttributeError("missing Windows flag")
    monkeypatch.setattr(folder_picker, "pick_folder", fail)
    with TestClient(create_app(web_ctx), client=("127.0.0.1", 12345)) as client:
        response = client.post("/api/settings/library-dir/pick")
    assert response.status_code == 503
    assert response.json()["detail"] == "missing Windows flag"


def test_native_picker_flags_and_unicode(monkeypatch):
    import os
    import pytest
    if os.name != "nt":
        pytest.skip("Windows shell API")
    from win32com.shell import shell
    calls = []
    def browse(*args):
        calls.append(args)
        return ([b"pidl"], "Games", 0)
    monkeypatch.setattr(shell, "SHBrowseForFolder", browse)
    monkeypatch.setattr(shell, "SHGetPathFromIDListW", lambda pidl: "D:\\Игры\\Русификаторы")
    assert folder_picker.pick_folder() == "D:\\Игры\\Русификаторы"
    assert calls[0][3] & 0x0040


def test_settings_accepts_multiple_folders_and_rejects_invalid_set(web_ctx, tmp_path):
    from switchagent import config
    second = tmp_path / "Игры"
    second.mkdir()
    paths = [str(config.LIBRARY_DIR), str(second)]
    client = TestClient(create_app(web_ctx))
    try:
        result = client.post("/api/settings/library-dir", json={"path": paths[0], "paths": paths})
        assert result.status_code == 200
        assert result.json()["library_dirs"] == paths
        invalid = client.post("/api/settings/library-dir", json={"path": paths[0], "paths": [str(tmp_path / "missing")]})
        assert invalid.status_code == 400
        assert client.get("/api/settings").json()["library_dirs"] == paths
    finally:
        web_ctx.stop_library_watcher()
