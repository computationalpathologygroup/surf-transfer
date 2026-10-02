# surf-transfer

Downloads slides from **FileSender** guest transfers and **SurfDrive** (WebDAV) shares into one
folder, tracks every file in a single JSON manifest, and shows that every slide is complete and
readable. Anything corrupt or missing can be traced to a file and a check.

```
FileSender ─┐                                   ┌─ verified     (safe to move)
            ├─> download ─> file checks ─> slide check ─┼─ corrupt      (ask the centre to re-send)
SurfDrive  ─┘   .part + sha256            (subprocess)  └─ unvalidated  (no validator for this format)
```

## Remote sources are read-only

The credentials can edit and delete on SurfDrive (and possibly FileSender). This tool never does:

- **WebDAV:** only `PROPFIND`, `GET`, `HEAD`. Never `PUT`, `DELETE`, `MOVE`, `COPY`, `MKCOL`,
  `PROPPATCH`, `LOCK`, `UNLOCK`, even if a share link grants edit rights.
- **FileSender:** only `GET` on the listing endpoints (`/guest`, `/transfer`, `/transfer/<id>`,
  `/transfer/fileidsextended`) and `download.php`. It never closes, extends, deletes or modifies a
  transfer or guest voucher. FileSender itself records a download in the transfer's audit log
  when `download.php` is read; that is on the server side and cannot be avoided.
- Enforced in code: all HTTP goes through `surf_transfer.http.ReadOnlyClient`, which holds the
  allowlist and raises before sending anything else. Tests check that every other method raises and
  that no other module imports an HTTP library.
- There is no cleanup on the server, no "mark as fetched", and no write-back feature.

(`filesender.py`, the stock FileSender client in this repo, can upload and delete. It is not used by
the downloader.)

## Install

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'        # openslide-python + openslide-bin ship the native library
surf-transfer --help           # or: python3 download_guest_transfers.py --help
```

## Quick start

```bash
# FileSender: all transfers of a guest voucher, or a single transfer by token or link
surf-transfer -e guest@example.com -o /share/incoming
surf-transfer --transfer-token 'https://filesender.surf.nl/?s=download&token=…' -o /share/incoming

# SurfDrive: a public share link, or a section from surfdrive.ini
surf-transfer --surfdrive-link https://surfdrive.surf.nl/s/AbCdEf --centre "Centre A" -o /share/incoming
surf-transfer --surfdrive user -o /share/incoming

# Where do things stand? What may be moved off the capacity-limited share?
surf-transfer -o /share/incoming --status
surf-transfer -o /share/incoming --report slides.csv
```

Both backends write into the same manifest (`<output-dir>/.filesender_state.json`). The default
name is unchanged so version-1 manifests migrate in place.

## Configuration

Credentials live outside the repo (`.env`, `filesender.py.ini` and `surfdrive.ini` are git-ignored).

**FileSender** — environment variables or a `.env` in the working directory, or the usual
`~/.filesender/filesender.py.ini` / `./.filesender/…` / `./filesender.py.ini`:

```
FILESENDER_USERNAME=you@institute.nl
API_SECRET=…                      # FileSender profile → API access
FILESENDER_BASE_URL=https://filesender.surf.nl/rest.php     # optional, this is the default
```

**SurfDrive** — `surfdrive.ini`, found in `~/.surfdrive/`, `./.surfdrive/` or `./`:

```ini
[surfdrive-public]                  # a public share link: https://surfdrive.surf.nl/s/<token>
token = AbCdEf12345                 # used as the WebDAV username
password =                          # share password, if the link has one
centre = Centre A                   # free-text provenance label (optional)

[surfdrive-user]                    # a folder shared to your own account
username = you@institute.nl
app_password = …                    # SurfDrive → Settings → Security → create app password
folder = Incoming/Centre B
centre = Centre B

[surfdrive-public:second]           # further shares: add ":<name>" to either section type
token = …
```

Endpoints (verified against the live service): public shares use
`https://surfdrive.surf.nl/public.php/webdav/` (note: no `/files/` prefix), user mode uses
`https://surfdrive.surf.nl/remote.php/dav/files/<user>/`. Plain `http://` is refused, and the client
will not follow a redirect to another scheme or host.

## Statuses

```
queued → downloading → downloaded ─→ validating ─┬─→ verified → moved → archived
                    └─→ failed (retried)          ├─→ corrupt
                                                  └─→ unvalidated
```

- **File level.** `downloaded` means the transport checks passed (size, source checksum when
  offered, streamed and re-read sha256). `moved` and `archived` behave exactly as before.
- **Slide level.** `verified`, `corrupt` or `unvalidated`, with the reason recorded.
- **Only `verified` slides are safe to move.** `--status` leads with that number.

## What is checked

| Check | FileSender | SurfDrive |
|---|---|---|
| size equals the listing | yes | yes |
| source checksum | none offered | `oc:checksums` (SHA1 / MD5 / ADLER32) when present, otherwise recorded as `absent` |
| local sha256, streamed and re-read from disk | yes | yes |
| slide completeness and readability | yes | yes |

The manifest records which checks each file passed, for example
`checks: {"size": true, "source_checksum": "sha1:match", "local_sha256": "…"}`. A file is never
`verified` without saying how. An absent source checksum is recorded, not treated as a failure.
Files from a version-1 manifest carry `checks: {"legacy": true}`.

### Slide validation

A slide is the unit that is validated, not the file. An MRXS slide is `X.mrxs` plus the folder `X/`.

1. **MRXS completeness.** `X/Slidedat.ini` is parsed (`[DATAFILE]` `FILE_COUNT`, `FILE_0…FILE_n`,
   plus `[HIERARCHICAL]` `INDEXFILE`, normally `Index.dat`) and compared with the files that arrived.
   Missing and unexpected `.dat` files are named. This is the only defence against a file the sender
   never uploaded. (`Index.dat` is not listed under `[DATAFILE]`; it is found by `INDEXFILE`.)
2. **openslide** (`openslide-python` + `openslide-bin`): open and read the metadata (dimensions,
   levels, MPP), read the **lowest-resolution level in full**, then read a **random sample of
   level-0 tiles** (default 64, `--sample-tiles`). The RNG seed and the sampled coordinates are stored
   in the manifest; `--seed` replays them.
3. `--deep-check` decodes every tile at every level, for slides that already look suspicious.

Whether a file is a slide is decided by `OpenSlide.detect_format()`, not by extension. A format that
no validator handles (flat TIFF, iSyntax, CZI, DICOM, …) ends as **`unvalidated`**: it passed the
file checks but nothing proves it is readable. Two refinements, both recorded with the reason:
a file with an openslide-only extension (`.svs`, `.ndpi`, …) that openslide cannot recognise is
`corrupt`, and a `.tif`/`.tiff` that fails a built-in TIFF structure check (truncated, broken
directory) is `corrupt` rather than `unvalidated`.

Each validation runs in **its own subprocess** with a timeout, so a segfault in openslide never
takes down the downloader or a manifest write. A crash or timeout is recorded as `corrupt` with the
last position read. Only the main process writes the manifest.

`corrupt` records which check failed (`missing_dat`, `unexpected_dat`, `slidedat`, `missing_index`,
`header`, `lowres_read`, `tile_read` with level-0 `(x, y)`, `deep_tile_read`, `timeout`, `crash`,
`zip`) and which member files are involved, so a re-send request can be written straight from it.
An internal validator error (a bug, not evidence about the file) ends as `unvalidated`.

### Zips

A `.zip` is downloaded and verified like any file, then unpacked **into its own folder**, beside the
loose files. That matters because senders may zip only part of a slide: the live test share has a
loose `X.mrxs` next to a zip holding only the `X/` data folder (`Index.dat`, `Data*.dat`,
`Slidedat.ini`). Unpacked next to each other they form one complete slide and openslide finds the data
folder beside the index file.

- Every member's CRC, size and a re-read sha256 are checked; paths that escape the folder are
  rejected; free space is checked first.
- **Nothing is overwritten.** A member identical to a file already on disk is reused; one that would
  overwrite a file with different content stops the unpack (`zip` check, both paths listed).
- Ordering: an MRXS slide is not validated while a zip in its folder is still pending, and a zip is
  unpacked only after the loose files in its folder are downloaded, so a slide is never judged
  "missing files" before its zip has been opened.
- Extracted files are tracked in the manifest (`origin: extracted`, `parent: <zip key>`) and their
  slides are validated like any other. A damaged zip is `corrupt` (check `zip`). Password-protected zips
  are reported, not guessed. Other archive formats (`.7z`, `.rar`, `.tar*`) end as `unvalidated` with
  "extract it manually".
- **The zip is deleted by default** once it is safely out of the way: every member was CRC-verified, no
  `zip_conflict` / `zip_paths` / `zip_member_size` / `zip_member_hash` failure occurred, and every slide
  fed by it has a final verdict (`verified`, `corrupt` or `unvalidated`; a corrupt slide's bytes equal the
  zip's, so the zip adds no evidence). A zip whose extraction failed (damaged, encrypted, conflicting,
  unsafe paths) is kept as the evidence. Only the local copy is deleted; sources stay read-only.
  The manifest keeps the zip as `extracted` (size, sha256 and `removed_at` stay), which counts as done in
  `--status` and is never downloaded again, nor mistaken for a `moved` file. Pass `--keep-zips` to keep
  every zip (it then stays `verified` beside its slide and uses twice the space until you move it).

### One manifest, one source

The manifest records the sources it was created for: the FileSender instance (plus guest, if named) or
the SurfDrive share token **and its `?dir=` folder**. A run that uses a source not on that list stops
before listing anything and names both; use a fresh `-o` for the new source, or pass
`--allow-source-change` if mixing is intended. A manifest without a record adopts the current source on its
first run. With `?dir=`, stored paths are relative to that folder.

## Provenance, completeness and re-sends

- Every slide records its **centre** (a label from the source config or `--centre`), **source id**,
  first-seen and received timestamps, and the checks passed.
- **Completeness is declared, not inferred.** When a centre says "this is all":
  `surf-transfer -o DIR --mark-complete "Centre A" --note "email from A, 2026-10-01"`.
  `--status` then reports each source as *complete (N slides, all verified)* or still *open*. A file
  that appears on a source after it was marked complete — or that changes on the source after it was
  downloaded — is **flagged**, not downloaded, until you review it and run with `--accept-flagged`.
- **Re-sends.** A re-sent slide arrives under a new file id. It is linked to the earlier record by
  slide name within the source, so the history reads "corrupt on date A, replaced and verified on
  date B". Superseded records stay in the manifest and in `--report`, and only the latest delivery
  counts for `--status` and the exit code.

## Unattended runs

- **Exit codes:** `0` all verified · `1` usage/config error · `2` failed, incomplete or flagged ·
  `3` corrupt slides · `4` stopped by `--max-files`/`--max-bytes` · `5` unvalidated slides only.
  When several apply the order is 3, 2, 4, 5.
- **Log file:** `surf_transfer.log` next to the manifest.
- **`--status --json`** for machine-readable status. **`--report out.csv`** writes one row per slide
  (source, centre, status, checks, failure reason and files, member files, history).
- **Free-space preflight:** a file that will not fit on the output volume (keeping `--min-free`,
  default 1G) is refused with a message instead of dying mid-write.

## Development

```bash
pytest                                   # no network, no real slides
ruff check src tests && ruff format src tests
mypy                                     # non-strict
```

Layout: `src/surf_transfer/` — pure core (`models`, `parsing`, `manifest`, `report`, `tiffcheck`),
edges (`http`, `sources`, `downloader`, `validation`), and the shell (`pipeline`, `app`, `cli`).
`Source` is a `typing.Protocol`; `Pipeline` takes a source and a validator, so tests pass a
`FakeSource` and a fake runner. `download_state.py` is the legacy v1 manifest, kept (with its
tests) for reference; the downloader itself uses `surf_transfer.manifest.Manifest`.

Adding a format means adding one `FormatValidator` to `validation.VALIDATORS`; the pipeline does not
change. DICOM support is deferred until a centre sends DICOM (see the GitHub issue).
