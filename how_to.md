# How to use surf-transfer

`surf-transfer` (also `python3 download_guest_transfers.py`, same flags) downloads from FileSender
guest transfers and SurfDrive shares into one output folder and validates every slide. See
`README.md` for the design; this page is the task-oriented version.

Remote sources are **read-only**: the tool only ever reads (WebDAV `PROPFIND`/`GET`/`HEAD`,
FileSender listing endpoints and `download.php`). It never deletes, moves, closes or modifies
anything remotely, even though your credentials could.

## 1. Set up credentials (never in the repo)

**FileSender** — `.env` in the working directory (or real environment variables):

```
FILESENDER_USERNAME=you@institute.nl
API_SECRET=…
```

or the existing `~/.filesender/filesender.py.ini`:

```ini
[system]
base_url = https://filesender.surf.nl/rest.php

[user]
username = your_username
apikey = your_api_key

[filesender]
centre = Centre A        ; optional provenance label
```

If the server answers `auth_remote_signature_check_failed`, the username and API secret do not match.
Check FileSender → profile → API access (regenerating the secret invalidates the old one).

**SurfDrive** — `~/.surfdrive/surfdrive.ini` (or `./.surfdrive/…`, `./surfdrive.ini`):

```ini
[surfdrive-public]            ; https://surfdrive.surf.nl/s/<token>
token = AbCdEf12345
password =                    ; only if the share link has a password
centre = Centre A

[surfdrive-user]              ; a folder shared with your own account
username = you@institute.nl
app_password = …              ; SurfDrive → Settings → Security → app passwords
folder = Incoming/Centre B
centre = Centre B

[surfdrive-public:second]     ; extra shares: ":<name>" on either section type
token = …
```

For a one-off public link no config is needed: `--surfdrive-link URL`
(share password, if any, in `SURFDRIVE_SHARE_PASSWORD`).

## 2. Download

```bash
# FileSender
python3 download_guest_transfers.py --list-guests
python3 download_guest_transfers.py -e guest@example.com -o /share/incoming
python3 download_guest_transfers.py --guest-id 12345 -o /share/incoming
python3 download_guest_transfers.py --transfer-token 'https://filesender.surf.nl/?s=download&token=…' -o /share/incoming

# SurfDrive
python3 download_guest_transfers.py --surfdrive-link https://surfdrive.surf.nl/s/AbCdEf --centre "Centre A" -o /share/incoming
python3 download_guest_transfers.py --surfdrive public -o /share/incoming     # section from surfdrive.ini
python3 download_guest_transfers.py --surfdrive all -o /share/incoming        # every section

# Both in one run, one manifest
python3 download_guest_transfers.py -e guest@example.com --surfdrive user -o /share/incoming

# Look first, change nothing locally except the manifest
python3 download_guest_transfers.py -e guest@example.com -o /share/incoming --dry-run

# Bounded, resumable batches
python3 download_guest_transfers.py -e guest@example.com -o /share/incoming --max-files 50 --max-bytes 200G
```

Files land in `<output>/<transfer subject | share name>/<path>`. Transfers that share a subject get
`_<transfer_id>` appended so their files never merge. If a FileSender listing carries sub-paths
(`X/Data0000.dat`) they are kept; unsafe paths (`../`, absolute) and two files with the same path in
one folder are refused and shown as failed.

Downloads are sequential. Each slide is queued for validation as soon as **all** its member files
are downloaded, so validating one slide overlaps downloading the next.

### Options

| Flag | Meaning |
|---|---|
| `-o DIR` | output directory |
| `--state-file PATH` | manifest (default `<output>/.filesender_state.json`) |
| `--max-files N`, `--max-bytes SIZE` | stop starting new files (K, M, G, T suffixes) |
| `--archive-dir DIR` | resolve files moved out of the output dir against DIR |
| `--verify-archive DIR` | standalone: hash-verify moved files against DIR, no credentials |
| `--dry-run` | list and sync the manifest only |
| `--force` | re-download even verified/moved/archived files |
| `--recheck-hashes` | re-hash local files instead of trusting the manifest |
| `--min-free SIZE` | refuse files that would leave less than this free (default 1G) |
| `--accept-flagged` | download files flagged as new-after-complete or changed-on-source |
| `--keep-zips` | keep each zip after it is unpacked (default: delete it once its slides have a verdict) |
| `--allow-source-change` | let this run use a source the manifest was not created for (default: refuse; use a fresh `-o`) |
| `--validate-workers N` | parallel validation subprocesses (default 2) |
| `--sample-tiles N` | random level-0 tiles per slide (default 64) |
| `--deep-check` | decode every tile at every level (slow; suspicious slides only) |
| `--validate-timeout S` | per-slide timeout (900 s; 21600 s with `--deep-check`) |
| `--seed N` | fix the tile-sampling seed to reproduce a failure |
| `--revalidate` | validate files already on disk, no downloads |
| `--slide NAME` | with `--revalidate`: only slides whose name contains NAME (also re-checks verified ones) |
| `--status [--json] [--check-remote]` | report and exit |
| `--report out.csv` | one row per slide, then exit |
| `--mark-complete SOURCE --note "…"` | declare a source complete |
| `--centre LABEL` | provenance label for an ad-hoc source |
| `-v` | verbose |

`-u`, `-a`, `-b` override the FileSender username, API key and base URL; `--insecure` skips TLS
verification.

## 3. Lifecycle

```
queued → downloading → downloaded ─→ validating ─┬─→ verified → moved → archived
                    └─→ failed (retried)          ├─→ corrupt
                                                  └─→ unvalidated (no validator for this format)
```

- **queued / downloading / failed** — as before. A failed download is retried next run.
- **downloaded** — transport checks passed: size, source checksum (SurfDrive `oc:checksums`, when
  present), and a sha256 computed while streaming and re-read from disk. Written to `<file>.part`
  and `os.replace`d into place only after all of that agrees.
- **verified** — for a slide: the slide checks passed. For a non-slide file (`.txt`, `.csv`, …):
  the file checks alone. Every `verified` entry records how in `checks`.
- **corrupt** — a slide check failed. The manifest and `--report` name the check and the files.
- **unvalidated** — passed the file checks, but no validator handles the format (flat TIFF,
  iSyntax, CZI, DICOM, other archives). Not proven readable; listed separately by `--status`.
- **moved** — was `verified`, but the file is no longer in the output dir. Never re-downloaded.
- **archived** — a `moved` file found at `--archive-dir` with a matching sha256.

**Only `verified` slides are safe to move out of the capacity-limited output share.** Moving stays
manual, and the tool never writes to, moves into or deletes from an archive location.

```bash
mv "/share/incoming/some transfer" /mnt/archive/
python3 download_guest_transfers.py -o /share/incoming --archive-dir /mnt/archive --status
```

## 4. Reading the results

```bash
python3 download_guest_transfers.py -o /share/incoming --status
python3 download_guest_transfers.py -o /share/incoming --status --json
python3 download_guest_transfers.py -o /share/incoming --report slides.csv
```

`--status` shows slides by status, **how many verified slides (and bytes) are safe to move**, then
`CORRUPT` slides with the failed check and files, `UNVALIDATED` slides with the reason, failed and
flagged files, and each source as *complete (N slides, all verified)* or *open*.

A corrupt entry looks like this, and is enough to write the re-send request:

```
✗ AAX218-T03-00710-AII: [missing_dat] Slidedat.ini vs files that arrived - missing: Data0003.dat
    files: T/AAX218-T03-00710-AII/Data0003.dat
```

Failure checks: `missing_dat`, `unexpected_dat`, `slidedat` (missing or unreadable `Slidedat.ini`),
`missing_index` (no `.mrxs`), `header` (openslide cannot open it), `lowres_read`, `tile_read`
(level-0 `x, y` given), `deep_tile_read`, `timeout`, `crash` (the worker died, e.g. SIGSEGV; the last
position read is recorded), `zip`.

To investigate a suspicious slide more deeply, or to reproduce a failure:

```bash
python3 download_guest_transfers.py -o /share/incoming --revalidate --slide AAX218 --deep-check
python3 download_guest_transfers.py -o /share/incoming --revalidate --slide AAX218 --seed 1234 --sample-tiles 64
```

`--revalidate` needs no credentials. Slides whose files were moved away are skipped (they are
not on disk). Legacy files from a version-1 manifest keep their status; `--revalidate` adds the slide
check to those still present.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | every slide verified |
| 1 | usage or config error |
| 2 | failed downloads, listing errors, flagged or unfinished files |
| 3 | one or more slides (or zips) are corrupt |
| 4 | stopped by `--max-files` / `--max-bytes` |
| 5 | only unvalidated slides remain |

If several apply: 3, then 2, then 4, then 5. A log file `surf_transfer.log` is written next to the
manifest.

## 5. MRXS, zips and other formats

- **MRXS** is `X.mrxs` plus the folder `X/` with `Slidedat.ini` and `Data*.dat`. Before openslide
  runs, `Slidedat.ini` (`[DATAFILE]`, `FILE_COUNT`, `FILE_0…FILE_n`) is compared with the files that
  arrived, plus the `INDEXFILE` from `[HIERARCHICAL]` (normally `Index.dat`, which `[DATAFILE]` does
  not list). A `.dat` the sender never uploaded is reported by name. A folder with no `.mrxs`, or an
  `.mrxs` with no folder, is reported too.
- **Single-file formats** (`.svs`, `.tif`, `.ndpi`, …) are slides of one member. Whether a file is a
  slide is decided by `OpenSlide.detect_format()`. A pyramidal tiled TIFF is validated; a plain flat
  TIFF is `unvalidated`, not `corrupt`.
- **Zips** are downloaded, then CRC-checked while being unpacked **into the zip's own folder**, so a
  loose `X.mrxs` and a zipped `X/` data folder end up side by side and are validated as one slide.
  Nothing is overwritten: identical existing files are reused, a differing one stops the unpack and
  both paths are reported. Once every slide from the zip has a verdict (even `corrupt`), the tool
  **deletes the local zip** and records it as `extracted` (done, never re-downloaded, not "moved"). A zip
  that failed to extract is kept as evidence, and `--keep-zips` keeps all of them. A damaged zip is `corrupt` (`zip`). `.7z`, `.rar`, `.tar*` are `unvalidated` ("extract it
  manually"); extract them into the output folder yourself and the next run picks the files up.
- Anything not clearly a slide (`.txt`, `.csv`, `.pdf`, …) gets the file checks only.

## 6. Completeness, flags and re-sends

```bash
# the centre told you "that is everything"
python3 download_guest_transfers.py -o /share/incoming --mark-complete "Centre A" --note "mail from A, 2026-10-01"
```

`SOURCE` is a source id (`surfdrive:<token>`, `filesender:guest:<id>`) or a centre label. After
that, a file that appears on the source — or changes there after it was downloaded — shows up as
**flagged** and is not downloaded. Review it, then run with `--accept-flagged`.

When a centre re-sends a corrupt slide it arrives under a new file id; the tool links it to the
earlier record by slide name, so `--report` shows the history ("corrupt (tile_read) … -> replaced",
then "verified …"). `--status` and the exit code count only the latest delivery.

## 7. Upgrading from the version-1 manifest

Nothing to do. The first run loads the old manifest, converts it to version 2 (keys become
`filesender:<transfer_id>:<file_id>`), writes a one-time backup `.filesender_state.json.v1.bak`, and
saves the new file. Entries that were `verified`, `moved` or `archived` keep their status, get
`checks: {"legacy": true}`, and are **never re-downloaded**. Run `--revalidate` to give those still on
disk their slide check.

## 8. Single transfer download with the stock client

`filesender.py` is the unmodified FileSender command-line client. It can also upload and delete, and
it does not validate slides, so prefer `--transfer-token` above for slide data:

```bash
python3 filesender.py -d "https://filesender.example.com/download.php?token=xxxxx"
python3 filesender.py -d "https://filesender.example.com/download.php?token=xxxxx" -o ~/Downloads -p
```
