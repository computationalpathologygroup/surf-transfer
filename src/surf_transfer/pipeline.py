"""Pipeline: owns the validation queue and worker pool, and is the only writer
of the manifest.

Downloads stay sequential in the caller. When a slide's member files are all
downloaded the slide is queued here; worker threads each run one subprocess
job at a time, so validating slide A overlaps downloading slide B. Workers only
return results; they never touch the manifest.
"""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from .manifest import Manifest
from .models import (
    DOWNLOADED,
    KIND_ARCHIVE,
    KIND_MRXS,
    UNVALIDATED,
    VERIFIED,
    CheckResult,
    Failure,
    Job,
    JobResult,
    SlideRecord,
    ValidationOptions,
)
from .util import now_iso

log = logging.getLogger("surf_transfer")


class JobRunner(Protocol):
    """SlideValidator in production; a fake in tests."""

    def run(self, job: Job) -> JobResult: ...


def build_job(
    manifest: Manifest, slide: SlideRecord, output_dir: Path, options: ValidationOptions
) -> Job:
    """Pure: describe what a worker must do for a slide (or archive)."""
    members = tuple(
        (manifest.files[m].rel_path, manifest.files[m].size)
        for m in slide.members
        if m in manifest.files
    )
    if slide.kind == KIND_MRXS:
        index = next((rel for rel, _ in members if rel.lower().endswith(".mrxs")), None)
    else:
        index = members[0][0] if members else None
    extract_dir = None
    kind = "validate"
    if slide.kind == KIND_ARCHIVE and index:
        kind = "unpack"
        extract_dir = index.rpartition("/")[0]  # next to the zip: X.mrxs and X/ pair up
    claimed = (
        tuple(e.rel_path for e in manifest.files.values() if e.key not in slide.members)
        if kind == "unpack"
        else ()
    )
    return Job(
        kind=kind,
        slide_key=slide.key,
        slide_kind=slide.kind,
        name=slide.name,
        root_dir=str(output_dir),
        members=members,
        index_path=index,
        options=options,
        extract_dir=extract_dir,
        claimed=claimed,
    )


class Pipeline:
    def __init__(
        self,
        manifest: Manifest,
        runner: JobRunner,
        output_dir: Path,
        options: ValidationOptions,
        workers: int = 2,
        on_result: Callable[[SlideRecord], None] | None = None,
    ):
        self.manifest = manifest
        self.runner = runner
        self.output_dir = Path(output_dir)
        self.options = options
        self.workers = max(1, workers)
        self._on_result = on_result
        self._jobs: queue.Queue[Job | None] = queue.Queue()
        self._results: queue.Queue[JobResult] = queue.Queue()
        self._threads: list[threading.Thread] = []
        self._outstanding = 0
        self._started = False

    # --- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        for i in range(self.workers):
            t = threading.Thread(target=self._work, name=f"validate-{i}", daemon=True)
            t.start()
            self._threads.append(t)

    def _work(self) -> None:
        while True:
            job = self._jobs.get()
            if job is None:
                return
            try:
                result = self.runner.run(job)
            except Exception as e:  # noqa: BLE001 - a runner bug must not strand the queue
                result = JobResult(
                    slide_key=job.slide_key,
                    kind=job.kind,
                    status=UNVALIDATED,
                    checks=[CheckResult("runner", False, str(e))],
                    failure=Failure("no_validator", f"validation runner error: {e}"),
                )
            self._results.put(result)

    # --- submitting ---------------------------------------------------------

    def submit_ready(self) -> int:
        """Queue every slide whose member files are all downloaded. Also settles
        plain (non-slide) files, which need only their file checks."""
        self.start()
        self._settle_plain_files()
        count = 0
        for slide in list(self.manifest.slides.values()):
            if self.manifest.slide_ready(slide):
                self.submit_slide(slide)
                count += 1
        return count

    def submit_slide(self, slide: SlideRecord) -> None:
        """Queue one slide unconditionally (used by --revalidate)."""
        self.start()
        self.manifest.begin_validation(slide)
        self._jobs.put(build_job(self.manifest, slide, self.output_dir, self.options))
        self._outstanding += 1
        log.info("queued for validation: %s", slide.name)

    def _settle_plain_files(self) -> None:
        in_slide = {m for s in self.manifest.slides.values() for m in s.members}
        now = now_iso()
        for key, entry in self.manifest.files.items():
            if entry.status == DOWNLOADED and key not in in_slide:
                entry.status = VERIFIED
                entry.verified_at = now
                entry.checks["slide"] = "not-a-slide"

    # --- collecting ---------------------------------------------------------

    def poll(self) -> int:
        """Apply any finished results (non-blocking); returns how many."""
        applied = 0
        while True:
            try:
                result = self._results.get_nowait()
            except queue.Empty:
                return applied
            self._apply(result)
            applied += 1

    def _apply(self, result: JobResult) -> None:
        self._outstanding -= 1
        slide = self.manifest.slides[result.slide_key]
        new_slides = self.manifest.apply_result(result)
        if result.status != VERIFIED and result.kind == "validate":
            for key in slide.members:  # never leave a safe-to-move status behind
                entry = self.manifest.files.get(key)
                if entry and entry.status == VERIFIED:
                    entry.status = DOWNLOADED
        self.manifest.save()
        detail = f" - {result.failure.check}: {result.failure.detail}" if result.failure else ""
        what = f"archive {slide.name}" if slide.kind == KIND_ARCHIVE else slide.name
        log.info("%s: %s%s", what, result.status, detail)
        if self._on_result:
            self._on_result(slide)
        if result.kind == "unpack" or new_slides:
            self.submit_ready()  # the unpacked files may complete a slide that was waiting

    def finish(self) -> None:
        """Wait for every queued job, apply the results, stop the workers."""
        self.start()
        while self._outstanding > 0:
            result = self._results.get()
            self._apply(result)
        for _ in self._threads:
            self._jobs.put(None)
        for t in self._threads:
            t.join()
        self._threads.clear()
        self._started = False

    def abort(self) -> None:
        """Stop without waiting (Ctrl-C). Unfinished slides go back to queued on
        the next load; the manifest keeps everything already applied."""
        for _ in self._threads:
            self._jobs.put(None)
        self._started = False
