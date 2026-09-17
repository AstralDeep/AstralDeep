"""Exceptions raised by the Astral SDK."""
from __future__ import annotations

from typing import Any, Optional


class AstralError(Exception):
    """Base class for every error the SDK raises."""


class AstralHTTPError(AstralError):
    """The server answered with a non-2xx status or a well-formed JSON-RPC error.

    ``code`` is the server's ``safe_error_code``/JSON-RPC error string (never a
    stack trace or free-text detail beyond what the server itself returns) —
    stable strings a caller can match on, e.g. ``"assignment_human_required"``
    or ``"work_not_found"``.
    """

    def __init__(self, message: str, *, status_code: Optional[int] = None,
                 code: Optional[str] = None, data: Any = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.data = data


class AstralAuthError(AstralHTTPError):
    """The bearer was rejected (missing, malformed, revoked, expired, exhausted)."""


class AstralConflictError(AstralHTTPError):
    """An optimistic-concurrency precondition failed (stale revision, replay conflict)."""


class AstralTimeoutError(AstralError):
    """A request exceeded its configured timeout without a server response."""


class RetryExhaustedError(AstralError):
    """A ``RetryPolicy`` gave up after its bounded number of attempts.

    ``last_error`` is the final underlying exception, preserved for
    diagnostics via ``raise ... from``.
    """

    def __init__(self, message: str, *, attempts: int, last_error: Optional[BaseException] = None) -> None:
        super().__init__(message)
        self.attempts = attempts
        self.last_error = last_error


__all__ = [
    "AstralError",
    "AstralHTTPError",
    "AstralAuthError",
    "AstralConflictError",
    "AstralTimeoutError",
    "RetryExhaustedError",
]
