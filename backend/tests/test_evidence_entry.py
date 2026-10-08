"""Exercises evidence entry, context limits, and service lifetime through the real host methods.
Signed human fixtures retain the ordinary authority boundary, while synthetic providers isolate physical model calls.
"""

import asyncio
from concurrent.futures import TimeoutError as FutureTimeout
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.evidence.evidence_agent import EvidenceAgent
from agents.evidence.mcp_server import TOOL_REGISTRY
from orchestrator import context_authority, evidence_context, local_agents
from orchestrator.context_presentation import evidence_components
from orchestrator.context_usage import ContextUsage
from orchestrator.evidence_context import EvidenceContext
from orchestrator.orchestrator import Orchestrator
from orchestrator.tests.test_dispatch_local_agents_040 import _make_card
from persistent_agents.models import AssignmentError
from shared.agent_runtime import AgentRuntime
from shared.feature_flags import flags
from shared.protocol import MCPRequest, MCPResponse
from tests.test_context_authority import (
    admitted_turn, bound as bound, fixture as fixture, human as human,
    runtime as runtime, service as service, signing_key as signing_key,
)
from tests.test_evidence_dispatch import (
    Completions, _make_tool_call, authorized_prepare, dispatch_host, model_host, response,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def entry_controls(monkeypatch):
    monkeypatch.setattr(flags, "is_enabled", lambda name: name == "observation_packing")
    monkeypatch.setenv("FF_LLM_STREAMING", "false")


@pytest.mark.parametrize("outcome", ["success", "failure", "cancellation"])
@pytest.mark.parametrize("cached", [False, True])
async def test_original_admitted_chat_entry_installs_and_closes_readonly_context(human, bound, fixture,
                                                                              monkeypatch, outcome, cached):
    async with admitted_turn(human, bound, fixture, operation=True) as turn:
        host, socket, chat, binding = turn
        if cached:
            host._evidence_context = SimpleNamespace()
            monkeypatch.setattr(flags, "is_enabled", lambda _name: False)
        host._handle_chat_message_with_guidance = Orchestrator._handle_chat_message_with_guidance.__get__(host)
        host._begin_conversation_publication = AsyncMock(return_value=(None, None, None))
        host._begin_detached_conversation_publication = AsyncMock(return_value=(None, None))
        observed = []

        async def implementation(actual_socket, message, actual_chat, _display, **kwargs):
            assert actual_socket is socket and actual_chat == chat and message == "Read captured evidence"
            assert kwargs["user_id"] == fixture[1] and kwargs["conversation_server_initiated"] is True
            lease = context_authority.current_context_authority(orchestrator=host, websocket=socket, chat_id=chat)
            observed.append(lease)

            async def child():
                proof = await lease.verify(orchestrator=host, websocket=socket, chat_id=chat)
                assert proof.owner_id == fixture[1] and proof.operation_fences == (binding.operations[0].fence,)
                with pytest.raises(AssignmentError, match="human_write_required"):
                    lease.require_write()

            await asyncio.create_task(child())
            if outcome == "failure":
                raise RuntimeError("synthetic implementation failure")
            if outcome == "cancellation":
                raise asyncio.CancelledError()
            return "synthetic accepted turn"

        host._handle_chat_message_impl = implementation
        if outcome == "success":
            assert await Orchestrator.handle_chat_message(host, socket, "Read captured evidence", chat,
                                                          user_id=fixture[1]) == "synthetic accepted turn"
        else:
            with pytest.raises(RuntimeError if outcome == "failure" else asyncio.CancelledError):
                await Orchestrator.handle_chat_message(host, socket, "Read captured evidence", chat, user_id=fixture[1])
        assert len(observed) == 1
        with pytest.raises(AssignmentError):
            await observed[0].verify(orchestrator=host, websocket=socket, chat_id=chat)
        with pytest.raises(AssignmentError):
            context_authority.current_context_authority(orchestrator=host, websocket=socket, chat_id=chat)
        host._begin_detached_conversation_publication.assert_awaited_once()


async def test_disabled_entry_preserves_original_internal_dispatch():
    host = SimpleNamespace(_get_user_id=lambda _socket: "alice")
    host._begin_conversation_publication = AsyncMock(return_value=(None, None, None))
    host._handle_chat_message_with_guidance = Orchestrator._handle_chat_message_with_guidance.__get__(host)
    observed = []

    async def implementation(socket, _message, chat, _display, **_kwargs):
        with pytest.raises(AssignmentError):
            context_authority.current_context_authority(orchestrator=host, websocket=socket, chat_id=chat)
        observed.append(True)
        return "ordinary internal result"

    host._handle_chat_message_impl = implementation
    from unittest.mock import patch
    with patch.object(flags, "is_enabled", return_value=False):
        assert await Orchestrator.handle_chat_message(host, object(), "hello", "conversation") == "ordinary internal result"
    assert observed == [True] and not hasattr(host, "_evidence_context")


def conversation_host(outcomes, *, rows=None):
    state = dispatch_host()
    host = state.host
    provider = model_host(Completions(outcomes))
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
    host.history.get_chat.return_value = {"title": "Existing title", "messages": rows or [
        {"role": "user", "content": "prior"}, {"role": "assistant", "content": "prior"},
        {"role": "user", "content": "Read captured evidence"},
    ]}
    host.workspace = SimpleNamespace(alive_rows=AsyncMock(return_value=[]), snapshot=MagicMock())
    host.agent_cards = {"evidence-1": _make_card("evidence-1", ["recall_observation"])}
    host.agents = {"evidence-1": object()}
    host._evidence_context.usage = provider.ledger
    host._evidence_context.prepare_model = authorized_prepare
    host._design_turn_post_done = AsyncMock(return_value=None)
    host._publish_conversation_snapshot = AsyncMock()

    async def prepare(messages, **_kwargs):
        return SimpleNamespace(messages=messages, status="unchanged", reason="within_budget")

    host._evidence_context.prepare = AsyncMock(side_effect=prepare)
    state.provider = provider
    return state


def complete_history():
    rows = [{"role": "user" if number % 2 == 0 else "assistant", "content": [
        {"type": "text", "content": f"Row {number}: " + "S" * 2500},
    ]} for number in range(24)]
    return rows + [{"role": "user", "content": "Read captured evidence"}]


@pytest.mark.parametrize("status", ["context_limit", "accepted"])
async def test_context_gate_receives_full_history_and_uses_only_request_local_proposal(status):
    rows = complete_history()
    original = deepcopy(rows)
    state = conversation_host([response("Synthetic answer")], rows=rows)
    host, seen = state.host, []
    proposal = [{"role": "system", "content": "Synthetic deterministic summary"},
                {"role": "user", "content": "Read captured evidence"}]

    async def prepare(messages, **kwargs):
        seen.append(deepcopy(messages))
        assert kwargs["owner"] == "alice" and kwargs["chat"] == "conversation"
        assert kwargs["overhead_tokens"] > 0
        return SimpleNamespace(messages=deepcopy(proposal), status=status, reason="synthetic boundary")

    host._evidence_context.prepare.side_effect = prepare
    await host._handle_chat_message_impl(state.socket, "Read captured evidence", "conversation", user_id="alice",
                                         selected_tools=["recall_observation"])
    assert len(seen) == 1
    assert [item["content"] for item in seen[0] if item["role"] != "system"] == [
        json.dumps(row["content"]) for row in rows[:-1]
    ] + ["Read captured evidence"]
    assert rows == original and host.history.get_chat.return_value["messages"] == original
    if status == "context_limit":
        assert state.provider.completions.calls == []
        assert host._append_conversation_message.await_count == 1
        assert any("Context limit; history retained." in str(call) for call in host._safe_send.await_args_list)
        assert host._rendered_ui[-1]["components"][0]["label"] == "Context limit"
    else:
        assert state.provider.completions.calls[0]["messages"] == proposal
        assert "TRUNCATED" not in repr(state.provider.completions.calls)
        assert host._rendered_ui[0]["components"][0]["label"] == "Generated summary"
    host.history.update_message.assert_not_called()
    host.workspace.snapshot.assert_not_called()


@pytest.mark.parametrize("failure", ["grammar", "agent", "delivery"])
async def test_unavailable_evidence_command_renders_bound_denial_and_keeps_user_publication(failure):
    state = conversation_host([])
    host = state.host
    result = MCPResponse(result={"text": "synthetic source"}, ui_components=evidence_components(state="source",
                         text="synthetic source"))
    host.execute_single_tool = AsyncMock(return_value=result)
    host._evidence_context.verify_delivery = AsyncMock(return_value=MCPResponse(error={"message": "not authorized"}))
    if failure == "agent":
        host.agent_cards.clear()
    message = "/evidence invalid" if failure == "grammar" else "/evidence recall obs_" + "x" * 43
    stage = SimpleNamespace(publication_role="assistant_result")
    await host._handle_chat_message_impl(state.socket, message, "conversation", user_id="alice",
        conversation_stage=stage, conversation_request_generation="synthetic-generation")
    assert host._append_conversation_message.await_count == 1
    assert host._append_conversation_message.await_args.kwargs["role"] == "user"
    assert host._rendered_ui[-1]["components"] == evidence_components(state="blocked")
    assert not state.provider.completions.calls
    host._publish_conversation_snapshot.assert_awaited_once()
    if failure == "delivery":
        host.execute_single_tool.assert_awaited_once()
        host._evidence_context.verify_delivery.assert_awaited_once()
    else:
        host.execute_single_tool.assert_not_awaited()
        host._evidence_context.verify_delivery.assert_not_awaited()


@pytest.mark.parametrize("denial", ["tool", "delivery"])
async def test_unavailable_model_requested_source_keeps_followup_literal_and_transient(denial):
    tool = _make_tool_call("context_usage", {})
    first = response(content=None)
    first.choices[0].message.tool_calls = [tool]
    final = json.dumps([{"type": "button", "label": "Synthetic unavailable-source action",
                         "action": "chat_message", "payload": {"message": "/evidence delete obs_" + "x" * 43}}])
    state = conversation_host([first, response(final)])
    host = state.host
    host.agent_cards = {"evidence-1": _make_card("evidence-1", ["context_usage"])}
    refused = MCPResponse(error={"code": "evidence_unavailable_or_not_authorized", "retryable": False,
                                 "message": "Evidence is unavailable or not authorized."})
    returned = refused if denial == "tool" else MCPResponse(result={"text": "Synthetic permitted text"},
        ui_components=evidence_components(state="source", text="Synthetic permitted text"))
    host.execute_single_tool = AsyncMock(return_value=returned)
    host._evidence_context.verify_delivery = AsyncMock(return_value=refused)
    await host._handle_chat_message_impl(state.socket, "Read captured evidence", "conversation", user_id="alice",
                                         selected_tools=["context_usage"])
    assert len(state.provider.completions.calls) == 2
    assert host._append_conversation_message.await_count == 1
    assert host._append_conversation_message.await_args.kwargs["role"] == "user"
    host._send_or_replace_components.assert_not_awaited()
    host._deliver_round_components.assert_not_awaited()
    host._design_turn_post_done.assert_not_awaited()
    assert host._rendered_ui[-1]["components"] == evidence_components(state="summary", text=final)


@pytest.mark.parametrize("available", [True, False])
async def test_max_turn_evidence_summary_does_not_enter_durable_publication(available):
    outcomes = []
    for index in range(10):
        value = response(content=None)
        tool = _make_tool_call("recall_observation", {"reference": "obs_" + "x" * 43, "offset": index})
        tool.id = f"call_{index}"
        value.choices[0].message.tool_calls = [tool]
        outcomes.append(value)
    state = conversation_host(outcomes)
    host = state.host
    source = "SYNTHETIC_MAX_TURN_PRIVATE_SOURCE"
    returned = MCPResponse(result={"text": source, "untrusted": True}, ui_components=[
        {"type": "keyvalue", "items": [{"key": "Captured source", "value": source}]},
    ])
    host.execute_single_tool = AsyncMock(return_value=returned)
    summary = evidence_components(state="summary", text=source) if available else None
    host._generate_tool_summary = AsyncMock(return_value=summary)
    stage = SimpleNamespace(publication_role="assistant_result", set_completion_summary=MagicMock())
    await host._handle_chat_message_impl(state.socket, "Read captured evidence", "conversation", user_id="alice",
        selected_tools=["recall_observation"], conversation_stage=stage,
        conversation_request_generation="synthetic-generation")
    assert len(state.provider.completions.calls) == host.execute_single_tool.await_count == 10
    assert host._append_conversation_message.await_count == 1
    assert host._append_conversation_message.await_args.kwargs["role"] == "user"
    stage.set_completion_summary.assert_not_called()
    host._design_turn_post_done.assert_not_awaited()
    host._send_or_replace_components.assert_not_awaited()
    host._deliver_round_components.assert_not_awaited()
    host.workspace.snapshot.assert_not_called()
    host._publish_conversation_snapshot.assert_awaited_once()
    if available:
        assert host._rendered_ui[-1]["components"] == summary
        assert source not in repr(host._append_conversation_message.await_args_list)
    else:
        assert host._rendered_ui[-1]["components"][0]["label"] == "Recall blocked"
        assert "were completed" not in repr(host._rendered_ui[-1])


def lifetime_host(monkeypatch):
    from orchestrator import orchestrator as module

    monkeypatch.setattr(module, "_unbind_orchestrator_process_consumers", lambda _host: None)
    host = object.__new__(Orchestrator)
    host._startup_background_tasks = set()
    host.async_task_manager = SimpleNamespace(drain=AsyncMock(return_value=0), stop_retention_sweep=AsyncMock())
    host.runtime_composition = SimpleNamespace(start=MagicMock(), close=AsyncMock())
    host.human_request_boundary = SimpleNamespace(close=MagicMock())
    host._owned_audit_recorder = SimpleNamespace(close=AsyncMock())
    return host


async def test_shutdown_drains_late_provider_charge_before_closing_audit_and_runtime(monkeypatch):
    host = lifetime_host(monkeypatch)
    order, entered, finish = [], asyncio.Event(), asyncio.Event()

    async def record(event):
        order.append(event.outputs_meta["usage_record"]["phase"])

    async def physical():
        entered.set()
        await finish.wait()
        return response()

    ledger = ContextUsage(record)
    host._evidence_context = EvidenceContext(host, usage=ledger)
    host._evidence_context.start()
    task = asyncio.create_task(ledger.model_call(owner_id="alice", conversation_id="conversation",
        purpose="chat_dispatch", model="synthetic-model", invoke=physical))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    async def audit_close():
        totals = await ledger.totals("alice", "conversation")
        assert totals["pending"] == 0 and totals["usage"]["total_tokens"] == {"known": 10, "unknown": 0}
        order.append("audit_closed")

    host._owned_audit_recorder.close.side_effect = audit_close
    closing = asyncio.create_task(host._close_started_services())
    await asyncio.sleep(0)
    assert not closing.done() and "audit_closed" not in order
    finish.set()
    await closing
    assert order[-2:] == ["settled", "audit_closed"]
    assert host._evidence_context._sweep_task is None
    host.runtime_composition.close.assert_awaited_once()
    await host._close_started_services()
    host._owned_audit_recorder.close.assert_awaited_once()


@pytest.mark.parametrize("failure", ["ordinary", "cancellation"])
async def test_evidence_shutdown_failure_still_closes_other_authority_services(monkeypatch, failure):
    host = lifetime_host(monkeypatch)
    error = RuntimeError("synthetic shutdown failure") if failure == "ordinary" else asyncio.CancelledError()
    host._evidence_context = SimpleNamespace(close=AsyncMock(side_effect=error))
    with pytest.raises(type(error)):
        await host._close_started_services()
    host.human_request_boundary.close.assert_called_once()
    host._owned_audit_recorder.close.assert_awaited_once()
    host.runtime_composition.close.assert_awaited_once()
    assert host._started_services_close_task is None


@pytest.mark.parametrize("enabled", [False, True])
async def test_startup_constructs_evidence_service_only_when_enabled_and_starts_once(monkeypatch, enabled):
    from orchestrator import session_store
    from orchestrator import orchestrator as module

    monkeypatch.setattr(flags, "is_enabled", lambda name: enabled and name == "observation_packing")
    monkeypatch.setenv("ASTRAL_ENV", "development")
    monkeypatch.setenv("USE_MOCK_AUTH", "false")
    monkeypatch.delenv("A2A_EXTERNAL_AGENTS", raising=False)
    monkeypatch.setattr(session_store, "assert_production_posture", lambda: None)
    host = lifetime_host(monkeypatch)
    host.voice_services = None
    host.generated_agent_publication_service = SimpleNamespace(
        recover_once=AsyncMock(return_value=SimpleNamespace(degraded_publication_ids=())),
        start=MagicMock(), close=AsyncMock(),
    )

    async def idle(*_args):
        await asyncio.Event().wait()

    def track(coroutine, *, name):
        coroutine.close()

    host._track_startup_background_task = track
    host.explicit_notes = SimpleNamespace(expiry_loop=idle)
    host._jwks_warm_loop = idle
    host._personal_agent_watchdog_task = None
    host._personal_agent_watchdog_loop = idle
    host._start_phi_warm = MagicMock()
    host.lifecycle_manager = SimpleNamespace(reconcile_orphaned_draft_permissions=lambda: 0,
        reconcile_legacy_directory_ownership=lambda: None)
    host._monitor_agents = idle
    host.task_manager = SimpleNamespace(prune_missing=MagicMock())
    host.async_task_manager.start_retention_sweep = MagicMock()
    serving = AsyncMock()
    monkeypatch.setattr(module.uvicorn, "Server", lambda _config: SimpleNamespace(serve=serving))
    if enabled:
        assert evidence_context.get_context(host) is not None
    else:
        assert evidence_context.get_context(host) is None
    before = getattr(host, "_evidence_context", None)
    await host._run_started_server()
    serving.assert_awaited_once()
    if enabled:
        assert host._evidence_context is before and host._evidence_context._sweep_task is not None
        running = host._evidence_context._sweep_task
        host._evidence_context.start()
        assert host._evidence_context._sweep_task is running
    else:
        assert not hasattr(host, "_evidence_context")
    await host._close_started_services()
    if enabled:
        assert host._evidence_context._sweep_task is None


@pytest.fixture
def evidence_agent(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_KEY_PATH", str(tmp_path / "synthetic-agent.pem"))
    monkeypatch.setenv("EVIDENCE_AGENT_PORT", "8765")
    monkeypatch.setenv("LETS_MODE", "off")
    host = SimpleNamespace(_evidence_context=SimpleNamespace(tool=AsyncMock()))
    return EvidenceAgent(host, port=8765), host


@pytest.mark.parametrize("name,arguments", [
    ("recall_observation", {"reference": "obs_" + "x" * 43, "offset": 0}),
    ("delete_observation", {"reference": "obs_" + "x" * 43}),
    ("context_usage", {}),
])
async def test_evidence_agent_uses_real_runtime_and_strips_untrusted_owner_arguments(evidence_agent, name, arguments):
    agent, host = evidence_agent
    socket = SimpleNamespace(send_text=AsyncMock())
    host._evidence_context.tool.return_value = MCPResponse(request_id="synthetic-call", result={"status": "bounded"})
    request = MCPRequest(request_id="synthetic-call", method="tools/call", params={"name": name, "arguments": {
        **arguments, "_runtime": "forged-runtime", "_authority": "forged-authority",
        "user_id": "mallory", "session_id": "foreign-chat",
    }})
    await agent.handle_mcp_request(socket, request)
    host._evidence_context.tool.assert_awaited_once_with("synthetic-call", name, arguments)
    assert type(request.params["arguments"]["_runtime"]) is AgentRuntime
    assert request.params["arguments"]["_runtime"].agent_id == "evidence-1"
    frame = json.loads(socket.send_text.await_args.args[0])
    assert frame["result"] == {"status": "bounded"} and frame["responder_info"]["name"] == "evidence-1"


async def test_evidence_tools_list_publishes_existing_scopes_and_bounded_schemas(evidence_agent):
    agent, _host = evidence_agent
    listed = agent.mcp_server.process_request(MCPRequest(request_id="tools", method="tools/list"))
    assert {item["name"] for item in listed.result["tools"]} == set(TOOL_REGISTRY)
    assert {skill.id: skill.scope for skill in agent.card.skills} == {
        "recall_observation": "tools:read", "delete_observation": "tools:write", "context_usage": "tools:read",
    }
    assert TOOL_REGISTRY["recall_observation"]["input_schema"]["properties"]["reference"]["maxLength"] == 47


@pytest.mark.parametrize("invalid", ["method", "tool", "tool_list", "tool_object", "arguments", "runtime",
                                    "request", "agent", "loop", "disabled"])
async def test_evidence_mcp_refuses_untrusted_or_unavailable_runtime(evidence_agent, monkeypatch, invalid):
    agent, host = evidence_agent
    request = MCPRequest(request_id="synthetic-call", method="tools/call",
                         params={"name": "context_usage", "arguments": {}})
    loop = asyncio.get_running_loop()
    closed_loop = None
    if invalid == "loop":
        closed_loop = asyncio.new_event_loop()
        closed_loop.close()
        loop = closed_loop
    runtime = AgentRuntime(object(), request, "evidence-1", loop)
    request.params["arguments"]["_runtime"] = runtime
    if invalid == "method":
        request.method = "unexpected"
    elif invalid == "tool":
        request.params["name"] = "unexpected"
    elif invalid == "tool_list":
        request.params["name"] = ["context_usage"]
    elif invalid == "tool_object":
        request.params["name"] = {"name": "context_usage"}
    elif invalid == "arguments":
        request.params["arguments"] = "unexpected"
    elif invalid == "runtime":
        request.params["arguments"]["_runtime"] = SimpleNamespace(request_id=request.request_id, agent_id="evidence-1")
    elif invalid == "request":
        runtime.request_id = "foreign-request"
    elif invalid == "agent":
        runtime.agent_id = "foreign-agent"
    elif invalid == "disabled":
        host._evidence_context = None
        monkeypatch.setattr(flags, "is_enabled", lambda _name: False)
    value = await asyncio.to_thread(agent.mcp_server.process_request, request)
    assert value.request_id == request.request_id and value.result is None
    assert value.error == {"code": "evidence_unavailable_or_not_authorized", "retryable": False,
                           "message": "Evidence is unavailable or not authorized."}
    if host._evidence_context is not None:
        host._evidence_context.tool.assert_not_awaited()


@pytest.mark.parametrize("failure", ["timeout", "ordinary"])
async def test_evidence_mcp_failure_redacts_provider_details_and_cancels_timeout(evidence_agent, monkeypatch, failure):
    agent, host = evidence_agent
    request = MCPRequest(request_id="synthetic-call", method="tools/call",
                         params={"name": "context_usage", "arguments": {}})
    request.params["arguments"]["_runtime"] = AgentRuntime(object(), request, "evidence-1", asyncio.get_running_loop())
    future = SimpleNamespace(result=MagicMock(side_effect=FutureTimeout("private-body") if failure == "timeout"
                                            else RuntimeError("private-body")), cancel=MagicMock())

    def deferred(coroutine, _loop):
        coroutine.close()
        return future

    monkeypatch.setattr("agents.evidence.mcp_server.asyncio.run_coroutine_threadsafe", deferred)
    value = agent.mcp_server.process_request(request)
    assert value.error["code"] == "evidence_unavailable_or_not_authorized" and "private-body" not in value.to_json()
    assert future.cancel.call_count == (1 if failure == "timeout" else 0)


@pytest.mark.parametrize("packing,compaction", [(False, False), (True, False), (False, True), (True, True)])
async def test_builtin_evidence_registration_is_default_off_and_follows_either_flag(monkeypatch, evidence_agent,
                                                                                  packing, compaction):
    from shared import attachment_materializer, attachment_resolver

    agent, _host = evidence_agent
    monkeypatch.setattr(flags, "is_enabled", lambda name: {
        "observation_packing": packing, "safe_compaction": compaction,
    }.get(name, False))
    monkeypatch.setattr(local_agents, "discover_built_in_agent_dirs", lambda: [])
    monkeypatch.setattr(attachment_resolver, "register_plane_runtime", lambda *_args: False)
    monkeypatch.setattr(attachment_materializer, "register_materialization_service", lambda *_args: None)
    plane = SimpleNamespace(runtime=object(), repositories=object(), blobs=object(), attachment_materializer=object())
    host = SimpleNamespace(runtime_composition=SimpleNamespace(plane=plane), local_agents={}, register_agent=AsyncMock())
    registered = await local_agents.register_built_ins(host)
    assert registered == (["evidence-1"] if packing or compaction else [])
    if registered:
        actual = host.local_agents["evidence-1"]
        assert isinstance(actual, EvidenceAgent) and actual.mcp_server._orchestrator is host
        host.register_agent.assert_awaited_once()
        assert host.register_agent.await_args.args[0] is None
        assert host.register_agent.await_args.args[1].agent_card.agent_id == agent.agent_id
    else:
        host.register_agent.assert_not_awaited()
        assert host.local_agents == {}
