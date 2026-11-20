"""Proves the node's stdio sandbox backend refuses when no confinement backend exists and, where one does, runs a fixture stdio server with only its credential variable, reaching only its listed egress host through the proxy and nothing directly.
It builds the launch spec the way the kernel does, then drives the child's stdio and the launcher's loopback bridge; the confined cases need macOS Seatbelt or the Linux harness.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import textwrap
import threading
from pathlib import Path

import pytest
from astral_exec import Refusal

from orchestrator import node_stdio_exec
from orchestrator.node_stdio_exec import ExecRefusal, ExecSandbox, default_backend
from orchestrator.node_stdio_launcher import LaunchRefused, LaunchSpec, NodeStdioLauncher

SERVER = textwrap.dedent(
    '''
    import json, os, socket, sys, urllib.parse

    def connect(host):
        proxy = urllib.parse.urlsplit(os.environ.get("HTTPS_PROXY", ""))
        if not proxy.hostname:
            return "no-proxy"
        try:
            sock = socket.create_connection((proxy.hostname, proxy.port), timeout=5)
            sock.sendall(("CONNECT %s:443 HTTP/1.1\\r\\nHost: %s:443\\r\\n\\r\\n" % (host, host)).encode())
            return sock.recv(64).split(b" ")[1].decode()
        except Exception as error:
            return type(error).__name__

    def direct(port):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=2).close()
            return "connected"
        except Exception as error:
            return type(error).__name__

    for line in sys.stdin:
        request = json.loads(line)
        params = request.get("params") or {}
        if request["method"] == "env":
            result = sorted(name for name in os.environ if not name.startswith(("LC_", "__CF", "PYTHON", "ASTRAL_EXEC")))
        elif request["method"] == "connect":
            result = connect(params["host"])
        else:
            result = direct(params["port"])
        print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
    '''
)
CONFINED = pytest.mark.skipif(
    default_backend(Path("/")) is None,
    reason="a confinement backend is needed",
)
LISTED = "listed.example.test"
UNLISTED = "unlisted.example.test"
LIMITS = {"memory_bytes": 1 << 30, "processes": 1, "open_files": 256, "file_bytes": 1 << 24}


def spec_for(script: Path, *, env=None, hosts=(), ident="stdio-1", reads=None) -> LaunchSpec:
    python = os.path.realpath(sys.executable)
    policy = {
        "v": 1,
        "reads": reads if reads is not None else [os.path.realpath(sys.base_prefix), str(script.parent)],
        "workspace": None,
        "hosts": [{"host": host, "port": 443} for host in hosts],
        "limits": dict(LIMITS),
        "time": {"wall_ms": 60000},
    }
    return LaunchSpec(
        id=ident, command=python, args=("-I", "-B", str(script)), env=env or {},
        egress=tuple(hosts), policy=policy, policy_digest="sha256:" + "0" * 64,
    )


@pytest.fixture
def script(tmp_path):
    folder = tmp_path.resolve() / "server"
    folder.mkdir()
    path = folder / "server.py"
    path.write_text(SERVER)
    return path


@pytest.fixture
def sandbox(tmp_path):
    data = tmp_path.resolve() / "node-data"
    data.mkdir()
    scratch = tmp_path.resolve() / "scratch"
    scratch.mkdir()
    return ExecSandbox(data, scratch_root=str(scratch))


def ask(process, method, **params):
    process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode() + b"\n")
    process.stdin.flush()
    return json.loads(process.stdout.readline())["result"]


def refusing(monkeypatch, code="exec.backend_unavailable"):
    monkeypatch.setattr(node_stdio_exec, "select", lambda policy, facts, data_dir: Refusal(code, "m", "m"))


def test_a_machine_with_no_backend_installs_none(monkeypatch, tmp_path):
    refusing(monkeypatch)
    assert default_backend(tmp_path) is None
    assert NodeStdioLauncher(default_backend(tmp_path)).refusal == "connection.sandbox_unavailable"


def test_a_probe_that_errors_installs_none(monkeypatch, tmp_path):
    monkeypatch.setattr(node_stdio_exec, "probe", lambda: (_ for _ in ()).throw(OSError("no")))
    assert default_backend(tmp_path) is None


def test_a_refused_selection_starts_nothing_and_leaves_no_files(monkeypatch, sandbox, script, tmp_path):
    refusing(monkeypatch)
    with pytest.raises(ExecRefusal):
        sandbox.spawn(spec_for(script))
    assert list((tmp_path / "scratch").iterdir()) == []


def test_the_launcher_turns_a_refused_backend_into_the_sandbox_code(monkeypatch, sandbox, script):
    refusing(monkeypatch)
    launcher = NodeStdioLauncher(sandbox)
    with pytest.raises(LaunchRefused) as raised:
        launcher.start(spec_for(script))
    assert raised.value.code == "connection.sandbox_unavailable"
    assert launcher.running() == ()


def test_an_invalid_policy_is_refused_before_any_process(sandbox, script, tmp_path):
    bad = spec_for(script, hosts=("one.example.test",))
    bad.policy["limits"]["processes"] = 0
    with pytest.raises(ExecRefusal):
        sandbox.spawn(bad)
    assert list((tmp_path / "scratch").iterdir()) == []


def test_a_read_root_that_does_not_exist_is_refused(sandbox, script):
    with pytest.raises(OSError):
        sandbox.spawn(spec_for(script, reads=["/nonexistent-astral-root"]))


@CONFINED
def test_a_confined_server_gets_only_its_credential_variable(sandbox, script):
    process = sandbox.spawn(spec_for(script, env={"SERVICE_TOKEN": "t0ken-value"}))
    try:
        names = ask(process, "env")
        assert names == ["SERVICE_TOKEN"]
    finally:
        process.stop()


@CONFINED
def test_a_confined_server_reaches_only_its_listed_host_through_the_proxy(sandbox, script):
    process = sandbox.spawn(spec_for(script, hosts=(LISTED,)))
    try:
        assert ask(process, "connect", host=UNLISTED) == "403"
        assert ask(process, "connect", host=LISTED) != "403"
    finally:
        process.stop()


@CONFINED
def test_a_confined_server_cannot_reach_a_loopback_port_directly(sandbox, script):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    accepted = []
    threading.Thread(target=lambda: accepted.append(listener.accept()), daemon=True).start()
    process = sandbox.spawn(spec_for(script))
    try:
        assert ask(process, "direct", port=listener.getsockname()[1]) != "connected"
    finally:
        process.stop()
        listener.close()
    assert accepted == []


@CONFINED
def test_stopping_ends_the_server_and_removes_its_files(sandbox, script, tmp_path):
    process = sandbox.spawn(spec_for(script, env={"SERVICE_TOKEN": "x"}))
    assert ask(process, "env")
    assert list((tmp_path / "scratch").iterdir())
    process.stop()
    assert process.poll() is not None
    assert list((tmp_path / "scratch").iterdir()) == []


@CONFINED
def test_a_launcher_bridges_the_confined_server_and_stops_it(sandbox, script):
    import urllib.request

    launcher = NodeStdioLauncher(sandbox)
    url, bearer = launcher.start(spec_for(script, env={"SERVICE_TOKEN": "y"}))
    try:
        request = urllib.request.Request(
            url, data=json.dumps({"jsonrpc": "2.0", "id": 7, "method": "env", "params": {}}).encode(), method="POST"
        )
        request.add_header("Authorization", f"Bearer {bearer}")
        with urllib.request.urlopen(request, timeout=20) as response:
            assert "SERVICE_TOKEN" in json.loads(response.read())["result"]
    finally:
        launcher.stop_all()
    assert launcher.running() == ()


def test_the_credentials_travel_over_an_inherited_pipe_and_no_file_holds_them(monkeypatch, sandbox, script, tmp_path):
    monkeypatch.setattr(node_stdio_exec, "select", lambda policy, facts, data_dir: object())
    seen = {}

    class FakeProcess:
        stdin = stdout = None

        def __init__(self, argv, **kwargs):
            descriptor = int(argv[argv.index("--env-file") + 1].rsplit("/", 1)[1])
            seen["argv"] = argv
            seen["pass_fds"] = kwargs["pass_fds"]
            seen["env"] = kwargs["env"]
            seen["orig"] = descriptor
            seen["fd"] = os.dup(descriptor)
            seen["files"] = sorted(p.name for c in (tmp_path / "scratch").iterdir() for p in c.iterdir())
            self.pid = 1

        def poll(self):
            return 0

    monkeypatch.setattr(node_stdio_exec.subprocess, "Popen", FakeProcess)
    secret = "s3cr3t-" + "x" * 100000
    process = sandbox.spawn(spec_for(script, env={"SERVICE_TOKEN": secret}))
    chunks = []
    while True:
        block = os.read(seen["fd"], 65536)
        if not block:
            break
        chunks.append(block)
    os.close(seen["fd"])
    assert json.loads(b"".join(chunks)) == {"SERVICE_TOKEN": secret}
    assert seen["pass_fds"] == (seen["orig"],)
    assert seen["files"] == ["policy.json"]
    assert secret not in " ".join(seen["argv"]) and secret not in json.dumps(seen["env"])
    process._scratch.clear()


def test_a_node_mesh_without_an_explicit_sandbox_installs_the_default_backend(monkeypatch, tmp_path):
    from orchestrator import node_mesh

    chosen = object()
    asked = []
    monkeypatch.setattr(node_mesh.node_stdio_exec, "default_backend", lambda directory: asked.append(directory) or chosen)
    mesh = node_mesh.NodeMesh(data_dir=tmp_path, binding=object())
    assert asked == [tmp_path] and mesh._launcher._backend is chosen
    assert node_mesh.NodeMesh(data_dir=tmp_path, binding=object(), sandbox=None)._launcher.refusal == "connection.sandbox_unavailable"


def test_a_node_mesh_on_a_machine_with_no_backend_keeps_the_refusal(monkeypatch, tmp_path):
    from orchestrator import node_mesh

    refusing(monkeypatch)
    mesh = node_mesh.NodeMesh(data_dir=tmp_path, binding=object())
    assert mesh._launcher.refusal == "connection.sandbox_unavailable"
