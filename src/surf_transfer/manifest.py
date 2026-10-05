"""The persistent manifest (version 2).

Tracks every file and every slide across FileSender and SurfDrive sources so
a batch can be interrupted, files moved out of the output directory by hand,
and a later run picks up exactly where it left off. No network. Records are
dataclasses in memory and plain JSON on disk; conversion happens only here.
"""

from __future__ import annotations

import copy
import dataclasses
import json
import os
import shutil
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from .models import (
    ARCHIVED,
    CORRUPT,
    DOWNLOADED,
    DOWNLOADING,
    EXTRACTED,
    FAILED,
    KIND_ARCHIVE,
    KIND_FILE,
    KIND_MRXS,
    MOVED,
    QUEUED,
    UNVALIDATED,
    VALIDATING,
    VERIFIED,
    CheckResult,
    Failure,
    FileEntry,
    JobResult,
    RemoteFile,
    SlideRecord,
)
from .parsing import archive_suffix, group_into_slides
from .util import now_iso, safe_relpath, sanitize_folder, sha256_file

MANIFEST_VERSION = 2


class SourceScopeError(ValueError):
    """The run's source differs from the one this manifest was created for."""


_LEGACY_STATUSES = (VERIFIED, MOVED, ARCHIVED)
_SLIDE_VERDICTS = (VERIFIED, CORRUPT, UNVALIDATED)


# --- v1 -> v2 ---------------------------------------------------------------


def migrate_v1_to_v2(data: dict[str, Any]) -> dict[str, Any]:
    """Pure: return a v2 manifest dict built from a v1 one (input untouched).

    Keys get the 'filesender:' prefix. verified/moved/archived entries keep
    their status and are marked checks={'legacy': True}, so they are never
    re-downloaded. Entries interrupted mid-download go back to queued."""
    if data.get("version", 1) == MANIFEST_VERSION:
        return data
    if data.get("version", 1) != 1:
        raise ValueError(f"cannot migrate manifest version {data.get('version')}")
    old = copy.deepcopy(data)
    ordered = sorted(
        old.get("files", {}).items(),
        key=lambda kv: (kv[1].get("downloaded_at") or "", kv[0]),
    )
    files: dict[str, Any] = {}
    for seq, (_, entry) in enumerate(ordered, 1):
        tid, fid = entry["transfer_id"], entry["file_id"]
        key = f"filesender:{tid}:{fid}"
        status = entry.get("status", QUEUED)
        if status == DOWNLOADING:
            status = QUEUED
        files[key] = {
            "key": key,
            "source_id": f"filesender:transfer:{tid}",
            "group_id": str(tid),
            "group_label": entry.get("transfer_subject"),
            "name": entry.get("name"),
            "rel_path": entry.get("rel_path"),
            "size": entry.get("size") or 0,
            "status": status,
            "centre": None,
            "remote_path": None,
            "etag": None,
            "sha256": entry.get("sha256"),
            "checks": {"legacy": True} if status in _LEGACY_STATUSES else {},
            "attempts": entry.get("attempts", 0),
            "adopted": entry.get("adopted", False),
            "origin": "remote",
            "parent": None,
            "first_seen": entry.get("downloaded_at"),
            "seq": seq,
            "downloaded_at": entry.get("downloaded_at"),
            "verified_at": entry.get("verified_at"),
            "archived_at": entry.get("archived_at"),
            "archive_path": entry.get("archive_path"),
            "error": entry.get("error"),
            "flagged": None,
        }
    return {
        "version": MANIFEST_VERSION,
        "updated": old.get("updated"),
        "seq": len(files),
        "sources": {},
        "files": files,
        "slides": {},
        "migrated_from": {"version": 1, "base_url": old.get("base_url")},
    }


# --- dict <-> dataclass (the only place this happens) -----------------------


def _file_from_dict(d: dict[str, Any]) -> FileEntry:
    known = {f.name for f in dataclasses.fields(FileEntry)}
    return FileEntry(**{k: v for k, v in d.items() if k in known})


def _slide_to_dict(slide: SlideRecord) -> dict[str, Any]:
    return dataclasses.asdict(slide)


def _slide_from_dict(d: dict[str, Any]) -> SlideRecord:
    known = {f.name for f in dataclasses.fields(SlideRecord)}
    data = {k: v for k, v in d.items() if k in known}
    data["checks"] = [CheckResult(**c) for c in data.get("checks", [])]
    failure = data.get("failure")
    if failure:
        failure = dict(failure)
        failure["members"] = tuple(failure.get("members", ()))
        if failure.get("coord") is not None:
            failure["coord"] = tuple(failure["coord"])
        data["failure"] = Failure(**failure)
    data["coords"] = [tuple(c) for c in data.get("coords", [])]
    return SlideRecord(**data)


@dataclasses.dataclass
class SyncResult:
    inserted: list[FileEntry] = dataclasses.field(default_factory=list)
    flagged: list[FileEntry] = dataclasses.field(default_factory=list)


def _scope(entry: FileEntry) -> str:
    """Identifies the transfer/share a file belongs to, independent of source_id."""
    kind = entry.key.removeprefix("extracted:").split(":", 1)[
        0
    ]  # extracted files share their zip's scope
    return f"{kind}:{entry.group_id}"


class Manifest:
    """JSON-backed record of every file and slide seen across all sources."""

    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)
        self.files: dict[str, FileEntry] = {}
        self.slides: dict[str, SlideRecord] = {}
        self.sources: dict[str, dict[str, Any]] = {}
        self.source_scope: list[str] = []  # what this manifest was created for; [] = unrecorded
        self.flat = False  # files sit directly under the output dir, no per-source folder
        self.updated = now_iso()
        self.seq = 0
        self.migrated_from: dict[str, Any] | None = None
        self._pending_v1_backup = False
        self._archive_index_cache: dict[str, dict[tuple[str, int], list[Path]]] = {}

    # --- persistence --------------------------------------------------------

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> Manifest:
        manifest = cls(path)
        p = Path(path)
        if not p.exists():
            return manifest
        with p.open("r", encoding="utf-8") as f:
            data = json.load(f)
        version = data.get("version", 1)
        if version > MANIFEST_VERSION:
            raise ValueError(
                f"{p} is manifest version {version}; this tool understands up to {MANIFEST_VERSION}"
            )
        if version == 1:
            data = migrate_v1_to_v2(data)
            manifest._pending_v1_backup = True
        manifest.files = {k: _file_from_dict(v) for k, v in data.get("files", {}).items()}
        manifest.slides = {k: _slide_from_dict(v) for k, v in data.get("slides", {}).items()}
        manifest.sources = data.get("sources", {})
        manifest.source_scope = list(data.get("source_scope") or [])
        manifest.flat = bool(data.get("flat", False))
        manifest.seq = data.get("seq", len(manifest.files))
        manifest.migrated_from = data.get("migrated_from")
        manifest.updated = data.get("updated") or now_iso()
        # A crash mid-download / mid-validation leaves entries in a transient state.
        for entry in manifest.files.values():
            if entry.status == DOWNLOADING:
                entry.status = QUEUED
        for slide in manifest.slides.values():
            if slide.status == VALIDATING:
                slide.status = QUEUED
        return manifest

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": MANIFEST_VERSION,
            "updated": self.updated,
            "seq": self.seq,
            "sources": self.sources,
            "source_scope": self.source_scope,
            "flat": self.flat,
            "files": {k: dataclasses.asdict(v) for k, v in self.files.items()},
            "slides": {k: _slide_to_dict(v) for k, v in self.slides.items()},
            "migrated_from": self.migrated_from,
        }

    def save(self) -> None:
        self.updated = now_iso()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self._pending_v1_backup and self.path.exists():
            backup = self.path.with_name(self.path.name + ".v1.bak")
            if not backup.exists():
                shutil.copy2(self.path, backup)
        self._pending_v1_backup = False
        tmp_path = self.path.with_name(self.path.name + ".tmp")
        with tmp_path.open("w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2, sort_keys=True)
        os.replace(tmp_path, self.path)
        self._render_txt()

    def _render_txt(self) -> None:
        name = self.path.name
        txt_name = name[: -len(".json")] + ".txt" if name.endswith(".json") else name + ".txt"
        lines = []
        for entry in sorted(self.files.values(), key=lambda e: e.rel_path or ""):
            lines.append(
                f"{entry.status:<11} | {entry.size or 0:>14} | {entry.rel_path} | "
                f"{(entry.sha256 or '')[:12]}"
            )
        content = "\n".join(lines)
        (self.path.parent / txt_name).write_text(
            content + ("\n" if content else ""), encoding="utf-8"
        )

    # --- files --------------------------------------------------------------

    def get(self, key: str) -> FileEntry | None:
        return self.files.get(key)

    def needs_download(self, key: str) -> bool:
        entry = self.files[key]
        return entry.status in (QUEUED, FAILED) and entry.flagged is None

    def claim_layout(self, flat: bool, source_count: int) -> None:
        """Pin the folder layout. Flat puts files directly in the output dir, so it needs
        exactly one source, and cannot be switched on a manifest that already tracks files."""
        if flat and source_count > 1:
            raise SourceScopeError(
                "--flat puts files directly in the output folder, so it supports one source "
                "per output folder; use a separate -o per source."
            )
        if flat != self.flat:
            if any(e.origin == "remote" for e in self.files.values()):
                have, want = ("flat", "per-source folders") if self.flat else ("per-source folders", "flat")
                raise SourceScopeError(
                    f"{self.path} already tracks files with {have} layout; this run asks for "
                    f"{want}. Use a fresh -o (or a fresh --state-file) to change layout."
                )
            self.flat = flat

    def _folder_for(self, scope: str, label: str | None, group_id: str) -> str:
        if self.flat:
            return ""
        for entry in self.files.values():
            if entry.origin == "remote" and _scope(entry) == scope:
                return (entry.rel_path or "").split("/")[0]
        base = sanitize_folder(label, fallback=f"transfer_{group_id}")
        taken = {
            (e.rel_path or "").split("/")[0] for e in self.files.values() if _scope(e) != scope
        }
        return base if base not in taken else f"{base}_{group_id}"

    def sync_listing(self, remote_files: Sequence[RemoteFile], centre: str | None) -> SyncResult:
        """Insert files from a listing that aren't tracked yet, flag the ones that
        are new on a source already marked complete or that changed on the source
        after being downloaded. Never changes the status of an existing entry."""
        result = SyncResult()
        rel_paths = {e.rel_path: e.key for e in self.files.values()}
        for remote in remote_files:
            info = self.sources.setdefault(
                remote.source_id,
                {"kind": remote.source_id.split(":", 1)[0], "centre": centre, "complete": None},
            )
            if centre and not info.get("centre"):
                info["centre"] = centre

            existing = self.files.get(remote.key)
            if existing is not None:
                self._refresh_existing(existing, remote, centre, result)
                continue

            scope = f"{remote.key.split(':', 1)[0]}:{remote.group_id}"
            self.seq += 1
            entry = FileEntry(
                key=remote.key,
                source_id=remote.source_id,
                group_id=remote.group_id,
                group_label=remote.group_label,
                name=remote.name,
                rel_path="",
                size=remote.size,
                centre=centre,
                remote_path=remote.remote_path,
                etag=remote.etag,
                first_seen=now_iso(),
                seq=self.seq,
            )
            problem = remote.problem
            try:
                folder = self._folder_for(scope, remote.group_label, remote.group_id)
                entry.rel_path = "/".join(p for p in (folder, safe_relpath(remote.name)) if p)
            except ValueError as e:
                folder = self._folder_for(scope, remote.group_label, remote.group_id)
                entry.rel_path = "/".join(
                    p for p in (folder, "__invalid__", remote.key.replace(":", "_")) if p
                )
                problem = f"unsafe remote path: {e}"
            if problem is None and entry.rel_path in rel_paths:
                problem = f"name collision with {rel_paths[entry.rel_path]} at {entry.rel_path}"
            if problem:
                entry.status = FAILED
                entry.error = problem
            else:
                rel_paths[entry.rel_path] = entry.key
            complete = info.get("complete")
            if complete:
                entry.flagged = f"appeared after source was marked complete on {complete['date']}"
                result.flagged.append(entry)
            self.files[entry.key] = entry
            result.inserted.append(entry)
        return result

    def _refresh_existing(
        self, entry: FileEntry, remote: RemoteFile, centre: str | None, result: SyncResult
    ) -> None:
        if (
            entry.source_id.startswith("filesender:transfer:")
            and remote.source_id != entry.source_id
        ):
            entry.source_id = remote.source_id  # claim a migrated v1 entry for its guest source
        if centre and not entry.centre:
            entry.centre = centre
        if remote.problem and entry.status in (QUEUED, FAILED):
            entry.status, entry.error = FAILED, remote.problem
        changed = remote.size != entry.size or (
            remote.etag and entry.etag and remote.etag != entry.etag
        )
        if not changed:
            return
        if entry.status in (QUEUED, FAILED):
            entry.size, entry.etag = remote.size, remote.etag or entry.etag
        elif entry.flagged is None:
            entry.flagged = "changed on the source after it was downloaded (size/etag differs)"
            result.flagged.append(entry)

    def accept_flagged(self) -> list[FileEntry]:
        """Clear every flag (the user has reviewed them); the file downloads next run."""
        accepted = [e for e in self.files.values() if e.flagged]
        for entry in accepted:
            entry.checks["flag_accepted"] = entry.flagged
            entry.flagged = None
            if entry.status in (VERIFIED, DOWNLOADED, MOVED, ARCHIVED, EXTRACTED):
                entry.status = QUEUED  # source changed under us: fetch the new version
                self.reopen_slides_of([entry.key])
        return accepted

    def reconcile_local(self, output_dir: str | os.PathLike[str]) -> list[FileEntry]:
        """Detect verified files that vanished or changed since verification, and
        downloaded files that vanished before their slide was verified."""
        output_dir = Path(output_dir)
        changed: list[FileEntry] = []
        for entry in self.files.values():
            if entry.status not in (VERIFIED, DOWNLOADED):
                continue
            local = output_dir / (entry.rel_path or "")
            if not local.exists():
                if entry.status == VERIFIED:
                    entry.status = MOVED
                else:
                    entry.status = FAILED
                    entry.error = "missing locally before its slide was verified"
                changed.append(entry)
            elif local.stat().st_size != entry.size:
                entry.status = FAILED
                entry.error = "local file size changed since verification"
                changed.append(entry)
        self.reopen_slides_of(e.key for e in changed if e.status == FAILED)
        self.refresh_slide_locations()
        return changed

    def adopt_existing(self, output_dir: str | os.PathLike[str], entry: FileEntry) -> bool:
        """Adopt a queued entry that's already on disk with a matching size."""
        if entry.status != QUEUED:
            return False
        local = Path(output_dir) / (entry.rel_path or "")
        if not local.exists() or local.stat().st_size != entry.size:
            return False
        digest = sha256_file(local)
        entry.sha256 = digest
        entry.status = DOWNLOADED
        entry.adopted = True
        entry.downloaded_at = now_iso()
        entry.checks = {
            "size": True,
            "source_checksum": "not-checked-adopted",
            "local_sha256": digest,
        }
        return True

    def _archive_index(self, archive_dir: Path) -> dict[tuple[str, int], list[Path]]:
        key = str(archive_dir)
        if key not in self._archive_index_cache:
            index: dict[tuple[str, int], list[Path]] = {}
            for p in archive_dir.rglob("*"):
                if not p.is_file():
                    continue
                try:
                    size = p.stat().st_size
                except OSError:
                    continue
                index.setdefault((p.name, size), []).append(p)
            self._archive_index_cache[key] = index
        return self._archive_index_cache[key]

    def resolve_archive(
        self, archive_dir: str | os.PathLike[str], entries: Iterable[FileEntry] | None = None
    ) -> tuple[list[FileEntry], list[FileEntry]]:
        """Match moved entries against files under archive_dir by rel_path, then by
        (basename, size). Returns (resolved, mismatched); entries with no candidate
        are left untouched in neither list."""
        archive = Path(archive_dir)
        if entries is None:
            entries = [e for e in self.files.values() if e.status == MOVED]
        resolved: list[FileEntry] = []
        mismatched: list[FileEntry] = []
        for entry in entries:
            rel = entry.rel_path or ""
            candidate = archive / rel
            if not candidate.exists():
                candidates = self._archive_index(archive).get((Path(rel).name, entry.size), [])
                if len(candidates) != 1:
                    continue
                candidate = candidates[0]
            if sha256_file(candidate) == entry.sha256:
                entry.status = ARCHIVED
                entry.archive_path = str(candidate)
                entry.archived_at = now_iso()
                resolved.append(entry)
            else:
                entry.error = f"hash mismatch against archive candidate {candidate}"
                mismatched.append(entry)
        self.refresh_slide_locations()
        return resolved, mismatched

    def stale_entries(self, seen_keys: Iterable[str]) -> list[FileEntry]:
        seen = set(seen_keys)
        return [e for k, e in self.files.items() if k not in seen]

    # --- sources ------------------------------------------------------------

    def claim_source_scope(self, scopes: Iterable[str], allow_change: bool = False) -> None:
        """Pin the manifest to the sources of this run, before anything is listed.
        An unrecorded manifest (new, or from before this guard) adopts them. Later runs
        may use any subset of the recorded scopes; a scope that is not recorded raises
        SourceScopeError unless allow_change, which adds it to the record."""
        wanted = sorted(set(scopes))
        if not self.source_scope:
            self.source_scope = wanted
            return
        new = [s for s in wanted if s not in self.source_scope]
        if not new:
            return
        if not allow_change:
            raise SourceScopeError(
                f"{self.path} was created for {', '.join(self.source_scope)}, but this run "
                f"uses {', '.join(new)}. Mixing sources in one output folder would mis-track "
                "files: use a fresh -o for the new source (or pass --allow-source-change if "
                "this is intended)."
            )
        self.source_scope = sorted({*self.source_scope, *wanted})

    def mark_complete(self, selector: str, note: str, date: str | None = None) -> list[str]:
        """Declare a source (matched by id or centre label) complete. Returns the ids."""
        matches = [
            sid for sid, info in self.sources.items() if selector in (sid, info.get("centre"))
        ]
        if not matches:
            known = ", ".join(sorted(self.sources)) or "none yet"
            raise ValueError(f"no source matches {selector!r} (known: {known})")
        stamp = date or now_iso()[:10]
        for sid in matches:
            self.sources[sid]["complete"] = {"date": stamp, "note": note}
        return matches

    # --- slides -------------------------------------------------------------

    def sync_slides(self) -> None:
        """Group the files of each source into slide records, keep existing
        verdicts, and link re-sends of a slide name to the delivery they replace."""
        by_source: dict[str, list[FileEntry]] = {}
        for entry in self.files.values():
            by_source.setdefault(entry.source_id, []).append(entry)

        for source_id, entries in by_source.items():
            by_key = {e.key: e for e in entries}
            for group in group_into_slides((e.key, e.rel_path) for e in entries):
                if group.kind == KIND_FILE:
                    continue
                members = sorted(group.member_keys)
                first = by_key[members[0]]
                index = by_key[group.index_key] if group.index_key else None
                anchor = index.rel_path if index else group.folder + "/" + group.name
                key = f"slide:{source_id}:{anchor}"
                container = next((by_key[m].parent for m in members if by_key[m].parent), None)
                record = self.slides.get(key)
                if record is None:
                    record = self.slides[key] = SlideRecord(
                        key=key,
                        source_id=source_id,
                        name=group.name,
                        kind=group.kind,
                        members=members,
                        centre=first.centre,
                        container=container,
                        first_seen=min(by_key[m].first_seen or "" for m in members) or None,
                        generation=_scope(first),
                        folder=group.folder,
                    )
                elif record.members != members:
                    record.members = members
                    if record.status in _SLIDE_VERDICTS + (MOVED, ARCHIVED, EXTRACTED):
                        self._reset_slide(record, "members changed after the last verdict")
                if record.centre is None:
                    record.centre = first.centre
                record.folder = group.folder
                if container:
                    record.container = container  # members may have joined from a zip later
        self._link_resends()

    def _reset_slide(self, slide: SlideRecord, note: str) -> None:
        slide.status = QUEUED
        slide.checks, slide.failure, slide.metadata = [], None, {}
        slide.coords, slide.seed, slide.validated_at = [], None, None
        slide.note = note

    def reopen_slides_of(self, file_keys: Iterable[str]) -> None:
        keys = set(file_keys)
        for slide in self.slides.values():
            if keys & set(slide.members) and slide.status != QUEUED:
                self._reset_slide(slide, "a member file changed or was re-downloaded")

    def order_key(self, slide: SlideRecord) -> tuple[int, str]:
        seqs = [self.files[m].seq for m in slide.members if m in self.files]
        return (min(seqs) if seqs else 0, slide.key)

    def _link_resends(self) -> None:
        """Link records with the same (source, name) from different deliveries."""
        by_name: dict[tuple[str, str], list[SlideRecord]] = {}
        for slide in self.slides.values():
            slide.replaces, slide.superseded_by = [], []
            if slide.kind != KIND_ARCHIVE:
                by_name.setdefault((slide.source_id, slide.name), []).append(slide)
        for records in by_name.values():
            records.sort(key=self.order_key)
            for i, newer in enumerate(records):
                for older in records[:i]:
                    same_delivery = older.generation == newer.generation
                    if same_delivery and (older.container or newer.container):
                        continue  # a loose copy and its zipped twin are not a re-send
                    if same_delivery and self.order_key(older)[0] == self.order_key(newer)[0]:
                        continue
                    newer.replaces.append(older.key)
                    older.superseded_by.append(newer.key)

    def current_slides(self) -> list[SlideRecord]:
        """Slides that are the latest delivery of their name (archives excluded)."""
        return [s for s in self.slides.values() if not s.superseded_by and s.kind != KIND_ARCHIVE]

    def archive_units(self) -> list[SlideRecord]:
        return [s for s in self.slides.values() if s.kind == KIND_ARCHIVE]

    def slide_ready(self, slide: SlideRecord) -> bool:
        """A slide may be validated once every member file has been downloaded."""
        if slide.status not in (QUEUED, DOWNLOADED) or not slide.members:
            return False
        if not all(m in self.files and self.files[m].status == DOWNLOADED for m in slide.members):
            return False
        if slide.kind == KIND_MRXS and self._pending_archive_beside(slide):
            return False  # a zip in the same folder may still supply its data files
        if slide.kind == KIND_ARCHIVE and self._pending_loose_beside(slide):
            return False  # unpack only once the loose files beside it are in place
        return True

    def _pending_archive_beside(self, slide: SlideRecord) -> bool:
        return any(
            a.source_id == slide.source_id
            and a.folder == slide.folder
            and a.status in (QUEUED, DOWNLOADED, VALIDATING)
            for a in self.archive_units()
        )

    def _pending_loose_beside(self, archive: SlideRecord) -> bool:
        return any(
            e.origin == "remote"
            and e.source_id == archive.source_id
            and e.status in (QUEUED, DOWNLOADING)
            and e.key not in archive.members
            and e.rel_path.rpartition("/")[0] == archive.folder
            and not archive_suffix(e.rel_path)
            and not e.flagged
            for e in self.files.values()
        )

    def slide_present_locally(self, slide: SlideRecord, output_dir: str | os.PathLike[str]) -> bool:
        return bool(slide.members) and all(
            m in self.files
            and (Path(output_dir) / (self.files[m].rel_path or "")).is_file()
            and (Path(output_dir) / (self.files[m].rel_path or "")).stat().st_size
            == self.files[m].size
            for m in slide.members
        )

    def member_paths(self, slide: SlideRecord) -> list[str]:
        return [self.files[m].rel_path for m in slide.members if m in self.files]

    def received_at(self, slide: SlideRecord) -> str | None:
        stamps = [
            stamp
            for m in slide.members
            if m in self.files and (stamp := self.files[m].downloaded_at)
        ]
        return max(stamps) if stamps else None

    def refresh_slide_locations(self) -> None:
        """verified / moved / archived / extracted at slide level follow the member files."""
        for slide in self.slides.values():
            if slide.status not in (VERIFIED, MOVED, ARCHIVED, EXTRACTED):
                continue
            statuses = {self.files[m].status for m in slide.members if m in self.files}
            if statuses and statuses <= {EXTRACTED}:
                slide.status = EXTRACTED
            elif statuses and statuses <= {ARCHIVED}:
                slide.status = ARCHIVED
            elif statuses and statuses <= {MOVED, ARCHIVED}:
                slide.status = MOVED
            else:
                slide.status = VERIFIED

    def deletable_zips(self) -> list[FileEntry]:
        """Zips whose contents are safely out: unpacked with every member CRC-verified
        (an unpack that hit zip_conflict / zip_paths / zip_member_* never reaches
        VERIFIED), and every slide fed by them at a final verdict. A corrupt slide
        counts as final: its bytes equal the zip's, so the zip adds no evidence."""
        final = _SLIDE_VERDICTS + (MOVED, ARCHIVED, EXTRACTED)
        out: list[FileEntry] = []
        for archive in self.archive_units():
            if archive.status != VERIFIED or not archive.members:
                continue
            parent = self.files.get(archive.members[0])
            if (
                parent is None
                or parent.origin != "remote"
                or parent.status != VERIFIED
                or parent.checks.get("zip_crc") is not True
            ):
                continue
            settled = True
            for child in (e for e in self.files.values() if e.parent == parent.key):
                fed = [
                    s
                    for s in self.slides.values()
                    if s.kind != KIND_ARCHIVE and child.key in s.members
                ]
                if fed:
                    settled = all(s.status in final for s in fed)
                else:
                    settled = child.status in (VERIFIED, MOVED, ARCHIVED)
                if not settled:
                    break
            if settled:
                out.append(parent)
        return out

    def mark_extracted(self, entry: FileEntry) -> None:
        """Record that the tool deleted this zip itself. Size and sha256 stay; unlike a
        VERIFIED file that vanished (-> MOVED), nothing is expected at the archive."""
        entry.status = EXTRACTED
        entry.removed_at = now_iso()
        entry.checks["removed"] = "deleted after extraction"
        self.refresh_slide_locations()

    # --- applying worker results (called by Pipeline, the only writer) ------

    def begin_validation(self, slide: SlideRecord) -> None:
        slide.status = VALIDATING
        slide.received_at = self.received_at(slide)

    def apply_result(self, result: JobResult) -> list[str]:
        """Record a worker's verdict. Returns keys of slides newly discovered
        (from unpacking an archive) that are now waiting for validation."""
        slide = self.slides[result.slide_key]
        now = now_iso()
        slide.status = result.status
        slide.format = result.format or slide.format
        slide.checks = list(result.checks)
        slide.failure = result.failure
        slide.metadata = result.metadata
        slide.seed, slide.coords, slide.deep = result.seed, list(result.coords), result.deep
        slide.validated_at = now
        slide.note = None
        if result.kind == "unpack":
            return self._apply_unpack(slide, result)
        if result.status == VERIFIED:
            for key in slide.members:
                entry = self.files.get(key)
                if entry and entry.status in (DOWNLOADED, VERIFIED):
                    if entry.status == DOWNLOADED:
                        entry.status = VERIFIED
                        entry.verified_at = now
                    entry.checks["slide"] = slide.key  # legacy files keep checks["legacy"] too
        return []

    def _apply_unpack(self, archive: SlideRecord, result: JobResult) -> list[str]:
        now = now_iso()
        parent_key = archive.members[0]
        parent = self.files[parent_key]
        if result.status == VERIFIED:
            parent.status = VERIFIED
            parent.verified_at = now
            parent.checks["zip_crc"] = True
            parent.checks["extracted_files"] = len(result.extracted)
        base = result.extract_dir or ""
        by_path = {e.rel_path: e for e in self.files.values()}
        for item in result.extracted:
            rel = f"{base}/{item.rel_path}" if base else item.rel_path
            key = f"extracted:{parent_key}:{item.rel_path}"
            if key in self.files:
                continue
            if item.existing and rel in by_path:
                continue  # an identical file is already tracked under its own key
            self.seq += 1
            checks: dict[str, Any] = {"size": True, "zip_crc": True, "local_sha256": item.sha256}
            if item.existing:
                checks["pre_existing_identical"] = True
            self.files[key] = FileEntry(
                key=key,
                source_id=parent.source_id,
                group_id=parent.group_id,
                group_label=parent.group_label,
                name=item.rel_path,
                rel_path=rel,
                size=item.size,
                status=DOWNLOADED,
                centre=parent.centre,
                sha256=item.sha256,
                checks=checks,
                origin="extracted",
                parent=parent_key,
                first_seen=now,
                seq=self.seq,
                downloaded_at=now,
            )
        before = set(self.slides)
        self.sync_slides()
        return sorted(set(self.slides) - before)

    # --- reporting helpers --------------------------------------------------

    def summary(self) -> dict[str, dict[str, int]]:
        """Per-status file counts and byte totals."""
        totals: dict[str, dict[str, int]] = {}
        for entry in self.files.values():
            bucket = totals.setdefault(entry.status, {"count": 0, "bytes": 0})
            bucket["count"] += 1
            bucket["bytes"] += entry.size or 0
        return totals

    def slide_summary(self) -> dict[str, dict[str, int]]:
        """Per-status slide counts and member bytes, latest deliveries only."""
        totals: dict[str, dict[str, int]] = {}
        for slide in self.current_slides():
            bucket = totals.setdefault(slide.status, {"count": 0, "bytes": 0})
            bucket["count"] += 1
            bucket["bytes"] += sum(
                self.files[m].size or 0 for m in slide.members if m in self.files
            )
        return totals
