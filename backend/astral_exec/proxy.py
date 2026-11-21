"""The per-child egress proxy: a CONNECT-only listener that tunnels to the host and port pairs of one policy and refuses everything else.
launch.py starts one per confined call, on both loopback families for the Seatbelt backend and on a Unix socket for the Linux forwarder, and closes it when the call ends.
It resolves each name once, refuses any answer holding a non-public address and connects to the exact address it checked, so a name can never reach the node's own network.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import shutil
import socket
import tempfile
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

from .forwarder import relay

MAX_TUNNELS = 32
MAX_HEAD_BYTES = 8192
MAX_HEADER_LINES = 64
HANDSHAKE_SECONDS = 10.0
CONNECT_SECONDS = 10.0
TUNNEL_DEADLINE_SECONDS = 20.0
BACKLOG = 64
ACCEPT_POLL_SECONDS = 0.05
BIND_ATTEMPTS = 32
SOCKET_NAME = "egress.sock"
_ALLOWED_VERSIONS = (b"HTTP/1.0", b"HTTP/1.1")
_TOKEN = frozenset(b"!#$%&'*+-.^_`|~0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz")
_NON_PUBLIC = tuple(
    ipaddress.ip_network(text)
    for text in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.0.0.0/24",
        "192.88.99.0/24",
        "192.168.0.0/16",
        "198.18.0.0/15",
        "224.0.0.0/3",
        "::/128",
        "::1/128",
        "64:ff9b::/96",
        "64:ff9b:1::/48",
        "100::/64",
        "2001::/23",
        "2002::/16",
        "fc00::/7",
        "fe80::/10",
        "ff00::/8",
    )
)
_REASONS = {
    200: "Connection Established",
    400: "Bad Request",
    403: "Forbidden",
    405: "Method Not Allowed",
    408: "Request Timeout",
    431: "Request Header Fields Too Large",
    502: "Bad Gateway",
    504: "Gateway Timeout",
    503: "Service Unavailable",
}
_LOG = logging.getLogger("astral_exec.proxy")

Resolver = Callable[[str, int], Sequence[str]]
Connector = Callable[[str, int, float], socket.socket]


class _Deny(Exception):
    def __init__(self, status: int, code: str, target: str = "-") -> None:
        super().__init__(code)
        self.status = status
        self.code = code
        self.target = target


def is_public(address: str) -> bool:
    try:
        parsed = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
        parsed = parsed.ipv4_mapped
    if any(parsed in network for network in _NON_PUBLIC if network.version == parsed.version):
        return False
    return parsed.is_global and not (
        parsed.is_private
        or parsed.is_loopback
        or parsed.is_link_local
        or parsed.is_multicast
        or parsed.is_reserved
        or parsed.is_unspecified
    )


def resolve_public(host: str, port: int, resolver: Resolver) -> list[str]:
    try:
        answers = list(resolver(host, port))
    except (OSError, ValueError, UnicodeError) as error:
        raise _Deny(502, "proxy.deny.resolve") from error
    if not answers:
        raise _Deny(502, "proxy.deny.resolve")
    if not all(is_public(answer) for answer in answers):
        raise _Deny(403, "proxy.deny.private")
    return answers


def _system_resolver(host: str, port: int) -> list[str]:
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


def _system_connector(address: str, port: int, timeout: float) -> socket.socket:
    family = (
        socket.AF_INET6
        if isinstance(ipaddress.ip_address(address), ipaddress.IPv6Address)
        else socket.AF_INET
    )
    upstream = socket.socket(family, socket.SOCK_STREAM)
    try:
        upstream.settimeout(timeout)
        upstream.connect((address, port))
        upstream.settimeout(None)
    except BaseException:
        upstream.close()
        raise
    return upstream


def _looks_numeric(host: str) -> bool:
    last = host.rsplit(".", 1)[-1]
    return last.isdigit() or last.startswith("0x")


class Proxy:
    def __init__(
        self,
        hosts: Iterable[Mapping[str, Any]],
        family: str,
        *,
        resolver: Resolver | None = None,
        connector: Connector | None = None,
        handshake_seconds: float = HANDSHAKE_SECONDS,
        deadline_seconds: float = TUNNEL_DEADLINE_SECONDS,
        max_tunnels: int = MAX_TUNNELS,
    ) -> None:
        if family not in ("tcp", "unix"):
            raise ValueError("family must be tcp or unix")
        self._allowed = frozenset((str(each["host"]), int(each["port"])) for each in hosts)
        if not self._allowed:
            raise ValueError("a proxy needs at least one host")
        self._names = frozenset(host for host, _port in self._allowed)
        self._family = family
        self._resolver = resolver or _system_resolver
        self._connector = connector or _system_connector
        self._handshake = handshake_seconds
        self._deadline = deadline_seconds
        self._slots = threading.BoundedSemaphore(max_tunnels)
        self._lock = threading.Lock()
        self._active: set[socket.socket] = set()
        self._listeners: list[socket.socket] = []
        self._threads: list[threading.Thread] = []
        self._folder: str | None = None
        self._address: int | str | None = None
        self._closed = False

    @property
    def address(self) -> int | str:
        if self._address is None:
            raise RuntimeError("the proxy is not started")
        return self._address

    def start(self) -> None:
        if self._address is not None or self._closed:
            raise RuntimeError("the proxy starts once")
        try:
            if self._family == "unix":
                self._address = self._listen_unix()
            else:
                self._address = self._listen_tcp()
            for listener in self._listeners:
                thread = threading.Thread(target=self._accept, args=(listener,), daemon=True)
                thread.start()
                self._threads.append(thread)
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        with self._lock:
            self._closed = True
            sockets = [*self._listeners, *self._active]
            self._listeners = []
        for each in sockets:
            try:
                each.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                each.close()
            except OSError:
                pass
        for thread in self._threads:
            thread.join(timeout=2.0)
        if self._folder is not None:
            shutil.rmtree(self._folder, ignore_errors=True)
            self._folder = None

    def _listen_unix(self) -> str:
        self._folder = tempfile.mkdtemp(prefix="astral-egress-")
        path = os.path.join(self._folder, SOCKET_NAME)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._listeners.append(listener)
        listener.bind(path)
        os.chmod(path, 0o600)
        listener.listen(BACKLOG)
        return path

    def _listen_tcp(self) -> int:
        for _attempt in range(BIND_ATTEMPTS):
            first = second = None
            try:
                first = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                second = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
                second.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                first.bind(("127.0.0.1", 0))
                port = first.getsockname()[1]
                second.bind(("::1", port))
            except OSError:
                for each in (first, second):
                    if each is not None:
                        each.close()
                continue
            for each in (first, second):
                each.listen(BACKLOG)
                self._listeners.append(each)
            return int(port)
        raise OSError("no port is free on both loopback families")

    def _accept(self, listener: socket.socket) -> None:
        listener.settimeout(ACCEPT_POLL_SECONDS)
        while not self._closed:
            try:
                client, _peer = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                if self._closed or listener.fileno() < 0:
                    return
                time.sleep(0.05)
                continue
            if not self._slots.acquire(blocking=False):
                self._reply(client, 503)
                _LOG.info("proxy decision code=proxy.deny.busy target=-")
                client.close()
                continue
            with self._lock:
                if self._closed:
                    client.close()
                    self._slots.release()
                    return
                self._active.add(client)
            threading.Thread(target=self._serve, args=(client,), daemon=True).start()

    def _serve(self, client: socket.socket) -> None:
        upstream: socket.socket | None = None
        try:
            upstream, leftover, target = self._admit(client)
            with self._lock:
                if self._closed:
                    return
                self._active.add(upstream)
            _LOG.info("proxy decision code=proxy.allow target=%s", target)
            client.sendall(b"HTTP/1.1 200 " + _REASONS[200].encode() + b"\r\n\r\n")
            if leftover:
                upstream.sendall(leftover)
            client.settimeout(None)
            relay(client, upstream)
        except _Deny as denied:
            _LOG.info("proxy decision code=%s target=%r", denied.code, denied.target)
            self._reply(client, denied.status)
        except OSError:
            _LOG.info("proxy decision code=proxy.error.io target=-")
        finally:
            with self._lock:
                self._active.discard(client)
                if upstream is not None:
                    self._active.discard(upstream)
            for each in (client, upstream):
                if each is not None:
                    try:
                        each.close()
                    except OSError:
                        pass
            self._slots.release()

    def _reply(self, client: socket.socket, status: int) -> None:
        text = f"HTTP/1.1 {status} {_REASONS[status]}\r\nConnection: close\r\nContent-Length: 0\r\n\r\n"
        try:
            client.settimeout(1.0)
            client.sendall(text.encode())
        except OSError:
            pass

    def _admit(self, client: socket.socket) -> tuple[socket.socket, bytes, str]:
        head, leftover = self._read_head(client)
        host, port = self._parse(head)
        target = f"{host}:{port}"
        if (host, port) not in self._allowed:
            code = "proxy.deny.port" if host in self._names else "proxy.deny.host"
            raise _Deny(403, code, target)
        deadline = time.monotonic() + self._deadline
        try:
            answers = self._resolve(host, port, deadline)
        except _Deny as denied:
            denied.target = target
            raise
        for address in answers:
            left = deadline - time.monotonic()
            if left <= 0:
                raise _Deny(504, "proxy.error.timeout", target)
            try:
                upstream = self._connector(address, port, min(CONNECT_SECONDS, left))
            except (OSError, ValueError):
                continue
            return upstream, leftover, target
        raise _Deny(502, "proxy.error.connect", target)

    def _resolve(self, host: str, port: int, deadline: float) -> list[str]:
        outcome: list[Any] = []

        def work() -> None:
            try:
                outcome.append(resolve_public(host, port, self._resolver))
            except BaseException as error:  # noqa: BLE001
                outcome.append(error)

        worker = threading.Thread(target=work, daemon=True)
        worker.start()
        worker.join(max(deadline - time.monotonic(), 0))
        if not outcome:
            raise _Deny(504, "proxy.error.timeout")
        if isinstance(outcome[0], BaseException):
            raise outcome[0]
        return outcome[0]

    def _read_head(self, client: socket.socket) -> tuple[bytes, bytes]:
        deadline = time.monotonic() + self._handshake
        buffer = b""
        while b"\r\n\r\n" not in buffer:
            if len(buffer) > MAX_HEAD_BYTES:
                raise _Deny(431, "proxy.deny.too_large")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _Deny(408, "proxy.deny.timeout")
            client.settimeout(remaining)
            try:
                chunk = client.recv(4096)
            except TimeoutError as error:
                raise _Deny(408, "proxy.deny.timeout") from error
            if not chunk:
                raise _Deny(400, "proxy.deny.malformed")
            buffer += chunk
        head, _sep, rest = buffer.partition(b"\r\n\r\n")
        if len(head) > MAX_HEAD_BYTES:
            raise _Deny(431, "proxy.deny.too_large")
        return head, rest

    def _parse(self, head: bytes) -> tuple[str, int]:
        lines = head.split(b"\r\n")
        if len(lines) - 1 > MAX_HEADER_LINES:
            raise _Deny(431, "proxy.deny.too_large")
        for line in lines[1:]:
            if b":" not in line or line[:1] in (b" ", b"\t") or any(b < 32 and b != 9 or b == 127 for b in line):
                raise _Deny(400, "proxy.deny.malformed")
        if any(b < 32 or b == 127 for b in lines[0]):
            raise _Deny(400, "proxy.deny.malformed")
        parts = lines[0].split(b" ")
        if len(parts) != 3 or parts[2] not in _ALLOWED_VERSIONS:
            raise _Deny(400, "proxy.deny.malformed")
        method, authority, _version = parts
        if not method or any(b not in _TOKEN for b in method):
            raise _Deny(400, "proxy.deny.malformed")
        if method != b"CONNECT":
            raise _Deny(405, "proxy.deny.method")
        return self._authority(authority)

    def _authority(self, raw: bytes) -> tuple[str, int]:
        try:
            text = raw.decode("ascii")
        except UnicodeDecodeError as error:
            raise _Deny(400, "proxy.deny.malformed") from error
        host, colon, digits = text.rpartition(":")
        if not colon or not host or not digits.isascii() or not digits.isdigit() or len(digits) > 5 or digits[0] == "0":
            raise _Deny(400, "proxy.deny.malformed")
        port = int(digits)
        if not 1 <= port <= 65535 or any(c in host for c in "/?#@ \t"):
            raise _Deny(400, "proxy.deny.malformed")
        host = host.lower()
        if host.startswith("["):
            raise _Deny(403, "proxy.deny.literal")
        try:
            ipaddress.ip_address(host)
        except ValueError:
            if not _looks_numeric(host):
                return host, port
        raise _Deny(403, "proxy.deny.literal")
