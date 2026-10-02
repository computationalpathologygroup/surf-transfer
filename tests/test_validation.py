"""Validation worker outcomes: success, openslide error, crash, timeout - and the
main process survives every one of them."""

import os
import zipfile
from pathlib import Path

import pytest

import validation_stubs
from slides import job_for, make_fake_mrxs, make_flat_tiff, make_pyramidal_tiff
from surf_transfer.models import (
    CHECK_CRASH,
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
    KIND_ARCHIVE,
    KIND_MRXS,
    UNVALIDATED,
    VERIFIED,
)
from surf_transfer.validation import SlideValidator, execute_job
from surf_transfer.validation import _NoProgress as NoProgress

openslide = pytest.importorskip("openslide")
pytest.importorskip("tifffile")


def run(job):
    return execute_job(job, NoProgress())


# --- MRXS completeness (needs no real slide: it fails before openslide) -----


def test_mrxs_missing_dat_is_corrupt_and_names_the_file(tmp_path):
    rels = make_fake_mrxs(
        tmp_path, dats=("Data0000.dat",), declared=["Data0000.dat", "Data0001.dat"]
    )
    result = run(job_for(tmp_path, rels, kind=KIND_MRXS))
    assert result.status == CORRUPT
    assert result.failure.check == CHECK_MISSING_DAT
    assert "Data0001.dat" in result.failure.detail
    assert result.failure.members == ("T/X/Data0001.dat",)


def test_mrxs_unexpected_dat_is_corrupt_and_names_the_file(tmp_path):
    rels = make_fake_mrxs(
        tmp_path, dats=("Data0000.dat", "Data0009.dat"), declared=["Data0000.dat"]
    )
    result = run(job_for(tmp_path, rels, kind=KIND_MRXS))
    assert result.status == CORRUPT
    assert result.failure.check == CHECK_UNEXPECTED_DAT
    assert result.failure.members == ("T/X/Data0009.dat",)


def test_mrxs_missing_index_dat_is_named_and_a_present_one_is_not_unexpected(tmp_path):
    rels = make_fake_mrxs(tmp_path, dats=("Data0000.dat",), index_file="Index.dat")
    result = run(job_for(tmp_path, rels, kind=KIND_MRXS))
    assert result.failure.check == CHECK_MISSING_DAT
    assert result.failure.members == ("T/X/Index.dat",)
    (tmp_path / "T/X/Index.dat").write_bytes(b"idx")
    rels.append("T/X/Index.dat")
    # past completeness now: only openslide rejects the fake bytes
    assert run(job_for(tmp_path, rels, kind=KIND_MRXS)).failure.check == CHECK_HEADER


def test_mrxs_missing_slidedat_ini(tmp_path):
    rels = make_fake_mrxs(tmp_path, with_ini=False)
    result = run(job_for(tmp_path, rels, kind=KIND_MRXS))
    assert result.failure.check == CHECK_SLIDEDAT and "Slidedat.ini" in result.failure.detail


def test_mrxs_folder_without_index_file(tmp_path):
    rels = make_fake_mrxs(tmp_path, with_index=False)
    result = run(job_for(tmp_path, rels, kind=KIND_MRXS, index=None))
    assert result.failure.check == CHECK_INDEX


def test_mrxs_complete_listing_proceeds_to_openslide_and_fails_on_garbage(tmp_path):
    rels = make_fake_mrxs(tmp_path)
    result = run(job_for(tmp_path, rels, kind=KIND_MRXS))
    assert result.status == CORRUPT and result.failure.check == CHECK_HEADER
    assert [c.name for c in result.checks if c.passed][:3] == [
        "index_file",
        "slidedat_parse",
        "dat_missing",
    ]


# --- openslide checks on a real (synthetic) pyramidal TIFF ------------------


def test_valid_slide_is_verified_with_metadata_and_reproducible_sample(tmp_path):
    make_pyramidal_tiff(tmp_path / "ok.tif")
    result = run(job_for(tmp_path, ["ok.tif"], sample_tiles=16, tile_size=256, seed=42))
    assert result.status == VERIFIED
    assert result.format == "generic-tiff"
    assert result.metadata["dimensions"] == [2048, 1536]
    assert result.metadata["level_count"] == 3
    assert result.seed == 42 and len(result.coords) == 16
    again = run(job_for(tmp_path, ["ok.tif"], sample_tiles=16, tile_size=256, seed=42))
    assert again.coords == result.coords
    assert [c.name for c in result.checks] == [
        "detect_format",
        "open",
        "lowres_read",
        "tile_sample",
    ]


def test_unseeded_run_records_a_fresh_seed(tmp_path):
    make_pyramidal_tiff(tmp_path / "ok.tif")
    a = run(job_for(tmp_path, ["ok.tif"], sample_tiles=4))
    assert isinstance(a.seed, int)


def test_deep_check_decodes_every_tile_at_every_level(tmp_path):
    make_pyramidal_tiff(tmp_path / "ok.tif")
    result = run(job_for(tmp_path, ["ok.tif"], sample_tiles=2, deep=True))
    assert result.status == VERIFIED and result.deep
    assert result.checks[-1].name == "deep_check" and result.checks[-1].passed


def test_flat_tiff_is_unvalidated_not_corrupt(tmp_path):
    make_flat_tiff(tmp_path / "flat.tif")
    result = run(job_for(tmp_path, ["flat.tif"]))
    assert result.status == UNVALIDATED
    assert result.failure.check == CHECK_NO_VALIDATOR
    assert "flat.tif" in result.failure.detail


def test_garbage_with_an_openslide_only_extension_is_corrupt(tmp_path):
    (tmp_path / "bad.svs").write_bytes(b"junk" * 100)
    result = run(job_for(tmp_path, ["bad.svs"]))
    assert result.status == CORRUPT and result.failure.check == CHECK_HEADER


def test_garbage_with_an_ambiguous_extension_is_unvalidated(tmp_path):
    (tmp_path / "x.czi").write_bytes(b"junk" * 100)
    assert run(job_for(tmp_path, ["x.czi"])).status == UNVALIDATED


def _wipe_page_data(path: Path, page_index: int) -> None:
    import tifffile

    with tifffile.TiffFile(path) as tf:
        page = tf.pages[page_index]
        spans = list(zip(page.dataoffsets, page.databytecounts, strict=True))
    data = bytearray(path.read_bytes())
    for offset, length in spans:
        data[offset : offset + length] = b"\xff" * length
    path.write_bytes(bytes(data))


def test_damaged_lowest_level_fails_the_lowres_check(tmp_path):
    path = make_pyramidal_tiff(tmp_path / "bad.tif")
    _wipe_page_data(path, 2)
    result = run(job_for(tmp_path, ["bad.tif"], sample_tiles=8, tile_size=256, seed=1))
    assert result.status == CORRUPT
    assert result.failure.check == CHECK_LOWRES and result.failure.level == 2
    assert result.failure.members == ("bad.tif",)


def test_damaged_level0_tile_fails_the_tile_check_with_its_coordinates(tmp_path):
    path = make_pyramidal_tiff(tmp_path / "bad.tif")
    _wipe_page_data(path, 0)
    result = run(job_for(tmp_path, ["bad.tif"], sample_tiles=16, tile_size=256, seed=1))
    assert result.status == CORRUPT
    assert result.failure.check == CHECK_TILE
    x, y = result.failure.coord
    assert (x, y) in result.coords and result.seed == 1  # reproducible from the manifest


def test_damaged_tiff_structure_is_corrupt_not_unvalidated(tmp_path):
    path = make_pyramidal_tiff(tmp_path / "bad.tif")
    data = bytearray(path.read_bytes())
    for i in range(8, len(data) - 4096, 7):
        data[i] = 0xFF
    path.write_bytes(bytes(data))
    result = run(job_for(tmp_path, ["bad.tif"], sample_tiles=8, tile_size=256, seed=1))
    assert result.status == CORRUPT


def test_truncated_flat_tiff_is_corrupt_but_an_intact_one_is_unvalidated(tmp_path):
    flat = make_flat_tiff(tmp_path / "flat.tif")
    assert run(job_for(tmp_path, ["flat.tif"])).status == UNVALIDATED
    flat.write_bytes(flat.read_bytes()[: flat.stat().st_size // 2])
    result = run(job_for(tmp_path, ["flat.tif"]))
    assert result.status == CORRUPT and "damaged TIFF" in result.failure.detail


def test_truncated_file_is_corrupt(tmp_path):
    path = make_pyramidal_tiff(tmp_path / "cut.tif")
    path.write_bytes(path.read_bytes()[: path.stat().st_size // 3])
    result = run(job_for(tmp_path, ["cut.tif"], sample_tiles=8, tile_size=256, seed=1))
    assert result.status == CORRUPT


# --- subprocess isolation ---------------------------------------------------


def test_subprocess_success_returns_the_workers_result(tmp_path):
    make_pyramidal_tiff(tmp_path / "ok.tif")
    result = SlideValidator().run(job_for(tmp_path, ["ok.tif"], sample_tiles=4, tile_size=256))
    assert result.status == VERIFIED and result.format == "generic-tiff"


def test_subprocess_openslide_error_lands_in_corrupt(tmp_path):
    (tmp_path / "bad.svs").write_bytes(b"junk" * 100)
    result = SlideValidator().run(job_for(tmp_path, ["bad.svs"]))
    assert result.status == CORRUPT and result.failure.check == CHECK_HEADER


def test_segfault_in_the_worker_is_corrupt_crash_and_the_parent_survives(tmp_path):
    (tmp_path / "x.svs").write_bytes(b"x")
    pid = os.getpid()
    result = SlideValidator(target=validation_stubs.segfault).run(job_for(tmp_path, ["x.svs"]))
    assert os.getpid() == pid
    assert result.status == CORRUPT
    assert result.failure.check == CHECK_CRASH
    assert "SIGSEGV" in result.failure.detail
    assert result.failure.coord == (1024, 2048) and result.failure.level == 0
    assert result.failure.members == ("x.svs",)
    # and the validator is still usable afterwards
    ok = SlideValidator(target=validation_stubs.ok).run(job_for(tmp_path, ["x.svs"]))
    assert ok.status == VERIFIED


def test_timeout_is_corrupt_timeout_and_the_worker_is_killed(tmp_path):
    (tmp_path / "x.svs").write_bytes(b"x")
    result = SlideValidator(target=validation_stubs.hang).run(
        job_for(tmp_path, ["x.svs"], timeout=1.5)
    )
    assert result.status == CORRUPT
    assert result.failure.check == CHECK_TIMEOUT
    assert "1.5s" in result.failure.detail and result.failure.level == 1


def test_silent_exit_without_result_is_a_crash(tmp_path):
    (tmp_path / "x.svs").write_bytes(b"x")
    result = SlideValidator(target=validation_stubs.exits_silently).run(
        job_for(tmp_path, ["x.svs"])
    )
    assert result.status == CORRUPT and result.failure.check == CHECK_CRASH
    assert "code 3" in result.failure.detail


def test_internal_validator_error_is_unvalidated_not_corrupt(tmp_path):
    (tmp_path / "x.svs").write_bytes(b"x")
    result = SlideValidator(target=validation_stubs.python_error).run(job_for(tmp_path, ["x.svs"]))
    assert result.status == UNVALIDATED
    assert "boom in validator" in result.failure.detail


# --- archives ---------------------------------------------------------------


def make_zip(path: Path, files: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return path


def unpack_job(tmp_path, name="a.zip"):
    return job_for(
        tmp_path, [name], kind=KIND_ARCHIVE, job_kind="unpack", extract_dir="a.extracted"
    )


def test_unpack_extracts_and_hashes_every_member(tmp_path):
    make_zip(
        tmp_path / "a.zip",
        {"X.mrxs": b"index", "X/Slidedat.ini": b"ini", "X/Data0000.dat": b"d" * 5000},
    )
    result = execute_job(unpack_job(tmp_path), NoProgress())
    assert result.status == VERIFIED and result.format == "zip"
    assert sorted(e.rel_path for e in result.extracted) == [
        "X.mrxs",
        "X/Data0000.dat",
        "X/Slidedat.ini",
    ]
    assert (tmp_path / "a.extracted/X/Data0000.dat").read_bytes() == b"d" * 5000
    assert not list(tmp_path.rglob("*.part"))


def test_unpack_detects_a_damaged_zip(tmp_path):
    path = make_zip(tmp_path / "a.zip", {"big.bin": os.urandom(200_000)})
    data = bytearray(path.read_bytes())
    data[1000] ^= 0xFF
    path.write_bytes(bytes(data))
    result = execute_job(unpack_job(tmp_path), NoProgress())
    assert result.status == CORRUPT and result.failure.check == CHECK_ZIP
    assert result.failure.members == ("a.zip",)
    assert not list(tmp_path.rglob("*.part"))


def test_unpack_rejects_path_traversal(tmp_path):
    make_zip(tmp_path / "a.zip", {"../escape.txt": b"x"})
    result = execute_job(unpack_job(tmp_path), NoProgress())
    assert result.status == CORRUPT and "unsafe path" in result.failure.detail
    assert not (tmp_path.parent / "escape.txt").exists()


def test_unpack_refuses_when_it_would_not_fit(tmp_path):
    make_zip(tmp_path / "a.zip", {"f.bin": b"x" * 100})
    job = job_for(
        tmp_path,
        ["a.zip"],
        kind=KIND_ARCHIVE,
        job_kind="unpack",
        extract_dir="a.extracted",
        min_free_bytes=10**18,
    )
    result = execute_job(job, NoProgress())
    assert result.status == CORRUPT and "free space" in result.failure.detail
    assert not (tmp_path / "a.extracted").exists()


def test_unsupported_archive_is_unvalidated(tmp_path):
    (tmp_path / "a.7z").write_bytes(b"7z")
    job = job_for(
        tmp_path, ["a.7z"], kind=KIND_ARCHIVE, job_kind="unpack", extract_dir="a.extracted"
    )
    result = execute_job(job, NoProgress())
    assert result.status == UNVALIDATED and "extract it manually" in result.failure.detail


def test_encrypted_zip_is_reported(tmp_path):
    path = make_zip(tmp_path / "a.zip", {"f": b"x"})
    data = bytearray(path.read_bytes())
    # set the encrypted bit in the central directory entry's general-purpose flags
    cd = data.rindex(b"PK\x01\x02")
    data[cd + 8] |= 1
    path.write_bytes(bytes(data))
    result = execute_job(unpack_job(tmp_path), NoProgress())
    assert result.status == CORRUPT and "password" in result.failure.detail


# --- extraction layout: next to the zip, never overwriting ------------------


def test_unpack_into_the_zips_own_folder_pairs_with_loose_files(tmp_path):
    (tmp_path / "T").mkdir()
    (tmp_path / "T" / "X.mrxs").write_bytes(b"loose index")
    make_zip(tmp_path / "T" / "a.zip", {"X/Slidedat.ini": b"ini", "X/Data0000.dat": b"d"})
    job = job_for(tmp_path, ["T/a.zip"], kind=KIND_ARCHIVE, job_kind="unpack", extract_dir="T")
    result = execute_job(job, NoProgress())
    assert result.status == VERIFIED
    assert (tmp_path / "T/X/Data0000.dat").read_bytes() == b"d"
    assert (tmp_path / "T/X.mrxs").read_bytes() == b"loose index"  # untouched


def test_unpack_into_the_output_root(tmp_path):
    make_zip(tmp_path / "a.zip", {"X/f.dat": b"d"})
    job = job_for(tmp_path, ["a.zip"], kind=KIND_ARCHIVE, job_kind="unpack", extract_dir="")
    assert execute_job(job, NoProgress()).status == VERIFIED
    assert (tmp_path / "X/f.dat").exists()


def test_member_identical_to_an_existing_file_is_reused_not_rewritten(tmp_path):
    (tmp_path / "X").mkdir()
    (tmp_path / "X/f.dat").write_bytes(b"same")
    before = (tmp_path / "X/f.dat").stat().st_mtime_ns
    make_zip(tmp_path / "a.zip", {"X/f.dat": b"same", "X/g.dat": b"new"})
    job = job_for(tmp_path, ["a.zip"], kind=KIND_ARCHIVE, job_kind="unpack", extract_dir="")
    result = execute_job(job, NoProgress())
    assert result.status == VERIFIED
    flags = {e.rel_path: e.existing for e in result.extracted}
    assert flags == {"X/f.dat": True, "X/g.dat": False}
    assert (tmp_path / "X/f.dat").stat().st_mtime_ns == before


def test_member_that_would_overwrite_different_content_is_a_conflict(tmp_path):
    (tmp_path / "X").mkdir()
    (tmp_path / "X/f.dat").write_bytes(b"precious original")
    make_zip(tmp_path / "a.zip", {"X/f.dat": b"different"})
    job = job_for(tmp_path, ["a.zip"], kind=KIND_ARCHIVE, job_kind="unpack", extract_dir="")
    result = execute_job(job, NoProgress())
    assert result.status == CORRUPT and result.failure.check == CHECK_ZIP
    assert "would overwrite X/f.dat" in result.failure.detail
    assert "X/f.dat" in result.failure.members and "a.zip" in result.failure.members
    assert (tmp_path / "X/f.dat").read_bytes() == b"precious original"
    assert not list(tmp_path.rglob("*.part"))


def test_member_colliding_with_a_listed_but_undownloaded_file_is_refused(tmp_path):
    make_zip(tmp_path / "a.zip", {"X/f.dat": b"d"})
    job = job_for(tmp_path, ["a.zip"], kind=KIND_ARCHIVE, job_kind="unpack", extract_dir="")
    job = type(job)(**{**job.__dict__, "claimed": ("X/f.dat",)})
    result = execute_job(job, NoProgress())
    assert result.status == CORRUPT and "not been downloaded" in result.failure.detail
    assert not (tmp_path / "X/f.dat").exists()
