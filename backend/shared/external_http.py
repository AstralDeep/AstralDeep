"""SSRF-guarded HTTP helper for user-supplied endpoints (CLASSify, Forecaster,
LLM-Factory agents): rejects loopback/private/link-local targets on every A/AAAA
record, and maps upstream errors to typed exceptions.
"""

import ipaddress
import logging
import os
import socket
from typing import Any, Dict, Iterable, Optional
from urllib.parse import urlparse, urlunparse

import requests

# Private import on purpose: keeps both SSRF guards in sync
from shared.net_guard import _effective_ip as _decode_embedded_ipv4

logger = logging.getLogger("external_http")

DEFAULT_TIMEOUT = 30
DEFAULT_MAX_RESPONSE_BYTES = 50 * 1024 * 1024


class ExternalHttpError(Exception):
    pass


class EgressBlockedError(ExternalHttpError):
    pass


class AuthFailedError(ExternalHttpError):
    pass


class ServiceUnreachableError(ExternalHttpError):
    pass


class RateLimitedError(ExternalHttpError):
    pass


class BadRequestError(ExternalHttpError):
    pass


class ResponseTooLargeError(ExternalHttpError):
    pass


class ContentEncodingError(ExternalHttpError):
    pass


def normalize_url(raw: str, *, preserve_trailing_slash: bool = False) -> str:
    if raw is None or not str(raw).strip():
        raise EgressBlockedError("URL is empty")
    s = str(raw).strip()
    if "://" not in s:
        s = "https://" + s
    parsed = urlparse(s)
    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()
    path = parsed.path or ""
    if not preserve_trailing_slash and path.endswith("/"):
        path = path[:-1]
    return urlunparse((scheme, netloc, path, parsed.params, parsed.query, parsed.fragment))


def _resolve_host_addresses(host: str) -> Iterable[str]:
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
    return bool(
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_unspecified
        or ip.is_reserved
    )


def _is_private_address(addr: str) -> bool:
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
    require_identity_encoding: bool = False,
    trust_environment: bool = True,
) -> requests.Response:
    validate_egress_url(url, allowed_private_hosts=allowed_private_hosts)
    headers: Dict[str, str] = {}
    if isinstance(api_key, str) and api_key.strip():
        headers["Authorization"] = f"Bearer {api_key}"
    if extra_headers:
        headers.update(extra_headers)
    if require_identity_encoding:
        headers = {
            key: value for key, value in headers.items()
            if key.lower() != "accept-encoding"
        }
        headers["Accept-Encoding"] = "identity"
    if files is None and data is None and json_body is not None:
        headers["Content-Type"] = "application/json"
    try:
        options = dict(
            headers=headers,
            json=json_body,
            files=files,
            data=data,
            params=params,
            timeout=timeout,
            allow_redirects=allow_redirects,
            stream=True,
        )
        if trust_environment:
            resp = requests.request(method.upper(), url, **options)
        else:
            with requests.Session() as session:
                session.trust_env = False
                resp = session.request(method.upper(), url, **options)
    except (requests.ConnectionError, requests.Timeout) as e:
        raise ServiceUnreachableError(f"Could not reach {url}: {e}") from e
    except requests.RequestException as e:
        raise ServiceUnreachableError(f"HTTP transport error: {e}") from e

    chunks = []
    total = 0
    try:
        if require_identity_encoding and resp.headers.get(
            "Content-Encoding", "identity"
        ).strip().lower() != "identity":
            raise ContentEncodingError("Response content encoding is not identity")
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
        snippet = (resp.text or "")[:500]
        raise RateLimitedError(f"Upstream server error ({status}): {snippet}")
    if 400 <= status < 500:
        snippet = (resp.text or "")[:500]
        raise BadRequestError(f"Upstream returned {status}: {snippet}")
    return resp
