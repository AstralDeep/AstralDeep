"""Fixed-origin, DNS-pinned speech HTTP egress tests for Feature 065."""

from __future__ import annotations

import asyncio
import socket
import ssl
from dataclasses import dataclass
from typing import Any

import pytest

from shared.streaming_egress import (
    EgressConfigurationError,
    EgressConnectionError,
    EgressLimitError,
    EgressLimits,
    EgressProtocolError,
    EgressResolutionError,
    EgressTimeoutError,
    FixedOriginHttpTransport,
)


PUBLIC_IP = "203.0.113.10"
SECOND_IP = "203.0.113.11"


@dataclass(frozen=True)
class Request:
    path: str = "/audio/speech"
    headers: dict[str, str] | None = None
    body: bytes = b"request-body"
    max_response_bytes: int = 1024
    timeout_seconds: float = 1.0

    def __post_init__(self) -> None:
        if self.headers is None:
            object.__setattr__(
                self,
                "headers",
                {
                    "authorization": "Bearer speech-key",
                    "content-type": "application/json",
                },
            )


class FakeResolver:
    def __init__(
        self,
        addresses: list[str] | None = None,
        *,
        blocker: asyncio.Event | None = None,
        error: Exception | None = None,
    ) -> None:
        self.addresses = addresses or [PUBLIC_IP]
        self.blocker = blocker
        self.error = error
        self.calls: list[tuple[str, int]] = []

    async def __call__(self, host: str, port: int) -> list[tuple[Any, ...]]:
        self.calls.append((host, port))
        if self.blocker is not None:
            await self.blocker.wait()
        if self.error is not None:
            raise self.error
        records: list[tuple[Any, ...]] = []
        for address in self.addresses:
            family = socket.AF_INET6 if ":" in address else socket.AF_INET
            sockaddr = (address, port, 0, 0) if family == socket.AF_INET6 else (address, port)
            records.append((family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sockaddr))
        return records


class FakeReader:
    def __init__(self, data: bytes, *, blocker: asyncio.Event | None = None) -> None:
        self.data = bytearray(data)
        self.blocker = blocker

    async def read(self, count: int) -> bytes:
        if self.blocker is not None:
            await self.blocker.wait()
        if not self.data:
            return b""
        chunk = bytes(self.data[:count])
        del self.data[:count]
        return chunk


class FakeWriter:
    def __init__(
        self,
        *,
        peer: str = PUBLIC_IP,
        tls: bool = True,
        drain_blocker: asyncio.Event | None = None,
        drain_error: Exception | None = None,
    ) -> None:
        self.peer = peer
        self.tls = tls
        self.drain_blocker = drain_blocker
        self.drain_error = drain_error
        self.writes: list[bytes] = []
        self.closed = False
        self.waited_closed = False

    def write(self, data: bytes) -> None:
        self.writes.append(bytes(data))

    async def drain(self) -> None:
        if self.drain_blocker is not None:
            await self.drain_blocker.wait()
        if self.drain_error is not None:
            raise self.drain_error

    def get_extra_info(self, name: str) -> object | None:
        if name == "peername":
            return (self.peer, 443)
        if name == "ssl_object":
            return object() if self.tls else None
        return None

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        self.waited_closed = True


class FakeConnector:
    def __init__(
        self,
        reader: FakeReader,
        writer: FakeWriter,
        *,
        blocker: asyncio.Event | None = None,
        error: Exception | None = None,
    ) -> None:
        self.reader = reader
        self.writer = writer
        self.blocker = blocker
        self.error = error
        self.calls: list[tuple[str, int, dict[str, Any]]] = []

    async def __call__(
        self, host: str, port: int, **kwargs: Any
    ) -> tuple[FakeReader, FakeWriter]:
        self.calls.append((host, port, kwargs))
        if self.blocker is not None:
            await self.blocker.wait()
        if self.error is not None:
            raise self.error
        return self.reader, self.writer


def _response(
    body: bytes = b"ok",
    *,
    status: int = 200,
    headers: list[tuple[str, str]] | None = None,
) -> bytes:
    values = headers if headers is not None else [("Content-Length", str(len(body)))]
    head = [f"HTTP/1.1 {status} Result", *(f"{name}: {value}" for name, value in values)]
    return ("\r\n".join(head) + "\r\n\r\n").encode("ascii") + body


def _transport(
    response: bytes = _response(),
    *,
    origin: str = "https://speech.example:8443/v1",
    addresses: list[str] | None = None,
    reader: FakeReader | None = None,
    writer: FakeWriter | None = None,
    resolver: FakeResolver | None = None,
    connector: FakeConnector | None = None,
    limits: EgressLimits | None = None,
    allow_insecure: bool = False,
) -> tuple[FixedOriginHttpTransport, FakeResolver, FakeConnector, FakeWriter]:
    selected_reader = reader or FakeReader(response)
    selected_writer = writer or FakeWriter()
    selected_resolver = resolver or FakeResolver(addresses)
    selected_connector = connector or FakeConnector(selected_reader, selected_writer)
    transport = FixedOriginHttpTransport(
        origin,
        limits=limits,
        allow_insecure_loopback_development=allow_insecure,
        _resolver=selected_resolver,
        _connector=selected_connector,
    )
    return transport, selected_resolver, selected_connector, selected_writer


@pytest.mark.asyncio
async def test_post_pins_ip_preserves_host_sni_and_ignores_proxy_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy-secret.invalid:8080")
    monkeypatch.setenv("NO_PROXY", "*")
    transport, resolver, connector, writer = _transport(
        _response(b"wav", headers=[("Content-Length", "3"), ("Content-Type", "audio/wav")]),
        addresses=[PUBLIC_IP, SECOND_IP],
    )

    response = await transport.post(Request())

    assert response.status == 200
    assert response.body == b"wav"
    assert response.headers["content-type"] == "audio/wav"
    assert resolver.calls == [("speech.example", 8443)]
    assert len(connector.calls) == 1
    host, port, options = connector.calls[0]
    assert (host, port) == (PUBLIC_IP, 8443)
    assert options["family"] == socket.AF_INET
    assert options["server_hostname"] == "speech.example"
    assert options["ssl"].verify_mode == ssl.CERT_REQUIRED
    assert options["ssl"].check_hostname is True
    assert options["ssl_handshake_timeout"] == 5.0
    wire = b"".join(writer.writes)
    assert wire.startswith(b"POST /v1/audio/speech HTTP/1.1\r\n")
    assert b"Host: speech.example:8443\r\n" in wire
    assert b"Content-Length: 12\r\n" in wire
    assert b"authorization: Bearer speech-key\r\n" in wire
    assert wire.endswith(b"\r\n\r\nrequest-body")
    assert b"proxy-secret" not in wire
    assert writer.closed and writer.waited_closed
    with pytest.raises(TypeError):
        response.headers["new"] = "value"  # type: ignore[index]


@pytest.mark.asyncio
async def test_get_is_body_free_and_uses_the_same_fixed_origin_policy() -> None:
    payload = b'{"object":"list","data":[]}'
    transport, resolver, connector, writer = _transport(
        _response(payload),
    )
    request = Request(
        path="/models",
        headers={
            "accept": "application/json",
            "authorization": "Bearer speech-key",
        },
        body=b"",
    )

    response = await transport.get(request)

    assert response.body == payload
    assert resolver.calls == [("speech.example", 8443)]
    assert len(connector.calls) == 1
    wire = b"".join(writer.writes)
    assert wire.startswith(b"GET /v1/models HTTP/1.1\r\n")
    assert b"Host: speech.example:8443\r\n" in wire
    assert b"Content-Length: 0\r\n" in wire
    assert wire.endswith(b"\r\n\r\n")


@pytest.mark.asyncio
async def test_get_rejects_a_body_before_dns_or_io() -> None:
    transport, resolver, connector, _writer = _transport()

    with pytest.raises(EgressConfigurationError) as caught:
        await transport.get(Request(path="/models", body=b"not-allowed"))

    assert caught.value.reason == "invalid_request_method"
    assert resolver.calls == []
    assert connector.calls == []


@pytest.mark.asyncio
async def test_transport_is_structurally_compatible_with_speech_adapter() -> None:
    from voice_agent.speech_adapters import SpeachesBatchSTT

    payload = b'{"text":"hello","language":"en"}'
    transport, _, _, writer = _transport(
        _response(
            payload,
            headers=[
                ("Content-Length", str(len(payload))),
                ("Content-Type", "application/json"),
            ],
        )
    )
    adapter = SpeachesBatchSTT(transport=transport, api_key="speech-key")

    transcript = await adapter.transcribe_pcm16(b"\0\0" * 320)

    assert transcript.text == "hello"
    wire = b"".join(writer.writes)
    assert wire.startswith(b"POST /v1/audio/transcriptions HTTP/1.1\r\n")
    assert b"authorization: Bearer speech-key\r\n" in wire


@pytest.mark.asyncio
async def test_explicit_loopback_development_allows_plain_http_only_to_loopback() -> None:
    writer = FakeWriter(peer="127.0.0.1", tls=False)
    transport, _, connector, _ = _transport(
        origin="http://localhost:8080/v1",
        addresses=["127.0.0.1"],
        writer=writer,
        allow_insecure=True,
    )

    await transport.post(Request())

    options = connector.calls[0][2]
    assert options["ssl"] is None
    assert options["server_hostname"] is None
    assert b"Host: localhost:8080\r\n" in b"".join(writer.writes)

    non_loopback, _, _, _ = _transport(
        origin="http://localhost:8080",
        addresses=["10.0.0.8"],
        writer=FakeWriter(peer="10.0.0.8", tls=False),
        allow_insecure=True,
    )
    with pytest.raises(EgressConfigurationError) as caught:
        await non_loopback.post(Request())
    assert caught.value.reason == "insecure_origin_not_loopback"


@pytest.mark.asyncio
async def test_real_stdlib_loopback_connection_obeys_explicit_development_policy() -> None:
    observed: list[bytes] = []

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = await reader.readuntil(b"\r\n\r\n")
        length = 0
        for line in head.split(b"\r\n"):
            if line.lower().startswith(b"content-length:"):
                length = int(line.split(b":", 1)[1])
        body = await reader.readexactly(length)
        observed.append(head + body)
        writer.write(_response(b"real"))
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        transport = FixedOriginHttpTransport(
            f"http://127.0.0.1:{port}/v1",
            allow_insecure_loopback_development=True,
        )
        response = await transport.post(Request())
    finally:
        server.close()
        await server.wait_closed()

    assert response.body == b"real"
    assert observed and observed[0].startswith(
        b"POST /v1/audio/speech HTTP/1.1\r\n"
    )


@pytest.mark.parametrize(
    "origin",
    (
        "",
        " https://speech.example",
        "ftp://speech.example",
        "http://speech.example",
        "http://127.0.0.1",
        "https://user:secret@speech.example",
        "https://speech.example/path?query=1",
        "https://speech.example/path#fragment",
        "https://speech.example:99999",
        "https://speech.example/%2e%2e",
        "https://speech.example/a//b",
        "https://speech.example./v1",
    ),
)
def test_origin_is_one_strict_tls_destination(origin: str) -> None:
    with pytest.raises(EgressConfigurationError):
        FixedOriginHttpTransport(origin)


def test_insecure_loopback_requires_explicit_development_switch() -> None:
    with pytest.raises(EgressConfigurationError, match="tls_required"):
        FixedOriginHttpTransport("http://127.0.0.1")
    FixedOriginHttpTransport(
        "http://[::1]:8000/v1", allow_insecure_loopback_development=True
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("request_value", "reason"),
    (
        (Request(path="https://other.example/audio"), "invalid_path"),
        (Request(path="/../audio"), "invalid_path"),
        (Request(headers={"Host": "other.example"}), "invalid_headers"),
        (Request(headers={"Proxy-Authorization": "secret"}), "invalid_headers"),
        (Request(headers={"x-test": "line\r\ninjection"}), "invalid_headers"),
        (Request(body=b"x" * 33), "request_too_large"),
        (Request(max_response_bytes=33), "invalid_response_limit"),
        (Request(max_response_bytes=32, timeout_seconds=0), "invalid_timeout"),
    ),
)
async def test_request_cannot_override_destination_or_bounds(
    request_value: Request, reason: str
) -> None:
    transport, resolver, _, _ = _transport(
        limits=EgressLimits(max_request_bytes=32, max_response_bytes=32)
    )
    with pytest.raises((EgressConfigurationError, EgressLimitError)) as caught:
        await transport.post(request_value)
    assert caught.value.reason == reason
    assert resolver.calls == []


@pytest.mark.asyncio
async def test_unexpected_request_failure_is_still_typed_and_redacted() -> None:
    class ExplosiveRequest:
        @property
        def path(self) -> str:
            raise RuntimeError("speech-key caller detail")

    transport, resolver, _, _ = _transport()
    with pytest.raises(EgressConnectionError) as caught:
        await transport.post(ExplosiveRequest())  # type: ignore[arg-type]
    assert caught.value.reason == "transport_failed"
    assert "speech-key" not in str(caught.value)
    assert resolver.calls == []


@pytest.mark.asyncio
async def test_dns_timeout_failure_and_address_limit_are_typed_and_redacted() -> None:
    blocked = asyncio.Event()
    resolver = FakeResolver(blocker=blocked)
    transport, _, _, _ = _transport(
        resolver=resolver, limits=EgressLimits(dns_timeout=0.01)
    )
    with pytest.raises(EgressTimeoutError) as caught:
        await transport.post(Request())
    assert caught.value.reason == "dns_timeout"

    resolver = FakeResolver(error=socket.gaierror("speech-key private endpoint"))
    transport, _, _, _ = _transport(resolver=resolver)
    with pytest.raises(EgressResolutionError) as caught:
        await transport.post(Request())
    assert caught.value.reason == "dns_failed"
    assert caught.value.retryable
    assert "speech-key" not in str(caught.value)
    assert "speech.example" not in str(caught.value)

    resolver = FakeResolver([f"203.0.113.{number}" for number in range(1, 5)])
    transport, _, _, _ = _transport(
        resolver=resolver, limits=EgressLimits(max_dns_addresses=3)
    )
    with pytest.raises(EgressResolutionError) as caught:
        await transport.post(Request())
    assert caught.value.reason == "dns_address_limit"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("records", "reason"),
    (
        ("not-records", "dns_invalid"),
        ([(socket.AF_INET,)], "dns_invalid"),
        (
            [(socket.AF_UNSPEC, socket.SOCK_STREAM, 0, "", ("ignored", 443))],
            "dns_empty",
        ),
        (
            [(socket.AF_INET, socket.SOCK_DGRAM, 0, "", (PUBLIC_IP, 443))],
            "dns_empty",
        ),
        (
            [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ())],
            "dns_invalid",
        ),
        (
            [
                (
                    socket.AF_INET,
                    socket.SOCK_STREAM,
                    socket.IPPROTO_TCP,
                    "",
                    ("not-an-ip", 443),
                )
            ],
            "dns_invalid",
        ),
    ),
)
async def test_dns_record_shape_is_fail_closed(
    records: object, reason: str
) -> None:
    async def resolver(_host: str, _port: int) -> Any:
        return records

    transport, _, _, _ = _transport(resolver=resolver)  # type: ignore[arg-type]
    with pytest.raises(EgressResolutionError) as caught:
        await transport.post(Request())
    assert caught.value.reason == reason


@pytest.mark.asyncio
@pytest.mark.parametrize("address", ("0.0.0.0", "224.0.0.1", "169.254.1.1"))
async def test_dns_rejects_unsafe_address_classes(address: str) -> None:
    transport, _, _, _ = _transport(addresses=[address])
    with pytest.raises(EgressResolutionError) as caught:
        await transport.post(Request())
    assert caught.value.reason == "dns_disallowed_address"


@pytest.mark.asyncio
async def test_peer_must_match_resolution_and_https_must_have_tls() -> None:
    transport, _, _, writer = _transport(writer=FakeWriter(peer="198.51.100.9"))
    with pytest.raises(EgressConnectionError) as caught:
        await transport.post(Request())
    assert caught.value.reason == "peer_mismatch"
    assert writer.closed

    transport, _, _, _ = _transport(writer=FakeWriter(tls=False))
    with pytest.raises(EgressConnectionError) as caught:
        await transport.post(Request())
    assert caught.value.reason == "tls_failed"


@pytest.mark.asyncio
async def test_connector_retries_only_resolved_ips_without_leaking_failures() -> None:
    reader = FakeReader(_response())
    writer = FakeWriter(peer=SECOND_IP)

    class RetryConnector(FakeConnector):
        async def __call__(
            self, host: str, port: int, **kwargs: Any
        ) -> tuple[FakeReader, FakeWriter]:
            self.calls.append((host, port, kwargs))
            if host == PUBLIC_IP:
                raise OSError("provider-private-connect-detail")
            return self.reader, self.writer

    connector = RetryConnector(reader, writer)
    transport, _, _, _ = _transport(
        addresses=[PUBLIC_IP, SECOND_IP], connector=connector
    )
    await transport.post(Request())
    assert [call[0] for call in connector.calls] == [PUBLIC_IP, SECOND_IP]


@pytest.mark.asyncio
async def test_transport_maps_tcp_tls_write_and_read_failures_without_details() -> None:
    for error, reason, retryable in (
        (OSError("private tcp endpoint"), "connect_failed", True),
        (ssl.SSLError("private certificate detail"), "tls_failed", False),
    ):
        connector = FakeConnector(FakeReader(_response()), FakeWriter(), error=error)
        transport, _, _, _ = _transport(connector=connector)
        with pytest.raises(EgressConnectionError) as caught:
            await transport.post(Request())
        assert caught.value.reason == reason
        assert caught.value.retryable is retryable
        assert "private" not in str(caught.value)

    writer = FakeWriter(drain_error=OSError("speech-key write detail"))
    transport, _, _, _ = _transport(writer=writer)
    with pytest.raises(EgressConnectionError) as caught:
        await transport.post(Request())
    assert caught.value.reason == "write_failed"
    assert "speech-key" not in str(caught.value)

    class ErrorReader(FakeReader):
        async def read(self, count: int) -> bytes:
            del count
            raise OSError("speech-key read detail")

    transport, _, _, _ = _transport(reader=ErrorReader(b""))
    with pytest.raises(EgressConnectionError) as caught:
        await transport.post(Request())
    assert caught.value.reason == "read_failed"
    assert "speech-key" not in str(caught.value)


@pytest.mark.asyncio
async def test_default_resolver_is_bounded_and_peer_metadata_is_mandatory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = asyncio.get_running_loop()
    calls: list[tuple[str, int, dict[str, Any]]] = []

    async def getaddrinfo(host: str, port: int, **kwargs: Any) -> list[tuple[Any, ...]]:
        calls.append((host, port, kwargs))
        return [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                (PUBLIC_IP, port),
            )
        ]

    monkeypatch.setattr(loop, "getaddrinfo", getaddrinfo)
    connector = FakeConnector(FakeReader(_response()), FakeWriter())
    transport = FixedOriginHttpTransport(
        "https://speech.example", _connector=connector
    )
    await transport.post(Request())
    assert calls == [
        (
            "speech.example",
            443,
            {
                "family": socket.AF_UNSPEC,
                "type": socket.SOCK_STREAM,
                "proto": socket.IPPROTO_TCP,
                "flags": 0,
            },
        )
    ]

    class MissingPeerWriter(FakeWriter):
        def get_extra_info(self, name: str) -> object | None:
            if name == "peername":
                return None
            return super().get_extra_info(name)

    transport, _, _, _ = _transport(writer=MissingPeerWriter())
    with pytest.raises(EgressConnectionError) as caught:
        await transport.post(Request())
    assert caught.value.reason == "peer_unavailable"

    transport, _, _, _ = _transport(writer=FakeWriter(peer="not-an-ip"))
    with pytest.raises(EgressConnectionError) as caught:
        await transport.post(Request())
    assert caught.value.reason == "peer_invalid"


@pytest.mark.asyncio
async def test_connect_write_read_and_total_timeouts_are_distinct() -> None:
    blocker = asyncio.Event()
    connector = FakeConnector(FakeReader(_response()), FakeWriter(), blocker=blocker)
    transport, _, _, _ = _transport(
        connector=connector, limits=EgressLimits(connect_timeout=0.01)
    )
    with pytest.raises(EgressTimeoutError) as caught:
        await transport.post(Request())
    assert caught.value.reason == "connect_timeout"

    writer = FakeWriter(drain_blocker=blocker)
    transport, _, _, _ = _transport(
        writer=writer, limits=EgressLimits(write_timeout=0.01)
    )
    with pytest.raises(EgressTimeoutError) as caught:
        await transport.post(Request())
    assert caught.value.reason == "write_timeout"
    assert writer.closed

    reader = FakeReader(b"", blocker=blocker)
    transport, _, _, writer = _transport(
        reader=reader, limits=EgressLimits(read_timeout=0.01)
    )
    with pytest.raises(EgressTimeoutError) as caught:
        await transport.post(Request())
    assert caught.value.reason == "read_timeout"
    assert writer.closed

    connector = FakeConnector(FakeReader(_response()), FakeWriter(), blocker=blocker)
    transport, _, _, _ = _transport(
        connector=connector, limits=EgressLimits(connect_timeout=1, total_timeout=1)
    )
    with pytest.raises(EgressTimeoutError) as caught:
        await transport.post(Request(timeout_seconds=0.01))
    assert caught.value.reason == "total_timeout"


@pytest.mark.asyncio
async def test_cancellation_propagates_and_is_not_relabelled() -> None:
    blocker = asyncio.Event()
    connector = FakeConnector(FakeReader(_response()), FakeWriter(), blocker=blocker)
    transport, _, _, _ = _transport(connector=connector)
    task = asyncio.create_task(transport.post(Request(timeout_seconds=10)))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "error_type", "reason"),
    (
        (_response(b"", status=302), EgressProtocolError, "redirect_refused"),
        (
            _response(b"", headers=[("Transfer-Encoding", "chunked")]),
            EgressProtocolError,
            "unsupported_framing",
        ),
        (
            _response(
                b"", headers=[("Content-Length", "0"), ("Transfer-Encoding", "chunked")]
            ),
            EgressProtocolError,
            "unsupported_framing",
        ),
        (
            b"HTTP/1.1 200 OK\r\nX-Test: value\r\n\r\n",
            EgressProtocolError,
            "ambiguous_framing",
        ),
        (
            _response(b"", headers=[("Content-Length", "0"), ("Content-Length", "0")]),
            EgressProtocolError,
            "ambiguous_framing",
        ),
        (
            _response(b"", headers=[("Content-Length", "+0")]),
            EgressProtocolError,
            "ambiguous_framing",
        ),
        (
            _response(
                b"", headers=[("Content-Length", "0"), ("Content-Encoding", "gzip")]
            ),
            EgressProtocolError,
            "content_encoding_refused",
        ),
        (
            b"HTTP/1.1 100 Continue\r\nContent-Length: 0\r\n\r\n",
            EgressProtocolError,
            "interim_response_refused",
        ),
        (
            b"NOT-HTTP\r\nContent-Length: 0\r\n\r\n",
            EgressProtocolError,
            "malformed_response",
        ),
        (
            b"HTTP/1.1 999 Invalid\r\nContent-Length: 0\r\n\r\n",
            EgressProtocolError,
            "malformed_response",
        ),
        (
            b"HTTP/1.1 200 OK\r\n folded: value\r\nContent-Length: 0\r\n\r\n",
            EgressProtocolError,
            "malformed_response",
        ),
        (
            b"HTTP/1.1 200 OK\r\nContent-Length: 4\r\n\r\nxx",
            EgressProtocolError,
            "truncated_response",
        ),
        (
            b"HTTP/1.1 200 OK\r\nContent-Length: 1\r\n\r\nxx",
            EgressProtocolError,
            "ambiguous_framing",
        ),
        (
            b"HTTP/1.1 200 OK\r\nContent-Length: 2048\r\n\r\n",
            EgressLimitError,
            "response_too_large",
        ),
    ),
)
async def test_response_framing_redirects_and_body_bounds_fail_closed(
    response: bytes, error_type: type[Exception], reason: str
) -> None:
    transport, _, _, writer = _transport(response)
    with pytest.raises(error_type) as caught:
        await transport.post(Request())
    assert caught.value.reason == reason  # type: ignore[attr-defined]
    rendered = str(caught.value)
    assert "speech-key" not in rendered
    assert response[-20:].decode("latin-1", errors="ignore") not in rendered
    assert writer.closed


@pytest.mark.asyncio
async def test_response_header_byte_and_count_bounds_are_enforced() -> None:
    response = _response(b"", headers=[("X-Large", "x" * 200), ("Content-Length", "0")])
    transport, _, _, _ = _transport(
        response, limits=EgressLimits(max_header_bytes=64)
    )
    with pytest.raises(EgressLimitError) as caught:
        await transport.post(Request(headers={}))
    assert caught.value.reason == "response_headers_too_large"

    response = _response(
        b"", headers=[("X-One", "1"), ("X-Two", "2"), ("Content-Length", "0")]
    )
    transport, _, _, _ = _transport(response, limits=EgressLimits(max_headers=2))
    with pytest.raises(EgressLimitError) as caught:
        await transport.post(Request())
    assert caught.value.reason == "response_header_count"


def test_limits_and_tls_context_fail_closed() -> None:
    with pytest.raises(EgressConfigurationError, match="invalid_limits"):
        EgressLimits(connect_timeout=0)
    with pytest.raises(EgressConfigurationError, match="invalid_limits"):
        EgressLimits(max_headers=True)  # type: ignore[arg-type]

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    with pytest.raises(EgressConfigurationError, match="invalid_tls_context"):
        FixedOriginHttpTransport("https://speech.example", ssl_context=context)
