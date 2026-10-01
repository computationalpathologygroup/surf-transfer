#!/usr/bin/env python3
"""
download_state.py — persistent manifest for the FileSender guest downloader.

Pure logic, no network calls. Tracks per-file download/verification/archive
state across runs so that a batch download can be interrupted, files moved
out of the output directory by hand, and a later run picks up exactly where
it left off without re-downloading anything already accounted for.
"""

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

STATUS_QUEUED = "queued"
STATUS_DOWNLOADING = "downloading"
STATUS_VERIFIED = "verified"
STATUS_MOVED = "moved"
STATUS_ARCHIVED = "archived"
STATUS_FAILED = "failed"

_SIZE_UNITS = {
    "": 1,
    "B": 1,
    "K": 1024,
    "KB": 1024,
    "M": 1024**2,
    "MB": 1024**2,
    "G": 1024**3,
    "GB": 1024**3,
    "T": 1024**4,
    "TB": 1024**4,
}

_SIZE_RE = re.compile(r"^\s*([0-9]*\.?[0-9]+)\s*([A-Za-z]*)\s*$")


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_file(path, chunk_size=8 * 1024 * 1024):
    """Stream a file from disk and return its hex sha256 digest."""
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def parse_size(text):
    """Parse a human size like '200G', '1.5T', '500M', or a plain byte count."""
    if isinstance(text, (int, float)):
        return int(text)
    match = _SIZE_RE.match(str(text))
    if not match:
        raise ValueError(f"Invalid size: {text!r}")
    number, suffix = match.group(1), match.group(2).upper()
    if suffix not in _SIZE_UNITS:
        raise ValueError(f"Unknown size suffix {suffix!r} in {text!r}")
    return int(float(number) * _SIZE_UNITS[suffix])


class RunBudget:
    """Tracks how many files/bytes a single run is allowed to download."""

    def __init__(self, max_files=None, max_bytes=None):
        self.max_files = max_files
        self.max_bytes = max_bytes
        self.files_done = 0
        self.bytes_done = 0
        self.stop_reason = None
        self.oversized_file = False

    @property
    def exhausted(self):
        return self.stop_reason is not None

    def can_start(self, size):
        """Whether a file of this size may begin downloading under the budget."""
        if self.exhausted:
            return False
        if self.max_files is not None and self.files_done >= self.max_files:
            self.stop_reason = f"reached --max-files limit ({self.max_files} file(s))"
            return False
        if self.max_bytes is not None:
            if size > self.max_bytes:
                self.oversized_file = True
                self.stop_reason = (
                    f"file size ({size} bytes) exceeds --max-bytes budget ({self.max_bytes} bytes)"
                )
                return False
            if self.bytes_done + size > self.max_bytes:
                self.stop_reason = f"reached --max-bytes limit ({self.max_bytes} bytes)"
                return False
        return True

    def record(self, size):
        self.files_done += 1
        self.bytes_done += size


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
