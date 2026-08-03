"""Strict in-memory OpenAI-compatible speech service for worker tests.

The helper binds only to an ephemeral loopback port, retains request bytes only
for the lifetime of the test, and has no dependency on the product backend.
"""

from __future__ import annotations

import asyncio
import io
import json
import wave
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Mapping


@dataclass(frozen=True, slots=True)
class RecordedRequest:
    method: str
    target: str
    headers: Mapping[str, str]
    body: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class FakeResponse:
    status: int
    body: bytes = field(repr=False)
    content_type: str
    headers: Mapping[str, str] = field(default_factory=dict)
    delay_seconds: float = 0.0


class StrictFakeSpeechService:
    """One bounded loopback-only speech origin with explicit response queues."""

    def __init__(self, *, api_key: str = "speech-test-key") -> None:
        self.api_key = api_key
        self.requests: list[RecordedRequest] = []
        self._responses: defaultdict[str, deque[FakeResponse]] = defaultdict(deque)
        self._server: asyncio.AbstractServer | None = None
        self.origin = ""

    async def __aenter__(self) -> StrictFakeSpeechService:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        socket = self._server.sockets[0]
        port = int(socket.getsockname()[1])
        self.origin = f"http://127.0.0.1:{port}/v1"
        return self

    async def __aexit__(self, *_args: object) -> None:
        server = self._server
        self._server = None
        if server is not None:
            server.close()
            await server.wait_closed()
        for request in self.requests:
            del request
        self.requests.clear()
        self._responses.clear()

    def enqueue(self, path: str, response: FakeResponse) -> None:
        if path not in {"/v1/models", "/v1/audio/transcriptions", "/v1/audio/speech"}:
            raise ValueError("unsupported_fake_speech_path")
        self._responses[path].append(response)

    async def _handle(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=1.0)
            if len(head) > 32 * 1024:
                await self._write(writer, FakeResponse(431, b"", "text/plain"))
                return
            request_line, *header_lines = head[:-4].split(b"\r\n")
            method_raw, target_raw, version = request_line.split(b" ", 2)
            if version != b"HTTP/1.1":
                await self._write(writer, FakeResponse(400, b"", "text/plain"))
                return
            headers: dict[str, str] = {}
            for line in header_lines:
                name, value = line.split(b":", 1)
                normalized = name.decode("ascii").lower()
                if normalized in headers:
                    await self._write(writer, FakeResponse(400, b"", "text/plain"))
                    return
                headers[normalized] = value.strip().decode("ascii")
            length_text = headers.get("content-length", "")
            if not length_text.isdigit() or int(length_text) > 4 * 1024 * 1024:
                await self._write(writer, FakeResponse(413, b"", "text/plain"))
                return
            body = await asyncio.wait_for(
                reader.readexactly(int(length_text)),
                timeout=1.0,
            )
            request = RecordedRequest(
                method=method_raw.decode("ascii"),
                target=target_raw.decode("ascii"),
                headers=headers,
                body=body,
            )
            self.requests.append(request)
            if headers.get("authorization") != f"Bearer {self.api_key}":
                response = FakeResponse(
                    401,
                    b'{"error":"private fake body"}',
                    "application/json",
                )
            elif self._responses[request.target]:
                response = self._responses[request.target].popleft()
            else:
                response = self._default_response(request)
            if response.delay_seconds:
                await asyncio.sleep(response.delay_seconds)
            await self._write(writer, response)
        except (asyncio.IncompleteReadError, TimeoutError, ValueError):
            try:
                await self._write(writer, FakeResponse(400, b"", "text/plain"))
            except Exception:
                pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    def _default_response(self, request: RecordedRequest) -> FakeResponse:
        if request.method == "GET" and request.target == "/v1/models":
            body = json.dumps(
                {
                    "object": "list",
                    "data": [
                        {
                            "id": "Systran/faster-whisper-large-v3",
                            "task": "automatic-speech-recognition",
                        },
                        {
                            "id": "speaches-ai/Kokoro-82M-v1.0-ONNX",
                            "task": "text-to-speech",
                            "sample_rate": 24_000,
                            "voices": [{"id": "af_heart", "name": "af_heart"}],
                        },
                    ],
                },
                separators=(",", ":"),
            ).encode()
            return FakeResponse(200, body, "application/json")
        if request.method == "POST" and request.target == "/v1/audio/transcriptions":
            return FakeResponse(
                200,
                b'{"text":"synthetic request","language":"en"}',
                "application/json",
            )
        if request.method == "POST" and request.target == "/v1/audio/speech":
            return FakeResponse(200, _wav(samples=2_400), "audio/wav")
        return FakeResponse(404, b'{"error":"not found"}', "application/json")

    @staticmethod
    async def _write(writer: asyncio.StreamWriter, response: FakeResponse) -> None:
        reasons = {200: "OK", 302: "Found", 400: "Bad Request", 401: "Unauthorized", 404: "Not Found", 413: "Content Too Large", 429: "Too Many Requests", 431: "Request Header Fields Too Large", 500: "Internal Server Error", 503: "Service Unavailable"}
        headers = {
            "Connection": "close",
            "Content-Length": str(len(response.body)),
            "Content-Type": response.content_type,
            **dict(response.headers),
        }
        head = [
            f"HTTP/1.1 {response.status} {reasons.get(response.status, 'Result')}",
            *(f"{name}: {value}" for name, value in headers.items()),
            "",
            "",
        ]
        writer.write("\r\n".join(head).encode("ascii") + response.body)
        await writer.drain()


def _wav(*, samples: int, sample_rate: int = 24_000) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(sample_rate)
        stream.writeframes(b"\0\0" * samples)
    return output.getvalue()
