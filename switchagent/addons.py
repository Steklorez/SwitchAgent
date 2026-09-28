"""Add-ons: the hand-curated catalog of open-source Switch utilities
(web/addons/catalog.yaml), and what SwitchAgent knows about putting one of
them on a console.

An entry the Add-ons tab can install names, in its `install:` block, the
project's GitHub repository, the one release asset to take, and -- the
part that makes it safe -- every place on the SD card its files may go.
Nothing of a release is ever written anywhere else, whatever its archive
says; boot files (bootloader/, Atmosphère's own package3, payloads,
Nintendo/, emummc/) cannot even be named there.

Two shapes of release:
  - an archive laid out like the SD card (Ultrahand's sdout.zip, SaltyNX,
    Fizeau): it is that add-on when it holds every one of the entry's
    `sign` files and nothing outside its `places`;
  - one Tesla overlay (.ovl) or homebrew app (.nro): it is that add-on
    when the NACP inside it carries the entry's `name` -- the name the
    Homebrew Menu and the overlay menu show, not the file name.

Recognised that way wherever it is in the Library (a download by
SwitchAgent or a file put there by hand), it becomes an ADDON item: off the
game grid, installed by the ordinary queue, every destination checked
again when its manifest is built.

On a console, an add-on is installed when all its `sign` files are there;
its version is read from the NACP of its `version_from` file (a .ovl/.nro
of at most CONSOLE_PROGRAM_MAX_BYTES, once per size). An add-on found
somewhere else -- the same NACP name at another path -- is "elsewhere":
installed, and not written a second time. Files a person edits (`keep`, e.g. a config.ini) are
written when missing and never replaced.
"""

from __future__ import annotations

import html
import logging
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Iterator, Optional

from . import nro

log = logging.getLogger("switchagent.addons")

CATALOG_PATH = Path(__file__).resolve().parent / "web" / "addons" / "catalog.yaml"
STATIC_DIR = Path(__file__).resolve().parent / "web" / "static"

# What a catalog entry may name in `status` / `install` besides an install
# block: the checks and installers SwitchAgent has built in.
STATUS_CHECKS = ("emuiibo",)
BUILTIN_INSTALLERS = ("emuiibo",)

SINGLE_FILE_SUFFIXES = (".ovl", ".nro")
# No release SwitchAgent installs is anywhere near this (Moonlight's app,
# the biggest, is 17.7 MB).
MAX_ASSET_BYTES = 64 * 1024 * 1024
# Reading a program back off a console to learn its version: overlays are
# ~1 MB; a big app is left at "installed".
CONSOLE_READ_MAX_BYTES = 4 * 1024 * 1024
# A program on a console is read back whole to name it (its NACP lies past
# its code): JKSV is 10 MB, sphaira 5 MB. Once per size, then remembered.
CONSOLE_PROGRAM_MAX_BYTES = 64 * 1024 * 1024
# kefir (github.com/rashevskyv/kefir) ships and updates some of these
# itself; its updater app is how a console running it is recognised.
KEFIR_SIGN = "switch/kefir-updater/kefir-updater.nro"

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_CODE_RE = re.compile(r"`([^`]+)`")

# Never an add-on's to write: the boot chain, the console's own data, and
# Atmosphère's own configuration. Lower-case prefixes of SD-relative paths.
_FORBIDDEN_PLACES = (
    "bootloader/", "nintendo/", "emummc/", "atmosphere/package3", "atmosphere/stratosphere.romfs",
    "atmosphere/fusee", "atmosphere/config/", "atmosphere/config_templates/", "atmosphere/hosts/",
    "atmosphere/kips/", "payload.bin", "boot.dat", "boot.ini", "hbmenu.nro", "switch/dbi/",
)
# Folders many programs share: a place is always something inside one.
_SHARED_FOLDERS = frozenset({
    "atmosphere", "atmosphere/contents", "atmosphere/exefs_patches", "switch", "switch/.overlays",
    "config", "bootloader", "nintendo", "emummc", "emuiibo",
})


class CatalogError(ValueError):
    """The catalog file breaks one of its own rules -- the message says which."""


@dataclass(frozen=True)
class Tip:
    problem: Optional[str]
    answer: str


@dataclass(frozen=True)
class InstallSpec:
    repo: str
    asset: str                      # the release asset: an exact name or a pattern
    name: str                       # the NACP name of `version_from`
    places: tuple[str, ...]         # "dir/" prefixes or exact files, SD-relative
    sign: tuple[str, ...]           # the files that, all together, are it
    version_from: Optional[str]     # the .ovl/.nro whose NACP names and dates it
    keep: tuple[str, ...] = ()      # written when missing, never replaced
    kefir: bool = False             # kefir ships and updates it
    # An archive not laid out like the SD card: (prefix in the archive, SD
    # folder it goes to) -- NXMP's "nxmp/" -> "switch/nxmp/", NooDS's bare
    # noods.nro ("") -> "switch/noods/". Every file must fall under one.
    layout: tuple[tuple[str, str], ...] = ()

    @property
    def single(self) -> bool:
        """A release that is one .ovl/.nro, not an archive."""
        return PurePosixPath(self.asset).suffix.lower() in SINGLE_FILE_SUFFIXES

    def allows(self, dest: str) -> bool:
        low = dest.lower()
        return any(low.startswith(p.lower()) if p.endswith("/") else low == p.lower() for p in self.places)

    def keeps(self, dest: str) -> bool:
        return dest.lower() in {k.lower() for k in self.keep}


@dataclass(frozen=True)
class Addon:
    id: str
    name: str
    tagline: str
    category: str
    author: str
    license: str
    source: str
    releases: str
    about: tuple[str, ...]
    how_it_works: tuple[str, ...]
    install_steps: tuple[str, ...]
    usage: tuple[str, ...]
    controls: tuple[tuple[str, str], ...] = ()
    tips: tuple[Tip, ...] = ()
    requires: tuple[str, ...] = ()
    preview: Optional[str] = None
    preview_caption: Optional[str] = None
    status: Optional[str] = None
    install: Optional[str] = None           # a built-in installer ("emuiibo")
    spec: Optional[InstallSpec] = None      # ...or SwitchAgent's generic one
    related: Optional[tuple[str, str]] = None
    icon: Optional[str] = None              # "addon-icons/<id>.<ext>" in web/static/

    @property
    def installable(self) -> bool:
        return self.install is not None or self.spec is not None


# ---------------------------------------------------------------------------
# The catalog file
# ---------------------------------------------------------------------------

def _text(entry: dict, key: str, where: str, *, required: bool = True) -> Optional[str]:
    value = entry.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip():
        raise CatalogError(f"{where}: `{key}` must be a non-empty text")
    return value.strip()


def _lines(entry: dict, key: str, where: str, *, required: bool = True) -> tuple[str, ...]:
    value = entry.get(key)
    if value is None and not required:
        return ()
    if not isinstance(value, list) or not value or not all(isinstance(v, str) and v.strip() for v in value):
        raise CatalogError(f"{where}: `{key}` must be a list of texts")
    return tuple(v.strip() for v in value)


def _https(url: str, where: str, key: str) -> str:
    if not url.startswith("https://"):
        raise CatalogError(f"{where}: `{key}` must be an https:// link")
    return url


def _sd_path(value, where: str, key: str) -> str:
    """One SD-relative path of an install block, checked: relative, plain,
    at least two levels deep ("config/x.ini", "switch/App/") and nowhere
    near the boot chain."""
    if not isinstance(value, str) or not value.strip():
        raise CatalogError(f"{where}: every `{key}` entry must be a path")
    path = value.strip()
    parts = path.rstrip("/").split("/")
    if (path.startswith("/") or "\\" in path or ":" in path
            or any(p in ("", ".", "..") for p in parts)):
        raise CatalogError(f"{where}: `{key}` {path!r} is not a plain SD-card path")
    low = path.lower()
    # A folder of its own at the root of the card (SaltyNX's SaltySD/) is
    # fine; one everybody shares is too broad.
    if low.rstrip("/") in _SHARED_FOLDERS:
        raise CatalogError(f"{where}: `{key}` {path!r} is too broad -- name a folder inside it")
    if len(parts) < 2 and not path.endswith("/"):
        raise CatalogError(f"{where}: `{key}` {path!r} is a file at the root of the card")
    if any(low.startswith(f) or f.startswith(low) for f in _FORBIDDEN_PLACES):
        raise CatalogError(f"{where}: `{key}` {path!r} is not a place an add-on may write")
    return path


def _install_spec(block, where: str) -> InstallSpec:
    if not isinstance(block, dict):
        raise CatalogError(f"{where}: `install` must be a built-in installer's name or an install block")
    repo = _text(block, "repo", where)
    if not _REPO_RE.match(repo):
        raise CatalogError(f"{where}: `repo` must be owner/name on GitHub")
    asset = _text(block, "asset", where)
    name = _text(block, "name", where)
    single = PurePosixPath(asset).suffix.lower() in SINGLE_FILE_SUFFIXES
    if single:
        to = _sd_path(_text(block, "to", where), where, "to")
        if PurePosixPath(to).suffix.lower() != PurePosixPath(asset).suffix.lower() or to.endswith("/"):
            raise CatalogError(f"{where}: `to` must be the file's own place, with its own extension")
        return InstallSpec(repo=repo, asset=asset, name=name, places=(to,), sign=(to,), version_from=to,
                           kefir=bool(block.get("kefir")))
    places = block.get("places")
    if not isinstance(places, list) or not places:
        raise CatalogError(f"{where}: an archive's install block needs `places`")
    layout = []
    raw_layout = block.get("layout") or {}
    if not isinstance(raw_layout, dict):
        raw_layout = None
    if raw_layout is None:
        raise CatalogError(f"{where}: `layout` maps a folder in the archive to a folder on the SD card")
    for source, target in raw_layout.items():
        source = str(source or "")
        if source and (not source.endswith("/") or source.startswith("/") or "\\" in source
                       or any(p in ("", ".", "..") for p in source.rstrip("/").split("/"))):
            raise CatalogError(f"{where}: `layout` source {source!r} must be a plain folder ending in /, or \"\"")
        target = _sd_path(target, where, "layout")
        if not target.endswith("/"):
            raise CatalogError(f"{where}: `layout` target {target!r} must be a folder ending in /")
        layout.append((source, target))
    layout.sort(key=lambda pair: -len(pair[0]))  # the most specific source first
    spec = InstallSpec(
        repo=repo, asset=asset, name=name, layout=tuple(layout),
        places=tuple(_sd_path(p, where, "places") for p in places),
        sign=tuple(_sd_path(p, where, "sign") for p in block.get("sign") or []),
        version_from=_sd_path(block["version_from"], where, "version_from") if block.get("version_from") else None,
        keep=tuple(_sd_path(p, where, "keep") for p in block.get("keep") or []),
        kefir=bool(block.get("kefir")),
    )
    if not spec.sign:
        raise CatalogError(f"{where}: an archive's install block needs `sign` -- the files that are it")
    for key, paths in (("sign", spec.sign), ("keep", spec.keep),
                       ("version_from", (spec.version_from,) if spec.version_from else ())):
        for path in paths:
            if path.endswith("/") or not spec.allows(path):
                raise CatalogError(f"{where}: `{key}` {path!r} is not a file inside its `places`")
    if spec.version_from and PurePosixPath(spec.version_from).suffix.lower() not in SINGLE_FILE_SUFFIXES:
        raise CatalogError(f"{where}: `version_from` must be an .ovl or .nro")
    return spec


def parse_catalog(doc) -> list[Addon]:
    """The catalog's YAML document -> its entries, in order. Raises
    CatalogError for anything the tab could not show truthfully, or an
    install that could write anywhere it should not."""
    if not isinstance(doc, dict) or not isinstance(doc.get("addons"), list):
        raise CatalogError("the catalog must have an `addons:` list")
    out: list[Addon] = []
    for index, entry in enumerate(doc["addons"]):
        where = f"entry {index + 1}"
        if not isinstance(entry, dict):
            raise CatalogError(f"{where}: must be a mapping")
        addon_id = _text(entry, "id", where)
        if not _ID_RE.match(addon_id):
            raise CatalogError(f"{where}: `id` must be lowercase letters, digits and dashes")
        where = f"`{addon_id}`"
        status = _text(entry, "status", where, required=False)
        if status is not None and status not in STATUS_CHECKS:
            raise CatalogError(f"{where}: unknown `status` check {status!r} (known: {', '.join(STATUS_CHECKS)})")
        install = entry.get("install")
        builtin, spec = None, None
        if isinstance(install, str):
            if install not in BUILTIN_INSTALLERS:
                raise CatalogError(f"{where}: unknown `install` {install!r} (known: {', '.join(BUILTIN_INSTALLERS)})")
            builtin = install
        elif install is not None:
            spec = _install_spec(install, where)
        preview = _text(entry, "preview", where, required=False)
        if preview is not None and (not (STATIC_DIR / preview).is_file() or "/" in preview or "\\" in preview):
            raise CatalogError(f"{where}: `preview` {preview!r} is not a file in web/static/")
        requires = entry.get("requires") or []
        if not isinstance(requires, list) or not all(isinstance(r, str) for r in requires):
            raise CatalogError(f"{where}: `requires` must be a list of ids")
        controls = []
        for control in entry.get("controls") or []:
            if not isinstance(control, dict) or not control.get("keys") or not control.get("action"):
                raise CatalogError(f"{where}: every control needs `keys` and `action`")
            controls.append((str(control["keys"]), str(control["action"])))
        tips = []
        for tip in entry.get("tips") or []:
            if isinstance(tip, str) and tip.strip():
                tips.append(Tip(problem=None, answer=tip.strip()))
            elif isinstance(tip, dict) and tip.get("problem") and tip.get("answer"):
                tips.append(Tip(problem=str(tip["problem"]).strip(), answer=str(tip["answer"]).strip()))
            else:
                raise CatalogError(f"{where}: a tip is a sentence or a {{problem, answer}} pair")
        related = entry.get("related")
        if related is not None:
            if not isinstance(related, dict) or not related.get("label") or not str(related.get("href", "")).startswith("/"):
                raise CatalogError(f"{where}: `related` needs a `label` and an in-app `href` starting with /")
            related = (str(related["label"]), str(related["href"]))
        about = _text(entry, "about", where)
        out.append(Addon(
            id=addon_id, name=_text(entry, "name", where), tagline=_text(entry, "tagline", where),
            category=_text(entry, "category", where), author=_text(entry, "author", where),
            license=_text(entry, "license", where),
            source=_https(_text(entry, "source", where), where, "source"),
            releases=_https(_text(entry, "releases", where), where, "releases"),
            about=tuple(p.strip() for p in re.split(r"\n\s*\n", about) if p.strip()),
            how_it_works=_lines(entry, "how_it_works", where),
            install_steps=_lines(entry, "install_steps", where),
            usage=_lines(entry, "usage", where),
            controls=tuple(controls), tips=tuple(tips), requires=tuple(requires),
            preview=preview, preview_caption=_text(entry, "preview_caption", where, required=False),
            status=status, install=builtin, spec=spec, related=related, icon=_icon_of(addon_id),
        ))
    ids = [a.id for a in out]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        raise CatalogError(f"ids used twice: {', '.join(duplicates)}")
    for addon in out:
        unknown = [r for r in addon.requires if r not in ids]
        if unknown:
            raise CatalogError(f"`{addon.id}` requires {', '.join(unknown)}, which the catalog does not have")
    _check_no_cycles(out)
    return out


ICON_DIR = STATIC_DIR / "addon-icons"
ICON_SUFFIXES = (".jpg", ".png", ".svg")


def _icon_of(addon_id: str) -> Optional[str]:
    """web/static/addon-icons/<id>.jpg|png|svg: the project's own icon, taken
    from its release's .nro, or SwitchAgent's own drawing where the project
    ships none (an overlay's is libtesla's placeholder, the same for all)."""
    for suffix in ICON_SUFFIXES:
        if (ICON_DIR / f"{addon_id}{suffix}").is_file():
            return f"addon-icons/{addon_id}{suffix}"
    return None


def _check_no_cycles(addons: list[Addon]) -> None:
    by_id = {a.id: a for a in addons}

    def visit(addon_id: str, trail: tuple[str, ...]) -> None:
        if addon_id in trail:
            raise CatalogError(f"`{addon_id}` requires itself through {' -> '.join(trail + (addon_id,))}")
        for r in by_id[addon_id].requires:
            visit(r, trail + (addon_id,))

    for a in addons:
        visit(a.id, ())


_catalog_lock = threading.Lock()
_catalog_cache: Optional[tuple[float, list[Addon]]] = None


def load_catalog(path: Path = CATALOG_PATH) -> list[Addon]:
    """The catalog, re-read when the file changes (so an edited entry shows
    up without restarting)."""
    global _catalog_cache
    import yaml

    mtime = path.stat().st_mtime
    with _catalog_lock:
        if _catalog_cache is not None and _catalog_cache[0] == mtime and path == CATALOG_PATH:
            return _catalog_cache[1]
    addons = parse_catalog(yaml.safe_load(path.read_text(encoding="utf-8")))
    if path == CATALOG_PATH:
        with _catalog_lock:
            _catalog_cache = (mtime, addons)
    return addons


def installable_catalog() -> list[Addon]:
    """The entries with a generic install block -- what the Library can
    recognise. Never raises: a catalog that does not load recognises
    nothing (and says so in the log), it does not stop a scan."""
    try:
        return [a for a in load_catalog() if a.spec is not None]
    except (CatalogError, OSError, ValueError) as exc:
        log.warning("the add-ons catalog could not be loaded: %s", exc)
        return []


def by_id(addon_id: str) -> Optional[Addon]:
    return next((a for a in load_catalog() if a.id == addon_id), None)


def install_order(addon_ids: list[str]) -> list[Addon]:
    """Every add-on named and everything it requires, requirements first,
    each once."""
    catalog = {a.id: a for a in load_catalog()}
    out: list[Addon] = []

    def add(addon_id: str) -> None:
        addon = catalog[addon_id]
        for r in addon.requires:
            add(r)
        if addon not in out:
            out.append(addon)

    for addon_id in addon_ids:
        add(addon_id)
    return out


def rich(text: str):
    """Catalog text for the page: escaped, with `backticks` as <code>."""
    from markupsafe import Markup

    return Markup(_CODE_RE.sub(lambda m: f"<code>{m.group(1)}</code>", html.escape(text, quote=False)))


# ---------------------------------------------------------------------------
# A release, recognised
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ReleaseMatch:
    addon_id: str
    # (path inside the archive, or the file's own name; SD destination; size)
    files: tuple[tuple[str, str, int], ...]
    keep: tuple[str, ...] = ()
    version_source: Optional[str] = None    # inside the archive: the file to read the version from

    def to_dict(self, version: Optional[str]) -> dict:
        return {"id": self.addon_id, "version": version, "files": [list(f) for f in self.files],
                "keep": list(self.keep)}


def _normalise(name: str) -> str:
    return name.replace("\\", "/").lstrip("/")


# What an archive made on a Mac or in Explorer carries along that is no part
# of the release: never copied, never counted against its places.
_JUNK_PARTS = ("__macosx",)
_JUNK_NAMES = (".ds_store", "thumbs.db", "desktop.ini")


def _is_junk(path: str) -> bool:
    parts = path.lower().split("/")
    return any(p in _JUNK_PARTS for p in parts[:-1]) or parts[-1] in _JUNK_NAMES or parts[-1].startswith("._")


def _laid_out(spec: InstallSpec, paths: list[tuple[str, int]]) -> list[list[tuple[str, str, int]]]:
    """The ways this archive could be laid out on the SD card, as (path in
    the archive, SD path, size): by the entry's own `layout` when it has one;
    else as it is, or from inside its one wrapper folder."""
    if spec.layout:
        mapped = []
        for path, size in paths:
            rule = next(((src, dst) for src, dst in spec.layout if path.lower().startswith(src.lower())), None)
            if rule is None:
                return []
            mapped.append((path, rule[1] + path[len(rule[0]):], size))
        return [mapped]
    options = [[(p, p, s) for p, s in paths]]
    tops = {p.split("/", 1)[0] for p, _ in paths}
    if len(tops) == 1 and all("/" in p for p, _ in paths):
        top = next(iter(tops))
        options.append([(p, p[len(top) + 1:], s) for p, s in paths])
    return options


def match_archive(files: list[tuple[str, int]], catalog: Optional[list[Addon]] = None) -> Optional[ReleaseMatch]:
    """The catalog add-on whose release this archive is: every one of its
    `sign` files in it, and nothing in it outside its `places` -- laid out
    at the root, or inside one wrapper folder. None otherwise."""
    catalog = installable_catalog() if catalog is None else catalog
    paths = [(_normalise(n), s) for n, s in files if not n.endswith("/") and not _is_junk(_normalise(n))]
    if not paths:
        return None
    for addon in catalog:
        spec = addon.spec
        if spec is None or spec.single:
            continue
        for laid_out in _laid_out(spec, paths):
            lowered = {dest.lower() for _src, dest, _s in laid_out}
            if all(spec.allows(dest) for _src, dest, _s in laid_out) and all(s.lower() in lowered for s in spec.sign):
                version_source = next((src for src, dest, _s in laid_out
                                       if spec.version_from and dest.lower() == spec.version_from.lower()), None)
                return ReleaseMatch(
                    addon_id=addon.id,
                    files=tuple(laid_out),
                    keep=tuple(dest for _src, dest, _s in laid_out if spec.keeps(dest)),
                    version_source=version_source,
                )
    return None


def match_program(file_name: str, info: Optional[nro.NroInfo], size: int,
                  catalog: Optional[list[Addon]] = None) -> Optional[ReleaseMatch]:
    """The catalog add-on this one .ovl/.nro is, by the name in its own
    NACP. None otherwise."""
    if info is None or not info.name:
        return None
    catalog = installable_catalog() if catalog is None else catalog
    suffix = PurePosixPath(file_name).suffix.lower()
    for addon in catalog:
        spec = addon.spec
        if (spec is not None and spec.single and PurePosixPath(spec.asset).suffix.lower() == suffix
                and info.name.strip().lower() == spec.name.lower()):
            return ReleaseMatch(addon_id=addon.id, files=((file_name, spec.places[0], size),))
    return None


# ---------------------------------------------------------------------------
# On a console
# ---------------------------------------------------------------------------

@dataclass
class ConsoleAddons:
    """What of the catalog's add-ons one console holds, as read over MTP."""
    files: dict[str, Optional[int]] = field(default_factory=dict)   # lower-cased path -> size, found only
    programs: dict[str, dict] = field(default_factory=dict)         # lower-cased path -> {name, version, size}
    kefir: bool = False
    complete: bool = False
    # Every path the read looked for (lower-cased): what is absent from
    # `files` but in here is known NOT to be there. A catalog entry added
    # since that read was never looked for -- "not checked yet", not "missing".
    checked: set = field(default_factory=set)

    def to_dict(self) -> dict:
        return {"files": self.files, "programs": self.programs, "kefir": self.kefir,
                "checked": sorted(self.checked)}

    @classmethod
    def from_dict(cls, d: dict) -> "ConsoleAddons":
        return cls(files=dict(d.get("files") or {}), programs=dict(d.get("programs") or {}),
                   kefir=bool(d.get("kefir")), complete=True, checked=set(d.get("checked") or ()))


def console_paths(catalog: list[Addon]) -> list[str]:
    paths = []
    for addon in catalog:
        spec = addon.spec
        if spec is None:
            continue
        paths.extend(spec.sign)
        if spec.version_from:
            paths.append(spec.version_from)
    paths.append(KEFIR_SIGN)
    return list(dict.fromkeys(paths))


# Where a person's homebrew can be, besides where the catalog puts it: the
# Homebrew Menu itself (sphaira often takes its place as /hbmenu.nro), a
# loose .nro straight in switch/, or any folder one level below it. Every
# program found there is known by its NACP name, never by its path.
_PROGRAM_EXTENSIONS = (".nro", ".ovl")


def read_device(backend, storage: str, catalog: list[Addon], *,
                known: Optional[ConsoleAddons] = None) -> Iterator[ConsoleAddons]:
    """Reads which add-ons a console holds, one MTP request at a time (a
    generator, like emuiibo.read_device: the worker thread can stop after
    any yield and carry on later). Folders are listed once each and found
    whatever their case (FAT does not care, MTP does); every program in the
    places a person puts homebrew is named from its own NACP, so one that
    lies somewhere else than the catalog's path is still recognised. A
    program is read back only when its size changed since the last read.
    The last state yielded has complete=True."""
    state = ConsoleAddons()
    previous = known.programs if known is not None else {}
    paths = console_paths(catalog)
    listings: dict[str, Optional[dict]] = {}
    real = {"": ""}  # lower-cased folder path -> as the console spells it

    def list_folder(key: str):
        if key in listings:
            return
        if key:
            parent, _, name = key.rpartition("/")
            yield from list_folder(parent)
            entry = (listings.get(parent) or {}).get(name)
            if entry is None or not entry.is_dir:
                listings[key] = None
                return
            real[key] = f"{real[parent]}/{entry.name}" if parent else entry.name
        entries = backend.list_directory(storage, real[key])
        listings[key] = None if entries is None else {e.name.lower(): e for e in entries}
        yield state

    def found(key: str):
        parent, _, name = key.rpartition("/")
        entry = (listings.get(parent) or {}).get(name)
        if entry is None or entry.is_dir:
            return None
        return (f"{real[parent]}/{entry.name}" if parent else entry.name), entry.size

    for parent in dict.fromkeys(p.rsplit("/", 1)[0].lower() for p in paths):
        yield from list_folder(parent)
    yield from list_folder("")
    yield from list_folder("switch")
    for name, entry in sorted((listings.get("switch") or {}).items()):
        if entry.is_dir:
            yield from list_folder(f"switch/{name}")
    state.checked = {p.lower() for p in paths}

    wanted: dict[str, tuple[str, Optional[int]]] = {}  # lower path -> (real path, size)
    for path in paths:
        hit = found(path.lower())
        if hit is not None:
            state.files[path.lower()] = hit[1]
            wanted[path.lower()] = hit
    for folder in ["", "switch"] + [f"switch/{n}" for n, e in sorted((listings.get("switch") or {}).items())
                                    if e.is_dir]:
        for name, entry in sorted((listings.get(folder) or {}).items()):
            if not entry.is_dir and name.endswith(_PROGRAM_EXTENSIONS):
                key = f"{folder}/{name}" if folder else name
                hit = found(key)
                state.files[key] = hit[1]
                wanted[key] = hit
    state.kefir = KEFIR_SIGN.lower() in state.files

    versioned = {a.spec.version_from.lower() for a in catalog if a.spec is not None and a.spec.version_from}
    for key, (path, size) in wanted.items():
        if key not in versioned and not key.endswith(_PROGRAM_EXTENSIONS):
            continue
        old = previous.get(key)
        # An entry from before programs were read up to this size (name
        # None, never "read") is read again once, not trusted forever.
        if old is not None and size is not None and old.get("size") == size and (
                old.get("read") or old.get("name") is not None or size > CONSOLE_PROGRAM_MAX_BYTES):
            state.programs[key] = {**old, "path": path}
            continue
        if size is None or size > CONSOLE_PROGRAM_MAX_BYTES:
            state.programs[key] = {"name": None, "version": None, "size": size, "path": path}
            continue
        data = backend.read_file(storage, path, max_bytes=CONSOLE_PROGRAM_MAX_BYTES)
        info = nro.nro_info_from_bytes(data) if data else None
        state.programs[key] = {"name": info.name if info else None, "version": info.version if info else None,
                               "size": size, "path": path, "read": True}
        yield state
    state.complete = True
    yield state


def found_elsewhere(addon: Addon, console: ConsoleAddons) -> Optional[tuple[str, dict]]:
    """(path, program) of this add-on somewhere else than the catalog puts
    it -- known by the name in its own NACP (sphaira as /hbmenu.nro, JKSV
    as a loose switch/JKSV.nro). None when it is nowhere, or when the
    catalog's own path holds it."""
    spec = addon.spec
    if spec is None or not spec.version_from:
        return None
    own = spec.version_from.lower()
    for key, program in sorted(console.programs.items()):
        name = (program.get("name") or "").strip().lower()
        if key != own and name and name == spec.name.lower():
            return program.get("path") or key, program
    return None


def status(addon: Addon, console: Optional[ConsoleAddons]) -> Optional[dict]:
    """{state, label, version, other} for an add-on with an install block,
    as of the console's last read -- "installed", "partial", "missing",
    "other" (another program sits where this one goes: Tesla Menu where
    Ultrahand's menu belongs, say) or "unknown" (never read)."""
    spec = addon.spec
    if spec is None:
        return None
    if console is None:
        return {"state": "unknown", "label": "not read yet", "version": None, "other": None, "kefir": False}
    if any(s.lower() not in console.checked for s in spec.sign):
        return {"state": "unknown", "label": "not checked yet — it is looked for the next time the Switch is read",
                "version": None, "other": None, "kefir": False}
    present = [s.lower() in console.files for s in spec.sign]
    program = console.programs.get(spec.version_from.lower()) if spec.version_from else None
    version = program.get("version") if program else None
    kefir = bool(spec.kefir and console.kefir)
    if all(present):
        name = (program or {}).get("name")
        if name and name.strip().lower() != spec.name.lower():
            return {"state": "other", "label": f"{name}{' ' + program['version'] if program.get('version') else ''} "
                                              f"is in its place", "version": None, "other": name, "kefir": kefir}
        label = f"installed{' — ' + version if version else ''}" + (" (kefir keeps it up to date)" if kefir else "")
        return {"state": "installed", "label": label, "version": version, "other": None, "kefir": kefir}
    if any(present):
        missing = [s for s, p in zip(spec.sign, present) if not p]
        return {"state": "partial", "label": f"incomplete — {', '.join(missing)} is missing",
                "version": version, "other": None, "kefir": kefir}
    elsewhere = found_elsewhere(addon, console)
    if elsewhere is not None:
        path, program = elsewhere
        version = program.get("version")
        return {"state": "elsewhere", "label": f"installed{' — ' + version if version else ''} as {path}",
                "version": version, "other": None, "kefir": False, "path": path}
    return {"state": "missing", "label": "not installed", "version": None, "other": None, "kefir": False}


_DATE_VERSION_RE = re.compile(r"^(\d{1,2})[./-](\d{1,2})[./-](\d{4})$")
_COMMIT_SUFFIX_RE = re.compile(r"[-.]g?(?=[0-9a-f]*[a-f])(?=[0-9a-f]*\d)[0-9a-f]{7,40}$", re.IGNORECASE)


def version_key(version: Optional[str]) -> Optional[tuple[int, ...]]:
    """A version as something to compare: "v2.5.3" -> (2, 5, 3),
    "1.4.1+r4" -> (1, 4, 1, 4) -- a rebuild of 1.4.1 is newer than it --
    and a date release like JKSV's "12/02/2025" (month/day/year, the same
    in its NACP as "12.02.2025") -> (2025, 12, 2). None when there is no
    number in it at all."""
    if not version:
        return None
    text = version.strip().lstrip("vV")
    # A build's commit hash is no part of its version: EdiZon's
    # "v1.0.15-a12ce33", Turnips' "1.7.7-6c8fda4". ("+r4", a rebuild, stays.)
    text = _COMMIT_SUFFIX_RE.sub("", text)
    date = _DATE_VERSION_RE.match(text)
    if date:
        month, day, year = (int(g) for g in date.groups())
        if 1 <= month <= 12 and 1 <= day <= 31:
            return (year, month, day)
    numbers = re.findall(r"\d+", text)
    return tuple(int(n) for n in numbers) if numbers else None


def is_newer(latest: Optional[str], installed: Optional[str]) -> bool:
    a, b = version_key(latest), version_key(installed)
    return a is not None and b is not None and a > b


def is_installed(addon: Addon, console: Optional[ConsoleAddons], emuiibo_state: Optional[dict] = None) -> bool:
    """For an Install click: does this console already have it (so a
    requirement is not installed again)?"""
    if addon.install == "emuiibo":
        components = (emuiibo_state or {}).get("components") or {}
        return bool(components.get("sysmodule") and components.get("overlay"))
    found = status(addon, console)
    return bool(found and found["state"] in ("installed", "elsewhere"))
