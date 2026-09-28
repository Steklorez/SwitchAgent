"""Which source a build was made from.

The version number alone cannot tell a release from a local test build of
the same version with a branch's changes in it -- both say 1.0.22. So the
packaged build carries the branch, the commit and whether there were
uncommitted changes, written next to this module at build time
(packaging/SwitchAgent.spec -> `_build.json`) and shown beside the version
in the header, Settings and the tray tooltip. A build of a clean `main`
(every GitHub release) shows the plain version, as before.

Run from a source checkout there is no `_build.json`; the same facts are
read from git directly, once.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Optional

from . import __version__

BUILD_FILE = Path(__file__).with_name("_build.json")
RELEASE_BRANCH = "main"


def _git(repo: Path, *args: str) -> Optional[str]:
    try:
        result = subprocess.run(
            ["git", *args], cwd=repo, capture_output=True, text=True, timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def collect(repo: Path) -> dict:
    """The facts about `repo`'s checkout right now -- what the build writes
    into `_build.json`. On GitHub Actions the branch comes from the run
    itself (a checkout there may be a detached HEAD)."""
    commit = _git(repo, "rev-parse", "--short", "HEAD")
    branch = os.environ.get("GITHUB_REF_NAME") or _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    status = _git(repo, "status", "--porcelain", "--untracked-files=no")
    return {
        "branch": branch,
        "commit": commit,
        "dirty": bool(status),
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


@lru_cache(maxsize=1)
def info() -> dict:
    try:
        return json.loads(BUILD_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    repo = Path(__file__).resolve().parent.parent
    if (repo / ".git").exists():
        facts = collect(repo)
        facts["built_at"] = None  # not a build: running from the source itself
        return facts
    return {}


def label(facts: Optional[dict] = None) -> str:
    """What goes after the version: "fix/sealed-forwarder · 46e6131*"
    ("*": built with uncommitted changes), or "" for a build of a clean
    main, or when nothing is known."""
    facts = info() if facts is None else facts
    commit = facts.get("commit")
    if not commit:
        return ""
    branch = facts.get("branch") or "?"
    if branch == RELEASE_BRANCH and not facts.get("dirty"):
        return ""
    return f"{branch} · {commit}{'*' if facts.get('dirty') else ''}"


def describe(facts: Optional[dict] = None) -> str:
    """One line for a tooltip or Settings: the version and everything known
    about where it came from."""
    facts = info() if facts is None else facts
    parts = [f"v{__version__}"]
    if facts.get("commit"):
        parts.append(f"{facts.get('branch') or '?'} @ {facts['commit']}")
        if facts.get("dirty"):
            parts.append("with uncommitted changes")
    built = facts.get("built_at")
    if built:
        try:
            parts.append("built " + datetime.fromisoformat(built).astimezone().strftime("%Y-%m-%d %H:%M"))
        except ValueError:
            pass
    return " · ".join(parts)
