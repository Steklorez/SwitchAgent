"""W3-008: update AVAILABILITY check only.

This module never downloads an executable, never runs anything it
fetches, and never replaces any file on disk except its own tiny JSON
cache (last_check_at/latest_version/release_url). Installing an update
is app_update.py's job, and only on the user's click in the top banner.

Privacy: the only outbound request this module ever makes is a bare
`GET` against GitHub's public Releases API -- no request body, no query
parameters, no headers beyond a User-Agent (required by GitHub's API).
It never sends device IDs, the game/library list, install history, or
any diagnostics payload; there is nothing in the request for any of that
to travel in.

GITHUB_REPO_SLUG points at the project's real GitHub mirror
(github.com/Steklorez/SwitchAgent, added 2026-09-12 alongside the
existing Bitbucket `origin` remote -- Bitbucket remains the primary
backup remote the other collaborating session pushes to; GitHub is
additionally where this check and `.github/workflows/release-windows.yml`
point). Until an actual GitHub Release has ever been published there,
every check will legitimately 404 and be swallowed by this module's own
required "offline/error: silent, non-fatal" behavior -- never a crash,
never a false "update available" claim.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

GITHUB_REPO_SLUG = "Steklorez/SwitchAgent"

DEFAULT_MIN_INTERVAL_HOURS = 24.0
_REQUEST_TIMEOUT_SECONDS = 10.0


@dataclass
class UpdateCheckResult:
    checked_at: Optional[str]           # ISO 8601 UTC, or None if never successfully checked
    latest_version: Optional[str]
    release_url: Optional[str]
    current_version: str
    update_available: bool
    error: Optional[str] = None         # last error text, if the most recent attempt failed (still non-fatal)

    def to_dict(self) -> dict:
        return asdict(self)


def _cache_path(app_data_root: Path) -> Path:
    return app_data_root / "update_check_cache.json"


def _load_cache(app_data_root: Path) -> Optional[dict]:
    path = _cache_path(app_data_root)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _save_cache(app_data_root: Path, data: dict) -> None:
    path = _cache_path(app_data_root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        pass  # best-effort cache -- a failed write never blocks the check itself


def _parse_version_tuple(version: str) -> tuple:
    """Best-effort, not a full semver parser -- strips a leading 'v',
    splits on '.', compares numeric segments as ints where possible so
    "0.10.0" correctly sorts after "0.9.0" (a plain string compare would
    get that backwards). Falls back to the raw string for any segment
    that isn't purely numeric, so a pre-release suffix like "0.2.0-rc1"
    still compares reasonably against "0.2.0" (as "greater", per PEP 440-
    style expectations for a nonempty suffix) without needing a real
    semver dependency for this low-stakes a comparison."""
    cleaned = version.strip().lstrip("vV")
    parts = re.split(r"[.\-+]", cleaned)
    result = []
    for p in parts:
        result.append(int(p) if p.isdigit() else p)
    return tuple(result)


def _is_newer(latest: str, current: str) -> bool:
    try:
        return _parse_version_tuple(latest) > _parse_version_tuple(current)
    except TypeError:
        # Mixed int/str comparison at the same position (e.g. numeric vs
        # a pre-release tag) -- fall back to a straight inequality check
        # rather than guessing an ordering that isn't well-defined here.
        return latest.strip().lstrip("vV") != current.strip().lstrip("vV")


def _fetch_latest_release(repo_slug: str) -> tuple[str, str]:
    """Returns (tag_name, html_url). Raises on any failure -- caller is
    responsible for the required silent/non-fatal handling."""
    url = f"https://api.github.com/repos/{repo_slug}/releases/latest"
    request = urllib.request.Request(url, headers={"User-Agent": "SwitchAgent-update-check"})
    with urllib.request.urlopen(request, timeout=_REQUEST_TIMEOUT_SECONDS) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return payload["tag_name"], payload["html_url"]


def check_for_update(
    current_version: str,
    *,
    app_data_root: Path,
    repo_slug: str = GITHUB_REPO_SLUG,
    force: bool = False,
    min_interval_hours: float = DEFAULT_MIN_INTERVAL_HOURS,
) -> UpdateCheckResult:
    """The one entry point. Returns cached data without making a network
    call if the cache is fresh enough (unless force=True, e.g. the
    Settings page's "Check now" button). On any failure -- offline, DNS,
    timeout, a 404 (e.g. GITHUB_REPO_SLUG still the placeholder above),
    malformed response -- returns the last known-good cached result (or
    an inert "nothing known yet" result if there is no cache), silently,
    with the failure text attached to `error` for optional display, never
    raised."""
    cache = _load_cache(app_data_root) or {}
    last_checked_at = cache.get("checked_at")

    if not force and last_checked_at:
        try:
            last_dt = datetime.fromisoformat(last_checked_at)
            age_hours = (datetime.now(timezone.utc) - last_dt).total_seconds() / 3600.0
            if age_hours < min_interval_hours:
                return UpdateCheckResult(
                    checked_at=cache.get("checked_at"),
                    latest_version=cache.get("latest_version"),
                    release_url=cache.get("release_url"),
                    current_version=current_version,
                    update_available=bool(cache.get("latest_version"))
                    and _is_newer(cache["latest_version"], current_version),
                    error=None,
                )
        except ValueError:
            pass  # malformed cached timestamp -- fall through to a fresh check

    try:
        latest_version, release_url = _fetch_latest_release(repo_slug)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        return UpdateCheckResult(
            checked_at=cache.get("checked_at"),
            latest_version=cache.get("latest_version"),
            release_url=cache.get("release_url"),
            current_version=current_version,
            update_available=bool(cache.get("latest_version"))
            and _is_newer(cache["latest_version"], current_version),
            error=str(exc),
        )

    now = datetime.now(timezone.utc).isoformat()
    _save_cache(app_data_root, {
        "checked_at": now, "latest_version": latest_version, "release_url": release_url,
    })
    return UpdateCheckResult(
        checked_at=now,
        latest_version=latest_version,
        release_url=release_url,
        current_version=current_version,
        update_available=_is_newer(latest_version, current_version),
        error=None,
    )
