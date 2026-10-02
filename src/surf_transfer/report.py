"""Status, JSON and CSV reports built from the manifest. Pure: no I/O except write_csv."""

from __future__ import annotations

import csv
from collections import Counter
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .manifest import Manifest
from .models import (
    ARCHIVED,
    CORRUPT,
    DOWNLOADED,
    EXTRACTED,
    FAILED,
    MOVED,
    QUEUED,
    UNVALIDATED,
    VALIDATING,
    VERIFIED,
    SlideRecord,
)
from .util import format_bytes

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_FAILED = 2  # failed downloads, listing errors, or files left undone
EXIT_CORRUPT = 3
EXIT_BUDGET = 4
EXIT_UNVALIDATED = 5

_FILE_ORDER = (QUEUED, "downloading", DOWNLOADED, FAILED, VERIFIED, MOVED, ARCHIVED, EXTRACTED)
_SLIDE_ORDER = (
    VERIFIED,
    MOVED,
    ARCHIVED,
    EXTRACTED,
    CORRUPT,
    UNVALIDATED,
    VALIDATING,
    DOWNLOADED,
    QUEUED,
)
_SAFE = (VERIFIED, MOVED, ARCHIVED, EXTRACTED)


def _slide_bytes(manifest: Manifest, slide: SlideRecord) -> int:
    return sum(manifest.files[m].size or 0 for m in slide.members if m in manifest.files)


def _failure(slide: SlideRecord) -> dict[str, Any] | None:
    f = slide.failure
    if not f:
        return None
    return {
        "check": f.check,
        "detail": f.detail,
        "members": list(f.members),
        "level": f.level,
        "coord": list(f.coord) if f.coord else None,
    }


def source_states(manifest: Manifest) -> list[dict[str, Any]]:
    """Per source: complete (N slides, all verified) / complete with problems / open."""
    slides_by_source: dict[str, list[SlideRecord]] = {}
    for s in manifest.current_slides():
        slides_by_source.setdefault(s.source_id, []).append(s)
    flagged: Counter[str] = Counter(e.source_id for e in manifest.files.values() if e.flagged)
    out = []
    for sid, info in sorted(manifest.sources.items()):
        slides = slides_by_source.get(sid, [])
        verified = sum(1 for s in slides if s.status in _SAFE)
        complete = info.get("complete")
        if complete and verified == len(slides) and not flagged[sid]:
            state = f"complete ({len(slides)} slides, all verified)"
        elif complete:
            state = (
                f"complete, but {len(slides) - verified} of {len(slides)} slides not verified"
                + (f" and {flagged[sid]} file(s) flagged" if flagged[sid] else "")
            )
        else:
            state = "open (not marked complete)"
        out.append(
            {
                "source": sid,
                "centre": info.get("centre"),
                "state": state,
                "complete": complete,
                "slides": len(slides),
                "verified": verified,
                "flagged_files": flagged[sid],
            }
        )
    return out


def status_data(manifest: Manifest) -> dict[str, Any]:
    slides = manifest.current_slides()
    safe = [s for s in slides if s.status in _SAFE]
    legacy_files = [e for e in manifest.files.values() if e.checks.get("legacy")]
    not_slide_checked = [
        s
        for s in slides
        if s.status in (QUEUED, DOWNLOADED)
        and any(manifest.files[m].checks.get("legacy") for m in s.members if m in manifest.files)
    ]
    return {
        "files": manifest.summary(),
        "slides": manifest.slide_summary(),
        "safe_to_move": {
            "count": sum(1 for s in slides if s.status == VERIFIED),
            "bytes": sum(_slide_bytes(manifest, s) for s in slides if s.status == VERIFIED),
        },
        "already_moved": sum(1 for s in safe if s.status in (MOVED, ARCHIVED)),
        "archives": dict(Counter(s.status for s in manifest.archive_units())),
        "archive_bytes": sum(
            manifest.files[m].size or 0
            for s in manifest.archive_units()
            if s.status == VERIFIED
            for m in s.members
            if m in manifest.files
        ),
        "extracted_bytes": sum(
            manifest.files[m].size or 0
            for s in manifest.archive_units()
            if s.status == EXTRACTED
            for m in s.members
            if m in manifest.files
        ),
        "unvalidated": [
            {"slide": s.name, "source": s.source_id, "failure": _failure(s)}
            for s in slides
            if s.status == UNVALIDATED
        ],
        "corrupt": [
            {"slide": s.name, "source": s.source_id, "failure": _failure(s)}
            for s in slides
            if s.status == CORRUPT
        ],
        "unvalidated_archives": [
            {"archive": s.name, "failure": _failure(s)}
            for s in manifest.archive_units()
            if s.status == UNVALIDATED
        ],
        "failed_files": [
            {"file": e.rel_path, "error": e.error}
            for e in manifest.files.values()
            if e.status == FAILED
        ],
        "flagged_files": [
            {"file": e.rel_path, "flag": e.flagged} for e in manifest.files.values() if e.flagged
        ],
        "legacy_files": len(legacy_files),
        "legacy_slides_not_validated": len(not_slide_checked),
        "sources": source_states(manifest),
    }


def format_status(data: dict[str, Any], state_path: Path | str = "") -> str:
    lines: list[str] = []
    if state_path:
        lines += [f"State file: {state_path}", ""]
    lines.append("Slides (latest delivery of each):")
    slides = data["slides"]
    if not slides:
        lines.append("  none yet")
    for status in _SLIDE_ORDER:
        b = slides.get(status)
        if b:
            lines.append(f"  {status:<11}: {b['count']:>5} slide(s), {format_bytes(b['bytes'])}")
    safe = data["safe_to_move"]
    lines += [
        "",
        f"SAFE TO MOVE out of the output share: {safe['count']} verified slide(s), "
        f"{format_bytes(safe['bytes'])}",
        "  Only `verified` slides count; corrupt / unvalidated / downloaded are NOT safe to move.",
    ]
    if data["already_moved"]:
        lines.append(f"  Already moved/archived: {data['already_moved']} slide(s)")
    if data["corrupt"]:
        lines += ["", f"CORRUPT ({len(data['corrupt'])}) - ask the centre to re-send:"]
        for item in data["corrupt"]:
            f = item["failure"]
            lines.append(f"  ✗ {item['slide']}: [{f['check']}] {f['detail']}")
            lines.append(f"      files: {', '.join(f['members'])}")
    if data["unvalidated"] or data["unvalidated_archives"]:
        lines += [
            "",
            "UNVALIDATED (passed file checks, no slide check exists - not proven readable):",
        ]
        for item in data["unvalidated"]:
            lines.append(f"  ? {item['slide']}: {item['failure']['detail']}")
        for item in data["unvalidated_archives"]:
            lines.append(f"  ? archive {item['archive']}: {item['failure']['detail']}")
    lines += ["", "Files:"]
    for status in _FILE_ORDER:
        b = data["files"].get(status)
        if b:
            lines.append(f"  {status:<11}: {b['count']:>5} file(s), {format_bytes(b['bytes'])}")
    if data["archives"]:
        counts = ", ".join(f"{n} {s}" for s, n in sorted(data["archives"].items()))
        lines.append(f"Archives (zip containers): {counts}")
        if data["extracted_bytes"]:
            lines.append(
                f"  Extracted zip(s) ({format_bytes(data['extracted_bytes'])}) were deleted after "
                "unpacking; they count as done, not as moved."
            )
        if data["archive_bytes"]:
            lines.append(
                f"  Kept zip(s) ({format_bytes(data['archive_bytes'])}) still sit beside the "
                "slides unpacked from them (--keep-zips, or those slides are not yet checked)."
            )
    if data["legacy_files"]:
        lines.append(
            f"Legacy (v1) files: {data['legacy_files']} - kept as-is, never re-downloaded; "
            f"run --revalidate to slide-check those still on disk"
            + (
                f" ({data['legacy_slides_not_validated']} slide(s) waiting)"
                if data["legacy_slides_not_validated"]
                else ""
            )
        )
    if data["failed_files"]:
        lines += ["", f"FAILED files ({len(data['failed_files'])}, retried next run):"]
        lines += [f"  ✗ {f['file']}: {f['error']}" for f in data["failed_files"]]
    if data["flagged_files"]:
        lines += [
            "",
            f"FLAGGED files ({len(data['flagged_files'])}, not downloaded until reviewed "
            "with --accept-flagged):",
        ]
        lines += [f"  ⚑ {f['file']}: {f['flag']}" for f in data["flagged_files"]]
    if data["sources"]:
        lines += ["", "Sources:"]
        for s in data["sources"]:
            label = f" [{s['centre']}]" if s["centre"] else ""
            note = f" - {s['complete']['date']}: {s['complete']['note']}" if s["complete"] else ""
            lines.append(f"  {s['source']}{label}: {s['state']}{note}")
    return "\n".join(lines)


# --- CSV --------------------------------------------------------------------

REPORT_COLUMNS = (
    "source",
    "centre",
    "slide",
    "status",
    "format",
    "kind",
    "checks",
    "file_checks",
    "failure_check",
    "failure_detail",
    "failure_files",
    "failure_level",
    "failure_x",
    "failure_y",
    "member_files",
    "first_seen",
    "received_at",
    "validated_at",
    "container",
    "history",
    "seed",
    "sampled_tiles",
    "deep_check",
    "note",
)


def _file_check_summary(manifest: Manifest, slide: SlideRecord) -> str:
    counts: Counter[str] = Counter()
    for m in slide.members:
        entry = manifest.files.get(m)
        if not entry:
            continue
        c = entry.checks
        if c.get("legacy"):
            counts["legacy"] += 1
        else:
            counts[f"source_checksum={c.get('source_checksum', 'n/a')}"] += 1
    return "; ".join(f"{k} x{n}" for k, n in sorted(counts.items()))


def _history(manifest: Manifest, slide: SlideRecord) -> str:
    parts = []
    for key in slide.replaces:
        older = manifest.slides.get(key)
        if older:
            why = f" ({older.failure.check})" if older.failure else ""
            parts.append(
                f"{older.status}{why} {older.validated_at or older.first_seen or ''} -> replaced"
            )
    if parts:
        parts.append(f"{slide.status} {slide.validated_at or ''}")
    return " ; ".join(parts)


def report_rows(manifest: Manifest, include_superseded: bool = True) -> list[dict[str, Any]]:
    rows = []
    slides = sorted(
        (s for s in manifest.slides.values() if include_superseded or not s.superseded_by),
        key=lambda s: (s.source_id, s.name, manifest.order_key(s)),
    )
    for s in slides:
        f = s.failure
        rows.append(
            {
                "source": s.source_id,
                "centre": s.centre or "",
                "slide": s.name + (" (superseded)" if s.superseded_by else ""),
                "status": s.status,
                "format": s.format or "",
                "kind": s.kind,
                "checks": "; ".join(f"{c.name}:{'ok' if c.passed else 'FAIL'}" for c in s.checks),
                "file_checks": _file_check_summary(manifest, s),
                "failure_check": f.check if f else "",
                "failure_detail": f.detail if f else "",
                "failure_files": " | ".join(f.members) if f else "",
                "failure_level": "" if not f or f.level is None else f.level,
                "failure_x": "" if not f or not f.coord else f.coord[0],
                "failure_y": "" if not f or not f.coord else f.coord[1],
                "member_files": " | ".join(manifest.member_paths(s)),
                "first_seen": s.first_seen or "",
                "received_at": manifest.received_at(s) or "",
                "validated_at": s.validated_at or "",
                "container": s.container or "",
                "history": _history(manifest, s),
                "seed": "" if s.seed is None else s.seed,
                "sampled_tiles": len(s.coords),
                "deep_check": "yes" if s.deep else "",
                "note": s.note or "",
            }
        )
    return rows


def write_csv(path: Path | str, rows: Iterable[dict[str, Any]]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=REPORT_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


# --- exit codes -------------------------------------------------------------


def exit_code(manifest: Manifest, budget_stopped: bool = False, listing_errors: int = 0) -> int:
    """0 = every slide verified. Otherwise the most serious problem, in the order
    corrupt (3) > failed/incomplete (2) > budget-stopped (4) > unvalidated (5)."""
    slides = manifest.current_slides()
    if any(s.status == CORRUPT for s in slides) or any(
        s.status == CORRUPT for s in manifest.archive_units()
    ):
        return EXIT_CORRUPT
    incomplete = (
        listing_errors
        or any(e.status == FAILED for e in manifest.files.values())
        or any(e.flagged for e in manifest.files.values())
    )
    if incomplete:
        return EXIT_FAILED
    if budget_stopped:
        return EXIT_BUDGET
    if any(s.status == UNVALIDATED for s in slides) or any(
        s.status == UNVALIDATED for s in manifest.archive_units()
    ):
        return EXIT_UNVALIDATED
    if any(s.status in (QUEUED, DOWNLOADED, VALIDATING) for s in slides) or any(
        e.status == QUEUED for e in manifest.files.values()
    ):
        return EXIT_FAILED
    return EXIT_OK
