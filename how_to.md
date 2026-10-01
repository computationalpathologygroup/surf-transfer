# FileSender Scripts

## Downloading from Guest Uploads

Use `download_guest_transfers.py` to download all transfers from a specific guest voucher.

**Note:** Each transfer is downloaded into its own subfolder named after the transfer subject (or `transfer_<id>` if no subject). If two transfers share a subject, the newer one's folder gets `_<transfer_id>` appended so their files never merge.

### List All Guests

```bash
python3 download_guest_transfers.py --list-guests
```

### Download All Transfers from a Guest

By email:
```bash
python3 download_guest_transfers.py -e guest@example.com -o ~/Downloads
```

By guest ID:
```bash
python3 download_guest_transfers.py --guest-id 12345 -o ~/Downloads
```

### Testing with Limited Transfers

Download only the first N transfers (useful for testing):
```bash
python3 download_guest_transfers.py -e guest@example.com -o ~/Downloads --limit 5
```

### Dry Run

See what would be downloaded without actually downloading:
```bash
python3 download_guest_transfers.py -e guest@example.com --dry-run
```

### Additional Options

- `-v, --verbose`: Show detailed output including API calls
- `--insecure`: Skip SSL certificate verification
- `--force`: Force re-download even if files are already verified/moved/archived
- `-u, --username`: Override username from config file
- `-a, --apikey`: Override API key from config file
- `-b, --base-url`: Override base URL from config file
- `--state-file PATH`: Path to the state manifest (default: `<output-dir>/.filesender_state.json`)
- `--max-files N`: Stop downloading new files once N have completed in this run
- `--max-bytes SIZE`: Stop downloading new files once this many bytes have completed (accepts `500M`, `200G`, `1.5T`, or a plain byte count)
- `--archive-dir DIR`: Resolve files that moved out of the output dir against `DIR` before downloading
- `--verify-archive DIR`: Standalone — hash-verify moved files against `DIR` and exit; no downloads, no credentials needed
- `--status`: Print the manifest report and exit
- `--recheck-hashes`: Re-hash local verified files from disk instead of trusting the manifest

### Resume Functionality

Every file the script has ever seen is tracked in a JSON manifest (`.filesender_state.json`
in the output directory by default, plus a human-readable `.filesender_state.txt`
alongside it). Each file goes through this lifecycle:

```
queued → downloading → verified ─┬─→ moved  ─→ archived   (via --archive-dir)
                                  └─→ failed (retried next run)
```

- **queued** — seen on FileSender, not yet on disk.
- **verified** — downloaded (or adopted from a pre-existing file) and hash-checked;
  the sha256 is computed while streaming and re-read from disk afterwards, so a bad
  write, a full disk, or corruption during a manual move is caught, not just a byte count.
- **moved** — was verified, but the file is no longer in the output directory. This is
  never re-downloaded. It's the state that matters most: because the share has a
  hard capacity limit, files get moved out by hand to a second location once a batch
  is done, and the manifest keys files by `<transfer_id>:<file_id>` (not folder path),
  so a move never gets mistaken for "still needs downloading."
- **archived** — a `moved` file has been located at a destination directory and its
  sha256 matches. This is the only state that constitutes proof the file made it
  to where it needed to go.
- **failed** — download or verification failed; retried automatically next run.

Downloads are written to `<file>.part` and only `os.replace()`d into their final name
after the streamed hash and a from-disk re-read agree — a network failure mid-file
never leaves a truncated file that looks complete.

The script never writes to, moves into, or deletes from an archive location — moving
files out of the output directory is always a manual step. Run with `--archive-dir`
afterwards (or standalone with `--verify-archive`) to have the manifest catch up.

Example — a run interrupted, resumed, then files moved out by hand:
```bash
# First run (interrupted, or stopped early by --max-files/--max-bytes)
python3 download_guest_transfers.py -e guest@example.com -o /share/incoming --max-files 50

# Resume - already-verified files are skipped, failed ones are retried
python3 download_guest_transfers.py -e guest@example.com -o /share/incoming --max-files 50

# Move a batch out of the capacity-limited share by hand
mv "/share/incoming/some transfer" /mnt/archive/

# Next run: moved files are resolved (hash-verified) against the archive and
# marked archived instead of being re-downloaded; remaining files continue
python3 download_guest_transfers.py -e guest@example.com -o /share/incoming --archive-dir /mnt/archive

# Check status without downloading anything
python3 download_guest_transfers.py -e guest@example.com -o /share/incoming --status

# Verify a move happened correctly, independent of a download run
python3 download_guest_transfers.py -o /share/incoming --verify-archive /mnt/archive

# Force complete re-download if needed
python3 download_guest_transfers.py -e guest@example.com -o /share/incoming --force
```

### Configuration

The script uses the same configuration file as `filesender.py`:
- `~/.filesender/filesender.py.ini`  
- `./.filesender/filesender.py.ini` (local directory)
- `./filesender.py.ini` (current directory)

Example config file:
```ini
[system]
base_url = https://filesender.surf.nl/rest.php
default_transfer_days_valid = 14

[user]
username = your_username
apikey = your_api_key
```

## Single Transfer Download

Use `filesender.py` for downloading a single transfer with a download link:

```bash
python3 filesender.py -d "https://filesender.example.com/download.php?token=xxxxx"
```

With options:
```bash
python3 filesender.py \
  -d "https://filesender.example.com/download.php?token=xxxxx" \
  -o ~/Downloads \
  -p  # Show progress
```