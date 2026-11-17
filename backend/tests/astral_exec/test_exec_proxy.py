"""Checks the egress proxy against loopback fixtures with an injected resolver and connector: which requests tunnel, which are refused and with what decision code, and that the checked address is the one connected.
It needs no conftest and no network, so the Linux harness runs it as is.
"""

from __future__ import annotations

import logging
import os
import socket
import threading
import time

import pytest
from astral_exec import proxy as proxy_module
from astral_exec.proxy import Proxy, is_public

PUBLIC = "93.184.216.34"
HOSTS = [
    {"host": "api.example.com", "port": 443},
    {"host": "files.example.com", "port": 80},
    {"host": "files.example.com", "port": 443},
]


class Fixture:
    def __init__(self) -> None:
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(8)
        self.port = self.listener.getsockname()[1]
        self.received = []
        self.calls = []
        self.lookups = []
        self.answers = {}
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.listener.accept()
            except OSError:
                return
            threading.Thread(target=self._echo, args=(conn,), daemon=True).start()

    def _echo(self, conn):
        with conn:
            while data := conn.recv(4096):
                self.received.append(data)
                conn.sendall(b"echo:" + data)

    def resolve(self, host, port):
        self.lookups.append((host, port))
        return self.answers.get(host, [PUBLIC])

    def connect(self, address, port, timeout):
        self.calls.append((address, port))
        return socket.create_connection(("127.0.0.1", self.port))


@pytest.fixture
def fixture():
    fx = Fixture()
    yield fx
    fx.listener.close()


@pytest.fixture
def running(fixture):
    proxies = []

    def make(family="tcp", **options):
        options.setdefault("resolver", fixture.resolve)
        options.setdefault("connector", fixture.connect)
        proxy = Proxy(HOSTS, family, **options)
        proxy.start()
        proxies.append(proxy)
        return proxy

    yield make
    for proxy in proxies:
        proxy.close()


def _open(proxy):
    if isinstance(proxy.address, str):
        conn = socket.socket(socket.AF_UNIX)
        conn.connect(proxy.address)
    else:
        conn = socket.create_connection(("127.0.0.1", proxy.address))
    conn.settimeout(10)
    return conn


def _read_head(conn):
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = conn.recv(4096)
        if not chunk:
            break
        data += chunk
    return data


def _status(proxy, payload):
    with _open(proxy) as conn:
        conn.sendall(payload)
        head = _read_head(conn)
    return int(head.split(b" ")[1]) if head else 0


def _connect(target, version="HTTP/1.1"):
    return f"CONNECT {target} {version}\r\nHost: {target}\r\n\r\n".encode()


@pytest.mark.parametrize("family", ["tcp", "unix"])
def test_allowed_host_tunnels_to_the_fixture(running, fixture, family):
    proxy = running(family)
    with _open(proxy) as conn:
        conn.sendall(_connect("api.example.com:443"))
        assert _read_head(conn).startswith(b"HTTP/1.1 200")
        conn.sendall(b"hello")
        got = b""
        while not got.endswith(b"echo:hello"):
            chunk = conn.recv(4096)
            assert chunk
            got += chunk
    assert fixture.calls == [(PUBLIC, 443)]


def test_both_loopback_families_serve_the_same_port(running):
    proxy = running("tcp")
    conn = socket.create_connection(("::1", proxy.address), timeout=10)
    with conn:
        conn.sendall(_connect("api.example.com:443"))
        assert _read_head(conn).startswith(b"HTTP/1.1 200")


def test_port_80_entry_tunnels_when_listed(running, fixture):
    proxy = running()
    assert _status(proxy, _connect("files.example.com:80")) == 200
    assert fixture.calls == [(PUBLIC, 80)]


def test_early_client_bytes_after_the_head_reach_the_upstream(running, fixture):
    proxy = running()
    with _open(proxy) as conn:
        conn.sendall(_connect("api.example.com:443") + b"early")
        data = _read_head(conn)
        assert data.startswith(b"HTTP/1.1 200")
        while not data.endswith(b"echo:early"):
            data += conn.recv(4096)
        assert data.endswith(b"\r\n\r\necho:early")


@pytest.mark.parametrize(
    "target,status,code",
    [
        ("other.example.com:443", 403, "proxy.deny.host"),
        ("api.example.com:22", 403, "proxy.deny.port"),
        ("api.example.com:8443", 403, "proxy.deny.port"),
        ("api.example.com:80", 403, "proxy.deny.port"),
        ("API.Example.com.:443", 403, "proxy.deny.host"),
        ("93.184.216.34:443", 403, "proxy.deny.literal"),
        ("127.0.0.1:443", 403, "proxy.deny.literal"),
        ("[::1]:443", 403, "proxy.deny.literal"),
        ("::1:443", 403, "proxy.deny.literal"),
        ("2130706433:443", 403, "proxy.deny.literal"),
        ("0x7f.1:443", 403, "proxy.deny.literal"),
        ("api.example.com:0", 400, "proxy.deny.malformed"),
        ("api.example.com:0443", 400, "proxy.deny.malformed"),
        ("api.example.com:65536", 400, "proxy.deny.malformed"),
        ("api.example.com:44x", 400, "proxy.deny.malformed"),
        ("api.example.com", 400, "proxy.deny.malformed"),
        (":443", 400, "proxy.deny.malformed"),
        ("api.example.com@evil.test:443", 400, "proxy.deny.malformed"),
        ("api.example.com/x:443", 400, "proxy.deny.malformed"),
        ("api.exämple.com:443", 400, "proxy.deny.malformed"),
    ],
)
def test_connect_targets_outside_the_policy_are_refused(
    running, fixture, caplog, target, status, code
):
    proxy = running()
    with caplog.at_level(logging.INFO, logger="astral_exec.proxy"):
        assert _status(proxy, _connect(target)) == status
    assert code in caplog.text
    assert fixture.lookups == [] and fixture.calls == []


@pytest.mark.parametrize(
    "payload,status,code",
    [
        (b"POST http://files.example.com/ HTTP/1.1\r\nHost: files.example.com\r\n\r\n", 405, "proxy.deny.method"),
        (b"GET http://files.example.com/ HTTP/1.1\r\nHost: files.example.com\r\n\r\n", 405, "proxy.deny.method"),
        (b"HEAD http://files.example.com/ HTTP/1.1\r\n\r\n", 405, "proxy.deny.method"),
        (b"connect api.example.com:443 HTTP/1.1\r\n\r\n", 405, "proxy.deny.method"),
        (b"CONNECT api.example.com:443\r\n\r\n", 400, "proxy.deny.malformed"),
        (b"CONNECT api.example.com:443 HTTP/2\r\n\r\n", 400, "proxy.deny.malformed"),
        (b"CONNECT  api.example.com:443 HTTP/1.1\r\n\r\n", 400, "proxy.deny.malformed"),
        (b"CONNECT api.example.com:443 HTTP/1.1 x\r\n\r\n", 400, "proxy.deny.malformed"),
        (b"\x00\x01garbage\r\n\r\n", 400, "proxy.deny.malformed"),
        (b"G(T / HTTP/1.1\r\n\r\n", 400, "proxy.deny.malformed"),
        (b"CONNECT api.example.com:443 HTTP/1.1\r\nbroken header\r\n\r\n", 400, "proxy.deny.malformed"),
        (b"CONNECT api.example.com:443 HTTP/1.1\r\n folded: x\r\n\r\n", 400, "proxy.deny.malformed"),
        (b"CONNECT api.example.com:443 HTTP/1.1\r\nX: a\x01b\r\n\r\n", 400, "proxy.deny.malformed"),
    ],
)
def test_other_methods_and_malformed_requests_are_refused(
    running, fixture, caplog, payload, status, code
):
    proxy = running()
    with caplog.at_level(logging.INFO, logger="astral_exec.proxy"):
        assert _status(proxy, payload) == status
    assert code in caplog.text
    assert fixture.calls == []


def test_control_bytes_in_the_request_line_are_refused_and_never_logged_raw(running, caplog):
    proxy = running()
    with caplog.at_level(logging.INFO, logger="astral_exec.proxy"):
        for line in (
            b"CONNECT evil.example.com:443\nproxy.allow target=x HTTP/1.1",
            b"CONNECT evil.example.com:443\rproxy.allow HTTP/1.1",
            b"CONNECT evil.example.com:443 HTTP/1.1\x7f",
        ):
            assert _status(proxy, line + b"\r\n\r\n") == 400
        assert _status(proxy, _connect("evil\x01.example.com:443")) == 400
    assert all("\n" not in r.getMessage() and "\r" not in r.getMessage() for r in caplog.records)
    assert [r.getMessage().count("code=") for r in caplog.records] == [1] * 4
    assert "proxy.allow" not in caplog.text


def test_a_denied_target_is_logged_in_a_quoted_form(running, caplog):
    proxy = running()
    with caplog.at_level(logging.INFO, logger="astral_exec.proxy"):
        _status(proxy, _connect("other.example.com:443"))
    assert "target='other.example.com:443'" in caplog.text


def test_a_resolver_that_never_answers_hits_the_tunnel_deadline(running, caplog):
    release = threading.Event()
    proxy = running(resolver=lambda h, p: release.wait(30) or [PUBLIC], deadline_seconds=0.2)
    try:
        with caplog.at_level(logging.INFO, logger="astral_exec.proxy"):
            assert _status(proxy, _connect("api.example.com:443")) == 504
        assert "proxy.error.timeout" in caplog.text
    finally:
        release.set()


def test_an_exhausted_deadline_stops_before_the_next_address(running, fixture):
    fixture.answers["api.example.com"] = ["93.184.216.34", "93.184.216.35"]
    seen = []

    def connector(address, port, timeout):
        seen.append(address)
        time.sleep(0.3)
        raise OSError("slow")

    proxy = running(connector=connector, deadline_seconds=0.2)
    assert _status(proxy, _connect("api.example.com:443")) == 504
    assert seen == ["93.184.216.34"]


def test_a_connector_that_raises_value_error_counts_as_a_failed_address(running):
    def connector(address, port, timeout):
        raise ValueError("bad address")

    proxy = running(connector=connector)
    assert _status(proxy, _connect("api.example.com:443")) == 502


def test_a_failed_ipv6_socket_closes_the_first_listener(monkeypatch):
    made = []
    real = socket.socket

    def factory(family=socket.AF_INET, *args, **kwargs):
        if family == socket.AF_INET6:
            raise OSError("no ipv6")
        made.append(real(family, *args, **kwargs))
        return made[-1]

    monkeypatch.setattr(proxy_module.socket, "socket", factory)
    with pytest.raises(OSError):
        Proxy(HOSTS, "tcp").start()
    assert made and all(each.fileno() == -1 for each in made)


def test_a_peer_that_closes_before_the_head_is_malformed(running, caplog):
    proxy = running()
    with caplog.at_level(logging.INFO, logger="astral_exec.proxy"):
        with _open(proxy) as conn:
            conn.sendall(b"CONNECT api.exam")
            conn.shutdown(socket.SHUT_WR)
            assert conn.recv(4096).startswith(b"HTTP/1.1 400")
    assert "proxy.deny.malformed" in caplog.text


@pytest.mark.parametrize(
    "answers",
    [
        ["10.0.0.1"],
        ["127.0.0.1"],
        ["169.254.169.254"],
        ["::1"],
        ["100.64.0.1"],
        ["192.88.99.1"],
        ["64:ff9b:1::1"],
        ["fd00:ec2::254"],
        ["::ffff:10.0.0.1"],
        ["fe80::1%en0"],
        ["0.0.0.0"],
        [PUBLIC, "10.0.0.1"],
        ["10.0.0.1", PUBLIC],
        ["not-an-address"],
    ],
)
def test_a_name_with_a_non_public_answer_is_refused_without_connecting(
    running, fixture, caplog, answers
):
    fixture.answers["api.example.com"] = answers
    proxy = running()
    with caplog.at_level(logging.INFO, logger="astral_exec.proxy"):
        assert _status(proxy, _connect("api.example.com:443")) == 403
    assert "proxy.deny.private" in caplog.text
    assert fixture.calls == []


@pytest.mark.parametrize(
    "answers", [[], OSError("no such name"), ValueError("bad")]
)
def test_a_name_that_does_not_resolve_is_refused(running, fixture, caplog, answers):
    def resolver(host, port):
        if isinstance(answers, Exception):
            raise answers
        return answers

    proxy = running(resolver=resolver)
    with caplog.at_level(logging.INFO, logger="astral_exec.proxy"):
        assert _status(proxy, _connect("api.example.com:443")) == 502
    assert "proxy.deny.resolve" in caplog.text
    assert fixture.calls == []


def test_the_name_is_resolved_once_and_the_checked_address_is_connected(running, fixture):
    fixture.answers["api.example.com"] = [PUBLIC, "2606:2800:220:1:248:1893:25c8:1946"]
    proxy = running()
    assert _status(proxy, _connect("api.example.com:443")) == 200
    assert fixture.lookups == [("api.example.com", 443)]
    assert fixture.calls == [(PUBLIC, 443)]


def test_a_failed_first_address_falls_to_the_next_checked_address(running, fixture):
    fixture.answers["api.example.com"] = ["93.184.216.34", "93.184.216.35"]
    seen = []

    def connector(address, port, timeout):
        seen.append(address)
        if address == "93.184.216.34":
            raise OSError("down")
        return fixture.connect(address, port, timeout)

    proxy = running(connector=connector)
    assert _status(proxy, _connect("api.example.com:443")) == 200
    assert seen == ["93.184.216.34", "93.184.216.35"]


def test_every_address_failing_is_a_bad_gateway(running, caplog):
    def connector(address, port, timeout):
        raise OSError("down")

    proxy = running(connector=connector)
    with caplog.at_level(logging.INFO, logger="astral_exec.proxy"):
        assert _status(proxy, _connect("api.example.com:443")) == 502
    assert "proxy.error.connect" in caplog.text


def test_the_log_carries_a_decision_code_and_never_a_query_string(running, caplog):
    proxy = running()
    with caplog.at_level(logging.INFO, logger="astral_exec.proxy"):
        _status(proxy, b"GET http://files.example.com/p?token=sekrit&a=b HTTP/1.1\r\n\r\n")
        _status(proxy, _connect("api.example.com:443"))
        _status(proxy, _connect("nope.example.com:443"))
    assert "sekrit" not in caplog.text and "token" not in caplog.text
    codes = [r.getMessage().split("code=")[1].split(" ")[0] for r in caplog.records]
    assert codes == ["proxy.deny.method", "proxy.allow", "proxy.deny.host"]


def test_an_oversize_head_and_too_many_headers_are_refused(running, fixture):
    proxy = running()
    assert _status(proxy, b"CONNECT api.example.com:443 HTTP/1.1\r\nX: " + b"a" * 20000) == 431
    many = b"".join(b"H%d: v\r\n" % i for i in range(200))
    assert _status(proxy, b"CONNECT api.example.com:443 HTTP/1.1\r\n" + many + b"\r\n") == 431
    assert fixture.calls == []


def test_a_silent_peer_is_dropped_by_the_handshake_timeout(running):
    proxy = running(handshake_seconds=0.2)
    with _open(proxy) as conn:
        assert _read_head(conn).startswith(b"HTTP/1.1 408")


def test_tunnels_beyond_the_limit_are_refused_and_slots_return(running, fixture):
    proxy = running(max_tunnels=1)
    first = _open(proxy)
    first.sendall(_connect("api.example.com:443"))
    assert _read_head(first).startswith(b"HTTP/1.1 200")
    assert _status(proxy, _connect("api.example.com:443")) == 503
    first.close()
    for _ in range(200):
        if _status(proxy, _connect("api.example.com:443")) == 200:
            break
    else:
        pytest.fail("the slot was never released")


def test_close_ends_open_tunnels_and_removes_the_socket(running):
    proxy = running("unix")
    path = proxy.address
    conn = _open(proxy)
    conn.sendall(_connect("api.example.com:443"))
    assert _read_head(conn).startswith(b"HTTP/1.1 200")
    proxy.close()
    assert conn.recv(4096) == b""
    conn.close()
    assert not os.path.exists(path)
    proxy.close()


def test_the_unix_socket_is_private_to_the_owner(running):
    proxy = running("unix")
    assert (os.stat(proxy.address).st_mode & 0o777) == 0o600
    assert (os.stat(os.path.dirname(proxy.address)).st_mode & 0o777) == 0o700


def test_lifecycle_is_checked(fixture):
    with pytest.raises(ValueError):
        Proxy(HOSTS, "udp")
    with pytest.raises(ValueError):
        Proxy([], "tcp")
    proxy = Proxy(HOSTS, "tcp", resolver=fixture.resolve, connector=fixture.connect)
    with pytest.raises(RuntimeError):
        proxy.address
    proxy.start()
    with pytest.raises(RuntimeError):
        proxy.start()
    proxy.close()
    with pytest.raises(RuntimeError):
        proxy.start()


def test_a_listener_that_cannot_bind_leaves_nothing_behind(monkeypatch):
    class Refusing(socket.socket):
        def bind(self, address):
            raise OSError("no")

    monkeypatch.setattr(proxy_module.socket, "socket", Refusing)
    proxy = Proxy(HOSTS, "tcp")
    with pytest.raises(OSError):
        proxy.start()
    proxy = Proxy(HOSTS, "unix")
    with pytest.raises(OSError):
        proxy.start()
    assert proxy._folder is None


def test_the_default_stack_refuses_a_name_that_resolves_to_loopback():
    proxy = Proxy([{"host": "localhost", "port": 443}], "tcp")
    proxy.start()
    try:
        assert _status(proxy, _connect("localhost:443")) == 403
    finally:
        proxy.close()


@pytest.mark.parametrize("bind", ["127.0.0.1", "::1"])
def test_the_default_connector_reaches_the_exact_address(bind):
    family = socket.AF_INET6 if ":" in bind else socket.AF_INET
    listener = socket.socket(family)
    listener.bind((bind, 0))
    listener.listen(1)
    try:
        upstream = proxy_module._system_connector(bind, listener.getsockname()[1], 5.0)
        accepted, _ = listener.accept()
        upstream.sendall(b"x")
        assert accepted.recv(1) == b"x"
        upstream.close()
        accepted.close()
    finally:
        listener.close()


def test_the_default_connector_closes_on_failure():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    listener.close()
    with pytest.raises(OSError):
        proxy_module._system_connector("127.0.0.1", port, 1.0)


def test_the_default_resolver_returns_address_strings():
    assert "127.0.0.1" in proxy_module._system_resolver("localhost", 443) or "::1" in (
        proxy_module._system_resolver("localhost", 443)
    )


@pytest.mark.parametrize(
    "address,expected",
    [
        (PUBLIC, True),
        ("2606:2800:220:1:248:1893:25c8:1946", True),
        ("10.1.2.3", False),
        ("172.16.0.1", False),
        ("192.168.1.1", False),
        ("100.100.100.200", False),
        ("198.18.0.1", False),
        ("224.0.0.1", False),
        ("255.255.255.255", False),
        ("64:ff9b::a00:1", False),
        ("2002:a00:1::", False),
        ("::ffff:127.0.0.1", False),
        ("garbage", False),
    ],
)
def test_is_public(address, expected):
    assert is_public(address) is expected
