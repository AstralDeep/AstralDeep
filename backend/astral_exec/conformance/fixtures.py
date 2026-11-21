"""Loopback fixtures for the conformance suite: a counting TCP listener, an echo upstream, a TLS upstream, a UDP listener and a Unix socket listener, plus the resolver and connector the proxy cases inject.
Every listener counts what reaches it, so a case proves a denial by the sandboxed tool's outcome and by the fixture seeing nothing.
"""

from __future__ import annotations

import os
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
from collections.abc import Mapping, Sequence
from pathlib import Path

PUBLIC = "93.184.216.34"
HOST = "fixture.example.com"
CERT_SECONDS = 30


class Counter:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._value = 0

    def add(self) -> None:
        with self._lock:
            self._value += 1

    @property
    def value(self) -> int:
        with self._lock:
            return self._value


def _stream() -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(16)
    return sock


def _accept_loop(listener: socket.socket, handle) -> None:
    while True:
        try:
            conn, _ = listener.accept()
        except OSError:
            return
        threading.Thread(target=handle, args=(conn,), daemon=True).start()


def make_certificate(cert_dir: Path, key_dir: Path) -> tuple[Path, Path]:
    openssl = shutil.which("openssl")
    if openssl is None:
        raise OSError("openssl is not installed, and the TLS fixture needs it")
    cert, key = cert_dir / "fixture.pem", key_dir / "fixture.key"
    subprocess.run(
        [
            openssl,
            "req",
            "-x509",
            "-newkey",
            "ec",
            "-pkeyopt",
            "ec_paramgen_curve:prime256v1",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(cert),
            "-days",
            "1",
            "-subj",
            f"/CN={HOST}",
            "-addext",
            f"subjectAltName=DNS:{HOST}",
        ],
        check=True,
        capture_output=True,
        timeout=CERT_SECONDS,
    )
    return cert, key


class Fixtures:
    def __init__(self, cert_dir: Path, key_dir: Path) -> None:
        self.cert: Path | None = None
        self.cert_error: str | None = None
        self.tcp_accepts = Counter()
        self.unix_accepts = Counter()
        self.udp_datagrams = Counter()
        self.echo_accepts = Counter()
        self.tls_accepts = Counter()
        self._socket_dir = tempfile.mkdtemp(prefix="ac-", dir="/tmp")
        self.unix_path = os.path.join(self._socket_dir, "s")
        self._listeners: list[socket.socket] = []
        self._start(cert_dir, key_dir)

    def _start(self, cert_dir: Path, key_dir: Path) -> None:
        context = None
        try:
            cert, key = make_certificate(cert_dir, key_dir)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(cert, key)
            self.cert = cert
        except (OSError, subprocess.SubprocessError, ssl.SSLError) as error:
            self.cert_error = f"{type(error).__name__}: {error}"
        tcp, echo, tls = _stream(), _stream(), _stream()
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        udp.bind(("127.0.0.1", 0))
        unix = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        unix.bind(self.unix_path)
        unix.listen(16)
        self._listeners = [tcp, echo, tls, udp, unix]
        self.tcp_port = tcp.getsockname()[1]
        self.echo_port = echo.getsockname()[1]
        self.tls_port = tls.getsockname()[1]
        self.udp_port = udp.getsockname()[1]
        threading.Thread(
            target=_accept_loop, args=(tcp, self._count(self.tcp_accepts)), daemon=True
        ).start()
        threading.Thread(
            target=_accept_loop,
            args=(unix, self._count(self.unix_accepts)),
            daemon=True,
        ).start()
        threading.Thread(
            target=_accept_loop, args=(echo, self._echo), daemon=True
        ).start()
        threading.Thread(
            target=_accept_loop,
            args=(tls, lambda conn: self._tls(conn, context)),
            daemon=True,
        ).start()
        threading.Thread(target=self._datagrams, args=(udp,), daemon=True).start()

    @staticmethod
    def _count(counter: Counter):
        def handle(conn: socket.socket) -> None:
            counter.add()
            conn.close()

        return handle

    def _echo(self, conn: socket.socket) -> None:
        self.echo_accepts.add()
        with conn:
            conn.settimeout(10)
            try:
                data = conn.recv(64)
                if data:
                    conn.sendall(b"PONG:" + data)
            except OSError:
                return

    def _tls(self, conn: socket.socket, context: ssl.SSLContext | None) -> None:
        self.tls_accepts.add()
        if context is None:
            conn.close()
            return
        try:
            conn.settimeout(10)
            with context.wrap_socket(conn, server_side=True) as tls:
                tls.recv(4096)
                tls.sendall(
                    b"HTTP/1.1 200 OK\r\nContent-Length: 10\r\nConnection: close\r\n\r\nfixture-ok"
                )
        except OSError:
            conn.close()

    def _datagrams(self, sock: socket.socket) -> None:
        while True:
            try:
                data, peer = sock.recvfrom(512)
            except OSError:
                return
            self.udp_datagrams.add()
            try:
                sock.sendto(data[:2] + b"\x81\x80" + data[4:], peer)
            except OSError:
                return

    def snapshot(self) -> dict[str, int]:
        return {
            "tcp": self.tcp_accepts.value,
            "unix": self.unix_accepts.value,
            "udp": self.udp_datagrams.value,
        }

    def stop(self) -> None:
        for listener in self._listeners:
            listener.close()
        shutil.rmtree(self._socket_dir, ignore_errors=True)


class Upstream:
    def __init__(
        self, fixtures: Fixtures, answers: Mapping[str, Sequence[str]]
    ) -> None:
        self._fixtures = fixtures
        self._answers = {host: list(addresses) for host, addresses in answers.items()}
        self.lookups: list[tuple[str, int]] = []
        self.dials: list[tuple[str, int]] = []

    def resolve(self, host: str, port: int) -> list[str]:
        self.lookups.append((host, port))
        if host not in self._answers:
            raise OSError("no such host")
        return list(self._answers[host])

    def connect(self, address: str, port: int, timeout: float) -> socket.socket:
        self.dials.append((address, port))
        target = self._fixtures.tls_port if port == 443 else self._fixtures.echo_port
        return socket.create_connection(("127.0.0.1", target), timeout=timeout)

    @property
    def options(self) -> dict:
        return {"resolver": self.resolve, "connector": self.connect}
