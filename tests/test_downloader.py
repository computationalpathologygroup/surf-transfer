import pytest

from fakes import FakeSource, adler32, sha1
from surf_transfer import downloader as downloader_module
from surf_transfer.downloader import Downloader
from surf_transfer.util import sha256_file

DATA = b"0123456789" * 50


def run(tmp_path, source, name="a.bin"):
    (remote,) = [r for r in source.list() if r.name == name]
    target = tmp_path / "out" / name
    return Downloader().fetch(source, remote, target), target


def test_success_records_every_check(tmp_path):
    outcome, target = run(tmp_path, FakeSource({"a.bin": DATA}))
    assert outcome.ok
    assert target.read_bytes() == DATA
    assert outcome.checks == {
        "size": True,
        "source_checksum": "absent",
        "local_sha256": sha256_file(target),
    }
    assert outcome.sha256 == sha256_file(target)
    assert not target.with_name("a.bin.part").exists()


def test_truncated_stream_is_a_size_mismatch_and_leaves_no_file(tmp_path):
    source = FakeSource({"a.bin": DATA})
    source.corrupt_in_flight.add("a.bin")
    outcome, target = run(tmp_path, source)
    assert not outcome.ok and "size mismatch" in outcome.error
    assert not target.exists() and not target.with_name("a.bin.part").exists()


@pytest.mark.parametrize("algo,fn", [("sha1", sha1), ("adler32", adler32)])
def test_source_checksum_match(tmp_path, algo, fn):
    outcome, _ = run(tmp_path, FakeSource({"a.bin": DATA}, checksums={"a.bin": {algo: fn(DATA)}}))
    assert outcome.ok and outcome.checks["source_checksum"] == f"{algo}:match"


def test_source_checksum_mismatch_fails_the_file(tmp_path):
    outcome, target = run(
        tmp_path, FakeSource({"a.bin": DATA}, checksums={"a.bin": {"sha1": "0" * 40}})
    )
    assert not outcome.ok and "sha1:mismatch" in outcome.error
    assert not target.exists()


def test_unsupported_source_algorithm_does_not_fail(tmp_path):
    outcome, _ = run(tmp_path, FakeSource({"a.bin": DATA}, checksums={"a.bin": {"crc64": "ab"}}))
    assert outcome.ok and outcome.checks["source_checksum"] == "unsupported:crc64"


def test_disk_reread_disagreeing_with_stream_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(downloader_module, "sha256_file", lambda p: "bad")
    outcome, target = run(tmp_path, FakeSource({"a.bin": DATA}))
    assert not outcome.ok and "sha256" in outcome.error
    assert not target.exists() and not target.with_name("a.bin.part").exists()


def test_stale_part_file_is_discarded(tmp_path):
    target = tmp_path / "out" / "a.bin"
    target.parent.mkdir()
    target.with_name("a.bin.part").write_bytes(b"junk")
    outcome, _ = run(tmp_path, FakeSource({"a.bin": DATA}))
    assert outcome.ok and target.read_bytes() == DATA


def test_stream_exception_cleans_up(tmp_path):
    class Boom(FakeSource):
        def open_stream(self, remote):
            raise ConnectionError("reset")

    outcome, target = run(tmp_path, Boom({"a.bin": DATA}))
    assert not outcome.ok and "reset" in outcome.error
    assert not target.with_name("a.bin.part").exists()


def test_existing_good_file_survives_a_failed_redownload(tmp_path):
    source = FakeSource({"a.bin": DATA})
    outcome, target = run(tmp_path, source)
    source.corrupt_in_flight.add("a.bin")
    outcome2, _ = run(tmp_path, source)
    assert not outcome2.ok and target.read_bytes() == DATA
