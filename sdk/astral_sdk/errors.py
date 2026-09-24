"""Exception hierarchy the SDK raises for AstralClient/AsyncAstralClient failures:
AstralHTTPError carries the server's stable safe-error-code string, and
RetryExhaustedError wraps the last error once RetryPolicy gives up.
"""

from __future__ import annotations

from typing import Any, Optional


class AstralError(Exception):
    pass


class AstralHTTPError(AstralError):
    def __init__(self, message: str, *, status_code: Optional[int] = None,
                 code: Optional[str] = None, data: Any = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.data = data


class AstralAuthError(AstralHTTPError):
    pass


class AstralConflictError(AstralHTTPError):
    pass


class AstralTimeoutError(AstralError):
    pass


class RetryExhaustedError(AstralError):
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
