"""Game covers, found by one background thread and kept on disk.

Where a cover comes from, first answer wins: TitleDB by TITLE_ID; the icon
inside the game's own .nro (a homebrew port's -- exact, no network); TitleDB
by name; Steam's store search by exactly the same name (ports of PC games:
no key, nothing about the user sent). Each is fetched once and kept in
DATA_DIR/covers; nothing is looked up again for a game that has one."""
import difflib
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit
from . import config, nro, preferences, title_id as title_id_mod

log = logging.getLogger(__name__)
TITLEDB = "https://raw.githubusercontent.com/blawar/titledb/master/US.en.json"
STEAM_SEARCH = "https://store.steampowered.com/api/storesearch/?cc=us&l=en&term="
# Portrait library art first (square tiles crop its middle, where the title
# is), the wide header where an old game has no portrait art.
STEAM_ART = ("https://cdn.akamai.steamstatic.com/steam/apps/{}/library_600x900.jpg",
             "https://cdn.akamai.steamstatic.com/steam/apps/{}/header.jpg")
_ALLOWED = {"raw.githubusercontent.com", "img-eshop.cdn.nintendo.net", "assets.nintendo.com",
            "store.steampowered.com", "cdn.akamai.steamstatic.com", "shared.akamai.steamstatic.com"}
# A game with no TITLE_ID is known to Library by its row: "sd-12",
# "port-294" (a forwarder that keeps its TITLE_ID sealed), "other-7".
_COVER_ID = re.compile(r"[0-9A-F]{16}|(?:SD|PORT|UNKNOWN|OTHER)-[0-9]{1,12}", re.IGNORECASE)
# How many files of a game's folder are looked at for its .nro -- a folder
# of thousands of data files must not stall the cover thread.
_NRO_SCAN_LIMIT = 2000
# Fraction of matching characters difflib requires before a name-search
# fallback (see CoverQueue._lookup_by_name) accepts a close-but-not-exact
# TitleDB name as a match. Deliberately strict -- a wrong cover is worse
# than no cover, this is only meant to absorb punctuation/whitespace noise
# an exact match on title_id_mod.normalize_name() didn't already handle,
# not to guess between genuinely different games.
_NAME_MATCH_CUTOFF = 0.9


def allowed_url(url):
    parts = urlsplit(url)
    return parts.scheme == "https" and parts.hostname in _ALLOWED and parts.port in (None, 443) and not parts.username


def _is_image(data):
    return data.startswith(b"\xff\xd8\xff") or data.startswith(b"\x89PNG\r\n\x1a\n") or (data[:4] == b"RIFF" and data[8:12] == b"WEBP")


def nro_icon(paths):
    """The icon of the first .nro among `paths` -- each a .nro itself or a
    folder searched for one (never an archive). None when there is none."""
    for raw in paths:
        path = Path(raw)
        try:
            if path.is_file():
                candidates = [path] if path.suffix.lower() == ".nro" else []
            elif path.is_dir():
                candidates = []
                for index, found in enumerate(path.rglob("*")):
                    if index >= _NRO_SCAN_LIMIT:
                        break
                    if found.suffix.lower() == ".nro":
                        candidates.append(found)
                candidates.sort()
            else:
                continue
        except OSError:
            continue
        for candidate in candidates:
            icon = nro.read_nro_icon(candidate)
            if icon:
                return icon
    return None


def steam_cover(name):
    """Steam's art for the store app named exactly `name` (compared through
    normalize_name, like every name match here). None rather than a guess:
    a near name is a different game as often as not. What a release adds in
    brackets ("Zuma Deluxe (port)") is not part of the name."""
    name = re.sub(r"\s*[(\[][^)\]]*[)\]]", "", name or "").strip()
    key = title_id_mod.normalize_name(name)
    if not key:
        return None
    found = json.loads(download(STEAM_SEARCH + urllib.parse.quote(name), 1024 * 1024))
    apps = [item["id"] for item in found.get("items", [])
            if item.get("type") == "app" and title_id_mod.normalize_name(item.get("name") or "") == key]
    if not apps:
        return None
    for template in STEAM_ART:
        try:
            data = download(template.format(int(apps[0])), 5 * 1024 * 1024)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                continue
            raise
        if _is_image(data):
            return data
    return None


def download(url, limit):
    if not allowed_url(url):
        raise ValueError("Unsupported cover host")
    class Redirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            if not allowed_url(newurl):
                raise ValueError("Unsupported redirect host")
            return super().redirect_request(req, fp, code, msg, headers, newurl)
    with urllib.request.build_opener(Redirect()).open(url, timeout=15) as response:
        content = response.read(limit + 1)
    if len(content) > limit:
        raise ValueError("Cover response exceeds size limit")
    return content


def valid_cover_id(value):
    return bool(value) and bool(_COVER_ID.fullmatch(value))


def image_path(title_id):
    if not valid_cover_id(title_id):
        raise ValueError("Invalid cover id")
    return config.DATA_DIR / "covers" / (title_id.upper() + ".img")


class NoCoverError(ValueError):
    """No source has a cover for this game -- a homebrew port's own
    TITLE_ID, as a rule. An answer, not a failure: nothing went wrong, and
    retrying will not change it."""


class CoverQueue:
    def __init__(self):
        self.lock = threading.Lock()
        self.pending = set()
        self.retry_after = {}
        self.running = False
        self.error = None
        self.index = None
        self.requested = set()
        self.names = {}
        self.paths = {}
        self.phase = "Idle"
        # Which titles ended without a cover, and why: `error` above keeps
        # the last reason for anyone who reads it, these say which of them
        # were a real failure (network, a bad image) and which were only
        # "TitleDB has none" -- what the Library page must not show as red.
        self.not_found = set()
        self.failed = set()

    def snapshot(self):
        with self.lock:
            ready = sorted(tid for tid in self.requested if image_path(tid).is_file())
            return {"running": self.running, "phase": self.phase, "ready": ready,
                    "total": len(self.requested), "error": self.error,
                    "not_found": len(self.not_found & self.requested),
                    "failed": len(self.failed & self.requested)}

    def submit(self, items, *, retry=False):
        """items: an iterable of either a bare TITLE_ID string, or a
        (title_id, name) pair -- `name` (the game's raw display name,
        release tags and all -- see services._resolve_entry_name) is used
        as a fallback TitleDB search key in _run() when the TITLE_ID itself
        isn't found there. A release filename's bracketed TITLE_ID is never
        more than a low-confidence guess (title_id.py's from_filename
        docstring) -- this is what lets a cover still be found when that
        guess is wrong or stale, without the user having to fix the
        filename themselves."""
        if not preferences.load()["covers"]:
            return
        with self.lock:
            if retry and not self.running:
                self.retry_after.clear()
                self.error = None
                self.not_found.clear()
                self.failed.clear()
            for item in items:
                tid, name, paths = (tuple(item) + (None, None))[:3] if isinstance(item, tuple) else (item, None, None)
                if valid_cover_id(tid):
                    tid = tid.upper()
                    self.requested.add(tid)
                    if name:
                        self.names[tid] = title_id_mod.strip_release_tags(name)
                    if paths:
                        self.paths[tid] = list(paths)
                    if not image_path(tid).exists() and self.retry_after.get(tid, 0) < time.time():
                        self.pending.add(tid)
                        self.not_found.discard(tid)
                        self.failed.discard(tid)
            if self.running or not self.pending:
                return
            self.running = True
            threading.Thread(target=self._run, daemon=True, name="switchagent-covers").start()

    def _load_index(self):
        """Returns {"by_id": {TITLE_ID: iconUrl}, "by_name": {normalized
        name: iconUrl}} -- both built from the same TitleDB download, so a
        TITLE_ID miss (see _run) can still resolve through the game's name.
        An old-format (flat TITLE_ID -> iconUrl) cache from before by_name
        existed is treated as stale rather than crashing on it."""
        self.phase = "Loading TitleDB catalogue"
        path = config.DATA_DIR / "covers" / "index.json"
        if path.exists() and time.time() - path.stat().st_mtime < 7 * 86400:
            cached = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(cached, dict) and "by_id" in cached and "by_name" in cached:
                return cached
        try:
            data = json.loads(download(TITLEDB, 128 * 1024 * 1024))
            by_id = {}
            by_name = {}
            for row in data.values():
                icon = row.get("iconUrl")
                if not icon or not allowed_url(icon):
                    continue
                tid = str(row.get("id") or "").upper()
                if tid:
                    by_id[tid] = icon
                name = row.get("name")
                if name:
                    by_name.setdefault(title_id_mod.normalize_name(name), icon)
            index = {"by_id": by_id, "by_name": by_name}
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_suffix(".tmp")
            temp.write_text(json.dumps(index), encoding="utf-8")
            temp.replace(path)
            return index
        except Exception:
            if path.exists():
                cached = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(cached, dict) and "by_id" in cached and "by_name" in cached:
                    return cached
            raise

    def _lookup_by_name(self, name):
        """Exact match on the normalized name first; a close-but-not-exact
        match (difflib, see _NAME_MATCH_CUTOFF) only if that fails -- e.g. a
        release filename's name has one extra/missing word TitleDB's
        doesn't. Returns None rather than guessing between two plausibly
        different games."""
        if not name:
            return None
        by_name = self.index["by_name"]
        key = title_id_mod.normalize_name(name)
        url = by_name.get(key)
        if url:
            return url
        close = difflib.get_close_matches(key, by_name.keys(), n=1, cutoff=_NAME_MATCH_CUTOFF)
        return by_name[close[0]] if close else None

    def _find(self, tid):
        name = self.names.get(tid)
        url = self.index["by_id"].get(tid)
        if url:
            self.phase = "Downloading covers"
            return download(url, 5 * 1024 * 1024)
        icon = nro_icon(self.paths.get(tid) or ())
        if icon:
            return icon
        url = self._lookup_by_name(name)
        if url:
            self.phase = "Downloading covers"
            return download(url, 5 * 1024 * 1024)
        if name:
            self.phase = "Searching Steam"
            return steam_cover(name)
        return None

    def _run(self):
        while True:
            with self.lock:
                if not self.pending or not preferences.load()["covers"]:
                    self.pending.clear()
                    self.running = False
                    self.phase = "Finished" if not self.error else "Finished with errors"
                    return
                tid = self.pending.pop()
            try:
                if self.index is None:
                    self.index = self._load_index()
                data = self._find(tid)
                if data is None:
                    raise NoCoverError("No cover in TitleDB, the game's own .nro or Steam")
                if not _is_image(data):
                    raise ValueError("Not a supported image")
                path = image_path(tid)
                path.parent.mkdir(parents=True, exist_ok=True)
                temp = path.with_suffix(".tmp")
                temp.write_bytes(data)
                temp.replace(path)
                self.error = None
                with self.lock:
                    self.not_found.discard(tid)
                    self.failed.discard(tid)
            except Exception as exc:
                log.info("Cover unavailable for %s: %s", tid, exc)
                with self.lock:
                    self.retry_after[tid] = time.time() + 3600
                    self.error = str(exc)
                    (self.not_found if isinstance(exc, NoCoverError) else self.failed).add(tid)
                    if self.index is None:
                        for pending in self.pending:
                            self.retry_after[pending] = time.time() + 3600
                        self.pending.clear()
