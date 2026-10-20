"""Verifies the bridge's security posture: the executor's hard security flag and system
scope, the safe-agent baseline's inability to enable it, registration and kill-switch
gating, card shape, owner binding, secret scrubbing and audit-field mapping.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agents.cresco import cresco_agent, mcp_tools, mcp_server
from agents.cresco.mcp_server import MCPServer, bind_owner_context
from agents.cresco.targets import TargetRefused
from agents.cresco.wsapi_client import FabricConfig, FabricFault
from orchestrator import local_agents as local_agents_module
from orchestrator.local_agents import BUILT_IN_AGENT_DIRS, FIRST_PARTY_PUBLIC_AGENT_IDS
from orchestrator.tool_security import ToolSecurityAnalyzer
from shared.feature_flags import FeatureFlags, flags


def test_registry_declares_tiered_scopes_and_clean_read_tools():
    assert mcp_tools.TOOL_REGISTRY["cresco_run_command"]["scope"] == "tools:system"
    assert mcp_tools.TOOL_REGISTRY["cresco_dispatch_work"]["scope"] == "tools:write"
    assert all(info["scope"] == "tools:read" for name, info in mcp_tools.TOOL_REGISTRY.items()
               if name.startswith(("cresco_list", "cresco_agent_info")))
    analyzer = ToolSecurityAnalyzer()
    for name in ("cresco_list_regions", "cresco_list_agents", "cresco_agent_info",
                 "cresco_dispatch_work"):
        assert analyzer.analyze_tool(name, mcp_tools.TOOL_REGISTRY[name]["description"],
                                     mcp_tools.TOOL_REGISTRY[name]["input_schema"]) is None, name


def test_executor_wrapper_is_hard_security_flagged():
    analyzer = ToolSecurityAnalyzer()
    info = mcp_tools.TOOL_REGISTRY["cresco_run_command"]
    flag = analyzer.analyze_tool("cresco_run_command", info["description"], info["input_schema"])
    assert flag is not None and flag.blocked is True
    assert flag.category.value == "CODE_EXECUTION"


def test_safe_agent_baseline_cannot_enable_the_executor(monkeypatch):
    monkeypatch.setenv("CRESCO_EXECUTOR_ENABLED", "true")
    monkeypatch.setitem(mcp_server.flags._flags, "cresco", True)
    monkeypatch.setenv("CRESCO_WSAPI_URL", "wss://fabric.example:8282")
    monkeypatch.setenv("CRESCO_SERVICE_KEY", "k")

    class SafeFlippingPermissions:
        def is_tool_allowed(self, user_id, agent_id, tool_name):
            return True

    ctx = mcp_tools.ToolContext(user_id="u1", orchestrator=SimpleNamespace(
        tool_permissions=SafeFlippingPermissions()))
    with pytest.raises(TargetRefused):
        mcp_tools.run_command(ctx, target="r1_a1_executor", command="ls")


def test_handler_refuses_without_any_permission_manager(monkeypatch):
    monkeypatch.setenv("CRESCO_EXECUTOR_ENABLED", "true")
    monkeypatch.setitem(mcp_server.flags._flags, "cresco", True)
    monkeypatch.setenv("CRESCO_WSAPI_URL", "wss://fabric.example:8282")
    monkeypatch.setenv("CRESCO_SERVICE_KEY", "k")
    ctx = mcp_tools.ToolContext(user_id="u1", orchestrator=None)
    with pytest.raises(TargetRefused):
        mcp_tools.run_command(ctx, target="r1_a1_executor", command="ls")


def test_operator_switch_gates_the_executor_before_permissions(monkeypatch):
    monkeypatch.delenv("CRESCO_EXECUTOR_ENABLED", raising=False)
    monkeypatch.setitem(mcp_server.flags._flags, "cresco", True)
    ctx = mcp_tools.ToolContext(user_id="u1", orchestrator=SimpleNamespace(
        tool_permissions=SimpleNamespace(is_tool_allowed=lambda *a: True)))
    with pytest.raises(FabricFault) as fault:
        mcp_tools.run_command(ctx, target="r1_a1_executor", command="ls")
    assert fault.value.detail == "executor_disabled_by_operator"


def test_agent_card_describes_the_bridge(monkeypatch):
    monkeypatch.setattr(cresco_agent.BaseA2AAgent, "_init_crypto",
                        lambda self: setattr(self, "_public_key_jwk", {"test": "ephemeral"}))
    agent = cresco_agent.CrescoAgent(port=8991)
    card = agent.card
    assert card.agent_id == "cresco-1" and card.name == "Cresco Bridge"
    assert {skill.name for skill in card.skills} == set(mcp_tools.TOOL_REGISTRY)
    scopes = {skill.name: skill.scope for skill in card.skills}
    assert scopes["cresco_run_command"] == "tools:system"
    assert scopes["cresco_dispatch_work"] == "tools:write"
    assert "cresco" in card.skills[0].tags
    assert agent.port == 8991


def test_agent_reads_its_port_from_the_environment(monkeypatch):
    monkeypatch.setattr(cresco_agent.BaseA2AAgent, "_init_crypto",
                        lambda self: setattr(self, "_public_key_jwk", {}))
    monkeypatch.setenv("CRESCO_AGENT_PORT", "8990")
    assert cresco_agent.CrescoAgent().port == 8990


def test_standalone_start_refuses_when_the_flag_is_off(monkeypatch):
    from shared.feature_flags import flags

    monkeypatch.setitem(flags._flags, "cresco", False)
    with pytest.raises(SystemExit) as stopped:
        cresco_agent.main()
    assert stopped.value.code in (78, 5)


def test_registration_posture_defaults():
    assert "cresco" not in BUILT_IN_AGENT_DIRS
    assert "cresco-1" in FIRST_PARTY_PUBLIC_AGENT_IDS
    assert FeatureFlags().is_enabled("cresco") is False


def test_bind_owner_context_stamps_the_verified_user():
    arguments = {"target": "r1_a1"}
    bind_owner_context(arguments, "user-9")
    assert arguments["user_id"] == "user-9"


def test_scrubbing_bounds_and_masks_untrusted_values():
    scrub = mcp_server._scrub
    assert scrub({"a": {"b": "service key: zzz"}}, ("zzz",))["a"]["b"] == "[REDACTED]"
    assert len(scrub(list(range(200)), ())) == 64
    assert scrub("x" * 3000, ()) == "x" * 1997 + "..."
    deep = {"a": {"b": {"c": {"d": {"e": {"f": 1}}}}}}
    assert scrub(deep, ())["a"]["b"]["c"]["d"]["e"] == "[bounded]"


def test_audit_fields_carry_fabric_identifiers_only():
    inputs, outputs = mcp_server._audit_fields(
        "cresco_dispatch_work",
        {"target": "r1_a1", "work_kind": "status", "work_payload": "secret text"},
        {"verdict": "completed", "work_id": "w1", "fault": None, "reason": None})
    assert inputs == {"cresco_tool": "cresco_dispatch_work", "cresco_target": "r1_a1",
                      "cresco_work_kind": "status"}
    assert outputs == {"cresco_verdict": "completed", "cresco_work_id": "w1"}


def test_fault_mapping_covers_the_closed_set():
    from agents.cresco.mcp_server import _FAULT_CODES

    assert set(_FAULT_CODES) == {"unavailable", "unreachable", "auth", "protocol", "tls"}


def test_tool_context_defaults_to_the_environment_config(monkeypatch):
    monkeypatch.setenv("CRESCO_WSAPI_URL", "wss://f.example:8282")
    monkeypatch.setenv("CRESCO_SERVICE_KEY", "k")
    ctx = mcp_tools.ToolContext(user_id="u")
    assert ctx.client().config.is_configured


def test_tool_context_honors_an_injected_config():
    config = FabricConfig(wsapi_url="wss://injected:8282", service_key="k")
    seen = {}
    ctx = mcp_tools.ToolContext(user_id="u", config=config,
                                client_factory=lambda cfg: seen.setdefault("cfg", cfg))
    ctx.client()
    assert seen["cfg"] is config


def test_mcpserver_lists_tools_with_schema_shape():
    server = MCPServer()
    listed = server.get_tool_list()
    assert all(entry["input_schema"]["type"] == "object" for entry in listed)
    assert all(entry["scope"] for entry in listed)


def test_standalone_start_runs_the_agent_when_enabled(monkeypatch):
    from shared.feature_flags import flags

    monkeypatch.setitem(flags._flags, "cresco", True)
    monkeypatch.setattr("sys.argv", ["cresco_agent.py", "--port", "8988"])
    monkeypatch.setattr(cresco_agent.BaseA2AAgent, "_init_crypto",
                        lambda self: setattr(self, "_public_key_jwk", {}))
    started = {}
    run = AsyncMock()

    class _FakeAgent:
        def __init__(self, port=None, *, orchestrator=None):
            started["port"] = port
            self.run = run

    monkeypatch.setattr(cresco_agent, "CrescoAgent", _FakeAgent)
    cresco_agent.main()
    assert started == {"port": 8988} and run.await_count == 1


def test_handlers_refuse_directly_when_the_flag_is_off(monkeypatch):
    from shared.feature_flags import flags

    monkeypatch.setitem(flags._flags, "cresco", False)
    ctx = mcp_tools.ToolContext(user_id="u1")
    with pytest.raises(FabricFault) as fault:
        mcp_tools.list_regions(ctx)
    assert fault.value.detail == "feature_flag_off"


def test_topology_results_of_an_unexpected_shape_fail_safe(bridge_env, monkeypatch):
    from agents.cresco.mcp_tools import ToolContext

    def shapeless(config):
        class _Shapeless:
            def call(self, action, params, **kwargs):
                return {"regions": "not-a-list"}

        return _Shapeless()

    ctx = ToolContext(user_id="u1", client_factory=shapeless)
    with pytest.raises(FabricFault) as fault:
        mcp_tools.list_regions(ctx)
    assert fault.value.detail == "unexpected_topology_shape"


def test_executor_authorized_path_validates_command_and_window(bridge_env, monkeypatch):
    monkeypatch.setenv("CRESCO_EXECUTOR_ENABLED", "true")
    monkeypatch.setitem(mcp_server.flags._flags, "cresco", True)
    ctx = mcp_tools.ToolContext(user_id="u1", orchestrator=SimpleNamespace(
        tool_permissions=SimpleNamespace(is_tool_allowed=lambda *a: True)))
    with pytest.raises(TargetRefused) as refused:
        mcp_tools.run_command(ctx, target="r1_a2_executor", command="   ")
    assert refused.value.code == "invalid_command"
    with pytest.raises(TargetRefused):
        mcp_tools.run_command(ctx, target="r1_a2_executor", command="ls", timeout_seconds=999)


def test_executor_refuses_before_any_unverified_transport_call(bridge_env, monkeypatch):
    monkeypatch.setenv("CRESCO_EXECUTOR_ENABLED", "true")

    class GuardedClient:
        def call(self, *args, **kwargs):
            pytest.fail("unverified executor operation attempted")

    ctx = mcp_tools.ToolContext(user_id="u1", orchestrator=SimpleNamespace(
        tool_permissions=SimpleNamespace(is_tool_allowed=lambda *a: True)),
        client_factory=lambda _: GuardedClient())
    with pytest.raises(FabricFault) as unavailable:
        mcp_tools.run_command(ctx, target="r1_a2_executor", command="ls")
    assert unavailable.value.detail == "executor_result_transport_unavailable"


@pytest.mark.asyncio
async def test_registration_survives_a_cresco_flag_check_failure(monkeypatch):
    from shared import attachment_materializer, attachment_resolver

    def selective(name):
        if name == "cresco":
            raise RuntimeError("unavailable")
        return False

    monkeypatch.setattr(flags, "is_enabled", selective)
    monkeypatch.setattr(local_agents_module, "discover_built_in_agent_dirs", list)
    monkeypatch.setattr(attachment_resolver, "register_plane_runtime", lambda *a: True)
    monkeypatch.setattr(attachment_materializer, "register_materialization_service", lambda *a: None)
    plane = SimpleNamespace(runtime=object(), repositories=object(), blobs=object(),
                            attachment_materializer=object())
    orch_ns = SimpleNamespace(runtime_composition=SimpleNamespace(plane=plane),
                              local_agents={}, register_agent=AsyncMock())
    assert await local_agents_module.register_built_ins(orch_ns) == []
