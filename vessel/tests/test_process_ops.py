# Tests for up, check, down and pair on a process deployment: no Docker call is made, data stays, and container
# folders behave as they did.

from __future__ import annotations

import argparse
import json
import os
import socket
import stat
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from conftest import needs_host_backend
from test_bundle import make_pack
from test_deploy import deployment_text

from astralvessel import cli, node, operate, process_ops
from astralvessel import runtime as rt
from astralvessel.probes import Report
from astralvessel.stack import Stack

MASTER = "m" * 20
DISTS = [{"name": n, "version": v} for n, v in (("cryptography", "46.0"), ("httpx", "0.28.1"), ("pydantic", "2.14.3"),
                                                  ("starlette", "1.10.0"), ("uvicorn", "0.54.0"))]


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Servers:
    def __init__(self, routes):
        self.servers = []
        self.routes = routes

    def listen(self, port, table):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.reply()

            def do_POST(self):
                self.reply()

            def reply(self):
                entry = table.get(self.path)
                if entry is None:
                    body, status = b"{}", 404
                else:
                    body, status = json.dumps(entry(self) if callable(entry) else entry).encode(), 200
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.servers.append(server)

    def close(self):
        for server in self.servers:
            server.shutdown()
            server.server_close()


@pytest.fixture
def pack(tmp_path):
    files = {"python/bin/python3.11": (b"#!/bin/sh\n", True), "app/backend/astral_exec/__init__.py": (b"", False)}
    descriptor, digest = make_pack(tmp_path / "pack-src", files=files)
    manifest = json.loads((tmp_path / "pack-src" / "node-pack.json").read_text())
    return tmp_path / "pack-src", manifest


@pytest.fixture
def docker(tmp_path, monkeypatch):
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    log = tmp_path / "docker.log"
    script = bin_dir / "docker"
    script.write_text(f'#!/bin/sh\necho "$@" >> {log}\nexit 1\n')
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return log


@pytest.fixture(autouse=True)
def installs(tmp_path, monkeypatch):
    monkeypatch.setattr(process_ops.bundles, "install_root", lambda: tmp_path / "bundles")


def args_for(folder, **over):
    values = dict(deployment=None, example=False, agent=None, image=None, port=None, upstream_url=None,
                  api_key=None, mount=None, timeout=5, bundle=None, bundle_sha256=None, foreground=True,
                  isolation_only=False, volumes=False, executor=False)
    return argparse.Namespace(**{**values, **over})


def patch_pack_dists(monkeypatch):
    real = process_ops.bundles.install

    def install(*a, **kw):
        installed = real(*a, **kw)
        object.__setattr__(installed, "manifest", {**installed.manifest, "installed_distributions": DISTS})
        return installed

    monkeypatch.setattr(process_ops.bundles, "install", install)


class FakeRuntime(rt.Runtime):
    servers: Servers | None = None
    started: list = []
    stopped: list = []

    @property
    def ports(self):
        return {**rt.ports_of(self.deployment), **self.fixed}

    def start(self, *, foreground=False):
        self.started.append(foreground)
        node_port = self.deployment.node.port
        self.servers.listen(node_port, {"/readyz": {"status": "ok", "db": "ok"}})
        for agent_id, agent in self.deployment.agents.items():
            self.servers.listen(self.fixed[f"sandbox-{agent_id}"], {"/health": {"confined": True,
                                                                              "profile": agent.fingerprint}})
        if self.deployment.agents:
            self.servers.listen(self.fixed["executor"], {"/health": {"ok": True}})

    def stop(self):
        self.stopped.append(True)

    def status(self):
        return {role: True for role in self.roles}


@needs_host_backend
def test_up_foreground_in_an_empty_folder_makes_no_docker_call_and_checks_in_order(tmp_path, pack, docker,
                                                                                   monkeypatch, capsys):
    patch_pack_dists(monkeypatch)
    folder = tmp_path / "work"
    folder.mkdir()
    monkeypatch.chdir(folder)
    monkeypatch.setenv("VESSEL_SANDBOX_TOKEN", MASTER)
    events, servers = [], Servers(None)
    port = free_port()
    FakeRuntime.servers, FakeRuntime.started, FakeRuntime.stopped, FakeRuntime.fixed = servers, [], [], {}
    original = process_ops.up

    def run_up(args, stack, deployment, **kw):
        original(args, stack, deployment, runtime_for=FakeRuntime, hold=lambda runtime: events.append("held"),
                 install_root=tmp_path / "bundles")

    monkeypatch.setattr(process_ops, "up", run_up)
    monkeypatch.setattr(process_ops, "run_process_probes", lambda runtime: events.append("probes") or Report(passed=3))
    monkeypatch.setattr(process_ops, "node_checks", lambda d, layout: events.append("node checks") or [])
    try:
        operate.up(args_for(folder, port=port, bundle=str(pack[0])))
    finally:
        servers.close()
    text = (folder / "deployment.toml").read_text()
    assert 'mode = "process"' in text and f"bundle = {json.dumps(str(pack[0]))}" in text
    assert events == ["probes", "node checks", "held"]
    assert FakeRuntime.started == [True] and FakeRuntime.stopped == [True]
    assert not docker.exists()
    assert not (folder / ".runtime" / "vessel.pid").exists()
    assert ".runtime/" in (folder / ".gitignore").read_text()
    assert "ready: node at" in capsys.readouterr().out


@needs_host_backend
def test_up_with_an_agent_waits_for_every_sandbox_and_the_executor(tmp_path, pack, docker, monkeypatch, package_dir):
    patch_pack_dists(monkeypatch)
    folder = tmp_path / "dep"
    folder.mkdir()
    (folder / "deployment.toml").write_text(
        '[runtime]\nmode = "process"\n[node]\nport = ' + str(free_port()) + "\n"
        + deployment_text(package_dir, mounts="", egress=False))
    monkeypatch.setenv("VESSEL_SANDBOX_TOKEN", MASTER)
    servers = Servers(None)
    FakeRuntime.servers, FakeRuntime.started, FakeRuntime.stopped = servers, [], []
    FakeRuntime.fixed = {"sandbox-guide": free_port(), "executor": free_port()}
    seen = []
    monkeypatch.setattr(process_ops, "run_process_probes", lambda runtime: Report(passed=1))
    monkeypatch.setattr(process_ops, "node_checks", lambda d, layout: [])
    monkeypatch.setattr(process_ops, "hold_until_signalled", lambda runtime: seen.append(1))
    try:
        process_ops.up(args_for(folder, bundle=str(pack[0])), Stack.at(folder / "deployment.toml"),
                       operate.load(folder / "deployment.toml"), runtime_for=FakeRuntime, hold=lambda r: seen.append(1),
                       install_root=tmp_path / "bundles")
    finally:
        servers.close()
    assert seen == [1] and not docker.exists()


@needs_host_backend
def test_up_names_a_role_that_never_became_ready(tmp_path, pack, docker, monkeypatch, capsys):
    patch_pack_dists(monkeypatch)
    folder = tmp_path / "work"
    folder.mkdir()
    (folder / "deployment.toml").write_text('[runtime]\nmode = "process"\n[node]\nport = ' + str(free_port()) + "\n")
    monkeypatch.setenv("VESSEL_SANDBOX_TOKEN", MASTER)
    FakeRuntime.servers, FakeRuntime.started, FakeRuntime.stopped, FakeRuntime.fixed = Servers(None), [], [], {}
    monkeypatch.setattr(FakeRuntime, "start", lambda self, foreground=False: None)
    with pytest.raises(SystemExit, match="not ready"):
        process_ops.up(args_for(folder, bundle=str(pack[0]), timeout=0.2), Stack.at(folder / "deployment.toml"),
                       operate.load(folder / "deployment.toml"), runtime_for=FakeRuntime,
                       install_root=tmp_path / "bundles")
    assert "not ready after" in capsys.readouterr().out and FakeRuntime.stopped == [True]


@needs_host_backend
def test_no_pack_gives_a_pointed_error(tmp_path, docker, monkeypatch):
    folder = tmp_path / "work"
    folder.mkdir()
    (folder / "deployment.toml").write_text('[runtime]\nmode = "process"\n')
    monkeypatch.setattr(process_ops.bundles, "pinned", lambda platform, pins=None: None)
    with pytest.raises(SystemExit, match="--bundle"):
        operate.up(args_for(folder, deployment=str(folder / "deployment.toml")))
    assert not docker.exists()


def test_up_in_a_folder_with_an_image_option_is_refused(tmp_path, docker, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit, match="container mode"):
        operate.up(args_for(tmp_path, image="x"))
    assert not (tmp_path / "deployment.toml").exists()


def process_folder(tmp_path):
    folder = tmp_path / "proc"
    (folder / ".runtime" / "node-data" / "node").mkdir(parents=True)
    (folder / ".runtime" / "node-data" / "node" / "plane.sqlite3").write_text("data")
    (folder / ".runtime" / "executor").mkdir()
    (folder / ".runtime" / "executor" / "spent.db").write_text("kept")
    (folder / ".generated").mkdir()
    (folder / ".generated" / "node.env").write_text("K=v\n")
    (folder / "deployment.toml").write_text('[runtime]\nmode = "process"\n')
    return folder


@needs_host_backend
def test_check_and_down_make_no_docker_call_and_down_keeps_data(tmp_path, pack, docker, monkeypatch):
    patch_pack_dists(monkeypatch)
    folder = process_folder(tmp_path)
    target = str(folder / "deployment.toml")
    calls = []
    monkeypatch.setattr(process_ops, "run_process_probes", lambda runtime: Report(passed=2))
    monkeypatch.setattr(process_ops, "node_checks", lambda d, layout: calls.append("node") or [])
    operate.check(args_for(folder, deployment=target, bundle=str(pack[0])))
    monkeypatch.setattr(process_ops, "node_checks", lambda d, layout: ["answers /readyz"])
    with pytest.raises(SystemExit):
        operate.check(args_for(folder, deployment=target, bundle=str(pack[0])))
    operate.down(args_for(folder, deployment=target))
    assert (folder / ".runtime" / "node-data" / "node" / "plane.sqlite3").read_text() == "data"
    assert (folder / ".runtime" / "executor" / "spent.db").read_text() == "kept"
    assert not docker.exists() and calls == ["node"]


def test_down_volumes_deletes_only_the_node_data(tmp_path, docker):
    folder = process_folder(tmp_path)
    operate.down(args_for(folder, deployment=str(folder / "deployment.toml"), volumes=True))
    assert not (folder / ".runtime" / "node-data").exists()
    assert (folder / ".runtime" / "executor" / "spent.db").read_text() == "kept"
    assert (folder / ".generated" / "node.env").exists() and not docker.exists()


@needs_host_backend
def test_down_stops_installed_units_and_a_supervisor_by_its_pid_file(tmp_path, docker, monkeypatch):
    folder = process_folder(tmp_path)
    child = subprocess.Popen(["sleep", "30"], start_new_session=True)
    (folder / ".runtime" / "vessel.pid").write_text(f"{child.pid}\n")
    operate.down(args_for(folder, deployment=str(folder / "deployment.toml")))
    assert child.poll() is None
    child.kill()
    child.wait()
    ran = []
    from astralvessel.units import Manager
    monkeypatch.setattr(Manager, "_file", lambda self, role: folder / "deployment.toml")
    monkeypatch.setattr(Manager, "stop", lambda self, roles: ran.append(roles))
    operate.down(args_for(folder, deployment=str(folder / "deployment.toml")))
    assert ran == [["node"]]


def container_folder(tmp_path, runtime=True):
    folder = tmp_path / "box"
    folder.mkdir()
    head = '[runtime]\nmode = "container"\n' if runtime else ""
    (folder / "deployment.toml").write_text(head)
    (folder / "compose.generated.yml").write_text("name: old\n")
    return folder


@pytest.mark.parametrize("runtime", [True, False])
def test_a_container_folder_answers_check_and_down_through_compose(tmp_path, monkeypatch, runtime):
    folder = container_folder(tmp_path, runtime)
    calls = []
    monkeypatch.setattr(Stack, "compose", lambda self, *a, **kw: calls.append(a))
    monkeypatch.setattr(operate, "run_probes", lambda *a: Report(passed=1))
    monkeypatch.setattr(operate, "node_checks", lambda *a: [])
    target = str(folder / "deployment.toml")
    operate.check(args_for(folder, deployment=target))
    operate.down(args_for(folder, deployment=target, volumes=True))
    assert calls == [("down", "-v")]
    assert operate.mode_for(Stack.at(target), operate.load(Path(target))) == "container"


def test_up_refuses_container_data_and_writes_nothing(tmp_path):
    folder = container_folder(tmp_path, runtime=False)
    (folder / ".generated").mkdir()
    (folder / ".generated" / "node.env").write_text("secret=1\n")
    before = {str(p.relative_to(folder)): p.read_bytes() for p in folder.rglob("*") if p.is_file()}
    with pytest.raises(SystemExit, match="deployment.container_data"):
        operate.up(args_for(folder, deployment=str(folder / "deployment.toml")))
    assert {str(p.relative_to(folder)): p.read_bytes() for p in folder.rglob("*") if p.is_file()} == before


@needs_host_backend
def test_the_data_checks_read_host_paths(tmp_path, monkeypatch, capsys):
    folder = process_folder(tmp_path)
    deployment = operate.load(folder / "deployment.toml")
    layout = process_ops.Layout(folder)
    monkeypatch.setattr(node, "health", lambda n: "ok")
    monkeypatch.setattr(node, "fetch_descriptor", lambda n: {})
    monkeypatch.setattr(node, "descriptor_problem", lambda d: None)
    monkeypatch.setattr(node, "owner_binding", lambda d: {})
    root = layout.node_data / "node"
    root.chmod(0o700)
    (root / "plane.sqlite3").chmod(0o600)
    assert process_ops.node_checks(deployment, layout) == []
    (root / "plane.sqlite3").chmod(0o644)
    root.chmod(0o755)
    failed = process_ops.node_checks(deployment, layout)
    assert failed == ["keeps its data directory private",
                      "keeps its database in a SQLite file readable only by the node"]
    monkeypatch.setattr(node, "owner_binding", lambda d: {"code": "node.legacy_owner"})
    assert "carries no legacy owner" in process_ops.node_checks(deployment, layout)


def test_pair_reads_the_secret_from_the_data_folder_and_asks_the_nodes_loopback(tmp_path, monkeypatch, capsys):
    folder = process_folder(tmp_path)
    port = free_port()
    (folder / "deployment.toml").write_text(f'[runtime]\nmode = "process"\n[node]\nport = {port}\n')
    (folder / ".runtime" / "node-data" / "node" / "pairing-secret").write_text("s3cret\n")
    seen = []

    def offer(handler):
        seen.append(handler.headers.get("Astral-Pairing-Secret"))
        return {"state": "offer", "code": "1234 5678", "payload": "PAY", "expires_ms": 9}

    servers = Servers(None)
    servers.listen(port, {"/api/node/mesh/offer": offer})
    monkeypatch.setattr(node, "describe_offer", lambda reply, n: f"code {reply['code']}")
    try:
        operate.pair(argparse.Namespace(deployment=str(folder / "deployment.toml"), executor=False))
    finally:
        servers.close()
    assert seen == ["s3cret"] and "code 1234 5678" in capsys.readouterr().out


def test_pair_executor_prints_the_offer(tmp_path, package_dir, capsys, monkeypatch):
    folder = process_folder(tmp_path)
    port = free_port()
    (folder / "deployment.toml").write_text('[runtime]\nmode = "process"\n' + deployment_text(
        package_dir, mounts="", egress=False))
    (folder / ".generated" / "executor.env").write_text("VESSEL_EXECUTOR_TOKEN=tok\n")
    monkeypatch.setattr(rt, "EXECUTOR_PORT", port)
    monkeypatch.setattr(process_ops, "ports_of", lambda d: {"executor": port})
    servers = Servers(None)
    servers.listen(port, {"/pairing": {"code": "9999 0000", "payload": "XP"}})
    try:
        operate.pair(argparse.Namespace(deployment=str(folder / "deployment.toml"), executor=True))
    finally:
        servers.close()
    assert "9999 0000" in capsys.readouterr().out


def test_the_command_line_takes_bundle_options():
    parsed = cli.parser().parse_args(["up", "--bundle", "/p", "--bundle-sha256", "a" * 64, "--foreground"])
    assert (parsed.bundle, parsed.bundle_sha256, parsed.foreground) == ("/p", "a" * 64, True)
    assert cli.parser().parse_args(["up"]).foreground is False
