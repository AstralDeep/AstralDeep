# A minimal forward proxy that reaches only public web servers on ports 80 and 443.
# Each target is resolved and checked, then connected by the checked address so DNS rebinding cannot slip through.
from __future__ import annotations

import asyncio
import logging
import re
import socket
from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

from egress.app import Forbidden, resolve_public

log = logging.getLogger("egress.proxy")

MAX_TUNNEL_BYTES = 16_000_000
IDLE_TIMEOUT = 60.0
CONNECT_TIMEOUT = 15.0
_REQUEST_LINE = re.compile(rb"^([A-Z]+) (\S+) HTTP/1\.[01]$")

Connector = Callable[[str, int], Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]]]


async def _default_connect(ip: str, port: int):
    return await asyncio.wait_for(asyncio.open_connection(ip, port), CONNECT_TIMEOUT)


async def _reply(writer: asyncio.StreamWriter, status: int, reason: str) -> None:
    body = f"{reason}\n".encode()
    head = (f"HTTP/1.1 {status} {reason}\r\nContent-Type: text/plain\r\nContent-Length: {len(body)}\r\n"
            "Connection: close\r\n\r\n")
    writer.write(head.encode() + body)
    await writer.drain()
    writer.close()


async def _pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter, budget: list[int]) -> None:
    try:
        while True:
            chunk = await asyncio.wait_for(src.read(65536), IDLE_TIMEOUT)
            if not chunk:
                break
            budget[0] -= len(chunk)
            if budget[0] < 0:
                log.warning("tunnel byte cap reached; closing")
                break
            dst.write(chunk)
            await dst.drain()
    except (TimeoutError, ConnectionError, asyncio.IncompleteReadError):
        pass
    finally:
        try:
            dst.close()
        except Exception:  # pragma: no cover
            pass


async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, *,
                 connect: Connector = _default_connect, resolve=socket.getaddrinfo) -> None:
    peer = writer.get_extra_info("peername")
    try:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 15.0)
    except (TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
        writer.close()
        return
    lines = head.split(b"\r\n")
    m = _REQUEST_LINE.match(lines[0])
    if not m:
        await _reply(writer, 400, "Bad request")
        return
    method, target = m.group(1).decode(), m.group(2).decode()

    if method == "CONNECT":
        host, _, port_s = target.rpartition(":")
        host = host.strip("[]")
        try:
            port = int(port_s)
            if port != 443:
                raise Forbidden("only port 443 may be tunneled")
            ip = resolve_public(host, port, resolve=resolve)
        except (ValueError, Forbidden) as exc:
            log.warning("CONNECT %s refused from %s: %s", target, peer, exc)
            await _reply(writer, 403, f"Refused: {exc}")
            return
        try:
            up_r, up_w = await connect(ip, port)
        except Exception as exc:
            await _reply(writer, 502, f"Cannot connect: {type(exc).__name__}")
            return
        log.info("CONNECT %s -> %s", target, ip)
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()
        budget = [MAX_TUNNEL_BYTES]
        await asyncio.gather(_pipe(reader, up_w, budget), _pipe(up_r, writer, budget))
        return

    if method in ("GET", "HEAD") and target.startswith("http://"):
        parts = urlsplit(target)
        host = parts.hostname or ""
        try:
            if parts.port not in (None, 80):
                raise Forbidden("only port 80 for plain http")
            ip = resolve_public(host, 80, resolve=resolve)
        except Forbidden as exc:
            log.warning("%s %s refused from %s: %s", method, target, peer, exc)
            await _reply(writer, 403, f"Refused: {exc}")
            return
        try:
            up_r, up_w = await connect(ip, 80)
        except Exception as exc:
            await _reply(writer, 502, f"Cannot connect: {type(exc).__name__}")
            return
        path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        headers = [line for line in lines[1:] if line and not line.lower().startswith((b"proxy-", b"connection:"))]
        up_w.write(f"{method} {path} HTTP/1.1\r\n".encode() + b"\r\n".join(headers) + b"\r\nConnection: close\r\n\r\n")
        await up_w.drain()
        log.info("%s %s -> %s", method, target, ip)
        budget = [MAX_TUNNEL_BYTES]
        await _pipe(up_r, writer, budget)
        up_w.close()
        return

    await _reply(writer, 405, "Only CONNECT to :443 and GET/HEAD over http are proxied")


async def serve(host: str = "0.0.0.0", port: int = 3128, **kwargs) -> asyncio.AbstractServer:
    async def _client(reader, writer):
        try:
            await handle(reader, writer, **kwargs)
        except Exception:  # pragma: no cover - keep the proxy alive
            log.exception("proxy connection failed")
            writer.close()
    server = await asyncio.start_server(_client, host, port, limit=65536)
    log.info("proxy listening on %s:%d", host, port)
    return server
