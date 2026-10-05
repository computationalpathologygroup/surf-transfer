"""Manifest v2: persistence, v1 -> v2 migration, listing sync, resend linking."""

import json

import pytest

from surf_transfer.manifest import (
    MANIFEST_VERSION,
    Manifest,
    SourceScopeError,
    migrate_v1_to_v2,
)
from surf_transfer.models import (
    ARCHIVED,
    DOWNLOADED,
    FAILED,
    MOVED,
    QUEUED,
    VERIFIED,
    RemoteFile,
)


def v1_entry(tid, fid, status, **kw):
    entry = {
        "transfer_id": tid,
        "file_id": fid,
        "transfer_subject": "Batch",
        "name": f"f{fid}.svs",
        "rel_path": f"Batch/f{fid}.svs",
        "size": 100,
        "sha256": "ab" * 32,
        "status": status,
        "adopted": False,
        "attempts": 1,
        "downloaded_at": "2025-01-01T00:00:00Z",
        "verified_at": "2025-01-01T00:00:00Z",
        "archived_at": None,
        "archive_path": None,
        "error": None,
    }
    entry.update(kw)
    return entry


def v1_manifest(*entries):
    return {
        "version": 1,
        "base_url": "https://filesender.surf.nl/rest.php",
        "updated": "2025-01-02T00:00:00Z",
        "files": {f"{e['transfer_id']}:{e['file_id']}": e for e in entries},
    }


def remote(tid, fid, name=None, size=100, source_id="filesender:guest:7", **kw):
    return RemoteFile(
        key=f"filesender:{tid}:{fid}",
        source_id=source_id,
        group_id=str(tid),
        group_label="Batch",
        name=name or f"f{fid}.svs",
        size=size,
        **kw,
    )


# --- migration --------------------------------------------------------------


def test_migration_prefixes_keys_and_bumps_version():
    v2 = migrate_v1_to_v2(v1_manifest(v1_entry(48213, 991204, VERIFIED)))
    assert v2["version"] == MANIFEST_VERSION == 2
    assert list(v2["files"]) == ["filesender:48213:991204"]
    entry = v2["files"]["filesender:48213:991204"]
    assert entry["key"] == "filesender:48213:991204"
    assert entry["group_id"] == "48213"
    assert entry["source_id"] == "filesender:transfer:48213"


@pytest.mark.parametrize("status", [VERIFIED, MOVED, ARCHIVED])
def test_migration_keeps_status_and_marks_legacy(status):
    v1 = v1_entry(1, 1, status, archive_path="/mnt/a/f1.svs" if status == ARCHIVED else None)
    entry = migrate_v1_to_v2(v1_manifest(v1))["files"]["filesender:1:1"]
    assert entry["status"] == status
    assert entry["checks"] == {"legacy": True}
    assert entry["sha256"] == "ab" * 32
    assert entry["archive_path"] == v1["archive_path"]
    assert entry["rel_path"] == "Batch/f1.svs"


@pytest.mark.parametrize("status", [QUEUED, FAILED])
def test_migration_does_not_mark_unfinished_entries_legacy(status):
    entry = migrate_v1_to_v2(v1_manifest(v1_entry(1, 1, status, sha256=None)))["files"][
        "filesender:1:1"
    ]
    assert entry["status"] == status
    assert entry["checks"] == {}


def test_migration_requeues_interrupted_downloads_like_v1_load_did():
    entry = migrate_v1_to_v2(v1_manifest(v1_entry(1, 1, "downloading", sha256=None)))["files"][
        "filesender:1:1"
    ]
    assert entry["status"] == QUEUED


def test_migration_does_not_mutate_its_input():
    original = v1_manifest(v1_entry(1, 1, VERIFIED))
    snapshot = json.dumps(original, sort_keys=True)
    migrate_v1_to_v2(original)
    assert json.dumps(original, sort_keys=True) == snapshot


def test_migration_is_idempotent_on_v2():
    v2 = migrate_v1_to_v2(v1_manifest(v1_entry(1, 1, VERIFIED)))
    assert migrate_v1_to_v2(v2) == v2


def test_load_migrates_v1_file_and_keeps_a_backup(tmp_path):
    path = tmp_path / ".filesender_state.json"
    path.write_text(json.dumps(v1_manifest(v1_entry(1, 1, VERIFIED))))
    m = Manifest.load(path)
    assert m.files["filesender:1:1"].status == VERIFIED
    m.save()
    assert json.loads(path.read_text())["version"] == 2
    backup = tmp_path / ".filesender_state.json.v1.bak"
    assert json.loads(backup.read_text())["version"] == 1


def test_backup_is_not_overwritten_by_later_saves(tmp_path):
    path = tmp_path / "s.json"
    path.write_text(json.dumps(v1_manifest(v1_entry(1, 1, VERIFIED))))
    m = Manifest.load(path)
    m.save()
    first = (tmp_path / "s.json.v1.bak").read_text()
    m.files["filesender:1:1"].error = "x"
    m.save()
    assert (tmp_path / "s.json.v1.bak").read_text() == first


def test_unknown_future_version_is_refused(tmp_path):
    path = tmp_path / "s.json"
    path.write_text(json.dumps({"version": 99, "files": {}}))
    with pytest.raises(ValueError, match="version 99"):
        Manifest.load(path)


@pytest.mark.parametrize("status", [MOVED, ARCHIVED, VERIFIED])
def test_migrated_entries_are_never_requeued_by_a_listing_sync(tmp_path, status):
    path = tmp_path / "s.json"
    path.write_text(json.dumps(v1_manifest(v1_entry(1, 1, status))))
    m = Manifest.load(path)
    result = m.sync_listing([remote(1, 1)], centre=None)
    assert result.inserted == []
    assert m.files["filesender:1:1"].status == status
    assert not m.needs_download("filesender:1:1")


def test_migrated_queued_entry_still_downloads(tmp_path):
    path = tmp_path / "s.json"
    path.write_text(json.dumps(v1_manifest(v1_entry(1, 1, QUEUED, sha256=None))))
    m = Manifest.load(path)
    assert m.needs_download("filesender:1:1")


def test_reconcile_never_touches_moved_or_archived(tmp_path):
    path = tmp_path / "s.json"
    path.write_text(json.dumps(v1_manifest(v1_entry(1, 1, MOVED), v1_entry(1, 2, ARCHIVED))))
    m = Manifest.load(path)
    assert m.reconcile_local(tmp_path / "out") == []
    assert m.files["filesender:1:1"].status == MOVED
    assert m.files["filesender:1:2"].status == ARCHIVED


# --- persistence & sync -----------------------------------------------------


def test_round_trip_preserves_files_slides_and_sources(tmp_path):
    path = tmp_path / "s.json"
    m = Manifest(path)
    m.sync_listing([remote(1, 1, "a/X.mrxs"), remote(1, 2, "a/X/Slidedat.ini")], centre="Centre A")
    m.sync_slides()
    m.mark_complete("Centre A", note="mail from A, 2026-10-01", date="2026-10-02")
    m.save()
    again = Manifest.load(path)
    assert set(again.files) == set(m.files)
    assert again.files["filesender:1:1"].centre == "Centre A"
    assert [s.name for s in again.slides.values()] == ["X"]
    assert again.sources["filesender:guest:7"]["complete"] == {
        "date": "2026-10-02",
        "note": "mail from A, 2026-10-01",
    }


def test_sync_assigns_folder_and_subpath():
    m = Manifest("unused.json")
    m.sync_listing([remote(5, 1, "X.mrxs"), remote(5, 2, "X/Data0000.dat")], centre=None)
    assert m.files["filesender:5:2"].rel_path == "Batch/X/Data0000.dat"


def test_same_subject_other_transfer_gets_suffixed_folder():
    m = Manifest("unused.json")
    m.sync_listing([remote(5, 1)], centre=None)
    m.sync_listing([remote(6, 2)], centre=None)
    assert m.files["filesender:5:1"].rel_path.startswith("Batch/")
    assert m.files["filesender:6:2"].rel_path.startswith("Batch_6/")


def test_sync_rejects_unsafe_remote_paths():
    m = Manifest("unused.json")
    result = m.sync_listing([remote(1, 1, "../evil.txt")], centre=None)
    entry = m.files["filesender:1:1"]
    assert entry.status == FAILED and "unsafe" in entry.error
    assert result.inserted == [entry]


def test_sync_marks_name_collisions_instead_of_overwriting():
    m = Manifest("unused.json")
    m.sync_listing([remote(1, 1, "Data0000.dat"), remote(1, 2, "Data0000.dat")], centre=None)
    assert m.files["filesender:1:1"].status == QUEUED
    assert m.files["filesender:1:2"].status == FAILED
    assert "collision" in m.files["filesender:1:2"].error


def test_listing_problem_blocks_the_file():
    m = Manifest("unused.json")
    m.sync_listing([remote(1, 1, problem="encrypted transfers are not supported")], centre=None)
    entry = m.files["filesender:1:1"]
    assert entry.status == FAILED and "encrypted" in entry.error


def test_file_appearing_after_mark_complete_is_flagged_not_queued_silently():
    m = Manifest("unused.json")
    m.sync_listing([remote(1, 1)], centre="A")
    m.mark_complete("A", note="done", date="2026-10-02")
    result = m.sync_listing([remote(1, 1), remote(1, 2)], centre="A")
    assert [e.key for e in result.flagged] == ["filesender:1:2"]
    assert "marked complete" in m.files["filesender:1:2"].flagged
    assert not m.needs_download("filesender:1:2")
    assert m.files["filesender:1:1"].flagged is None
    m.accept_flagged()
    assert m.needs_download("filesender:1:2")


def test_mark_complete_unknown_selector_raises():
    with pytest.raises(ValueError, match="no source"):
        Manifest("unused.json").mark_complete("nope", note="x")


# --- slides, resend linking -------------------------------------------------


def _mrxs(tid, fids=(1, 2)):
    return [remote(tid, fids[0], "X.mrxs"), remote(tid, fids[1], "X/Slidedat.ini")]


def test_resend_under_new_ids_links_to_the_corrupt_original():
    m = Manifest("unused.json")
    m.sync_listing(_mrxs(1), centre="A")
    m.sync_slides()
    (first,) = m.slides.values()
    first.status = "corrupt"
    m.sync_listing(_mrxs(2, (3, 4)), centre="A")
    m.sync_slides()
    assert len(m.slides) == 2
    second = next(s for s in m.slides.values() if s.key != first.key)
    assert second.replaces == [first.key]
    assert first.superseded_by == [second.key]
    assert [s.key for s in m.current_slides()] == [second.key]


def test_sync_slides_is_idempotent():
    m = Manifest("unused.json")
    m.sync_listing(_mrxs(1) + _mrxs(2, (3, 4)), centre="A")
    m.sync_slides()
    snapshot = {k: (s.replaces, s.superseded_by, s.members) for k, s in m.slides.items()}
    m.sync_slides()
    m.sync_slides()
    assert {k: (s.replaces, s.superseded_by, s.members) for k, s in m.slides.items()} == snapshot


def test_same_slide_name_in_different_sources_is_never_linked():
    m = Manifest("unused.json")
    m.sync_listing(_mrxs(1), centre="A")
    m.sync_listing([remote(2, 3, "X.mrxs", source_id="surfdrive:s")], centre="B")
    m.sync_slides()
    assert all(not s.replaces for s in m.slides.values())


def test_slide_becomes_ready_only_when_all_members_downloaded():
    m = Manifest("unused.json")
    m.sync_listing(_mrxs(1), centre=None)
    m.sync_slides()
    (slide,) = m.slides.values()
    assert not m.slide_ready(slide)
    m.files["filesender:1:1"].status = DOWNLOADED
    assert not m.slide_ready(slide)
    m.files["filesender:1:2"].status = DOWNLOADED
    assert m.slide_ready(slide)


def test_slide_not_ready_again_once_it_has_a_verdict():
    m = Manifest("unused.json")
    m.sync_listing(_mrxs(1), centre=None)
    m.sync_slides()
    (slide,) = m.slides.values()
    for e in m.files.values():
        e.status = DOWNLOADED
    slide.status = "corrupt"
    assert not m.slide_ready(slide)


def test_flat_layout_has_no_source_folder_and_persists(tmp_path):
    m = Manifest(tmp_path / "state.json")
    m.claim_layout(True, 1)
    m.sync_listing([remote(1, 1, name="a.svs"), remote(1, 2, name="sub/b.dat")], centre=None)
    assert sorted(e.rel_path for e in m.files.values()) == ["a.svs", "sub/b.dat"]
    m.save()
    again = Manifest.load(tmp_path / "state.json")
    assert again.flat
    again.claim_layout(True, 1)
    with pytest.raises(SourceScopeError):
        again.claim_layout(False, 1)


def test_flat_refuses_several_sources_and_layout_switch(tmp_path):
    m = Manifest(tmp_path / "state.json")
    with pytest.raises(SourceScopeError):
        m.claim_layout(True, 2)
    m.sync_listing([remote(1, 1, name="a.svs")], centre=None)
    with pytest.raises(SourceScopeError):
        m.claim_layout(True, 1)
