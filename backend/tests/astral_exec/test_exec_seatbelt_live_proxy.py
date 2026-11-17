"""Runs confined tools under sandbox-exec with a policy that holds hosts and checks that the network is reachable only through the per-call egress proxy.
It runs only on macOS with /usr/bin/sandbox-exec and forwards the proxy to a loopback fixture, so it needs no network.
"""

from __future__ import annotations

import http.server
import json
import os
import pwd
import socket
import sys
import threading

import pytest
from astral_exec import Policy, launch
from astral_exec.constants import SANDBOX_EXEC
from astral_exec.facts import probe

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin" or not os.path.exists(SANDBOX_EXEC),
    reason="Seatbelt runs need macOS with /usr/bin/sandbox-exec",
)

PYTHON = os.path.realpath(sys.executable)
PREFIX = os.path.realpath(sys.base_prefix)
HOME = pwd.getpwuid(os.getuid()).pw_dir
LIMITS = {
    "memory_bytes": 1 << 30,
    "processes": 1,
    "open_files": 256,
    "file_bytes": 1 << 24,
}
PUBLIC = "93.184.216.34"
CURL = "/usr/bin/curl"


class Web(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        self.hits = []
        super().__init__(("127.0.0.1", 0), self.Handler)

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.server.hits.append(self.path)
            body = b"fixture-body"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass


@pytest.fixture
def web():
    server = Web()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture
def confined(tmp_path, web):
    data = tmp_path.resolve() / "data"
    workspace = data / "exec" / "call"
    workspace.mkdir(parents=True)
    lookups = []

    def resolve(host, port):
        lookups.append((host, port))
        return [PUBLIC]

    options = {
        "resolver": resolve,
        "connector": lambda address, port, timeout: socket.create_connection(
            ("127.0.0.1", web.server_address[1])
        ),
    }

    def go(argv, hosts):
        document = {
            "v": 1,
            "reads": [PREFIX],
            "workspace": str(workspace),
            "hosts": hosts,
            "limits": LIMITS,
            "time": {"wall_ms": 60000},
        }
        policy = Policy.load(document, "darwin", str(data), HOME)
        return launch._run(policy, argv, {}, facts=probe(), proxy_options=options)

    go.lookups = lookups
    return go


HOSTS = [{"host": "fixture.example.com", "port": 80}]


def test_curl_reaches_the_fixture_only_through_the_proxy(confined, web):
    outcome = confined(
        [CURL, "-q", "-sS", "-p", "--max-time", "30", "http://fixture.example.com/a?x=1"],
        HOSTS,
    )
    assert (outcome.code, outcome.exit_code) == (None, 0), outcome.stderr
    assert outcome.stdout == b"fixture-body"
    assert web.hits == ["/a?x=1"]
    assert confined.lookups == [("fixture.example.com", 80)]


def test_a_direct_connection_to_the_fixture_is_refused_by_the_profile(confined, web):
    port = web.server_address[1]
    outcome = confined(
        [CURL, "-q", "-sS", "--noproxy", "*", "--max-time", "30", f"http://127.0.0.1:{port}/"],
        HOSTS,
    )
    assert outcome.exit_code != 0 and outcome.stdout == b""
    assert web.hits == []


def test_a_host_outside_the_policy_is_refused_by_the_proxy(confined, web):
    outcome = confined(
        [CURL, "-q", "-sS", "-p", "--max-time", "30", "http://other.example.com/"],
        HOSTS,
    )
    assert outcome.exit_code != 0 and outcome.stdout == b""
    assert web.hits == [] and confined.lookups == []


def test_the_child_can_open_sockets_only_to_the_proxy_port(confined, web):
    code = (
        "import json, os, socket, sys\n"
        "def attempt(host, port, family):\n"
        "    s = socket.socket(family)\n"
        "    s.settimeout(5)\n"
        "    try:\n"
        "        s.connect((host, port))\n"
        "        return 'connected'\n"
        "    except OSError as e:\n"
        "        return 'refused' if e.errno in (1, 13) else 'other'\n"
        "    finally:\n"
        "        s.close()\n"
        "proxy = int(os.environ['HTTP_PROXY'].rsplit(':', 1)[1])\n"
        "fixture = int(sys.argv[1])\n"
        "print(json.dumps({\n"
        "  'proxy4': attempt('127.0.0.1', proxy, socket.AF_INET),\n"
        "  'proxy6': attempt('::1', proxy, socket.AF_INET6),\n"
        "  'fixture4': attempt('127.0.0.1', fixture, socket.AF_INET),\n"
        "  'fixture6': attempt('::1', fixture, socket.AF_INET6),\n"
        "  'public': attempt('93.184.216.34', 443, socket.AF_INET),\n"
        "}))\n"
    )
    outcome = confined(
        [PYTHON, "-I", "-B", "-c", code, str(web.server_address[1])], HOSTS
    )
    assert (outcome.code, outcome.exit_code) == (None, 0), outcome.stderr
    seen = json.loads(outcome.stdout)
    assert seen["proxy4"] == "connected" and seen["proxy6"] == "connected"
    assert seen["fixture4"] == "refused" and seen["fixture6"] != "connected"
    assert seen["public"] == "refused"


def test_a_policy_without_hosts_gets_no_proxy_and_no_network(confined):
    outcome = confined(
        [PYTHON, "-I", "-B", "-c", "import os; print(sorted(k for k in os.environ if 'PROXY' in k.upper()))"],
        [],
    )
    assert outcome.stdout.strip() == b"[]"
