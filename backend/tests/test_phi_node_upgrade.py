"""Proves the node refuses a request turn it cannot protect with the code the client maps to the PHI adoption card, and keeps generated and draft agents off a PHI member node at request time, boot and membership change.
It drives the real kernel binding through the dev mesh and the bridge, and checks start.py, node_api.py and the exec-policy contract against orchestrator/phi_node_policy.py."""

from __future__ import annotations

import asyncio
import io
import json
import subprocess
import urllib.error
from pathlib import Path
from types import SimpleNamespace

import astral_kernel as ak
import pytest

import start
from orchestrator import node_api, node_mesh, phi_node_policy
from orchestrator.agent_lifecycle import AgentLifecycleManager
from orchestrator.local_agents import FIRST_PARTY_AGENT_DIRS
from orchestrator.node_mesh_calls import OrchestratorCalls
from tests.helpers.dev_mesh import dev_node
from tests.helpers.dev_mesh import until as wait_until
from tests.test_node_mesh_kernel_integration import paired, until  # noqa: F401
from tests.test_node_mesh_kernel_request_tools import ask, setup_native
from tests.test_node_mesh_peer_tools import FakeModel, model_server, node_host  # noqa: F401
from tests.test_node_mesh_turn_tools import dice_grant, grant, turn_host  # noqa: F401

CONTRACT = Path(__file__).resolve().parents[2] / "specs/106-sign-in-free-clients/contracts/vectors/exec-policy-v1.json"


async def settled(condition, what):
    def safe():
        try:
            return condition()
        except ak.KernelError:
            return False

    await wait_until(safe, what)


async def test_an_unadopted_node_ends_a_request_with_phi_group_not_ready_and_runs_no_model_turn(turn_host, model_server, tmp_path, monkeypatch):  # noqa: F811
    host = turn_host
    async with paired(tmp_path, monkeypatch, creator=host.mesh_creator, calls=OrchestratorCalls(host.orch), history=host.orch.history,
                      kernel_answers=True, adopt=False) as (node, laptop, device, bridge):
        await setup_native(host, node, laptop, device, bridge, model_server, monkeypatch)
        await grant(node, laptop, dice_grant(host))
        assert await node.phi_member() is False
        _, request = await ask(node, laptop, device)
        await until(lambda: laptop.request(request).state == ak.RequestState.REFUSED, "the node to refuse the unprotected turn")
        assert laptop.request(request).code == "phi.group_not_ready"
        assert not FakeModel.seen and not any(host.dispatched.values())


def test_a_sink_failure_that_names_a_phi_code_surfaces_that_code():
    sink = ak.KernelError.State(code="engine.sink", message="engine.sink: event sink failed: phi.group_not_ready: phi.group_not_ready")
    other = ak.KernelError.State(code="engine.sink", message="engine.sink: event sink failed: disk full")
    excluded = ak.KernelError.State(code="engine.sink", message="engine.sink: event sink failed: phi.device_excluded: x")
    assert node_mesh._code_with_phi(sink) == "phi.group_not_ready"
    assert node_mesh._code_with_phi(excluded) == "phi.device_excluded"
    assert node_mesh._code_with_phi(other) == "engine.sink"
    assert node_mesh._code_with_phi(ak.KernelError.State(code="phi.group_not_ready", message="m")) == "phi.group_not_ready"


async def test_the_refusal_code_follows_the_nodes_standing_in_the_group(tmp_path, monkeypatch):
    async with dev_node(tmp_path, monkeypatch, adopt=False) as mesh:
        assert await mesh.node.phi_refusal() == "phi.group_not_ready"
        await settled(mesh.owner.phi_markers_synced, "the PHI markers to sync")
        mesh.owner.adopt_phi_group()
        await settled(mesh.node.phi_member, "the node to join the PHI group")
        assert await mesh.node.phi_refusal() is None
        mesh.owner.exclude_phi_device(mesh.node_device)
        await settled(lambda: mesh.node.kernel.phi_group().local_code, "the exclusion to reach the node")
        assert await mesh.node.phi_refusal() == "phi.device_excluded"


class Lifecycle:
    def __init__(self):
        self.stopped = 0

    async def stop_running_drafts(self):
        self.stopped += 1
        return ["draft"]


async def test_the_guard_stops_running_drafts_when_the_node_joins_or_rejoins_the_group(tmp_path, monkeypatch):
    async with dev_node(tmp_path, monkeypatch, adopt=False) as mesh:
        node_mesh.install(mesh.node)
        lifecycle = Lifecycle()
        guard = phi_node_policy.DraftGuard(mesh.node, lifecycle)
        try:
            guard.start()
            await asyncio.sleep(0.5)
            assert lifecycle.stopped == 0
            await settled(mesh.owner.phi_markers_synced, "the PHI markers to sync")
            mesh.owner.adopt_phi_group()
            await settled(lambda: lifecycle.stopped >= 1, "the draft agents to stop when the node joins")
            mesh.owner.exclude_phi_device(mesh.node_device)
            await settled(lambda: mesh.node.kernel.phi_group().local_code, "the exclusion to reach the node")
            await asyncio.sleep(0.5)
            before = lifecycle.stopped
            mesh.owner.include_phi_device(mesh.node_device)
            await settled(lambda: lifecycle.stopped > before, "the draft agents to stop when the node rejoins")
        finally:
            await guard.stop()
            node_mesh.install(None)


async def test_the_guard_survives_a_failing_lifecycle_and_ignores_changes_after_its_loop_closed(caplog):
    class Failing:
        async def stop_running_drafts(self):
            raise RuntimeError("boom")

    async def status():
        return {"enrolled": True}

    async def member():
        return True

    mesh = SimpleNamespace(started=True, status=status, phi_member=member, subscribe=lambda callback: None)
    node_mesh.install(mesh)
    try:
        guard = phi_node_policy.DraftGuard(mesh, Failing())
        await guard.enforce()
        assert "phi_node_guard_failed" in caplog.text
        guard._on_change(object())
    finally:
        node_mesh.install(None)


async def test_the_lifecycle_stops_every_running_draft():
    manager = object.__new__(AgentLifecycleManager)
    manager._draft_processes = {"a": object(), "b": object()}
    stopped = []

    async def stop(draft_id):
        stopped.append(draft_id)

    manager.stop_draft_agent = stop
    assert await manager.stop_running_drafts() == ["a", "b"] and stopped == ["a", "b"]


async def test_the_agent_server_route_reports_the_policy_to_a_loopback_caller(tmp_path, monkeypatch):
    local = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"), headers={})
    response = SimpleNamespace(headers={})
    assert await node_api.get_agent_server_policy(local, response) == {"allowed": True}
    async with dev_node(tmp_path, monkeypatch) as mesh:
        node_mesh.install(mesh.node)
        try:
            assert await node_api.get_agent_server_policy(local, response) == {
                "allowed": False, "code": "exec.agent_server_unsupported", "reason": "phi_node", "message": phi_node_policy.MESSAGE,
            }
            with pytest.raises(Exception) as remote:
                await node_api.get_agent_server_policy(SimpleNamespace(client=SimpleNamespace(host="10.0.0.9"), headers={}), response)
            assert getattr(remote.value, "status_code", None) == 403
        finally:
            node_mesh.install(None)


def fake_reply(payload):
    return lambda url, timeout: io.BytesIO(json.dumps(payload).encode())


def test_start_py_launches_generated_agents_only_where_the_node_allows_them(monkeypatch):
    monkeypatch.setattr(start.urllib.request, "urlopen", fake_reply({"allowed": False}))
    assert start._agent_servers_refused(8001) is True
    monkeypatch.setattr(start.urllib.request, "urlopen", fake_reply({"allowed": True}))
    assert start._agent_servers_refused(8001) is False

    def http(code):
        def raise_it(url, timeout):
            raise urllib.error.HTTPError(url, code, "x", {}, None)
        return raise_it

    monkeypatch.setattr(start.urllib.request, "urlopen", http(404))
    assert start._agent_servers_refused(8001) is False
    monkeypatch.setattr(start.urllib.request, "urlopen", http(403))
    assert start._agent_servers_refused(8001) is True

    def down(url, timeout):
        raise OSError("down")

    monkeypatch.setattr(start.urllib.request, "urlopen", down)
    assert start._agent_servers_refused(8001) is False


def test_the_first_party_agent_set_is_exactly_the_tracked_agent_directories():
    root = Path(__file__).resolve().parents[1]
    tracked = subprocess.run(["git", "ls-files", "agents"], cwd=root, check=True, capture_output=True, text=True).stdout.split()
    directories = {path.split("/")[1] for path in tracked if path.count("/") >= 2} - {"tests"}
    assert directories == set(FIRST_PARTY_AGENT_DIRS)


def test_the_policy_message_is_the_contract_message_and_the_reason_is_pinned():
    constants = json.loads(CONTRACT.read_text(encoding="utf-8"))["constants"]
    assert constants["agent_server_reasons"] == [phi_node_policy.REASON]
    assert constants["messages"][phi_node_policy.REASON] == phi_node_policy.MESSAGE
    assert phi_node_policy.CODE in constants["messages"]
