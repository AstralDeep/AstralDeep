"""Checks that remote MCP origins use the approved external HTTP DNS and TLS boundary.
The resolver is faked so these tests make no outbound network connections.
"""

from __future__ import annotations

import asyncio
import socket
import ssl

import pytest

from shared.external_http import EgressBlockedError, fixed_origin_transport_for_external_url
from shared.streaming_egress import EgressResolutionError


@pytest.mark.parametrize("url", [
    "http://mcp.example/mcp", "https://mcp.example:8443/mcp",
    "https://user:pass@mcp.example/mcp", "https://mcp.example/mcp#fragment",
])
def test_external_origin_refuses_insecure_or_embedded_authority(url):
    with pytest.raises(EgressBlockedError):
        fixed_origin_transport_for_external_url(url)


@pytest.mark.asyncio
async def test_external_origin_rejects_mixed_public_and_private_dns(monkeypatch):
    loop = asyncio.get_running_loop()

    async def answers(_host, _port, **_kwargs):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("8.8.8.8", 443)),
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", 443)),
        ]

    monkeypatch.setattr(loop, "getaddrinfo", answers)
    transport, path = fixed_origin_transport_for_external_url("https://mcp.example/mcp")
    assert path == "/mcp"
    with pytest.raises(EgressResolutionError, match="dns_failed"):
        await transport._resolve()


@pytest.mark.asyncio
async def test_external_origin_pins_public_dns_and_verifies_tls(monkeypatch):
    loop = asyncio.get_running_loop()

    async def answers(_host, _port, **_kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("8.8.8.8", 443))]

    monkeypatch.setattr(loop, "getaddrinfo", answers)
    transport, path = fixed_origin_transport_for_external_url("https://mcp.example/mcp?version=1")
    resolved = await transport._resolve()
    assert path == "/mcp?version=1"
    assert resolved[0].ip == "8.8.8.8"
    assert transport._ssl_context.verify_mode is ssl.CERT_REQUIRED
    assert transport._ssl_context.check_hostname is True
