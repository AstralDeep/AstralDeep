"""Stdlib-only stand-in for requests.request used across agent test suites: install a
route table and any matching request returns the configured (status, body) pair,
avoiding the responses third-party dependency.
"""

from __future__ import annotations

import json as _json
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import patch


class _FakeResponse:
    def __init__(self, status_code: int, body: bytes, headers: Optional[Dict[str, str]] = None) -> None:
        self.status_code = status_code
        self._content = body
        self.headers = headers or {}

    def iter_content(self, chunk_size: int = 64 * 1024):
        if not self._content:
            return iter([])
        view = memoryview(self._content)
        return (bytes(view[i:i + chunk_size]) for i in range(0, len(view), chunk_size))

    def close(self) -> None:
        pass

    @property
    def content(self) -> bytes:
        return self._content

    @property
    def text(self) -> str:
        try:
            return self._content.decode("utf-8")
        except UnicodeDecodeError:
            return self._content.decode("latin1", errors="replace")

    def json(self) -> Any:
        if not self._content:
            return None
        return _json.loads(self._content.decode("utf-8"))


class HttpMock:
    def __init__(self) -> None:
        self.routes: List[Tuple[str, str, _FakeResponse]] = []
        self.calls: List[Dict[str, Any]] = []
        self._patcher = None

    def add(self, method: str, url: str, *, status: int = 200,
            json: Any = None, body: Optional[bytes] = None,
            headers: Optional[Dict[str, str]] = None) -> None:
        if body is None:
            body = (_json.dumps(json).encode("utf-8")) if json is not None else b""
        self.routes.append((method.upper(), url, _FakeResponse(status, body, headers)))

    def _handler(self, method: str, url: str, **kwargs: Any) -> _FakeResponse:
        self.calls.append({"method": method.upper(), "url": url, **kwargs})
        for m, u, resp in self.routes:
            if m == method.upper() and u == url:
                return resp
        return _FakeResponse(404, b'{"detail": "no mock route registered"}')

    def __enter__(self) -> "HttpMock":
        self._patcher = patch("requests.request", side_effect=self._handler)
        self._patcher.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._patcher is not None:
            self._patcher.stop()
            self._patcher = None
