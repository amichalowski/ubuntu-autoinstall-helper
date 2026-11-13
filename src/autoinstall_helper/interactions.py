"""Shared helpers for recording and replaying external interactions."""

from __future__ import annotations

import urllib.request
from typing import Any


class FakeResponse:
    """Minimal stand-in for the object returned by ``urllib.request.urlopen``."""

    def __init__(self, body: bytes, status: int, headers: dict[str, str]) -> None:
        self._body = body
        self.status = status
        self.code = status
        self.headers = headers

    def read(self) -> bytes:
        return self._body

    def geturl(self) -> str:
        return ""

    def close(self) -> None:
        pass

    def __enter__(self) -> FakeResponse:  # noqa: PYI034 (Self needs Python 3.11+; target is 3.10)
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def extract_url(url_or_req: Any) -> str:
    """Return the target URL for either a plain URL string or a Request."""
    if isinstance(url_or_req, urllib.request.Request):
        return url_or_req.full_url
    return str(url_or_req)


def extract_method(url_or_req: Any) -> str:
    """Return the HTTP method (Request defaults to GET when unset)."""
    if isinstance(url_or_req, urllib.request.Request):
        return url_or_req.get_method() or "GET"
    return "GET"


def extract_headers(url_or_req: Any) -> dict[str, str]:
    """Return the request headers (empty for a plain URL string)."""
    if isinstance(url_or_req, urllib.request.Request):
        return dict(url_or_req.header_items())
    return {}


def decode(data: bytes | str | None) -> str | None:
    """Return text for recorded output, decoding bytes defensively."""
    if data is None:
        return None
    if isinstance(data, bytes):
        return data.decode("utf-8", errors="replace")
    return data
