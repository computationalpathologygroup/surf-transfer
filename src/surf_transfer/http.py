"""The only module allowed to talk HTTP.

Remote sources are strictly read-only. ReadOnlyClient holds an allowlist of
methods (and optionally of URL paths) and raises ForbiddenRequest *before*
anything is sent. tests/test_readonly_client.py also fails if any other module
imports an HTTP library, so nothing can bypass this.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import urljoin, urlsplit

import requests

READ_ONLY_METHODS = frozenset({"GET", "HEAD", "PROPFIND"})
_MAX_REDIRECTS = 5


class ForbiddenRequest(Exception):
    """Raised when code tries to send a request the read-only policy forbids."""


class ReadOnlyClient:
    def __init__(
        self,
        allowed_methods: frozenset[str] = READ_ONLY_METHODS,
        allowed_paths: Sequence[str] | None = None,
        verify: bool = True,
        session: Any = None,
    ):
        extra = {m.upper() for m in allowed_methods} - READ_ONLY_METHODS
        if extra:
            raise ForbiddenRequest(f"cannot allow write methods: {sorted(extra)}")
        self.allowed_methods = frozenset(m.upper() for m in allowed_methods)
        self._allowed_paths = [re.compile(p) for p in allowed_paths] if allowed_paths else None
        self.verify = verify
        self._session = session if session is not None else requests.Session()

    def check(self, method: str, url: str) -> None:
        """Raise ForbiddenRequest unless this method and URL path are allowed."""
        if method.upper() not in self.allowed_methods:
            raise ForbiddenRequest(
                f"{method.upper()} is not allowed; remote sources are read-only "
                f"(allowed: {', '.join(sorted(self.allowed_methods))})"
            )
        if self._allowed_paths is not None:
            path = urlsplit(url).path
            if not any(p.fullmatch(path) for p in self._allowed_paths):
                raise ForbiddenRequest(f"endpoint {path} is not on the read-only allowlist")

    def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        auth: tuple[str, str] | None = None,
        data: bytes | str | None = None,
        stream: bool = False,
        timeout: float = 30,
    ) -> requests.Response:
        method = method.upper()
        self.check(method, url)
        origin = urlsplit(url)
        for _ in range(_MAX_REDIRECTS + 1):
            response = self._session.request(
                method,
                url,
                params=params,
                headers=headers,
                auth=auth,
                data=data,
                stream=stream,
                timeout=timeout,
                verify=self.verify,
                allow_redirects=False,
            )
            if response.status_code not in (301, 302, 303, 307, 308):
                return response  # type: ignore[no-any-return]
            target = urljoin(url, response.headers.get("Location", ""))
            parts = urlsplit(target)
            if parts.scheme != origin.scheme or parts.netloc != origin.netloc:
                raise ForbiddenRequest(
                    f"refusing redirect from {origin.scheme}://{origin.netloc} to {target}: "
                    "credentials stay on the original scheme and host"
                )
            self.check(method, target)
            response.close()
            url, params = target, None
        raise ForbiddenRequest("too many redirects")
