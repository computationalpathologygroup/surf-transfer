"""Dependency-free structural check of a TIFF file.

Tells a legitimately flat TIFF (which openslide cannot read, so `unvalidated`)
apart from a damaged one (`corrupt`): header, IFD chain, and every strip/tile
offset+length must lie inside the file.
"""

from __future__ import annotations

import os
import struct
from typing import BinaryIO

_STRIP_OFFSETS, _STRIP_COUNTS, _TILE_OFFSETS, _TILE_COUNTS = 273, 279, 324, 325
_TYPE_SIZES = {
    1: 1,
    2: 1,
    3: 2,
    4: 4,
    5: 8,
    6: 1,
    7: 1,
    8: 2,
    9: 4,
    10: 8,
    11: 4,
    12: 8,
    16: 8,
    17: 8,
    18: 8,
}
_INT_FORMATS = {1: "B", 3: "H", 4: "I", 16: "Q"}
_MAX_IFDS = 100_000


class _Bad(Exception):
    pass


def tiff_structure_error(path: str | os.PathLike[str]) -> str | None:
    """None if the TIFF structure is sound, else a short description of the problem."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            _walk(f, size)
    except _Bad as e:
        return str(e)
    except (OSError, struct.error) as e:
        return f"unreadable: {e}"
    return None


def _read(f: BinaryIO, offset: int, n: int, size: int) -> bytes:
    if offset < 0 or offset + n > size:
        raise _Bad(f"structure points outside the file (offset {offset}, {n} bytes, file {size})")
    f.seek(offset)
    data = f.read(n)
    if len(data) != n:
        raise _Bad("unexpected end of file")
    return data


def _walk(f: BinaryIO, size: int) -> None:
    header = _read(f, 0, 8, size) if size >= 8 else b""
    if len(header) < 8 or header[:2] not in (b"II", b"MM"):
        raise _Bad("not a TIFF (bad header)")
    end = "<" if header[:2] == b"II" else ">"
    magic = struct.unpack(end + "H", header[2:4])[0]
    if magic == 42:
        big = False
        ifd = struct.unpack(end + "I", header[4:8])[0]
    elif magic == 43:
        big = True
        ifd = struct.unpack(end + "Q", _read(f, 8, 8, size))[0]
    else:
        raise _Bad(f"bad TIFF magic {magic}")

    count_fmt, entry_size, offset_fmt, inline = ("Q", 20, "Q", 8) if big else ("H", 12, "I", 4)
    count_size = 8 if big else 2
    seen: set[int] = set()
    while ifd:
        if ifd in seen or len(seen) > _MAX_IFDS:
            raise _Bad("IFD chain loops")
        seen.add(ifd)
        n = struct.unpack(end + count_fmt, _read(f, ifd, count_size, size))[0]
        if n == 0 or n > 65535:
            raise _Bad(f"implausible IFD entry count {n}")
        table = _read(f, ifd + count_size, n * entry_size + inline, size)
        tags: dict[int, list[int]] = {}
        for i in range(n):
            entry = table[i * entry_size : (i + 1) * entry_size]
            tag, typ = struct.unpack(end + "HH", entry[:4])
            cnt = struct.unpack(end + ("Q" if big else "I"), entry[4 : 4 + (8 if big else 4)])[0]
            if tag not in (_STRIP_OFFSETS, _STRIP_COUNTS, _TILE_OFFSETS, _TILE_COUNTS):
                continue
            if typ not in _INT_FORMATS:
                raise _Bad(f"tag {tag} has unsupported type {typ}")
            width = _TYPE_SIZES[typ]
            raw_field = entry[4 + (8 if big else 4) :]
            if cnt * width <= inline:
                raw = raw_field[: cnt * width]
            else:
                where = struct.unpack(end + offset_fmt, raw_field[:inline])[0]
                raw = _read(f, where, cnt * width, size)
            tags[tag] = list(struct.unpack(end + str(cnt) + _INT_FORMATS[typ], raw))
        for offs, lens in ((_STRIP_OFFSETS, _STRIP_COUNTS), (_TILE_OFFSETS, _TILE_COUNTS)):
            if offs in tags:
                if lens not in tags or len(tags[lens]) != len(tags[offs]):
                    raise _Bad("strip/tile offsets and byte counts disagree")
                for o, length in zip(tags[offs], tags[lens], strict=True):
                    if o + length > size:
                        raise _Bad(
                            f"image data runs past the end of the file (offset {o}, "
                            f"{length} bytes, file {size}) - truncated?"
                        )
        ifd = struct.unpack(
            end + offset_fmt, _read(f, ifd + count_size + n * entry_size, inline, size)
        )[0]
