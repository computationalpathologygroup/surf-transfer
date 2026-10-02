"""The whole loop with an in-memory source and a fake validator: no network, no subprocesses."""

import json

import pytest

from fakes import FakeRunner, FakeSource
from surf_transfer import app, report
from surf_transfer.config import RunOptions
from surf_transfer.manifest import Manifest, SourceScopeError
from surf_transfer.models import (
    ARCHIVED,
    CORRUPT,
    DOWNLOADED,
    FAILED,
    MOVED,
    QUEUED,
    UNVALIDATED,
    VERIFIED,
)
from surf_transfer.sources import SourceError

BIG = lambda p: 10**15  # noqa: E731 - plenty of free space


def options(tmp_path, **kw):
    kw.setdefault("min_free_bytes", 0)
    return RunOptions(output_dir=tmp_path / "out", state_file=tmp_path / "out" / "state.json", **kw)


def do_run(tmp_path, source, runner=None, free=BIG, **kw):
    opts = options(tmp_path, **kw)
    manifest = Manifest.load(opts.state_file)
    runner = runner or FakeRunner()
    summary = app.run(manifest, [source], opts, runner, free_bytes=free)
    return manifest, summary, runner


FILES = {"a.svs": b"A" * 100, "b.svs": b"B" * 100, "notes.txt": b"hello"}


def test_fresh_run_downloads_checks_and_validates_everything(tmp_path):
    source = FakeSource(FILES)
    manifest, summary, runner = do_run(tmp_path, source)
    assert summary.downloaded == 3 and summary.failed == 0
    assert {e.status for e in manifest.files.values()} == {VERIFIED}
    a = next(e for e in manifest.files.values() if e.name == "a.svs")
    assert a.checks["size"] is True and a.checks["source_checksum"] == "absent"
    assert len(a.checks["local_sha256"]) == 64 and a.checks["slide"].startswith("slide:")
    assert {s.name for s in manifest.slides.values()} == {"a", "b"}  # notes.txt is not a slide
    assert (tmp_path / "out" / "Folder G1" / "a.svs").read_bytes() == FILES["a.svs"]
    assert report.exit_code(manifest) == report.EXIT_OK


def test_provenance_is_recorded(tmp_path):
    manifest, _, _ = do_run(tmp_path, FakeSource(FILES, centre="Centre A"))
    slide = next(s for s in manifest.slides.values() if s.name == "a")
    assert slide.centre == "Centre A" and slide.source_id == "fake:s"
    assert slide.first_seen and slide.received_at and slide.validated_at


def test_manifest_on_disk_matches_and_second_run_downloads_nothing(tmp_path):
    source = FakeSource(FILES)
    do_run(tmp_path, source)
    first_opened = list(source.opened)
    manifest, summary, runner = do_run(tmp_path, source)
    assert source.opened == first_opened and summary.downloaded == 0 and runner.jobs == []
    assert json.loads((tmp_path / "out" / "state.json").read_text())["version"] == 2


def test_files_moved_out_by_hand_are_marked_moved_and_never_redownloaded(tmp_path):
    source = FakeSource(FILES)
    do_run(tmp_path, source)
    for p in (tmp_path / "out" / "Folder G1").iterdir():
        p.unlink()  # "moved" elsewhere
    source.opened.clear()
    manifest, summary, _ = do_run(tmp_path, source)
    assert source.opened == []
    assert {e.status for e in manifest.files.values()} == {MOVED}
    assert {s.status for s in manifest.slides.values()} == {MOVED}


def test_archive_dir_resolves_moved_files_to_archived(tmp_path):
    source = FakeSource(FILES)
    do_run(tmp_path, source)
    archive = tmp_path / "archive"
    archive.mkdir()
    for p in (tmp_path / "out" / "Folder G1").iterdir():
        p.rename(archive / p.name)
    source.opened.clear()
    manifest, _, _ = do_run(tmp_path, source, archive_dir=archive)
    assert source.opened == []
    assert {e.status for e in manifest.files.values()} == {ARCHIVED}
    assert {s.status for s in manifest.slides.values()} == {ARCHIVED}


def test_budget_stop_defers_files_and_gives_exit_code_4(tmp_path):
    manifest, summary, _ = do_run(tmp_path, FakeSource(FILES), max_files=1)
    assert summary.downloaded == 1 and summary.deferred == 2 and "max-files" in summary.budget_stop
    assert report.exit_code(manifest, budget_stopped=True) == report.EXIT_BUDGET
    manifest2, summary2, _ = do_run(tmp_path, FakeSource(FILES), max_files=10)
    assert summary2.downloaded == 2 and {e.status for e in manifest2.files.values()} == {VERIFIED}


def test_failed_download_is_retried_and_reports_exit_code_2(tmp_path):
    source = FakeSource(FILES)
    source.corrupt_in_flight.add("a.svs")
    manifest, summary, _ = do_run(tmp_path, source)
    assert summary.failed == 1
    a = next(e for e in manifest.files.values() if e.name == "a.svs")
    assert a.status == FAILED and "size mismatch" in a.error
    assert not (tmp_path / "out" / "Folder G1" / "a.svs").exists()
    assert report.exit_code(manifest) == report.EXIT_FAILED
    source.corrupt_in_flight.clear()
    manifest, summary, _ = do_run(tmp_path, source)
    assert summary.downloaded == 1 and a.name == "a.svs"
    assert report.exit_code(manifest) == report.EXIT_OK


def test_free_space_preflight_refuses_a_file_that_will_not_fit(tmp_path):
    source = FakeSource(FILES)
    manifest, summary, _ = do_run(tmp_path, source, free=lambda p: 50)
    assert source.opened == ["notes.txt"]  # the 5-byte file fits, the 100-byte slides do not
    assert summary.no_space == 2 and summary.downloaded == 1
    a = next(e for e in manifest.files.values() if e.name == "a.svs")
    assert a.status == QUEUED and "not enough free space" in a.error
    assert report.exit_code(manifest) == report.EXIT_FAILED


def test_free_space_preflight_honours_the_reserve(tmp_path):
    source = FakeSource({"a.svs": b"x" * 100})
    _, summary, _ = do_run(tmp_path, source, free=lambda p: 150, min_free_bytes=100)
    assert summary.no_space == 1 and source.opened == []


def test_file_appearing_after_mark_complete_is_flagged_and_not_downloaded(tmp_path):
    source = FakeSource({"a.svs": b"A" * 10})
    manifest, _, _ = do_run(tmp_path, source)
    manifest.mark_complete("Fake Centre", note="done", date="2026-10-01")
    manifest.save()
    source.files["late.svs"] = b"L" * 10
    source.opened.clear()
    manifest, summary, _ = do_run(tmp_path, source)
    assert summary.flagged == 1 and source.opened == []
    late = next(e for e in manifest.files.values() if e.name == "late.svs")
    assert late.flagged and late.status == QUEUED
    assert "marked complete" in report.format_status(report.status_data(manifest))
    assert report.exit_code(manifest) == report.EXIT_FAILED
    manifest, summary, _ = do_run(tmp_path, source, accept_flagged=True)
    assert summary.downloaded == 1 and late.name == "late.svs"
    assert {e.status for e in manifest.files.values()} == {VERIFIED}


def test_source_changed_after_download_is_flagged_not_silently_replaced(tmp_path):
    source = FakeSource({"a.svs": b"A" * 10})
    do_run(tmp_path, source)
    source.files["a.svs"] = b"changed content!"
    source.opened.clear()
    manifest, summary, _ = do_run(tmp_path, source)
    entry = next(iter(manifest.files.values()))
    assert source.opened == [] and "changed on the source" in entry.flagged
    assert entry.status == VERIFIED


def test_force_redownloads_everything(tmp_path):
    source = FakeSource(FILES)
    do_run(tmp_path, source)
    source.opened.clear()
    manifest, summary, runner = do_run(tmp_path, source, force=True)
    assert sorted(source.opened) == sorted(FILES)
    assert {s.status for s in manifest.slides.values()} == {VERIFIED}
    assert len(runner.jobs) == 2  # slides were re-validated after being re-downloaded


def test_recheck_hashes_catches_silent_corruption_of_same_size(tmp_path):
    source = FakeSource({"a.svs": b"A" * 100})
    do_run(tmp_path, source)
    (tmp_path / "out" / "Folder G1" / "a.svs").write_bytes(b"Z" * 100)
    source.opened.clear()
    do_run(tmp_path, source)
    assert source.opened == []  # trusted the manifest
    manifest, _, _ = do_run(tmp_path, source, recheck_hashes=True)
    assert source.opened == ["a.svs"]
    assert (tmp_path / "out" / "Folder G1" / "a.svs").read_bytes() == b"A" * 100


def test_dry_run_syncs_the_manifest_but_downloads_nothing(tmp_path):
    source = FakeSource(FILES)
    manifest, summary, runner = do_run(tmp_path, source, dry_run=True)
    assert source.opened == [] and runner.jobs == []
    assert {e.status for e in manifest.files.values()} == {QUEUED}
    assert len(Manifest.load(tmp_path / "out" / "state.json").files) == 3


def test_a_source_that_cannot_be_listed_is_reported_and_others_continue(tmp_path):
    bad = FakeSource({}, source_id="fake:bad")
    bad.list_error = SourceError("401")
    good = FakeSource(FILES, source_id="fake:good", group="G2")
    opts = options(tmp_path)
    manifest = Manifest.load(opts.state_file)
    summary = app.run(manifest, [bad, good], opts, FakeRunner(), free_bytes=BIG)
    assert summary.listing_errors == ["401"] and summary.downloaded == 3
    assert report.exit_code(manifest, listing_errors=1) == report.EXIT_FAILED


def test_two_backends_share_one_manifest(tmp_path):
    a = FakeSource(
        {"x.svs": b"x" * 10},
        source_id="filesender:guest:1",
        group="1",
        keys={"x.svs": "filesender:1:1"},
    )
    b = FakeSource(
        {"y.svs": b"y" * 10},
        source_id="surfdrive:tok",
        group="tok",
        label="share",
        keys={"y.svs": "surfdrive:tok:77"},
    )
    opts = options(tmp_path)
    manifest = Manifest.load(opts.state_file)
    app.run(manifest, [a, b], opts, FakeRunner(), free_bytes=BIG)
    assert set(manifest.files) == {"filesender:1:1", "surfdrive:tok:77"}
    assert {s.source_id for s in manifest.slides.values()} == {
        "filesender:guest:1",
        "surfdrive:tok",
    }
    assert {src["kind"] for src in manifest.sources.values()} == {"filesender", "surfdrive"}


# --- verdicts and exit codes -------------------------------------------------


def test_corrupt_slide_gives_exit_code_3_and_names_the_reason(tmp_path):
    manifest, _, _ = do_run(tmp_path, FakeSource(FILES), runner=FakeRunner(verdicts={"a": CORRUPT}))
    slide = next(s for s in manifest.slides.values() if s.name == "a")
    assert slide.status == CORRUPT and slide.failure.coord == (1, 2)
    assert report.exit_code(manifest) == report.EXIT_CORRUPT
    text = report.format_status(report.status_data(manifest))
    assert "CORRUPT (1)" in text and "tile_read" in text and "a.svs" in text


def test_unvalidated_slide_gives_exit_code_5_and_is_listed_separately(tmp_path):
    manifest, _, _ = do_run(
        tmp_path, FakeSource(FILES), runner=FakeRunner(verdicts={"a": UNVALIDATED})
    )
    assert report.exit_code(manifest) == report.EXIT_UNVALIDATED
    data = report.status_data(manifest)
    assert [u["slide"] for u in data["unvalidated"]] == ["a"]
    assert "UNVALIDATED" in report.format_status(data)


def test_only_verified_slides_count_as_safe_to_move(tmp_path):
    runner = FakeRunner(verdicts={"a": CORRUPT, "b": UNVALIDATED})
    source = FakeSource({**FILES, "c.svs": b"C" * 100})
    manifest, _, _ = do_run(tmp_path, source, runner=runner)
    data = report.status_data(manifest)
    assert data["safe_to_move"] == {"count": 1, "bytes": 100}
    assert data["slides"][CORRUPT]["count"] == 1 and data["slides"][UNVALIDATED]["count"] == 1


# --- re-send history ---------------------------------------------------------


def test_resend_links_corrupt_original_to_the_verified_replacement(tmp_path):
    first = FakeSource({"a.svs": b"A" * 10}, group="T1", keys={"a.svs": "fake:T1:1"})
    do_run(tmp_path, first, runner=FakeRunner(verdicts={"a": CORRUPT}))
    second = FakeSource({"a.svs": b"A" * 11}, group="T2", keys={"a.svs": "fake:T2:9"})
    manifest, _, _ = do_run(tmp_path, second)
    old, new = sorted(manifest.slides.values(), key=lambda s: manifest.order_key(s))
    assert (old.status, new.status) == (CORRUPT, VERIFIED)
    assert new.replaces == [old.key] and old.superseded_by == [new.key]
    assert report.exit_code(manifest) == report.EXIT_OK  # only the latest delivery counts
    rows = report.report_rows(manifest)
    assert "corrupt (tile_read)" in rows[-1]["history"] and "verified" in rows[-1]["history"]
    assert report.status_data(manifest)["slides"][VERIFIED]["count"] == 1


# --- revalidate ----------------------------------------------------------------


def test_revalidate_rechecks_files_on_disk_without_downloading(tmp_path):
    source = FakeSource({"a.svs": b"A" * 10})
    opts = options(tmp_path)
    manifest, _, _ = do_run(tmp_path, source, runner=FakeRunner(verdicts={"a": CORRUPT}))
    source.opened.clear()
    queued, skipped = app.revalidate(manifest, opts, FakeRunner())
    assert (queued, skipped) == (1, 0) and source.opened == []
    slide = next(iter(manifest.slides.values()))
    assert slide.status == VERIFIED
    assert next(iter(manifest.files.values())).status == VERIFIED


def test_revalidate_skips_slides_that_were_moved_away(tmp_path):
    source = FakeSource({"a.svs": b"A" * 10})
    manifest, _, _ = do_run(tmp_path, source, runner=FakeRunner(verdicts={"a": UNVALIDATED}))
    (tmp_path / "out" / "Folder G1" / "a.svs").unlink()
    queued, skipped = app.revalidate(manifest, options(tmp_path), FakeRunner())
    assert (queued, skipped) == (0, 1)


def test_revalidate_leaves_verified_slides_alone_unless_named(tmp_path):
    manifest, _, _ = do_run(tmp_path, FakeSource({"a.svs": b"A" * 10}))
    runner = FakeRunner()
    assert app.revalidate(manifest, options(tmp_path), runner)[0] == 0
    assert app.revalidate(manifest, options(tmp_path, slide_filter=("a",)), runner)[0] == 1


def test_revalidate_runs_legacy_files_through_slide_validation(tmp_path):
    out = tmp_path / "out"
    (out / "Batch").mkdir(parents=True)
    (out / "Batch" / "f1.svs").write_bytes(b"x" * 100)
    v1 = {
        "version": 1,
        "base_url": "u",
        "files": {
            "1:1": {
                "transfer_id": 1,
                "file_id": 1,
                "transfer_subject": "Batch",
                "name": "f1.svs",
                "rel_path": "Batch/f1.svs",
                "size": 100,
                "sha256": "ab" * 32,
                "status": "verified",
                "downloaded_at": "2025-01-01T00:00:00Z",
                "verified_at": "2025-01-01T00:00:00Z",
            }
        },
    }
    (out / "state.json").write_text(json.dumps(v1))
    opts = options(tmp_path)
    manifest = Manifest.load(opts.state_file)
    data = report.status_data(manifest)
    assert data["legacy_files"] == 1
    manifest.sync_slides()
    assert app.revalidate(manifest, opts, FakeRunner())[0] == 1
    entry = manifest.files["filesender:1:1"]
    assert entry.status == VERIFIED and entry.checks["legacy"] is True and "slide" in entry.checks


def test_migrated_v1_manifest_does_not_redownload_anything_already_accounted_for(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    entries = {}
    for fid, status in ((1, "verified"), (2, "moved"), (3, "archived")):
        entries[f"7:{fid}"] = {
            "transfer_id": 7,
            "file_id": fid,
            "transfer_subject": "Batch",
            "name": f"s{fid}.svs",
            "rel_path": f"Batch/s{fid}.svs",
            "size": 10,
            "sha256": "cd" * 32,
            "status": status,
        }
    (out / "Batch").mkdir()
    (out / "Batch" / "s1.svs").write_bytes(b"x" * 10)
    (out / "state.json").write_text(json.dumps({"version": 1, "base_url": "u", "files": entries}))
    source = FakeSource(
        {"s1.svs": b"x" * 10, "s2.svs": b"y" * 10, "s3.svs": b"z" * 10, "s4.svs": b"w" * 10},
        source_id="filesender:guest:5",
        group="7",
        label="Batch",
        keys={f"s{i}.svs": f"filesender:7:{i}" for i in range(1, 5)},
    )
    manifest, summary, _ = do_run(tmp_path, source)
    assert source.opened == ["s4.svs"]
    assert manifest.files["filesender:7:2"].status == MOVED
    assert manifest.files["filesender:7:3"].status == ARCHIVED
    assert manifest.files["filesender:7:1"].checks["legacy"] is True
    assert (
        manifest.files["filesender:7:1"].source_id == "filesender:guest:5"
    )  # claimed by its guest
    backup = out / "state.json.v1.bak"
    assert json.loads(backup.read_text())["version"] == 1


def test_interrupted_run_leaves_downloaded_slides_that_the_next_run_validates(tmp_path):
    source = FakeSource({"a.svs": b"A" * 10})
    opts = options(tmp_path)
    manifest = Manifest.load(opts.state_file)
    manifest.sync_listing(source.list(), centre="c")
    manifest.sync_slides()
    entry = next(iter(manifest.files.values()))
    local = opts.output_dir / entry.rel_path
    local.parent.mkdir(parents=True)
    local.write_bytes(b"A" * 10)
    entry.status, entry.sha256 = DOWNLOADED, "ab" * 32
    manifest.save()
    manifest, summary, runner = do_run(tmp_path, source)
    assert source.opened == [] and [j.name for j in runner.jobs] == ["a"]
    assert next(iter(manifest.slides.values())).status == VERIFIED


# --- source guard: a manifest belongs to the source it was created for ---


def drive_source(folder, files=None, group="G1"):
    from surf_transfer.config import SurfDriveConfig
    from surf_transfer.sources import SurfDriveSource

    cfg = SurfDriveConfig(
        mode="public",
        base_url="https://surfdrive.surf.nl",
        name="link",
        username="TOKEN",
        remote_folder=folder,
    )
    fake = FakeSource(files or FILES, group=group)
    fake.scope_id = SurfDriveSource(cfg, client=None).scope_id  # type: ignore[arg-type]
    return fake


def test_same_source_passes_again_and_is_recorded(tmp_path):
    do_run(tmp_path, drive_source("40x/A"))
    manifest, summary, _ = do_run(tmp_path, drive_source("40x/A"))
    assert summary.downloaded == 0
    assert manifest.source_scope == ["surfdrive:public:https://surfdrive.surf.nl:TOKEN:dir=40x/A"]
    assert json.loads((tmp_path / "out" / "state.json").read_text())["source_scope"]


def test_a_different_surfdrive_dir_is_refused_before_listing(tmp_path):
    do_run(tmp_path, drive_source("40x/A"))
    other = drive_source("40x/B")
    opened_before = list(other.opened)
    opts = options(tmp_path)
    manifest = Manifest.load(opts.state_file)
    with pytest.raises(SourceScopeError) as err:
        app.run(manifest, [other], opts, FakeRunner(), free_bytes=BIG)
    message = str(err.value)
    assert "dir=40x/A" in message and "dir=40x/B" in message  # names both
    assert "fresh -o" in message and "--allow-source-change" in message
    assert manifest.sources == Manifest.load(opts.state_file).sources  # nothing listed or synced
    assert other.opened == opened_before
    assert manifest.source_scope == ["surfdrive:public:https://surfdrive.surf.nl:TOKEN:dir=40x/A"]


def test_allow_source_change_overrides_the_guard(tmp_path):
    do_run(tmp_path, drive_source("40x/A"))
    manifest, summary, _ = do_run(
        tmp_path, drive_source("40x/B", {"c.svs": b"C" * 50}, group="G2"), allow_source_change=True
    )
    assert summary.downloaded == 1
    assert len(manifest.source_scope) == 2


def test_manifest_without_a_recorded_source_adopts_the_current_one(tmp_path):
    do_run(tmp_path, drive_source("40x/A"))
    state = tmp_path / "out" / "state.json"
    data = json.loads(state.read_text())
    del data["source_scope"]  # a manifest written before the guard existed
    state.write_text(json.dumps(data))
    manifest, summary, _ = do_run(tmp_path, drive_source("40x/B"))
    assert summary.failed == 0
    assert manifest.source_scope == ["surfdrive:public:https://surfdrive.surf.nl:TOKEN:dir=40x/B"]
    with pytest.raises(SourceScopeError):
        do_run(tmp_path, drive_source("40x/A"))


def test_filesender_scope_is_the_instance_and_guest(tmp_path):
    from surf_transfer.config import FileSenderConfig
    from surf_transfer.sources import FileSenderSource

    cfg = FileSenderConfig(base_url="https://fs.example/rest.php", username="u", apikey="k")
    a = FileSenderSource(cfg, client=None, guest_id=3)  # type: ignore[arg-type]
    b = FileSenderSource(cfg, client=None, guest_email="G@X.nl")  # type: ignore[arg-type]
    assert a.scope_id == "filesender:https://fs.example/rest.php:guest:3"
    assert b.scope_id == "filesender:https://fs.example/rest.php:guest-email:g@x.nl"
