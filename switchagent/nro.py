"""What a homebrew application (.nro) calls itself.

The Homebrew Menu shows each app by the name, author and version in the
NACP embedded in the .nro -- a filename like `app-v2.1.0-release.nro` is a
download artefact, not a name. Read here so a lone .nro in the library
(see sd_files.py) gets the same name on its card.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class NroInfo:
    name: Optional[str]
    author: Optional[str]
    version: Optional[str]


# NroHeader (switchbrew.org/wiki/NRO): "NRO0" at 0x10, total size at 0x18.
# The homebrew asset section starts right after, at file offset `size`:
# "ASET", version, then three (offset, size) u64 pairs -- icon, NACP, romfs
# -- relative to the asset header itself.
_NRO_MAGIC_OFFSET = 0x10
_NRO_SIZE_OFFSET = 0x18
_ASET_ICON_OFFSET = 0x08
_ASET_NACP_OFFSET = 0x18
# The icon the Homebrew Menu shows: a JPEG, 256x256 by convention.
_ICON_LIMIT = 2 * 1024 * 1024
# NACP: 16 language entries of 0x300 bytes (name 0x200, author 0x100);
# DisplayVersion is 0x10 bytes at 0x3060.
_NACP_LANG_ENTRY = 0x300
_NACP_LANGUAGES = 16
_NACP_NAME_SIZE = 0x200
_NACP_AUTHOR_SIZE = 0x100
_NACP_VERSION_OFFSET = 0x3060
_NACP_VERSION_SIZE = 0x10


def _c_string(raw: bytes) -> Optional[str]:
    text = raw.split(b"\x00", 1)[0].decode("utf-8", errors="replace").strip()
    return text or None


def read_nro_info(path: Path) -> Optional[NroInfo]:
    """Name/author/version from the NRO's embedded NACP. None when the file
    is not an NRO at all; an NroInfo of all-None fields when it is one but
    carries no metadata (nothing to show, but still a valid app). Never
    raises for a malformed file -- a scan must not fail on one. A Tesla
    overlay (.ovl) is an NRO too, and reads the same way."""
    try:
        with path.open("rb") as f:
            return _read_info(f.read, f.seek)
    except OSError:
        return None


def read_nro_icon(path: Path) -> Optional[bytes]:
    """The JPEG icon embedded in the NRO's asset section -- the picture the
    Homebrew Menu shows for it. None when there is none, or the file is not
    an NRO; never raises for a malformed file."""
    try:
        with path.open("rb") as f:
            header = f.read(0x80)
            if len(header) < 0x20 or header[_NRO_MAGIC_OFFSET:_NRO_MAGIC_OFFSET + 4] != b"NRO0":
                return None
            (nro_size,) = struct.unpack_from("<I", header, _NRO_SIZE_OFFSET)
            f.seek(nro_size)
            aset = f.read(0x38)
            if len(aset) < 0x38 or aset[:4] != b"ASET":
                return None
            icon_offset, icon_size = struct.unpack_from("<QQ", aset, _ASET_ICON_OFFSET)
            if not 0 < icon_size <= _ICON_LIMIT:
                return None
            f.seek(nro_size + icon_offset)
            icon = f.read(icon_size)
    except OSError:
        return None
    return icon if len(icon) == icon_size and icon.startswith(b"\xff\xd8\xff") else None


def nro_info_from_bytes(data: bytes) -> Optional[NroInfo]:
    """read_nro_info() for a file already in memory -- one read back off a
    console, say."""
    import io

    stream = io.BytesIO(data)
    return _read_info(stream.read, stream.seek)


def _read_info(read, seek) -> Optional[NroInfo]:
    header = read(0x80)
    if len(header) < 0x20 or header[_NRO_MAGIC_OFFSET:_NRO_MAGIC_OFFSET + 4] != b"NRO0":
        return None
    (nro_size,) = struct.unpack_from("<I", header, _NRO_SIZE_OFFSET)
    seek(nro_size)
    aset = read(0x38)
    if len(aset) < 0x38 or aset[:4] != b"ASET":
        return NroInfo(None, None, None)
    nacp_offset, nacp_size = struct.unpack_from("<QQ", aset, _ASET_NACP_OFFSET)
    if nacp_size < _NACP_VERSION_OFFSET + _NACP_VERSION_SIZE:
        return NroInfo(None, None, None)
    seek(nro_size + nacp_offset)
    nacp = read(_NACP_VERSION_OFFSET + _NACP_VERSION_SIZE)
    if len(nacp) < _NACP_VERSION_OFFSET + _NACP_VERSION_SIZE:
        return NroInfo(None, None, None)

    name = author = None
    # First language with a name set -- AmericanEnglish is entry 0, and
    # most homebrew fills every entry with the same text anyway.
    for i in range(_NACP_LANGUAGES):
        entry = nacp[i * _NACP_LANG_ENTRY:(i + 1) * _NACP_LANG_ENTRY]
        name = _c_string(entry[:_NACP_NAME_SIZE])
        if name:
            author = _c_string(entry[_NACP_NAME_SIZE:_NACP_NAME_SIZE + _NACP_AUTHOR_SIZE])
            break
    version = _c_string(nacp[_NACP_VERSION_OFFSET:_NACP_VERSION_OFFSET + _NACP_VERSION_SIZE])
    return NroInfo(name, author, version)
