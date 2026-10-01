#!/usr/bin/env python3
"""
FileSender Guest Transfer Downloader
Downloads all transfers from a specific guest voucher.
"""

import argparse
import requests
import hmac
import hashlib
import time
import os
import re
import sys
import configparser
from datetime import datetime, timezone
from pathlib import Path
from os.path import expanduser
import urllib3

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

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Default settings - safe at import time, no I/O and no argv parsing.
base_url = '[base_url]'
username = None
apikey = None
homepath = expanduser("~")
debug = False
insecure = False
force_download = False

_ALL_STATUSES = (
    STATUS_QUEUED,
    STATUS_DOWNLOADING,
    STATUS_VERIFIED,
    STATUS_MOVED,
    STATUS_ARCHIVED,
    STATUS_FAILED,
)


def _now_iso():
    return datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def load_config():
    """Load base_url/username/apikey from filesender.py.ini, if one exists."""
    config = configparser.ConfigParser()
    config_paths = [
        homepath + '/.filesender/filesender.py.ini',
        './.filesender/filesender.py.ini',  # Local directory
        './filesender.py.ini'  # Current directory
    ]
    for config_path in config_paths:
        if Path(config_path).exists():
            config.read(config_path)
            break

    cfg_base_url = config['system'].get('base_url', '[base_url]') if 'system' in config else '[base_url]'
    cfg_username = config['user'].get('username') if 'user' in config else None
    cfg_apikey = config['user'].get('apikey') if 'user' in config else None
    return cfg_base_url, cfg_username, cfg_apikey


def build_arg_parser():
    parser = argparse.ArgumentParser(
        description='Download all transfers from a specific FileSender guest',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
Examples:
  # List all guests
  python3 download_guest_transfers.py --list-guests

  # Download all transfers from a guest by email
  python3 download_guest_transfers.py -e guest@example.com -o ~/Downloads

  # Download only first 5 transfers (for testing)
  python3 download_guest_transfers.py -e guest@example.com -o ~/Downloads --limit 5

  # Download by guest ID
  python3 download_guest_transfers.py --guest-id 12345 -o ~/Downloads

  # Bounded batch, resumable across runs
  python3 download_guest_transfers.py -e guest@example.com -o ~/Downloads --max-files 50 --max-bytes 200G

  # Resolve files moved out of the output dir against their new home
  python3 download_guest_transfers.py -e guest@example.com -o ~/Downloads --archive-dir /mnt/archive
    '''
    )

    parser.add_argument('--list-guests', action='store_true', help='List all guest vouchers')
    parser.add_argument('-e', '--email', help='Guest email address')
    parser.add_argument('--guest-id', type=int, help='Guest ID')
    parser.add_argument('-o', '--output-dir', default='.', help='Output directory for downloads')
    parser.add_argument('--limit', type=int, help='Limit number of transfers to download (for testing)')
    parser.add_argument('-v', '--verbose', action='store_true', help='Verbose output')
    parser.add_argument('--insecure', action='store_true', help='Skip SSL certificate verification')
    parser.add_argument('--dry-run', action='store_true', help='Sync the manifest and show what would be downloaded, without downloading')
    parser.add_argument('--force', action='store_true', help='Force re-download even if files are already verified/moved/archived')
    parser.add_argument('-u', '--username', help='FileSender username (overrides config file)')
    parser.add_argument('-a', '--apikey', help='FileSender API key (overrides config file)')
    parser.add_argument('-b', '--base-url', help='FileSender base URL (overrides config file)')

    parser.add_argument('--state-file', help='Path to the state manifest (default: <output-dir>/.filesender_state.json)')
    parser.add_argument('--max-files', type=int, help='Stop downloading new files once this many have completed in this run')
    parser.add_argument('--max-bytes', help='Stop downloading new files once this many bytes have completed in this run (accepts suffixes: K, M, G, T)')
    parser.add_argument('--archive-dir', help='Resolve files moved out of the output dir against this directory before downloading')
    parser.add_argument('--verify-archive', metavar='DIR', help='Standalone: resolve and hash-verify moved files against DIR, then exit (no downloads, no credentials required)')
    parser.add_argument('--status', action='store_true', help='Print the state manifest report and exit')
    parser.add_argument('--recheck-hashes', action='store_true', help='Re-hash local verified files from disk instead of trusting the manifest')

    return parser


def parse_args(argv=None):
    return build_arg_parser().parse_args(argv)


def make_signed_request(method, path, params=None):
    """Make an authenticated request to the FileSender API"""
    if params is None:
        params = {}

    params['remote_user'] = username
    params['timestamp'] = str(round(time.time()))

    # Build signature - flatten params properly
    flat_params = '&'.join([f'{k}={v}' for k, v in sorted(params.items())])
    # Extract the domain without the protocol (remove https:// or http:// once)
    domain = base_url.replace('https://', '', 1).replace('http://', '', 1)
    signed_string = f'{method.lower()}&{domain}{path}?{flat_params}'

    signature = hmac.new(apikey.encode(), signed_string.encode('ascii'), hashlib.sha1).hexdigest()
    params['signature'] = signature

    url = base_url + path
    headers = {'Accept': 'application/json'}

    try:
        if debug:
            print(f'  API: {method.upper()} {path}')
            print(f'  Signed string: {signed_string}')
            print(f'  Signature: {signature}')

        if method.lower() == 'get':
            response = requests.get(url, params=params, headers=headers, verify=not insecure, timeout=30)
        else:
            response = requests.request(method, url, params=params, headers=headers, verify=not insecure, timeout=30)

        response.raise_for_status()
        return response
    except Exception as e:
        print(f'Error calling {path}: {e}')
        if debug and hasattr(e, 'response') and e.response:
            print(f'Response: {e.response.text[:500]}')
        return None

def get_guests():
    """Get all guest vouchers"""
    response = make_signed_request('get', '/guest')
    if response and response.status_code == 200:
        return response.json()
    return []

def get_transfers():
    """Get all transfers"""
    response = make_signed_request('get', '/transfer')
    if response and response.status_code == 200:
        return response.json()
    return []

def get_transfer_details(transfer_id):
    """Get detailed information about a specific transfer"""
    response = make_signed_request('get', f'/transfer/{transfer_id}')
    if response and response.status_code == 200:
        return response.json()
    return None


def format_bytes(bytes_val):
    """Format bytes into human-readable format"""
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if bytes_val < 1024.0:
            return f"{bytes_val:.2f} {unit}"
        bytes_val /= 1024.0
    return f"{bytes_val:.2f} PB"


def format_speed(bytes_per_sec):
    """Format download speed"""
    return format_bytes(bytes_per_sec) + "/s"


def resolve_transfer_folder(state, transfer_id, subject):
    """Sanitize a transfer's subject into a folder name. If another transfer_id
    already claims that folder name in the manifest, append _<transfer_id> so
    two transfers sharing a subject don't silently merge their files. Existing
    layouts are untouched - only a newly colliding transfer gets suffixed."""
    subject = (subject or '').strip()
    if subject:
        folder_name = re.sub(r'[<>:"/\\|?*]', '_', subject)
        folder_name = folder_name.strip('. ')
        if not folder_name:
            folder_name = f'transfer_{transfer_id}'
    else:
        folder_name = f'transfer_{transfer_id}'

    for entry in state.data['files'].values():
        if entry['transfer_id'] == transfer_id:
            continue
        rel_path = entry.get('rel_path') or ''
        if rel_path.split('/')[0] == folder_name:
            return f'{folder_name}_{transfer_id}'
    return folder_name


def _fetch_and_sync(transfer_id, transfer_details, output_dir, state):
    """Fetch a transfer's file list via its download token, resolve its output
    folder, and sync any not-yet-tracked files into the manifest, adopting
    ones that are already present on disk with a matching size. Returns
    (download_token, file_list, folder_name), or None if the file list
    couldn't be retrieved."""
    if 'recipients' not in transfer_details or not transfer_details['recipients']:
        print(f'  ❌ No recipients found for transfer {transfer_id}')
        return None

    download_token = transfer_details['recipients'][0]['token']

    response = make_signed_request('get', '/transfer/fileidsextended', {'token': download_token})
    if not response or response.status_code != 200:
        print(f'  ❌ Failed to get file list')
        return None

    file_list = response.json()
    if not file_list:
        print(f'  ❌ No files in transfer')
        return None

    subject = transfer_details.get('subject', '').strip()
    folder_name = resolve_transfer_folder(state, transfer_id, subject)
    transfer_output_dir = Path(output_dir) / folder_name
    transfer_output_dir.mkdir(parents=True, exist_ok=True)

    if debug:
        print(f'  Transfer folder: {folder_name}')

    inserted = state.sync_listing(transfer_id, subject, folder_name, file_list)
    for entry in inserted:
        if state.adopt_existing(output_dir, entry):
            print(f'  ✓ Adopted existing file: {entry["rel_path"]}')
    if inserted:
        state.save()

    return download_token, file_list, folder_name


def download_one_file(download_token, file_info, local_path):
    """Download a single file to a .part file, hashing as it streams, then
    atomically move it into place after re-reading and re-hashing from disk.
    Returns the sha256 hex digest on success, or None on failure (the .part
    is always cleaned up)."""
    file_id = file_info['id']
    file_size = file_info['size']
    file_name = file_info['name']

    part_path = local_path.with_name(local_path.name + '.part')
    if part_path.exists():
        part_path.unlink()

    download_params = {'token': download_token, 'files_ids': file_id}
    download_url_base = base_url.replace('/rest.php', '/download.php')

    digest = hashlib.sha256()
    bytes_downloaded = 0

    try:
        start_time = time.time()
        last_update_time = start_time
        last_bytes = 0

        with requests.get(download_url_base, params=download_params, stream=True, verify=not insecure, timeout=600) as r:
            r.raise_for_status()
            with open(part_path, 'wb') as f:
                for chunk in r.iter_content(chunk_size=8192 * 1024):  # 8MB chunks
                    if not chunk:
                        continue
                    f.write(chunk)
                    digest.update(chunk)
                    bytes_downloaded += len(chunk)

                    current_time = time.time()
                    if current_time - last_update_time >= 0.5 or bytes_downloaded >= file_size:
                        elapsed = current_time - start_time
                        if elapsed > 0:
                            recent_bytes = bytes_downloaded - last_bytes
                            recent_time = current_time - last_update_time
                            speed = recent_bytes / recent_time if recent_time > 0 else 0
                            percent = (bytes_downloaded / file_size * 100) if file_size > 0 else 0
                            progress_bar = '=' * int(percent / 2) + '>' + ' ' * (50 - int(percent / 2))
                            print(f'\r      [{progress_bar}] {percent:.1f}% | {format_bytes(bytes_downloaded)}/{format_bytes(file_size)} | {format_speed(speed)}', end='', flush=True)
                            last_update_time = current_time
                            last_bytes = bytes_downloaded
        print()
    except Exception as e:
        print(f'\n    ❌ Failed to download {file_name}: {e}')
        if part_path.exists():
            part_path.unlink()
        return None

    if bytes_downloaded != file_size:
        print(f'    ❌ Size mismatch for {file_name}: got {bytes_downloaded} bytes, expected {file_size}')
        part_path.unlink()
        return None

    streamed_digest = digest.hexdigest()
    disk_digest = sha256_file(part_path)
    if disk_digest != streamed_digest:
        print(f'    ❌ Hash mismatch for {file_name}: bytes written to disk do not match bytes streamed')
        part_path.unlink()
        return None

    os.replace(part_path, local_path)
    return streamed_digest


def download_transfer_directly(transfer_id, transfer_details, output_dir, state, budget,
                                archive_dir=None, recheck_hashes=False):
    """Download every not-yet-verified file in a transfer. Manifest lookups
    decide what's skipped (archived/moved/verified), adopted (pre-existing
    on disk), or downloaded. Deferrals due to an exhausted run budget leave
    the entry as-is for a future run."""
    result = _fetch_and_sync(transfer_id, transfer_details, output_dir, state)
    if result is None:
        return False
    download_token, file_list, folder_name = result

    if archive_dir:
        moved = [state.get(transfer_id, f['id']) for f in file_list]
        moved = [e for e in moved if e and e['status'] == STATUS_MOVED]
        if moved:
            resolved, mismatched = state.resolve_archive(archive_dir, moved)
            if resolved or mismatched:
                state.save()
            for entry in resolved:
                print(f'  ✓ Archived (hash verified): {entry["rel_path"]}')
            for entry in mismatched:
                print(f'  ⚠ Archive hash mismatch, left as moved: {entry["rel_path"]} - {entry["error"]}')

    statuses = [state.get(transfer_id, f['id'])['status'] for f in file_list]
    if not force_download and all(s in (STATUS_ARCHIVED, STATUS_MOVED, STATUS_VERIFIED) for s in statuses):
        print(f'  ✓ Transfer already complete (all {len(file_list)} files accounted for)')
        return True

    for file_idx, file_info in enumerate(file_list, 1):
        entry = state.get(transfer_id, file_info['id'])
        file_name = file_info['name']
        file_size = file_info['size']
        local_path = Path(output_dir) / entry['rel_path']
        local_path.parent.mkdir(parents=True, exist_ok=True)

        status = entry['status']

        if not force_download:
            if status in (STATUS_ARCHIVED, STATUS_MOVED):
                print(f'    [{file_idx}/{len(file_list)}] Skipping: {file_name} (status={status})')
                continue

            if status == STATUS_VERIFIED:
                if recheck_hashes:
                    if local_path.exists() and local_path.stat().st_size == entry['size'] and sha256_file(local_path) == entry['sha256']:
                        print(f'    [{file_idx}/{len(file_list)}] Skipping: {file_name} (re-hash verified)')
                        continue
                    entry.update(status=STATUS_FAILED, error='re-hash mismatch or file missing')
                    state.save()
                elif local_path.exists() and local_path.stat().st_size == entry['size']:
                    print(f'    [{file_idx}/{len(file_list)}] Skipping: {file_name} ({format_bytes(file_size)}) - already verified')
                    continue
                elif not local_path.exists():
                    entry.update(status=STATUS_MOVED)
                    state.save()
                    print(f'    [{file_idx}/{len(file_list)}] {file_name} moved out of output dir - expected at archive')
                    continue
                else:
                    entry.update(status=STATUS_FAILED, error='local file size no longer matches manifest')
                    state.save()

        if not budget.can_start(file_size):
            print(f'    [{file_idx}/{len(file_list)}] Deferred (run budget): {file_name} ({format_bytes(file_size)}) - {budget.stop_reason}')
            continue

        print(f'    [{file_idx}/{len(file_list)}] Downloading: {file_name} ({format_bytes(file_size)})')
        entry.update(status=STATUS_DOWNLOADING, attempts=entry.get('attempts', 0) + 1)
        state.save()

        digest = download_one_file(download_token, file_info, local_path)
        if digest is None:
            entry.update(status=STATUS_FAILED, error='download or verification failed')
            state.save()
            continue

        now = _now_iso()
        entry.update(status=STATUS_VERIFIED, sha256=digest, downloaded_at=now, verified_at=now)
        state.save()
        budget.record(file_size)
        print(f'      ✓ Hash verified')

    return True

def list_guests_command():
    """List all guest vouchers"""
    print('Fetching guests...\n')
    guests = get_guests()

    if not guests:
        print('No guests found.')
        return

    print(f'Found {len(guests)} guest(s):\n')
    for guest in guests:
        print(f'Guest ID: {guest["id"]}')
        print(f'  Email: {guest["email"]}')
        print(f'  Subject: {guest.get("subject", "N/A")}')
        print(f'  Created: {guest.get("created", {}).get("formatted", "N/A")}')
        print(f'  Expires: {guest.get("expires", {}).get("formatted", "N/A")}')
        print(f'  Transfer Count: {guest.get("transfer_count", 0)}')
        print()


def _resolve_state_path(state_file, output_dir):
    return Path(state_file) if state_file else Path(output_dir) / '.filesender_state.json'


def _print_state_summary(state):
    summary = state.summary()
    for status in _ALL_STATUSES:
        bucket = summary.get(status, {'count': 0, 'bytes': 0})
        if bucket['count']:
            print(f'  {status:<11}: {bucket["count"]:>5} file(s), {format_bytes(bucket["bytes"])}')


def status_command(args):
    """Print the manifest report: per-status counts, and entries no longer
    present in the current FileSender listing."""
    state_path = _resolve_state_path(args.state_file, args.output_dir)
    state = DownloadState.load(state_path, base_url=base_url)

    print(f'State file: {state_path}\n')
    _print_state_summary(state)

    print('\nFetching current FileSender listing to check for entries gone from the server...')
    transfers = get_transfers()
    seen_keys = set()
    for transfer in transfers:
        details = get_transfer_details(transfer['id'])
        if not details:
            continue
        for file_info in details.get('files', []):
            seen_keys.add(DownloadState.key(transfer['id'], file_info['id']))

    stale = state.stale_entries(seen_keys)
    if stale:
        print(f'\n{len(stale)} entrie(s) in the manifest no longer appear on FileSender:')
        for entry in stale:
            print(f'  - {entry["rel_path"]} (status={entry["status"]})')
    else:
        print('\nAll manifest entries are still present on FileSender.')


def verify_archive_command(args):
    """Standalone: resolve and hash-verify moved manifest entries against a
    destination directory, report results, and exit. No credentials or
    network access required."""
    state_path = _resolve_state_path(args.state_file, args.output_dir)
    state = DownloadState.load(state_path, base_url=base_url)

    moved = [e for e in state.data['files'].values() if e['status'] == STATUS_MOVED]
    print(f'Verifying {len(moved)} moved file(s) against {args.verify_archive}...\n')
    resolved, mismatched = state.resolve_archive(args.verify_archive, moved)
    state.save()

    for entry in resolved:
        print(f'  ✓ {entry["rel_path"]}')
    if mismatched:
        print(f'\n❌ {len(mismatched)} file(s) failed hash verification:')
        for entry in mismatched:
            print(f'  ✗ {entry["rel_path"]}: {entry["error"]}')
        sys.exit(1)

    unresolved = len(moved) - len(resolved) - len(mismatched)
    if unresolved:
        print(f'\n⚠ {unresolved} moved file(s) had no candidate found under {args.verify_archive}')
    print(f'\n✓ {len(resolved)} file(s) verified against archive.')


def download_guest_transfers(guest_email=None, guest_id=None, output_dir='.', limit=None, dry_run=False,
                              state_file=None, max_files=None, max_bytes=None, archive_dir=None,
                              recheck_hashes=False):
    """Download all transfers from a specific guest"""

    # Get guests and transfers
    print('Fetching guests and transfers...')
    if not force_download:
        print('(Use --force to re-download existing files)\n')
    else:
        print('(Forcing re-download of all files)\n')

    guests = get_guests()
    transfers = get_transfers()

    if not guests:
        print('❌ No guests found.')
        return

    if not transfers:
        print('❌ No transfers found.')
        return

    # Find the guest
    target_guest = None
    if guest_email:
        for guest in guests:
            if guest['email'].lower() == guest_email.lower():
                target_guest = guest
                break
        if not target_guest:
            print(f'❌ Guest with email "{guest_email}" not found.')
            print('\nAvailable guests:')
            for g in guests:
                print(f'  - {g["email"]} (ID: {g["id"]})')
            return
    elif guest_id:
        for guest in guests:
            if guest['id'] == guest_id:
                target_guest = guest
                break
        if not target_guest:
            print(f'❌ Guest with ID {guest_id} not found.')
            return

    print(f'\n✓ Found guest: {target_guest["email"]} (ID: {target_guest["id"]})')
    print(f'  Subject: {target_guest.get("subject", "N/A")}')
    print(f'  Transfer Count: {target_guest.get("transfer_count", 0)}')

    # Filter transfers by guest email
    guest_transfers = [t for t in transfers if t.get('user_email', '').lower() == target_guest['email'].lower()]

    if not guest_transfers:
        print(f'\n❌ No transfers found for guest {target_guest["email"]}')
        return

    print(f'\n✓ Found {len(guest_transfers)} transfer(s) from this guest')

    # Apply limit if specified
    if limit and limit < len(guest_transfers):
        print(f'  ⚠ Limiting to first {limit} transfer(s) for testing')
        guest_transfers = guest_transfers[:limit]

    # Show what will be downloaded
    print(f'\nTransfers to download:')
    total_files = 0
    for i, transfer in enumerate(guest_transfers, 1):
        file_count = len(transfer.get('files', []))
        total_files += file_count
        print(f'{i:3d}. Transfer {transfer["id"]} - "{transfer.get("subject", "No subject")}" ({file_count} file(s))')

    print(f'\nTotal: {len(guest_transfers)} transfer(s), {total_files} file(s)')
    print(f'Output directory: {output_dir}')

    # Create output directory and load the manifest
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    state_path = _resolve_state_path(state_file, output_dir)
    state = DownloadState.load(state_path, base_url=base_url)
    state.reconcile_local(output_dir)

    budget = RunBudget(max_files=max_files, max_bytes=max_bytes)

    if dry_run:
        print('\n🔍 Dry run - syncing manifest only, no files will be downloaded\n')

    successful = 0
    failed = 0

    for i, transfer in enumerate(guest_transfers, 1):
        transfer_id = transfer['id']
        subject = transfer.get('subject', 'No subject')
        file_count = len(transfer.get('files', []))

        verb = 'Checking' if dry_run else 'Downloading'
        print(f'[{i}/{len(guest_transfers)}] {verb} transfer {transfer_id}: "{subject}" ({file_count} file(s))')

        transfer_details = get_transfer_details(transfer_id)
        if not transfer_details:
            print(f'  ❌ Failed to get transfer details')
            failed += 1
            print()
            continue

        if dry_run:
            result = _fetch_and_sync(transfer_id, transfer_details, output_dir, state)
            if result is None:
                failed += 1
            else:
                successful += 1
            print()
            continue

        success = download_transfer_directly(
            transfer_id, transfer_details, output_dir, state, budget,
            archive_dir=archive_dir, recheck_hashes=recheck_hashes,
        )
        if success:
            print(f'  ✓ Transfer processed')
            successful += 1
        else:
            failed += 1
        print()

    state.save()

    print(f'\n{"="*60}')
    if dry_run:
        print('Dry run complete - manifest synced, nothing downloaded.')
    else:
        print('Summary:')
        print(f'  ✓ Successful transfers: {successful}')
        print(f'  ✗ Failed transfers: {failed}')
        if budget.exhausted:
            print(f'  ⏸ Run budget reached: {budget.stop_reason}')
    print()
    _print_state_summary(state)
    print(f'{"="*60}')

def main():
    global base_url, username, apikey, debug, insecure, force_download

    cfg_base_url, cfg_username, cfg_apikey = load_config()
    base_url, username, apikey = cfg_base_url, cfg_username, cfg_apikey

    args = parse_args()

    # Override config with command line arguments
    if args.username:
        username = args.username
    if args.apikey:
        apikey = args.apikey
    if args.base_url:
        base_url = args.base_url

    debug = args.verbose
    insecure = args.insecure
    force_download = args.force

    if args.verify_archive:
        verify_archive_command(args)
        return

    if not all([username, apikey, base_url]):
        print('❌ Error: Missing required configuration (username, apikey, or base_url)')
        print('Please configure ~/.filesender/filesender.py.ini or provide via command line')
        sys.exit(1)

    if args.list_guests:
        list_guests_command()
    elif args.status:
        status_command(args)
    elif args.email or args.guest_id:
        max_bytes = parse_size(args.max_bytes) if args.max_bytes else None
        download_guest_transfers(
            guest_email=args.email,
            guest_id=args.guest_id,
            output_dir=args.output_dir,
            limit=args.limit,
            dry_run=args.dry_run,
            state_file=args.state_file,
            max_files=args.max_files,
            max_bytes=max_bytes,
            archive_dir=args.archive_dir,
            recheck_hashes=args.recheck_hashes,
        )
    else:
        build_arg_parser().print_help()
        print('\n❌ Error: Please specify --list-guests, -e EMAIL, --guest-id ID, --status, or --verify-archive')
        sys.exit(1)

if __name__ == '__main__':
    main()
