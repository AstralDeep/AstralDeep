"""Checks normal extraction preparation and coded standalone MCP placement denials.
Canonical host invocation and durable accounting are exercised through normal turn ingress in the host suite.
"""

from __future__ import annotations

from types import MethodType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.fhir import fhir_agent, mcp_tools
from agents.fhir.tests.test_extraction import NOTE
from orchestrator.concurrency_cap import ConcurrencyCap
from orchestrator.hooks import HookManager
from orchestrator.orchestrator import GateRefusal, Orchestrator
from shared.protocol import MCPRequest, MCPResponse, Message

TOOL = "extract_note_fields"
OWNER = "owner-1"
CHAT = "chat-1"


@pytest.fixture
def agent(monkeypatch):
    monkeypatch.setattr(
        fhir_agent.BaseA2AAgent, "_init_crypto",
        lambda self: setattr(self, "_public_key_jwk", {"test": "ephemeral"}))
    return fhir_agent.FhirAgent(port=8998)


@pytest.fixture
def orch(monkeypatch, agent):
    monkeypatch.setenv("DELEGATION_REQUIRED", "false")
    o = Orchestrator.__new__(Orchestrator)
    o.agents = {}
    o.a2a_clients = {}
    o.local_agents = {"fhir-1": agent}
    o.agent_cards = {"fhir-1": agent.card}
    o.ui_sessions = {}
    o.security_flags = {}
    o._pending_cap_entries = {}
    o._hop_cap_entries = {}
    o._job_context = {}
    o.concurrency_cap = ConcurrencyCap(max_per_user_agent=3)
    o.hooks = HookManager()
    o.stream_manager = None
    o.audit_recorder = MagicMock()
    o.audit_recorder.record = AsyncMock()
    o.send_ui_render = AsyncMock()
    o.tool_permissions = MagicMock()
    o.tool_permissions.is_tool_allowed = MagicMock(return_value=True)
    o.tool_permissions.get_tool_scope_map = MagicMock(
        return_value={name: info["scope"] for name, info in mcp_tools.TOOL_REGISTRY.items()})
    o.tool_permissions.get_effective_permissions = MagicMock(
        return_value={name: True for name in mcp_tools.TOOL_REGISTRY})
    o._map_file_paths = lambda cid, a, **k: a
    o.credential_manager = MagicMock()
    o.credential_manager.get_agent_credentials_encrypted = MagicMock(return_value=None)
    o._llm_store = SimpleNamespace(
        get=AsyncMock(return_value=SimpleNamespace(
            api_key="owner-key", base_url="http://provider", model="owner-model")),
        get_system=AsyncMock(return_value=None),
    )
    return o


class LoopbackOrchestrator:
    def __init__(self, agent):
        self.local_agents = {agent.card.agent_id: agent}
        self.pending_requests = {}
        self.pending_ui_sockets = {}
        self.stream_manager = None
        self.frames = 0
        self._dispatch_context = {}
        self._register_dispatch_context = MethodType(Orchestrator._register_dispatch_context, self)
        self._terminal_dispatch_refusal = MethodType(Orchestrator._terminal_dispatch_refusal, self)
        self.execute = MethodType(Orchestrator._execute_in_process, self)

    async def handle_agent_message(self, websocket, message):
        self.frames += 1
        reply = Message.from_json(message)
        pending = self.pending_requests.get(reply.request_id) if isinstance(reply, MCPResponse) else None
        if pending is not None and not pending.done():
            pending.set_result(reply)


@pytest.mark.asyncio
async def test_gate_stack_prepares_owner_bound_dispatch(orch):
    websocket = object()
    orch.ui_sessions = {websocket: {"sub": OWNER}}
    prepared = await orch._authorize_and_prepare(
        websocket, "fhir-1", TOOL, {"note_text": NOTE, "note_id": "SYN-001"}, CHAT, OWNER)
    assert not isinstance(prepared, GateRefusal)
    assert set(prepared.args) == {"note_text", "note_id"}
    assert prepared.delegation_token is None


@pytest.mark.asyncio
async def test_gate_stack_refuses_without_permission(orch):
    orch.tool_permissions.is_tool_allowed = MagicMock(return_value=False)
    outcome = await orch._authorize_and_prepare(
        object(), "fhir-1", TOOL, {"note_text": NOTE}, CHAT, OWNER)
    assert isinstance(outcome, GateRefusal)
    assert "restricted" in outcome.response.error["message"]


@pytest.mark.asyncio
async def test_real_dispatch_executes_end_to_end(orch, agent, monkeypatch):
    websocket = object()
    orch.ui_sessions = {websocket: {"sub": OWNER}}
    prepared = await orch._authorize_and_prepare(
        websocket, "fhir-1", TOOL, {"note_text": NOTE, "note_id": "SYN-001"}, CHAT, OWNER)
    assert not isinstance(prepared, GateRefusal)

    monkeypatch.setitem(mcp_tools.flags._flags, "clinical_note_extract", True)
    transport = LoopbackOrchestrator(agent)
    response = await transport.execute("fhir-1", TOOL, dict(prepared.args), timeout=15.0, ui_websocket=websocket)
    assert response.error["code"] == "EXTRACT_HOST_UNAVAILABLE"
    assert not response.ui_components


@pytest.mark.asyncio
async def test_real_dispatch_carries_flag_off_denial(orch, agent, monkeypatch):
    websocket = object()
    orch.ui_sessions = {websocket: {"sub": OWNER}}
    prepared = await orch._authorize_and_prepare(
        websocket, "fhir-1", TOOL, {"note_text": NOTE}, CHAT, OWNER)
    assert not isinstance(prepared, GateRefusal)
    monkeypatch.setitem(mcp_tools.flags._flags, "clinical_note_extract", False)

    transport = LoopbackOrchestrator(agent)
    response = await transport.execute(
        "fhir-1", TOOL, dict(prepared.args), timeout=15.0, ui_websocket=websocket)
    assert response.error is not None
    assert response.error["code"] == "EXTRACT_DISABLED"
    assert not response.ui_components


@pytest.mark.asyncio
async def test_noncanonical_host_dispatch_refuses_without_provider_authority(orch, monkeypatch):
    monkeypatch.setitem(mcp_tools.flags._flags, "clinical_note_extract", True)
    response = await orch._dispatch_tool_call("fhir-1", TOOL, {"note_text": NOTE}, 60, object(),
                                             protected_owner_id=OWNER, protected_conversation_id=CHAT)
    assert response.error["code"] == "EXTRACT_HOST_UNAVAILABLE"


@pytest.mark.asyncio
async def test_mcp_server_lists_the_pilot_only_through_the_registry(agent):
    server = agent.mcp_server
    listed = server.process_request(MCPRequest(request_id="l", method="tools/list", params={}))
    names = {tool["name"] for tool in listed.result["tools"]}
    assert TOOL in names
    entry = next(tool for tool in listed.result["tools"] if tool["name"] == TOOL)
    assert entry["input_schema"]["required"] == ["note_text"]
