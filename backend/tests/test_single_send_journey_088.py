"""Tests that one Send starts one turn with zero mandatory preflight
(orchestrator/async_tasks.py, remote_confirmation.py, user_skills.py) over the real
socket-to-dispatch path, and that a mid-turn effect still needs separate approval.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from orchestrator import remote_confirmation as rc
from orchestrator import slash_commands, user_skills
from orchestrator.async_tasks import BackgroundTaskManager
from orchestrator.task_state import TaskManager
from orchestrator.user_skill_catalog import UserSkillFacade
from shared.feature_flags import flags
from shared.protocol import AgentCard, AgentSkill
from tests.helpers.remote_plane_runtime import make_remote_confirmation_plane_source
from tests.test_human_socket_ingress_088 import (
    metadata as metadata, ingress as ingress, surface as surface, command as command,
    context as context, fixture as fixture, runtime as runtime, service as service,
    signing_key as signing_key, registered,
)

pytestmark = pytest.mark.asyncio

RESEARCH_AGENT = "web-research-1"
RESEARCH_TOOL = "web_search"
PREFLIGHT_TYPES = {"chrome_render", "chrome_surface", "chrome_modal"}


def _assistant(content=None, tool_calls=None):
    return SimpleNamespace(role="assistant", content=content, tool_calls=tool_calls,
                           reasoning_content=None)


def _usage():
    return SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2)


def _tool_call(name=RESEARCH_TOOL, call_id="c1", arguments="{}"):
    return SimpleNamespace(id=call_id,
                           function=SimpleNamespace(name=name, arguments=arguments))


def send(state, **payload):
    request = str(uuid4())
    state.socket.feed(json.dumps({
        "type": "ui_event", "action": "chat_message",
        "submission_id": str(uuid4()), "request_generation": request,
        "connection_generation": state.connection_generation,
        "payload": payload}))
    return request


async def terminal(state):
    return await state.arrived(
        lambda value: value.get("type") == "operation_status"
        and value.get("terminal") is True)


def _preflight_frames(payloads):
    found = []
    for value in payloads:
        if value.get("type") in PREFLIGHT_TYPES:
            found.append(value)
        elif "confirmation_required" in json.dumps(value.get("components", [])):
            found.append(value)
    return found


@pytest.fixture
async def journey(metadata, runtime, fixture, tmp_path, monkeypatch):
    state, _, _ = metadata
    orch = state.orch
    owner = fixture[1]
    monkeypatch.setitem(flags._flags, "user_skills", True)
    monkeypatch.setitem(flags._flags, "slash_commands", True)
    monkeypatch.setitem(flags._flags, "bg_continuity", False)
    orch._user_skill_store = UserSkillFacade(orch, str(tmp_path))
    user_skills.UserSkillStore(str(tmp_path)).save(
        owner, name="Weekly shape", instructions="Use the exact original weekly outline.",
        applies_to="always", command="weekly")
    orch.cancelled_sessions = {}
    orch._chat_locks = {}
    orch._workspace_locks = {}
    orch._chain_budgets = {}
    orch.token_usage = {}
    orch._datamark_sanitize_spans = False
    orch.task_manager = TaskManager(orch.work_admission)
    orch.async_task_manager = BackgroundTaskManager(orch.work_admission)
    orch.async_task_manager.bind(plane_runtime=runtime,
                                 plane_repositories=runtime.repositories)
    chat = str(uuid4())

    async def no_stage(*_args, **_kwargs):
        return None, None, None

    async def no_detached(*_args, **_kwargs):
        return None, None

    monkeypatch.setattr(orch, "_begin_conversation_publication", no_stage)
    monkeypatch.setattr(orch, "_begin_detached_conversation_publication", no_detached)
    monkeypatch.setattr(orch, "_bind_conversation_scope", lambda *_a, **_k: None)
    monkeypatch.setattr(orch.history, "get_chat",
                        lambda *_a, **_k: {"id": chat, "messages": [{}, {}]})
    monkeypatch.setattr(orch.history, "add_message", lambda *_a, **_k: None,
                        raising=False)
    monkeypatch.setattr(orch.history, "get_latest_message_id", lambda *_a, **_k: 1,
                        raising=False)
    monkeypatch.setattr(orch.history, "get_file_mappings", lambda *_a, **_k: {},
                        raising=False)
    monkeypatch.setattr(orch.rote, "adapt", lambda _ws, components: components,
                        raising=False)
    monkeypatch.setattr(orch, "_llm_store",
                        SimpleNamespace(get=AsyncMock(return_value=None),
                                        get_system=AsyncMock(return_value=None)),
                        raising=False)
    monkeypatch.setattr(orch, "workspace",
                        SimpleNamespace(alive_rows=AsyncMock(return_value=[])),
                        raising=False)
    monkeypatch.setattr(orch, "_deliver_round_components", AsyncMock(return_value=[]))
    monkeypatch.setattr(orch, "_send_or_replace_components", AsyncMock(return_value=[]))
    monkeypatch.setattr(orch, "_emit_llm_usage_report", AsyncMock(), raising=False)
    monkeypatch.setattr(orch, "summarize_chat_title", AsyncMock())

    orch.agent_cards[RESEARCH_AGENT] = AgentCard(
        name="Web research", description="public research", agent_id=RESEARCH_AGENT,
        skills=[AgentSkill(name="search", description="search the public web",
                           id=RESEARCH_TOOL, input_schema={"type": "object"})])
    orch.agents[RESEARCH_AGENT] = object()

    observed = SimpleNamespace(
        turn_starts=[], preflight_at_start=[], llm_preflight=[], phi=[],
        permission=[], expansions=[], model_calls=[], tool_dispatches=[],
        chat_id=chat, owner=owner, state=state, orch=orch)

    outer = orch.handle_chat_message

    async def turn(websocket, message, chat_id, *args, **kwargs):
        observed.turn_starts.append(message)
        observed.preflight_at_start.append(_preflight_frames(state.socket.payloads()))
        return await outer(websocket, message, chat_id, *args, **kwargs)

    monkeypatch.setattr(orch, "handle_chat_message", turn)

    async def resolve(websocket, *_args, **_kwargs):
        observed.llm_preflight.append(websocket)
        return SimpleNamespace(client=None)

    monkeypatch.setattr(orch, "_resolve_llm_client_for", resolve, raising=False)

    async def phi(_websocket, chat_id, user_id, text):
        observed.phi.append((chat_id, user_id, text))

    monkeypatch.setattr(orch, "_notify_phi_if_detected", phi)

    allowed = orch.tool_permissions.is_tool_allowed

    def permission(*args, **kwargs):
        verdict = allowed(*args, **kwargs)
        observed.permission.append((args, kwargs, verdict))
        return verdict

    monkeypatch.setattr(orch.tool_permissions, "is_tool_allowed", permission)

    expand = slash_commands.expand_message

    def expansion(text, commands):
        result = expand(text, commands)
        observed.expansions.append(result)
        return result

    monkeypatch.setattr(slash_commands, "expand_message", expansion)
    try:
        yield observed
    finally:
        await orch.async_task_manager.drain(timeout_seconds=5)


def _script(observed, replies):
    remaining = list(replies)

    async def call_llm(websocket, messages, tools_desc=None, temperature=None,
                       feature="tool_dispatch"):
        observed.model_calls.append(tools_desc)
        return (remaining.pop(0) if len(remaining) > 1 else remaining[0]), _usage()

    observed.orch._call_llm = call_llm

    async def dispatch(*args, **kwargs):
        observed.tool_dispatches.append((args, kwargs))
        return SimpleNamespace(result={"summary": "one public release"}, error=None,
                               ui_components=[], correlation_id=None)

    observed.orch.execute_single_tool = dispatch


async def test_one_ordinary_send_starts_one_turn_with_no_preflight_dialog(journey):
    state = journey.state
    _script(journey, [_assistant(content="Here is the answer.")])
    await registered(state)

    generation = send(state, message="what is on my plate today?",
                      chat_id=journey.chat_id)
    result = await terminal(state)

    assert result["state"] == "completed"
    assert result["request_generation"] == generation
    assert journey.turn_starts == ["what is on my plate today?"]
    assert journey.preflight_at_start == [[]]
    assert not _preflight_frames(state.socket.payloads())
    assert len(journey.llm_preflight) == 1, "feature-054 LLM preflight must still gate"
    assert journey.phi and journey.phi[0][2] == "what is on my plate today?", (
        "feature-030 PHI notice must still see the turn's saved text")
    assert journey.expansions == ["what is on my plate today?"], (
        "guidance/slash expansion must still run on the ordinary path")
    assert journey.permission, (
        "the per-tool permission predicate must still decide the turn's tool list")
    assert len(journey.model_calls) == 1


async def test_one_research_send_starts_one_turn_with_no_preflight_dialog(journey):
    state = journey.state
    _script(journey, [
        _assistant(tool_calls=[_tool_call(
            arguments=json.dumps({"query": "public release notes"}))]),
        _assistant(content="One public release was published."),
    ])
    await registered(state)

    ask = "research the public release notes and summarise them"
    send(state, message=ask, chat_id=journey.chat_id)
    result = await terminal(state)

    assert result["state"] == "completed"
    assert journey.turn_starts == [ask], "research needs no second submission"
    assert journey.preflight_at_start == [[]]
    assert not _preflight_frames(state.socket.payloads())
    assert len(journey.tool_dispatches) == 1
    assert len(journey.llm_preflight) == 1
    assert journey.phi and journey.phi[0][2] == ask
    assert journey.expansions == [ask]
    assert journey.permission, "the tool list was still permission-filtered"


async def test_duplicate_submission_of_the_same_input_starts_no_second_turn(journey):
    state = journey.state
    _script(journey, [_assistant(content="Here is the answer.")])
    await registered(state)

    frame = {"type": "ui_event", "action": "chat_message",
             "submission_id": str(uuid4()), "request_generation": str(uuid4()),
             "connection_generation": state.connection_generation,
             "payload": {"message": "same question", "chat_id": journey.chat_id}}
    state.socket.feed(json.dumps(frame))
    state.socket.feed(json.dumps(frame))
    await terminal(state)
    await state.barrier()

    assert journey.turn_starts == ["same question"]
    assert not _preflight_frames(state.socket.payloads())


class _Rows:
    def __init__(self):
        self.rows: dict = {}


def _confirmation_orchestrator(db):
    source = make_remote_confirmation_plane_source(db)
    return SimpleNamespace(
        plane_repository_source=source,
        runtime_composition=SimpleNamespace(plane=SimpleNamespace(
            runtime=source.plane_runtime, repositories=source.plane_repositories)),
        credential_manager=object(), ui_sessions={},
        send_ui_render=AsyncMock(), execute_single_tool=AsyncMock())


async def test_send_does_not_approve_a_consequential_effect_reached_in_the_turn():
    db = _Rows()
    orch = _confirmation_orchestrator(db)
    args = {"machine_id": "m", "path": "/data", "recursive": True}

    outcome = rc.evaluate(orch, object(), "remote-compute-1", "remove_path",
                          dict(args), "chat", "sc002-owner")

    assert outcome is not None, "Send must not carry approval for a consequential effect"
    message, components = outcome
    assert "confirmation_required" in message
    assert components
    (row,) = db.rows.values()
    assert row["status"] == "pending" and row["owner_user_id"] == "sc002-owner"


async def test_the_separately_approved_effect_then_proceeds_exactly_once():
    db = _Rows()
    orch = _confirmation_orchestrator(db)
    args = {"machine_id": "m", "job_id": "123"}
    rc.evaluate(orch, object(), "remote-compute-1", "cancel_job", dict(args),
                "chat", "sc002-owner")
    (pid,) = db.rows
    db.rows[pid]["status"] = "approved"

    assert rc.evaluate(orch, object(), "remote-compute-1", "cancel_job",
                       dict(args, **{rc._MARKER: pid}), "chat", "sc002-owner") is None
    assert db.rows[pid]["status"] == "consumed"
    replay = rc.evaluate(orch, object(), "remote-compute-1", "cancel_job",
                         dict(args, **{rc._MARKER: pid}), "chat", "sc002-owner")
    assert replay is not None and "no longer valid" in replay[0]
