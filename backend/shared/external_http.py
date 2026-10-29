"""SSRF-guarded HTTP helper for user-supplied endpoints (CLASSify, Forecaster,
LLM-Factory agents): rejects loopback/private/link-local targets on every A/AAAA
record, and maps upstream errors to typed exceptions.
"""

import ipaddress
import logging
import math
import re
import os
import socket
from typing import Any, Dict, Iterable, Optional
from urllib.parse import urlparse, urlsplit, urlunparse

import httpx
import requests
from urllib3.connection import HTTPConnection
from urllib3.connectionpool import HTTPConnectionPool
from urllib3.exceptions import NewConnectionError

# Private import on purpose: keeps both SSRF guards in sync
from shared.net_guard import _effective_ip as _decode_embedded_ipv4
from shared.net_guard import is_private_http_address

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
    allowed = set(_allowed_private_hosts_from_env() if allowed_private_hosts is None else allowed_private_hosts)
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


_PRIVATE_HTTP_AUTHORITY = object()


class _PrivateHttpCapture:
    __slots__ = ("_authority", "url", "hosts", "family", "address", "port", "host_header")

    def __init__(self, authority, url, hosts, family, address, port, host_header):
        if authority is not _PRIVATE_HTTP_AUTHORITY:
            raise EgressBlockedError("private HTTP capture unavailable")
        for name, value in zip(self.__slots__,
                (authority, url, hosts, family, address, port, host_header), strict=True):
            object.__setattr__(self, name, value)

    def __setattr__(self, name, value):
        raise AttributeError("private HTTP capture is immutable")


def _private_http_url(value):
    if (type(value) is not str or not 1 <= len(value) <= 4096
            or any(not 33 <= ord(char) <= 126 for char in value)):
        raise ValueError
    parsed = urlsplit(value)
    if (parsed.scheme != "http" or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment
            or not re.fullmatch(r"[A-Za-z0-9._~!$&'()*+,;=:@/-]*", parsed.path)
            or "//" in parsed.path or any(segment in {".", ".."} for segment in parsed.path.split("/"))):
        raise ValueError
    port = parsed.port if parsed.port is not None else 80
    if not 1 <= port <= 65535:
        raise ValueError
    canonical = requests.Request("POST", value).prepare().url
    return canonical, parsed.hostname, port, urlsplit(canonical).netloc


def _resolve_private_http_addresses(host, port):
    return socket.getaddrinfo(host, port, family=socket.AF_UNSPEC,
                              type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)


def capture_private_http_target(url, *, allowed_private_hosts):
    try:
        canonical, host, port, host_header = _private_http_url(url)
        hosts = allowed_private_hosts
        if (type(hosts) is not tuple or len(hosts) > 32 or hosts != tuple(sorted(set(hosts)))
                or any(type(item) is not str or re.fullmatch(r"[a-z0-9.:-]{1,253}", item) is None for item in hosts)
                or host not in hosts):
            raise ValueError
        validate_egress_url(url, allowed_private_hosts=hosts)
        records = _resolve_private_http_addresses(host, port)
        if type(records) not in (list, tuple) or not 1 <= len(records) <= 8:
            raise ValueError
        approved = []
        for record in records:
            if (type(record) is not tuple or len(record) != 5
                    or record[0] not in (socket.AF_INET, socket.AF_INET6)
                    or record[1] != socket.SOCK_STREAM or record[2] != socket.IPPROTO_TCP
                    or type(record[4]) is not tuple or len(record[4]) < 2):
                raise ValueError
            address = ipaddress.ip_address(record[4][0])
            if ("%" in str(address) or not is_private_http_address(str(address))
                    or address.version != (4 if record[0] == socket.AF_INET else 6)):
                raise ValueError
            approved.append((record[0], address.compressed))
        family, address = approved[0]
        return _PrivateHttpCapture(_PRIVATE_HTTP_AUTHORITY, canonical, hosts,
                                   family, address, port, host_header)
    except (ValueError, TypeError, OSError, requests.RequestException):
        raise EgressBlockedError("private HTTP capture unavailable") from None


class _PrivateHttpAdapter(requests.adapters.HTTPAdapter):
    def __init__(self, captured):
        super().__init__(max_retries=0)
        class Connection(HTTPConnection):
            def _new_conn(self):
                connection = socket.socket(captured.family, socket.SOCK_STREAM, socket.IPPROTO_TCP)
                try:
                    if (type(self.timeout) not in (int, float) or not math.isfinite(self.timeout)
                            or self.timeout <= 0):
                        raise ValueError
                    connection.settimeout(self.timeout)
                    for option in self.socket_options or ():
                        connection.setsockopt(*option)
                    address = ((captured.address, captured.port) if captured.family == socket.AF_INET
                               else (captured.address, captured.port, 0, 0))
                    connection.connect(address)
                    return connection
                except (OSError, ValueError):
                    connection.close()
                    raise NewConnectionError(self, "private HTTP connection failed") from None
        self._captured = captured
        self._pool = HTTPConnectionPool(captured.address, captured.port, maxsize=1, block=True,
                                        retries=False)
        self._pool.ConnectionCls = Connection

    def get_connection_with_tls_context(self, request, verify, proxies=None, cert=None):
        if request.url != self._captured.url or request.method != "POST" or proxies:
            raise EgressBlockedError("private HTTP request changed")
        return self._pool

    def get_connection(self, url, proxies=None):
        if url != self._captured.url or proxies:
            raise EgressBlockedError("private HTTP request changed")
        return self._pool

    def add_headers(self, request, **kwargs):
        if request.url != self._captured.url or "Host" in request.headers:
            raise EgressBlockedError("private HTTP Host changed")
        request.headers["Host"] = self._captured.host_header

    def close(self):
        self._pool.close()
        super().close()


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
    _private_http_capture: object = None,
) -> requests.Response:
    if _private_http_capture is not None:
        capture = _private_http_capture
        try:
            if (type(capture) is not _PrivateHttpCapture or capture._authority is not _PRIVATE_HTTP_AUTHORITY
                    or _private_http_url(url)[0] != capture.url or allowed_private_hosts != capture.hosts
                    or method != "POST" or trust_environment is not False
                    or allow_redirects is not False or require_identity_encoding is not True
                    or any(name.lower() == "host" for name in (extra_headers or {}))):
                raise ValueError
        except (AttributeError, ValueError, TypeError):
            raise EgressBlockedError("private HTTP request changed") from None
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
                if _private_http_capture is not None:
                    session.mount("http://", _PrivateHttpAdapter(_private_http_capture))
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


def request_http2(
    method: str,
    url: str,
    *,
    api_key: str = "",
    json_body: Any = None,
    extra_headers: Optional[Dict[str, str]] = None,
    timeout: int = DEFAULT_TIMEOUT,
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
) -> httpx.Response:
    validate_egress_url(url)
    try:
        client = httpx.Client(http2=True, follow_redirects=False, timeout=timeout)
    except ImportError:
        raise ExternalHttpError("http2 transport is unavailable") from None
    headers: Dict[str, str] = {}
    if isinstance(api_key, str) and api_key.strip():
        headers["Authorization"] = f"Bearer {api_key}"
    if extra_headers:
        headers.update(extra_headers)
    try:
        with client:
            with client.stream(
                method.upper(),
                url,
                json=json_body,
                headers=headers,
            ) as resp:
                chunks = []
                total = 0
                for chunk in resp.iter_bytes(chunk_size=min(64 * 1024, max_response_bytes + 1)):
                    total += len(chunk)
                    if total > max_response_bytes:
                        raise ResponseTooLargeError(
                            f"Response exceeded {max_response_bytes} bytes"
                        )
                    chunks.append(chunk)
                resp._content = b"".join(chunks)
    except httpx.HTTPError as e:
        raise ServiceUnreachableError(f"Could not reach {url}: {e}") from e

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


async def async_post(
    url: str, *, body: bytes, headers: Dict[str, str],
    timeout_seconds: float = 60.0, max_response_bytes: int = 2 * 1024 * 1024,
):
    import asyncio
    import math
    from types import SimpleNamespace
    from urllib.parse import urlsplit

    from shared.streaming_egress import EgressLimits, FixedOriginHttpTransport, StreamingEgressError

    if (type(url) is not str or len(url) > 4096
            or type(body) is not bytes or len(body) > 1024 * 1024
            or type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds)
            or not 0 < timeout_seconds <= 60
            or type(max_response_bytes) is not int or not 0 < max_response_bytes <= 2 * 1024 * 1024):
        raise ExternalHttpError("async_request_invalid")
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.query or parsed.fragment
                or parsed.port not in (None, 443)):
            raise ValueError
    except ValueError:
        raise EgressBlockedError("async_endpoint_invalid") from None

    async def resolver(host, port):
        records = await asyncio.get_running_loop().getaddrinfo(
            host, port, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP,
        )
        if not records or len(records) > 8 or any(
                _is_private_address(record[4][0])
                or not ipaddress.ip_address(record[4][0]).is_global
                or not _decode_embedded_ipv4(ipaddress.ip_address(record[4][0])).is_global
                for record in records):
            raise EgressBlockedError("async_dns_denied")
        return records

    limits = EgressLimits(max_request_bytes=1024 * 1024, max_response_bytes=2 * 1024 * 1024,
                          total_timeout=timeout_seconds)
    transport = FixedOriginHttpTransport(f"https://{parsed.netloc}", limits=limits, _resolver=resolver)
    request = SimpleNamespace(path=parsed.path, headers=headers, body=body,
                              max_response_bytes=max_response_bytes, timeout_seconds=timeout_seconds)
    source = transport.post_stream(request)
    try:
        async for part in source:
            yield part
    except asyncio.CancelledError:
        raise
    except StreamingEgressError as error:
        failure = ExternalHttpError("async_egress_" + error.reason)
        if error.reason.startswith("http_status_"):
            failure.status_code = int(error.reason.rsplit("_", 1)[1])
        raise failure from None
    finally:
        await source.aclose()
