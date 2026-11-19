"""Exercises the registered public-reader exception and its limits in protected tool dispatch."""

from __future__ import annotations

import json
from importlib import import_module
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator import hitl
from shared.protocol import MCPResponse
from tests.test_lets_gate_ordering import gate_orchestrator  # noqa: F401


READS = [
    ("web-research-1", "web_search", {"query": "weather Lexington KY", "max_results": 8}),
    ("web-research-1", "research_brief", {"topic": "recent space research", "depth": "standard"}),
    ("web-research-1", "fetch_page", {"url": "https://www.nasa.gov/news"}),
    ("general-1", "search_arxiv", {"query": "cat:cs.AI", "max_results": 10}),
    ("summarizer-1", "summarize_url", {"url": "https://arxiv.org/abs/2401.12345"}),
]


def _register(orch, agent, tool):
    package, name, *_ = hitl._PUBLIC_READS[(agent, tool)]
    cls = getattr(import_module(f"agents.{package}.{package}_agent"), name)
    instance = cls.__new__(cls)
    instance.mcp_server = SimpleNamespace(tools=import_module(f"agents.{package}.mcp_tools").TOOL_REGISTRY)
    orch.local_agents[agent] = instance
    return instance


@pytest.fixture
def runtime(gate_orchestrator, monkeypatch):  # noqa: F811
    from audit import hooks

    orch, ws = gate_orchestrator
    monkeypatch.setattr(hitl, "hitl_enabled", lambda: True)
    monkeypatch.setattr(hooks, "get_recorder", lambda: None)
    monkeypatch.setattr("audit.recorder.get_recorder", lambda: None)
    monkeypatch.setattr("personalization.phi_gate.get_phi_gate",
                        lambda: SimpleNamespace(contains_phi=lambda _text: False,
                                                contains_phi_for_sharing=lambda _text: False))
    orch._active_request = {"chat-a": "Research the public web for this question"}
    orch._ws_active_chat = {id(ws): "chat-a"}
    orch._is_user_agent = lambda _id: False
    orch.send_ui_render = AsyncMock()
    orch.audit_recorder = SimpleNamespace(record=AsyncMock())
    orch.history = SimpleNamespace(get_chat=MagicMock(return_value={"id": "chat-a"}))
    orch._llm_context_user_id = lambda _ws: "owner-a"
    orch._llm_store = SimpleNamespace(get=AsyncMock(return_value=None))
    orch._execute_with_retry = AsyncMock(return_value=MCPResponse(result={"_data": {"ok": True}}))
    from orchestrator.workspace import WorkspaceManager
    orch.workspace = SimpleNamespace(aupsert=AsyncMock(return_value=[{"op": "upsert"}]),
        resolve_identity=WorkspaceManager.__new__(WorkspaceManager).resolve_identity)
    orch.send_ui_upsert = AsyncMock()

    async def mutate(**kwargs):
        return await kwargs["mutation"]()
    orch.run_detached_conversation_mutation = mutate
    return orch, ws


async def _dispatch(runtime, tool="send_email", args=None, agent="agent-a"):
    orch, ws = runtime
    tc = SimpleNamespace(id="test", function=SimpleNamespace(name=tool, arguments=json.dumps(args or {})))
    return await orch.execute_single_tool(ws, tc, {tool: agent}, "chat-a", user_id="owner-a")


@pytest.mark.parametrize("agent,tool,args", READS)
async def test_registered_public_research_runs_without_second_confirmation(runtime, agent, tool, args):
    orch, _ = runtime
    _register(orch, agent, tool)
    response = await _dispatch(runtime, tool, args, agent)
    assert response.error is None and response.result["_data"]["ok"]
    orch._execute_with_retry.assert_awaited_once()


async def test_fetched_untrusted_public_url_still_reads(runtime, monkeypatch):
    from orchestrator import taint
    orch, _ = runtime
    _register(orch, "web-research-1", "fetch_page")
    monkeypatch.setattr(taint, "taint_enabled", lambda: True)
    tracker = MagicMock()
    tracker.effective_trust_of_args.return_value = taint.UNTRUSTED
    orch._taint_tracker = lambda _chat: tracker
    response = await _dispatch(runtime, "fetch_page", {"url": "https://www.nasa.gov/news"}, "web-research-1")
    assert not response.error
    orch._execute_with_retry.assert_awaited_once()


@pytest.mark.parametrize("args", [
    {"url": "https://example.org", "headers": {"Authorization": "secret"}},
    {"url": "https://example.org", "body": "private record"},
    {"url": "https://alice:pass@example.org"},
    {"url": "https://example.org/?token=secret"},
    {"url": "https://example.org/?token=abcdef"},
    {"url": "https://example.org/?signature=abcdef"},
    {"url": "https://example.org/?key=abcdef"},
    {"url": "https://example.org/?code=abcdef"},
    {"url": "https://example.org/?api_key=abc"},
    {"url": "https://example.org/%2561pi_key=abc"},
    {"url": "http://127.0.0.1"}, {"url": "http://169.254.169.254"},
    {"url": "http://10.0.0.1"}, {"url": "http://[::1]"},
    {"url": "http://localhost"}, {"url": "https://hospital.internal"},
    {"url": "file:///tmp/report"}, {"url": "https://example.org:abc"},
    {"url": "https://example.org\nprivate"}, {"url": "https://example.org#private"},
    {"url": "https://example.org", "_credentials": "private"},
])
async def test_private_or_unknown_fetch_arguments_are_held(runtime, args):
    orch, _ = runtime
    _register(orch, "web-research-1", "fetch_page")
    response = await _dispatch(runtime, "fetch_page", args, "web-research-1")
    assert response.error["code"] == "confirmation_required"
    assert not response.ui_components
    orch._execute_with_retry.assert_not_awaited()


@pytest.mark.parametrize("query", ["MRN 123456 diabetes", "SSN 123-45-6789", "patient Alice's diagnosis",
                                    "password: abc", "bearer abcd", "alice@example.com"])
async def test_sensitive_search_queries_are_not_public_authority(runtime, query):
    orch, _ = runtime
    _register(orch, "web-research-1", "web_search")
    response = await _dispatch(runtime, "web_search", {"query": query}, "web-research-1")
    assert response.error["code"] == "confirmation_required"
    assert not response.ui_components
    orch._execute_with_retry.assert_not_awaited()


@pytest.mark.parametrize("agent,tool,args", READS)
def test_remote_names_cannot_claim_first_party_contract(runtime, agent, tool, args):
    orch, _ = runtime
    orch.local_agents[agent] = SimpleNamespace(mcp_server=SimpleNamespace(tools={tool: {"scope": "tools:read"}}))
    assert not hitl.registered_public_reader(orch, agent, tool)
    assert hitl.assess_risk(tool, args, agent_id=agent) == [hitl.EGRESS]


def test_registry_write_scope_and_replaced_function_cannot_opt_in(runtime):
    orch, _ = runtime
    instance = _register(orch, "web-research-1", "fetch_page")
    real = instance.mcp_server.tools["fetch_page"]
    instance.mcp_server = SimpleNamespace(tools={"fetch_page": {**real, "scope": "tools:write"}})
    assert not hitl.registered_public_reader(orch, "web-research-1", "fetch_page")
    instance.mcp_server.tools["fetch_page"] = {**real, "function": lambda: None}
    assert not hitl.registered_public_reader(orch, "web-research-1", "fetch_page")
    assert not hitl.registered_public_reader(orch, "other", "read")
    instance.mcp_server.tools.clear()
    assert not hitl.registered_public_reader(orch, "web-research-1", "fetch_page")


def test_public_contract_rejects_malformed_options_and_keeps_cross_principal():
    for args in ({}, {"query": ""}, {"query": []}, {"query": "x", "max_results": True},
                 {"query": "x", "max_results": 21}, {"query": "x" * 4097}):
        assert not hitl.public_read_arguments("web-research-1", "web_search", args)
    assert not hitl.public_read_arguments("web-research-1", "research_brief", {"topic": "x", "depth": []})
    assert not hitl.public_read_arguments("other", "fetch_page", {})
    assert not hitl.public_read_arguments("web-research-1", "fetch_page", {"url": "https://private"})
    assert hitl.assess_risk("fetch_page", {"url": "https://example.org"}, agent_id="web-research-1",
        public_reader=True, actor_principal="a", target_principal="b") == [hitl.CROSS_PRINCIPAL]


@pytest.mark.parametrize("gate", ["permission", "policy", "phi_hook"])
async def test_public_read_exemption_never_bypasses_other_gates(runtime, monkeypatch, gate):
    from orchestrator import policy
    from shared.feature_flags import flags
    orch, _ = runtime
    _register(orch, "web-research-1", "fetch_page")
    if gate == "permission":
        orch.tool_permissions.is_tool_allowed.return_value = False
    elif gate == "policy":
        monkeypatch.setattr(policy, "policy_enabled", lambda: True)
        monkeypatch.setattr(policy, "load_rules", lambda: [])
        monkeypatch.setattr(policy, "evaluate_policy", lambda *_: policy.PolicyDecision(effect=policy.DENY))
    else:
        monkeypatch.setitem(flags._flags, "hook_system", True)
        orch.hooks.emit.return_value = SimpleNamespace(action="block", reason="PHI detected")
    assert (await _dispatch(runtime, "fetch_page", {"url": "https://example.org"}, "web-research-1")).error
    orch._execute_with_retry.assert_not_awaited()
