"""Command line entry point: one CLI for FileSender and SurfDrive."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

from . import app, report
from .config import (
    Config,
    RunOptions,
    SurfDriveConfig,
    default_home,
    load_env,
    load_filesender_config,
    load_surfdrive_configs,
    parse_filesender_token,
    parse_surfdrive_folder,
    parse_surfdrive_link,
)
from .manifest import Manifest, SourceScopeError
from .sources import (
    FileSenderSource,
    Source,
    SourceError,
    SurfDriveSource,
    make_filesender_client,
    make_surfdrive_client,
)
from .util import parse_size
from .validation import SlideValidator

DEFAULT_STATE_NAME = ".filesender_state.json"  # unchanged, so v1 manifests migrate in place


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Download FileSender guest transfers and SurfDrive (WebDAV) shares, "
        "and verify every slide. Remote sources are only ever read.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # FileSender: every transfer of a guest voucher, or one transfer by token/link
  surf-transfer -e guest@example.com -o /share/incoming
  surf-transfer --transfer-token 'https://filesender.surf.nl/?s=download&token=...' \\
      -o /share/incoming

  # SurfDrive: a public share link, or a section from surfdrive.ini
  surf-transfer --surfdrive-link https://surfdrive.surf.nl/s/AbCdEf -o /share/incoming
  surf-transfer --surfdrive user -o /share/incoming

  # Bounded batch, resumable across runs
  surf-transfer -e guest@example.com -o /share/incoming --max-files 50 --max-bytes 200G

  # What is safe to move? What is corrupt?
  surf-transfer -o /share/incoming --status
  surf-transfer -o /share/incoming --report slides.csv

  # Re-run slide validation on files already on disk (no downloads)
  surf-transfer -o /share/incoming --revalidate [--deep-check] [--slide NAME]

  # The centre says "that is everything"
  surf-transfer -o /share/incoming --mark-complete "Centre A" --note "email from A, 2026-10-01"

Exit codes: 0 all verified | 1 usage/config error | 2 failed or incomplete |
            3 corrupt slides | 4 stopped by --max-files/--max-bytes | 5 unvalidated slides only
""",
    )
    src = parser.add_argument_group("sources")
    src.add_argument("--list-guests", action="store_true", help="List all guest vouchers")
    src.add_argument("-e", "--email", help="FileSender guest email address")
    src.add_argument("--guest-id", type=int, help="FileSender guest ID")
    src.add_argument(
        "--transfer-token",
        metavar="TOKEN_OR_LINK",
        help="Download one FileSender transfer by its download token or link",
    )
    src.add_argument(
        "--surfdrive-link",
        metavar="URL",
        help="Download a public SurfDrive share link (password: SURFDRIVE_SHARE_PASSWORD)",
    )
    src.add_argument(
        "--surfdrive",
        action="append",
        default=[],
        metavar="NAME",
        help="Use a surfdrive.ini section: 'public', 'user', 'public:<name>' or 'all' (repeatable)",
    )
    src.add_argument("--centre", help="Label for the centre sending these files (provenance)")
    src.add_argument("--limit", type=int, help="Limit number of FileSender transfers (for testing)")
    src.add_argument("-u", "--username", help="FileSender username (overrides config)")
    src.add_argument("-a", "--apikey", help="FileSender API key (overrides config)")
    src.add_argument("-b", "--base-url", help="FileSender base URL (overrides config)")
    src.add_argument("--insecure", action="store_true", help="Skip SSL certificate verification")

    out = parser.add_argument_group("output and state")
    out.add_argument("-o", "--output-dir", default=".", help="Output directory for downloads")
    out.add_argument(
        "--state-file", help=f"State manifest (default: <output-dir>/{DEFAULT_STATE_NAME})"
    )
    out.add_argument(
        "--archive-dir", help="Resolve files moved out of the output dir against this directory"
    )
    out.add_argument(
        "--verify-archive",
        metavar="DIR",
        help="Standalone: hash-verify moved files against DIR, then exit (no credentials needed)",
    )
    out.add_argument(
        "--min-free",
        default="1G",
        metavar="SIZE",
        help="Refuse to start a file unless this much stays free on the output volume (default 1G)",
    )

    run = parser.add_argument_group("download control")
    run.add_argument(
        "--dry-run", action="store_true", help="Sync the manifest and show what would be downloaded"
    )
    run.add_argument(
        "--force",
        action="store_true",
        help="Re-download even if files are already verified/moved/archived",
    )
    run.add_argument(
        "--max-files", type=int, help="Stop starting new files after this many in this run"
    )
    run.add_argument(
        "--max-bytes", help="Stop starting new files after this many bytes (K, M, G, T)"
    )
    run.add_argument(
        "--recheck-hashes",
        action="store_true",
        help="Re-hash local files instead of trusting the manifest",
    )
    run.add_argument(
        "--accept-flagged",
        action="store_true",
        help="Download files flagged as new-after-complete or changed-on-source",
    )

    run.add_argument(
        "--flat",
        action="store_true",
        help="Put files directly in the output folder instead of a per-source subfolder "
        "(one source per output folder; cannot be changed once files are tracked)",
    )
    run.add_argument(
        "--keep-zips",
        action="store_true",
        help="Keep each zip after it is unpacked and its slides are checked "
        "(default: delete it, the extracted slide is what gets moved)",
    )

    run.add_argument(
        "--allow-source-change",
        action="store_true",
        help="Let this run use a source the manifest was not created for "
        "(default: refuse; use a fresh -o instead)",
    )

    val = parser.add_argument_group("slide validation")
    val.add_argument(
        "--validate-workers",
        type=int,
        default=2,
        metavar="N",
        help="Parallel validation subprocesses (default 2)",
    )
    val.add_argument(
        "--sample-tiles",
        type=int,
        default=64,
        metavar="N",
        help="Random level-0 tiles to read per slide (default 64)",
    )
    val.add_argument(
        "--deep-check",
        action="store_true",
        help="Decode every tile at every level (slow; for suspicious slides)",
    )
    val.add_argument(
        "--validate-timeout",
        type=float,
        default=None,
        metavar="SECONDS",
        help="Per-slide timeout (default 900, or 21600 with --deep-check)",
    )
    val.add_argument("--seed", type=int, help="Fix the tile-sampling seed (to reproduce a failure)")
    val.add_argument(
        "--revalidate",
        action="store_true",
        help="Run slide validation on files already on disk, without downloading",
    )
    val.add_argument(
        "--slide",
        action="append",
        default=[],
        metavar="NAME",
        help="With --revalidate: only slides whose name contains NAME (also re-checks verified)",
    )

    rep = parser.add_argument_group("reporting")
    rep.add_argument("--status", action="store_true", help="Print the manifest report and exit")
    rep.add_argument("--json", action="store_true", help="With --status: machine-readable output")
    rep.add_argument(
        "--check-remote",
        action="store_true",
        help="With --status: also list (read-only) and show entries gone from the source",
    )
    rep.add_argument("--report", metavar="CSV", help="Write one row per slide to CSV and exit")
    rep.add_argument(
        "--mark-complete",
        metavar="SOURCE",
        help="Declare a source (id or centre label) complete; needs --note",
    )
    rep.add_argument("--note", help="Who/when, stored with --mark-complete")
    rep.add_argument("-v", "--verbose", action="store_true", help="Verbose output")
    return parser


def setup_logging(state_path: Path, verbose: bool) -> None:
    logger = logging.getLogger("surf_transfer")
    logger.handlers.clear()
    logger.setLevel(logging.DEBUG)
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(console)
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        logfile = logging.FileHandler(state_path.parent / "surf_transfer.log", encoding="utf-8")
        logfile.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(logfile)
    except OSError:
        logger.warning("cannot write a log file next to the manifest")


def build_config(args: argparse.Namespace, env: dict[str, str], home: Path) -> Config:
    output_dir = Path(args.output_dir)
    deep = args.deep_check
    run = RunOptions(
        output_dir=output_dir,
        state_file=Path(args.state_file) if args.state_file else output_dir / DEFAULT_STATE_NAME,
        verbose=args.verbose,
        force=args.force,
        dry_run=args.dry_run,
        recheck_hashes=args.recheck_hashes,
        max_files=args.max_files,
        max_bytes=parse_size(args.max_bytes) if args.max_bytes else None,
        archive_dir=Path(args.archive_dir) if args.archive_dir else None,
        validate_workers=args.validate_workers,
        sample_tiles=args.sample_tiles,
        deep_check=deep,
        validate_timeout=args.validate_timeout or (21600.0 if deep else 900.0),
        seed=args.seed,
        min_free_bytes=parse_size(args.min_free),
        accept_flagged=args.accept_flagged,
        keep_zips=args.keep_zips,
        flat=args.flat,
        allow_source_change=args.allow_source_change,
        slide_filter=tuple(args.slide),
    )
    filesender = load_filesender_config(
        home,
        env,
        {
            "base_url": args.base_url,
            "username": args.username,
            "apikey": args.apikey,
            "centre": args.centre,
        },
    )
    if filesender and args.insecure:
        filesender = replace(filesender, insecure=True)

    surfdrive: list[SurfDriveConfig] = []
    configured = load_surfdrive_configs(home, insecure=args.insecure)
    for name in args.surfdrive:
        if name == "all":
            surfdrive += list(configured)
            continue
        matches = [c for c in configured if name in (c.name, f"{c.mode}:{c.name}", c.mode)]
        if not matches:
            known = ", ".join(f"{c.mode}:{c.name}" for c in configured) or "none (no surfdrive.ini)"
            raise ValueError(f"no surfdrive.ini section matches {name!r} (known: {known})")
        surfdrive += matches[:1] if name in ("public", "user") else matches
    if args.surfdrive_link:
        base_url, token = parse_surfdrive_link(args.surfdrive_link)
        surfdrive.append(
            SurfDriveConfig(
                mode="public",
                base_url=base_url,
                name="link",
                username=token,
                password=env.get("SURFDRIVE_SHARE_PASSWORD", ""),
                remote_folder=parse_surfdrive_folder(args.surfdrive_link),
                centre=args.centre,
                insecure=args.insecure,
            )
        )
    return Config(run=run, filesender=filesender, surfdrive=tuple(surfdrive))


def build_sources(args: argparse.Namespace, config: Config) -> list[Source]:
    sources: list[Source] = []
    wants_filesender = bool(args.email or args.guest_id is not None or args.transfer_token)
    if wants_filesender:
        fs = config.filesender
        if fs is None:
            raise ValueError(
                "FileSender needs a username and API secret: set FILESENDER_USERNAME and "
                "API_SECRET "
                "(env or .env), or use ~/.filesender/filesender.py.ini / -u / -a"
            )
        token = parse_filesender_token(args.transfer_token) if args.transfer_token else None
        sources.append(
            FileSenderSource(
                fs,
                make_filesender_client(fs),
                guest_email=args.email,
                guest_id=args.guest_id,
                token=token,
                limit=args.limit,
            )
        )
    for sd in config.surfdrive:
        sources.append(SurfDriveSource(sd, make_surfdrive_client(sd)))
    return sources


def _print(text: str) -> None:
    print(text)


def cmd_list_guests(config: Config) -> int:
    fs = config.filesender
    if fs is None:
        _print("Error: FileSender username/API secret not configured")
        return report.EXIT_USAGE
    source = FileSenderSource(fs, make_filesender_client(fs))
    try:
        guests = source.get_guests()
    except SourceError as e:
        _print(f"Error: {e}")
        return report.EXIT_FAILED
    if not guests:
        _print("No guests found.")
        return report.EXIT_OK
    _print(f"Found {len(guests)} guest(s):\n")
    for g in guests:
        _print(
            f"Guest ID: {g['id']}\n  Email: {g['email']}\n  Subject: {g.get('subject', 'N/A')}\n"
            f"  Created: {g.get('created', {}).get('formatted', 'N/A')}\n"
            f"  Expires: {g.get('expires', {}).get('formatted', 'N/A')}\n"
            f"  Transfer Count: {g.get('transfer_count', 0)}\n"
        )
    return report.EXIT_OK


def cmd_status(args: argparse.Namespace, config: Config, manifest: Manifest) -> int:
    data = report.status_data(manifest)
    if args.check_remote:
        listings = []
        for source in build_sources(args, config):
            listings.append((source, source.list()))
        seen = {r.key for _, files in listings for r in files}
        data["gone_from_source"] = [
            {"file": e.rel_path, "status": e.status}
            for e in manifest.stale_entries(seen)
            if e.origin == "remote"
        ]
    if args.json:
        _print(json.dumps(data, indent=2, sort_keys=True))
    else:
        _print(report.format_status(data, config.run.state_file))
        for item in data.get("gone_from_source", []):
            _print(f"  gone from source: {item['file']} (status={item['status']})")
    return report.exit_code(manifest)


def cmd_verify_archive(args: argparse.Namespace, config: Config, manifest: Manifest) -> int:
    moved = [e for e in manifest.files.values() if e.status == "moved"]
    _print(f"Verifying {len(moved)} moved file(s) against {args.verify_archive}...\n")
    resolved, mismatched = manifest.resolve_archive(args.verify_archive, moved)
    manifest.save()
    for entry in resolved:
        _print(f"  ✓ {entry.rel_path}")
    if mismatched:
        _print(f"\n❌ {len(mismatched)} file(s) failed hash verification:")
        for entry in mismatched:
            _print(f"  ✗ {entry.rel_path}: {entry.error}")
        return report.EXIT_FAILED
    unresolved = len(moved) - len(resolved) - len(mismatched)
    if unresolved:
        _print(f"\n⚠ {unresolved} moved file(s) had no candidate found under {args.verify_archive}")
    _print(f"\n✓ {len(resolved)} file(s) verified against archive.")
    return report.EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    env = load_env(os.environ)
    try:
        config = build_config(args, env, default_home())
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return report.EXIT_USAGE

    state_path = config.run.state_file
    setup_logging(state_path, args.verbose)
    try:
        manifest = Manifest.load(state_path)
    except (ValueError, json.JSONDecodeError) as e:
        print(f"Error: cannot load manifest {state_path}: {e}", file=sys.stderr)
        return report.EXIT_USAGE

    if args.verify_archive:
        return cmd_verify_archive(args, config, manifest)
    if args.mark_complete:
        if not args.note:
            print('Error: --mark-complete needs --note "<who/when>"', file=sys.stderr)
            return report.EXIT_USAGE
        try:
            ids = manifest.mark_complete(args.mark_complete, args.note)
        except ValueError as e:
            print(f"Error: {e}", file=sys.stderr)
            return report.EXIT_USAGE
        manifest.save()
        _print(f"Marked complete: {', '.join(ids)}")
        return report.EXIT_OK
    if args.report:
        manifest.sync_slides()
        report.write_csv(args.report, report.report_rows(manifest))
        _print(f"Wrote {args.report}")
        return report.EXIT_OK
    if args.status:
        manifest.sync_slides()
        try:
            return cmd_status(args, config, manifest)
        except (ValueError, SourceError) as e:
            print(f"Error: {e}", file=sys.stderr)
            return report.EXIT_USAGE
    if args.list_guests:
        return cmd_list_guests(config)

    runner = SlideValidator()
    if args.revalidate:
        try:
            queued, skipped = app.revalidate(manifest, config.run, runner)
        except KeyboardInterrupt:
            return 130
        _print(f"Revalidated {queued} slide(s); {skipped} not fully on disk.")
        _print(report.format_status(report.status_data(manifest)))
        return report.exit_code(manifest)

    try:
        sources = build_sources(args, config)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return report.EXIT_USAGE
    if not sources:
        parser.print_help()
        print(
            "\nError: specify a source (-e, --guest-id, --transfer-token, --surfdrive, "
            "--surfdrive-link) or one of --status, --report, --revalidate, --verify-archive, "
            "--mark-complete",
            file=sys.stderr,
        )
        return report.EXIT_USAGE

    try:
        summary = app.run(manifest, sources, config.run, runner)
    except SourceScopeError as e:
        print(f"Error: {e}", file=sys.stderr)
        return report.EXIT_USAGE
    except KeyboardInterrupt:
        print("\nInterrupted; manifest saved. Re-run to continue.", file=sys.stderr)
        return 130
    _print("\n" + "=" * 60)
    _print(
        f"Downloaded {summary.downloaded}, failed {summary.failed}, skipped {summary.skipped}, "
        f"deferred {summary.deferred}, no space {summary.no_space}, flagged {summary.flagged}"
    )
    if summary.budget_stop:
        _print(f"Run budget reached: {summary.budget_stop}")
    for err in summary.listing_errors:
        _print(f"Listing error: {err}")
    _print(report.format_status(report.status_data(manifest)))
    _print("=" * 60)
    if config.run.dry_run:
        return report.EXIT_FAILED if summary.listing_errors else report.EXIT_OK
    return report.exit_code(
        manifest,
        budget_stopped=summary.budget_stop is not None,
        listing_errors=len(summary.listing_errors),
    )
