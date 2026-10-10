"""Verifies consented provider transmission independently of strict evidence retention.
Real owner authority, current model records and SDK wire requests preserve complete static instructions and source denials.
"""

import asyncio
from collections import Counter
from copy import deepcopy
import importlib
import json
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from llm_config.tests.test_evidence_transport import wire as wire
from agents.evidence.evidence_agent import EvidenceAgent
from orchestrator import context_engineering, datamarking
from orchestrator.context_usage import ContextUsage
from orchestrator.evidence_archive import EvidenceDenied
from orchestrator.evidence_context import _json, has_references
from orchestrator.orchestrator import CHAT_SYSTEM_TEMPLATE, Orchestrator
from orchestrator.tool_visibility import eligible_tool_pairs
from personalization import phi_gate
from personalization.phi_gate import PHIGate
from shared.feature_flags import flags
from shared.protocol import AgentCard, AgentSkill
from tests.test_evidence_model import (
    BASE_URL, MODEL, admitted_model, bound as bound, configured_model as configured_model,
    fixture as fixture, human as human, model_controls as model_controls,
    replace_key, runtime as runtime, service as service, signing_key as signing_key, withdraw_consent,
)
from tests.test_evidence_service import AGENT, evidence_turn, pack

pytestmark = pytest.mark.asyncio


def static_request(state):
    host = state.host
    host.agents = {}
    host.local_agents = {}
    host.agent_cards = {}
    host.security_flags = {}
    host._is_draft_agent = lambda _agent: False
    registries = {}
    for name in ("general", "connectors", "dice_roller", "journal_review", "medical", "ml_services",
                 "summarizer", "weather", "web_research", "evidence"):
        registry = importlib.import_module("agents." + name + ".mcp_server").TOOL_REGISTRY
        agent = name.replace("_", "-") + "-1"
        registries[agent] = registry
        skills = [AgentSkill(name, item["description"], name, scope=item.get("scope", "tools:read"),
                            input_schema=deepcopy(item.get("input_schema", {}))) for name, item in registry.items()]
        host.agent_cards[agent] = AgentCard(agent, "Code-owned catalog", agent, skills=skills)
        host.local_agents[agent] = SimpleNamespace()
        host.tool_permissions.register_tool_scopes(agent, {value.id: value.scope for value in skills})
        host.tool_permissions.set_agent_scopes(state.owner, agent, {value.scope: True for value in skills})
    pairs = eligible_tool_pairs(host, state.owner, identity_claims={"sub": state.owner})
    assert len(pairs) == sum(len(value) for value in registries.values())
    occurrences = Counter(skill.id for _agent, skill in pairs)
    tools = []
    for agent, skill in pairs:
        collided = occurrences[skill.id] > 1
        tools.append({"type": "function", "function": {
            "name": agent + "__" + skill.id if collided else skill.id,
            "description": "[Provider: " + agent + "] " + skill.description if collided else skill.description,
            "parameters": Orchestrator._adapt_tool_schema_for_model(skill.input_schema),
        }})
    system = context_engineering.compose_system_prompt(CHAT_SYSTEM_TEMPLATE, cache_stable=True)
    for name in ("agentic_creation", "scheduling_chat", "memory_chat", "desktop_codegen"):
        module = importlib.import_module("orchestrator." + name)
        tools.extend(module.meta_tool_definitions())
        system += module.SYSTEM_PROMPT_ADDENDUM
    return {"model": MODEL, "messages": [
        {"role": "system", "content": system},
        {"role": "user", "content": "Read my uploaded synthetic document."},
        {"role": "system", "content": datamarking.spotlight_system_addendum(datamarking.make_turn_sentinel())},
    ], "tools": tools, "tool_choice": "auto", "max_tokens": 512}


async def model(state, turn, request):
    client, source, resolved = await state.host._resolve_llm_client_for(
        turn.socket, evidence_bound=has_references(request.get("messages", [])))
    assert source is state.host._CredentialSource.USER
    return await state.host._context_model_call(turn.socket, turn.chat, "conversation", resolved.model,
        lambda: asyncio.to_thread(client.chat.completions.create, **request), request=request,
        base_url=resolved.base_url, provider_capture=client._evidence_provider_capture)


def configure_call(host, turn):
    host._llm_audit_principals = Orchestrator._llm_audit_principals.__get__(host)
    host._valid_reasoning_effort = Orchestrator._valid_reasoning_effort
    host._safe_llm_error_metadata = Orchestrator._safe_llm_error_metadata
    host._is_image_message = Orchestrator._is_image_message
    host._messages_have_images = Orchestrator._messages_have_images.__get__(host)
    host._llm_rejects_images = Orchestrator._llm_rejects_images
    host._llm_unsupported_extras = Orchestrator._llm_unsupported_extras
    host._llm_streaming_enabled = lambda: False
    host._record_llm_call = AsyncMock()
    host._record_llm_unconfigured = AsyncMock()
    host._emit_llm_usage_report = AsyncMock()
    host._ws_active_chat = {id(turn.socket): turn.chat}
    host._llm_unsupported_params = {}
    host._llm_vision_unsupported = set()
    host.audit_recorder = None
    host.MAX_RETRIES = 1


async def test_actual_eligible_catalog_larger_than_retention_limit_is_sent_verbatim(
    configured_model, human, bound, fixture, monkeypatch, wire,
):
    state = configured_model
    state.entry.update(context_tokens=200_000, max_output_tokens=512)
    state.policy.write_text(json.dumps([state.entry]))
    request = static_request(state)
    original = deepcopy(request)
    assert len(_json(request).encode()) > 65_536
    screened = []

    def analyze(**values):
        screened.append(values["text"])
        return [SimpleNamespace(start=value.start(), end=value.end(), entity_type="PERSON")
                for value in re.finditer(r"\bcanvas\b", values["text"], re.IGNORECASE)]

    gate = PHIGate(analyzer=SimpleNamespace(analyze=analyze))
    monkeypatch.setattr(phi_gate, "get_phi_gate", lambda: gate)
    async with admitted_model(state, human, bound, fixture) as turn:
        response = await model(state, turn, request)
        assert response.usage.total_tokens == 10
        assert request == original
        observed, transports, _addresses, _reply = wire
        assert len(observed) == 1 and json.loads(observed[0].content) == original
        assert screened == [] and all(value.is_closed for value in transports)
        altered, changed = gate.redact_for_storage(context_engineering.compose_system_prompt(CHAT_SYSTEM_TEMPLATE))
        assert changed and altered != context_engineering.compose_system_prompt(CHAT_SYSTEM_TEMPLATE)
        await state.ledger.drain()
        totals = await state.ledger.totals(state.owner, turn.chat)
        assert totals["model_calls"] == totals["succeeded"] == 1
        assert state.context.archive.observation_count == 0


@pytest.mark.parametrize("loss", ["consent", "provider", "budget", "request", "origin"])
async def test_complete_sdk_request_stays_current_after_pending_accounting_wait(
    configured_model, human, bound, fixture, wire, loss,
):
    state = configured_model
    request = {"model": MODEL, "messages": [{"role": "user", "content": "Complete consented request."}]}
    changed = []
    current = []

    async def record(event):
        state.events.append(event)
        if event.outputs_meta["usage_record"]["phase"] == "pending" and not changed:
            changed.append(True)
            if loss == "consent":
                await withdraw_consent(state)
            elif loss == "provider":
                await replace_key(state)
            elif loss == "budget":
                state.entry["context_tokens"] = 2048
                state.policy.write_text(json.dumps([state.entry]))
            elif loss == "request":
                request["messages"][0]["content"] = "Changed after admission."
            else:
                current[0].lease._turn.origin.close()

    state.context.usage = state.ledger = ContextUsage(record)
    async with admitted_model(state, human, bound, fixture) as turn:
        current.append(turn)
        with pytest.raises(Exception):
            await model(state, turn, request)
        assert changed == [True] and wire[0] == []
        await state.ledger.drain()
        totals = await state.ledger.totals(state.owner, turn.chat)
        assert totals["model_calls"] == totals["failed"] == 1


@pytest.mark.parametrize("loss", ["permission", "privacy", "service", "adapter", "placement"])
async def test_cached_reference_with_both_controls_off_cannot_escape_current_source_guard(
    configured_model, human, bound, fixture, tmp_path, monkeypatch, wire, loss,
):
    state = configured_model
    async with evidence_turn(human, bound, fixture, tmp_path, monkeypatch) as source:
        state.context = source.evidence
        state.context.usage = state.ledger
        state.host._evidence_context = state.context
        captured = await pack(source)
        assert captured.error is None
        turn = SimpleNamespace(socket=source.socket, chat=source.chat, lease=source.lease)
        configure_call(state.host, turn)
        for name in ("observation_packing", "safe_compaction"):
            monkeypatch.setitem(flags._flags, name, False)
            monkeypatch.setenv("FF_" + name.upper(), "false")
        if loss == "permission":
            await asyncio.to_thread(state.host.tool_permissions.set_agent_scopes, state.owner, AGENT, {"tools:read": False})
        elif loss == "privacy":
            monkeypatch.setattr(phi_gate, "get_phi_gate", lambda: PHIGate(build_if_missing=False))
        elif loss == "service":
            del state.host._evidence_context
        elif loss == "adapter":
            state.host.local_agents.pop("evidence-1")
        else:
            monkeypatch.setitem(flags._flags, "inprocess_agents", False)
            monkeypatch.setenv("FF_INPROCESS_AGENTS", "false")
        messages = [{"role": "user", "content": "Explain captured source " + captured.result["reference"]}]
        original = deepcopy(messages)
        result = await Orchestrator._call_llm(state.host, source.socket, messages, stream_chat_id=source.chat)
        assert result == (None, None) and messages == original
        assert wire[0] == []
        state.host._record_llm_call.assert_awaited_once()
        assert state.host._record_llm_call.await_args.kwargs["outcome"] == "failure"


@pytest.mark.parametrize("boundary", ["pending", "authority"])
@pytest.mark.parametrize("loss", ["adapter", "replacement", "service", "placement"])
async def test_cached_reference_adapter_stays_exact_until_physical_sdk_dispatch(
    configured_model, human, bound, fixture, tmp_path, monkeypatch, wire, boundary, loss,
):
    state = configured_model
    async with evidence_turn(human, bound, fixture, tmp_path, monkeypatch) as source:
        state.context = source.evidence
        state.host._evidence_context = state.context
        captured = await pack(source)
        assert captured.error is None
        state.context.usage = state.ledger
        for name in ("observation_packing", "safe_compaction"):
            monkeypatch.setitem(flags._flags, name, False)
            monkeypatch.setenv("FF_" + name.upper(), "false")
        changed = []

        def revoke():
            if changed:
                return
            changed.append(True)
            if loss == "adapter":
                source.host.local_agents.pop("evidence-1")
            elif loss == "replacement":
                replacement = EvidenceAgent(source.host, port=0)
                source.host.local_agents["evidence-1"] = replacement
                source.host.agent_cards["evidence-1"] = replacement.card
            elif loss == "service":
                del source.host._evidence_context
            else:
                monkeypatch.setitem(flags._flags, "inprocess_agents", False)
                monkeypatch.setenv("FF_INPROCESS_AGENTS", "false")

        if boundary == "pending":
            async def record(event):
                state.events.append(event)
                if event.outputs_meta["usage_record"]["phase"] == "pending":
                    revoke()
            state.context.usage = state.ledger = ContextUsage(record)
        else:
            original = type(source.lease).verify

            async def verify(lease, *args, **kwargs):
                proof = await original(lease, *args, **kwargs)
                revoke()
                return proof

            monkeypatch.setattr(type(source.lease), "verify", verify)
        request = {"model": MODEL, "messages": [{"role": "user", "content": captured.result["reference"]}]}
        before = deepcopy(request["messages"])
        turn = SimpleNamespace(socket=source.socket, chat=source.chat)
        with pytest.raises(EvidenceDenied):
            await model(state, turn, request)
        assert changed == [True] and request["messages"] == before and wire[0] == []
        await state.ledger.drain()
        totals = await state.ledger.totals(state.owner, source.chat)
        assert totals["model_calls"] == totals["failed"] == 1


async def test_cached_reference_with_disabled_controls_uses_real_current_guarded_provider(
    configured_model, human, bound, fixture, tmp_path, monkeypatch, wire,
):
    state = configured_model
    async with evidence_turn(human, bound, fixture, tmp_path, monkeypatch) as source:
        state.context = source.evidence
        state.context.usage = state.ledger
        state.host._evidence_context = state.context
        captured = await pack(source)
        assert captured.error is None
        for name in ("observation_packing", "safe_compaction"):
            monkeypatch.setitem(flags._flags, name, False)
            monkeypatch.setenv("FF_" + name.upper(), "false")
        request = {"model": MODEL, "messages": [{"role": "user", "content": captured.result["reference"]}]}
        response = await model(state, source, request)
        assert response.usage.total_tokens == 10
        assert len(wire[0]) == 1 and json.loads(wire[0][0].content) == request
        await state.ledger.drain()
        totals = await state.ledger.totals(state.owner, source.chat)
        assert totals["model_calls"] == totals["succeeded"] == 1


@pytest.mark.parametrize("stream", [False, True])
async def test_real_sdk_redirect_refusal_records_one_failed_physical_attempt(
    configured_model, human, bound, fixture, wire, stream,
):
    state = configured_model
    wire[3].update(status=307, headers={"location": "https://unapproved.invalid/collect"})
    async with admitted_model(state, human, bound, fixture) as turn:
        request = {"model": MODEL, "messages": [{"role": "user", "content": "Protected synthetic body."}],
                   "stream": stream}
        with pytest.raises(Exception):
            await model(state, turn, request)
        assert len(wire[0]) == 1 and all(value.is_closed for value in wire[1])
        await state.ledger.drain()
        totals = await state.ledger.totals(state.owner, turn.chat)
        assert totals["model_calls"] == totals["failed"] == 1
        assert totals["usage"]["total_tokens"] == {"known": 0, "unknown": 1}


async def test_missing_service_and_uncaptured_reference_denies_even_without_provider_capture(
    configured_model, human, bound, fixture, monkeypatch,
):
    state = configured_model
    async with admitted_model(state, human, bound, fixture) as turn:
        del state.host._evidence_context
        monkeypatch.setitem(flags._flags, "observation_packing", False)
        monkeypatch.setitem(flags._flags, "safe_compaction", False)
        physical = AsyncMock()
        with pytest.raises(EvidenceDenied):
            await state.host._context_model_call(turn.socket, turn.chat, "conversation", MODEL, physical,
                request={"messages": [{"role": "user", "content": "obs_uncaptured"}]}, base_url=BASE_URL)
        physical.assert_not_awaited()


async def test_disabled_reference_free_legacy_provider_remains_unwrapped(
    configured_model, human, bound, fixture, monkeypatch,
):
    state = configured_model
    async with admitted_model(state, human, bound, fixture) as turn:
        del state.host._evidence_context
        for name in ("observation_packing", "safe_compaction"):
            monkeypatch.setitem(flags._flags, name, False)
            monkeypatch.setenv("FF_" + name.upper(), "false")
        client, source, resolved = await state.host._resolve_llm_client_for(turn.socket)
        try:
            assert source is state.host._CredentialSource.USER and str(client.base_url).rstrip("/") == BASE_URL
            assert not hasattr(client, "_evidence_provider_capture")
            expected = object()
            physical = AsyncMock(return_value=expected)
            result = await state.host._context_model_call(turn.socket, turn.chat, "conversation", MODEL, physical,
                request={"messages": [{"role": "user", "content": "Ordinary request."}]}, base_url=resolved.base_url)
            assert result is expected
            physical.assert_awaited_once()
            assert state.events == []
        finally:
            client.close()


async def test_unadmitted_public_plaintext_route_returns_existing_unavailable_posture(
    configured_model, human, bound, fixture, wire,
):
    state = configured_model
    await asyncio.to_thread(state.store.set_sync, state.owner, provider="custom",
        base_url="http://synthetic-model.invalid/v1", model=MODEL, api_key="synthetic-owned-key")
    async with admitted_model(state, human, bound, fixture) as turn:
        configure_call(state.host, turn)
        messages = [{"role": "user", "content": "Original complete conversation request."}]
        assert await Orchestrator._call_llm(state.host, turn.socket, messages, stream_chat_id=turn.chat) == (None, None)
        assert wire[0] == [] and state.events == []
        state.host._record_llm_unconfigured.assert_awaited_once()
