"""Proves the node refuses a request turn it cannot protect with the code the client maps to the PHI adoption card, and keeps generated and draft agents off every node the owner did not exclude from the PHI group at request time, boot and membership change.
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
from astral_exec.constants import MESSAGES
from orchestrator import node_api, node_mesh, phi_node_policy, sandbox
from orchestrator.agent_lifecycle import AgentLifecycleManager
from orchestrator.local_agents import FIRST_PARTY_AGENT_DIRS
from tests.helpers.dev_mesh import dev_node
from tests.helpers.excluded_node import stand_in_mesh
from tests.helpers.dev_mesh import until as wait_until
from tests.test_node_mesh_kernel_integration import paired, until  # noqa: F401
from tests.test_node_mesh_peer_tools import FakeModel, model_server, node_host  # noqa: F401

CONTRACT = Path(__file__).resolve().parents[2] / "specs/106-sign-in-free-clients/contracts/vectors/exec-policy-v1.json"


async def settled(condition, what):
    def safe():
        try:
            return condition()
        except ak.KernelError:
            return False

    await wait_until(safe, what)


async def test_an_unadopted_node_answers_a_plain_turn_and_ends_an_uncertain_one_with_phi_group_not_ready(model_server, tmp_path, monkeypatch):  # noqa: F811
    FakeModel.tool = None
    async with dev_node(tmp_path, monkeypatch, adopt=False) as mesh:
        owner_device = (await mesh.owner.kernel.mesh_status()).device_id
        mesh.owner.kernel.set_model_provider(ak.ModelProviderConfig(
            name="local", base_url=f"http://127.0.0.1:{model_server}/v1", models=["model-1"], api_keys=["fixture-key"], fallback_order=0,
            self_hosted=True, strict_tools=False, recipients=[owner_device, mesh.node_device],
        ))
        await settled(lambda: any(p.name == "local" for p in mesh.node.kernel.model_providers()), "the provider to reach the node")
        assert await mesh.node.phi_member() is False

        async def request(text):
            chat, request_id = mesh.owner.send_request(mesh.node_device, text)
            await settled(lambda: mesh.node.kernel.request(request_id), "the request to reach the node")
            return chat, request_id

        _, plain = await request("Please summarise the weekly report")
        assert (await asyncio.wait_for(mesh.node.answer_request(plain, mesh.node.kernel), 30)).status == "completed"
        _, uncertain = await request("Appointment on 2024-03-15 was moved")
        with pytest.raises(node_mesh.NodeMeshError) as refused:
            await mesh.node.answer_request(uncertain, mesh.node.kernel)
        assert refused.value.code == "phi.group_not_ready"
        seen = len(FakeModel.seen)
        assert seen == 1


def test_a_sink_failure_that_names_a_phi_code_surfaces_that_code():
    sink = ak.KernelError.State(code="engine.sink", message="engine.sink: event sink failed: phi.group_not_ready: phi.group_not_ready")
    other = ak.KernelError.State(code="engine.sink", message="engine.sink: event sink failed: disk full")
    excluded = ak.KernelError.State(code="engine.sink", message="engine.sink: event sink failed: phi.device_excluded: x")
    assert node_mesh._code_with_phi(sink) == "phi.group_not_ready"
    assert node_mesh._code_with_phi(excluded) == "phi.device_excluded"
    assert node_mesh._code_with_phi(other) == "engine.sink"
    assert node_mesh._code_with_phi(ak.KernelError.State(code="phi.group_not_ready", message="m")) == "phi.group_not_ready"


class Lifecycle:
    def __init__(self):
        self.stopped = 0
        self.relaunched = 0

    async def stop_running_drafts(self):
        self.stopped += 1
        return ["draft"]

    async def relaunch_live_agents(self):
        self.relaunched += 1
        return ["live"]


async def test_the_guard_relaunches_live_agents_only_once_the_owner_excludes_the_node_and_stops_them_when_it_rejoins(tmp_path, monkeypatch):
    async with dev_node(tmp_path, monkeypatch, adopt=False) as mesh:
        node_mesh.install(mesh.node)
        lifecycle = Lifecycle()
        guard = phi_node_policy.DraftGuard(mesh.node, lifecycle)
        try:
            guard.start()
            await settled(lambda: lifecycle.stopped >= 1, "the guard to refuse a node that was never excluded")
            assert lifecycle.relaunched == 0
            await settled(mesh.owner.phi_markers_synced, "the PHI markers to sync")
            mesh.owner.adopt_phi_group()
            await settled(mesh.node.phi_member, "the node to join the PHI group")
            assert lifecycle.relaunched == 0
            mesh.owner.exclude_phi_device(mesh.node_device)
            await settled(lambda: lifecycle.relaunched == 1, "the live agents to relaunch once the node is excluded")
            await asyncio.sleep(0.5)
            assert lifecycle.relaunched == 1
            before = lifecycle.stopped
            mesh.owner.include_phi_device(mesh.node_device)
            await settled(lambda: lifecycle.stopped > before, "the draft agents to stop when the node rejoins")
            assert lifecycle.relaunched == 1
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
    assert await node_api.get_agent_server_policy(local, response) == {
        "allowed": False, "code": "exec.agent_server_unsupported", "reason": "not_excluded", "message": MESSAGES["not_excluded"],
    }
    async with dev_node(tmp_path, monkeypatch) as mesh:
        node_mesh.install(mesh.node)
        try:
            assert await node_api.get_agent_server_policy(local, response) == {
                "allowed": False, "code": "exec.agent_server_unsupported", "reason": "phi_node", "message": MESSAGES["phi_node"],
            }
            mesh.owner.exclude_phi_device(mesh.node_device)
            await settled(lambda: mesh.node.kernel.phi_group().local_code, "the exclusion to reach the node")
            assert await node_api.get_agent_server_policy(local, response) == {"allowed": True}
            with pytest.raises(Exception) as remote:
                await node_api.get_agent_server_policy(SimpleNamespace(client=SimpleNamespace(host="10.0.0.9"), headers={}), response)
            assert getattr(remote.value, "status_code", None) == 403
        finally:
            node_mesh.install(None)


def fake_reply(payload):
    return lambda url, timeout: io.BytesIO(json.dumps(payload).encode())


def test_start_py_launches_generated_agents_only_after_the_node_answers_allowed(monkeypatch):
    monkeypatch.setattr(start.urllib.request, "urlopen", fake_reply({"allowed": True}))
    assert start._agent_servers_refused(8001) is False
    for answer in ({"allowed": False}, {"allowed": "true"}, {}, ["allowed"]):
        monkeypatch.setattr(start.urllib.request, "urlopen", fake_reply(answer))
        assert start._agent_servers_refused(8001) is True

    def http(code):
        def raise_it(url, timeout):
            raise urllib.error.HTTPError(url, code, "x", {}, None)
        return raise_it

    for code in (404, 403, 500):
        monkeypatch.setattr(start.urllib.request, "urlopen", http(code))
        assert start._agent_servers_refused(8001) is True

    def down(url, timeout):
        raise OSError("down")

    monkeypatch.setattr(start.urllib.request, "urlopen", down)
    assert start._agent_servers_refused(8001) is True
    monkeypatch.setattr(start.urllib.request, "urlopen", lambda url, timeout: io.BytesIO(b"not json"))
    assert start._agent_servers_refused(8001) is True


def test_the_first_party_agent_set_is_exactly_the_tracked_agent_directories():
    root = Path(__file__).resolve().parents[1]
    tracked = subprocess.run(["git", "ls-files", "agents"], cwd=root, check=True, capture_output=True, text=True).stdout.split()
    directories = {path.split("/")[1] for path in tracked if path.count("/") >= 2} - {"tests"}
    assert directories == set(FIRST_PARTY_AGENT_DIRS)


def test_the_policy_message_is_the_contract_message_and_the_reason_is_pinned():
    constants = json.loads(CONTRACT.read_text(encoding="utf-8"))["constants"]
    assert constants["agent_server_reasons"] == [phi_node_policy.PHI_NODE, phi_node_policy.NOT_EXCLUDED] == ["phi_node", "not_excluded"]
    for reason in constants["agent_server_reasons"]:
        assert phi_node_policy.AgentServerUnsupported(reason).detail() == {
            "code": phi_node_policy.CODE, "reason": reason, "message": constants["messages"][reason],
        }
    assert phi_node_policy.CODE in constants["messages"]


class Proc:
    def __init__(self):
        self.stopped = False

    def poll(self):
        return 0 if self.stopped else None

    def terminate(self, reason):
        self.stopped = True


class Supervisor:
    def __init__(self):
        self.spawned = []
        self.calls = []

    def spawn(self, **kwargs):
        proc = Proc()
        self.spawned.append((kwargs["owner"].owner_id, kwargs["argv"][-1], proc))
        self.calls.append(kwargs)
        return proc


@pytest.fixture
def agent_dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(start, "agent_launch", lambda path: sandbox.agent_launch(str(tmp_path / path.strip("/"))))
    for name in ("made", "held"):
        (tmp_path / "agents" / name).mkdir(parents=True)


def test_generated_agents_started_before_the_node_joined_the_group_are_stopped_and_return_when_it_leaves(agent_dirs):
    supervisor = Supervisor()
    agents = start.GeneratedAgents(supervisor, "python")
    agents.register("made", "/agents/made/made_agent.py", "/agents/made", 8010)
    agents.register("held", "/agents/held/held_agent.py", "/agents/held", 8011)
    agents.hold("held")
    agents.launch("made")
    agents.reconcile(False)
    assert [(name, port) for name, port, _ in supervisor.spawned] == [("made", "8010"), ("held", "8011")]
    first = supervisor.spawned[0][2]
    agents.reconcile(True)
    assert first.stopped and supervisor.spawned[1][2].stopped
    agents.reconcile(True)
    assert len(supervisor.spawned) == 2
    agents.reconcile(False)
    assert [(name, port) for name, port, _ in supervisor.spawned[2:]] == [("made", "8010"), ("held", "8011")]
    agents.reconcile(False)
    assert len(supervisor.spawned) == 4


def test_a_generated_agent_that_crashed_is_not_restarted_by_the_policy_check(agent_dirs):
    supervisor = Supervisor()
    agents = start.GeneratedAgents(supervisor, "python")
    agents.register("made", "/agents/made/made_agent.py", "/agents/made", 8010)
    agents.launch("made")
    supervisor.spawned[0][2].stopped = True
    agents.reconcile(False)
    assert len(supervisor.spawned) == 1


def test_a_generated_agent_gets_the_draft_launch_with_limits_a_scrubbed_environment_and_loopback(agent_dirs, monkeypatch, capsys):
    monkeypatch.delenv("ASTRAL_ENV", raising=False)
    monkeypatch.setenv("FF_SANDBOX_CODEGEN", "false")
    monkeypatch.setenv("HOST", "0.0.0.0")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-node")
    monkeypatch.setenv("DB_PASSWORD", "plane-password")
    supervisor = Supervisor()
    agents = start.GeneratedAgents(supervisor, "python")
    agents.register("made", "/agents/made/made_agent.py", "/agents/made", 8010)
    agents.launch("made")
    call = supervisor.calls[0]
    assert call["env"]["HOST"] == "127.0.0.1" and callable(call["preexec_fn"])
    assert "OPENAI_API_KEY" not in call["env"] and "DB_PASSWORD" not in call["env"]
    assert call["cwd"] == "/agents/made"
    assert "runs unconfined with process resource limits only" in capsys.readouterr().out


def test_a_generated_agent_whose_limits_cannot_be_set_up_is_refused_and_never_retried(monkeypatch, capsys):
    def broken(path):
        raise OSError("limits unavailable")

    monkeypatch.setattr(start, "agent_launch", broken)
    supervisor = Supervisor()
    agents = start.GeneratedAgents(supervisor, "python")
    agents.register("made", "/agents/made/made_agent.py", "/agents/made", 8010)
    agents.launch("made")
    agents.reconcile(False)
    agents.reconcile(False)
    assert supervisor.spawned == []
    assert "Refusing made: its resource limits could not be set up (OSError)" in capsys.readouterr().out


class LiveStore:
    def __init__(self, rows, broken=False):
        self.rows = rows
        self.broken = broken

    def list_relaunchable_drafts(self):
        if self.broken:
            raise RuntimeError("store down")
        return self.rows


async def test_the_lifecycle_relaunches_live_agents_that_are_not_running_and_reports_each_failure(caplog):
    manager = object.__new__(AgentLifecycleManager)
    rows = [{"id": "running", "agent_name": "A"}, {"id": "fresh", "agent_name": "B"}, {"id": "refused", "agent_name": "C"}]
    manager.draft_store = LiveStore(rows)
    manager._draft_processes = {"running": Proc()}
    started = []

    async def start_draft_agent(draft_id, align_scopes=True):
        assert align_scopes is False
        if draft_id == "refused":
            raise phi_node_policy.AgentServerUnsupported("not_excluded")
        started.append(draft_id)

    manager.start_draft_agent = start_draft_agent
    assert await manager.relaunch_live_agents() == ["fresh"] and started == ["fresh"]
    assert "relaunch: C (refused) failed" in caplog.text
    manager.draft_store = LiveStore(rows, broken=True)
    assert await manager.relaunch_live_agents() == []
    assert "could not list live generated agents" in caplog.text


async def test_the_guard_treats_an_unreadable_policy_as_a_refusal_and_stops_running_drafts(caplog):
    async def broken_status():
        raise RuntimeError("status exploded")

    mesh = stand_in_mesh()
    mesh.status = broken_status
    node_mesh.install(mesh)
    try:
        lifecycle = Lifecycle()
        guard = phi_node_policy.DraftGuard(mesh, lifecycle)
        guard._allowed = True
        await guard.enforce()
        assert lifecycle.stopped == 1 and guard._allowed is False
        assert "phi_node_guard_policy_unreadable" in caplog.text
    finally:
        node_mesh.install(None)


async def test_one_draft_that_will_not_stop_does_not_spare_the_others(caplog):
    manager = object.__new__(AgentLifecycleManager)
    manager._draft_processes = {"a": object(), "b": object(), "c": object()}
    tried = []

    async def stop(draft_id):
        tried.append(draft_id)
        if draft_id == "b":
            raise RuntimeError("stuck")

    manager.stop_draft_agent = stop
    assert await manager.stop_running_drafts() == ["a", "c"]
    assert tried == ["a", "b", "c"]
    assert "stop_running_drafts: b could not be stopped" in caplog.text


async def test_the_guard_timer_notices_a_mesh_that_stopped_without_a_change_event():
    mesh = stand_in_mesh()
    mesh.subscribe = lambda callback: None
    node_mesh.install(mesh)
    lifecycle = Lifecycle()
    guard = phi_node_policy.DraftGuard(mesh, lifecycle, recheck_s=0.01)
    try:
        guard.start()
        await settled(lambda: lifecycle.relaunched == 1, "the first answer to relaunch live agents")
        assert lifecycle.stopped == 0
        mesh.started = False
        await settled(lambda: lifecycle.stopped >= 1, "the timer to stop the drafts on a stopped mesh")
        assert lifecycle.relaunched == 1
    finally:
        await guard.stop()
        node_mesh.install(None)
    assert guard._allowed is False and guard._ticker is None
