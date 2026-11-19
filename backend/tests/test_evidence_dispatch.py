"""Exercises evidence authority and physical usage through the orchestrator's
ordinary dispatch and streamed providers. Synthetic
providers preserve the real host seams without third-party network services.
"""

from __future__ import annotations

import asyncio
from collections import deque
from copy import deepcopy
import json
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from openai.types.chat import ChatCompletionMessage, ChatCompletionMessageFunctionToolCall
import pytest

from orchestrator import evidence_context
from orchestrator.context_usage import ContextUsage
from orchestrator.orchestrator import Orchestrator
from orchestrator.tests.test_dispatch_local_agents_040 import _build_orch
from shared.feature_flags import flags
from shared.protocol import MCPResponse
from tests.test_chain_hop import _parent
from tests.test_llm_streaming import _bare_orch

pytestmark = pytest.mark.asyncio


def usage(prompt=8, completion=2):
    return SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion,
                           cached_tokens=0, total_tokens=prompt + completion)


def response(content="Synthetic answer", prompt=8):
    message = ChatCompletionMessage(role="assistant", content=content, tool_calls=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=usage(prompt))


def _make_tool_call(name, args=None):
    return ChatCompletionMessageFunctionToolCall.model_validate({
        "id": "call_1", "type": "function", "function": {
            "name": name, "arguments": json.dumps(args or {}),
        },
    })


def chunk(content=None, reported=None):
    delta = SimpleNamespace(role=None, content=content, tool_calls=None)
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason=None)], usage=reported)


class Completions:
    def __init__(self, outcomes):
        self.outcomes = deque(outcomes)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        outcome = self.outcomes.popleft()
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome() if callable(outcome) else outcome


class ChargedFailure(RuntimeError):
    def __init__(self, *, status=503, prompt=4, text="synthetic provider failure"):
        super().__init__(text)
        self.status_code = status
        self.usage = usage(prompt)


async def authorized_prepare(*, websocket, owner, chat, model, base_url, request, provider_capture):
    assert websocket is not None and owner == "alice" and chat == "conversation"
    assert model and base_url and isinstance(request, dict)
    assert request.get("model") == model and isinstance(request.get("messages"), list)

    async def guard():
        return None

    return guard


@pytest.fixture(autouse=True)
def feature_controls(monkeypatch):
    for name in ("hook_system", "taint_tracking", "runtime_supervisor", "hitl_highrisk",
                 "policy_engine", "model_router", "user_skills", "slash_commands"):
        monkeypatch.setitem(flags._flags, name, False)
        monkeypatch.setenv("FF_" + name.upper(), "false")
    monkeypatch.setenv("FF_OBSERVATION_PACKING", "true")
    monkeypatch.setenv("FF_SAFE_COMPACTION", "false")
    monkeypatch.setitem(flags._flags, "observation_packing", True)
    monkeypatch.setitem(flags._flags, "safe_compaction", False)
    monkeypatch.setenv("FF_LLM_STREAMING", "true")


def model_host(completions):
    host = _bare_orch(completions)
    resolve = host._resolve_llm_client_for

    async def resolve_bound(socket, *, evidence_bound=False):
        return await resolve(socket)

    host._resolve_llm_client_for = resolve_bound
    socket = object()
    host.ui_sessions = {socket: {"sub": "alice", "preferred_username": "Alice"}}
    host._ws_active_chat = {id(socket): "conversation"}
    host.token_usage = {}
    host.history = MagicMock()
    host._broadcast_user_history = AsyncMock()
    host._llm_audit_principals = Orchestrator._llm_audit_principals.__get__(host)
    events = []

    async def record(event):
        events.append(event.model_copy(deep=True))

    ledger = ContextUsage(record)
    host._evidence_context = SimpleNamespace(usage=ledger, prepare_model=authorized_prepare,
        summary_view=AsyncMock(return_value="view_" + "v" * 43))
    return SimpleNamespace(host=host, socket=socket, ledger=ledger, events=events, completions=completions)


async def total(state):
    await state.ledger.drain()
    return await state.ledger.totals("alice", "conversation")


async def test_nonstream_physical_retry_and_parameter_probe_keep_each_charge(monkeypatch):
    probe = ChargedFailure(status=400, prompt=4, text="400 unknown parameter reasoning_effort private-body")
    outage = ChargedFailure(prompt=6, text="private upstream transport body")
    state = model_host(Completions([probe, outage, response(prompt=8)]))

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr("orchestrator.orchestrator.asyncio.sleep", no_sleep)
    message, _ = await state.host._call_llm(state.socket, [{"role": "user", "content": "Private prompt"}],
                                          reasoning_effort="high")
    assert message.content == "Synthetic answer"
    totals = await total(state)
    assert totals["model_calls"] == len(state.completions.calls) == 3
    assert totals["failed"] == totals["charged_failed"] == totals["retries"] == 2
    assert totals["usage"]["total_tokens"] == {"known": 24, "unknown": 0}
    assert "reasoning_effort" not in state.completions.calls[-1]
    assert all(event.actor_user_id == "alice" for event in state.events)
    stored = "".join(event.model_dump_json() for event in state.events)
    assert "Private prompt" not in stored and "private-body" not in stored and "private upstream" not in stored


async def test_malformed_provider_result_is_charged_before_host_rejects_it():
    state = model_host(Completions([SimpleNamespace(choices=[], usage=usage())]))
    assert await state.host._call_llm(state.socket, [{"role": "user", "content": "hello"}]) == (None, None)
    totals = await total(state)
    assert totals["model_calls"] == 1
    assert totals["usage"]["total_tokens"] == {"known": 10, "unknown": 0}


async def test_stream_failure_settles_its_charge_before_continuing_the_visible_answer():
    def failed_stream():
        yield chunk("partial private content", usage(4))
        raise ChargedFailure(prompt=4)

    state = model_host(Completions([failed_stream, response("Recovered", prompt=8)]))
    message, _ = await state.host._call_llm(state.socket, [{"role": "user", "content": "hello"}],
                                          allow_stream=True, stream_chat_id="conversation")
    assert message is not None and message.content == "partial private contentRecovered"
    totals = await total(state)
    assert totals["model_calls"] == 2 and totals["retries"] == totals["charged_failed"] == 1
    assert totals["usage"]["total_tokens"] == {"known": 16, "unknown": 0}
    assert totals["pending"] == 0


async def test_stream_success_records_one_call_without_counting_chunk_deliveries():
    state = model_host(Completions([lambda: iter([chunk("One "), chunk("answer"), chunk(reported=usage())])]))
    message, _ = await state.host._call_llm(state.socket, [{"role": "user", "content": "hello"}],
                                          allow_stream=True, stream_chat_id="conversation")
    assert message.content == "One answer"
    totals = await total(state)
    assert totals["attempts"] == totals["model_calls"] == totals["succeeded"] == 1
    assert totals["usage"]["total_tokens"] == {"known": 10, "unknown": 0}
    assert len(state.host._sent_frames) >= 2


@pytest.mark.parametrize("outcome", ["missing_usage", "create_failure"])
async def test_stream_transport_failures_preserve_known_and_unknown_charge_distinction(outcome):
    def interrupted():
        yield chunk("partial")
        raise RuntimeError("private stream exception")

    first = interrupted if outcome == "missing_usage" else ChargedFailure(prompt=4)
    state = model_host(Completions([first, response("Recovered", prompt=8)]))
    message, _ = await state.host._call_llm(state.socket, [{"role": "user", "content": "hello"}],
                                          allow_stream=True, stream_chat_id="conversation")
    assert message is not None and message.content == "Recovered"
    totals = await total(state)
    assert totals["model_calls"] == 2 and totals["retries"] == totals["failed"] == 1
    assert totals["usage"]["total_tokens"] == {
        "known": 10 if outcome == "missing_usage" else 16,
        "unknown": 1 if outcome == "missing_usage" else 0,
    }
    assert totals["charged_failed"] == (0 if outcome == "missing_usage" else 1)


@pytest.mark.parametrize("streamed", [False, True])
async def test_runtime_caller_cancellation_survives_provider_thread_and_late_charge(streamed):
    entered, finish = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()

    def provider():
        loop.call_soon_threadsafe(entered.set)
        assert finish.wait(5)
        return response()

    def provider_stream():
        yield chunk("started")
        loop.call_soon_threadsafe(entered.set)
        assert finish.wait(5)
        yield chunk(reported=usage())

    state = model_host(Completions([provider_stream if streamed else provider]))
    task = asyncio.create_task(state.host._call_llm(state.socket, [{"role": "user", "content": "hello"}],
                                                  allow_stream=streamed, stream_chat_id="conversation"))
    try:
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert (await state.ledger.totals("alice", "conversation"))["cancelled"] == 1
    finally:
        finish.set()
        await state.ledger.drain()
    totals = await total(state)
    assert totals["model_calls"] == totals["charged_cancelled"] == 1
    assert totals["usage"]["total_tokens"] == {"known": 10, "unknown": 0}


@pytest.mark.parametrize("cached", [False, True])
async def test_feature_off_preserves_provider_arguments_and_emits_no_context_events(monkeypatch, cached):
    state = model_host(Completions([response()]))
    monkeypatch.setenv("FF_OBSERVATION_PACKING", "false")
    monkeypatch.setenv("FF_SAFE_COMPACTION", "false")
    monkeypatch.setitem(flags._flags, "observation_packing", False)
    monkeypatch.setitem(flags._flags, "safe_compaction", False)
    if not cached:
        del state.host._evidence_context
    messages = [{"role": "user", "content": "hello"}]
    message, _ = await state.host._call_llm(state.socket, messages)
    assert message.content == "Synthetic answer"
    assert state.completions.calls == [{"model": "m1", "messages": messages}]
    assert not state.events


def dispatch_host():
    host = _build_orch()
    host._execute_with_retry = Orchestrator._execute_with_retry.__get__(host)
    host._source_ledger = Orchestrator._source_ledger.__get__(host)
    host._dispatch_context = {}
    host.pending_requests = {}
    host.pending_ui_sockets = {}
    host._hop_cap_entries = {}
    host._chain_budgets = {}
    host._safe_send = AsyncMock()
    host.tool_permissions.get_tool_scope.return_value = "tools:read"
    host.tool_permissions.get_enabled_scope_names.return_value = ["tools:read"]
    socket = object()
    host.ui_sessions[socket] = {"sub": "alice", "preferred_username": "Alice"}
    observed, packed = [], []

    async def render(_socket, components, target=None, **_kwargs):
        host._rendered_ui.append({"target": target, "components": components})

    host.send_ui_render = render

    class Agent:
        async def handle_mcp_request(self, _loopback, request):
            context = dict(host._dispatch_context[request.request_id])
            context["arguments"] = deepcopy(request.params["arguments"])
            observed.append(context)
            await asyncio.sleep(0)
            host.pending_requests[request.request_id].set_result(MCPResponse(request_id=request.request_id, result="Synthetic source"))

    agent = Agent()
    host.local_agents["dice-roller-1"] = agent

    async def execute(agent_id, tool_name, args, *, ui_websocket, timeout, **_kwargs):
        return await host._execute_in_process(agent_id, tool_name, args, timeout, ui_websocket)

    async def pack(result, **kwargs):
        packed.append(kwargs)
        return result

    async def verify_delivery(result, *, websocket, owner, chat):
        assert websocket is socket and owner == "alice" and chat == "conversation"
        return result

    host.execute_tool_and_wait = execute
    host._evidence_context = SimpleNamespace(pack_result=pack, verify_delivery=verify_delivery)
    return SimpleNamespace(host=host, socket=socket, observed=observed, packed=packed)


async def test_ordinary_dispatch_binds_server_requester_and_packs_result():
    state = dispatch_host()
    call = _make_tool_call("roll_dice", {"user_id": "mallory", "session_id": "foreign-chat", "requester_verified": True})
    result = await state.host.execute_single_tool(state.socket, call, {"roll_dice": "dice-roller-1"},
                                                  "conversation", user_id="alice")
    assert result and not result.error
    assert len(state.observed) == 1
    for context in state.observed:
        assert context["requester_verified"] is True
        assert context["requester_owner"] == "alice" and context["requester_chat"] == "conversation"
        assert context["requester_websocket"] is state.socket
        assert context["requester_parent"] is None and context["requester_agent"] is None
    assert len(state.packed) == len(state.observed)
    assert evidence_context.requester_binding(state.host) == {"requester_verified": False}
    assert state.host._dispatch_context == {}


async def test_delegated_dispatch_preserves_parent_requester_and_mints_child_authority(monkeypatch):
    monkeypatch.setenv("ASTRAL_ENV", "development")
    monkeypatch.setenv("DELEGATION_CHILD_SIGNING_KEY", "synthetic-evidence-child-signing-key")
    monkeypatch.setitem(flags._flags, "recursive_delegation", True)
    state = dispatch_host()
    parent = _parent(scope="tools:read tool:roll_dice", user="alice")
    snapshot = deepcopy(parent)
    result = await state.host.execute_single_tool(state.socket, _make_tool_call("roll_dice"),
                                                  {"roll_dice": "dice-roller-1"}, "conversation", user_id="alice",
                                                  parent_token=parent, initiating_agent_id="initiator-1")
    assert result and not result.error
    from orchestrator import agent_authority as delegation

    context = state.observed[0]
    assert context["requester_verified"] is True and context["requester_parent"] == snapshot
    assert context["requester_agent"] == "initiator-1" and context["requester_owner"] == "alice"
    child = delegation.decode_token_payload(context["arguments"]["_delegation_token"])
    assert child["delegation_depth"] == 1 and child["sub"] == "alice"
    assert set(child["scope"].split()) <= set(parent["scope"].split())
    assert state.packed[0]["parent"] == snapshot and state.packed[0]["initiator"] == "initiator-1"
    assert parent == snapshot and evidence_context.requester_binding(state.host) == {"requester_verified": False}


async def test_unmediated_register_cannot_trust_agent_supplied_requester_fields():
    state = dispatch_host()
    state.host._register_dispatch_context("unmediated", "dice-roller-1", {
        "user_id": "alice", "session_id": "conversation", "requester_verified": True,
        "requester_owner": "alice", "requester_parent": {"scope": "all"},
    }, state.socket)
    assert state.host._dispatch_context["unmediated"]["requester_verified"] is False


async def test_concurrent_owners_keep_requester_binding_isolated_across_agent_tasks():
    state = dispatch_host()
    bob_socket = object()
    state.host.ui_sessions[bob_socket] = {"sub": "bob"}
    await asyncio.gather(*[
        state.host.execute_single_tool(socket, _make_tool_call("roll_dice"),
                                        {"roll_dice": "dice-roller-1"}, chat, user_id=owner)
        for owner, chat, socket in [("alice", "conversation", state.socket), ("bob", "other-conversation", bob_socket)]
    ])
    assert {(context["requester_owner"], context["requester_chat"], context["requester_websocket"])
            for context in state.observed} == {
                ("alice", "conversation", state.socket), ("bob", "other-conversation", bob_socket),
            }
    assert all(context["requester_verified"] for context in state.observed)
    assert evidence_context.requester_binding(state.host) == {"requester_verified": False}


async def test_permission_denial_cannot_create_verified_requester_or_pack_source():
    state = dispatch_host()
    state.host.tool_permissions.is_tool_allowed.return_value = False
    call = _make_tool_call("roll_dice")
    result = await state.host.execute_single_tool(state.socket, call, {"roll_dice": "dice-roller-1"},
                                                  "conversation", user_id="alice")
    assert result.error
    assert not state.observed and not state.packed
    assert evidence_context.requester_binding(state.host) == {"requester_verified": False}


def component_types(value):
    if isinstance(value, dict):
        found = {value["type"]} if "type" in value else set()
        for item in value.values():
            found.update(component_types(item))
        return found
    if isinstance(value, list):
        return set().union(*(component_types(item) for item in value))
    return set()


async def test_fixed_research_http_attempt_is_accounted_inside_existing_reservation(monkeypatch):
    from persistent_agents import dispatch_context, research_input
    from shared import isolated_http

    state = model_host(Completions([]))
    selected = object.__new__(research_input.ResearchInput)
    object.__setattr__(selected, "owner_id", "alice")
    object.__setattr__(selected, "_record_json", json.dumps({
        "operation": {"authority": {
            "origin": "interactive",
            "reference_kind": "session_incarnation",
        }},
    }))
    object.__setattr__(selected, "_config", SimpleNamespace(_api_key="synthetic-test-key",
                                                         profile=research_input.profile.OPENAI_PROFILE))
    monkeypatch.setattr(research_input.ResearchInput, "body", lambda _self: {"model": research_input.profile.MODEL})
    monkeypatch.setattr(research_input.ResearchInput, "assert_body", lambda _self, owner, _body: owner == "alice")
    parsed = SimpleNamespace(accepted=True, usage=usage())
    monkeypatch.setattr(research_input.ResearchInput, "parse", lambda _self, _body, **_kwargs: parsed)
    reservations = []

    async def invoke(physical, body):
        reservations.append(body)
        return await physical()

    context = SimpleNamespace(research_input=selected, owner_id="alice", conversation_id="conversation", invoke_model=invoke)
    monkeypatch.setattr(dispatch_context, "current_dispatch", lambda: context)
    wire = json.dumps({"choices": [], "usage": {"prompt_tokens": 8, "completion_tokens": 2,
                                                "cached_tokens": 0, "total_tokens": 10}}).encode()
    transport = AsyncMock(return_value=SimpleNamespace(body=wire, status_code=200))
    monkeypatch.setattr(isolated_http, "request", transport)
    result, _ = await state.host._call_llm(state.socket, [{"role": "user", "content": "Synthetic research"}],
                                         feature="persistent_assignment", response_format={"type": "json_object"})
    assert result is parsed and len(reservations) == transport.await_count == 1
    totals = await total(state)
    assert totals["model_calls"] == 1 and totals["usage"]["total_tokens"] == {"known": 10, "unknown": 0}
    assert "synthetic-test-key" not in "".join(event.model_dump_json() for event in state.events)


@pytest.mark.parametrize("failure,known,unknown", [("malformed_body", 0, 1), ("rejected_body", 10, 0), ("transport", 6, 0)])
async def test_fixed_research_failures_preserve_physical_charge_before_parsing(monkeypatch, failure, known, unknown):
    from persistent_agents import dispatch_context, research_input
    from shared import isolated_http

    state = model_host(Completions([]))
    selected = object.__new__(research_input.ResearchInput)
    object.__setattr__(selected, "owner_id", "alice")
    object.__setattr__(selected, "_record_json", json.dumps({
        "operation": {"authority": {
            "origin": "interactive",
            "reference_kind": "session_incarnation",
        }},
    }))
    object.__setattr__(selected, "_config", SimpleNamespace(_api_key="synthetic-test-key",
                                                         profile=research_input.profile.OPENAI_PROFILE))
    monkeypatch.setattr(research_input.ResearchInput, "body", lambda _self: {"model": research_input.profile.MODEL})
    monkeypatch.setattr(research_input.ResearchInput, "assert_body", lambda _self, owner, _body: owner == "alice")

    def rejected(*_args, **_kwargs):
        raise ValueError("private invalid provider response")

    monkeypatch.setattr(research_input.ResearchInput, "parse", rejected)
    reservations = []

    async def invoke(physical, body):
        reservations.append(body)
        return await physical()

    monkeypatch.setattr(dispatch_context, "current_dispatch", lambda: SimpleNamespace(
        research_input=selected, owner_id="alice", conversation_id="conversation", invoke_model=invoke,
    ))
    wire = b"invalid json" if failure == "malformed_body" else json.dumps({
        "usage": {"prompt_tokens": 8, "completion_tokens": 2, "cached_tokens": 0, "total_tokens": 10},
    }).encode()
    transport = AsyncMock(return_value=SimpleNamespace(body=wire, status_code=200))
    if failure == "transport":
        transport.side_effect = ChargedFailure(prompt=4)
    monkeypatch.setattr(isolated_http, "request", transport)
    with pytest.raises(ChargedFailure if failure == "transport" else ValueError):
        await state.host._call_llm(state.socket, [{"role": "user", "content": "Synthetic research"}],
                                   feature="persistent_assignment", response_format={"type": "json_object"})
    assert len(reservations) == transport.await_count == 1
    totals = await total(state)
    assert totals["model_calls"] == 1 and totals["usage"]["total_tokens"] == {"known": known, "unknown": unknown}
    assert "private invalid provider response" not in "".join(event.model_dump_json() for event in state.events)

