"""Bounded fixed-destination HTTP egress for the Feature 065 speech worker.

This module deliberately implements only HTTP/1.1 request/response transport.
The worker's authenticated pool-control WebSocket uses the separately approved
``websockets`` runtime. Caller-provided URLs, redirects, proxy discovery,
cookies, netrc credentials, compression, and connection reuse are absent by
construction.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
import ssl
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Any, Protocol
from urllib.parse import urlsplit


_HEADER_NAME = re.compile(rb"[!#$%&'*+\-.^_`|~0-9A-Za-z]+\Z")
_STATUS_LINE = re.compile(rb"HTTP/1\.[01] ([0-9]{3})(?: [\x20-\x7e]*)?\Z")
_FORBIDDEN_REQUEST_HEADERS = frozenset(
    {
        "accept-encoding",
        "connection",
        "content-length",
        "cookie",
        "cookie2",
        "expect",
        "host",
        "proxy-authorization",
        "proxy-connection",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)


class StreamingEgressError(RuntimeError):
    """Base for content-free failures safe for logs and control frames."""

    def __init__(self, reason: str, *, retryable: bool = False) -> None:
        self.reason = reason
        self.retryable = retryable
        super().__init__(f"streaming egress failed: {reason}")


class EgressConfigurationError(StreamingEgressError):
    """The fixed origin, request, or limits violate local policy."""


class EgressResolutionError(StreamingEgressError):
    """The fixed host could not be resolved to a bounded address set."""


class EgressConnectionError(StreamingEgressError):
    """TCP, TLS, or peer validation failed."""


class EgressTimeoutError(StreamingEgressError):
    """A bounded DNS, connection, write, read, or total deadline elapsed."""


class EgressLimitError(StreamingEgressError):
    """A request, header block, or response body exceeded its hard bound."""


class EgressProtocolError(StreamingEgressError):
    """The upstream response used ambiguous or unsupported HTTP framing."""


class HttpRequestLike(Protocol):
    """Structural request contract implemented by voice speech adapters."""

    path: str
    headers: Mapping[str, str]
    body: bytes
    max_response_bytes: int
    timeout_seconds: float


@dataclass(frozen=True, slots=True)
class StreamingHttpResponse:
    """Bounded HTTP response compatible with ``SpeechTransport`` consumers."""

    status: int
    headers: Mapping[str, str]
    body: bytes


@dataclass(frozen=True, slots=True)
class EgressLimits:
    """Hard transport limits; all timeouts are wall-clock seconds."""

    dns_timeout: float = 3.0
    connect_timeout: float = 5.0
    tls_handshake_timeout: float = 5.0
    write_timeout: float = 10.0
    read_timeout: float = 30.0
    total_timeout: float = 35.0
    close_timeout: float = 0.25
    max_request_bytes: int = 4 * 1024 * 1024
    max_response_bytes: int = 8 * 1024 * 1024
    max_header_bytes: int = 32 * 1024
    max_headers: int = 64
    max_dns_addresses: int = 8
    write_chunk_bytes: int = 64 * 1024

    def __post_init__(self) -> None:
        for name in (
            "dns_timeout",
            "connect_timeout",
            "tls_handshake_timeout",
            "write_timeout",
            "read_timeout",
            "total_timeout",
            "close_timeout",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise EgressConfigurationError("invalid_limits")
        for name in (
            "max_request_bytes",
            "max_response_bytes",
            "max_header_bytes",
            "max_headers",
            "max_dns_addresses",
            "write_chunk_bytes",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise EgressConfigurationError("invalid_limits")


@dataclass(frozen=True, slots=True)
class _Origin:
    host: str
    port: int
    tls: bool
    host_header: str
    base_path: str


@dataclass(frozen=True, slots=True)
class _Address:
    family: socket.AddressFamily
    ip: str


@dataclass(frozen=True, slots=True)
class _Request:
    method: str
    path: str
    headers: tuple[tuple[str, str], ...]
    body: bytes
    max_response_bytes: int
    timeout_seconds: float


Resolver = Callable[[str, int], Awaitable[Sequence[tuple[Any, ...]]]]
Connector = Callable[..., Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]]]


class FixedOriginHttpTransport:
    """Async GET/POST transport pinned to one operator-configured origin.

    ``_resolver`` and ``_connector`` are explicit test seams. Production callers
    leave them unset and therefore use the event loop's resolver and a direct
    numeric-IP ``asyncio.open_connection`` call.
    """

    def __init__(
        self,
        origin: str,
        *,
        limits: EgressLimits | None = None,
        allow_insecure_loopback_development: bool = False,
        ssl_context: ssl.SSLContext | None = None,
        _resolver: Resolver | None = None,
        _connector: Connector | None = None,
    ) -> None:
        self._limits = limits or EgressLimits()
        self._origin = _parse_origin(
            origin,
            allow_insecure_loopback_development=allow_insecure_loopback_development,
        )
        self._allow_insecure_loopback = allow_insecure_loopback_development
        self._resolver = _resolver or self._system_resolver
        self._connector = _connector or asyncio.open_connection
        self._ssl_context = _tls_context(ssl_context) if self._origin.tls else None

    async def post(self, request: HttpRequestLike) -> StreamingHttpResponse:
        """Send one bounded POST and return body bytes without exposing internals."""

        return await self._request("POST", request)

    async def get(self, request: HttpRequestLike) -> StreamingHttpResponse:
        """Send one bounded, body-free GET to the same fixed origin."""

        return await self._request("GET", request)

    async def _request(
        self,
        method: str,
        request: HttpRequestLike,
    ) -> StreamingHttpResponse:
        """Apply identical egress policy to the two approved HTTP methods."""

        try:
            validated = _validate_request(
                request,
                self._origin,
                self._limits,
                method=method,
            )
            total = min(validated.timeout_seconds, self._limits.total_timeout)
            async with asyncio.timeout(total):
                return await self._send_request(validated)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            raise EgressTimeoutError("total_timeout", retryable=True) from None
        except StreamingEgressError:
            raise
        except Exception:
            raise EgressConnectionError("transport_failed", retryable=True) from None

    async def _send_request(self, request: _Request) -> StreamingHttpResponse:
        addresses = await self._resolve()
        reader, writer = await self._connect(addresses)
        try:
            request_head = _request_head(self._origin, request)
            try:
                await _with_timeout(
                    _write_request(
                        writer,
                        request_head,
                        request.body,
                        chunk_bytes=self._limits.write_chunk_bytes,
                    ),
                    self._limits.write_timeout,
                    "write_timeout",
                )
            except StreamingEgressError:
                raise
            except Exception:
                raise EgressConnectionError("write_failed", retryable=True) from None
            try:
                return await _with_timeout(
                    _read_response(
                        reader,
                        max_header_bytes=self._limits.max_header_bytes,
                        max_headers=self._limits.max_headers,
                        max_body_bytes=request.max_response_bytes,
                    ),
                    self._limits.read_timeout,
                    "read_timeout",
                )
            except StreamingEgressError:
                raise
            except Exception:
                raise EgressConnectionError("read_failed", retryable=True) from None
        finally:
            await _close_writer(writer, self._limits.close_timeout)

    async def _system_resolver(self, host: str, port: int) -> Sequence[tuple[Any, ...]]:
        loop = asyncio.get_running_loop()
        return await loop.getaddrinfo(
            host,
            port,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP,
            flags=0,
        )

    async def _resolve(self) -> tuple[_Address, ...]:
        try:
            records = await _with_timeout(
                self._resolver(self._origin.host, self._origin.port),
                self._limits.dns_timeout,
                "dns_timeout",
            )
        except EgressTimeoutError:
            raise
        except Exception:
            raise EgressResolutionError("dns_failed", retryable=True) from None
        if not isinstance(records, Sequence) or isinstance(records, (str, bytes)):
            raise EgressResolutionError("dns_invalid")
        addresses: list[_Address] = []
        seen: set[tuple[socket.AddressFamily, str]] = set()
        for record in records:
            if not isinstance(record, tuple) or len(record) != 5:
                raise EgressResolutionError("dns_invalid")
            family, socktype, protocol, _canonical, sockaddr = record
            if family not in (socket.AF_INET, socket.AF_INET6):
                continue
            if socktype != socket.SOCK_STREAM or protocol not in (0, socket.IPPROTO_TCP):
                continue
            if not isinstance(sockaddr, tuple) or not sockaddr:
                raise EgressResolutionError("dns_invalid")
            try:
                address = ipaddress.ip_address(sockaddr[0])
            except (TypeError, ValueError):
                raise EgressResolutionError("dns_invalid") from None
            if address.is_unspecified or address.is_multicast or address.is_link_local:
                raise EgressResolutionError("dns_disallowed_address")
            normalized = address.compressed
            key = (family, normalized)
            if key not in seen:
                seen.add(key)
                addresses.append(_Address(family=family, ip=normalized))
            if len(addresses) > self._limits.max_dns_addresses:
                raise EgressResolutionError("dns_address_limit")
        if not addresses:
            raise EgressResolutionError("dns_empty", retryable=True)
        if not self._origin.tls and (
            not self._allow_insecure_loopback
            or any(not ipaddress.ip_address(item.ip).is_loopback for item in addresses)
        ):
            raise EgressConfigurationError("insecure_origin_not_loopback")
        return tuple(addresses)

    async def _connect(
        self, addresses: tuple[_Address, ...]
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        approved = {item.ip for item in addresses}

        async def attempts() -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
            tls_failed = False
            for address in addresses:
                try:
                    reader, writer = await self._connector(
                        address.ip,
                        self._origin.port,
                        family=address.family,
                        ssl=self._ssl_context,
                        server_hostname=(self._origin.host if self._origin.tls else None),
                        ssl_handshake_timeout=(
                            self._limits.tls_handshake_timeout
                            if self._origin.tls
                            else None
                        ),
                        limit=self._limits.max_header_bytes + 1,
                    )
                except asyncio.CancelledError:
                    raise
                except (ssl.CertificateError, ssl.SSLError):
                    tls_failed = True
                    continue
                except Exception:
                    continue
                try:
                    _validate_peer(writer, approved=approved, require_tls=self._origin.tls)
                except StreamingEgressError:
                    writer.close()
                    raise
                return reader, writer
            reason = "tls_failed" if tls_failed else "connect_failed"
            raise EgressConnectionError(reason, retryable=not tls_failed)

        return await _with_timeout(
            attempts(), self._limits.connect_timeout, "connect_timeout"
        )


def _parse_origin(
    value: str, *, allow_insecure_loopback_development: bool
) -> _Origin:
    if not isinstance(value, str) or not value or value != value.strip():
        raise EgressConfigurationError("invalid_origin")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise EgressConfigurationError("invalid_origin")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise EgressConfigurationError("invalid_origin") from None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise EgressConfigurationError("invalid_origin")
    try:
        host = parsed.hostname.encode("idna").decode("ascii").lower()
    except UnicodeError:
        raise EgressConfigurationError("invalid_origin") from None
    if not host or host.endswith(".") or "%" in host:
        raise EgressConfigurationError("invalid_origin")
    tls = parsed.scheme == "https"
    default_port = 443 if tls else 80
    port = port or default_port
    if not 1 <= port <= 65_535:
        raise EgressConfigurationError("invalid_origin")
    if not tls and (
        not allow_insecure_loopback_development or not _literal_loopback_host(host)
    ):
        raise EgressConfigurationError("tls_required")
    base_path = _validated_path(parsed.path or "/", allow_root=True)
    if base_path == "/":
        base_path = ""
    else:
        base_path = base_path.rstrip("/")
    host_display = f"[{host}]" if ":" in host else host
    host_header = host_display if port == default_port else f"{host_display}:{port}"
    return _Origin(
        host=host,
        port=port,
        tls=tls,
        host_header=host_header,
        base_path=base_path,
    )


def _literal_loopback_host(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _validated_path(value: str, *, allow_root: bool) -> str:
    if not isinstance(value, str) or not value.startswith("/"):
        raise EgressConfigurationError("invalid_path")
    if (
        "\\" in value
        or "?" in value
        or "#" in value
        or "%" in value
        or "//" in value
        or any(ord(character) < 0x20 or ord(character) > 0x7E for character in value)
    ):
        raise EgressConfigurationError("invalid_path")
    path = PurePosixPath(value)
    if any(part in {".", ".."} for part in path.parts):
        raise EgressConfigurationError("invalid_path")
    normalized = path.as_posix()
    if normalized != value.rstrip("/") and not (allow_root and value == "/"):
        raise EgressConfigurationError("invalid_path")
    if not allow_root and normalized == "/":
        raise EgressConfigurationError("invalid_path")
    return normalized


def _validate_request(
    value: HttpRequestLike,
    origin: _Origin,
    limits: EgressLimits,
    *,
    method: str,
) -> _Request:
    del origin  # The fixed origin is intentionally not caller-overridable.
    try:
        path = _validated_path(value.path, allow_root=False)
        headers_value = value.headers
        body = value.body
        max_response = value.max_response_bytes
        timeout = value.timeout_seconds
    except (AttributeError, TypeError):
        raise EgressConfigurationError("invalid_request") from None
    if not isinstance(headers_value, Mapping) or len(headers_value) > limits.max_headers:
        raise EgressConfigurationError("invalid_headers")
    headers: list[tuple[str, str]] = []
    header_bytes = 0
    names: set[str] = set()
    for raw_name, raw_value in headers_value.items():
        if not isinstance(raw_name, str) or not isinstance(raw_value, str):
            raise EgressConfigurationError("invalid_headers")
        try:
            name_bytes = raw_name.encode("ascii")
            value_bytes = raw_value.encode("ascii")
        except UnicodeEncodeError:
            raise EgressConfigurationError("invalid_headers") from None
        name = raw_name.lower()
        if (
            _HEADER_NAME.fullmatch(name_bytes) is None
            or name in names
            or name in _FORBIDDEN_REQUEST_HEADERS
            or any(byte < 0x20 or byte == 0x7F for byte in value_bytes)
        ):
            raise EgressConfigurationError("invalid_headers")
        names.add(name)
        header_bytes += len(name_bytes) + len(value_bytes) + 4
        headers.append((name, raw_value))
    if header_bytes > limits.max_header_bytes:
        raise EgressLimitError("request_headers_too_large")
    if not isinstance(body, bytes):
        raise EgressConfigurationError("invalid_request")
    if method not in {"GET", "POST"} or (method == "GET" and body):
        raise EgressConfigurationError("invalid_request_method")
    if len(body) > limits.max_request_bytes:
        raise EgressLimitError("request_too_large")
    if (
        isinstance(max_response, bool)
        or not isinstance(max_response, int)
        or max_response <= 0
        or max_response > limits.max_response_bytes
    ):
        raise EgressConfigurationError("invalid_response_limit")
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or timeout <= 0
    ):
        raise EgressConfigurationError("invalid_timeout")
    return _Request(
        method=method,
        path=path,
        headers=tuple(sorted(headers)),
        body=body,
        max_response_bytes=max_response,
        timeout_seconds=float(timeout),
    )


def _tls_context(context: ssl.SSLContext | None) -> ssl.SSLContext:
    selected = context or ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
    if selected.verify_mode != ssl.CERT_REQUIRED or not selected.check_hostname:
        raise EgressConfigurationError("invalid_tls_context")
    if selected.minimum_version < ssl.TLSVersion.TLSv1_2:
        selected.minimum_version = ssl.TLSVersion.TLSv1_2
    selected.set_alpn_protocols(["http/1.1"])
    return selected


def _request_head(origin: _Origin, request: _Request) -> bytes:
    target = f"{origin.base_path}{request.path}"
    lines = [
        f"{request.method} {target} HTTP/1.1",
        f"Host: {origin.host_header}",
        "Connection: close",
        f"Content-Length: {len(request.body)}",
    ]
    lines.extend(f"{name}: {value}" for name, value in request.headers)
    return ("\r\n".join(lines) + "\r\n\r\n").encode("ascii")


async def _write_request(
    writer: asyncio.StreamWriter,
    head: bytes,
    body: bytes,
    *,
    chunk_bytes: int,
) -> None:
    writer.write(head)
    await writer.drain()
    for offset in range(0, len(body), chunk_bytes):
        writer.write(body[offset : offset + chunk_bytes])
        await writer.drain()


async def _read_response(
    reader: asyncio.StreamReader,
    *,
    max_header_bytes: int,
    max_headers: int,
    max_body_bytes: int,
) -> StreamingHttpResponse:
    header, initial_body = await _read_header_block(reader, max_header_bytes)
    status, headers = _parse_headers(header, max_headers=max_headers)
    if 300 <= status <= 399:
        raise EgressProtocolError("redirect_refused")
    if 100 <= status <= 199:
        raise EgressProtocolError("interim_response_refused")
    if "transfer-encoding" in headers:
        raise EgressProtocolError("unsupported_framing")
    if headers.get("content-encoding", "identity").lower() != "identity":
        raise EgressProtocolError("content_encoding_refused")
    content_length = headers.get("content-length")
    if content_length is None or not content_length.isascii() or not content_length.isdigit():
        raise EgressProtocolError("ambiguous_framing")
    length = int(content_length)
    if length > max_body_bytes:
        raise EgressLimitError("response_too_large")
    if len(initial_body) > length:
        raise EgressProtocolError("ambiguous_framing")
    body = bytearray(initial_body)
    while len(body) < length:
        chunk = await reader.read(min(64 * 1024, length - len(body)))
        if not chunk:
            raise EgressProtocolError("truncated_response")
        body.extend(chunk)
    return StreamingHttpResponse(
        status=status,
        headers=MappingProxyType(dict(headers)),
        body=bytes(body),
    )


async def _read_header_block(
    reader: asyncio.StreamReader, max_header_bytes: int
) -> tuple[bytes, bytes]:
    buffer = bytearray()
    marker = b"\r\n\r\n"
    while True:
        location = buffer.find(marker)
        if location >= 0:
            end = location + len(marker)
            if end > max_header_bytes:
                raise EgressLimitError("response_headers_too_large")
            return bytes(buffer[:location]), bytes(buffer[end:])
        if len(buffer) >= max_header_bytes:
            raise EgressLimitError("response_headers_too_large")
        chunk = await reader.read(min(4096, max_header_bytes + 1 - len(buffer)))
        if not chunk:
            raise EgressProtocolError("malformed_response")
        buffer.extend(chunk)


def _parse_headers(data: bytes, *, max_headers: int) -> tuple[int, dict[str, str]]:
    lines = data.split(b"\r\n")
    if not lines:
        raise EgressProtocolError("malformed_response")
    status_match = _STATUS_LINE.fullmatch(lines[0])
    if status_match is None:
        raise EgressProtocolError("malformed_response")
    status = int(status_match.group(1))
    if not 100 <= status <= 599:
        raise EgressProtocolError("malformed_response")
    if len(lines) - 1 > max_headers:
        raise EgressLimitError("response_header_count")
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if not line or line[:1] in b" \t" or b":" not in line:
            raise EgressProtocolError("malformed_response")
        raw_name, raw_value = line.split(b":", 1)
        if _HEADER_NAME.fullmatch(raw_name) is None:
            raise EgressProtocolError("malformed_response")
        value = raw_value.strip(b" \t")
        if any(byte < 0x20 and byte != 0x09 or byte == 0x7F for byte in value):
            raise EgressProtocolError("malformed_response")
        name = raw_name.decode("ascii").lower()
        if name in headers:
            raise EgressProtocolError("ambiguous_framing")
        headers[name] = value.decode("latin-1")
    return status, headers


def _validate_peer(
    writer: asyncio.StreamWriter, *, approved: set[str], require_tls: bool
) -> None:
    peer = writer.get_extra_info("peername")
    if not isinstance(peer, tuple) or not peer:
        raise EgressConnectionError("peer_unavailable")
    try:
        peer_ip = ipaddress.ip_address(peer[0]).compressed
    except (TypeError, ValueError):
        raise EgressConnectionError("peer_invalid") from None
    if peer_ip not in approved:
        raise EgressConnectionError("peer_mismatch")
    if require_tls and writer.get_extra_info("ssl_object") is None:
        raise EgressConnectionError("tls_failed")


async def _with_timeout(
    operation: Awaitable[Any], timeout: float, reason: str
) -> Any:
    try:
        async with asyncio.timeout(timeout):
            return await operation
    except asyncio.CancelledError:
        raise
    except TimeoutError:
        raise EgressTimeoutError(reason, retryable=True) from None


async def _close_writer(writer: asyncio.StreamWriter, timeout: float) -> None:
    try:
        writer.close()
    except Exception:
        return
    try:
        async with asyncio.timeout(timeout):
            await writer.wait_closed()
    except asyncio.CancelledError:
        raise
    except Exception:
        return


__all__ = [
    "EgressConfigurationError",
    "EgressConnectionError",
    "EgressLimitError",
    "EgressLimits",
    "EgressProtocolError",
    "EgressResolutionError",
    "EgressTimeoutError",
    "FixedOriginHttpTransport",
    "StreamingEgressError",
    "StreamingHttpResponse",
]
