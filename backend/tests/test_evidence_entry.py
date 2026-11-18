
import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator import context_authority, evidence_context, slash_commands, user_skills
from orchestrator.context_presentation import evidence_components, persistent_reference_components, usage_components
from orchestrator.context_usage import ContextUsage
from orchestrator.evidence_context import EvidenceContext
from orchestrator.orchestrator import Orchestrator
from orchestrator.tests.test_dispatch_local_agents_040 import _make_card
from orchestrator.user_skill_catalog import CurrentSkill
from persistent_agents.models import AssignmentError
from shared.feature_flags import FeatureFlags, flags
from shared.protocol import MCPResponse
from tests.test_evidence_dispatch import (
    Completions, _make_tool_call, authorized_prepare, dispatch_host, model_host, response,
)
from tests.test_context_presentation import (
    publication_runtime as publication_runtime,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def entry_controls(monkeypatch):
    monkeypatch.setattr(flags, "is_enabled", lambda name: name == "observation_packing")
    monkeypatch.setenv("FF_LLM_STREAMING", "false")


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
    host._evidence_context.summary_view = AsyncMock(return_value="view_" + "v" * 43)
    host._design_turn_post_done = AsyncMock(return_value=None)
    host._publish_conversation_snapshot = AsyncMock()

    async def prepare(messages, **_kwargs):
        return SimpleNamespace(messages=messages, status="unchanged", reason="within_budget")

    host._evidence_context.prepare = AsyncMock(side_effect=prepare)
    state.provider = provider
    return state


def default_slash_controls(monkeypatch, *, evidence=True, skills=False):
    monkeypatch.delenv("FF_SLASH_COMMANDS", raising=False)
    defaults = FeatureFlags()
    assert defaults.is_enabled("slash_commands")
    active = dict.fromkeys(flags._flags, False)
    active.update(observation_packing=evidence, slash_commands=defaults.is_enabled("slash_commands"),
                  user_skills=skills)
    monkeypatch.setattr(flags, "_flags", active)
    monkeypatch.setattr(flags, "is_enabled", FeatureFlags.is_enabled.__get__(flags))


@pytest.mark.parametrize("alias", [False, True])
async def test_default_slash_evidence_usage_precedes_expansion_and_existing_skill_alias(monkeypatch, alias):
    default_slash_controls(monkeypatch, skills=alias)
    state = conversation_host([])
    host = state.host
    host.agent_cards = {"evidence-1": _make_card("evidence-1", ["context_usage"])}
    totals = await state.provider.ledger.totals("alice", "conversation")
    returned = MCPResponse(result=totals, ui_components=usage_components(totals))
    host.execute_single_tool = AsyncMock(return_value=returned)
    host._evidence_context.verify_delivery = AsyncMock(wraps=host._evidence_context.verify_delivery)
    host._resolve_llm_client_for = AsyncMock(side_effect=AssertionError("unexpected model preflight"))
    host._evidence_context.prepare = AsyncMock(side_effect=AssertionError("unexpected context budget"))
    host._evidence_context.prepare_model = AsyncMock(side_effect=AssertionError("unexpected model admission"))
    skill = CurrentSkill(slug="synthetic-evidence-alias", name="Synthetic command alias",
        instructions="Run the synthetic alias instead of source inspection.", applies_to=("always",),
        command="evidence", enabled=True, skill_id="synthetic-skill", revision=1)
    assert "Synthetic command alias" in slash_commands.expand_message("/evidence usage", {"evidence": skill})
    facade = SimpleNamespace(list=AsyncMock(return_value=[skill]))
    catalog = MagicMock(return_value=facade if alias else None)
    monkeypatch.setattr(user_skills, "store_for", catalog)
    expand = MagicMock(wraps=slash_commands.expand_message)
    monkeypatch.setattr(slash_commands, "expand_message", expand)

    await host._handle_chat_message_impl(state.socket, "/evidence usage", "conversation", user_id="alice")

    host.execute_single_tool.assert_awaited_once()
    args, kwargs = host.execute_single_tool.await_args
    assert args[0] is state.socket and args[2:] == ({"context_usage": "evidence-1"}, "conversation")
    assert args[1].function.name == "context_usage" and json.loads(args[1].function.arguments) == {}
    assert kwargs == {"user_id": "alice"}
    expand.assert_not_called()
    catalog.assert_not_called()
    facade.list.assert_not_awaited()
    host._resolve_llm_client_for.assert_not_awaited()
    host._evidence_context.prepare.assert_not_awaited()
    host._evidence_context.prepare_model.assert_not_awaited()
    host._evidence_context.verify_delivery.assert_awaited_once()
    assert host._append_conversation_message.await_count == 2
    first, last = [call.kwargs for call in host._append_conversation_message.await_args_list]
    assert first["role"] == "user" and first["content"] == "/evidence usage"
    assert last["role"] == "assistant" and last["content"] == persistent_reference_components(returned)
    assert host._rendered_ui[-1]["components"] == last["content"]
    assert not state.provider.completions.calls and not state.provider.events


@pytest.mark.parametrize("message", ["/evidence", "/evidence invalid", "/evidence usage extra",
                                      "/evidence recall obs_short", "/evidence delete obs_short"])
async def test_default_slash_malformed_evidence_command_blocks_before_any_expansion(monkeypatch, message):
    default_slash_controls(monkeypatch)
    state = conversation_host([])
    host = state.host
    host.execute_single_tool = AsyncMock()
    host._evidence_context.verify_delivery = AsyncMock(wraps=host._evidence_context.verify_delivery)
    host._resolve_llm_client_for = AsyncMock(side_effect=AssertionError("unexpected model preflight"))
    host._evidence_context.prepare = AsyncMock(side_effect=AssertionError("unexpected context budget"))
    expand = MagicMock(wraps=slash_commands.expand_message)
    catalog = MagicMock(return_value=None)
    monkeypatch.setattr(slash_commands, "expand_message", expand)
    monkeypatch.setattr(user_skills, "store_for", catalog)

    await host._handle_chat_message_impl(state.socket, message, "conversation", user_id="alice")

    expand.assert_not_called()
    catalog.assert_not_called()
    host.execute_single_tool.assert_not_awaited()
    host._resolve_llm_client_for.assert_not_awaited()
    host._evidence_context.prepare.assert_not_awaited()
    host._evidence_context.verify_delivery.assert_not_awaited()
    assert host._rendered_ui[-1]["components"] == evidence_components(state="blocked")
    assert host._append_conversation_message.await_count == 2
    first, last = [call.kwargs for call in host._append_conversation_message.await_args_list]
    assert first["role"] == "user" and first["content"] == message
    assert last["role"] == "assistant" and last["content"] == evidence_components(state="blocked")
    assert not state.provider.completions.calls and not state.provider.events


async def test_default_slash_disabled_evidence_without_cached_service_preserves_expansion(monkeypatch):
    default_slash_controls(monkeypatch, evidence=False)
    state = conversation_host([response("Synthetic ordinary command response")])
    host = state.host
    del host._evidence_context
    message = "/evidence usage"
    expanded = slash_commands.expand_message(message)
    expand = MagicMock(wraps=slash_commands.expand_message)
    monkeypatch.setattr(slash_commands, "expand_message", expand)
    host.execute_single_tool = AsyncMock()

    await host._handle_chat_message_impl(state.socket, message, "conversation", user_id="alice")

    expand.assert_called_once_with(message, {})
    host.execute_single_tool.assert_not_awaited()
    assert len(state.provider.completions.calls) == 1
    assert state.provider.completions.calls[0]["messages"][-1] == {"role": "user", "content": expanded}
    assert host._append_conversation_message.await_count == 2
    first = host._append_conversation_message.await_args_list[0].kwargs
    assert first["role"] == "user" and first["content"] == message
    assert not hasattr(host, "_evidence_context") and not state.provider.events


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
        assert host._append_conversation_message.await_count == 2
        assert host._append_conversation_message.await_args.kwargs["role"] == "assistant"
        assert host._append_conversation_message.await_args.kwargs["content"] == evidence_components(state="limit")
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
    assert host._append_conversation_message.await_count == 2
    first, last = [call.kwargs for call in host._append_conversation_message.await_args_list]
    assert first["role"] == "user" and first["content"] == message
    assert last["role"] == "assistant" and last["content"] == evidence_components(state="blocked")
    assert not host._rendered_ui
    assert not state.provider.completions.calls
    host._publish_conversation_snapshot.assert_awaited_once()
    if failure == "delivery":
        host.execute_single_tool.assert_awaited_once()
        host._evidence_context.verify_delivery.assert_awaited_once()
    else:
        host.execute_single_tool.assert_not_awaited()
        host._evidence_context.verify_delivery.assert_not_awaited()


@pytest.mark.parametrize("denial", ["tool", "delivery"])
async def test_unavailable_model_requested_source_commits_only_followup_reference_metadata(denial):
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
    assert host._append_conversation_message.await_count == 3
    assert host._append_conversation_message.await_args_list[0].kwargs["role"] == "user"
    assert all(call.kwargs["role"] == "assistant" for call in host._append_conversation_message.await_args_list[1:])
    host._send_or_replace_components.assert_not_awaited()
    host._deliver_round_components.assert_not_awaited()
    host._design_turn_post_done.assert_not_awaited()
    assert host._rendered_ui[-1]["components"] == persistent_reference_components(None,
        kind="summary", view_id="view_" + "v" * 43)
    assert final not in repr(host._rendered_ui) and final not in repr(host._append_conversation_message.await_args_list)
    host._evidence_context.summary_view.assert_awaited_once()
    assert host._evidence_context.summary_view.await_args.args[0] == final


@pytest.mark.parametrize("available", [True, False])
async def test_max_turn_evidence_summary_commits_only_reference_or_unavailable_metadata(available):
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
    summary = persistent_reference_components(None, kind="summary", view_id="view_" + "v" * 43) if available else None
    host._generate_tool_summary = AsyncMock(return_value=summary)
    stage = SimpleNamespace(publication_role="assistant_result", set_completion_summary=MagicMock())
    await host._handle_chat_message_impl(state.socket, "Read captured evidence", "conversation", user_id="alice",
        selected_tools=["recall_observation"], conversation_stage=stage,
        conversation_request_generation="synthetic-generation")
    assert len(state.provider.completions.calls) == host.execute_single_tool.await_count == 10
    assert host._append_conversation_message.await_count == 12
    assert host._append_conversation_message.await_args_list[0].kwargs["role"] == "user"
    assert all(call.kwargs["role"] == "assistant" for call in host._append_conversation_message.await_args_list[1:])
    assert source not in repr(host._append_conversation_message.await_args_list)
    assert source not in repr(host._rendered_ui)
    assert host._generate_tool_summary.await_args.kwargs["evidence_transient"] is True
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
    from orchestrator import boot_posture
    from orchestrator import orchestrator as module

    monkeypatch.setattr(flags, "is_enabled", lambda name: enabled and name == "observation_packing")
    monkeypatch.setenv("ASTRAL_ENV", "development")
    monkeypatch.delenv("A2A_EXTERNAL_AGENTS", raising=False)
    monkeypatch.setattr(boot_posture, "assert_production_posture", lambda: None)
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


