"""Backends: where files come from. All HTTP goes through ReadOnlyClient."""

from __future__ import annotations

import hashlib
import hmac
import re
import time
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from typing import Any, Protocol
from urllib.parse import quote, urlsplit

from .config import FileSenderConfig, SurfDriveConfig
from .http import ReadOnlyClient
from .models import RemoteFile
from .parsing import DavEntry, parse_propfind

CHUNK = 8 * 1024 * 1024

RemoteFiles = list[RemoteFile]
GuestRows = list[dict[str, Any]]  # alias: methods named `list` shadow the builtin in class bodies


class SourceError(Exception):
    """A source could not be listed or read."""


class Source(Protocol):
    source_id: str
    centre: str | None

    def list(self) -> RemoteFiles: ...

    def open_stream(self, remote: RemoteFile) -> AbstractContextManager[Iterator[bytes]]: ...


# --- FileSender -------------------------------------------------------------


def filesender_allowed_paths(base_url: str) -> list[str]:
    """The read endpoints of the FileSender REST API (and download.php). Nothing
    else - no close/extend/delete of transfers, no guest changes - can be reached."""
    rest = re.escape(urlsplit(base_url).path.rstrip("/"))
    download = re.escape(urlsplit(base_url.replace("/rest.php", "/download.php")).path)
    return [
        rf"{rest}/guest",
        rf"{rest}/transfer",
        rf"{rest}/transfer/\d+",
        rf"{rest}/transfer/fileidsextended",
        download,
    ]


def make_filesender_client(cfg: FileSenderConfig) -> ReadOnlyClient:
    return ReadOnlyClient(
        allowed_methods=frozenset({"GET"}),
        allowed_paths=filesender_allowed_paths(cfg.base_url),
        verify=not cfg.insecure,
    )


class FileSenderSource:
    """Guest transfers (via the REST API) or a single transfer by download token."""

    def __init__(
        self,
        cfg: FileSenderConfig,
        client: ReadOnlyClient,
        guest_email: str | None = None,
        guest_id: int | None = None,
        token: str | None = None,
        limit: int | None = None,
        clock: Any = time.time,
    ):
        self.cfg = cfg
        self.client = client
        self.guest_email, self.guest_id, self.token, self.limit = (
            guest_email,
            guest_id,
            token,
            limit,
        )
        self._clock = clock
        self.centre = cfg.centre
        self.errors: list[str] = []
        self.source_id = (
            f"filesender:guest:{guest_id}" if guest_id is not None else "filesender:pending"
        )
        # Known before any request: the instance plus the guest, if one was named.
        self.scope_id = f"filesender:{cfg.base_url}" + (
            f":guest:{guest_id}"
            if guest_id is not None
            else f":guest-email:{guest_email.lower()}"
            if guest_email
            else ""
        )

    # signed GET --------------------------------------------------------------

    def _signed_get(self, path: str, params: dict[str, str] | None = None) -> Any:
        params = dict(params or {})
        params["remote_user"] = self.cfg.username
        params["timestamp"] = str(round(self._clock()))
        flat = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        domain = self.cfg.base_url.replace("https://", "", 1).replace("http://", "", 1)
        signed = f"get&{domain}{path}?{flat}"
        params["signature"] = hmac.new(
            self.cfg.apikey.encode(), signed.encode("ascii"), hashlib.sha1
        ).hexdigest()
        try:
            response = self.client.request(
                "GET",
                self.cfg.base_url + path,
                params=params,
                headers={"Accept": "application/json"},
            )
            response.raise_for_status()
            return response.json()
        except Exception as e:  # noqa: BLE001 - report any transport/auth error uniformly
            raise SourceError(f"FileSender {path}: {_scrub(str(e))}") from e

    def get_guests(self) -> GuestRows:
        result = self._signed_get("/guest")
        return list(result or [])

    def _transfers(self) -> GuestRows:
        return list(self._signed_get("/transfer") or [])

    # listing -----------------------------------------------------------------

    def list(self) -> RemoteFiles:
        if self.token:
            return self._list_token(self.token, subject=None)
        guest = self._find_guest()
        self.source_id = f"filesender:guest:{guest['id']}"
        self.centre = self.centre or guest.get("subject")
        transfers = [
            t
            for t in self._transfers()
            if (t.get("user_email") or "").lower() == guest["email"].lower()
        ]
        if self.limit:
            transfers = transfers[: self.limit]
        files: list[RemoteFile] = []
        for transfer in transfers:
            try:
                details = self._signed_get(f"/transfer/{transfer['id']}")
                recipients = details.get("recipients") or []
                if not recipients:
                    raise SourceError(f"transfer {transfer['id']} has no recipients")
                files += self._list_token(
                    recipients[0]["token"], subject=(details.get("subject") or "").strip()
                )
            except SourceError as e:
                self.errors.append(str(e))
        return files

    def _find_guest(self) -> dict[str, Any]:
        guests = self.get_guests()
        for guest in guests:
            if self.guest_email and guest["email"].lower() == self.guest_email.lower():
                return guest
            if self.guest_id is not None and guest["id"] == self.guest_id:
                return guest
        known = ", ".join(f"{g['email']} (ID {g['id']})" for g in guests) or "none"
        raise SourceError(f"guest not found; available guests: {known}")

    def _list_token(self, token: str, subject: str | None) -> RemoteFiles:
        listing = self._signed_get("/transfer/fileidsextended", {"token": token})
        if not listing:
            raise SourceError("transfer has no files")
        files = []
        for item in listing:
            tid = str(item.get("transferid") or "")
            if self.token and self.source_id == "filesender:pending":
                self.source_id = f"filesender:transfer:{tid}"
            problem = None
            if item.get("encrypted"):
                problem = (
                    "encrypted transfer: download.php would return ciphertext, "
                    "decryption is not supported"
                )
            files.append(
                RemoteFile(
                    key=f"filesender:{tid}:{item['id']}",
                    source_id=self.source_id,
                    group_id=tid,
                    group_label=subject or f"transfer_{tid}",
                    name=str(item["name"]),
                    size=int(item["size"]),
                    problem=problem,
                    extra={"token": token},
                )
            )
        return files

    # download ----------------------------------------------------------------

    @contextmanager
    def _stream(self, remote: RemoteFile) -> Iterator[Iterator[bytes]]:
        url = self.cfg.base_url.replace("/rest.php", "/download.php")
        file_id = remote.key.rsplit(":", 1)[1]
        response = self.client.request(
            "GET",
            url,
            params={"token": remote.extra["token"], "files_ids": file_id},
            stream=True,
            timeout=600,
        )
        try:
            response.raise_for_status()
            yield response.iter_content(chunk_size=CHUNK)
        finally:
            response.close()

    def open_stream(self, remote: RemoteFile) -> AbstractContextManager[Iterator[bytes]]:
        return self._stream(remote)


def _scrub(message: str) -> str:
    """Drop signature/timestamp query values from an error message."""
    return re.sub(r"(signature|timestamp|remote_user)=[^&\s]*", r"\1=…", message)


# --- SurfDrive (WebDAV) -----------------------------------------------------

_PROPFIND_BODY = (
    '<?xml version="1.0"?>'
    '<d:propfind xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns"><d:prop>'
    "<d:getcontentlength/><d:getetag/><d:resourcetype/><oc:fileid/><oc:checksums/>"
    "</d:prop></d:propfind>"
)


def make_surfdrive_client(cfg: SurfDriveConfig) -> ReadOnlyClient:
    """Only PROPFIND, GET and HEAD - never a write, even if the share grants edit rights."""
    return ReadOnlyClient(verify=not cfg.insecure)


def surfdrive_endpoint(cfg: SurfDriveConfig) -> str:
    if cfg.mode == "public":
        return f"{cfg.base_url}/public.php/webdav/"
    return f"{cfg.base_url}/remote.php/dav/files/{quote(cfg.username, safe='@')}/"


class SurfDriveSource:
    def __init__(self, cfg: SurfDriveConfig, client: ReadOnlyClient):
        if not cfg.base_url.startswith("https://") and not cfg.insecure:
            raise SourceError("SurfDrive base_url must be https")
        self.cfg, self.client = cfg, client
        self.centre = cfg.centre
        self.source_id = f"surfdrive:{cfg.username}"
        # Share (or account) plus the folder inside it: `?dir=` changes what rel_paths mean.
        self.scope_id = (
            f"surfdrive:{cfg.mode}:{cfg.base_url}:{cfg.username}:dir={cfg.remote_folder}"
        )
        self._root = surfdrive_endpoint(cfg)
        self._auth = (cfg.username, cfg.password)
        self._label = (
            cfg.name
            if cfg.name not in ("public", "user", "link")
            else f"surfdrive_{cfg.username[:8]}"
        )
        self.errors: list[str] = []

    def _url(self, rel: str) -> str:
        rel = "/".join(p for p in [self.cfg.remote_folder, rel] if p)
        return self._root + quote(rel.strip("/"), safe="/@")

    def _propfind(self, rel: str) -> list[DavEntry]:
        url = self._url(rel)
        response = self.client.request(
            "PROPFIND",
            url if url.endswith("/") else url + "/",
            headers={"Depth": "1", "Content-Type": "application/xml"},
            auth=self._auth,
            data=_PROPFIND_BODY,
            timeout=60,
        )
        if response.status_code == 401:
            raise SourceError("SurfDrive rejected the credentials (401)")
        if response.status_code != 207:
            raise SourceError(f"PROPFIND {rel or '/'}: HTTP {response.status_code}")
        base = urlsplit(self._url(rel)).path
        return parse_propfind(response.text, base)

    def list(self) -> RemoteFiles:
        files: list[RemoteFile] = []
        pending, seen = [""], set()
        while pending:
            directory = pending.pop()
            if directory in seen:
                continue
            seen.add(directory)
            try:
                entries = self._propfind(directory)
            except SourceError:
                if directory == "":
                    raise
                self.errors.append(f"could not list {directory}")
                continue
            for entry in entries:
                rel = f"{directory}/{entry.path}".strip("/") if directory else entry.path
                if entry.is_dir:
                    pending.append(rel)
                    continue
                remote_path = "/".join(p for p in [self.cfg.remote_folder, rel] if p)
                ident = entry.file_id or f"path:{remote_path}"
                files.append(
                    RemoteFile(
                        key=f"{self.source_id}:{ident}",
                        source_id=self.source_id,
                        group_id=self.cfg.username
                        + (f":{self.cfg.remote_folder}" if self.cfg.remote_folder else ""),
                        group_label=self._label,
                        name=rel,
                        size=entry.size or 0,
                        checksums=entry.checksums,
                        etag=entry.etag,
                        remote_path=remote_path,
                    )
                )
        return sorted(files, key=lambda f: f.name)

    @contextmanager
    def _stream(self, remote: RemoteFile) -> Iterator[Iterator[bytes]]:
        response = self.client.request(
            "GET",
            self._root + quote((remote.remote_path or remote.name).strip("/"), safe="/@"),
            auth=self._auth,
            stream=True,
            timeout=600,
        )
        try:
            response.raise_for_status()
            yield response.iter_content(chunk_size=CHUNK)
        finally:
            response.close()

    def open_stream(self, remote: RemoteFile) -> AbstractContextManager[Iterator[bytes]]:
        return self._stream(remote)
