"""A build says which source it was made from, so a local test build of a
branch is never mistaken for the release of the same version."""

from __future__ import annotations

import json

from switchagent import __version__, build_info


def test_a_clean_main_build_shows_the_plain_version():
    assert build_info.label({"branch": "main", "commit": "d5c91f0", "dirty": False}) == ""


def test_a_branch_build_shows_branch_and_commit():
    assert build_info.label({"branch": "fix/x", "commit": "46e6131", "dirty": False}) == "fix/x · 46e6131"


def test_uncommitted_changes_are_marked():
    assert build_info.label({"branch": "main", "commit": "d5c91f0", "dirty": True}) == "main · d5c91f0*"


def test_nothing_known_shows_nothing():
    assert build_info.label({}) == ""


def test_describe_says_everything_known():
    text = build_info.describe({
        "branch": "fix/x", "commit": "46e6131", "dirty": True, "built_at": "2026-09-28T14:30:00+00:00",
    })
    assert text.startswith(f"v{__version__} · fix/x @ 46e6131 · with uncommitted changes · built 2026-09-28")


def test_the_packaged_build_reads_what_the_build_wrote(tmp_path, monkeypatch):
    written = {"branch": "fix/x", "commit": "46e6131", "dirty": False, "built_at": None}
    build_file = tmp_path / "_build.json"
    build_file.write_text(json.dumps(written), encoding="utf-8")
    monkeypatch.setattr(build_info, "BUILD_FILE", build_file)
    build_info.info.cache_clear()
    try:
        assert build_info.info() == written
    finally:
        build_info.info.cache_clear()


def test_collect_reads_the_checkout(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_REF_NAME", raising=False)
    answers = {
        ("rev-parse", "--short", "HEAD"): "46e6131",
        ("rev-parse", "--abbrev-ref", "HEAD"): "fix/x",
        ("status", "--porcelain", "--untracked-files=no"): " M file.py",
    }
    monkeypatch.setattr(build_info, "_git", lambda _repo, *args: answers[args])
    facts = build_info.collect(tmp_path)
    assert (facts["branch"], facts["commit"], facts["dirty"]) == ("fix/x", "46e6131", True)


def test_on_github_actions_the_branch_comes_from_the_run(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_REF_NAME", "main")
    monkeypatch.setattr(build_info, "_git", lambda _repo, *args: "HEAD" if "--abbrev-ref" in args else "")
    assert build_info.collect(tmp_path)["branch"] == "main"
