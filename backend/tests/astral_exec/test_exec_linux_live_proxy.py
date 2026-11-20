"""Runs confined tools through the Linux backend in the bubblewrap harness with a policy that holds hosts and checks that the namespace reaches the network only through the forwarder and the per-call egress proxy.
It runs only under scripts/exec_harness/run.sh on Linux and forwards the proxy to a loopback fixture, so it needs no network.
"""

from __future__ import annotations

import errno
import json
import os
import socket
import sys
import threading

import pytest
from astral_exec import Policy, launch
from astral_exec.facts import probe
from exec_vectors import minimal_policy

pytestmark = pytest.mark.skipif(
    os.environ.get("ASTRAL_EXEC_HARNESS") != "1" or sys.platform != "linux",
    reason="runs only under scripts/exec_harness/run.sh on linux",
)

PY = sys.executable
PUBLIC = "93.184.216.34"
HOSTS = [{"host": "fixture.example.com", "port": 80}]

TOOL = r"""
import json, os, socket, sys

def tunnel(target):
    s = socket.create_connection(("127.0.0.1", 3128), timeout=10)
    s.sendall(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode())
    head = b""
    while b"\r\n\r\n" not in head:
        chunk = s.recv(4096)
        if not chunk:
            break
        head += chunk
    status = int(head.split(b" ")[1]) if head else 0
    body = b""
    if status == 200:
        s.sendall(b"ping")
        body = s.recv(4096)
    s.close()
    return [status, body.decode()]

def attempt(host, port):
    s = socket.socket(socket.AF_INET)
    s.settimeout(5)
    try:
        s.connect((host, port))
        return "connected"
    except OSError as e:
        return e.errno
    finally:
        s.close()

print(json.dumps({
    "allowed": tunnel("fixture.example.com:80"),
    "other_host": tunnel("other.example.com:80"),
    "other_port": tunnel("fixture.example.com:22"),
    "literal": tunnel("127.0.0.1:80"),
    "direct_public": attempt("93.184.216.34", 80),
    "direct_fixture": attempt("127.0.0.1", int(sys.argv[1])),
    "env": os.environ.get("HTTPS_PROXY"),
}))
"""


class Echo:
    def __init__(self):
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(8)
        self.port = self.listener.getsockname()[1]
        self.lookups = []
        self.calls = []
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.listener.accept()
            except OSError:
                return
            threading.Thread(target=self._echo, args=(conn,), daemon=True).start()

    @staticmethod
    def _echo(conn):
        with conn:
            data = conn.recv(64)
            if data:
                conn.sendall(b"PONG:" + data)

    def resolve(self, host, port):
        self.lookups.append((host, port))
        return [PUBLIC]

    def connect(self, address, port, timeout):
        self.calls.append((address, port))
        return socket.create_connection(("127.0.0.1", self.port))


def _policy(tmp_path, hosts):
    data = tmp_path / "data"
    workspace = data / "exec" / "call"
    home = tmp_path / "home"
    for directory in (workspace, home):
        directory.mkdir(parents=True)
    document = minimal_policy(workspace=str(workspace), reads=[], hosts=hosts)
    return Policy.load(document, "linux", str(data), str(home))


def test_the_namespace_reaches_the_fixture_only_through_forwarder_and_proxy(tmp_path):
    echo = Echo()
    try:
        outcome = launch._run(
            _policy(tmp_path, HOSTS),
            [PY, "-I", "-S", "-c", TOOL, str(echo.port)],
            {},
            facts=probe(),
            proxy_options={"resolver": echo.resolve, "connector": echo.connect},
        )
    finally:
        echo.listener.close()
    assert (outcome.code, outcome.exit_code) == (None, 0), outcome.stderr
    seen = json.loads(outcome.stdout)
    assert seen["allowed"] == [200, "PONG:ping"]
    assert seen["other_host"][0] == 403
    assert seen["other_port"][0] == 403
    assert seen["literal"][0] == 403
    assert seen["direct_public"] in (errno.EACCES, errno.EPERM, errno.ENETUNREACH)
    assert seen["direct_fixture"] in (errno.EACCES, errno.EPERM, errno.ECONNREFUSED)
    assert seen["env"] == "http://127.0.0.1:3128"
    assert echo.lookups == [("fixture.example.com", 80)]
    assert echo.calls == [(PUBLIC, 80)]


def test_a_proxy_that_cannot_resolve_a_private_answer_never_connects(tmp_path):
    echo = Echo()
    try:
        outcome = launch._run(
            _policy(tmp_path, HOSTS),
            [PY, "-I", "-S", "-c", TOOL, str(echo.port)],
            {},
            facts=probe(),
            proxy_options={
                "resolver": lambda host, port: ["10.0.0.1"],
                "connector": echo.connect,
            },
        )
    finally:
        echo.listener.close()
    assert (outcome.code, outcome.exit_code) == (None, 0), outcome.stderr
    assert json.loads(outcome.stdout)["allowed"][0] == 403
    assert echo.calls == []


def test_the_socket_folder_is_removed_when_the_call_ends(tmp_path):
    before = {name for name in os.listdir("/tmp") if name.startswith("astral-egress-")}
    launch._run(
        _policy(tmp_path, HOSTS),
        [PY, "-I", "-S", "-c", "pass"],
        {},
        facts=probe(),
        proxy_options={"resolver": lambda h, p: [PUBLIC], "connector": lambda *a: None},
    )
    after = {name for name in os.listdir("/tmp") if name.startswith("astral-egress-")}
    assert after == before
