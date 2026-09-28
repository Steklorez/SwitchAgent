"""Files that are copied onto the SD card as they are, and which game they
belong to.

A homebrew port is released as two halves (real library, 2026-09-23):

  Zuma Deluxe (homebrew port) for Nintendo Switch [NSP]/
    Zuma Deluxe (port) [01333de6fb400000].nsp    412 KB -- the forwarder
    switch.7z                                     7 MB -- switch/zumaportable/...

The .nsp is a forwarder: installed through DBI like any game, it puts an
icon on the home menu and, when started, launches an .nro from the SD card
-- `sdmc:/switch/zumaportable/dbc17o.nro` for this one, stored in plain
text inside the package. Nothing but the `switch/` folder makes that file
exist, and it has to land on the card exactly where the release laid it
out. That is the one thing an SD_FILES item is: a `switch/` tree, copied to
`switch/` on the SD card, file by file, never anywhere else.

Such a tree has no TITLE_ID of its own, so without help it becomes a
nameless card called "switch" next to the game it is half of. Which game it
belongs to is decided here, in order:

  1. its own filename, if the release put a [TITLE_ID] in it -- the same
     label rule every package already follows;
  2. a forwarder in the library that launches an .nro inside it -- the
     package itself says which folder it needs, so this is structure, not
     a guess;
  3. the folder it sits in: the nearest folder above it that holds any
     game at all decides, and only if that is exactly one game. A folder
     with two games in it says nothing about which one this belongs to,
     and then it stays its own card.

A lone .nro found anywhere else in the library -- a homebrew app
downloaded on its own -- is the same thing in its smallest form: a
switch/ folder of one file, `switch/<name>/<name>.nro`, the layout the
Homebrew Menu lists and most apps' own instructions use. Its card is
named after the app itself, from the NACP inside the .nro (nro.py).

Only `switch/` is recognised on purpose: it is the homebrew folder, the one
these releases ship, and copying a release's other top-level files (a
README next to it, say) onto the root of someone's SD card is not something
to do by inference.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable, Optional

from . import config
from . import title_id as title_id_mod
from .model import ContentType

# The SD-card folder an SD_FILES item is copied into, spelled the way every
# CFW SD card already has it. The source may say "Switch/"; the SD card is
# FAT32/exFAT and treats both as the same folder, but a WPD folder lookup
# does not, so the destination is always this exact spelling.
SD_ROOT_DIR = "switch"

# A forwarder is a few hundred KB (both real ones measured: 395 KB and 412
# KB). Anything bigger is read for nothing -- a real game is gigabytes, and
# library indexing deliberately never reads those (see
# scanner._classify_package_file).
FORWARDER_MAX_BYTES = 16 * 1024 * 1024

# nx-hbloader's `nextNroPath`, as it sits in a forwarder's RomFS. The
# loader's own fallback is sdmc:/hbmenu.nro and is not a target.
_LAUNCH_RE = re.compile(rb"sdmc:/([\x21-\x7e][\x20-\x7e]*?\.nro)", re.IGNORECASE)
_FALLBACK_LAUNCH = "hbmenu.nro"

NRO_EXTENSION = ".nro"

# Names that describe the SD-card layout rather than the game: an archive
# called "switch.7z", or a folder called "SD", says nothing about what is
# inside it, so a card never takes its name from one of these.
_STRUCTURAL_NAMES = {"switch", "sd", "sdcard", "sd card", "sd_card", "sdmc", "sd-card"}


@dataclass(frozen=True)
class SdSummary:
    """What an SD_FILES item puts on the card. `apps` is every
    `switch/<name>` it writes into -- a folder, or a loose .nro directly in
    switch/ -- and `nro` every launchable file, both as SD-relative paths."""
    apps: tuple[str, ...]
    nro: tuple[str, ...]
    files: int
    size: int

    def to_dict(self) -> dict:
        return {"apps": list(self.apps), "nro": list(self.nro), "files": self.files, "size": self.size}

    @classmethod
    def from_dict(cls, d: dict) -> "SdSummary":
        return cls(apps=tuple(d.get("apps") or ()), nro=tuple(d.get("nro") or ()),
                   files=int(d.get("files") or 0), size=int(d.get("size") or 0))


def summarize(relative_files: Iterable[tuple[str, int]]) -> Optional[SdSummary]:
    """(path relative to the switch/ folder, size) pairs -> SdSummary, or
    None when there is not a single file to copy."""
    apps: set[str] = set()
    nro: set[str] = set()
    files = 0
    size = 0
    for rel, file_size in relative_files:
        parts = [p for p in rel.replace("\\", "/").split("/") if p not in ("", ".")]
        if not parts:
            continue
        files += 1
        size += max(0, file_size)
        apps.add(f"{SD_ROOT_DIR}/{parts[0]}")
        if parts[-1].lower().endswith(".nro"):
            nro.add(f"{SD_ROOT_DIR}/" + "/".join(parts))
    if not files:
        return None
    return SdSummary(apps=tuple(sorted(apps)), nro=tuple(sorted(nro)), files=files, size=size)


def loose_nro_relative(filename: str) -> str:
    """Where a lone .nro goes, relative to switch/: its own folder."""
    return f"{PurePosixPath(filename).stem}/{filename}"


def source_files(source: Path) -> list[tuple[Path, str]]:
    """(local file, path relative to switch/) for everything an SD_FILES
    source puts on the card: every file of a switch/ folder, or a lone
    .nro placed by loose_nro_relative."""
    if source.is_file():
        return [(source, loose_nro_relative(source.name))]
    return [
        (f, f.relative_to(source).as_posix())
        for f in sorted((p for p in source.rglob("*") if p.is_file()), key=lambda p: p.as_posix())
    ]


def destination_for(relative_path: str) -> str:
    """Where one file of an SD_FILES item goes, relative to the SD card's
    root: always under switch/, whatever the source's own spelling was."""
    parts = [p for p in relative_path.replace("\\", "/").split("/") if p not in ("", ".")]
    return "/".join([SD_ROOT_DIR, *parts])


# ---------------------------------------------------------------------------
# Archives: found from the listing alone, never by extracting
# ---------------------------------------------------------------------------

# How deep inside an archive its switch/ folder may sit: at the top
# ("switch/...", how both real releases ship it) or under one wrapper folder
# ("Zuma v1.2/switch/..."). A folder called "Switch" buried deeper is some
# game's own data layout (Data/Switch/textures/...), not an SD card.
_MAX_ARCHIVE_SD_ROOT_DEPTH = 2


def archive_sd_root(entries) -> Optional[tuple[str, ...]]:
    """The path parts of the one `switch/` folder inside an archive listing
    (`("switch",)` for switch.7z, `("Pack v2", "switch")` for a wrapped
    one), or None. Two different switch/ folders in one archive are two
    different layouts, and picking one of them would be guessing. One
    holding an installable package is a download folder that happens to be
    called Switch, and one inside atmosphere/ belongs to a mod."""
    roots: set[tuple[str, ...]] = set()
    for entry in entries:
        if entry.is_dir:
            continue
        parts = [p for p in entry.name.replace("\\", "/").split("/") if p not in ("", ".")]
        for index, part in enumerate(parts[:-1]):
            lowered = part.lower()
            if lowered == "atmosphere":
                break
            if lowered == SD_ROOT_DIR:
                if index + 1 > _MAX_ARCHIVE_SD_ROOT_DEPTH:
                    break
                if PurePosixPath(parts[-1]).suffix.lower() in config.PACKAGE_EXTENSIONS:
                    return None
                roots.add(tuple(parts[:index + 1]))
                break
    return roots.pop() if len(roots) == 1 else None


def archive_summary(entries, root: tuple[str, ...]) -> Optional[SdSummary]:
    depth = len(root)

    def below_root():
        for entry in entries:
            if entry.is_dir:
                continue
            parts = [p for p in entry.name.replace("\\", "/").split("/") if p not in ("", ".")]
            if len(parts) > depth and tuple(parts[:depth]) == root:
                yield "/".join(parts[depth:]), entry.size

    return summarize(below_root())


# ---------------------------------------------------------------------------
# Folders already on disk
# ---------------------------------------------------------------------------

def folder_summary(folder: Path) -> Optional[SdSummary]:
    """SdSummary for a `switch` folder found in the library, or None when
    it is not one to copy: empty, or holding anything the library installs
    another way. A folder named "Switch" full of .nsp files is somebody's
    way of sorting their downloads, not an SD card layout, and treating it
    as one would put whole games onto the card as loose files."""
    files: list[tuple[str, int]] = []
    for path in folder.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() in config.ALL_TRACKED_EXTENSIONS:
            return None
        files.append((path.relative_to(folder).as_posix(), path.stat().st_size))
    return summarize(files)


def find_sd_folders(candidates: Iterable[Path], *, exclude: Iterable[Path] = ()) -> list[Path]:
    """The top-most `switch` folders among `candidates` that hold something
    to copy. A switch folder nested inside an accepted one is part of it; a
    rejected one (e.g. a download category full of packages) is not a
    reason to ignore an SD folder further down inside it, so those are
    still looked at. Nothing overlapping an atmosphere mod folder counts --
    that tree already belongs to the mod."""
    excluded = list(exclude)
    accepted: list[Path] = []
    for folder in sorted(set(candidates), key=lambda p: (len(p.parts), str(p))):
        if any(folder.is_relative_to(done) for done in accepted):
            continue
        if any(folder.is_relative_to(mod) or mod.is_relative_to(folder) for mod in excluded):
            continue
        if folder_summary(folder) is not None:
            accepted.append(folder)
    return accepted


# ---------------------------------------------------------------------------
# Forwarders
# ---------------------------------------------------------------------------

def forwarder_launch_path(path: Path) -> Optional[str]:
    """The SD-card path a forwarder package launches ("switch/zumaportable/
    dbc17o.nro"), or None. Read straight out of the package bytes, where
    nx-hbloader's nextNroPath sits in plain text (both real forwarders in
    the library, 2026-09-23). A package whose RomFS is encrypted simply
    yields nothing -- the folder rule still applies to it."""
    try:
        if path.stat().st_size > FORWARDER_MAX_BYTES:
            return None
        data = path.read_bytes()
    except OSError:
        return None
    for match in _LAUNCH_RE.finditer(data):
        target = match.group(1).decode("ascii")
        if target.lower() != _FALLBACK_LAUNCH:
            return target
    return None


def app_of(sd_path: str) -> str:
    """"switch/zumaportable/dbc17o.nro" -> "switch/zumaportable"; a loose
    "switch/foo.nro" is its own app."""
    parts = [p for p in sd_path.split("/") if p]
    return "/".join(parts[:2])


# ---------------------------------------------------------------------------
# library_items.details_json
# ---------------------------------------------------------------------------

def details(
    *, sd: Optional[SdSummary] = None, launches: Optional[str] = None, app_name: Optional[str] = None,
) -> dict:
    """The part of a row's details_json this module owns (the scanner adds
    its own keys around it -- see scanner._details_json)."""
    data = {}
    if sd is not None:
        data["sd_files"] = sd.to_dict()
    if launches:
        data["launches"] = launches
    if app_name:
        data["app_name"] = app_name
    return data


def row_details(row) -> dict:
    try:
        raw = row["details_json"]
    except (IndexError, KeyError):
        return {}
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def sd_summary_of(row) -> Optional[SdSummary]:
    data = row_details(row).get("sd_files")
    return SdSummary.from_dict(data) if isinstance(data, dict) else None


def row_display_name(row) -> str:
    """display_name() for a library row: a lone .nro's own app name when
    its NACP had one."""
    app_name = row_details(row).get("app_name")
    if isinstance(app_name, str) and app_name:
        return app_name
    return display_name(row["absolute_path"])


def launches_of(row) -> Optional[str]:
    value = row_details(row).get("launches")
    return value if isinstance(value, str) and value else None


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------

def _meaningful(name: str) -> bool:
    return bool(name.strip(":\\/ ")) and name.strip().lower() not in _STRUCTURAL_NAMES


def display_name(absolute_path: str) -> str:
    """A human name for an SD_FILES item that stands on its own card: its
    own name, unless that only describes the SD layout ("switch.7z", a
    folder called "switch"), in which case the nearest folder above it that
    means something -- the release folder, as a rule."""
    path = Path(absolute_path)
    stem_kinds = config.ARCHIVE_EXTENSIONS | {NRO_EXTENSION}
    own = path.stem if path.suffix.lower() in stem_kinds else path.name
    if _meaningful(own):
        return own
    for parent in path.parents:
        if _meaningful(parent.name):
            return parent.name
    return own


def part_label(absolute_path: str) -> str:
    """How one SD_FILES item reads inside its game's card, where the game's
    name is already on screen: the file you would find on disk. An archive
    is its filename; a folder is "<the folder it sits in>/switch"."""
    path = Path(absolute_path)
    if path.suffix.lower() in config.ARCHIVE_EXTENSIONS or not path.parent.name:
        return path.name
    return f"{path.parent.name}/{path.name}"


# ---------------------------------------------------------------------------
# Which game an SD_FILES item belongs to
# ---------------------------------------------------------------------------

def _family(title_id_value: Optional[str]) -> Optional[str]:
    if not title_id_value:
        return None
    try:
        return title_id_mod.classify_title_variant(title_id_value).base_title_id
    except (ValueError, TypeError):
        return None


def assign_owners(rows) -> dict[int, tuple[Optional[str], Optional[str]]]:
    """{row id: (family TITLE_ID or None, how it was decided)} for every
    SD_FILES row among `rows` -- see this module's docstring for the three
    rules, applied in order. `rows` should be the library as it is now
    (retired rows excluded): an owner has to be something you could
    actually install alongside."""
    families_below: dict[str, set[str]] = {}
    families_at_top: dict[str, set[str]] = {}
    launched_by: dict[str, set[str]] = {}
    for row in rows:
        if row["content_type"] != ContentType.GAME_PACKAGE.value:
            continue
        family = _family(row["title_id"])
        if family is None:
            continue
        families_at_top.setdefault(str(Path(row["absolute_path"]).parent), set()).add(family)
        for parent in Path(row["absolute_path"]).parents:
            families_below.setdefault(str(parent), set()).add(family)
        launches = launches_of(row)
        if launches:
            launched_by.setdefault(app_of(launches).lower(), set()).add(family)

    owners: dict[int, tuple[Optional[str], Optional[str]]] = {}
    for row in rows:
        if row["content_type"] != ContentType.SD_FILES.value:
            continue
        if row["title_id_source"] == "filename" and _family(row["title_id"]):
            owners[row["id"]] = (row["title_id"], "filename")
            continue

        summary = sd_summary_of(row)
        by_forwarder: set[str] = set()
        for app in (summary.apps if summary else ()):
            by_forwarder |= launched_by.get(app.lower(), set())
        if len(by_forwarder) == 1:
            owners[row["id"]] = (next(iter(by_forwarder)), "forwarder")
            continue

        owner, how = None, None
        for parent in Path(row["absolute_path"]).parents:
            found = families_below.get(str(parent))
            if found:
                if len(found) == 1:
                    owner, how = next(iter(found)), "folder"
                else:
                    # A release holding more than one game (Animal
                    # Crossing, with its Island Transfer Tool in a
                    # sub-folder): the one whose package lies right in the
                    # release folder is the release's game. Several there:
                    # nobody. Such a part is the game's, but not needed by
                    # it -- "release": ticked by hand, never with the game.
                    top = families_at_top.get(str(parent)) or set()
                    if len(top) == 1:
                        owner, how = next(iter(top)), "release"
                break
        owners[row["id"]] = (owner, how)
    return owners


# A forwarder whose package gives nothing away (field report, 2026-09-28:
# Need for Speed: Most Wanted's port -- no [TITLE_ID] in its name, every NCA
# encrypted, so neither its TITLE_ID nor the .nro it launches can be read
# without the console's keys). Its release folder still says what it is:
#
#   Need-For-Speed-Most-Wanted/
#     Need-for-Speed-Most-Wanted.nsp   326 KB -- the forwarder
#     switch/nfsmw-nx/...              5.9 GB -- what it launches
#
# One forwarder-sized package with no TITLE_ID, a switch/ folder (or archive)
# no game claims, side by side in the same folder, and no other game there:
# that is a port, one card, installed together. Anything less clear-cut --
# two such packages, a real game beside them -- stays as it was.
SEALED_FORWARDER = "sealed_forwarder"
PORT_PART = "port"
# assign_owners' answer for a part that came with a game's release without
# being needed by it (see its own comment).
RELEASE_PART = "release"


def sealed_forwarders(rows, owners: dict[int, tuple[Optional[str], Optional[str]]]) -> tuple[set[int], set[int]]:
    """(package row ids that are a port's sealed forwarder, SD_FILES row ids
    that are that port's files) -- see SEALED_FORWARDER. `owners` is
    assign_owners' answer: an SD part some game already claims is that
    game's, not a port's."""
    candidates: dict[str, list[int]] = {}
    games_in: set[str] = set()
    parts_in: dict[str, list[int]] = {}
    for row in rows:
        folder = str(Path(row["absolute_path"]).parent)
        if row["content_type"] == ContentType.GAME_PACKAGE.value:
            if _family(row["title_id"]):
                games_in.add(folder)
            elif ((row["size"] or 0) <= FORWARDER_MAX_BYTES
                    and row["status"] in ("NEEDS_REVIEW", "AVAILABLE")
                    and row["title_id_source"] in (None, SEALED_FORWARDER)):
                candidates.setdefault(folder, []).append(row["id"])
        elif row["content_type"] == ContentType.SD_FILES.value:
            if owners.get(row["id"], (None, None))[0] is None:
                parts_in.setdefault(folder, []).append(row["id"])
    forwarders: set[int] = set()
    parts: set[int] = set()
    for folder, ids in candidates.items():
        if len(ids) == 1 and folder in parts_in and folder not in games_in:
            forwarders.add(ids[0])
            parts.update(parts_in[folder])
    return forwarders, parts


def provider_of(launches: str, parts: Iterable[tuple[str, Optional[SdSummary]]]) -> Optional[str]:
    """The label of the first part whose files include `launches`, or None.
    Compared case-insensitively, because the SD card's own filesystem is."""
    wanted = launches.lower()
    for label, summary in parts:
        if summary is not None and any(n.lower() == wanted for n in summary.nro):
            return label
    return None
