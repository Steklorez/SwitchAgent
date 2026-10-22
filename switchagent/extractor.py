"""Safe archive inspection and extraction.

Two clearly separate operations:

  - peek_archive() / classify_entries() -- read an archive's own directory
    listing (filenames, declared sizes, CRC32). Never writes anything to
    disk, never decompresses entry bytes. Used for classification and for
    conflict-preview against mock_switch without paying for a real
    extraction.

  - safe_extract() -- actually decompresses an archive into
    work/<job-id>/, and only there. Every entry name is validated BEFORE
    anything is written (Zip Slip / absolute path / UNC / symlink / depth),
    and size + file-count limits from config.yaml are enforced both from
    declared metadata up front and, for ZIP, again against real bytes
    written while streaming (declared sizes in a hand-crafted archive are
    not something we fully trust). Nothing extracted here is ever executed.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import uuid
import zipfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Optional

from . import addons, config, emuiibo, sd_files, title_id
from .model import ArchiveEntry, ConflictEntry, ConflictState, ContentType


class ArchiveError(Exception):
    """Base class for every extractor-raised error."""


class CorruptArchiveError(ArchiveError):
    pass


class UnsupportedArchiveError(ArchiveError):
    pass


class ExtractionBackendMissingError(ArchiveError):
    """Raised when the archive's own listing works (pure Python) but actual
    decompression needs an external tool that isn't installed (this is
    exactly rarfile's situation without unrar/7z/bsdtar on PATH)."""


class ZipSlipError(ArchiveError):
    """Raised for any entry that would land outside the destination
    directory, or that tries to become a symlink/absolute path/UNC path."""


class ExtractionLimitError(ArchiveError):
    """Raised when an archive exceeds config.yaml's declared or observed
    size/file-count/depth limits."""


# ---------------------------------------------------------------------------
# Listing (peek) -- read-only, no extraction
# ---------------------------------------------------------------------------

def list_zip_entries(path: Path) -> list[ArchiveEntry]:
    try:
        with zipfile.ZipFile(path) as zf:
            out = []
            for info in zf.infolist():
                # ZIP stores unix permission bits (incl. S_IFLNK) in the top
                # 16 bits of external_attr, but only when written by a
                # unix-aware tool -- 0 on most Windows-made zips, which is
                # fine, it just means "not a symlink" for those.
                mode = (info.external_attr >> 16) & 0xFFFF
                is_symlink = (mode & 0xF000) == 0xA000  # S_IFLNK
                out.append(ArchiveEntry(
                    name=info.filename.replace("\\", "/"),
                    size=info.file_size,
                    is_dir=info.is_dir(),
                    is_symlink=is_symlink,
                    crc32=info.CRC,
                ))
            return out
    except (zipfile.BadZipFile, OSError) as exc:
        raise CorruptArchiveError(str(exc)) from exc


def list_7z_entries(path: Path) -> list[ArchiveEntry]:
    import py7zr
    import py7zr.exceptions

    try:
        with py7zr.SevenZipFile(path, mode="r") as z:
            out = []
            for info in z.list():
                out.append(ArchiveEntry(
                    name=info.filename.replace("\\", "/"),
                    size=info.uncompressed or 0,
                    is_dir=info.is_directory,
                    is_symlink=info.is_symlink,
                    crc32=info.crc32,
                ))
            return out
    except py7zr.exceptions.Bad7zFile as exc:
        raise CorruptArchiveError(str(exc)) from exc


def list_rar_entries(path: Path) -> list[ArchiveEntry]:
    import rarfile

    try:
        with rarfile.RarFile(path) as rf:
            out = []
            for info in rf.infolist():
                out.append(ArchiveEntry(
                    name=info.filename.replace("\\", "/"),
                    size=info.file_size,
                    is_dir=info.is_dir(),
                    is_symlink=bool(info.is_symlink()),
                    crc32=info.CRC,
                ))
            return out
    except rarfile.RarExecError as exc:
        # Listing normally doesn't need the external tool (rarfile parses
        # RAR headers in pure Python) -- but if some archive feature ever
        # does trigger a tool call during listing, report it as a missing
        # backend, not a corrupt archive.
        raise ExtractionBackendMissingError(str(exc)) from exc
    except rarfile.Error as exc:
        # Covers BadRarFile / NotRarFile / NeedFirstVolume / PasswordRequired
        # etc. -- anything that means "can't even list this archive's
        # contents".
        raise CorruptArchiveError(str(exc)) from exc


_LISTERS = {
    ".zip": list_zip_entries,
    ".7z": list_7z_entries,
    ".rar": list_rar_entries,
}


def list_archive_entries(path: Path) -> list[ArchiveEntry]:
    ext = path.suffix.lower()
    lister = _LISTERS.get(ext)
    if lister is None:
        raise UnsupportedArchiveError(f"unsupported archive extension: {ext}")
    return lister(path)


# Reading one member means decompressing up to it in a solid 7z; past this
# size an archive is not worth that just for a few bytes of metadata.
READ_MEMBER_MAX_ARCHIVE_BYTES = 256 * 1024 * 1024


def read_archive_member(path: Path, name: str, *, max_bytes: int) -> Optional[bytes]:
    """The bytes of ONE file inside an archive (a 7z member passes through a
    throwaway temp folder, never work/) -- or None when that is not possible cheaply (RAR, a member larger
    than max_bytes, an archive too big to be worth decompressing into).
    Read-only, for small metadata such as an overlay's version; installing
    still always goes through safe_extract()."""
    parts = PurePosixPath(name).parts
    if not parts or any(p in ("..", "/") or ":" in p for p in parts):
        return None
    try:
        if path.stat().st_size > READ_MEMBER_MAX_ARCHIVE_BYTES:
            return None
        ext = path.suffix.lower()
        if ext == ".zip":
            with zipfile.ZipFile(path) as zf:
                info = zf.getinfo(name)
                if info.file_size > max_bytes:
                    return None
                return zf.read(info)
        if ext == ".7z":
            import tempfile

            import py7zr

            with py7zr.SevenZipFile(path, mode="r") as z:
                sizes = {i.filename.replace("\\", "/"): i.uncompressed or 0 for i in z.list()}
                if sizes.get(name, max_bytes + 1) > max_bytes:
                    return None
            with tempfile.TemporaryDirectory() as tmp:
                with py7zr.SevenZipFile(path, mode="r") as z:
                    z.extract(path=tmp, targets=[name])
                target = Path(tmp).joinpath(*PurePosixPath(name).parts)
                return target.read_bytes() if target.is_file() else None
    except Exception:  # noqa: BLE001 -- metadata only: any failure is just "not available"
        return None
    return None


def read_archive_members(path: Path, names: list[str], *, max_total_bytes: int) -> dict[str, bytes]:
    """read_archive_member() for many small files at once (a 7z is
    decompressed once, not once per file). Whatever cannot be read is simply
    absent from the result; never raises."""
    wanted = [n for n in dict.fromkeys(names)
              if PurePosixPath(n).parts and not any(p in ("..", "/") or ":" in p for p in PurePosixPath(n).parts)]
    out: dict[str, bytes] = {}
    try:
        if not wanted or path.stat().st_size > READ_MEMBER_MAX_ARCHIVE_BYTES:
            return out
        ext = path.suffix.lower()
        if ext == ".zip":
            with zipfile.ZipFile(path) as zf:
                infos = {i.filename.replace("\\", "/"): i for i in zf.infolist()}
                if sum(infos[n].file_size for n in wanted if n in infos) > max_total_bytes:
                    return out
                for name in wanted:
                    if name in infos:
                        out[name] = zf.read(infos[name])
        elif ext == ".7z":
            import tempfile

            import py7zr

            with py7zr.SevenZipFile(path, mode="r") as z:
                sizes = {i.filename.replace("\\", "/"): i.uncompressed or 0 for i in z.list()}
            present = [n for n in wanted if n in sizes]
            if not present or sum(sizes[n] for n in present) > max_total_bytes:
                return out
            try:
                # In memory: 766 amiibo.json in 0.3s, where writing each to a
                # temp file first took 4.7s.
                from py7zr.io import BytesIOFactory
            except ImportError:
                BytesIOFactory = None
            if BytesIOFactory is not None:
                factory = BytesIOFactory(max_total_bytes)
                with py7zr.SevenZipFile(path, mode="r") as z:
                    z.extract(targets=present, factory=factory)
                for name in present:
                    product = factory.products.get(name)
                    if product is not None:
                        product.seek(0)
                        out[name] = product.read()
                return out
            with tempfile.TemporaryDirectory() as tmp:
                with py7zr.SevenZipFile(path, mode="r") as z:
                    z.extract(path=tmp, targets=present)
                for name in present:
                    target = Path(tmp).joinpath(*PurePosixPath(name).parts)
                    if target.is_file():
                        out[name] = target.read_bytes()
    except Exception:  # noqa: BLE001 -- metadata only: any failure is just "not available"
        return out
    return out


def hash_entries(entries: list[ArchiveEntry]) -> str:
    """Cheap, format-agnostic content fingerprint: hashes each entry's
    name+size+CRC32 as already declared by the archive's own directory
    listing, instead of decompressing everything just to fingerprint it.
    Good enough to tell 'this is a different archive' across rescans."""
    import hashlib

    h = hashlib.sha256()
    for entry in sorted(entries, key=lambda e: e.name):
        h.update(entry.name.encode("utf-8", "surrogateescape"))
        h.update(str(entry.size).encode())
        h.update(str(entry.crc32 or 0).encode())
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Classification from a listing -- still no extraction
# ---------------------------------------------------------------------------

@dataclass
class PackageArchiveEntry:
    """One installable package (NSP/NSZ/XCI/XCZ) found inside an archive --
    classify_entries() collects EVERY one of these, not just the first (see
    ClassifiedArchive.package_entries below for why the first-only fields
    still exist alongside this)."""
    name: str  # archive-relative path, exactly as declared by the archive's own listing
    format: str
    title_id_guess: title_id.TitleIdGuess
    size: int


@dataclass
class ClassifiedArchive:
    content_type: ContentType
    package_format: Optional[str]
    package_entry_name: Optional[str]
    package_title_id_guess: title_id.TitleIdGuess
    atmosphere_title_id_guess: title_id.TitleIdGuess
    atmosphere_root: Optional[str]  # e.g. "atmosphere/contents/0100.../" prefix
    # Every installable package entry found in the archive, in listing
    # order (package_entry_name/package_format/package_title_id_guess above
    # are always this list's first element, kept as their own fields so
    # every existing single-package caller keeps working unchanged -- see
    # this dataclass's callers in preview.py for the actual multi-entry
    # (Base+Update+DLC) fan-out this enables).
    package_entries: list[PackageArchiveEntry] = field(default_factory=list)
    # The archive's switch/ folder, if it has one (see sd_files.py): the
    # whole of an SD_FILES archive, or the part of a package/mod archive
    # that goes onto the SD card next to what it installs.
    sd_root: Optional[tuple[str, ...]] = None
    sd_summary: Optional[sd_files.SdSummary] = None
    # EMUIIBO: what of the archive is emuiibo, and where each file goes.
    emuiibo_release: Optional[emuiibo.ReleasePlan] = None
    # AMIIBO: every virtual amiibo in the archive, with its destination.
    amiibo: Optional[emuiibo.Collection] = None
    # ADDON: which catalog add-on's release this is, file by file.
    addon: Optional["addons.ReleaseMatch"] = None
    # An archive holding just an .nro (or a few), with no switch/ folder to
    # say where: each is placed like a lone .nro in the library
    # (sd_files.loose_nro_relative). Entry names, in listing order.
    loose_nro: list[str] = field(default_factory=list)


def is_atmosphere_program_file(entry_name: str) -> bool:
    """A file inside atmosphere/contents/<id>/ -- a sysmodule's or a
    game's exefs.nsp included. That .nsp is loaded by Atmosphère from the SD
    card; it is not a package DBI could ever install."""
    return title_id.from_atmosphere_path(entry_name).title_id is not None


def file_list(entries: list[ArchiveEntry]) -> list[tuple[str, int]]:
    return [(e.name, e.size) for e in entries if not e.is_dir]


def classify_entries(entries: list[ArchiveEntry], *, archive_name: Optional[str] = None) -> ClassifiedArchive:
    """`archive_name` (the archive's file name) lets a collection packed as
    one folder named like the archive be told apart from a group inside it
    (see emuiibo.find_collection)."""
    package_entry_name = None
    package_format = None
    package_title_id_guess = title_id.TitleIdGuess(None, None, False)
    package_entries: list[PackageArchiveEntry] = []
    atmosphere_title_id_guess = title_id.TitleIdGuess(None, None, False)
    atmosphere_root = None

    # emuiibo's own release is recognised before anything else: it is a
    # sysmodule under atmosphere/contents/ plus an overlay in switch/, which
    # the rules below would otherwise split into "a mod" and "SD files" of a
    # program id that is no game at all.
    release = emuiibo.plan_release(file_list(entries))
    if release is not None and release.files:
        return ClassifiedArchive(
            content_type=ContentType.EMUIIBO, package_format=None, package_entry_name=None,
            package_title_id_guess=package_title_id_guess, atmosphere_title_id_guess=atmosphere_title_id_guess,
            atmosphere_root=None, emuiibo_release=release,
        )
    # So is an Add-ons catalog utility's release (Ultrahand's sdout.zip,
    # SaltyNX...): only the places its catalog entry names, never "a mod"
    # of a program id that is no game.
    addon = addons.match_archive(file_list(entries))
    if addon is not None:
        return ClassifiedArchive(
            content_type=ContentType.ADDON, package_format=None, package_entry_name=None,
            package_title_id_guess=package_title_id_guess, atmosphere_title_id_guess=atmosphere_title_id_guess,
            atmosphere_root=None, addon=addon,
        )

    for entry in entries:
        if entry.is_dir:
            continue
        ext = PurePosixPath(entry.name).suffix.lower()
        if ext in config.PACKAGE_EXTENSIONS and not is_atmosphere_program_file(entry.name):
            guess = title_id.from_filename(PurePosixPath(entry.name).name)
            package_entries.append(PackageArchiveEntry(
                name=entry.name, format=ext.lstrip(".").upper(),
                title_id_guess=guess, size=entry.size,
            ))
            if package_entry_name is None:
                package_entry_name = entry.name
                package_format = ext.lstrip(".").upper()
                package_title_id_guess = guess

        if atmosphere_root is None:
            guess = title_id.from_atmosphere_path(entry.name)
            if guess.title_id is not None:
                atmosphere_title_id_guess = guess
                atmosphere_root = title_id.atmosphere_mod_root(entry.name)

    has_package = package_entry_name is not None
    has_atmosphere = atmosphere_root is not None
    sd_root = sd_files.archive_sd_root(entries)
    sd_summary = sd_files.archive_summary(entries, sd_root) if sd_root is not None else None
    if sd_summary is None:
        sd_root = None

    amiibo = None
    if has_package and has_atmosphere:
        content_type = ContentType.MIXED
    elif has_package:
        content_type = ContentType.GAME_PACKAGE
    elif has_atmosphere:
        content_type = ContentType.ATMOSPHERE_MOD
    else:
        wrapper = PurePosixPath(archive_name).stem if archive_name else None
        amiibo = emuiibo.find_collection(file_list(entries), wrapper_name=wrapper)
        if amiibo is not None:
            # A switch/ folder next to the amiibo still goes along (see
            # web/services._sd_sub_report), exactly as it would with a mod.
            content_type = ContentType.AMIIBO
        elif sd_root is not None:
            content_type = ContentType.SD_FILES
        else:
            content_type = ContentType.UNKNOWN

    loose_nro: list[str] = []
    if content_type is ContentType.UNKNOWN:
        nro_entries = [
            e for e in entries
            if not e.is_dir and PurePosixPath(e.name).suffix.lower() == sd_files.NRO_EXTENSION
        ]
        placed = [(sd_files.loose_nro_relative(PurePosixPath(e.name).name), e.size) for e in nro_entries]
        # Not loose: an .nro inside a switch/ folder that archive_sd_root
        # declined (two different layouts, say) -- re-placing it would pick
        # one for the user. Nor two .nro of the same name, which would land
        # on one path. Either way which one is meant is not guessed.
        in_switch = any(
            part.lower() == sd_files.SD_ROOT_DIR
            for e in nro_entries for part in PurePosixPath(e.name).parts[:-1]
        )
        if nro_entries and not in_switch and len({rel.lower() for rel, _ in placed}) == len(placed):
            loose_nro = [e.name for e in nro_entries]
            sd_summary = sd_files.summarize(placed)
            content_type = ContentType.SD_FILES

    return ClassifiedArchive(
        content_type=content_type,
        package_format=package_format,
        package_entry_name=package_entry_name,
        package_title_id_guess=package_title_id_guess,
        atmosphere_title_id_guess=atmosphere_title_id_guess,
        atmosphere_root=atmosphere_root,
        package_entries=package_entries,
        sd_root=sd_root,
        sd_summary=sd_summary,
        amiibo=amiibo if content_type is ContentType.AMIIBO else None,
        loose_nro=loose_nro,
    )


# ---------------------------------------------------------------------------
# Path safety
# ---------------------------------------------------------------------------

def safe_relative_path(entry_name: str, dest_root: Path) -> Path:
    """Resolves an archive entry name against dest_root and guarantees the
    result cannot land outside it. Raises ZipSlipError for anything
    suspicious: NUL bytes, UNC-style prefixes, absolute paths (POSIX or
    Windows-drive style), and literal '..' path segments. The final
    belt-and-braces check is a resolved-path containment test, which catches
    any traversal trick not covered by the earlier, more readable checks."""
    if "\x00" in entry_name:
        raise ZipSlipError(f"NUL byte in entry name: {entry_name!r}")

    normalized = entry_name.replace("\\", "/")

    if normalized.startswith("//") or normalized.startswith("\\\\"):
        raise ZipSlipError(f"UNC-style path rejected: {entry_name!r}")

    if PureWindowsPath(normalized).drive or PurePosixPath(normalized).is_absolute():
        raise ZipSlipError(f"absolute path rejected: {entry_name!r}")

    parts = [p for p in normalized.split("/") if p not in ("", ".")]
    if not parts:
        raise ZipSlipError(f"empty entry name: {entry_name!r}")
    if any(p == ".." for p in parts):
        raise ZipSlipError(f"path traversal ('..') rejected: {entry_name!r}")

    dest_root_resolved = dest_root.resolve()
    target = (dest_root_resolved / Path(*parts)).resolve()
    if not target.is_relative_to(dest_root_resolved):
        raise ZipSlipError(f"entry escapes destination directory: {entry_name!r}")
    return target


def entry_depth(entry_name: str) -> int:
    normalized = entry_name.replace("\\", "/")
    return len([p for p in normalized.split("/") if p not in ("", ".")])


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

@dataclass
class ExtractResult:
    dest_root: Path
    file_count: int
    total_size: int


def new_job_id() -> str:
    return uuid.uuid4().hex[:16]


def _validate_entries_against_limits(
    entries: list[ArchiveEntry], dest_root: Path, limits: config.ExtractionLimits,
) -> None:
    file_entries = [e for e in entries if not e.is_dir]

    for e in entries:
        if e.is_symlink:
            raise ZipSlipError(f"symlink entries are not allowed: {e.name!r}")

    if len(file_entries) > limits.max_file_count:
        raise ExtractionLimitError(
            f"too many files in archive: {len(file_entries)} > {limits.max_file_count}"
        )

    total_declared_size = sum(e.size for e in file_entries)
    if total_declared_size > limits.max_extracted_size_bytes:
        raise ExtractionLimitError(
            f"declared extracted size too large: {total_declared_size} > "
            f"{limits.max_extracted_size_bytes}"
        )

    for e in file_entries:
        depth = entry_depth(e.name)
        if depth > limits.max_directory_depth:
            raise ExtractionLimitError(
                f"entry path too deep ({depth} > {limits.max_directory_depth}): {e.name!r}"
            )
        # Also validates absolute/UNC/traversal/escape for every entry,
        # BEFORE any byte of the archive is decompressed.
        safe_relative_path(e.name, dest_root)


def _extract_zip_streaming(
    archive_path: Path, dest_root: Path, limits: config.ExtractionLimits,
) -> tuple[int, int]:
    """Writes each entry via a chunked read/write loop, counting real bytes
    written as we go -- catches a declared-size lie mid-stream instead of
    trusting the central directory blindly."""
    total_bytes = 0
    file_count = 0
    with zipfile.ZipFile(archive_path) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            target = safe_relative_path(info.filename, dest_root)
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, target.open("wb") as dst:
                while True:
                    chunk = src.read(1024 * 1024)
                    if not chunk:
                        break
                    total_bytes += len(chunk)
                    if total_bytes > limits.max_extracted_size_bytes:
                        raise ExtractionLimitError(
                            f"extracted size exceeded limit while streaming "
                            f"'{info.filename}': {total_bytes} > {limits.max_extracted_size_bytes}"
                        )
                    dst.write(chunk)
            file_count += 1
    return file_count, total_bytes


def _extract_7z(
    archive_path: Path, dest_root: Path, limits: config.ExtractionLimits, entries: list[ArchiveEntry],
) -> None:
    import py7zr
    import py7zr.exceptions

    try:
        with py7zr.SevenZipFile(
            archive_path, mode="r", max_extract_size=limits.max_extracted_size_bytes,
        ) as z:
            z.extractall(path=dest_root)
    except py7zr.exceptions.DecompressionBombError as exc:
        # py7zr enforces max_extract_size itself, mid-stream, against real
        # decompressed bytes -- the same "don't trust declared sizes" spirit
        # as the ZIP streaming path above, just enforced by the library.
        raise ExtractionLimitError(str(exc)) from exc
    except py7zr.exceptions.UnsupportedCompressionMethodError as exc:
        # A method py7zr cannot decode. Real case (2026-09-23): Mega Man X
        # Regenesis' switch.7z, made by a current 7-Zip, compresses one of
        # its two blocks as LZMA2 + the ARM64 branch filter (method 0x0A,
        # added in 7-Zip 23). py7zr 1.1.3 lists it fine and then fails to
        # unpack it with "Archive is compressed by an unsupported
        # compression algorithm". 7-Zip itself and the tar.exe that ships
        # with Windows (libarchive 3.8.8) both unpack it byte-for-byte, so
        # the archive goes to one of those rather than failing the install.
        _clear_directory(dest_root)
        _extract_7z_with_external_tool(archive_path, dest_root, entries, reason=exc)


def _clear_directory(path: Path) -> None:
    """Drops whatever a failed attempt managed to write before giving up."""
    shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=True)


def external_7z_tools() -> list[tuple[str, str]]:
    """(label, executable) for every tool on this machine that can unpack
    what py7zr cannot, in the order they are tried: 7-Zip (on PATH, or
    where its installer puts it), then Windows' own tar.exe -- libarchive,
    on every Windows 10 1803+ and 11. Only the System32 tar is considered:
    a `tar` on PATH may well be GNU tar (Git for Windows), which cannot
    read 7z at all."""
    candidates = [shutil.which(name) for name in ("7z", "7za", "7zz")]
    for env in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
        base = os.environ.get(env)
        if base:
            candidates.append(os.path.join(base, "7-Zip", "7z.exe"))
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for exe in candidates:
        if exe and os.path.isfile(exe) and os.path.normcase(exe) not in seen:
            seen.add(os.path.normcase(exe))
            found.append(("7-Zip", exe))
    if sys.platform == "win32":
        tar = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "tar.exe")
        if os.path.isfile(tar):
            found.append(("tar", tar))
    return found


def _external_7z_command(label: str, exe: str, archive_path: Path, dest_root: Path) -> list[str]:
    if label == "7-Zip":
        # -y: no prompts; -bd/-bso0/-bsp0: nothing on stdout. stdin is
        # closed by the caller, so a password prompt cannot hang the job.
        return [exe, "x", "-y", "-bd", "-bso0", "-bsp0", f"-o{dest_root}", "--", str(archive_path)]
    return [exe, "-xf", str(archive_path), "-C", str(dest_root)]


def _extract_7z_with_external_tool(archive_path: Path, dest_root: Path, entries: list[ArchiveEntry],
                                   *, reason: Exception) -> None:
    """Unpacks with the first external tool that manages it, then holds the
    result to the archive's OWN listing: every file present, at its
    declared size and CRC32. A tool this module does not control is not
    trusted for anything the listing can check -- and safe_extract() still
    runs its usual post-extraction check (no symlink, nothing outside
    dest_root, limits) after this."""
    tools = external_7z_tools()
    if not tools:
        raise ExtractionBackendMissingError(
            f"{archive_path.name} uses a compression method the built-in 7z extractor cannot unpack "
            f"(7-Zip's ARM64 filter, for one), and neither 7-Zip nor Windows' tar.exe was found to "
            f"unpack it instead -- installing 7-Zip fixes this. ({reason})"
        )
    failures = []
    for label, exe in tools:
        try:
            proc = subprocess.run(
                _external_7z_command(label, exe, archive_path, dest_root),
                stdin=subprocess.DEVNULL, capture_output=True, text=True, errors="replace",
                # The desktop app has no console; without this every call
                # would flash one up on screen.
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as exc:
            failures.append(f"{label}: {exc}")
            _clear_directory(dest_root)
            continue
        if proc.returncode == 0:
            mismatch = _first_listing_mismatch(entries, dest_root)
            if mismatch is None:
                return
            failures.append(f"{label}: {mismatch}")
        else:
            failures.append(f"{label}: exit {proc.returncode}: {(proc.stderr or proc.stdout).strip()[:200]}")
        _clear_directory(dest_root)
    raise ArchiveError(f"could not unpack {archive_path.name}: " + "; ".join(failures))


def _first_listing_mismatch(entries: list[ArchiveEntry], dest_root: Path) -> Optional[str]:
    for entry in entries:
        if entry.is_dir:
            continue
        target = safe_relative_path(entry.name, dest_root)
        if not target.is_file():
            return f"'{entry.name}' was not unpacked"
        size = target.stat().st_size
        if size != entry.size:
            return f"'{entry.name}' unpacked at {size} bytes, the archive says {entry.size}"
        if entry.crc32 is not None:
            crc = 0
            with target.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    crc = zlib.crc32(chunk, crc)
            if crc & 0xFFFFFFFF != entry.crc32:
                return f"'{entry.name}' does not match the archive's CRC32"
    return None


def _bundled_7zip() -> Path | None:
    """The 7z.exe shipped with the app (packaging/vendor/7zip), if present."""
    from . import paths

    root = paths.resource_root()
    exe = (root / "vendor" / "7zip" if paths.is_frozen() else root / "packaging" / "vendor" / "7zip") / "7z.exe"
    return exe if exe.is_file() else None


def _use_bundled_7zip() -> None:
    """Point rarfile at the bundled 7z.exe, so .rar needs no installed tool."""
    import rarfile

    exe = _bundled_7zip()
    if exe is not None:
        rarfile.SEVENZIP_TOOL = str(exe)


def rar_backend_available() -> bool:
    """Whether an external unrar/7z/bsdtar tool usable by `rarfile` for
    real decompression is currently on PATH -- used by the Web UI's
    Settings page (see web/services.get_settings()) to show this honestly
    instead of only discovering it the moment a user tries to install a
    .rar. Does not raise; a probe failure just means "not available"."""
    import rarfile

    _use_bundled_7zip()
    try:
        rarfile.tool_setup()
    except rarfile.Error:
        return False
    return True


def _extract_rar(archive_path: Path, dest_root: Path) -> None:
    import rarfile

    _use_bundled_7zip()
    try:
        with rarfile.RarFile(archive_path) as rf:
            rf.extractall(path=dest_root)
    except rarfile.RarCannotExec as exc:
        raise ExtractionBackendMissingError(
            "RAR extraction needs an external unrar/7z/bsdtar tool on PATH "
            "(rarfile can only parse RAR headers in pure Python, not "
            "decompress) -- none was found on this machine."
        ) from exc
    except rarfile.Error as exc:
        raise ArchiveError(f"RAR extraction failed: {exc}") from exc


def _post_check_extracted_dir(dest_root: Path, limits: config.ExtractionLimits) -> tuple[int, int]:
    """Defense in depth after 7z/RAR extraction (whose libraries don't
    offer the same per-byte streaming control ZIP does here): walk what was
    actually written and confirm nothing escaped dest_root, no symlink was
    created, and totals are within limits. Deletes dest_root and raises if
    violated -- a bomb that briefly touched disk still doesn't get to keep
    the space or the escaped file."""
    dest_resolved = dest_root.resolve()
    total = 0
    count = 0
    for p in dest_root.rglob("*"):
        if p.is_symlink():
            raise ZipSlipError(f"symlink present after extraction: {p}")
        if not p.resolve().is_relative_to(dest_resolved):
            raise ZipSlipError(f"extracted entry escaped destination: {p}")
        if p.is_file():
            count += 1
            total += p.stat().st_size
    if count > limits.max_file_count:
        raise ExtractionLimitError(f"too many files after extraction: {count} > {limits.max_file_count}")
    if total > limits.max_extracted_size_bytes:
        raise ExtractionLimitError(
            f"extracted size too large: {total} > {limits.max_extracted_size_bytes}"
        )
    return count, total


def safe_extract(
    archive_path: Path,
    job_id: str,
    *,
    limits: Optional[config.ExtractionLimits] = None,
    work_root: Optional[Path] = None,
) -> ExtractResult:
    """Extracts archive_path into work_root/job_id/, never anywhere else.
    Validates every entry name before writing anything, enforces size/count/
    depth limits from config.yaml, refuses symlink entries outright, and
    never executes anything it writes. On any failure the partially-written
    destination is removed.

    work_root defaults to config.WORK_DIR, resolved at CALL time rather than
    baked in as a parameter default -- a parameter default is evaluated once
    at import time, which would silently ignore any later
    monkeypatch/override of config.WORK_DIR (tests rely on exactly this)."""
    if limits is None:
        limits = config.load_extraction_limits()
    if work_root is None:
        work_root = config.WORK_DIR

    dest_root = (work_root / job_id).resolve()
    if dest_root.exists():
        raise ArchiveError(f"work dir already exists, refusing to reuse it: {dest_root}")
    work_root.mkdir(parents=True, exist_ok=True)
    dest_root.mkdir(parents=False, exist_ok=False)

    try:
        entries = list_archive_entries(archive_path)
        _validate_entries_against_limits(entries, dest_root, limits)

        ext = archive_path.suffix.lower()
        if ext == ".zip":
            file_count, total_size = _extract_zip_streaming(archive_path, dest_root, limits)
        elif ext == ".7z":
            _extract_7z(archive_path, dest_root, limits, entries)
            file_count, total_size = _post_check_extracted_dir(dest_root, limits)
        elif ext == ".rar":
            _extract_rar(archive_path, dest_root)
            file_count, total_size = _post_check_extracted_dir(dest_root, limits)
        else:
            raise UnsupportedArchiveError(f"unsupported archive extension: {ext}")
    except Exception:
        shutil.rmtree(dest_root, ignore_errors=True)
        raise

    return ExtractResult(dest_root=dest_root, file_count=file_count, total_size=total_size)


# ---------------------------------------------------------------------------
# Conflict preview against the (currently mock) SD card destination
# ---------------------------------------------------------------------------

def compute_conflicts_from_folder(
    source_folder: Path, dest_root: Path,
) -> list[ConflictEntry]:
    """Compares an already-on-disk atmosphere mod folder (either a
    MOD_FOLDER found directly in inbox/, or an archive already extracted to
    work/<job-id>/) against a destination tree, file by file, via SHA-256."""
    import hashlib

    conflicts = []
    for f in sorted(source_folder.rglob("*"), key=lambda p: p.as_posix()):
        if not f.is_file():
            continue
        rel = f.relative_to(source_folder).as_posix()
        dest_file = dest_root / rel
        if not dest_file.exists():
            conflicts.append(ConflictEntry(rel, ConflictState.NEW))
            continue
        src_hash = hashlib.sha256(f.read_bytes()).hexdigest()
        dst_hash = hashlib.sha256(dest_file.read_bytes()).hexdigest()
        state = ConflictState.SAME if src_hash == dst_hash else ConflictState.CONFLICT
        conflicts.append(ConflictEntry(rel, state))
    return conflicts


def compute_conflicts_from_archive_entries(
    entries: list[ArchiveEntry], atmosphere_root: str, dest_root: Path,
) -> list[ConflictEntry]:
    """Same idea, but without extracting: compares each archived atmosphere
    entry's CRC32 (already known from the archive's own directory listing)
    against zlib.crc32 of the corresponding file already on the destination.
    CRC32 isn't a cryptographic hash, but for "is this literally the same
    staged file" -- a low-stakes, non-adversarial comparison against our own
    mock/SD destination -- it's exactly what the archive format already
    gives us for free, without paying for a real extraction just to preview
    conflicts."""
    conflicts = []
    prefix = atmosphere_root
    for entry in entries:
        if entry.is_dir or not entry.name.startswith(prefix):
            continue
        rel = entry.name[len(prefix):]
        if not rel:
            continue
        dest_file = dest_root / rel
        if not dest_file.exists():
            conflicts.append(ConflictEntry(rel, ConflictState.NEW))
            continue
        dst_crc = zlib.crc32(dest_file.read_bytes()) & 0xFFFFFFFF
        state = ConflictState.SAME if entry.crc32 == dst_crc else ConflictState.CONFLICT
        conflicts.append(ConflictEntry(rel, state))
    return conflicts
