"""Exercises strict public-address resolution on the approved external HTTP transport factory."""

from __future__ import annotations

import asyncio
import socket

import pytest

from shared.external_http import (
    EgressBlockedError,
    fixed_origin_transport_for_external_url,
)
from shared.streaming_egress import EgressResolutionError


def _record(address: str):
    return (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, 443))


@pytest.mark.asyncio
async def test_external_transport_rejects_private_address_in_any_dns_answer(monkeypatch):
    async def resolve(_loop, _host, _port, **_kwargs):
        return [_record("8.8.8.8"), _record("127.0.0.1")]

    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", resolve)
    transport, path = fixed_origin_transport_for_external_url("https://mcp.example/mcp")

    assert path == "/mcp"
    with pytest.raises(EgressResolutionError):
        await transport._resolve()


@pytest.mark.asyncio
async def test_external_transport_accepts_public_dns_answers(monkeypatch):
    async def resolve(_loop, _host, _port, **_kwargs):
        return [_record("8.8.8.8"), _record("1.1.1.1")]

    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", resolve)
    transport, _path = fixed_origin_transport_for_external_url("https://mcp.example/mcp")

    addresses = await transport._resolve()

    assert [item.ip for item in addresses] == ["8.8.8.8", "1.1.1.1"]


def test_external_transport_rejects_non_https_and_redirect_targets():
    for url in ("http://mcp.example/mcp", "https://user:pass@mcp.example/mcp",
                "https://mcp.example:8443/mcp", "https://mcp.example/mcp#fragment"):
        with pytest.raises(EgressBlockedError):
            fixed_origin_transport_for_external_url(url)
