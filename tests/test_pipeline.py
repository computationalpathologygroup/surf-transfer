"""Pipeline: eligibility, overlap, single-writer, and result handling (fake runner, no subprocesses)."""

import threading
import time

from fakes import FakeRunner
from surf_transfer.manifest import Manifest
from surf_transfer.models import (
    CORRUPT,
    DOWNLOADED,
    EXTRACTED,
    UNVALIDATED,
    VERIFIED,
    ExtractedFile,
    JobResult,
    RemoteFile,
    ValidationOptions,
)
from surf_transfer.pipeline import Pipeline, build_job


def remote(fid, name, tid=1, size=10):
    return RemoteFile(
        key=f"filesender:{tid}:{fid}",
        source_id="filesender:guest:1",
        group_id=str(tid),
        group_label="Batch",
        name=name,
        size=size,
    )


def make_manifest(tmp_path, names, status=DOWNLOADED):
    m = Manifest(tmp_path / "state.json")
    m.sync_listing([remote(i, n) for i, n in enumerate(names, 1)], centre="A")
    m.sync_slides()
    for e in m.files.values():
        e.status = status
    return m


def pipeline(m, runner, tmp_path, workers=2):
    return Pipeline(m, runner, tmp_path, ValidationOptions(), workers=workers)


def test_slide_is_queued_only_once_every_member_is_downloaded(tmp_path):
    m = make_manifest(tmp_path, ["X.mrxs", "X/Slidedat.ini", "X/Data0000.dat"], status="queued")
    runner = FakeRunner()
    p = pipeline(m, runner, tmp_path)
    m.files["filesender:1:1"].status = DOWNLOADED
    m.files["filesender:1:2"].status = DOWNLOADED
    assert p.submit_ready() == 0
    m.files["filesender:1:3"].status = DOWNLOADED
    assert p.submit_ready() == 1
    p.finish()
    assert [j.name for j in runner.jobs] == ["X"]
    (slide,) = m.slides.values()
    assert slide.status == VERIFIED
    assert {e.status for e in m.files.values()} == {VERIFIED}


def test_a_slide_with_a_failed_member_is_never_queued(tmp_path):
    m = make_manifest(tmp_path, ["X.mrxs", "X/Slidedat.ini"])
    m.files["filesender:1:2"].status = "failed"
    runner = FakeRunner()
    p = pipeline(m, runner, tmp_path)
    assert p.submit_ready() == 0
    p.finish()
    assert runner.jobs == []


def test_a_slide_is_not_validated_twice(tmp_path):
    m = make_manifest(tmp_path, ["a.svs"])
    runner = FakeRunner()
    p = pipeline(m, runner, tmp_path)
    p.submit_ready()
    p.submit_ready()
    p.finish()
    p.submit_ready()
    p.finish()
    assert len(runner.jobs) == 1


def test_validation_overlaps_with_the_caller_and_does_not_block_it(tmp_path):
    m = make_manifest(tmp_path, ["a.svs"])
    gate = threading.Event()
    runner = FakeRunner(gate=gate)
    p = pipeline(m, runner, tmp_path)
    started = time.monotonic()
    p.submit_ready()
    assert time.monotonic() - started < 1.0  # returned while the job is still running
    assert p.poll() == 0
    assert m.slides[next(iter(m.slides))].status == "validating"
    gate.set()
    p.finish()
    assert m.slides[next(iter(m.slides))].status == VERIFIED


def test_workers_run_jobs_concurrently(tmp_path):
    m = make_manifest(tmp_path, ["a.svs", "b.svs", "c.svs", "d.svs"])
    runner = FakeRunner(delay=0.2)
    p = pipeline(m, runner, tmp_path, workers=2)
    p.submit_ready()
    p.finish()
    assert runner.peak == 2


def test_only_the_calling_thread_writes_the_manifest(tmp_path):
    m = make_manifest(tmp_path, ["a.svs", "b.svs"])
    writers = []
    for name in ("save", "apply_result", "begin_validation"):
        original = getattr(m, name)

        def wrapped(*a, _orig=original, _name=name, **kw):
            writers.append((_name, threading.current_thread().name))
            return _orig(*a, **kw)

        setattr(m, name, wrapped)
    runner = FakeRunner(delay=0.05)
    p = pipeline(m, runner, tmp_path)
    p.submit_ready()
    p.finish()
    assert writers
    assert {t for _, t in writers} == {threading.current_thread().name}
    assert all(t.startswith("validate-") for t in runner.threads)


def test_corrupt_slide_keeps_its_members_unverified_and_records_the_failure(tmp_path):
    m = make_manifest(tmp_path, ["bad.svs", "good.svs"])
    runner = FakeRunner(verdicts={"bad": CORRUPT})
    p = pipeline(m, runner, tmp_path)
    p.submit_ready()
    p.finish()
    by_name = {s.name: s for s in m.slides.values()}
    assert by_name["bad"].status == CORRUPT
    assert by_name["bad"].failure.check == "tile_read" and by_name["bad"].failure.coord == (1, 2)
    assert by_name["good"].status == VERIFIED
    statuses = {e.name: e.status for e in m.files.values()}
    assert statuses == {"bad.svs": DOWNLOADED, "good.svs": VERIFIED}


def test_unvalidated_slide_stays_downloaded_not_safe_to_move(tmp_path):
    m = make_manifest(tmp_path, ["x.czi"])
    p = pipeline(m, FakeRunner(verdicts={"x": UNVALIDATED}), tmp_path)
    p.submit_ready()
    p.finish()
    assert next(iter(m.slides.values())).status == UNVALIDATED
    assert next(iter(m.files.values())).status == DOWNLOADED


def test_revalidating_a_verified_slide_that_turns_out_corrupt_downgrades_its_files(tmp_path):
    m = make_manifest(tmp_path, ["a.svs"], status=VERIFIED)
    slide = next(iter(m.slides.values()))
    p = pipeline(m, FakeRunner(verdicts={"a": CORRUPT}), tmp_path)
    p.submit_slide(slide)
    p.finish()
    assert slide.status == CORRUPT
    assert next(iter(m.files.values())).status == DOWNLOADED


def test_runner_exception_becomes_unvalidated_and_does_not_hang(tmp_path):
    class Boom:
        def run(self, job):
            raise RuntimeError("kaput")

    m = make_manifest(tmp_path, ["a.svs"])
    p = pipeline(m, Boom(), tmp_path)
    p.submit_ready()
    p.finish()
    slide = next(iter(m.slides.values()))
    assert slide.status == UNVALIDATED and "kaput" in slide.failure.detail


def test_non_slide_files_are_verified_by_their_file_checks_alone(tmp_path):
    m = make_manifest(tmp_path, ["notes.txt", "a.svs"])
    runner = FakeRunner()
    p = pipeline(m, runner, tmp_path)
    p.submit_ready()
    p.finish()
    assert [j.name for j in runner.jobs] == ["a"]
    assert m.files["filesender:1:1"].status == VERIFIED
    assert m.files["filesender:1:1"].checks["slide"] == "not-a-slide"


def test_unpacked_archive_registers_files_and_queues_its_slides(tmp_path):
    m = make_manifest(tmp_path, ["z.zip"])

    class Runner(FakeRunner):
        def run(self, job):
            if job.kind == "unpack":
                self.jobs.append(job)
                return JobResult(
                    slide_key=job.slide_key,
                    kind="unpack",
                    status=VERIFIED,
                    format="zip",
                    extract_dir=job.extract_dir,
                    extracted=[
                        ExtractedFile("X.mrxs", 5, "a" * 64),
                        ExtractedFile("X/Slidedat.ini", 3, "b" * 64),
                    ],
                )
            return super().run(job)

    runner = Runner()
    p = pipeline(m, runner, tmp_path)
    p.submit_ready()
    p.finish()
    assert [(j.kind, j.name) for j in runner.jobs] == [("unpack", "z"), ("validate", "X")]
    zip_entry = m.files["filesender:1:1"]
    assert zip_entry.status == EXTRACTED and zip_entry.checks["zip_crc"] is True
    extracted = [e for e in m.files.values() if e.origin == "extracted"]
    assert sorted(e.rel_path for e in extracted) == ["Batch/X.mrxs", "Batch/X/Slidedat.ini"]
    assert all(e.status == VERIFIED and e.parent == "filesender:1:1" for e in extracted)
    slide = next(s for s in m.slides.values() if s.name == "X")
    assert slide.status == VERIFIED and slide.container == "filesender:1:1"


def test_corrupt_zip_is_recorded_and_nothing_is_extracted(tmp_path):
    m = make_manifest(tmp_path, ["z.zip"])
    p = pipeline(m, FakeRunner(verdicts={"z": CORRUPT}), tmp_path)
    p.submit_ready()
    p.finish()
    archive = next(iter(m.slides.values()))
    assert archive.status == CORRUPT and archive.kind == "archive"
    assert [e for e in m.files.values() if e.origin == "extracted"] == []


def test_build_job_describes_members_index_and_extract_dir(tmp_path):
    m = make_manifest(tmp_path, ["X.mrxs", "X/Slidedat.ini", "z.zip"])
    jobs = {s.name: build_job(m, s, tmp_path, ValidationOptions()) for s in m.slides.values()}
    assert jobs["X"].index_path == "Batch/X.mrxs"
    assert sorted(r for r, _ in jobs["X"].members) == ["Batch/X.mrxs", "Batch/X/Slidedat.ini"]
    assert jobs["z"].kind == "unpack" and jobs["z"].extract_dir == "Batch"


# --- ordering between archives and the loose files beside them -------------


def mrxs_and_zip(tmp_path, zip_name="X.zip"):
    m = Manifest(tmp_path / "state.json")
    m.sync_listing([remote(1, "X.mrxs"), remote(2, zip_name), remote(3, "other.svs")], centre="A")
    m.sync_slides()
    return m


def slide_named(m, name, kind=None):
    return next(s for s in m.slides.values() if s.name == name and (kind is None or s.kind == kind))


def test_mrxs_slide_waits_while_a_zip_beside_it_is_pending(tmp_path):
    m = mrxs_and_zip(tmp_path)
    for key in ("filesender:1:1", "filesender:1:3"):
        m.files[key].status = DOWNLOADED  # the zip has not arrived yet
    assert not m.slide_ready(slide_named(m, "X", "mrxs"))
    assert m.slide_ready(slide_named(m, "other"))  # a single-file slide is never blocked
    m.files["filesender:1:2"].status = DOWNLOADED
    assert not m.slide_ready(slide_named(m, "X", "mrxs"))  # downloaded but not unpacked yet
    slide_named(m, "X", "archive").status = VERIFIED
    assert m.slide_ready(slide_named(m, "X", "mrxs"))


def test_a_corrupt_zip_does_not_block_the_slide_forever(tmp_path):
    m = mrxs_and_zip(tmp_path)
    for e in m.files.values():
        e.status = DOWNLOADED
    slide_named(m, "X", "archive").status = CORRUPT
    assert m.slide_ready(slide_named(m, "X", "mrxs"))


def test_zip_unpacks_only_after_the_loose_files_beside_it_are_downloaded(tmp_path):
    m = mrxs_and_zip(tmp_path, zip_name="A.zip")
    m.files["filesender:1:2"].status = DOWNLOADED  # zip first, loose files still queued
    assert not m.slide_ready(slide_named(m, "A", "archive"))
    for key in ("filesender:1:1", "filesender:1:3"):
        m.files[key].status = DOWNLOADED
    assert m.slide_ready(slide_named(m, "A", "archive"))


def test_a_zip_in_another_folder_does_not_block_anything(tmp_path):
    m = Manifest(tmp_path / "state.json")
    m.sync_listing([remote(1, "d1/X.mrxs"), remote(2, "d2/z.zip")], centre="A")
    m.sync_slides()
    m.files["filesender:1:1"].status = DOWNLOADED
    assert m.slide_ready(slide_named(m, "X", "mrxs"))


class UnpackRunner(FakeRunner):
    def run(self, job):
        if job.kind == "unpack":
            self.jobs.append(job)
            return JobResult(
                slide_key=job.slide_key,
                kind="unpack",
                status=VERIFIED,
                format="zip",
                extract_dir=job.extract_dir,
                extracted=[ExtractedFile("X.tif", 5, "a" * 64)],
            )
        return super().run(job)


def zip_on_disk(tmp_path):
    m = make_manifest(tmp_path, ["z.zip"])
    (tmp_path / "Batch").mkdir()
    (tmp_path / "Batch" / "z.zip").write_bytes(b"x" * 10)  # size 10 matches the listing
    return m


def test_zip_is_deleted_once_its_slide_is_settled_even_when_corrupt(tmp_path):
    m = zip_on_disk(tmp_path)
    p = pipeline(m, UnpackRunner(verdicts={"X": CORRUPT}), tmp_path)
    p.submit_ready()
    p.finish()
    zip_entry = m.files["filesender:1:1"]
    assert not (tmp_path / "Batch" / "z.zip").exists()
    assert zip_entry.status == EXTRACTED and zip_entry.size == 10 and zip_entry.removed_at
    assert next(s for s in m.slides.values() if s.name == "X").status == CORRUPT


def test_zip_stays_while_a_slide_it_fed_has_no_verdict(tmp_path):
    m = zip_on_disk(tmp_path)
    p = pipeline(m, UnpackRunner(), tmp_path)
    p.start()
    p.submit_ready()
    # Apply only the unpack result: the extracted slide is queued, not decided.
    p._apply(p._results.get(timeout=5))
    assert (tmp_path / "Batch" / "z.zip").exists()
    assert m.files["filesender:1:1"].status == VERIFIED
    p.finish()
    assert not (tmp_path / "Batch" / "z.zip").exists()


def test_keep_zips_keeps_the_file_and_the_verified_state(tmp_path):
    m = zip_on_disk(tmp_path)
    p = Pipeline(m, UnpackRunner(), tmp_path, ValidationOptions(), keep_zips=True)
    p.submit_ready()
    p.finish()
    assert (tmp_path / "Batch" / "z.zip").exists()
    assert m.files["filesender:1:1"].status == VERIFIED


def test_deleted_zip_survives_reconcile_and_a_save_load_round_trip(tmp_path):
    m = zip_on_disk(tmp_path)
    p = pipeline(m, UnpackRunner(), tmp_path)
    p.submit_ready()
    p.finish()
    m.save()
    again = Manifest.load(tmp_path / "state.json")
    again.reconcile_local(tmp_path)
    entry = again.files["filesender:1:1"]
    assert entry.status == EXTRACTED and entry.sha256 == m.files["filesender:1:1"].sha256
    assert entry.removed_at == m.files["filesender:1:1"].removed_at
    assert not again.needs_download("filesender:1:1")


def test_zip_whose_size_changed_on_disk_is_not_deleted(tmp_path):
    m = zip_on_disk(tmp_path)
    (tmp_path / "Batch" / "z.zip").write_bytes(b"someone else's file")
    p = pipeline(m, UnpackRunner(), tmp_path)
    p.submit_ready()
    p.finish()
    assert (tmp_path / "Batch" / "z.zip").exists()
    assert m.files["filesender:1:1"].status == VERIFIED
