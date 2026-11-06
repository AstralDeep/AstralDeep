"""Checks that remote MCP SSE parsers decode UTF-8 across stream chunks."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from orchestrator import remote_mcp_connectors as connectors


async def _chunks(*bodies: bytes):
    for body in bodies:
        yield SimpleNamespace(body=body)


@pytest.mark.asyncio
async def test_sse_session_decodes_split_multibyte_character():
    session = connectors._SSESession({"url": "https://mcp.example"}, None, "/sse")
    session.events = _chunks(b"data: caf\xc3", b"\xa9\n\n").__aiter__()

    assert await session._next_event() == {"data": "café"}


@pytest.mark.asyncio
async def test_sse_session_normalizes_crlf_across_chunks():
    session = connectors._SSESession({"url": "https://mcp.example"}, None, "/sse")
    session.events = _chunks(
        b"event: endpoint\r", b"\ndata: /messages\r", b"\n\r", b"\n"
    ).__aiter__()

    assert await session._next_event() == {"event": "endpoint", "data": "/messages"}


@pytest.mark.asyncio
async def test_sse_session_retains_event_size_limit():
    session = connectors._SSESession({"url": "https://mcp.example"}, None, "/sse")
    session.events = _chunks(b"data:" + b"x" * 65532).__aiter__()

    with pytest.raises(
        connectors.RemoteMCPError, match="event exceeded the allowed size"
    ):
        await session._next_event()


@pytest.mark.asyncio
async def test_first_sse_response_decodes_split_multibyte_character():
    body = b'data: {"id":7,"result":{"label":"caf\xc3\xa9"}}\n\n'
    split = body.index(b"\xc3") + 1

    response = await connectors._first_sse_response(
        _chunks(body[:split], body[split:]), 7
    )

    assert response == {"id": 7, "result": {"label": "café"}}


@pytest.mark.asyncio
@pytest.mark.parametrize("parser", ["session", "first_response"])
async def test_sse_parsers_reject_invalid_utf8(parser):
    if parser == "session":
        session = connectors._SSESession({"url": "https://mcp.example"}, None, "/sse")
        session.events = _chunks(b"data: \xff\n\n").__aiter__()
        operation = session._next_event()
    else:
        operation = connectors._first_sse_response(_chunks(b"data: \xff\n\n"), 7)

    with pytest.raises(UnicodeDecodeError):
        await operation


@pytest.mark.asyncio
async def test_sse_session_rejects_incomplete_utf8_at_stream_end():
    session = connectors._SSESession({"url": "https://mcp.example"}, None, "/sse")
    session.events = _chunks(b"data: caf\xc3").__aiter__()

    with pytest.raises(UnicodeDecodeError):
        await session._next_event()
