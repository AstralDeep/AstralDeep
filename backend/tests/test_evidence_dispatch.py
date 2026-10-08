"""Exercises evidence authority and physical usage through the orchestrator's
ordinary dispatch, streamed providers, and auxiliary model calls. Synthetic
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
from uuid import uuid4

from openai.types.chat import ChatCompletionMessage, ChatCompletionMessageFunctionToolCall
import pytest

from orchestrator import evidence_context
from orchestrator.context_usage import ContextUsage
from orchestrator.orchestrator import Orchestrator
from orchestrator.tests.test_dispatch_local_agents_040 import _build_orch, _make_card
from shared.feature_flags import flags
from shared.protocol import MCPResponse
from tests.helpers.voice_plane_runtime import isolated_plane_runtime
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
    host._evidence_context = SimpleNamespace(usage=ledger, prepare_model=authorized_prepare)
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


async def test_stream_failure_settles_its_charge_before_nonstream_fallback():
    def failed_stream():
        yield chunk("partial private content", usage(4))
        raise ChargedFailure(prompt=4)

    state = model_host(Completions([failed_stream, response("Recovered", prompt=8)]))
    message, _ = await state.host._call_llm(state.socket, [{"role": "user", "content": "hello"}],
                                          allow_stream=True, stream_chat_id="conversation")
    assert message is not None and message.content == "Recovered"
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


async def test_title_and_tool_summary_contribute_to_whole_conversation_total():
    state = model_host(Completions([response("Synthetic Title", prompt=3), response("Synthetic summary", prompt=5)]))
    await state.host.summarize_chat_title("conversation", "hello", user_id="alice", websocket=state.socket)
    result = await state.host._generate_tool_summary(state.socket, [{"role": "tool", "content": "Synthetic source"}],
                                                    chat_id="conversation", user_id="alice")
    assert result and result[0] == {"type": "badge", "label": "Generated summary", "variant": "warning"}
    assert result[1]["items"] == [{"key": "Permitted text", "value": "Synthetic summary"}]
    totals = await total(state)
    assert totals["model_calls"] == 2 and totals["by_purpose"] == {"chat_title": 1, "tool_summary": 1}
    assert totals["usage"]["total_tokens"] == {"known": 12, "unknown": 0}
    state.host.history.update_chat_title.assert_called_once_with("conversation", "Synthetic Title", user_id="alice")


async def test_enabled_tool_summary_preserves_complete_records_and_returns_literal_labeled_text():
    action_json = json.dumps({"type": "button", "action": "chat_message", "payload": {"message": "synthetic action"}})
    state = model_host(Completions([response(action_json)]))
    messages = [{"role": "tool", "content": f"record-{index}: " + "x" * 1600} for index in range(10)]
    messages[0]["content"] += " obs_" + "x" * 43
    result = await state.host._generate_tool_summary(state.socket, messages, chat_id="conversation", user_id="alice")
    assert json.loads(state.completions.calls[0]["messages"][1]["content"]) == messages
    assert result[0]["label"] == "Generated summary"
    assert result[1]["type"] == "keyvalue" and result[1]["items"][0]["value"] == action_json
    assert all(item["type"] in {"badge", "keyvalue", "alert"} for item in result)


async def test_disabled_tool_summary_keeps_legacy_record_selection_and_presentation(monkeypatch):
    monkeypatch.setattr(flags, "is_enabled", lambda _name: False)
    state = model_host(Completions([response("Legacy summary")]))
    messages = [{"role": "tool", "content": f"record-{index}: " + "x" * 1600} for index in range(10)]
    messages[-1] = {"role": "system", "content": "excluded internal instruction"}
    result = await state.host._generate_tool_summary(state.socket, messages, chat_id="conversation", user_id="alice")
    selected = state.completions.calls[0]["messages"]
    assert len(selected) == 9 and all(item["role"] == "user" for item in selected[1:])
    assert selected[1]["content"] == messages[2]["content"][:1500] + "..."
    assert "excluded internal instruction" not in repr(selected)
    assert result[0]["type"] == "card" and result[0]["title"] == "Round results"
    assert "Legacy summary" in repr(result) and not state.events


async def test_auxiliary_title_and_summary_failures_preserve_charged_usage():
    state = model_host(Completions([ChargedFailure(prompt=3), ChargedFailure(prompt=5)]))
    await state.host.summarize_chat_title("conversation", "hello", user_id="alice", websocket=state.socket)
    assert await state.host._generate_tool_summary(state.socket, [], chat_id="conversation", user_id="alice") is None
    totals = await total(state)
    assert totals["model_calls"] == totals["charged_failed"] == totals["failed"] == 2
    assert totals["usage"]["total_tokens"] == {"known": 12, "unknown": 0}
    state.host.history.update_chat_title.assert_called_once_with("conversation", "hello", user_id="alice")


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


@pytest.mark.parametrize("parallel", [False, True])
async def test_ordinary_and_parallel_dispatch_bind_server_requester_and_pack_result(parallel):
    state = dispatch_host()
    call = _make_tool_call("roll_dice", {"user_id": "mallory", "session_id": "foreign-chat", "requester_verified": True})
    if parallel:
        result = await state.host.execute_parallel_tools(state.socket, [call, call], {"roll_dice": "dice-roller-1"},
                                                         "conversation", user_id="alice")
        assert len(result) == 2 and all(item and not item.error for item in result)
    else:
        result = await state.host.execute_single_tool(state.socket, call, {"roll_dice": "dice-roller-1"},
                                                      "conversation", user_id="alice")
        assert result and not result.error
    assert len(state.observed) == (2 if parallel else 1)
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
    from orchestrator import delegation

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


@pytest.mark.parametrize("parallel", [False, True])
async def test_permission_denial_cannot_create_verified_requester_or_pack_source(parallel):
    state = dispatch_host()
    state.host.tool_permissions.is_tool_allowed.return_value = False
    call = _make_tool_call("roll_dice")
    if parallel:
        result = await state.host.execute_parallel_tools(state.socket, [call], {"roll_dice": "dice-roller-1"},
                                                         "conversation", user_id="alice")
        assert result[0].error
    else:
        result = await state.host.execute_single_tool(state.socket, call, {"roll_dice": "dice-roller-1"},
                                                      "conversation", user_id="alice")
        assert result.error
    assert not state.observed and not state.packed
    assert evidence_context.requester_binding(state.host) == {"requester_verified": False}


async def test_evidence_command_delivers_source_without_assistant_or_workspace_persistence():
    state = dispatch_host()
    source = "SYNTHETIC_PRIVATE_CAPTURED_SOURCE"
    state.host.agent_cards["evidence-1"] = _make_card("evidence-1", ["recall_observation"])
    result = MCPResponse(result={"text": source, "untrusted": True},
                         ui_components=[{"type": "keyvalue", "items": [{"key": "Captured source", "value": source}]}])
    state.host.execute_single_tool = AsyncMock(return_value=result)
    state.host._append_conversation_message = AsyncMock()
    state.host._deliver_round_components = AsyncMock()
    state.host.workspace = MagicMock()
    await state.host._handle_chat_message_impl(state.socket, "/evidence recall obs_" + "x" * 43,
                                               "conversation", user_id="alice", selected_tools=["recall_observation"])
    calls = state.host._append_conversation_message.await_args_list
    assert len(calls) == 1 and calls[0].kwargs["role"] == "user"
    assert source not in repr(calls)
    state.host._deliver_round_components.assert_not_awaited()
    assert not state.host.workspace.mock_calls
    assert state.host._rendered_ui[-1]["components"] == result.ui_components


@pytest.mark.parametrize("agent_id,marked", [("evidence-1", False), ("dice-roller-1", True)])
@pytest.mark.parametrize("final_kind", ["ordinary_summary", "repeated_source", "action_json", "reasoning_source"])
async def test_model_requested_evidence_stays_transient_through_normal_tool_round(monkeypatch, agent_id, marked, final_kind):
    monkeypatch.setattr(flags, "is_enabled", lambda name: name == "observation_packing")
    monkeypatch.setenv("FF_LLM_STREAMING", "false")
    state = dispatch_host()
    host = state.host
    source = "SYNTHETIC_PRIVATE_MODEL_REQUESTED_SOURCE"
    tool = _make_tool_call("recall_observation", {"reference": "obs_" + "x" * 43})
    first = response(content=None)
    first.choices[0].message.tool_calls = [tool]
    final_content = {
        "ordinary_summary": "Source reviewed",
        "repeated_source": source,
        "action_json": json.dumps([{"type": "button", "label": source, "action": "chat_message",
                                    "payload": {"message": "Delete synthetic source"}}]),
        "reasoning_source": "Source reviewed",
    }[final_kind]
    final = response(final_content)
    if final_kind == "reasoning_source":
        final.choices[0].message.reasoning_content = source
    provider = model_host(Completions([first, final]))
    host._resolve_llm_client_for = provider.host._resolve_llm_client_for
    host._CredentialSource = provider.host._CredentialSource
    host._LLMUnavailable = provider.host._LLMUnavailable
    host._llm_unsupported_params = {}
    host.llm_reasoning_effort = None
    host.audit_recorder = None
    host._record_llm_call = AsyncMock()
    host._emit_llm_usage_report = AsyncMock()
    host.rote = provider.host.rote
    host.token_usage = {}
    host.cancelled_sessions = {}
    host._chat_recorders = {}
    host._datamark_sanitize_spans = False
    host._append_conversation_message = AsyncMock(return_value=1)
    host._notify_phi_if_detected = AsyncMock()
    host._deliver_round_components = AsyncMock(return_value=[])
    host._send_or_replace_components = AsyncMock(return_value=[])
    host._broadcast_user_history = AsyncMock()
    host._start_heartbeat = AsyncMock(return_value=SimpleNamespace(cancel=lambda: None))
    host._typesafe_start_routing = AsyncMock(return_value=(None, None))
    host._skill_store = lambda _owner: None
    host.personalization_service = SimpleNamespace(build_prompt_fragment=lambda *_a, **_k: "")
    host.history.get_chat.return_value = {"title": "Existing title", "messages": [
        {"role": "user", "content": "prior"}, {"role": "assistant", "content": "prior"},
        {"role": "user", "content": "Read retained source"},
    ]}
    host.workspace = SimpleNamespace(alive_rows=AsyncMock(return_value=[]), snapshot=MagicMock())
    host.agent_cards = {agent_id: _make_card(agent_id, ["recall_observation"])}
    host.agents = {agent_id: object()}
    host._evidence_context.usage = provider.ledger
    host._evidence_context.prepare_model = authorized_prepare

    async def prepare(messages, **_kwargs):
        return SimpleNamespace(messages=messages, status="unchanged", reason="within_budget")

    host._evidence_context.prepare = prepare
    returned = MCPResponse(result={"text": source, "untrusted": True}, ui_components=[
        {"type": "keyvalue", "items": [{"key": "Captured source", "value": source}]},
    ])
    if marked:
        returned._evidence_transient = True
    host.execute_single_tool = AsyncMock(return_value=returned)
    host.execute_parallel_tools = AsyncMock(return_value=[returned])
    await host._handle_chat_message_impl(state.socket, "Read retained source", "conversation", user_id="alice",
                                         selected_tools=["recall_observation"])
    assert any(source in repr(item["components"]) for item in host._rendered_ui)
    assert source not in repr(host._append_conversation_message.await_args_list)
    assert source not in repr(host._deliver_round_components.await_args_list)
    assert host._append_conversation_message.await_count == 1
    assert host._append_conversation_message.await_args.kwargs["role"] == "user"
    assert any(item["components"][0].get("label") == "Generated summary" for item in host._rendered_ui)
    assert final_content in repr(host._rendered_ui)
    assert "button" not in component_types(host._rendered_ui)
    host._send_or_replace_components.assert_not_awaited()
    host._deliver_round_components.assert_not_awaited()
    host.execute_single_tool.assert_awaited_once()
    host.workspace.snapshot.assert_not_called()
    assert len(provider.completions.calls) == 2


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
    object.__setattr__(selected, "_config", SimpleNamespace(_api_key="synthetic-test-key"))
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
    object.__setattr__(selected, "_config", SimpleNamespace(_api_key="synthetic-test-key"))
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


@pytest.fixture(scope="module")
def recorder_plane():
    with isolated_plane_runtime("evidence_dispatch") as runtime:
        yield runtime


@pytest.mark.parametrize("agent_id,packing", [("evidence-1", False), ("dice-roller-1", True)])
async def test_actual_chat_step_recorder_persists_only_transient_metadata(recorder_plane, monkeypatch, agent_id, packing):
    from orchestrator.chat_steps import ChatStepRecorder

    monkeypatch.setitem(flags._flags, "observation_packing", packing)
    monkeypatch.setenv("FF_OBSERVATION_PACKING", str(packing).lower())
    chat = "evidence-recorder-" + uuid4().hex
    owner = "alice"
    runtime = recorder_plane
    with runtime.transaction() as transaction:
        runtime.repositories.history.conversations.create(
            transaction, owner_id=owner, conversation_id=chat, title="Synthetic chat",
            agent_id=None, created_at=100,
        )
        message = runtime.repositories.history.messages.append(
            transaction, owner_id=owner, conversation_id=chat, role="user",
            content="Inspect synthetic source", timestamp=101,
        )
    socket, emitted = object(), []

    async def send(_socket, payload):
        emitted.append(json.loads(payload))
        return True

    recorder = ChatStepRecorder(
        plane_runtime=runtime, plane_repositories=runtime.repositories,
        websocket=socket, safe_send=send, chat_id=chat, user_id=owner,
        turn_message_id=message.message_id,
    )
    host = Orchestrator.__new__(Orchestrator)
    host._chat_recorders = {id(socket): recorder}
    source = "SYNTHETIC_PRIVATE_SOURCE_MUST_NOT_ENTER_STEPS"
    result = MCPResponse(result={"text": source}, ui_components=[
        {"type": "keyvalue", "items": [{"key": "Captured source", "value": source}]},
    ])
    host._dispatch_tool_call = AsyncMock(return_value=result)
    returned = await host.execute_tool_and_wait(
        agent_id, "recall_observation", {"reference": "obs_" + "x" * 43},
        ui_websocket=socket, protected_owner_id=owner, protected_conversation_id=chat,
    )
    assert returned is result and source in repr(returned.result)
    with runtime.transaction() as transaction:
        steps = runtime.repositories.chat_steps.list_steps(transaction, owner_id=owner, conversation_id=chat)
        foreign = runtime.repositories.chat_steps.list_steps(transaction, owner_id="bob", conversation_id=chat)
    assert len(steps) == 1 and not foreign
    step = steps[0]
    assert step.status.value == "completed" and step.turn_message_id == message.message_id
    assert json.loads(step.result_summary) == {"status": "completed", "source_content": "transient"}
    assert source not in repr((step.args_truncated, step.result_summary, step.error_message, emitted))
    assert [item["step"]["status"] for item in emitted] == ["in_progress", "completed"]
    assert not recorder._in_flight
