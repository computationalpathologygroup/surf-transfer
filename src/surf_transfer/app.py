"""Imperative shell: sync listings, run the download loop, feed the pipeline."""

from __future__ import annotations

import logging
import shutil
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .budget import RunBudget
from .config import RunOptions
from .downloader import Downloader
from .manifest import Manifest
from .models import (
    ARCHIVED,
    DOWNLOADED,
    DOWNLOADING,
    FAILED,
    KIND_ARCHIVE,
    MOVED,
    VERIFIED,
    RemoteFile,
    ValidationOptions,
)
from .pipeline import JobRunner, Pipeline
from .sources import Source, SourceError
from .util import fits_free_space, format_bytes, now_iso, sha256_file

log = logging.getLogger("surf_transfer")


@dataclass
class RunSummary:
    downloaded: int = 0
    failed: int = 0
    skipped: int = 0
    deferred: int = 0
    no_space: int = 0
    flagged: int = 0
    listing_errors: list[str] = field(default_factory=list)
    budget_stop: str | None = None


def _free_bytes(path: Path) -> int:
    return shutil.disk_usage(path).free


def validation_options(opts: RunOptions) -> ValidationOptions:
    return ValidationOptions(
        sample_tiles=opts.sample_tiles,
        deep=opts.deep_check,
        timeout=opts.validate_timeout,
        seed=opts.seed,
        min_free_bytes=opts.min_free_bytes,
    )


def _progress_printer() -> Callable[[int, int], None] | None:
    if not sys.stdout.isatty():
        return None
    state = {"shown": 0.0}

    def show(done: int, total: int) -> None:
        percent = (done / total * 100) if total else 100.0
        if percent - state["shown"] >= 1 or done >= total:
            state["shown"] = percent
            print(
                f"\r      {percent:5.1f}% {format_bytes(done)}/{format_bytes(total)}",
                end="",
                flush=True,
            )
            if done >= total:
                print()
                state["shown"] = 0.0

    return show


def sync_sources(
    manifest: Manifest, sources: Sequence[Source], opts: RunOptions, summary: RunSummary
) -> list[tuple[Source, list[RemoteFile]]]:
    """List every source (read-only) and sync the manifest. Source errors are
    recorded, not raised, so one bad source does not hide the others."""
    listings: list[tuple[Source, list[RemoteFile]]] = []
    for source in sources:
        try:
            remote_files = source.list()
        except SourceError as e:
            log.error("cannot list %s: %s", getattr(source, "source_id", source), e)
            summary.listing_errors.append(str(e))
            continue
        summary.listing_errors += list(getattr(source, "errors", []))
        result = manifest.sync_listing(remote_files, centre=source.centre)
        for entry in result.flagged:
            log.warning("flagged %s: %s", entry.rel_path, entry.flagged)
        summary.flagged += len(result.flagged)
        for entry in result.inserted:
            if manifest.adopt_existing(opts.output_dir, entry):
                log.info("adopted existing file: %s", entry.rel_path)
        log.info(
            "%s: %d file(s) listed, %d new",
            source.source_id,
            len(remote_files),
            len(result.inserted),
        )
        listings.append((source, remote_files))
    manifest.sync_slides()
    manifest.save()
    return listings


def _resolve_archive(manifest: Manifest, archive_dir: Path) -> None:
    moved = [e for e in manifest.files.values() if e.status == MOVED]
    if not moved:
        return
    resolved, mismatched = manifest.resolve_archive(archive_dir, moved)
    for entry in resolved:
        log.info("archived (hash verified): %s", entry.rel_path)
    for entry in mismatched:
        log.warning("archive hash mismatch, left as moved: %s - %s", entry.rel_path, entry.error)
    manifest.save()


def _existing_file_decision(manifest: Manifest, key: str, local: Path, recheck: bool) -> str:
    """For an entry that is already past download: 'skip', 'moved' or 'redo'."""
    entry = manifest.files[key]
    if entry.status in (MOVED, ARCHIVED):
        return "skip"
    if not local.exists():
        return "moved" if entry.status == VERIFIED else "redo"
    if local.stat().st_size != entry.size:
        return "redo"
    if recheck and entry.sha256 and sha256_file(local) != entry.sha256:
        return "redo"
    return "skip"


def run_downloads(
    manifest: Manifest,
    listings: list[tuple[Source, list[RemoteFile]]],
    opts: RunOptions,
    pipeline: Pipeline,
    summary: RunSummary,
    downloader: Downloader | None = None,
    free_bytes: Callable[[Path], int] = _free_bytes,
) -> None:
    downloader = downloader or Downloader(_progress_printer())
    budget = RunBudget(max_files=opts.max_files, max_bytes=opts.max_bytes)
    output_dir = opts.output_dir

    for source, remotes in listings:
        total = len(remotes)
        for index, remote in enumerate(remotes, 1):
            pipeline.poll()
            entry = manifest.files[remote.key]
            local = output_dir / (entry.rel_path or "")
            tag = f"[{index}/{total}] {entry.name}"

            if entry.flagged and not opts.accept_flagged:
                log.warning("%s: flagged, not downloading (%s)", tag, entry.flagged)
                summary.skipped += 1
                continue
            if remote.problem or (
                entry.status == FAILED
                and entry.error
                and entry.error.startswith(("name collision", "unsafe remote path"))
            ):
                log.error("%s: cannot download: %s", tag, remote.problem or entry.error)
                entry.status, entry.error = FAILED, remote.problem or entry.error
                summary.failed += 1
                continue

            if not opts.force and entry.status in (VERIFIED, DOWNLOADED, MOVED, ARCHIVED):
                decision = _existing_file_decision(manifest, remote.key, local, opts.recheck_hashes)
                if decision == "skip":
                    log.info("%s: skipping (status=%s)", tag, entry.status)
                    summary.skipped += 1
                    continue
                if decision == "moved":
                    entry.status = MOVED
                    manifest.refresh_slide_locations()
                    manifest.save()
                    log.info("%s: moved out of the output dir - expected at the archive", tag)
                    summary.skipped += 1
                    continue
                entry.status, entry.error = FAILED, "local file missing or changed; re-downloading"
                manifest.reopen_slides_of([remote.key])

            if not budget.can_start(remote.size):
                log.info("%s: deferred (run budget): %s", tag, budget.stop_reason)
                summary.deferred += 1
                continue
            free = free_bytes(output_dir)
            if not fits_free_space(free, remote.size, opts.min_free_bytes):
                msg = (
                    f"not enough free space for {format_bytes(remote.size)} "
                    f"({format_bytes(free)} free, "
                    f"keeping {format_bytes(opts.min_free_bytes)} spare)"
                )
                log.error("%s: %s", tag, msg)
                entry.error = msg
                summary.no_space += 1
                continue

            log.info("%s: downloading (%s)", tag, format_bytes(remote.size))
            entry.status = DOWNLOADING
            entry.attempts += 1
            manifest.save()
            outcome = downloader.fetch(source, remote, local)
            if not outcome.ok:
                entry.status, entry.error = FAILED, outcome.error
                manifest.save()
                log.error("%s: %s", tag, outcome.error)
                summary.failed += 1
                continue
            now = now_iso()
            entry.status = DOWNLOADED
            entry.sha256, entry.checks, entry.error = outcome.sha256, outcome.checks, None
            entry.downloaded_at, entry.verified_at = now, None
            manifest.reopen_slides_of([remote.key])
            manifest.save()
            budget.record(remote.size)
            summary.downloaded += 1
            log.info("%s: downloaded, checks %s", tag, outcome.checks)
            pipeline.submit_ready()

    summary.budget_stop = budget.stop_reason


def run(
    manifest: Manifest,
    sources: Sequence[Source],
    opts: RunOptions,
    runner: JobRunner,
    downloader: Downloader | None = None,
    free_bytes: Callable[[Path], int] = _free_bytes,
) -> RunSummary:
    """Sync every source, download what is missing, validate slides as they complete."""
    summary = RunSummary()
    opts.output_dir.mkdir(parents=True, exist_ok=True)
    manifest.reconcile_local(opts.output_dir)
    if opts.accept_flagged:
        for entry in manifest.accept_flagged():
            log.info("accepted flagged file: %s", entry.rel_path)
    listings = sync_sources(manifest, sources, opts, summary)
    if opts.archive_dir:
        _resolve_archive(manifest, opts.archive_dir)
    if opts.dry_run:
        log.info("dry run: manifest synced, nothing downloaded")
        return summary

    pipeline = Pipeline(
        manifest, runner, opts.output_dir, validation_options(opts), workers=opts.validate_workers
    )
    try:
        pipeline.submit_ready()  # slides left downloaded-but-unvalidated by an earlier run
        run_downloads(manifest, listings, opts, pipeline, summary, downloader, free_bytes)
        pipeline.submit_ready()
        pipeline.finish()
    except KeyboardInterrupt:
        pipeline.abort()
        manifest.save()
        raise
    manifest.refresh_slide_locations()
    manifest.save()
    return summary


def revalidate(manifest: Manifest, opts: RunOptions, runner: JobRunner) -> tuple[int, int]:
    """Run slide validation on locally present files, without downloading.
    Returns (validated, skipped_not_on_disk)."""
    manifest.sync_slides()
    manifest.reconcile_local(opts.output_dir)
    pipeline = Pipeline(
        manifest, runner, opts.output_dir, validation_options(opts), workers=opts.validate_workers
    )
    queued = skipped = 0
    wanted = [w.lower() for w in opts.slide_filter]
    try:
        for slide in list(manifest.slides.values()):
            if slide.superseded_by:
                continue
            if wanted and not any(w in slide.name.lower() for w in wanted):
                continue
            if slide.kind == KIND_ARCHIVE:
                if slide.status == VERIFIED:
                    continue  # already unpacked and CRC-verified
            elif not wanted and slide.status == VERIFIED:
                continue  # already slide-verified; name it with --slide to force a re-check
            if not manifest.slide_present_locally(slide, opts.output_dir):
                log.warning(
                    "%s: not fully on disk (moved or missing), cannot revalidate", slide.name
                )
                skipped += 1
                continue
            pipeline.submit_slide(slide)
            queued += 1
        pipeline.finish()
    except KeyboardInterrupt:
        pipeline.abort()
        manifest.save()
        raise
    manifest.refresh_slide_locations()
    manifest.save()
    return queued, skipped
