"""Checks strict-public async provider egress through the approved shared transport. Fixtures never connect to a provider."""

import asyncio
import socket
from types import SimpleNamespace

import pytest

from llm_config.tests.native_fixtures import native_offline as native_offline

from shared import external_http
from shared.tests.test_streaming_egress_065 import FakeReader, FakeWriter


def test_explicit_empty_private_allowlist_does_not_read_ambient_exemption(monkeypatch):
    monkeypatch.setenv("EXTERNAL_AGENT_ALLOWED_PRIVATE_HOSTS", "api.anthropic.com")
    monkeypatch.setattr(external_http, "_resolve_host_addresses", lambda _: ["127.0.0.1"])
    with pytest.raises(external_http.EgressBlockedError):
        external_http.validate_egress_url("https://api.anthropic.com/v1/messages", allowed_private_hosts=[])


@pytest.fixture
async def wire(monkeypatch):
    state = SimpleNamespace(body=b'{"ok":true}', status=200, headers=None, addresses=["93.184.216.34"], writer=None, blocker=None, calls=[])

    async def resolve(host, port, **_):
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, port)) for ip in state.addresses]

    async def connect(host, port, **kwargs):
        state.calls.append((host, port, kwargs))
        writer = FakeWriter(peer="93.184.216.34")
        state.writer = writer
        headers = state.headers or b"Content-Type: application/json\r\nContent-Length: " + str(len(state.body)).encode()
        raw = b"HTTP/1.1 " + str(state.status).encode() + b" Status\r\n" + headers + b"\r\n\r\n" + state.body
        return FakeReader(raw, blocker=state.blocker), writer

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
    monkeypatch.setattr(asyncio, "open_connection", connect)
    return state


async def collect(**kwargs):
    return b"".join([part async for part in external_http.async_post(
        "https://api.anthropic.com/v1/messages", body=b"{}",
        headers={"x-api-key": "owner-fixture", "content-type": "application/json"}, **kwargs)])


async def test_async_provider_post_pins_dns_tls_and_closes_without_ambient_headers(wire, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://private.proxy")
    monkeypatch.setenv("ANTHROPIC_CUSTOM_HEADERS", "x-private: ambient")
    assert await collect() == b'{"ok":true}'
    host, port, options = wire.calls[0]
    assert host == "93.184.216.34" and port == 443
    assert options["server_hostname"] == "api.anthropic.com"
    assert options["ssl"].check_hostname
    assert wire.writer.closed and wire.writer.waited_closed
    head = b"".join(wire.writer.writes)
    assert b"POST /v1/messages HTTP/1.1\r\n" in head
    assert b"x-api-key: owner-fixture\r\n" in head
    assert b"ambient" not in head and b"proxy" not in head


@pytest.mark.parametrize("addresses", [["127.0.0.1"], ["93.184.216.34", "10.0.0.2"], [], ["::ffff:127.0.0.1"]])
async def test_async_provider_post_refuses_all_private_or_empty_dns(wire, addresses, monkeypatch):
    monkeypatch.setenv("EXTERNAL_AGENT_ALLOWED_PRIVATE_HOSTS", "api.anthropic.com")
    wire.addresses = addresses
    with pytest.raises(external_http.ExternalHttpError):
        await collect()
    assert not wire.calls


async def test_chunked_provider_body_is_decoded_with_aggregate_bound(wire):
    wire.headers = b"Content-Type: text/event-stream\r\nTransfer-Encoding: chunked"
    wire.body = b"3\r\nabc\r\n2\r\nde\r\n0\r\n\r\n"
    assert await collect() == b"abcde"
    with pytest.raises(external_http.ExternalHttpError):
        await collect(max_response_bytes=4)
    assert wire.writer.closed


@pytest.mark.parametrize("headers,body", [
    (b"Content-Length: 4", b"abc"),
    (b"Content-Encoding: gzip\r\nContent-Length: 3", b"abc"),
    (b"Transfer-Encoding: chunked\r\nContent-Length: 3", b"abc"),
    (b"Transfer-Encoding: chunked", b"Z\r\nabc\r\n0\r\n\r\n"),
    (b"Transfer-Encoding: chunked", b"3\r\nabcXX0\r\n\r\n"),
    (b"Transfer-Encoding: chunked", b"0\r\nx-private: secret\r\n\r\n"),
    (b"Content-Type: application/json", b"abc"),
])
async def test_malformed_or_compressed_provider_framing_fails_closed(wire, headers, body):
    wire.headers, wire.body = headers, body
    with pytest.raises(external_http.ExternalHttpError):
        await collect()
    assert wire.writer.closed


@pytest.mark.parametrize("status", [301, 401, 403, 429, 500])
async def test_provider_status_errors_do_not_expose_body_or_retry(wire, status):
    wire.status, wire.body = status, b"OWNER PRIVATE UPSTREAM DETAIL"
    with pytest.raises(external_http.ExternalHttpError) as caught:
        await collect()
    assert "PRIVATE" not in str(caught.value)
    assert len(wire.calls) == 1 and wire.writer.closed


async def test_cancelling_provider_body_read_closes_connection(wire):
    wire.blocker = asyncio.Event()
    task = asyncio.create_task(collect())
    while wire.writer is None and not task.done():
        await asyncio.sleep(0)
    if task.done():
        await task
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert wire.writer.closed and wire.writer.waited_closed


@pytest.mark.parametrize("address", ["100.64.0.1", "198.18.0.1", "64:ff9b::7f00:1"])
async def test_strict_public_native_transport_denies_non_global_and_wrapped_targets(wire, address):
    wire.addresses = [address]
    with pytest.raises(external_http.ExternalHttpError):
        await collect()
    assert wire.calls == []


async def test_closing_async_body_consumer_closes_the_pinned_socket(wire):
    source = external_http.async_post("https://api.anthropic.com/v1/messages", body=b"{}", headers={})
    assert await anext(source) == wire.body
    await source.aclose()
    assert wire.writer.closed
