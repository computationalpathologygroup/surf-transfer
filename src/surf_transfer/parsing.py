"""Pure parsers and checks. No network, no disk, no subprocesses."""

from __future__ import annotations

import configparser
import random
import re
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from urllib.parse import unquote

from .models import KIND_ARCHIVE, KIND_FILE, KIND_MRXS, KIND_SINGLE

# --- Slidedat.ini -----------------------------------------------------------


@dataclass(frozen=True)
class Slidedat:
    file_count: int | None
    files: list[str]  # [DATAFILE] FILE_0..FILE_n
    index_file: str | None = None  # [HIERARCHICAL] INDEXFILE (normally Index.dat)

    @property
    def expected(self) -> list[str]:
        """Every file besides Slidedat.ini that the slide needs."""
        return self.files + ([self.index_file] if self.index_file else [])


def parse_slidedat(text: str) -> Slidedat:
    """Read [DATAFILE] FILE_COUNT / FILE_0..FILE_n and [HIERARCHICAL] INDEXFILE."""
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.optionxform = str  # type: ignore[assignment,method-assign]
    parser.read_string(text.lstrip("﻿"))
    if not parser.has_section("DATAFILE"):
        raise ValueError("Slidedat.ini has no [DATAFILE] section")
    section = parser["DATAFILE"]
    count_raw = section.get("FILE_COUNT")
    try:
        count = int(count_raw) if count_raw is not None else None
    except ValueError:
        raise ValueError(f"Slidedat.ini FILE_COUNT is not a number: {count_raw!r}") from None
    if count is None:
        raise ValueError("Slidedat.ini [DATAFILE] has no FILE_COUNT")
    files: list[str] = []
    for i in range(count):
        name = section.get(f"FILE_{i}")
        if not name:
            raise ValueError(
                f"Slidedat.ini [DATAFILE] lists FILE_COUNT={count} but FILE_{i} is missing"
            )
        files.append(name.strip())
    index_file = None
    if parser.has_section("HIERARCHICAL"):
        index_file = (parser["HIERARCHICAL"].get("INDEXFILE") or "").strip() or None
    return Slidedat(file_count=count, files=files, index_file=index_file)


@dataclass(frozen=True)
class Completeness:
    missing: list[str] = field(default_factory=list)
    unexpected: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.missing and not self.unexpected


def check_completeness(expected: Iterable[str], present: Iterable[str]) -> Completeness:
    """Compare the .dat files Slidedat.ini declares against those that arrived.
    Only .dat files can be 'unexpected'; Slidedat.ini and other files are ignored."""
    expected_set = set(expected)
    present_dats = {p for p in present if p.lower().endswith(".dat")}
    return Completeness(
        missing=sorted(expected_set - set(present)),
        unexpected=sorted(present_dats - expected_set),
    )


# --- oc:checksums -----------------------------------------------------------

_CHECKSUM_PREFERENCE = ("sha256", "sha1", "md5", "adler32")
_CHECKSUM_TOKEN = re.compile(r"^([A-Za-z0-9]+):([0-9A-Fa-f]+)$")


def parse_oc_checksums(value: str | None) -> dict[str, str]:
    """Parse 'SHA1:… MD5:… ADLER32:…' into {algo: lowercase hex}."""
    result: dict[str, str] = {}
    for token in (value or "").split():
        match = _CHECKSUM_TOKEN.match(token)
        if match:
            result[match.group(1).lower()] = match.group(2).lower()
    return result


def pick_checksum_algo(source: dict[str, str]) -> str | None:
    """The strongest algorithm we can compute locally, or None."""
    for algo in _CHECKSUM_PREFERENCE:
        if algo in source:
            return algo
    return None


def compare_checksums(source: dict[str, str], computed: dict[str, str]) -> str:
    """Outcome string for the manifest: 'sha1:match', 'sha1:mismatch', 'absent',
    or 'unsupported:<algos>' (the source offered only algorithms we don't compute)."""
    if not source:
        return "absent"
    algo = pick_checksum_algo(source)
    if algo is None:
        return "unsupported:" + ",".join(sorted(source))
    if algo not in computed:
        return f"{algo}:not-computed"
    return f"{algo}:{'match' if source[algo] == computed[algo].lower() else 'mismatch'}"


# --- grouping files into slides --------------------------------------------

_ARCHIVE_EXTS = (".zip", ".7z", ".rar", ".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tar.xz")
# Files that are plainly not slides; everything else goes through detect_format.
_NON_SLIDE_EXTS = frozenset(
    ".txt .csv .tsv .json .xml .xlsx .xls .docx .doc .pdf .md .log .ini .yaml .yml .html .htm "
    ".png .jpg .jpeg .gif .md5 .sha1 .sha256 .sha256sum .md5sum".split()
)


@dataclass
class SlideGroup:
    name: str
    kind: str
    member_keys: list[str]
    index_key: str | None = None  # the .mrxs file for MRXS groups; None if it never arrived
    folder: str = ""  # rel_path of the directory holding the group


def archive_suffix(path: str) -> str | None:
    low = path.lower()
    for ext in sorted(_ARCHIVE_EXTS, key=len, reverse=True):
        if low.endswith(ext):
            return ext
    return None


def _stem(path: str) -> str:
    suffix = archive_suffix(path)
    if suffix:
        return PurePosixPath(path[: -len(suffix)]).name
    return PurePosixPath(path).stem


def group_into_slides(files: Iterable[tuple[str, str]]) -> list[SlideGroup]:
    """Group (key, rel_path) pairs into slide units.

    An MRXS slide is X.mrxs plus every file under X/. A folder holding
    Slidedat.ini with no sibling X.mrxs is still reported as an MRXS group (with
    index_key=None) so the missing index file is caught. Archives and single-file
    candidates are one member each; clearly-non-slide files are kind "file"."""
    items = [(key, PurePosixPath(path).as_posix()) for key, path in files]
    groups: list[SlideGroup] = []
    claimed: set[str] = set()

    mrxs_dirs: dict[str, SlideGroup] = {}
    for key, path in items:
        if path.lower().endswith(".mrxs"):
            folder = path[: -len(".mrxs")]
            group = SlideGroup(
                name=PurePosixPath(folder).name,
                kind=KIND_MRXS,
                member_keys=[key],
                index_key=key,
                folder=str(PurePosixPath(path).parent),
            )
            mrxs_dirs[folder] = group
            claimed.add(key)
    for _key, path in items:
        if path.lower().endswith("/slidedat.ini"):
            folder = path[: -len("/Slidedat.ini")]
            if folder not in mrxs_dirs:
                mrxs_dirs[folder] = SlideGroup(
                    name=PurePosixPath(folder).name,
                    kind=KIND_MRXS,
                    member_keys=[],
                    index_key=None,
                    folder=str(PurePosixPath(folder).parent),
                )
    for key, path in items:
        if key in claimed:
            continue
        for folder, group in mrxs_dirs.items():
            if path.startswith(folder + "/"):
                group.member_keys.append(key)
                claimed.add(key)
                break
    groups.extend(mrxs_dirs.values())

    for key, path in items:
        if key in claimed:
            continue
        if archive_suffix(path):
            kind = KIND_ARCHIVE
        elif PurePosixPath(path).suffix.lower() in _NON_SLIDE_EXTS:
            kind = KIND_FILE
        else:
            kind = KIND_SINGLE
        groups.append(
            SlideGroup(
                name=_stem(path),
                kind=kind,
                member_keys=[key],
                index_key=key,
                folder=str(PurePosixPath(path).parent),
            )
        )
    return groups


# --- tile sampling ----------------------------------------------------------


def sample_tile_coords(
    dims: tuple[int, int], n: int, seed: int, tile: int = 512
) -> list[tuple[int, int]]:
    """n reproducible level-0 tile origins that lie fully inside the slide."""
    width, height = dims
    rng = random.Random(seed)
    max_x = max(width - tile, 0)
    max_y = max(height - tile, 0)
    return [(rng.randint(0, max_x), rng.randint(0, max_y)) for _ in range(n)]


def iter_tiles(dims: tuple[int, int], tile: int) -> Iterator[tuple[int, int, int, int]]:
    """Yield (x, y, w, h) boxes that tile a width x height area exactly once."""
    width, height = dims
    for y in range(0, height, tile):
        for x in range(0, width, tile):
            yield x, y, min(tile, width - x), min(tile, height - y)


# --- WebDAV PROPFIND --------------------------------------------------------

_DAV = "{DAV:}"
_OC = "{http://owncloud.org/ns}"


@dataclass(frozen=True)
class DavEntry:
    path: str  # relative to the listed base, no leading/trailing slash
    is_dir: bool
    size: int | None
    etag: str | None
    file_id: str | None
    checksums: dict[str, str]


def parse_propfind(xml_text: str, base_path: str) -> list[DavEntry]:
    """Parse a multistatus body. base_path is the URL path of the listed root;
    entries are returned relative to it and the root itself is omitted."""
    base = unquote(base_path).strip("/")
    root = ET.fromstring(xml_text)
    entries: list[DavEntry] = []
    for response in root.findall(f"{_DAV}response"):
        href = response.findtext(f"{_DAV}href") or ""
        full = unquote(href).strip("/")
        if full == base:
            continue
        if base and not full.startswith(base + "/"):
            continue
        rel = full[len(base) :].strip("/") if base else full

        props: dict[str, ET.Element] = {}
        for propstat in response.findall(f"{_DAV}propstat"):
            status = propstat.findtext(f"{_DAV}status") or ""
            if " 200 " not in status + " ":
                continue
            prop = propstat.find(f"{_DAV}prop")
            if prop is not None:
                for child in prop:
                    props[child.tag] = child

        rtype = props.get(f"{_DAV}resourcetype")
        is_dir = rtype is not None and rtype.find(f"{_DAV}collection") is not None
        length = props.get(f"{_DAV}getcontentlength")
        etag = props.get(f"{_DAV}getetag")
        fileid = props.get(f"{_OC}fileid")
        checksums = props.get(f"{_OC}checksums")
        checksum_text = ""
        if checksums is not None:
            parts = [c.text or "" for c in checksums] or [checksums.text or ""]
            checksum_text = " ".join(parts)
        entries.append(
            DavEntry(
                path=rel,
                is_dir=is_dir,
                size=int(length.text) if length is not None and length.text else None,
                etag=(etag.text or "").strip('"') or None if etag is not None else None,
                file_id=(fileid.text or "").strip() or None if fileid is not None else None,
                checksums=parse_oc_checksums(checksum_text),
            )
        )
    return entries
