"""Verifies terminal preparation preserves literal arguments and retained session identities under current owner gates. Real Plane file mappings still apply to ordinary context-aware agents."""

import base64
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI
import pytest

from agents.terminal_shell import mcp_tools
from agents.terminal_shell.terminal_shell_agent import TerminalShellAgent
from audit.recorder import Recorder
from orchestrator import hitl_confirmation, remote_confirmation
from orchestrator.concurrency_cap import ConcurrencyCap
from orchestrator.credential_manager import CredentialManager
from orchestrator.history import HistoryManager
from orchestrator.hooks import HookManager
from orchestrator.orchestrator import GateRefusal, Orchestrator, PreparedDispatch
from orchestrator.terminal_host_review import review_registered_terminal
from orchestrator.terminal_security import AGENT_ID, TerminalAuthority, bind_terminal_authority
from orchestrator.terminal_sessions import TerminalSessionManager
from orchestrator.tool_security import ToolSecurityAnalyzer
from rote.capabilities import DeviceProfile
from shared.feature_flags import flags
from shared.protocol import AgentCard, AgentSkill, MCPResponse, RegisterAgent
from tests.test_agent_key_enforcement import _make_fake
from tests.test_human_request_authority_088 import (
    fixture as fixture, human as human, runtime as runtime,
    service as service, signing_key as signing_key,
)
from tests.test_human_socket_authority_088 import socket_request as socket_request
from tests.test_terminal_ssh_runtime import isolated as isolated


@pytest.fixture
def bound(service, fixture, runtime, tmp_path):
    orch = Orchestrator.__new__(Orchestrator)
    orch.__dict__.update(vars(_make_fake()))
    orch.__dict__.update(vars(service.assignments.orch))
    del orch.register_agent
    del orch._get_user_id
    orch.persistent_assignments = service.assignments
    service.assignments.orch = orch
    orch.web_sessions = fixture[0]
    orch.audit_repo = service.audit
    orch.runtime_composition = SimpleNamespace(plane=SimpleNamespace(
        runtime=runtime, repositories=runtime.repositories))
    orch.credential_manager = CredentialManager(
        plane_runtime=runtime, plane_repositories=runtime.repositories,
        data_dir=str(tmp_path / "credentials"))
    orch.history = HistoryManager(data_dir=str(tmp_path / "history"),
        plane_runtime=runtime, plane_repositories=runtime.repositories)
    orch.security_analyzer = ToolSecurityAnalyzer()
    orch.hooks = HookManager()
    orch.concurrency_cap = ConcurrencyCap(max_per_user_agent=3)
    orch.stream_manager = None
    orch._llm_store = SimpleNamespace(get=AsyncMock(return_value=None), get_system=AsyncMock(return_value=None))
    orch.delegation = SimpleNamespace(exchange_token_for_agent=AsyncMock(return_value={"access_token": "synthetic-delegation"}))
    orch.send_ui_render = AsyncMock(return_value=True)
    for name in ("agents", "a2a_clients", "local_agents", "pending_requests", "_pending_request_agent",
                 "pending_ui_sockets", "_dispatch_context", "_chat_recorders", "agent_urls",
                 "_pending_cap_entries", "_hop_cap_entries", "_job_context", "_ws_active_chat",
                 "_conversation_scopes", "cancelled_sessions"):
        setattr(orch, name, {})
    app = FastAPI()
    app.state.orchestrator = orch
    yield service.assignments, app
    service.store.async_runtime.close()

@pytest.fixture
async def active(human, socket_request, isolated, monkeypatch, tmp_path):
    orch = human[2]
    recorder = Recorder(orch.audit_repo, retry_queue=tmp_path / "literal-audit-retry.jsonl")
    orch.audit_recorder = recorder
    monkeypatch.setattr("audit.recorder.get_recorder", lambda: recorder)
    socket, connection, message = socket_request
    owner = orch.ui_sessions[socket]["sub"]
    isolated.entry["owner_id"] = owner
    isolated.save()
    monkeypatch.setitem(flags._flags, "inprocess_agents", True)
    monkeypatch.setitem(flags._flags, "observation_packing", False)
    monkeypatch.setitem(flags._flags, "safe_compaction", False)
    monkeypatch.setitem(flags._flags, "tool_streaming", False)
    monkeypatch.setenv("DELEGATION_REQUIRED", "true")
    monkeypatch.delenv("AGENT_API_KEY", raising=False)
    monkeypatch.setattr(mcp_tools, "_ORCH", None)
    monkeypatch.setattr(TerminalShellAgent, "_init_crypto",
        lambda self: setattr(self, "_public_key_jwk", {}))
    orch.terminal_sessions = TerminalSessionManager(orchestrator=orch)
    agent = TerminalShellAgent(orchestrator=orch)
    await orch.register_agent(None, RegisterAgent(agent_card=agent.card))
    orch.local_agents[AGENT_ID] = agent
    assert review_registered_terminal(orch)
    for tool, entry in mcp_tools.TOOL_REGISTRY.items():
        orch.tool_permissions.set_tool_permission(owner, AGENT_ID, tool, entry["scope"], True)
    info = orch.terminal_sessions.create(owner,
        authority=bind_terminal_authority(orch, socket, owner, "start_terminal"),
        runtime_binding=orch.terminal_sessions.runtime_binding(owner))
    session = orch.terminal_sessions._sessions[info["session_id"]]
    chat = orch.history.create_chat(user_id=owner)
    orch.history.add_file_mapping(chat, "uploaded.txt", "/mapped/uploaded.txt", user_id=owner)
    orch._ws_active_chat[id(socket)] = chat
    orch.rote = SimpleNamespace(get_profile=lambda _: DeviceProfile.default())
    message.update(action="chrome_open", payload={"surface": "terminal", "params": {"mode": "list"}})
    sent = []

    async def send(ws, frame):
        assert ws is socket
        sent.append(json.loads(frame))
        return True

    orch._safe_send = send
    yield SimpleNamespace(orch=orch, socket=socket, connection=connection, message=message,
        owner=owner, chat=chat, session=session, info=info, sent=sent, isolated=isolated)
    orch.terminal_sessions.shutdown()
    await recorder.close()

async def _approved_prepare(active, monkeypatch, tool, arguments):
    prepared = []
    reviews = []
    async def prepare_only(socket, call, tool_to_agent, chat, *, user_id, **kwargs):
        outcome = await active.orch._authorize_and_prepare(socket, AGENT_ID, tool,
            json.loads(call.function.arguments), chat, user_id, auto_subscribe_stream=False)
        prepared.append(outcome)
        if isinstance(outcome, GateRefusal):
            return outcome.response
        return MCPResponse(error={"message": "Synthetic preparation-only boundary", "retryable": False})
    monkeypatch.setattr(active.orch, "execute_single_tool", prepare_only)
    outcome = await active.orch._authorize_and_prepare(active.socket, AGENT_ID, tool,
        arguments, active.chat, active.owner, auto_subscribe_stream=False)
    for _ in range(3):
        if isinstance(outcome, PreparedDispatch):
            if tool != "terminal_read":
                assert reviews
            if tool in {"start_terminal", "terminal_write"}:
                assert "remote_op_decision" in reviews
            return outcome
        assert isinstance(outcome, GateRefusal), outcome
        response = outcome.response
        assert response.result and response.result["_data"]["status"] == "confirmation_required", response.error
        content = response.ui_components[0]["content"]
        button = next(item for item in content if item.get("type") == "button"
            and item.get("payload", {}).get("decision") == "approve")
        reviews.append(button["action"])
        before = len(prepared)
        if button["action"] == "remote_op_decision":
            await remote_confirmation.handle_decision(active.orch, active.socket, active.owner, button["payload"])
        else:
            assert button["action"] == "authorize_action"
            await hitl_confirmation.handle_decision(active.orch, active.socket, active.owner, button["payload"])
        assert len(prepared) == before + 1
        outcome = prepared[-1]
    raise AssertionError("normal approvals did not reach preparation")


@pytest.mark.asyncio
@pytest.mark.parametrize("tool,extra", [
    ("terminal_read", {"offset": 0, "wait_seconds": 0}),
    ("terminal_resize", {"cols": 90, "rows": 30}),
    ("terminal_interrupt", {}),
    ("close_terminal", {}),
])
async def test_terminal_preparation_preserves_actual_terminal_id_and_owner(active, monkeypatch, tool, extra):
    active.orch.history.add_file_mapping(active.chat, active.session.session_id,
        "/mapped/terminal-named-upload", user_id=active.owner)
    original = {"session_id": active.session.session_id, **extra,
                "user_id": "forged-owner", "_terminal_authority": "forged-authority"}
    outcome = await _approved_prepare(active, monkeypatch, tool, dict(original))
    assert isinstance(outcome, PreparedDispatch)
    assert outcome.args["session_id"] == active.session.session_id != active.chat
    assert outcome.args["user_id"] == active.owner
    authority = outcome.args["_terminal_authority"]
    assert type(authority) is TerminalAuthority and authority.owner_id == active.owner
    authority.assert_current(tool)
    assert outcome.stream_params["session_id"] == active.session.session_id
    assert {key: outcome.stream_params[key] for key in extra} == extra


@pytest.mark.asyncio
async def test_nonterminal_preparation_keeps_normal_file_mapping_and_chat_context(active):
    agent = "context-agent"
    active.orch.agent_cards[agent] = AgentCard(name="Context", description="Context consumer", agent_id=agent,
        skills=[AgentSkill(id="read", name="read", description="Read mapped input", scope="tools:read",
            input_schema={"type": "object", "properties": {
                "session_id": {"type": "string"}, "user_id": {"type": "string"},
                "path": {"type": "string"}}, "additionalProperties": False})])
    active.orch.local_agents[agent] = object()
    active.orch.tool_permissions.register_tool_scopes(agent, {"read": "tools:read"})
    active.orch.tool_permissions.set_tool_permission(active.owner, agent, "read", "tools:read", True)
    outcome = await active.orch._authorize_and_prepare(active.socket, agent, "read",
        {"session_id": "model-context", "user_id": "forged-owner", "path": "uploaded.txt"},
        active.chat, active.owner)
    assert isinstance(outcome, PreparedDispatch)
    assert outcome.args["session_id"] == active.chat
    assert outcome.args["user_id"] == active.owner
    assert outcome.args["path"] == "/mapped/uploaded.txt"


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["data", "data_b64"])
async def test_approved_terminal_write_keeps_literal_input_after_upload_collision(active, monkeypatch, field):
    raw = b"uploaded.txt" if field == "data" else b"\x00\xffliteral input\n"
    value = raw.decode() if field == "data" else base64.b64encode(raw).decode()
    active.orch.history.add_file_mapping(active.chat, value, "/mapped/uploaded-input", user_id=active.owner)
    arguments = {"session_id": active.session.session_id, field: value}
    outcome = await _approved_prepare(active, monkeypatch, "terminal_write", deepcopy(arguments))
    assert outcome.args["session_id"] == active.session.session_id
    assert outcome.args[field] == value
    assert outcome.args["user_id"] == active.owner
    assert all(effect[0] != "send" for channel in active.isolated.state.channels for effect in channel.effects)


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["command", "directory", "target", "runtime_binding"])
async def test_approved_terminal_start_keeps_literal_shell_fields(active, monkeypatch, field):
    arguments = {"command": "printf 'uploaded.txt'", "directory": "/workspace/literal",
        "target": active.isolated.target.machine_id,
        "runtime_binding": active.orch.terminal_sessions.runtime_binding(active.owner)}
    active.orch.history.add_file_mapping(active.chat, arguments[field], "/mapped/uploaded-shell-field", user_id=active.owner)
    outcome = await _approved_prepare(active, monkeypatch, "start_terminal", dict(arguments))
    assert {key: outcome.args[key] for key in arguments} == arguments
    assert "session_id" not in outcome.args
    assert outcome.args["user_id"] == active.owner
    assert len(active.isolated.state.channels) == 1
