"""Checks that a draft agent start on the personal and server profiles never reports confinement, states the unconfined launch in its log, status and creation card, binds the agent to loopback, keeps its limits in production whatever FF_SANDBOX_CODEGEN says, refuses to start at all when resource limit setup fails, and leaves nothing running when the node joins the PHI group during the launch.
Runs orchestrator/agent_lifecycle.py and orchestrator/agentic_creation.py against the fake store and supervisor from test_lets_agent_lifecycle_wiring.py on a node the owner excluded from the PHI group."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from orchestrator import agent_lifecycle, agentic_creation, node_mesh, phi_node_policy, sandbox
from orchestrator.agent_lifecycle import DraftLaunchRefused, draft_confinement
from tests.helpers.excluded_node import stand_in_mesh
from tests.test_lets_agent_lifecycle_wiring import _manager

PROFILES = ["personal", "server"]


pytestmark = pytest.mark.usefixtures("excluded_node")


@pytest.fixture(autouse=True)
def quick(monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr("orchestrator.agent_lifecycle.asyncio.sleep", no_sleep)


def prepared(tmp_path: Path, events: list[str]):
    manager = _manager(tmp_path, events)
    agent_dir = tmp_path / "agent_a"
    agent_dir.mkdir()
    (agent_dir / "agent_a_agent.py").write_text("print('ready')\n", encoding="utf-8")
    (agent_dir / "mcp_tools.py").write_text("TOOL_REGISTRY = {'read': {'scope': 'tools:read'}}\n", encoding="utf-8")
    return manager


@pytest.mark.parametrize("profile", PROFILES)
async def test_a_started_draft_agent_is_reported_unconfined_with_its_limits(tmp_path, monkeypatch, caplog, profile):
    monkeypatch.setenv("ASTRAL_NODE_PROFILE", profile)
    events: list[str] = []
    manager = prepared(tmp_path, events)
    with caplog.at_level(logging.WARNING, logger=agent_lifecycle.logger.name):
        result = await manager.start_draft_agent("draft-a")
    assert events.count("process_spawn") == 1
    assert result["confinement"]["confined"] is False
    assert result["confinement"]["resource_limits"] is True
    assert "runs unconfined" in result["confinement"]["notice"]
    assert any("runs unconfined" in record.getMessage() for record in caplog.records)


async def test_a_development_draft_agent_started_with_the_sandbox_flag_off_says_it_has_no_limits(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRAL_ENV", "development")
    monkeypatch.setenv("FF_SANDBOX_CODEGEN", "false")
    monkeypatch.setenv("SECRET_SAUCE_TOKEN", "kept-in-development")
    manager = prepared(tmp_path, [])
    result = await manager.start_draft_agent("draft-a")
    assert result["confinement"] == draft_confinement(False)
    assert "no resource limits" in result["confinement"]["notice"]
    env = manager.process_supervisor.calls[0]["env"]
    assert env["HOST"] == sandbox.LOOPBACK_HOST and env["SECRET_SAUCE_TOKEN"] == "kept-in-development"


async def test_a_production_draft_agent_keeps_its_limits_and_scrub_with_the_sandbox_flag_off(tmp_path, monkeypatch):
    monkeypatch.delenv("ASTRAL_ENV", raising=False)
    monkeypatch.setenv("FF_SANDBOX_CODEGEN", "false")
    monkeypatch.setenv("DB_PASSWORD", "plane-password")
    monkeypatch.setenv("AGENT_API_KEY", "registration-key")
    manager = prepared(tmp_path, [])
    result = await manager.start_draft_agent("draft-a")
    assert result["confinement"] == draft_confinement(True)
    call = manager.process_supervisor.calls[0]
    assert callable(call["preexec_fn"])
    assert call["env"]["HOST"] == sandbox.LOOPBACK_HOST
    assert "DB_PASSWORD" not in call["env"] and call["env"]["AGENT_API_KEY"] == "registration-key"


@pytest.mark.parametrize("profile", PROFILES)
async def test_a_failure_setting_up_resource_limits_refuses_the_start_on_every_profile(tmp_path, monkeypatch, profile):
    monkeypatch.setenv("ASTRAL_NODE_PROFILE", profile)

    def broken(_limits):
        raise OSError("limits unavailable")

    monkeypatch.setattr(sandbox, "make_preexec", broken)
    events: list[str] = []
    manager = prepared(tmp_path, events)
    with pytest.raises(DraftLaunchRefused):
        await manager.start_draft_agent("draft-a")
    assert "process_spawn" not in events and "lets_admit" not in events
    assert manager.db.draft["status"] == agent_lifecycle.ERROR
    assert "resource limits" in manager.db.draft["error_message"]


def test_the_launch_path_has_no_quiet_unsandboxed_fallback() -> None:
    assert "launching unsandboxed" not in Path(agent_lifecycle.__file__).read_text(encoding="utf-8")


def test_the_creation_card_carries_the_unconfined_notice() -> None:
    notice = draft_confinement(True)["notice"]
    card = agentic_creation.creation_card({"id": "d1", "agent_name": "A", "description": "d"}, {"status": "passed", "summary": "ok"}, note=notice)
    assert notice in str(card)


def joins_the_group_on_cue():
    state = {"member": False}
    mesh = stand_in_mesh()

    async def phi_member():
        return state["member"]

    mesh.phi_member = phi_member
    node_mesh.install(mesh)
    return state


async def test_a_node_that_joins_the_group_before_the_spawn_never_starts_the_draft(tmp_path):
    state = joins_the_group_on_cue()
    events: list[str] = []
    manager = prepared(tmp_path, events)
    admit = manager.governed_lifecycle.admit_new_runtime

    async def admit_then_join(**values):
        result = await admit(**values)
        state["member"] = True
        return result

    manager.governed_lifecycle.admit_new_runtime = admit_then_join
    with pytest.raises(phi_node_policy.AgentServerUnsupported) as refused:
        await manager.start_draft_agent("draft-a")
    assert refused.value.reason == "phi_node"
    assert "lets_admit" in events and "process_spawn" not in events
    assert manager._draft_processes == {}


async def test_a_node_that_joins_the_group_during_the_spawn_stops_the_draft_it_just_started(tmp_path):
    state = joins_the_group_on_cue()
    events: list[str] = []
    manager = prepared(tmp_path, events)
    spawn = manager.process_supervisor.spawn

    def spawn_then_join(**values):
        process = spawn(**values)
        state["member"] = True
        return process

    manager.process_supervisor.spawn = spawn_then_join
    with pytest.raises(phi_node_policy.AgentServerUnsupported):
        await manager.start_draft_agent("draft-a")
    assert events.count("process_spawn") == 1 and "process_terminate" in events
    assert manager._draft_processes == {}
