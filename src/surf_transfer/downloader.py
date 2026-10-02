"""Downloader: .part file, streamed hashing, from-disk re-read, os.replace."""

from __future__ import annotations

import hashlib
import os
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .models import RemoteFile
from .parsing import compare_checksums, pick_checksum_algo
from .sources import Source
from .util import sha256_file


class _Adler32:
    def __init__(self) -> None:
        self._value = 1

    def update(self, data: bytes) -> None:
        self._value = zlib.adler32(data, self._value)

    def hexdigest(self) -> str:
        return f"{self._value & 0xFFFFFFFF:08x}"


def _new_hasher(algo: str) -> Any:
    if algo == "adler32":
        return _Adler32()
    return hashlib.new(algo)


@dataclass
class DownloadOutcome:
    ok: bool
    sha256: str | None = None
    checks: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


class Downloader:
    """Fetches one file into place. The final name only ever appears after the
    size, the source checksum (when offered) and the streamed-vs-re-read sha256
    all agree, so a truncated or damaged transfer never looks complete."""

    def __init__(self, progress: Callable[[int, int], None] | None = None):
        self._progress = progress

    def fetch(self, source: Source, remote: RemoteFile, local_path: Path) -> DownloadOutcome:
        local_path.parent.mkdir(parents=True, exist_ok=True)
        part = local_path.with_name(local_path.name + ".part")
        part.unlink(missing_ok=True)

        sha256 = hashlib.sha256()
        algo = pick_checksum_algo(remote.checksums)
        source_hasher = _new_hasher(algo) if algo else None
        written = 0
        try:
            with source.open_stream(remote) as chunks, open(part, "wb") as out:
                for chunk in chunks:
                    if not chunk:
                        continue
                    out.write(chunk)
                    sha256.update(chunk)
                    if source_hasher is not None:
                        source_hasher.update(chunk)
                    written += len(chunk)
                    if self._progress:
                        self._progress(written, remote.size)
        except Exception as e:  # noqa: BLE001 - any transport failure is a failed attempt
            return self._fail(part, f"download failed: {e}")

        if written != remote.size:
            return self._fail(
                part, f"size mismatch: got {written} bytes, listing says {remote.size}"
            )

        computed = {algo: source_hasher.hexdigest()} if algo and source_hasher else {}
        source_check = compare_checksums(remote.checksums, computed)
        if source_check.endswith(":mismatch"):
            return self._fail(part, f"source checksum mismatch ({source_check})")

        streamed = sha256.hexdigest()
        if sha256_file(part) != streamed:
            return self._fail(part, "bytes on disk do not match bytes streamed (sha256)")

        os.replace(part, local_path)
        return DownloadOutcome(
            ok=True,
            sha256=streamed,
            checks={"size": True, "source_checksum": source_check, "local_sha256": streamed},
        )

    @staticmethod
    def _fail(part: Path, error: str) -> DownloadOutcome:
        part.unlink(missing_ok=True)
        return DownloadOutcome(ok=False, error=error)
