"""Exercises the orchestrator-side Cresco seams: flag-gated registration, safe-seed
exclusion, the executor's security-flag hard block in the real gate stack, the
confirmation policy over dispatch tools, unattended-channel refusals, taint posture and
subprocess start gating.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.cresco import cresco_agent, mcp_tools
from agents.cresco.cresco_agent import CrescoAgent
from agents.cresco.mcp_server import MCPServer
from orchestrator import local_agents, remote_confirmation, taint
from orchestrator.local_agents import BUILT_IN_AGENT_DIRS, FIRST_PARTY_PUBLIC_AGENT_IDS
from orchestrator.tool_security import ToolSecurityAnalyzer
from shared.feature_flags import FeatureFlags, flags
from shared.protocol import RegisterAgent


@pytest.fixture
def cresco_agent_instance(monkeypatch):
    monkeypatch.setattr(cresco_agent.BaseA2AAgent, "_init_crypto",
                        lambda self: setattr(self, "_public_key_jwk", {"test": "ephemeral"}))
    return CrescoAgent(port=8989)


@pytest.fixture
def orch(orchestrator_factory, monkeypatch):
    instance = orchestrator_factory()
    instance.audit_recorder = MagicMock()
    instance.audit_recorder.record = AsyncMock()
    instance.send_ui_render = AsyncMock()
    instance.tool_permissions.is_tool_allowed = MagicMock(return_value=True)
    instance._map_file_paths = lambda cid, a, **k: a
    return instance


def _tc(tool, args=None):
    return SimpleNamespace(
        function=SimpleNamespace(name=tool, arguments=json.dumps(args or {}))
    )


def test_flag_defaults_off_and_is_read_from_the_environment(monkeypatch):
    monkeypatch.delenv("FF_CRESCO", raising=False)
    assert FeatureFlags().is_enabled("cresco") is False
    monkeypatch.setenv("FF_CRESCO", "true")
    assert FeatureFlags().is_enabled("cresco") is True


def test_registration_posture_lists_the_public_id_without_a_static_directory():
    assert "cresco" not in BUILT_IN_AGENT_DIRS
    assert "cresco-1" in FIRST_PARTY_PUBLIC_AGENT_IDS


def test_security_analyzer_hard_blocks_only_the_executor_on_the_real_card(cresco_agent_instance):
    flagged = ToolSecurityAnalyzer().analyze_agent(cresco_agent_instance.card)
    assert set(flagged) == {"cresco_run_command"}
    assert flagged["cresco_run_command"].blocked is True


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [True, False])
async def test_in_process_registration_follows_the_flag(monkeypatch, enabled):
    from shared import attachment_materializer, attachment_resolver

    for name in ("gaiakeep", "cresco", "remote_compute", "computer_use", "fhir"):
        monkeypatch.setitem(flags._flags, name, False)
    monkeypatch.setitem(flags._flags, "cresco", enabled)
    monkeypatch.setattr(local_agents, "discover_built_in_agent_dirs", list)
    monkeypatch.setattr(attachment_resolver, "register_plane_runtime", lambda *a: True)
    monkeypatch.setattr(attachment_materializer, "register_materialization_service", lambda *a: None)
    monkeypatch.setattr(cresco_agent.BaseA2AAgent, "_init_crypto",
                        lambda self: setattr(self, "_public_key_jwk", {"test": "ephemeral"}))
    plane = SimpleNamespace(runtime=object(), repositories=object(), blobs=object(),
                            attachment_materializer=object())
    registration = MagicMock()

    async def register_agent(websocket, message):
        registration(websocket, message)
        return None

    orch_ns = SimpleNamespace(runtime_composition=SimpleNamespace(plane=plane),
                              local_agents={}, register_agent=register_agent)
    registered = await local_agents.register_built_ins(orch_ns)
    assert registered == (["cresco-1"] if enabled else [])
    assert ("cresco-1" in orch_ns.local_agents) is enabled
    if enabled:
        args, _kwargs = registration.call_args
        message = args[1]
        assert message.agent_card.agent_id == "cresco-1"
        assert isinstance(message, RegisterAgent)


@pytest.mark.asyncio
async def test_safe_seed_never_marks_the_bridge_safe(monkeypatch):
    from tests.test_remote_orchestrator_wiring_063 import _drive_start

    _, seeded = await _drive_start(monkeypatch, remote_compute=False, gaiakeep=True)
    assert "cresco-1" not in seeded[0][1]
    assert "gaiakeep-1" not in seeded[0][1]


@pytest.mark.asyncio
async def test_executor_is_system_blocked_in_the_real_gate_stack(orch, monkeypatch):
    analyzer = ToolSecurityAnalyzer()
    card = MCPServer().get_tool_list()
    flag = analyzer.analyze_tool("cresco_run_command",
                                 next(t["description"] for t in card if t["name"] == "cresco_run_command"),
                                 next(t["input_schema"] for t in card if t["name"] == "cresco_run_command"))
    orch.security_flags["cresco-1"] = {"cresco_run_command": flag.to_dict()}
    response = await orch.execute_single_tool(
        MagicMock(), _tc("cresco_run_command", {"target": "r1_a1_executor", "command": "ls"}),
        {"cresco_run_command": "cresco-1"}, "chat-1", user_id="u1")
    assert response.error and "system-blocked" in response.error["message"]


@pytest.mark.asyncio
async def test_dispatch_requires_a_confirmation_card_through_the_real_gate_stack(orch, monkeypatch):
    monkeypatch.setitem(flags._flags, "cresco", True)
    orch.tool_permissions.register_tool_scopes(
        "cresco-1", {name: info["scope"] for name, info in mcp_tools.TOOL_REGISTRY.items()})
    response = await orch.execute_single_tool(
        MagicMock(), _tc("cresco_dispatch_work",
                         {"target": "r1_a1", "work_kind": "status", "work_payload": "hello"}),
        {"cresco_dispatch_work": "cresco-1"}, "chat-1", user_id="u1")
    assert response.result is not None
    assert response.result["_data"]["status"] == "confirmation_required"
    assert response.result["_data"]["message"].startswith("confirmation_required")
    assert response.ui_components and response.ui_components[0]["type"] in ("alert", "card", "container")


@pytest.mark.asyncio
async def test_read_tools_do_not_require_a_confirmation_card(orch, monkeypatch):
    monkeypatch.setitem(flags._flags, "cresco", True)
    refusal = remote_confirmation.evaluate(
        orch, MagicMock(), "cresco-1", "cresco_list_regions", {}, "chat-1", "u1")
    assert refusal is None


def test_confirmation_policy_classifies_every_registered_tool():
    policy = remote_confirmation.policy_for("cresco-1")
    assert policy is not None and policy.machine_key == "target"
    for tool, info in mcp_tools.TOOL_REGISTRY.items():
        expected = "never" if info["scope"] == "tools:read" else "always"
        assert remote_confirmation.classification_for(tool, "cresco-1") == expected, tool
    assert remote_confirmation.classification_for("unknown_tool", "cresco-1") is None


@pytest.mark.asyncio
async def test_unattended_mcp_channel_cannot_run_cresco_effects(orch, monkeypatch):
    monkeypatch.setitem(flags._flags, "cresco", True)
    orch.tool_permissions.register_tool_scopes(
        "cresco-1", {name: info["scope"] for name, info in mcp_tools.TOOL_REGISTRY.items()})
    ws = MagicMock()
    orch.ui_sessions[ws] = {"_invocation_channel": "mcp", "sub": "u1"}
    response = await orch.execute_single_tool(
        ws, _tc("cresco_dispatch_work", {"target": "r1_a1", "work_kind": "status"}),
        {"cresco_dispatch_work": "cresco-1"}, "chat-1", user_id="u1")
    assert response.error and "unattended" in response.error["message"].lower()
    assert "cresco_dispatch_work" in response.error["message"]


def test_fabric_content_is_untrusted_and_effects_are_sinks():
    assert taint.classify_source("cresco-1", "cresco_list_regions") == taint.UNTRUSTED
    assert taint.is_sink("cresco-1", "cresco_dispatch_work") is True
    assert taint.is_sink("cresco-1", "cresco_run_command") is True
    assert taint.is_sink("cresco-1", "cresco_list_regions") is False


def test_start_gate_follows_the_flag(monkeypatch):
    import start

    monkeypatch.setitem(flags._flags, "cresco", True)
    assert start._cresco_enabled() is True
    monkeypatch.setitem(flags._flags, "cresco", False)
    assert start._cresco_enabled() is False


def test_start_gate_fails_closed(monkeypatch):
    import start

    def unavailable(name):
        raise RuntimeError("unavailable")

    monkeypatch.setattr(flags, "is_enabled", unavailable)
    assert start._cresco_enabled() is False


@pytest.mark.parametrize(("enabled", "inprocess", "spawned"), [
    (False, False, False), (False, True, False), (True, True, False), (True, False, True),
])
def test_subprocess_spawn_respects_flag_and_transport_mode(monkeypatch, tmp_path, enabled, inprocess, spawned):
    import start
    from tests.test_start_wait import _agents_tree, _run_main

    backend, agents = _agents_tree(tmp_path)
    directory = agents / "cresco"
    directory.mkdir()
    (directory / "cresco_agent.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(start, "_cresco_enabled", lambda: enabled)
    _, started = _run_main(monkeypatch, backend, inprocess=inprocess, remote_flag=False)
    assert ("cresco" in started) is spawned
