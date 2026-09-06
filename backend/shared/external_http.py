"""External HTTP egress helper for user-supplied URLs.

Used by the three external-service agents (CLASSify, Forecaster, LLM-Factory)
to talk to user-configured endpoints safely. Provides:

- ``normalize_url`` — adds ``https://`` if missing, strips trailing slash, lowercases scheme/host.
- ``validate_egress_url`` — rejects loopback / RFC1918 / link-local / non-http
  schemes (DNS rebinding is mitigated by resolving all A/AAAA records and
  rejecting if any resolve into a private range).
- ``request`` — wrapper over ``requests`` with optional Bearer auth (only when
  a non-blank key is supplied), bounded timeout, redirect-disable by default,
  and a response-size cap.

All upstream error classes are mapped to typed exceptions defined here, so
agent tools can render targeted user-facing messages (FR-021, FR-022).
"""
import ipaddress
import logging
import os
import socket
from typing import Any, Dict, Iterable, Optional
from urllib.parse import urlparse, urlunparse

import requests

# One decoder for IPv4-in-IPv6 encodings across both egress guards (HTTP here,
# SSH in net_guard) so they can never disagree about what an address *is*.
# ``net_guard`` is pure-stdlib and imports nothing from this module, so the
# dependency is one-way. Private name imported deliberately: promoting it to a
# public alias belongs in net_guard, which this change does not own.
from shared.net_guard import _effective_ip as _decode_embedded_ipv4

logger = logging.getLogger("external_http")

DEFAULT_TIMEOUT = 30
DEFAULT_MAX_RESPONSE_BYTES = 50 * 1024 * 1024


class ExternalHttpError(Exception):
    """Base class for all external-egress errors."""


class EgressBlockedError(ExternalHttpError):
    """URL fails SSRF policy (private host, bad scheme, etc.)."""


class AuthFailedError(ExternalHttpError):
    """Upstream returned 401 or 403."""


class ServiceUnreachableError(ExternalHttpError):
    """DNS / connection failure or timeout — retryable."""


class RateLimitedError(ExternalHttpError):
    """Upstream returned 429 or 5xx — retryable."""


class BadRequestError(ExternalHttpError):
    """Upstream returned a non-auth 4xx."""


class ResponseTooLargeError(ExternalHttpError):
    """Upstream response exceeded the configured size cap."""


def normalize_url(raw: str, *, preserve_trailing_slash: bool = False) -> str:
    """Normalize a user-supplied URL into a canonical form.

    - Adds ``https://`` when no scheme is present.
    - Lowercases the scheme and host.
    - Strips a trailing slash from the path by default for service endpoints.
      Page readers can preserve the exact resource path with
      ``preserve_trailing_slash=True``.
    """
    if raw is None or not str(raw).strip():
        raise EgressBlockedError("URL is empty")
    s = str(raw).strip()
    if "://" not in s:
        s = "https://" + s
    parsed = urlparse(s)
    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()
    path = parsed.path or ""
    # Strip trailing slash from any path (including the root "/").
    if not preserve_trailing_slash and path.endswith("/"):
        path = path[:-1]
    return urlunparse((scheme, netloc, path, parsed.params, parsed.query, parsed.fragment))


def _resolve_host_addresses(host: str) -> Iterable[str]:
    """Yield every address (IPv4 + IPv6) that ``host`` resolves to."""
    try:
        info = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        raise ServiceUnreachableError(f"DNS resolution failed for '{host}': {e}") from e
    seen = set()
    for _family, _socktype, _proto, _canon, sockaddr in info:
        addr = sockaddr[0]
        if addr not in seen:
            seen.add(addr)
            yield addr


def _classify_blocked(ip) -> bool:
    """True when a parsed address object is loopback / private / link-local /
    multicast / unspecified / reserved."""
    return bool(
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_unspecified
        or ip.is_reserved
    )


def _is_private_address(addr: str) -> bool:
    """Return True if the IP literal is loopback / private / link-local / multicast / unspecified / reserved.

    The literal is classified AND, when it carries an IPv4-in-IPv6 encoding
    (``::ffff:a.b.c.d``, 6to4 ``2002:…``, NAT64 ``64:ff9b::…``), so is the
    embedded IPv4 — either verdict blocks. CPython >= 3.11.10 (post
    CVE-2024-4032) already classifies those wrappers correctly on its own, so
    the decode is defense in depth against an interpreter that does not; ORing
    the two verdicts means it can only ever tighten the result, never widen it.
    An unparseable literal is blocked (fail-closed).
    """
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return True
    return _classify_blocked(ip) or _classify_blocked(_decode_embedded_ipv4(addr))


def _allowed_private_hosts_from_env() -> Iterable[str]:
    raw = os.getenv("EXTERNAL_AGENT_ALLOWED_PRIVATE_HOSTS", "")
    return [h.strip() for h in raw.split(",") if h.strip()]


def validate_egress_url(
    url: str,
    allowed_private_hosts: Optional[Iterable[str]] = None,
) -> None:
    """Raise :class:`EgressBlockedError` if the URL is not a safe egress target."""
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https"):
        raise EgressBlockedError(f"Scheme '{scheme}' is not allowed (only http/https)")
    host = parsed.hostname
    if not host:
        raise EgressBlockedError("URL has no host")
    allowed = set(allowed_private_hosts or _allowed_private_hosts_from_env())
    if host in allowed:
        return
    try:
        addresses = list(_resolve_host_addresses(host))
    except ServiceUnreachableError:
        # Surface the DNS failure as an egress block (clearer to the user).
        raise EgressBlockedError(f"Host '{host}' could not be resolved")
    for addr in addresses:
        if _is_private_address(addr):
            raise EgressBlockedError(
                f"Host '{host}' resolves to private/loopback address '{addr}'; "
                "egress is blocked. Add the host to EXTERNAL_AGENT_ALLOWED_PRIVATE_HOSTS to override."
            )


def request(
    method: str,
    url: str,
    *,
    api_key: str,
    json_body: Any = None,
    files: Optional[Dict[str, Any]] = None,
    data: Optional[Dict[str, Any]] = None,
    params: Optional[Dict[str, Any]] = None,
    timeout: int = DEFAULT_TIMEOUT,
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    allow_redirects: bool = False,
    extra_headers: Optional[Dict[str, str]] = None,
    allowed_private_hosts: Optional[Iterable[str]] = None,
) -> requests.Response:
    """Make an HTTP request to a user-supplied external service.

    Enforces the SSRF guard, sets ``Authorization: Bearer <api_key>`` when
    ``api_key`` is non-blank (keyless callers pass ``""`` and get NO
    ``Authorization`` header — a bare ``Bearer `` is malformed and some
    origins, e.g. Wikipedia, answer it with 400), caps the response body at
    ``max_response_bytes``, and maps upstream error classes to typed
    exceptions:

    - 401 / 403 → :class:`AuthFailedError`
    - 429 / 5xx → :class:`RateLimitedError`
    - other 4xx → :class:`BadRequestError`
    - DNS / timeout / connection refused → :class:`ServiceUnreachableError`
    - oversize body → :class:`ResponseTooLargeError`

    Returns the ``requests.Response`` on 2xx (3xx is treated as 2xx when
    ``allow_redirects=True``). The caller is responsible for parsing JSON.
    """
    validate_egress_url(url, allowed_private_hosts=allowed_private_hosts)
    headers: Dict[str, str] = {}
    if isinstance(api_key, str) and api_key.strip():
        headers["Authorization"] = f"Bearer {api_key}"
    if extra_headers:
        headers.update(extra_headers)
    if files is None and data is None and json_body is not None:
        headers["Content-Type"] = "application/json"
    try:
        resp = requests.request(
            method.upper(),
            url,
            headers=headers,
            json=json_body,
            files=files,
            data=data,
            params=params,
            timeout=timeout,
            allow_redirects=allow_redirects,
            stream=True,
        )
    except (requests.ConnectionError, requests.Timeout) as e:
        raise ServiceUnreachableError(f"Could not reach {url}: {e}") from e
    except requests.RequestException as e:
        raise ServiceUnreachableError(f"HTTP transport error: {e}") from e

    chunks = []
    total = 0
    try:
        for chunk in resp.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            chunks.append(chunk)
            total += len(chunk)
            if total > max_response_bytes:
                resp.close()
                raise ResponseTooLargeError(
                    f"Response exceeded {max_response_bytes} bytes"
                )
    finally:
        resp.close()
    resp._content = b"".join(chunks)
    resp._content_consumed = True

    status = resp.status_code
    if status in (401, 403):
        raise AuthFailedError(f"Authentication failed ({status})")
    if status == 429:
        snippet = (resp.text or "")[:500]
        raise RateLimitedError(f"Rate-limited by upstream ({status}): {snippet}")
    if 500 <= status < 600:
        # 5xx is mapped to RateLimitedError for retry purposes (the orchestrator's
        # retry policy treats it as transient), but the message must NOT claim
        # rate-limiting — surface the upstream's body so the LLM and user can
        # see what actually went wrong (e.g. "model doesn't support embeddings").
        snippet = (resp.text or "")[:500]
        raise RateLimitedError(f"Upstream server error ({status}): {snippet}")
    if 400 <= status < 500:
        snippet = (resp.text or "")[:500]
        raise BadRequestError(f"Upstream returned {status}: {snippet}")
    return resp
