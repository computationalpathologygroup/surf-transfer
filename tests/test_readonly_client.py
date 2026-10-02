"""The read-only rule, enforced in code."""

import ast
import json
import re
from pathlib import Path

import pytest

from fakes import FakeResponse, FakeSession
from surf_transfer.config import FileSenderConfig, SurfDriveConfig
from surf_transfer.http import READ_ONLY_METHODS, ForbiddenRequest, ReadOnlyClient
from surf_transfer.sources import (
    FileSenderSource,
    SurfDriveSource,
    filesender_allowed_paths,
    make_filesender_client,
    make_surfdrive_client,
)

WRITE_METHODS = [
    "PUT",
    "DELETE",
    "MOVE",
    "COPY",
    "MKCOL",
    "PROPPATCH",
    "LOCK",
    "UNLOCK",
    "POST",
    "PATCH",
    "OPTIONS",
    "TRACE",
    "put",
    "delete",
    "Move",
    "SEARCH",
    "",
    "FOO",
]


@pytest.mark.parametrize("method", WRITE_METHODS)
def test_every_other_method_raises_before_anything_is_sent(method):
    session = FakeSession()
    client = ReadOnlyClient(session=session)
    with pytest.raises(ForbiddenRequest):
        client.request(method, "https://surfdrive.surf.nl/public.php/webdav/x")
    assert session.calls == []


@pytest.mark.parametrize("method", ["GET", "get", "HEAD", "PROPFIND"])
def test_allowed_methods_are_sent(method):
    session = FakeSession()
    ReadOnlyClient(session=session).request(method, "https://h/x")
    assert session.calls[0][0] == method.upper()


def test_a_client_cannot_be_configured_to_allow_writes():
    with pytest.raises(ForbiddenRequest):
        ReadOnlyClient(allowed_methods=frozenset({"GET", "PUT"}))


def test_redirect_to_http_is_refused_and_not_followed():
    def handler(method, url, kw):
        return FakeResponse(
            301, headers={"Location": "http://surfdrive.surf.nl/public.php/webdav/"}
        )

    session = FakeSession(handler)
    with pytest.raises(ForbiddenRequest, match="redirect"):
        ReadOnlyClient(session=session).request("PROPFIND", "https://surfdrive.surf.nl/files/x")
    assert len(session.calls) == 1


def test_redirect_to_another_host_is_refused():
    def handler(method, url, kw):
        return FakeResponse(302, headers={"Location": "https://evil.example/x"})

    with pytest.raises(ForbiddenRequest):
        ReadOnlyClient(session=FakeSession(handler)).request("GET", "https://h/x")


def test_same_host_redirect_is_followed_with_the_same_method():
    def handler(method, url, kw):
        if url.endswith("/a"):
            return FakeResponse(301, headers={"Location": "/b"})
        return FakeResponse(200)

    session = FakeSession(handler)
    response = ReadOnlyClient(session=session).request("PROPFIND", "https://h/a")
    assert response.status_code == 200
    assert [c[0] for c in session.calls] == ["PROPFIND", "PROPFIND"]
    assert all(c[2]["allow_redirects"] is False for c in session.calls)


# --- FileSender endpoint allowlist -----------------------------------------

FS = FileSenderConfig(base_url="https://filesender.surf.nl/rest.php", username="u@x.nl", apikey="k")


@pytest.mark.parametrize(
    "path",
    [
        "/rest.php/guest",
        "/rest.php/transfer",
        "/rest.php/transfer/123",
        "/rest.php/transfer/fileidsextended",
        "/download.php",
    ],
)
def test_filesender_read_endpoints_are_allowed(path):
    session = FakeSession()
    client = ReadOnlyClient(
        frozenset({"GET"}), filesender_allowed_paths(FS.base_url), session=session
    )
    client.request("GET", "https://filesender.surf.nl" + path)
    assert len(session.calls) == 1


@pytest.mark.parametrize(
    "path",
    [
        "/rest.php/transfer/123/close",
        "/rest.php/transfer/123/extend",
        "/rest.php/guest/5",
        "/rest.php/transfer/abc",
        "/rest.php/user",
        "/rest.php/transfer/123/recipient",
        "/index.php",
        "/rest.php/transfer/fileidsextended/x",
    ],
)
def test_filesender_other_endpoints_are_refused(path):
    session = FakeSession()
    client = ReadOnlyClient(
        frozenset({"GET"}), filesender_allowed_paths(FS.base_url), session=session
    )
    with pytest.raises(ForbiddenRequest, match="allowlist"):
        client.request("GET", "https://filesender.surf.nl" + path)
    assert session.calls == []


@pytest.mark.parametrize("method", ["DELETE", "PUT", "POST", "PATCH"])
def test_filesender_source_cannot_send_writes_even_on_allowed_paths(method):
    session = FakeSession()
    client = ReadOnlyClient(
        frozenset({"GET"}), filesender_allowed_paths(FS.base_url), session=session
    )
    with pytest.raises(ForbiddenRequest):
        client.request(method, "https://filesender.surf.nl/rest.php/transfer/123")
    assert session.calls == []


def test_filesender_client_factory_only_allows_get():
    client = make_filesender_client(FS)
    assert client.allowed_methods == {"GET"}


def test_filesender_listing_only_issues_get_requests_on_read_endpoints():
    def handler(method, url, kw):
        if url.endswith("/guest"):
            return FakeResponse(json_data=[{"id": 7, "email": "g@x.nl", "subject": "Centre A"}])
        if url.endswith("/rest.php/transfer"):
            return FakeResponse(json_data=[{"id": 11, "user_email": "g@x.nl", "files": []}])
        if url.endswith("/transfer/11"):
            return FakeResponse(json_data={"subject": "Batch", "recipients": [{"token": "tok"}]})
        return FakeResponse(
            json_data=[
                {"id": 5, "transferid": 11, "name": "X/Data0000.dat", "size": 3, "encrypted": False}
            ]
        )

    session = FakeSession(handler)
    client = ReadOnlyClient(
        frozenset({"GET"}), filesender_allowed_paths(FS.base_url), session=session
    )
    source = FileSenderSource(FS, client, guest_email="g@x.nl")
    files = source.list()
    assert [c[0] for c in session.calls] == ["GET"] * len(session.calls) and session.calls
    assert files[0].key == "filesender:11:5"
    assert files[0].name == "X/Data0000.dat"  # sub-path preserved when the listing has it
    assert source.source_id == "filesender:guest:7"
    assert source.centre == "Centre A"


def test_filesender_encrypted_transfer_is_flagged_not_downloaded():
    def handler(method, url, kw):
        return FakeResponse(
            json_data=[{"id": 5, "transferid": 11, "name": "a", "size": 3, "encrypted": True}]
        )

    client = ReadOnlyClient(
        frozenset({"GET"}), filesender_allowed_paths(FS.base_url), session=FakeSession(handler)
    )
    (remote,) = FileSenderSource(FS, client, token="tok").list()
    assert "encrypted" in remote.problem


def test_filesender_signature_matches_the_documented_scheme():
    import hashlib
    import hmac

    session = FakeSession(lambda m, u, kw: FakeResponse(json_data=[]))
    client = ReadOnlyClient(
        frozenset({"GET"}), filesender_allowed_paths(FS.base_url), session=session
    )
    FileSenderSource(FS, client, guest_id=1, clock=lambda: 1000).get_guests()
    params = session.calls[0][2]["params"]
    expected = hmac.new(
        b"k",
        b"get&filesender.surf.nl/rest.php/guest?remote_user=u@x.nl&timestamp=1000",
        hashlib.sha1,
    ).hexdigest()
    assert params["signature"] == expected


def test_filesender_errors_do_not_leak_the_signature():
    def handler(method, url, kw):
        return FakeResponse(500)

    client = ReadOnlyClient(
        frozenset({"GET"}), filesender_allowed_paths(FS.base_url), session=FakeSession(handler)
    )
    from surf_transfer.sources import SourceError

    with pytest.raises(SourceError) as info:
        FileSenderSource(FS, client, guest_id=1).get_guests()
    assert "signature=" not in str(info.value) or "signature=…" in str(info.value)


# --- SurfDrive ------------------------------------------------------------

SD = SurfDriveConfig(
    mode="public",
    base_url="https://surfdrive.surf.nl",
    name="public",
    username="TOKEN",
    password="",
)


def test_surfdrive_client_factory_allows_only_read_methods():
    assert make_surfdrive_client(SD).allowed_methods == READ_ONLY_METHODS


def test_surfdrive_source_only_sends_read_methods():
    root = """<d:multistatus xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">
    <d:response><d:href>/public.php/webdav/</d:href><d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype></d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>
    <d:response><d:href>/public.php/webdav/a.svs</d:href><d:propstat><d:prop><d:getcontentlength>4</d:getcontentlength><d:resourcetype/><oc:fileid>9</oc:fileid></d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>
    </d:multistatus>"""
    session = FakeSession(lambda m, u, kw: FakeResponse(207, text=root, content=b"abcd"))
    source = SurfDriveSource(SD, ReadOnlyClient(session=session))
    (remote,) = source.list()
    with source.open_stream(remote) as chunks:
        assert b"".join(chunks) == b"abcd"
    assert {c[0] for c in session.calls} <= READ_ONLY_METHODS
    assert remote.key == "surfdrive:TOKEN:9"
    assert session.calls[0][1] == "https://surfdrive.surf.nl/public.php/webdav/"
    assert session.calls[0][2]["auth"] == ("TOKEN", "")
    assert session.calls[0][2]["headers"]["Depth"] == "1"


def test_surfdrive_refuses_plain_http_base_url():
    from surf_transfer.sources import SourceError

    cfg = SurfDriveConfig(
        mode="public", base_url="http://surfdrive.surf.nl", name="p", username="T"
    )
    with pytest.raises(SourceError, match="https"):
        SurfDriveSource(cfg, ReadOnlyClient(session=FakeSession()))


# --- no code path bypasses the client -------------------------------------

SRC = Path(__file__).parent.parent / "src" / "surf_transfer"
HTTP_LIBS = {
    "requests",
    "urllib3",
    "http.client",
    "http.server",
    "httpx",
    "aiohttp",
    "socket",
    "urllib.request",
    "ftplib",
    "pycurl",
    "smtplib",
}
ROOT_MODULES = [
    Path(__file__).parent.parent / n for n in ("download_guest_transfers.py", "download_state.py")
]


def _imports(path: Path) -> set[str]:
    """Dotted module names imported by a file, including `from urllib import request`."""
    found = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            found |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
            found |= {f"{node.module}.{a.name}" for a in node.names}
    return found


def _is_http(name: str) -> bool:
    return any(name == lib or name.startswith(lib + ".") for lib in HTTP_LIBS)


def test_only_the_http_module_imports_an_http_library():
    offenders = {
        p.name: sorted(n for n in _imports(p) if _is_http(n))
        for p in list(SRC.glob("*.py")) + [m for m in ROOT_MODULES if m.exists()]
        if p.name != "http.py" and any(_is_http(n) for n in _imports(p))
    }
    assert offenders == {}


def test_no_dynamic_import_or_raw_request_calls_elsewhere():
    pattern = re.compile(
        r"__import__\(|importlib\.import_module|requests\.(get|post|put|delete|request|Session)"
    )
    offenders = [
        p.name for p in SRC.glob("*.py") if p.name != "http.py" and pattern.search(p.read_text())
    ]
    assert offenders == []


def test_no_write_method_literal_is_ever_passed_to_the_client():
    forbidden = {
        "PUT",
        "DELETE",
        "MOVE",
        "COPY",
        "MKCOL",
        "PROPPATCH",
        "LOCK",
        "UNLOCK",
        "POST",
        "PATCH",
    }
    hits = []
    for path in SRC.glob("*.py"):
        if path.name == "http.py":
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "request"
            ):
                first = node.args[0] if node.args else None
                if isinstance(first, ast.Constant) and str(first.value).upper() in forbidden:
                    hits.append((path.name, node.lineno))
    assert hits == []


def test_policy_error_message_is_json_safe():
    assert json.dumps(str(ForbiddenRequest("x")))
