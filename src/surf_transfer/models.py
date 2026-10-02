"""Record types and status vocabulary. Pure data, no I/O."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# File-level statuses.
QUEUED = "queued"
DOWNLOADING = "downloading"
DOWNLOADED = "downloaded"  # transport checks passed; slide checks may still be pending
FAILED = "failed"  # transport failure, retried next run
VERIFIED = "verified"
MOVED = "moved"
ARCHIVED = "archived"

# Slide-level statuses (VERIFIED / MOVED / ARCHIVED are shared with files).
VALIDATING = "validating"
CORRUPT = "corrupt"
UNVALIDATED = "unvalidated"

FILE_STATUSES = (QUEUED, DOWNLOADING, DOWNLOADED, FAILED, VERIFIED, MOVED, ARCHIVED)
SLIDE_STATUSES = (QUEUED, DOWNLOADED, VALIDATING, VERIFIED, CORRUPT, UNVALIDATED, MOVED, ARCHIVED)

# Slide kinds.
KIND_MRXS = "mrxs"
KIND_SINGLE = "single"  # one-member slide candidate (.svs, .tif, .ndpi, ...)
KIND_ARCHIVE = "archive"  # a .zip that must be unpacked before its slides can be grouped
KIND_FILE = "file"  # clearly not a slide: file checks only

# Failure check names recorded on corrupt slides.
CHECK_MISSING_DAT = "missing_dat"
CHECK_UNEXPECTED_DAT = "unexpected_dat"
CHECK_SLIDEDAT = "slidedat"
CHECK_INDEX = "missing_index"
CHECK_HEADER = "header"
CHECK_LOWRES = "lowres_read"
CHECK_TILE = "tile_read"
CHECK_DEEP = "deep_tile_read"
CHECK_TIMEOUT = "timeout"
CHECK_CRASH = "crash"
CHECK_ZIP = "zip"
CHECK_NO_VALIDATOR = "no_validator"


@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    detail: str = ""


@dataclass(frozen=True)
class Failure:
    """Which check failed, on which member files, and where."""

    check: str
    detail: str
    members: tuple[str, ...] = ()  # rel_paths of the files involved
    level: int | None = None
    coord: tuple[int, int] | None = None  # level-0 (x, y)


@dataclass(frozen=True)
class RemoteFile:
    """One file as listed by a source."""

    key: str  # manifest key, e.g. filesender:<transfer>:<file>
    source_id: str
    group_id: str  # transfer id / share id: scopes the output folder
    group_label: str  # human hint for the output folder (transfer subject, share name)
    name: str  # path inside the group, '/'-separated
    size: int
    checksums: dict[str, str] = field(default_factory=dict)  # algo -> hex, from the source
    etag: str | None = None
    remote_path: str | None = None
    problem: str | None = None  # listing-time reason the file cannot be downloaded
    extra: dict[str, str] = field(default_factory=dict)  # source-private (e.g. token)


@dataclass
class FileEntry:
    key: str
    source_id: str
    group_id: str
    name: str
    rel_path: str
    size: int
    status: str = QUEUED
    centre: str | None = None
    group_label: str | None = None
    remote_path: str | None = None
    etag: str | None = None
    sha256: str | None = None
    checks: dict[str, Any] = field(default_factory=dict)
    attempts: int = 0
    adopted: bool = False
    origin: str = "remote"  # "remote" | "extracted"
    parent: str | None = None  # archive entry key, for extracted files
    first_seen: str | None = None
    seq: int = 0  # insertion order across the manifest; orders deliveries of one slide
    downloaded_at: str | None = None
    verified_at: str | None = None
    archived_at: str | None = None
    archive_path: str | None = None
    error: str | None = None
    flagged: str | None = None  # e.g. appeared after the source was marked complete


@dataclass
class SlideRecord:
    key: str
    source_id: str
    name: str
    kind: str
    members: list[str]  # FileEntry keys
    centre: str | None = None
    format: str | None = None  # openslide vendor, or "zip"
    status: str = QUEUED
    checks: list[CheckResult] = field(default_factory=list)
    failure: Failure | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    seed: int | None = None
    coords: list[tuple[int, int]] = field(default_factory=list)
    deep: bool = False
    container: str | None = None  # key of the archive record this slide came out of
    first_seen: str | None = None
    received_at: str | None = None
    validated_at: str | None = None
    note: str | None = None
    generation: str | None = None  # group_id of the delivery (transfer / share) it arrived in
    folder: str = ""  # directory (rel_path) the slide's index file / data folder sits in
    replaces: list[str] = field(default_factory=list)  # earlier deliveries of the same slide name
    superseded_by: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ValidationOptions:
    sample_tiles: int = 64
    tile_size: int = 512
    deep: bool = False
    timeout: float = 900.0  # seconds per job
    min_free_bytes: int = 0  # unpack jobs: free space to leave after extraction
    seed: int | None = None  # None: pick a fresh random seed per slide and record it


@dataclass(frozen=True)
class Job:
    """Everything a worker subprocess needs. Plain data so it pickles."""

    kind: str  # "validate" | "unpack"
    slide_key: str
    slide_kind: str
    name: str
    root_dir: str  # output directory the member rel_paths are relative to
    members: tuple[tuple[str, int], ...]  # (rel_path, size) of every member file
    index_path: str | None  # rel_path of the file openslide opens
    options: ValidationOptions = field(default_factory=ValidationOptions)
    extract_dir: str | None = None  # unpack jobs: directory to extract into ("" = output root)
    claimed: tuple[str, ...] = ()  # unpack jobs: rel_paths already owned by other entries


@dataclass(frozen=True)
class ExtractedFile:
    rel_path: str  # relative to the extraction directory
    size: int
    sha256: str
    existing: bool = False  # an identical file was already on disk; nothing was overwritten


@dataclass
class JobResult:
    slide_key: str
    kind: str
    status: str  # verified | corrupt | unvalidated
    format: str | None = None
    checks: list[CheckResult] = field(default_factory=list)
    failure: Failure | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    seed: int | None = None
    coords: list[tuple[int, int]] = field(default_factory=list)
    deep: bool = False
    seconds: float = 0.0
    extracted: list[ExtractedFile] = field(default_factory=list)
    extract_dir: str | None = None
