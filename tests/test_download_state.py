"""
test_download_state.py — tests for download_state.py, the FileSender downloader's
persistent manifest logic (no network, no argv parsing).

Run from the filesender directory:
    pytest tests/test_download_state.py -v
"""

import hashlib
import json
import sys
from pathlib import Path

_PIPELINE_DIR = Path(__file__).parent.parent
if str(_PIPELINE_DIR) not in sys.path:
    sys.path.insert(0, str(_PIPELINE_DIR))

import pytest
from download_state import (
    DownloadState,
    RunBudget,
    STATUS_ARCHIVED,
    STATUS_DOWNLOADING,
    STATUS_FAILED,
    STATUS_MOVED,
    STATUS_QUEUED,
    STATUS_VERIFIED,
    parse_size,
    sha256_file,
)

from conftest import write_file


# ---------------------------------------------------------------------------
# Manifest round-trip and atomic save
# ---------------------------------------------------------------------------

def test_manifest_round_trip(state_path):
    state = DownloadState(state_path, base_url="https://filesender.example/rest.php")
    state.upsert(48213, 991204, name="case_017.svs", rel_path="Batch 3/case_017.svs",
                 size=1024, status=STATUS_QUEUED)
    state.save()

    reloaded = DownloadState.load(state_path)
    entry = reloaded.get(48213, 991204)
    assert entry is not None
    assert entry["name"] == "case_017.svs"
    assert entry["rel_path"] == "Batch 3/case_017.svs"
    assert entry["size"] == 1024
    assert entry["status"] == STATUS_QUEUED
    assert reloaded.data["base_url"] == "https://filesender.example/rest.php"


def test_save_leaves_no_tmp_file_behind(state_path):
    state = DownloadState(state_path)
    state.upsert(1, 1, name="a", rel_path="a", size=10, status=STATUS_QUEUED)
    state.save()

    tmp_path = state_path.parent / (state_path.name + ".tmp")
    assert state_path.exists()
    assert not tmp_path.exists()


def test_save_renders_readable_txt_report(state_path):
    state = DownloadState(state_path)
    state.upsert(1, 1, name="a", rel_path="dir/a.bin", size=10, status=STATUS_VERIFIED,
                 sha256="abc123def456abc123")
    state.save()

    txt_path = state_path.parent / ".filesender_state.txt"
    assert txt_path.exists()
    content = txt_path.read_text()
    assert "verified" in content
    assert "dir/a.bin" in content
    assert "abc123def456" in content  # first 12 chars of the hash


def test_save_survives_a_leftover_tmp_from_a_simulated_crash(state_path):
    # Simulate a crash that left a half-written .tmp file behind from a
    # previous run (the os.replace() that would have completed it never ran).
    tmp_path = state_path.parent / (state_path.name + ".tmp")
    tmp_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path.write_text("{not valid json, truncated mid-wr")

    state = DownloadState(state_path)
    state.upsert(1, 1, name="a", rel_path="a", size=10, status=STATUS_QUEUED)
    state.save()

    assert not tmp_path.exists()
    reloaded = DownloadState.load(state_path)
    assert reloaded.get(1, 1)["name"] == "a"


def test_load_resets_stale_downloading_status_to_queued(state_path):
    state = DownloadState(state_path)
    state.upsert(1, 1, name="a", rel_path="a", size=10, status=STATUS_DOWNLOADING, attempts=1)
    state.save()

    reloaded = DownloadState.load(state_path)
    assert reloaded.get(1, 1)["status"] == STATUS_QUEUED


# ---------------------------------------------------------------------------
# sha256_file
# ---------------------------------------------------------------------------

def test_sha256_file_matches_known_vector(tmp_path):
    path = tmp_path / "hello.txt"
    write_file(path, b"hello world")
    assert sha256_file(path) == hashlib.sha256(b"hello world").hexdigest()


def test_sha256_file_detects_corruption(tmp_path):
    path = tmp_path / "data.bin"
    write_file(path, b"original bytes")
    original_digest = sha256_file(path)

    path.write_bytes(b"original bytes" + b"X")
    corrupted_digest = sha256_file(path)

    assert original_digest != corrupted_digest


def test_sha256_file_handles_chunk_boundaries(tmp_path):
    path = tmp_path / "big.bin"
    content = b"a" * 100
    write_file(path, content)
    assert sha256_file(path, chunk_size=7) == hashlib.sha256(content).hexdigest()


# ---------------------------------------------------------------------------
# Adoption of pre-existing files
# ---------------------------------------------------------------------------

def test_adopt_existing_file_with_matching_size(state_path, output_dir):
    content = b"pre-existing content"
    local_path = output_dir / "folder" / "file.bin"
    write_file(local_path, content)

    state = DownloadState(state_path)
    entry = state.upsert(1, 1, name="file.bin", rel_path="folder/file.bin",
                          size=len(content), status=STATUS_QUEUED)

    adopted = state.adopt_existing(output_dir, entry)

    assert adopted is True
    assert entry["status"] == STATUS_VERIFIED
    assert entry["adopted"] is True
    assert entry["sha256"] == hashlib.sha256(content).hexdigest()
    assert entry["verified_at"] is not None


def test_adopt_existing_rejects_size_mismatch(state_path, output_dir):
    write_file(output_dir / "folder" / "file.bin", b"short")

    state = DownloadState(state_path)
    entry = state.upsert(1, 1, name="file.bin", rel_path="folder/file.bin",
                          size=99999, status=STATUS_QUEUED)

    adopted = state.adopt_existing(output_dir, entry)

    assert adopted is False
    assert entry["status"] == STATUS_QUEUED
    assert entry["sha256"] is None


def test_adopt_existing_skips_missing_file(state_path, output_dir):
    state = DownloadState(state_path)
    entry = state.upsert(1, 1, name="file.bin", rel_path="folder/file.bin",
                          size=10, status=STATUS_QUEUED)

    assert state.adopt_existing(output_dir, entry) is False
    assert entry["status"] == STATUS_QUEUED


def test_adopt_existing_only_applies_to_queued_entries(state_path, output_dir):
    content = b"content"
    write_file(output_dir / "file.bin", content)

    state = DownloadState(state_path)
    entry = state.upsert(1, 1, name="file.bin", rel_path="file.bin",
                          size=len(content), status=STATUS_FAILED)

    assert state.adopt_existing(output_dir, entry) is False
    assert entry["status"] == STATUS_FAILED


# ---------------------------------------------------------------------------
# sync_listing
# ---------------------------------------------------------------------------

def test_sync_listing_inserts_only_unseen_files(state_path):
    state = DownloadState(state_path)
    file_list = [
        {"id": 1, "name": "a.bin", "size": 10},
        {"id": 2, "name": "b.bin", "size": 20},
    ]
    inserted_first = state.sync_listing(100, "Subject", "folder", file_list)
    assert len(inserted_first) == 2
    assert state.get(100, 1)["status"] == STATUS_QUEUED
    assert state.get(100, 1)["rel_path"] == str(Path("folder") / "a.bin")

    inserted_second = state.sync_listing(100, "Subject", "folder", file_list)
    assert inserted_second == []


# ---------------------------------------------------------------------------
# Lifecycle: verified -> moved / failed via reconcile_local
# ---------------------------------------------------------------------------

def test_reconcile_local_marks_verified_as_moved_when_file_is_gone(state_path, output_dir):
    content = b"data"
    local_path = output_dir / "file.bin"
    write_file(local_path, content)

    state = DownloadState(state_path)
    entry = state.upsert(1, 1, name="file.bin", rel_path="file.bin", size=len(content),
                          status=STATUS_VERIFIED, sha256=hashlib.sha256(content).hexdigest())

    local_path.unlink()
    changed = state.reconcile_local(output_dir)

    assert changed == [entry]
    assert entry["status"] == STATUS_MOVED


def test_reconcile_local_marks_verified_as_failed_on_size_mismatch(state_path, output_dir):
    content = b"data"
    local_path = output_dir / "file.bin"
    write_file(local_path, content)

    state = DownloadState(state_path)
    entry = state.upsert(1, 1, name="file.bin", rel_path="file.bin", size=len(content),
                          status=STATUS_VERIFIED, sha256=hashlib.sha256(content).hexdigest())

    local_path.write_bytes(b"different length content")
    changed = state.reconcile_local(output_dir)

    assert changed == [entry]
    assert entry["status"] == STATUS_FAILED
    assert entry["error"]


def test_reconcile_local_leaves_intact_verified_files_alone(state_path, output_dir):
    content = b"data"
    write_file(output_dir / "file.bin", content)

    state = DownloadState(state_path)
    entry = state.upsert(1, 1, name="file.bin", rel_path="file.bin", size=len(content),
                          status=STATUS_VERIFIED, sha256=hashlib.sha256(content).hexdigest())

    changed = state.reconcile_local(output_dir)

    assert changed == []
    assert entry["status"] == STATUS_VERIFIED


def test_reconcile_local_ignores_non_verified_entries(state_path, output_dir):
    state = DownloadState(state_path)
    entry = state.upsert(1, 1, name="file.bin", rel_path="file.bin", size=10, status=STATUS_QUEUED)

    changed = state.reconcile_local(output_dir)

    assert changed == []
    assert entry["status"] == STATUS_QUEUED


# ---------------------------------------------------------------------------
# Archive resolution
# ---------------------------------------------------------------------------

def _moved_entry(state, rel_path, content, transfer_id=1, file_id=1):
    return state.upsert(
        transfer_id, file_id,
        name=Path(rel_path).name, rel_path=rel_path, size=len(content),
        status=STATUS_MOVED, sha256=hashlib.sha256(content).hexdigest(),
    )


def test_resolve_archive_matches_by_rel_path(state_path, tmp_path):
    content = b"archived content"
    archive_dir = tmp_path / "archive"
    write_file(archive_dir / "Batch 3" / "case_017.svs", content)

    state = DownloadState(state_path)
    entry = _moved_entry(state, "Batch 3/case_017.svs", content)

    resolved, mismatched = state.resolve_archive(archive_dir, [entry])

    assert resolved == [entry]
    assert mismatched == []
    assert entry["status"] == STATUS_ARCHIVED
    assert entry["archive_path"] == str(archive_dir / "Batch 3" / "case_017.svs")
    assert entry["archived_at"] is not None


def test_resolve_archive_falls_back_to_basename_and_size_index(state_path, tmp_path):
    content = b"reorganised content"
    archive_dir = tmp_path / "archive"
    # File lives at a different path than rel_path implies - archive got reorganised.
    write_file(archive_dir / "some_other_folder" / "case_017.svs", content)

    state = DownloadState(state_path)
    entry = _moved_entry(state, "Batch 3/case_017.svs", content)

    resolved, mismatched = state.resolve_archive(archive_dir, [entry])

    assert resolved == [entry]
    assert entry["status"] == STATUS_ARCHIVED
    assert entry["archive_path"] == str(archive_dir / "some_other_folder" / "case_017.svs")


def test_resolve_archive_hash_mismatch_leaves_entry_moved(state_path, tmp_path):
    original_content = b"expected content"
    archive_dir = tmp_path / "archive"
    write_file(archive_dir / "file.bin", b"different content, same name and size!")

    state = DownloadState(state_path)
    entry = state.upsert(
        1, 1, name="file.bin", rel_path="file.bin", size=len(original_content),
        status=STATUS_MOVED, sha256=hashlib.sha256(original_content).hexdigest(),
    )
    # Make the archive candidate share the same size so it's found by rel_path,
    # even though its content (and thus hash) differs.
    (archive_dir / "file.bin").write_bytes(b"X" * len(original_content))

    resolved, mismatched = state.resolve_archive(archive_dir, [entry])

    assert resolved == []
    assert mismatched == [entry]
    assert entry["status"] == STATUS_MOVED
    assert entry["error"]


def test_resolve_archive_no_candidate_leaves_entry_untouched(state_path, tmp_path):
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()

    state = DownloadState(state_path)
    entry = _moved_entry(state, "missing.bin", b"content")

    resolved, mismatched = state.resolve_archive(archive_dir, [entry])

    assert resolved == []
    assert mismatched == []
    assert entry["status"] == STATUS_MOVED


def test_resolve_archive_ambiguous_basename_match_is_not_resolved(state_path, tmp_path):
    content = b"ambiguous content"
    archive_dir = tmp_path / "archive"
    write_file(archive_dir / "one" / "dup.bin", content)
    write_file(archive_dir / "two" / "dup.bin", content)

    state = DownloadState(state_path)
    entry = _moved_entry(state, "elsewhere/dup.bin", content)

    resolved, mismatched = state.resolve_archive(archive_dir, [entry])

    assert resolved == []
    assert mismatched == []
    assert entry["status"] == STATUS_MOVED


# ---------------------------------------------------------------------------
# stale_entries / summary
# ---------------------------------------------------------------------------

def test_stale_entries_reports_keys_not_in_current_listing(state_path):
    state = DownloadState(state_path)
    state.upsert(1, 1, name="a", rel_path="a", size=1, status=STATUS_VERIFIED)
    state.upsert(1, 2, name="b", rel_path="b", size=1, status=STATUS_VERIFIED)

    stale = state.stale_entries(seen_keys={DownloadState.key(1, 1)})

    assert len(stale) == 1
    assert stale[0]["name"] == "b"


def test_summary_totals_by_status(state_path):
    state = DownloadState(state_path)
    state.upsert(1, 1, name="a", rel_path="a", size=100, status=STATUS_VERIFIED)
    state.upsert(1, 2, name="b", rel_path="b", size=200, status=STATUS_VERIFIED)
    state.upsert(1, 3, name="c", rel_path="c", size=50, status=STATUS_QUEUED)

    summary = state.summary()

    assert summary[STATUS_VERIFIED] == {"count": 2, "bytes": 300}
    assert summary[STATUS_QUEUED] == {"count": 1, "bytes": 50}


# ---------------------------------------------------------------------------
# parse_size
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("1024", 1024),
    ("500M", 500 * 1024**2),
    ("500MB", 500 * 1024**2),
    ("200G", 200 * 1024**3),
    ("1.5T", int(1.5 * 1024**4)),
    ("10K", 10 * 1024),
    ("42B", 42),
])
def test_parse_size_accepts_suffixes(text, expected):
    assert parse_size(text) == expected


def test_parse_size_accepts_plain_numbers():
    assert parse_size(2048) == 2048
    assert parse_size(2048.0) == 2048


def test_parse_size_rejects_invalid_input():
    with pytest.raises(ValueError):
        parse_size("not-a-size")
    with pytest.raises(ValueError):
        parse_size("200X")


# ---------------------------------------------------------------------------
# RunBudget
# ---------------------------------------------------------------------------

def test_run_budget_stops_after_max_files():
    budget = RunBudget(max_files=2)

    assert budget.can_start(10)
    budget.record(10)
    assert budget.can_start(10)
    budget.record(10)

    assert budget.can_start(10) is False
    assert budget.exhausted
    assert "max-files" in budget.stop_reason


def test_run_budget_stops_after_max_bytes():
    budget = RunBudget(max_bytes=1000)

    assert budget.can_start(600)
    budget.record(600)

    assert budget.can_start(500) is False
    assert budget.exhausted
    assert "max-bytes" in budget.stop_reason
    assert budget.oversized_file is False


def test_run_budget_reports_oversized_single_file_distinctly():
    budget = RunBudget(max_bytes=1000)

    assert budget.can_start(2000) is False
    assert budget.oversized_file is True
    assert "exceeds" in budget.stop_reason


def test_run_budget_oversized_file_detected_even_with_remaining_budget():
    budget = RunBudget(max_bytes=1000)
    budget2 = RunBudget(max_bytes=1000)
    # A file larger than the whole budget is flagged distinctly from an
    # ordinary "budget used up" stop, regardless of how much has been spent.
    assert budget.can_start(1500) is False
    assert budget.oversized_file is True

    budget2.can_start(100)  # never called can_start with a real download
    assert budget2.oversized_file is False


def test_run_budget_stays_exhausted_once_triggered():
    budget = RunBudget(max_files=1)
    budget.record(10)
    assert budget.can_start(10) is False
    first_reason = budget.stop_reason

    # Even a tiny file that would otherwise fit must not resurrect the budget.
    assert budget.can_start(1) is False
    assert budget.stop_reason == first_reason


def test_run_budget_with_no_limits_never_stops():
    budget = RunBudget()
    assert budget.can_start(10**12)
    budget.record(10**12)
    assert budget.can_start(10**12)
    assert not budget.exhausted
