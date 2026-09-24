"""Shared helpers wrapping shared.external_http so the Outlook, Canva, and Adobe
connector tools get uniform credential-test verdicts and user-facing error strings.
"""

from typing import Dict

from shared.external_http import (
    AuthFailedError,
    BadRequestError,
    EgressBlockedError,
    RateLimitedError,
    ServiceUnreachableError,
)


def verdict_for_exception(exc: Exception) -> Dict[str, str]:
    if isinstance(exc, AuthFailedError):
        return {"credential_test": "auth_failed", "detail": str(exc)}
    if isinstance(exc, (ServiceUnreachableError, EgressBlockedError, RateLimitedError)):
        return {"credential_test": "unreachable", "detail": str(exc)}
    return {"credential_test": "unexpected", "detail": str(exc)}


def user_facing_error(exc: Exception, service: str) -> str:
    if isinstance(exc, AuthFailedError):
        return f"The saved {service} credentials were rejected. Update them in the agent's settings."
    if isinstance(exc, ServiceUnreachableError):
        return f"{service} is unreachable. Try again later."
    if isinstance(exc, RateLimitedError):
        return f"{service} call failed: {exc}"
    if isinstance(exc, EgressBlockedError):
        return f"{service} URL is not allowed: {exc}"
    if isinstance(exc, BadRequestError):
        return f"{service} rejected the request: {exc}"
    return f"{service} call failed: {exc}"
