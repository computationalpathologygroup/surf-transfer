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
from surf_transfer.models import CORRUPT, UNVALIDATED, VERIFIED
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
    assert archive.status == VERIFIED
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
    assert archive.status == VERIFIED


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
