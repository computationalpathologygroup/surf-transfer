"""Test doubles: a fake requests.Session and an in-memory Source."""

from __future__ import annotations

import hashlib
import threading
import time
import zlib
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from surf_transfer.models import (
    VERIFIED,
    CheckResult,
    Failure,
    JobResult,
    RemoteFile,
)


class FakeResponse:
    def __init__(self, status_code=200, text="", json_data=None, content=b"", headers=None):
        self.status_code = status_code
        self.text = text
        self._json = json_data
        self._content = content
        self.headers = headers or {}
        self.closed = False

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size=1024):
        for i in range(0, len(self._content), chunk_size):
            yield self._content[i : i + chunk_size]

    def close(self):
        self.closed = True


class FakeSession:
    """Records every request; answers from a handler(method, url, kwargs)."""

    def __init__(self, handler=None):
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.handler = handler or (lambda m, u, kw: FakeResponse(200))

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.handler(method, url, kwargs)


class FakeSource:
    """In-memory Source: files are bytes; no network."""

    def __init__(
        self,
        files: dict[str, bytes],
        source_id="fake:s",
        centre="Fake Centre",
        checksums: dict[str, dict[str, str]] | None = None,
        group="G1",
        keys: dict[str, str] | None = None,
        label: str | None = None,
    ):
        self.keys = keys or {}
        self.label = label or f"Folder {group}"
        self.source_id = source_id
        self.centre = centre
        self.files = files
        self.checksums = checksums or {}
        self.group = group
        self.opened: list[str] = []
        self.list_error: Exception | None = None
        self.errors: list[str] = []
        self.corrupt_in_flight: set[str] = set()  # names whose stream is truncated

    def list(self) -> list[RemoteFile]:
        if self.list_error:
            raise self.list_error
        return [
            RemoteFile(
                key=self.keys.get(name, f"fake:{self.group}:{i}"),
                source_id=self.source_id,
                group_id=self.group,
                group_label=self.label,
                name=name,
                size=len(data),
                checksums=self.checksums.get(name, {}),
            )
            for i, (name, data) in enumerate(sorted(self.files.items()))
        ]

    @contextmanager
    def _stream(self, remote: RemoteFile) -> Iterator[Iterator[bytes]]:
        self.opened.append(remote.name)
        data = self.files[remote.name]
        if remote.name in self.corrupt_in_flight:
            data = data[: len(data) // 2]
        yield iter([data[i : i + 7] for i in range(0, len(data), 7)] or [b""])

    def open_stream(self, remote: RemoteFile):
        return self._stream(remote)


def sha1(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def adler32(data: bytes) -> str:
    return f"{zlib.adler32(data) & 0xFFFFFFFF:08x}"


class FakeRunner:
    def __init__(self, verdicts=None, delay=0.0, gate: threading.Event | None = None):
        self.jobs = []
        self.threads = set()
        self.verdicts = verdicts or {}
        self.delay, self.gate = delay, gate
        self.active = self.peak = 0
        self._lock = threading.Lock()

    def run(self, job):
        with self._lock:
            self.jobs.append(job)
            self.active += 1
            self.peak = max(self.peak, self.active)
        self.threads.add(threading.current_thread().name)
        if self.gate:
            self.gate.wait(10)
        time.sleep(self.delay)
        with self._lock:
            self.active -= 1
        status = self.verdicts.get(job.name, VERIFIED)
        failure = (
            None
            if status == VERIFIED
            else Failure(
                "tile_read",
                "tile at level-0 (1, 2) failed",
                members=tuple(r for r, _ in job.members),
                coord=(1, 2),
            )
        )
        return JobResult(
            slide_key=job.slide_key,
            kind=job.kind,
            status=status,
            checks=[CheckResult("stub", status == VERIFIED)],
            failure=failure,
        )
