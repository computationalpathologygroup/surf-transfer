#!/usr/bin/env python3
"""
download_state.py — the version-1 manifest (legacy).

The downloader now uses surf_transfer.manifest.Manifest (version 2), which
migrates v1 files automatically. DownloadState is kept so the v1 behaviour and
its tests stay available; parse_size, sha256_file and RunBudget are re-exported
from the package. Pure logic, no network calls. Tracks per-file download/verification/archive
state across runs so that a batch download can be interrupted, files moved
out of the output directory by hand, and a later run picks up exactly where
it left off without re-downloading anything already accounted for.
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from surf_transfer.budget import RunBudget  # noqa: E402,F401  (re-exported)
from surf_transfer.models import (  # noqa: E402
    ARCHIVED as STATUS_ARCHIVED,
    DOWNLOADING as STATUS_DOWNLOADING,
    FAILED as STATUS_FAILED,
    MOVED as STATUS_MOVED,
    QUEUED as STATUS_QUEUED,
    VERIFIED as STATUS_VERIFIED,
)
from surf_transfer.util import now_iso as _now_iso  # noqa: E402
from surf_transfer.util import parse_size, sha256_file  # noqa: E402,F401  (re-exported)


def _new_entry(transfer_id, file_id):
    return {
        "transfer_id": transfer_id,
        "file_id": file_id,
        "transfer_subject": None,
        "name": None,
        "rel_path": None,
        "size": None,
        "sha256": None,
        "status": STATUS_QUEUED,
        "adopted": False,
        "attempts": 0,
        "downloaded_at": None,
        "verified_at": None,
        "archived_at": None,
        "archive_path": None,
        "error": None,
    }


class DownloadState:
    """A JSON-backed manifest of every file seen across FileSender transfers."""

    def __init__(self, path, base_url=None):
        self.path = Path(path)
        self.data = {
            "version": 1,
            "base_url": base_url,
            "updated": _now_iso(),
            "files": {},
        }
        self._archive_index_cache = {}

    @staticmethod
    def key(transfer_id, file_id):
        return f"{transfer_id}:{file_id}"

    @classmethod
    def load(cls, path, base_url=None):
        state = cls(path, base_url=base_url)
        p = Path(path)
        if p.exists():
            with p.open("r", encoding="utf-8") as f:
                state.data = json.load(f)
            if base_url is not None:
                state.data["base_url"] = base_url
        # A crash mid-download leaves an entry stuck in "downloading"; the
        # caller is responsible for deleting the matching stale .part file.
        for entry in state.data["files"].values():
            if entry.get("status") == STATUS_DOWNLOADING:
                entry["status"] = STATUS_QUEUED
        return state

    def get(self, transfer_id, file_id):
        return self.data["files"].get(self.key(transfer_id, file_id))

    def upsert(self, transfer_id, file_id, **fields):
        k = self.key(transfer_id, file_id)
        entry = self.data["files"].get(k)
        if entry is None:
            entry = _new_entry(transfer_id, file_id)
            self.data["files"][k] = entry
        entry.update(fields)
        return entry

    def sync_listing(self, transfer_id, subject, folder, file_list):
        """Insert any files from a FileSender listing that aren't tracked yet."""
        inserted = []
        for file_info in file_list:
            file_id = file_info["id"]
            if self.get(transfer_id, file_id) is not None:
                continue
            name = file_info["name"]
            rel_path = str(Path(folder) / name)
            entry = self.upsert(
                transfer_id,
                file_id,
                transfer_subject=subject,
                name=name,
                rel_path=rel_path,
                size=file_info["size"],
            )
            inserted.append(entry)
        return inserted

    def reconcile_local(self, output_dir):
        """Detect verified files that have disappeared or changed since verification."""
        output_dir = Path(output_dir)
        changed = []
        for entry in self.data["files"].values():
            if entry["status"] != STATUS_VERIFIED:
                continue
            local_path = output_dir / entry["rel_path"]
            if not local_path.exists():
                entry["status"] = STATUS_MOVED
                changed.append(entry)
            elif local_path.stat().st_size != entry["size"]:
                entry["status"] = STATUS_FAILED
                entry["error"] = "local file size changed since verification"
                changed.append(entry)
        return changed

    def adopt_existing(self, output_dir, entry):
        """Adopt a queued entry that's already present on disk with a matching size."""
        if entry["status"] != STATUS_QUEUED:
            return False
        local_path = Path(output_dir) / entry["rel_path"]
        if not local_path.exists() or local_path.stat().st_size != entry["size"]:
            return False
        digest = sha256_file(local_path)
        entry.update(
            sha256=digest,
            status=STATUS_VERIFIED,
            adopted=True,
            verified_at=_now_iso(),
        )
        return True

    def _archive_index(self, archive_dir):
        archive_dir = str(archive_dir)
        if archive_dir not in self._archive_index_cache:
            index = {}
            for p in Path(archive_dir).rglob("*"):
                if not p.is_file():
                    continue
                try:
                    size = p.stat().st_size
                except OSError:
                    continue
                index.setdefault((p.name, size), []).append(p)
            self._archive_index_cache[archive_dir] = index
        return self._archive_index_cache[archive_dir]

    def resolve_archive(self, archive_dir, entries=None):
        """Match moved entries against files under archive_dir by rel_path, then by
        (basename, size) fallback. Returns (resolved, mismatched) entry lists;
        entries with no candidate at all are left untouched in neither list."""
        archive_dir = Path(archive_dir)
        if entries is None:
            entries = [e for e in self.data["files"].values() if e["status"] == STATUS_MOVED]
        resolved, mismatched = [], []
        for entry in entries:
            candidate = archive_dir / entry["rel_path"]
            if not candidate.exists():
                candidates = self._archive_index(archive_dir).get(
                    (Path(entry["rel_path"]).name, entry["size"]), []
                )
                if len(candidates) != 1:
                    continue
                candidate = candidates[0]
            digest = sha256_file(candidate)
            if digest == entry["sha256"]:
                entry.update(
                    status=STATUS_ARCHIVED,
                    archive_path=str(candidate),
                    archived_at=_now_iso(),
                )
                resolved.append(entry)
            else:
                entry["error"] = f"hash mismatch against archive candidate {candidate}"
                mismatched.append(entry)
        return resolved, mismatched

    def stale_entries(self, seen_keys):
        """Manifest entries whose key wasn't among the keys seen in the latest listing."""
        return [e for k, e in self.data["files"].items() if k not in seen_keys]

    def summary(self):
        """Per-status counts and byte totals."""
        totals = {}
        for entry in self.data["files"].values():
            bucket = totals.setdefault(entry["status"], {"count": 0, "bytes": 0})
            bucket["count"] += 1
            bucket["bytes"] += entry["size"] or 0
        return totals

    def save(self):
        self.data["updated"] = _now_iso()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.parent / (self.path.name + ".tmp")
        with tmp_path.open("w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2, sort_keys=True)
        os.replace(tmp_path, self.path)
        self._render_txt()

    def _render_txt(self):
        name = self.path.name
        txt_name = name[: -len(".json")] + ".txt" if name.endswith(".json") else name + ".txt"
        txt_path = self.path.parent / txt_name
        lines = []
        for entry in sorted(self.data["files"].values(), key=lambda e: e["rel_path"] or ""):
            digest = (entry.get("sha256") or "")[:12]
            lines.append(
                f"{entry['status']:<11} | {entry['size'] or 0:>14} | {entry['rel_path']} | {digest}"
            )
        content = "\n".join(lines)
        with txt_path.open("w", encoding="utf-8") as f:
            f.write(content + ("\n" if content else ""))
