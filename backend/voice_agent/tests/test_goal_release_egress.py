"""Exercise streaming egress framing and failure privacy in the isolated worker."""

from __future__ import annotations

import asyncio
import socket
from dataclasses import dataclass
from typing import Any

import pytest

from voice_agent.streaming_egress import (
    EgressLimitError,
    EgressProtocolError,
    EgressTimeoutError,
    FixedOriginHttpTransport,
)


@dataclass
class Request:
    path: str = "/audio/speech"
    headers: dict[str, str] | None = None
    body: bytes = b"private-prompt"
    max_response_bytes: int = 32
    timeout_seconds: float = 1.0

    def __post_init__(self) -> None:
        if self.headers is None:
            self.headers = {"authorization": "Bearer private-token"}


class Reader:
    def __init__(self, payload: bytes, blocker: asyncio.Event | None = None) -> None:
        self.payload = bytearray(payload)
        self.blocker = blocker

    async def read(self, count: int) -> bytes:
        if self.blocker is not None:
            await self.blocker.wait()
        result = bytes(self.payload[:count])
        del self.payload[:count]
        return result


class Writer:
    def __init__(self, peer: str = "203.0.113.10") -> None:
        self.peer = peer
        self.data: list[bytes] = []
        self.closed = False

    def write(self, value: bytes) -> None:
        self.data.append(value)

    async def drain(self) -> None:
        return None

    def get_extra_info(self, name: str) -> object | None:
        if name == "peername":
            return (self.peer, 443)
        if name == "ssl_object":
            return object()
        return None

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


class Resolver:
    async def __call__(self, host: str, port: int) -> list[tuple[Any, ...]]:
        return [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("203.0.113.10", port),
            )
        ]


class Connector:
    def __init__(self, reader: Reader, writer: Writer) -> None:
        self.reader = reader
        self.writer = writer

    async def __call__(
        self, host: str, port: int, **kwargs: Any
    ) -> tuple[Reader, Writer]:
        return self.reader, self.writer


def transport(
    response: bytes,
    *,
    reader: Reader | None = None,
    writer: Writer | None = None,
) -> tuple[FixedOriginHttpTransport, Writer]:
    selected_writer = writer or Writer()
    selected_reader = reader or Reader(response)
    connector = Connector(selected_reader, selected_writer)
    client = FixedOriginHttpTransport(
        "https://speech.example/v1",
        _resolver=Resolver(),
        _connector=connector,
    )
    return client, selected_writer


@pytest.mark.asyncio
async def test_post_stream_yields_chunked_audio_and_closes_connection() -> None:
    client, writer = transport(
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
        b"3\r\nabc\r\n2\r\nde\r\n0\r\n\r\n"
    )

    audio = [part async for part in client.post_stream(Request())]

    assert audio == [b"abc", b"de"]
    assert writer.closed
    assert b"private-token" in b"".join(writer.data)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "reason"),
    [
        (b"HTTP/1.1 302 Found\r\nContent-Length: 0\r\n\r\n", "http_status_302"),
        (
            b"HTTP/1.1 200 OK\r\nContent-Encoding: gzip\r\nContent-Length: 0\r\n\r\n",
            "content_encoding_refused",
        ),
        (
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
            b"3\r\nabc\r\n0\r\nX-Private: value\r\n\r\n",
            "chunk_trailer_refused",
        ),
    ],
)
async def test_post_stream_rejects_unsafe_responses_without_echoing_credentials(
    response: bytes, reason: str
) -> None:
    client, writer = transport(response)

    with pytest.raises(EgressProtocolError, match=reason) as caught:
        _ = [part async for part in client.post_stream(Request())]

    assert "private-token" not in str(caught.value)
    assert writer.closed


@pytest.mark.asyncio
async def test_post_stream_enforces_audio_bound_and_total_timeout() -> None:
    too_large, _ = transport(
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
        b"3\r\nabc\r\n0\r\n\r\n"
    )
    with pytest.raises(EgressLimitError, match="response_too_large"):
        _ = [part async for part in too_large.post_stream(Request(max_response_bytes=2))]

    blocked_reader = Reader(b"", blocker=asyncio.Event())
    timed_out, writer = transport(b"", reader=blocked_reader)
    with pytest.raises(EgressTimeoutError, match="total_timeout"):
        _ = [part async for part in timed_out.post_stream(Request(timeout_seconds=0.01))]
    assert writer.closed
