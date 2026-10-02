"""End to end with real worker subprocesses and real openslide on synthetic slides.
Skipped when openslide or tifffile is not installed."""

import io
import zipfile

import pytest

from fakes import FakeSource
from slides import make_flat_tiff, make_pyramidal_tiff
from surf_transfer import app, report
from surf_transfer.config import RunOptions
from surf_transfer.manifest import Manifest
from surf_transfer.models import CORRUPT, EXTRACTED, MOVED, UNVALIDATED, VERIFIED
from surf_transfer.validation import SlideValidator

pytest.importorskip("openslide")
pytest.importorskip("tifffile")


def read(path):
    return path.read_bytes()


def zip_bytes(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


def test_end_to_end_mixed_delivery(tmp_path):
    src = tmp_path / "src"
    good = read(make_pyramidal_tiff(src / "good.tif"))
    truncated = good[: len(good) // 3]
    flat = read(make_flat_tiff(src / "flat.tif"))
    bundle = zip_bytes({"inner.tif": good, "readme.txt": b"hello"})
    broken_zip = bytearray(bundle)
    broken_zip[300] ^= 0xFF

    source = FakeSource(
        {
            "good.tif": good,
            "truncated.tif": truncated,
            "flat.tif": flat,
            "bundle.zip": bundle,
            "notes.txt": b"hi",
        }
    )
    opts = RunOptions(
        output_dir=tmp_path / "out",
        state_file=tmp_path / "out" / "state.json",
        min_free_bytes=0,
        sample_tiles=8,
        validate_workers=2,
        validate_timeout=60,
        seed=5,
    )
    manifest = Manifest.load(opts.state_file)
    summary = app.run(manifest, [source], opts, SlideValidator(), free_bytes=lambda p: 10**15)
    assert summary.downloaded == 5 and summary.failed == 0

    by_name = {s.name: s for s in manifest.slides.values() if s.kind != "archive"}
    assert by_name["good"].status == VERIFIED and by_name["good"].seed == 5
    assert by_name["good"].metadata["dimensions"] == [2048, 1536]
    assert by_name["truncated"].status == CORRUPT
    assert by_name["flat"].status == UNVALIDATED
    assert by_name["inner"].status == VERIFIED and by_name["inner"].container
    (archive,) = manifest.archive_units()
    assert archive.status == EXTRACTED
    assert not (tmp_path / "out" / "Folder G1" / "bundle.zip").exists()
    assert (tmp_path / "out" / "Folder G1" / "inner.tif").exists()
    extracted = {e.name: e for e in manifest.files.values() if e.origin == "extracted"}
    assert set(extracted) == {"inner.tif", "readme.txt"}
    assert extracted["readme.txt"].status == VERIFIED  # non-slide: file checks only
    assert report.exit_code(manifest) == report.EXIT_CORRUPT

    data = report.status_data(manifest)
    assert [c["slide"] for c in data["corrupt"]] == ["truncated"]
    assert [u["slide"] for u in data["unvalidated"]] == ["flat"]
    rows = {r["slide"]: r for r in report.report_rows(manifest)}
    assert rows["truncated"]["failure_files"].endswith("truncated.tif")
    assert rows["good"]["checks"].startswith("detect_format:ok; open:ok; lowres_read:ok")


def test_damaged_zip_is_corrupt_and_extracts_nothing(tmp_path):
    good = read(make_pyramidal_tiff(tmp_path / "g.tif"))
    bundle = bytearray(zip_bytes({"inner.tif": good}))
    bundle[len(bundle) // 2] ^= 0xFF
    source = FakeSource({"bundle.zip": bytes(bundle)})
    opts = RunOptions(
        output_dir=tmp_path / "out",
        state_file=tmp_path / "out" / "state.json",
        min_free_bytes=0,
        validate_timeout=60,
    )
    manifest = Manifest.load(opts.state_file)
    app.run(manifest, [source], opts, SlideValidator(), free_bytes=lambda p: 10**15)
    (archive,) = manifest.archive_units()
    assert archive.status == CORRUPT and archive.failure.check == "zip"
    assert (tmp_path / "out" / "Folder G1" / "bundle.zip").exists()  # failed extract: kept
    assert archive.failure.members == ("Folder G1/bundle.zip",)
    assert not list((tmp_path / "out").rglob("*.part"))
    assert report.exit_code(manifest) == report.EXIT_CORRUPT


def test_resend_replaces_a_corrupt_slide_and_history_is_readable(tmp_path):
    good = read(make_pyramidal_tiff(tmp_path / "g.tif"))
    opts = RunOptions(
        output_dir=tmp_path / "out",
        state_file=tmp_path / "out" / "state.json",
        min_free_bytes=0,
        sample_tiles=4,
        validate_timeout=60,
    )
    manifest = Manifest.load(opts.state_file)
    first = FakeSource({"S1.tif": good[: len(good) // 3]}, group="T1", keys={"S1.tif": "fake:T1:1"})
    app.run(manifest, [first], opts, SlideValidator(), free_bytes=lambda p: 10**15)
    assert report.exit_code(manifest) == report.EXIT_CORRUPT
    second = FakeSource({"S1.tif": good}, group="T2", keys={"S1.tif": "fake:T2:5"})
    app.run(manifest, [first, second], opts, SlideValidator(), free_bytes=lambda p: 10**15)
    assert report.exit_code(manifest) == report.EXIT_OK
    row = [r for r in report.report_rows(manifest) if "superseded" not in r["slide"]][0]
    assert "corrupt (header)" in row["history"] or "corrupt (" in row["history"]
    assert row["status"] == "verified"


# --- a loose .mrxs plus a zipped data folder: the delivery the live test share uses ---

SLIDEDAT = "[DATAFILE]\nFILE_COUNT = {n}\n" + "".join(
    f"FILE_{i} = Data{i:04d}.dat\n" for i in range(5)
)


def mrxs_delivery(declared: int, dats: int, zip_name: str):
    folder = {"X/Slidedat.ini": SLIDEDAT.format(n=declared).encode()}
    folder.update({f"X/Data{i:04d}.dat": b"dat" for i in range(dats)})
    return {"X.mrxs": b"index file", zip_name: zip_bytes(folder)}


@pytest.mark.parametrize("zip_name", ["X.zip", "A.zip"])  # zip sorts after / before the .mrxs
def test_loose_mrxs_plus_zipped_folder_is_one_slide_and_is_checked_once(tmp_path, zip_name):
    source = FakeSource(mrxs_delivery(declared=3, dats=3, zip_name=zip_name))
    opts = RunOptions(
        output_dir=tmp_path / "out",
        state_file=tmp_path / "out" / "state.json",
        min_free_bytes=0,
        validate_timeout=60,
    )
    manifest = Manifest.load(opts.state_file)
    app.run(manifest, [source], opts, SlideValidator(), free_bytes=lambda p: 10**15)

    slides = [s for s in manifest.slides.values() if s.kind != "archive"]
    assert [s.name for s in slides] == ["X"]  # not two half-slides
    slide = slides[0]
    assert slide.replaces == [] and slide.superseded_by == []  # a zipped twin is not a re-send
    members = sorted(manifest.member_paths(slide))
    assert members == [
        "Folder G1/X.mrxs",
        "Folder G1/X/Data0000.dat",
        "Folder G1/X/Data0001.dat",
        "Folder G1/X/Data0002.dat",
        "Folder G1/X/Slidedat.ini",
    ]
    # Completeness passed (every declared .dat arrived); only openslide rejects the fake bytes.
    assert slide.failure.check == "header"
    assert [c.name for c in slide.checks if c.passed] == [
        "index_file",
        "slidedat_parse",
        "dat_missing",
        "dat_unexpected",
    ]
    assert slide.container is not None
    (archive,) = manifest.archive_units()
    assert archive.status == EXTRACTED  # the slide is corrupt, but the zip adds no evidence
    assert not (tmp_path / "out" / "Folder G1" / zip_name).exists()


def test_zipped_folder_with_a_missing_dat_names_the_file_and_the_zip_members(tmp_path):
    source = FakeSource(mrxs_delivery(declared=4, dats=3, zip_name="X.zip"))
    opts = RunOptions(
        output_dir=tmp_path / "out",
        state_file=tmp_path / "out" / "state.json",
        min_free_bytes=0,
        validate_timeout=60,
    )
    manifest = Manifest.load(opts.state_file)
    app.run(manifest, [source], opts, SlideValidator(), free_bytes=lambda p: 10**15)
    (slide,) = [s for s in manifest.slides.values() if s.kind == "mrxs"]
    assert slide.status == CORRUPT and slide.failure.check == "missing_dat"
    assert slide.failure.members == ("Folder G1/X/Data0003.dat",)
    assert report.exit_code(manifest) == report.EXIT_CORRUPT


def test_zip_member_that_differs_from_a_loose_file_is_reported_not_overwritten(tmp_path):
    source = FakeSource(
        {
            "X/Data0000.dat": b"loose original",
            "bundle.zip": zip_bytes({"X/Data0000.dat": b"different"}),
        }
    )
    opts = RunOptions(
        output_dir=tmp_path / "out",
        state_file=tmp_path / "out" / "state.json",
        min_free_bytes=0,
        validate_timeout=60,
    )
    manifest = Manifest.load(opts.state_file)
    app.run(manifest, [source], opts, SlideValidator(), free_bytes=lambda p: 10**15)
    (archive,) = manifest.archive_units()
    assert archive.status == CORRUPT and "would overwrite" in archive.failure.detail
    assert (tmp_path / "out" / "Folder G1" / "X" / "Data0000.dat").read_bytes() == b"loose original"
    assert (
        tmp_path / "out" / "Folder G1" / "bundle.zip"
    ).exists()  # conflict: the zip is the evidence


# --- the tool deletes a zip once its contents are extracted and settled ---


def zip_opts(tmp_path, **kw):
    return RunOptions(
        output_dir=tmp_path / "out",
        state_file=tmp_path / "out" / "state.json",
        min_free_bytes=0,
        sample_tiles=4,
        validate_timeout=60,
        **kw,
    )


def run_once(manifest, source, opts):
    return app.run(manifest, [source], opts, SlideValidator(), free_bytes=lambda p: 10**15)


def test_extracted_zip_is_deleted_and_recorded_with_size_and_hash(tmp_path):
    good = read(make_pyramidal_tiff(tmp_path / "g.tif"))
    bundle = zip_bytes({"inner.tif": good})
    opts = zip_opts(tmp_path)
    manifest = Manifest.load(opts.state_file)
    run_once(manifest, FakeSource({"bundle.zip": bundle}), opts)

    folder = tmp_path / "out" / "Folder G1"
    assert not (folder / "bundle.zip").exists() and (folder / "inner.tif").exists()
    zip_entry = next(e for e in manifest.files.values() if e.origin == "remote")
    assert zip_entry.status == EXTRACTED
    assert zip_entry.size == len(bundle) and zip_entry.sha256
    assert zip_entry.removed_at
    (archive,) = manifest.archive_units()
    assert archive.status == EXTRACTED
    inner = next(s for s in manifest.slides.values() if s.name == "inner")
    assert inner.status == VERIFIED


def test_rerun_after_deletion_neither_redownloads_nor_marks_moved(tmp_path):
    good = read(make_pyramidal_tiff(tmp_path / "g.tif"))
    source = FakeSource({"bundle.zip": zip_bytes({"inner.tif": good})})
    opts = zip_opts(tmp_path)
    manifest = Manifest.load(opts.state_file)
    run_once(manifest, source, opts)
    assert source.opened == ["bundle.zip"]

    for _ in range(2):  # fresh load from disk each time, as a real rerun does
        manifest = Manifest.load(opts.state_file)
        summary = run_once(manifest, source, opts)
        assert summary.downloaded == 0 and summary.failed == 0
        assert source.opened == ["bundle.zip"]  # still only the first download
        assert not (tmp_path / "out" / "Folder G1" / "bundle.zip").exists()
        assert not any(e.status == MOVED for e in manifest.files.values())
        (archive,) = manifest.archive_units()
        assert archive.status == EXTRACTED
    data = report.status_data(manifest)
    assert data["archives"] == {EXTRACTED: 1}
    assert data["already_moved"] == 0 and data["extracted_bytes"] > 0
    assert report.exit_code(manifest) == report.EXIT_OK
    assert "deleted after unpacking" in report.format_status(data)


def test_keep_zips_leaves_the_zip_in_place(tmp_path):
    good = read(make_pyramidal_tiff(tmp_path / "g.tif"))
    opts = zip_opts(tmp_path, keep_zips=True)
    manifest = Manifest.load(opts.state_file)
    run_once(manifest, FakeSource({"bundle.zip": zip_bytes({"inner.tif": good})}), opts)
    assert (tmp_path / "out" / "Folder G1" / "bundle.zip").exists()
    (archive,) = manifest.archive_units()
    assert archive.status == VERIFIED
    zip_entry = next(e for e in manifest.files.values() if e.origin == "remote")
    assert zip_entry.status == VERIFIED and zip_entry.removed_at is None


def test_crash_between_save_and_unlink_is_finished_by_the_next_run(tmp_path, monkeypatch):
    good = read(make_pyramidal_tiff(tmp_path / "g.tif"))
    source = FakeSource({"bundle.zip": zip_bytes({"inner.tif": good})})
    opts = zip_opts(tmp_path)
    zip_path = tmp_path / "out" / "Folder G1" / "bundle.zip"
    real_unlink = type(zip_path).unlink

    def crashing_unlink(self, *args, **kwargs):
        if self.name == "bundle.zip":
            raise KeyboardInterrupt  # the process dies after the manifest save
        return real_unlink(self, *args, **kwargs)

    manifest = Manifest.load(opts.state_file)
    with monkeypatch.context() as patch:
        patch.setattr(type(zip_path), "unlink", crashing_unlink)
        with pytest.raises(KeyboardInterrupt):
            run_once(manifest, source, opts)
    assert zip_path.exists()
    on_disk = Manifest.load(opts.state_file)
    zip_entry = next(e for e in on_disk.files.values() if e.origin == "remote")
    assert zip_entry.status == EXTRACTED and zip_entry.removed_at  # saved before the unlink

    manifest = Manifest.load(opts.state_file)
    summary = run_once(manifest, source, opts)
    assert not zip_path.exists()
    assert summary.downloaded == 0 and source.opened == ["bundle.zip"]
    assert not any(e.status == MOVED for e in manifest.files.values())
    assert next(e for e in manifest.files.values() if e.origin == "remote").status == EXTRACTED
    assert report.exit_code(manifest) == report.EXIT_OK


def test_keep_zips_does_not_delete_a_leftover_extracted_zip(tmp_path):
    good = read(make_pyramidal_tiff(tmp_path / "g.tif"))
    source = FakeSource({"bundle.zip": zip_bytes({"inner.tif": good})})
    opts = zip_opts(tmp_path)
    manifest = Manifest.load(opts.state_file)
    run_once(manifest, source, opts)
    zip_path = tmp_path / "out" / "Folder G1" / "bundle.zip"
    zip_path.write_bytes(
        b"x" * next(e for e in manifest.files.values() if e.origin == "remote").size
    )
    run_once(Manifest.load(opts.state_file), source, zip_opts(tmp_path, keep_zips=True))
    assert zip_path.exists()
    run_once(Manifest.load(opts.state_file), source, opts)
    assert not zip_path.exists()
