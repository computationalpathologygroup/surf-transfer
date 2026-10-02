"""Slide validation. Everything here runs in a worker subprocess.

Corrupt files can segfault openslide, so each job gets its own process
(SlideValidator.run) with a timeout; the parent turns a crash or a timeout
into a `corrupt` verdict naming the last position read. openslide is only
imported inside the worker, never in the main process.
"""

from __future__ import annotations

import hashlib
import multiprocessing as mp
import os
import random
import shutil
import signal
import time
import traceback
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from .models import (
    CHECK_CRASH,
    CHECK_DEEP,
    CHECK_HEADER,
    CHECK_INDEX,
    CHECK_LOWRES,
    CHECK_MISSING_DAT,
    CHECK_NO_VALIDATOR,
    CHECK_SLIDEDAT,
    CHECK_TILE,
    CHECK_TIMEOUT,
    CHECK_UNEXPECTED_DAT,
    CHECK_ZIP,
    CORRUPT,
    KIND_MRXS,
    UNVALIDATED,
    VERIFIED,
    CheckResult,
    ExtractedFile,
    Failure,
    Job,
    JobResult,
)
from .parsing import check_completeness, iter_tiles, parse_slidedat, sample_tile_coords
from .tiffcheck import tiff_structure_error
from .util import fits_free_space, safe_relpath, sha256_file

# Extensions only openslide formats use: if detection fails on these the file is
# damaged, not "a format we cannot read". Flat .tif/.tiff, .dcm, .czi, .isyntax
# and the like stay `unvalidated`.
_OPENSLIDE_ONLY_EXTS = (".svs", ".ndpi", ".scn", ".vms", ".vmu", ".bif", ".svslide", ".mrxs")
_LOWRES_CHUNK = 2048
_DEEP_CHUNK = 1024


class Progress(Protocol):
    def __setitem__(self, index: int, value: int) -> None: ...

    def __getitem__(self, index: int) -> int: ...


class _NoProgress:
    def __init__(self) -> None:
        self._v = [-1, -1, -1]

    def __setitem__(self, index: int, value: int) -> None:
        self._v[index] = value

    def __getitem__(self, index: int) -> int:
        return self._v[index]


def _openslide() -> Any:
    import openslide

    return openslide


# --- shared openslide checks ------------------------------------------------


def _fail(job: Job, check: str, detail: str, checks: list[CheckResult], **kw: Any) -> JobResult:
    failure = Failure(
        check=check,
        detail=detail,
        members=kw.pop("members", tuple(rel for rel, _ in job.members)),
        level=kw.pop("level", None),
        coord=kw.pop("coord", None),
    )
    return JobResult(
        slide_key=job.slide_key, kind=job.kind, status=CORRUPT, checks=checks, failure=failure, **kw
    )


def check_openslide(
    job: Job, path: Path, progress: Progress, checks: list[CheckResult]
) -> JobResult:
    """Open, read the lowest level in full, sample level-0 tiles (and, with
    --deep-check, decode every tile at every level)."""
    ops = _openslide()
    opts = job.options
    try:
        slide = ops.OpenSlide(str(path))
    except Exception as e:  # noqa: BLE001 - any open failure means unreadable header
        checks.append(CheckResult("open", False, str(e)))
        return _fail(job, CHECK_HEADER, f"openslide could not open the slide: {e}", checks)

    with slide:
        dims0 = tuple(slide.dimensions)
        levels = slide.level_count
        props = slide.properties
        mpp = (props.get("openslide.mpp-x"), props.get("openslide.mpp-y"))
        metadata: dict[str, Any] = {
            "vendor": props.get("openslide.vendor"),
            "dimensions": list(dims0),
            "level_count": levels,
            "level_dimensions": [list(d) for d in slide.level_dimensions],
            "mpp_x": float(mpp[0]) if mpp[0] else None,
            "mpp_y": float(mpp[1]) if mpp[1] else None,
        }
        checks.append(CheckResult("open", True, f"{dims0[0]}x{dims0[1]}, {levels} level(s)"))

        # Lowest-resolution level, in full.
        low = levels - 1
        low_dims = tuple(slide.level_dimensions[low])
        down = float(slide.level_downsamples[low])
        for x, y, w, h in iter_tiles(low_dims, _LOWRES_CHUNK):
            loc = (int(x * down), int(y * down))
            progress[0], progress[1], progress[2] = low, loc[0], loc[1]
            try:
                slide.read_region(loc, low, (w, h))
            except Exception as e:  # noqa: BLE001
                checks.append(CheckResult("lowres_read", False, str(e)))
                return _fail(
                    job,
                    CHECK_LOWRES,
                    f"reading level {low} at level-0 {loc} failed: {e}",
                    checks,
                    level=low,
                    coord=loc,
                    metadata=metadata,
                )
        checks.append(CheckResult("lowres_read", True, f"level {low} {low_dims[0]}x{low_dims[1]}"))

        # Random level-0 tiles, reproducible from the stored seed.
        seed = opts.seed if opts.seed is not None else random.SystemRandom().randrange(2**32)
        tile = min(opts.tile_size, dims0[0], dims0[1])
        coords = sample_tile_coords(dims0, opts.sample_tiles, seed, tile)
        for x, y in coords:
            progress[0], progress[1], progress[2] = 0, x, y
            try:
                slide.read_region((x, y), 0, (tile, tile))
            except Exception as e:  # noqa: BLE001
                checks.append(CheckResult("tile_sample", False, str(e)))
                return _fail(
                    job,
                    CHECK_TILE,
                    f"tile at level-0 ({x}, {y}) failed: {e}",
                    checks,
                    level=0,
                    coord=(x, y),
                    metadata=metadata,
                    seed=seed,
                    coords=coords,
                )
        checks.append(CheckResult("tile_sample", True, f"{len(coords)} tiles, seed {seed}"))

        if opts.deep:
            for level in range(levels):
                ldims = tuple(slide.level_dimensions[level])
                ds = float(slide.level_downsamples[level])
                for x, y, w, h in iter_tiles(ldims, _DEEP_CHUNK):
                    loc = (int(x * ds), int(y * ds))
                    progress[0], progress[1], progress[2] = level, loc[0], loc[1]
                    try:
                        slide.read_region(loc, level, (w, h))
                    except Exception as e:  # noqa: BLE001
                        checks.append(CheckResult("deep_check", False, str(e)))
                        return _fail(
                            job,
                            CHECK_DEEP,
                            f"level {level} tile at level-0 {loc} failed: {e}",
                            checks,
                            level=level,
                            coord=loc,
                            metadata=metadata,
                            seed=seed,
                            coords=coords,
                            deep=True,
                        )
            checks.append(CheckResult("deep_check", True, f"all tiles at {levels} level(s)"))

    return JobResult(
        slide_key=job.slide_key,
        kind=job.kind,
        status=VERIFIED,
        format=metadata["vendor"],
        checks=checks,
        metadata=metadata,
        seed=seed,
        coords=coords,
        deep=opts.deep,
    )


# --- per-format validators --------------------------------------------------


class FormatValidator(Protocol):
    """One per slide format. Add DICOM / iSyntax by adding an implementation to
    VALIDATORS; nothing else changes."""

    name: str

    def handles(self, job: Job, root: Path) -> bool: ...

    def validate(self, job: Job, root: Path, progress: Progress) -> JobResult: ...


class MrxsValidator:
    name = "mrxs"

    def handles(self, job: Job, root: Path) -> bool:
        return job.slide_kind == KIND_MRXS

    def validate(self, job: Job, root: Path, progress: Progress) -> JobResult:
        checks: list[CheckResult] = []
        rels = [rel for rel, _ in job.members]
        if job.index_path is None:
            checks.append(CheckResult("index_file", False, "no .mrxs file arrived"))
            return _fail(
                job,
                CHECK_INDEX,
                "the .mrxs index file is missing (only the data folder arrived)",
                checks,
            )
        checks.append(CheckResult("index_file", True))

        folder = job.index_path[: -len(".mrxs")]
        ini_rel = next((r for r in rels if r.lower() == f"{folder}/slidedat.ini".lower()), None)
        if ini_rel is None:
            checks.append(CheckResult("slidedat_present", False, f"{folder}/Slidedat.ini"))
            return _fail(
                job,
                CHECK_SLIDEDAT,
                f"{folder}/Slidedat.ini did not arrive",
                checks,
                members=(job.index_path,),
            )
        try:
            info = parse_slidedat((root / ini_rel).read_text(encoding="utf-8", errors="replace"))
        except (ValueError, OSError) as e:
            checks.append(CheckResult("slidedat_parse", False, str(e)))
            return _fail(
                job, CHECK_SLIDEDAT, f"cannot read Slidedat.ini: {e}", checks, members=(ini_rel,)
            )
        checks.append(
            CheckResult(
                "slidedat_parse",
                True,
                f"{info.file_count} data file(s) declared"
                + (f" + {info.index_file}" if info.index_file else ""),
            )
        )

        prefix = folder + "/"
        present = [
            r[len(prefix) :] for r in rels if r.startswith(prefix) and "/" not in r[len(prefix) :]
        ]
        comp = check_completeness(info.expected, present)
        checks.append(CheckResult("dat_missing", not comp.missing, ", ".join(comp.missing)))
        checks.append(
            CheckResult("dat_unexpected", not comp.unexpected, ", ".join(comp.unexpected))
        )
        if not comp.ok:
            parts = []
            if comp.missing:
                parts.append("missing: " + ", ".join(comp.missing))
            if comp.unexpected:
                parts.append("unexpected: " + ", ".join(comp.unexpected))
            check = CHECK_MISSING_DAT if comp.missing else CHECK_UNEXPECTED_DAT
            involved = tuple(prefix + n for n in comp.missing) + tuple(
                prefix + n for n in comp.unexpected
            )
            return _fail(
                job,
                check,
                "Slidedat.ini vs files that arrived - " + "; ".join(parts),
                checks,
                members=involved,
            )

        return check_openslide(job, root / job.index_path, progress, checks)


class OpenSlideValidator:
    """Any single-file format openslide detects (svs, ndpi, pyramidal tiff, ...)."""

    name = "openslide"

    def handles(self, job: Job, root: Path) -> bool:
        if job.index_path is None or job.slide_kind == KIND_MRXS:
            return False
        return self._detect(root / job.index_path) is not None

    @staticmethod
    def _detect(path: Path) -> str | None:
        try:
            return _openslide().OpenSlide.detect_format(str(path))  # type: ignore[no-any-return]
        except Exception:  # noqa: BLE001
            return None

    def validate(self, job: Job, root: Path, progress: Progress) -> JobResult:
        assert job.index_path is not None
        checks = [CheckResult("detect_format", True, str(self._detect(root / job.index_path)))]
        return check_openslide(job, root / job.index_path, progress, checks)


VALIDATORS: tuple[FormatValidator, ...] = (MrxsValidator(), OpenSlideValidator())


def validate_slide(job: Job, progress: Progress) -> JobResult:
    root = Path(job.root_dir)
    for validator in VALIDATORS:
        if validator.handles(job, root):
            return validator.validate(job, root, progress)
    assert job.index_path is not None
    if job.index_path.lower().endswith(_OPENSLIDE_ONLY_EXTS):
        return _fail(
            job,
            CHECK_HEADER,
            f"{Path(job.index_path).name} is not recognised as a slide by openslide",
            [CheckResult("detect_format", False, "no format detected")],
        )
    if job.index_path.lower().endswith((".tif", ".tiff")):
        problem = tiff_structure_error(root / job.index_path)
        if problem:
            return _fail(
                job,
                CHECK_HEADER,
                f"{Path(job.index_path).name} is a damaged TIFF: {problem}",
                [CheckResult("tiff_structure", False, problem)],
            )
    reason = (
        f"no validator handles {Path(job.index_path).name}: openslide does not detect "
        "it as a readable slide (flat TIFF, iSyntax, CZI, DICOM, ...)"
    )
    return JobResult(
        slide_key=job.slide_key,
        kind=job.kind,
        status=UNVALIDATED,
        checks=[CheckResult("detect_format", False, "no format detected")],
        failure=Failure(CHECK_NO_VALIDATOR, reason, members=tuple(r for r, _ in job.members)),
    )


# --- archives ---------------------------------------------------------------


def unpack_archive(job: Job) -> JobResult:
    """Extract a zip next to itself, verifying every member's CRC (zipfile does
    this while reading), its size, and a from-disk sha256 re-read."""
    assert job.index_path is not None and job.extract_dir is not None  # "" is a valid dir
    root = Path(job.root_dir)
    archive = root / job.index_path
    members = tuple(rel for rel, _ in job.members)
    if not job.index_path.lower().endswith(".zip"):
        reason = (
            f"unsupported archive format ({Path(job.index_path).suffix}); "
            "extract it manually and re-run"
        )
        return JobResult(
            slide_key=job.slide_key,
            kind=job.kind,
            status=UNVALIDATED,
            format="archive",
            checks=[CheckResult("archive_format", False, reason)],
            failure=Failure(CHECK_NO_VALIDATOR, reason, members=members),
        )

    checks: list[CheckResult] = []
    target = root / job.extract_dir
    extracted: list[ExtractedFile] = []
    try:
        with zipfile.ZipFile(archive) as zf:
            infos = [i for i in zf.infolist() if not i.is_dir()]
            if any(i.flag_bits & 0x1 for i in infos):
                checks.append(CheckResult("zip_encrypted", False))
                return _fail(
                    job,
                    CHECK_ZIP,
                    "password-protected zip is not supported",
                    checks,
                    members=members,
                )
            needed = sum(i.file_size for i in infos)
            free = shutil.disk_usage(root).free
            if not fits_free_space(free, needed, job.options.min_free_bytes):
                checks.append(CheckResult("free_space", False, f"need {needed}, free {free}"))
                return _fail(
                    job,
                    CHECK_ZIP,
                    f"not enough free space to extract ({needed} bytes needed, {free} free)",
                    checks,
                    members=members,
                )
            checks.append(CheckResult("zip_listing", True, f"{len(infos)} file(s), {needed} bytes"))
            for info in infos:
                try:
                    rel = safe_relpath(info.filename)
                except ValueError as e:
                    checks.append(CheckResult("zip_paths", False, info.filename))
                    return _fail(
                        job, CHECK_ZIP, f"unsafe path in zip: {e}", checks, members=members
                    )
                dest = target / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                part = dest.with_name(dest.name + ".part")
                digest = hashlib.sha256()
                written = 0
                try:
                    with zf.open(info) as src, open(part, "wb") as out:
                        while chunk := src.read(8 * 1024 * 1024):
                            out.write(chunk)
                            digest.update(chunk)
                            written += len(chunk)
                except BaseException:
                    part.unlink(missing_ok=True)  # e.g. BadZipFile on a CRC mismatch
                    raise
                if written != info.file_size:
                    part.unlink(missing_ok=True)
                    checks.append(CheckResult("zip_member_size", False, rel))
                    return _fail(
                        job,
                        CHECK_ZIP,
                        f"{info.filename}: extracted {written} bytes, zip says {info.file_size}",
                        checks,
                        members=members,
                    )
                if sha256_file(part) != digest.hexdigest():
                    part.unlink(missing_ok=True)
                    checks.append(CheckResult("zip_member_hash", False, rel))
                    return _fail(
                        job,
                        CHECK_ZIP,
                        f"{info.filename}: bytes on disk differ from bytes extracted",
                        checks,
                        members=members,
                    )
                full = digest.hexdigest()
                dest_rel = f"{job.extract_dir}/{rel}" if job.extract_dir else rel
                if dest.exists():
                    same = sha256_file(dest) == full
                    part.unlink(missing_ok=True)
                    if not same:
                        checks.append(CheckResult("zip_conflict", False, dest_rel))
                        return _fail(
                            job,
                            CHECK_ZIP,
                            f"zip member {info.filename} would overwrite {dest_rel}, which has "
                            "different content; nothing was overwritten",
                            checks,
                            members=(*members, dest_rel),
                        )
                    extracted.append(ExtractedFile(rel, written, full, existing=True))
                    continue
                if dest_rel in job.claimed:
                    part.unlink(missing_ok=True)
                    checks.append(CheckResult("zip_conflict", False, dest_rel))
                    return _fail(
                        job,
                        CHECK_ZIP,
                        f"zip member {info.filename} collides with the listed file {dest_rel}, "
                        "which has not been downloaded",
                        checks,
                        members=(*members, dest_rel),
                    )
                os.replace(part, dest)
                extracted.append(ExtractedFile(rel, written, full))
    except (zipfile.BadZipFile, EOFError, OSError, NotImplementedError, RuntimeError) as e:
        checks.append(CheckResult("zip_crc", False, str(e)))
        return _fail(job, CHECK_ZIP, f"zip is damaged or unreadable: {e}", checks, members=members)
    checks.append(CheckResult("zip_crc", True, f"{len(extracted)} member(s) CRC-verified"))
    return JobResult(
        slide_key=job.slide_key,
        kind=job.kind,
        status=VERIFIED,
        format="zip",
        checks=checks,
        extracted=extracted,
        extract_dir=job.extract_dir,
    )


# --- worker entry points ----------------------------------------------------


def execute_job(job: Job, progress: Progress) -> JobResult:
    """The worker's logic, callable in-process for tests."""
    start = time.monotonic()
    result = unpack_archive(job) if job.kind == "unpack" else validate_slide(job, progress)
    result.seconds = round(time.monotonic() - start, 3)
    return result


def run_job(job: Job, conn: Any, progress: Progress) -> None:
    """Subprocess target: run the job and send exactly one message back."""
    try:
        conn.send(execute_job(job, progress))
    except BaseException:  # noqa: BLE001 - report instead of dying silently
        conn.send(("error", traceback.format_exc()))
    finally:
        conn.close()


class SlideValidator:
    """Runs one job in its own subprocess, with a timeout. Never raises: a
    crash, a timeout or an internal error becomes a JobResult."""

    def __init__(self, target: Callable[[Job, Any, Any], None] = run_job):
        self._target = target
        self._ctx = mp.get_context("spawn")

    def run(self, job: Job) -> JobResult:
        parent, child = self._ctx.Pipe(duplex=False)
        progress = self._ctx.Array("q", [-1, -1, -1], lock=False)
        proc = self._ctx.Process(target=self._target, args=(job, child, progress), daemon=True)
        started = time.monotonic()
        proc.start()
        child.close()
        message: Any = None
        timed_out = False
        try:
            if parent.poll(job.options.timeout):
                try:
                    message = parent.recv()
                except EOFError:
                    message = None  # child died before sending anything
            else:
                timed_out = True
        finally:
            if proc.is_alive() and (timed_out or message is None):
                proc.kill()
            proc.join(10)
            parent.close()
        seconds = round(time.monotonic() - started, 3)
        where = _position(progress)
        members = tuple(rel for rel, _ in job.members)

        def verdict(check: str, detail: str) -> JobResult:
            return JobResult(
                slide_key=job.slide_key,
                kind=job.kind,
                status=CORRUPT,
                seconds=seconds,
                checks=[CheckResult(check, False, detail)],
                failure=Failure(
                    check,
                    detail,
                    members=members,
                    level=progress[0] if progress[0] >= 0 else None,
                    coord=(progress[1], progress[2]) if progress[0] >= 0 else None,
                ),
            )

        if timed_out:
            return verdict(CHECK_TIMEOUT, f"validation exceeded {job.options.timeout:g}s{where}")
        if isinstance(message, JobResult):
            return message
        if isinstance(message, tuple) and message and message[0] == "error":
            tail = str(message[1]).strip().splitlines()[-1]
            return JobResult(
                slide_key=job.slide_key,
                kind=job.kind,
                status=UNVALIDATED,
                seconds=seconds,
                checks=[CheckResult("validator", False, tail)],
                failure=Failure(
                    CHECK_NO_VALIDATOR,
                    f"validator error (not evidence of corruption): {tail}",
                    members=members,
                ),
            )
        code = proc.exitcode
        if code is not None and code < 0:
            try:
                name = signal.Signals(-code).name
            except ValueError:
                name = f"signal {-code}"
            return verdict(CHECK_CRASH, f"validator process died with {name}{where}")
        return verdict(
            CHECK_CRASH, f"validator process exited with code {code} without a result{where}"
        )


def _position(progress: Any) -> str:
    if progress[0] < 0:
        return ""
    return f", last read: level {progress[0]} at level-0 ({progress[1]}, {progress[2]})"
