"""Verifies physical model admission against the host's typed human lease and current Plane records.
Operator budgets, consent, provider bindings, and privacy checks remain current through usage recording waits.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import UTC, datetime
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from cryptography.fernet import Fernet
import pytest

from agents.evidence.mcp_server import TOOL_REGISTRY
from llm_config.client_factory import build_llm_client
from llm_config.data_sharing import DataSharingStore
from llm_config.types import CredentialSource, LLMUnavailable
from llm_config.user_store import UserLLMConfigStore
from orchestrator import context_authority
from orchestrator.context_budget import ContextBudgetUnavailable
from orchestrator.context_usage import ContextUsage
from orchestrator.evidence_archive import EvidenceDenied, EvidenceError
from orchestrator.evidence_context import EvidenceContext
from orchestrator.orchestrator import Orchestrator
from personalization import phi_gate
from personalization.phi_gate import PHIGate
from persistent_agents.models import AssignmentError
from shared.feature_flags import flags
from tests.test_context_authority import (
    acquire, admitted_turn, bound as bound, fixture as fixture, human as human,
    runtime as runtime, service as service, signing_key as signing_key,
)
from tests.test_evidence_service import AGENT, evidence_turn, pack
from tests.test_evidence_dispatch import response

pytestmark = pytest.mark.asyncio
MODEL = "synthetic-model"
BASE_URL = "https://synthetic-model.invalid/v1"
PROVIDER = "custom"


@pytest.fixture(autouse=True)
def model_controls(monkeypatch):
    monkeypatch.setitem(flags._flags, "observation_packing", True)
    monkeypatch.setitem(flags._flags, "safe_compaction", False)
    gate = PHIGate(analyzer=SimpleNamespace(analyze=lambda **_kwargs: []))
    monkeypatch.setattr(phi_gate, "get_phi_gate", lambda: gate)


@pytest.fixture
def configured_model(human, fixture, runtime, monkeypatch, tmp_path):
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())
    host, owner = human[2], fixture[1]
    store = UserLLMConfigStore(plane_runtime=runtime, plane_repositories=runtime.repositories)
    store.set_sync(owner, provider=PROVIDER, base_url=BASE_URL, model=MODEL, api_key="synthetic-initial-key")
    consent = DataSharingStore(plane_runtime=runtime, plane_repositories=runtime.repositories)
    consent.acknowledge_sync(owner)
    policy = tmp_path / "context-model.json"
    entry = dict(owner_id=owner, provider=PROVIDER, base_url=BASE_URL, model=MODEL,
                 context_tokens=4096, max_output_tokens=128, output_parameter="max_tokens")
    policy.write_text(json.dumps([entry]))
    monkeypatch.setenv("ASTRAL_CONTEXT_BUDGET_POLICY", str(policy))
    events = []

    async def record(event):
        events.append(event.model_copy(deep=True))

    ledger = ContextUsage(record)
    context = EvidenceContext(host, usage=ledger)
    host._llm_store = store
    host._data_sharing_store = consent
    host._evidence_context = context
    host._CredentialSource = CredentialSource
    host._LLMUnavailable = LLMUnavailable
    host._build_llm_client = build_llm_client
    host._llm_context_user_id = Orchestrator._llm_context_user_id.__get__(host)
    host._context_model_call = Orchestrator._context_model_call.__get__(host)
    host._resolve_llm_client_for = Orchestrator._resolve_llm_client_for.__get__(host)
    host._drain_llm_discard_notes = AsyncMock()
    host.ui_sessions = {}
    return SimpleNamespace(host=host, owner=owner, store=store, consent=consent,
                           policy=policy, entry=entry, context=context, ledger=ledger, events=events)


@asynccontextmanager
async def admitted_model(state, human, bound, fixture):
    async with admitted_turn(human, bound, fixture) as turn:
        host, socket, chat, _binding = turn
        host.ui_sessions[socket] = {"sub": state.owner}
        lease = await acquire(turn)
        try:
            with context_authority.use_context_authority(lease):
                capture = await state.store.capture_user(state.owner)
                yield SimpleNamespace(host=host, socket=socket, chat=chat, lease=lease, capture=capture)
        finally:
            host.ui_sessions.pop(socket, None)


def model_request(**kwargs):
    return dict(model=MODEL, messages=[{"role": "user", "content": "Synthetic request"}], **kwargs)


async def prepare(state, turn, request, **kwargs):
    values = dict(websocket=turn.socket, owner=state.owner, chat=turn.chat,
                  model=MODEL, base_url=BASE_URL, request=request, provider_capture=turn.capture)
    values.update(kwargs)
    return await state.context.prepare_model(**values)


async def invoke(state, turn, request, physical):
    return await state.host._context_model_call(
        turn.socket, turn.chat, "conversation", MODEL, physical,
        request=request, base_url=BASE_URL, provider_capture=turn.capture,
    )


async def replace_key(state, *, key="synthetic-replacement-key"):
    await asyncio.to_thread(state.store.set_sync, state.owner, provider=PROVIDER,
                            base_url=BASE_URL, model=MODEL, api_key=key)


async def withdraw_consent(state):
    def replace_notice():
        with state.store._repository.plane_runtime.transaction() as transaction:
            state.store._repository.plane_runtime.repositories.preferences.data_sharing.acknowledge(
                transaction, owner_id=state.owner, notice_version="synthetic-unaccepted-notice", at=datetime.now(UTC),
            )
    await asyncio.to_thread(replace_notice)


async def test_typed_origin_dispatches_one_bounded_physical_model(configured_model, human, bound, fixture):
    state = configured_model
    async with admitted_model(state, human, bound, fixture) as turn:
        request = model_request(max_completion_tokens=512)
        observed = []
        returned = SimpleNamespace(content="Synthetic provider source", usage=SimpleNamespace(
            prompt_tokens=8, completion_tokens=2, cached_tokens=0, total_tokens=10,
        ))

        async def physical():
            observed.append(deepcopy(request))
            return returned

        assert await invoke(state, turn, request, physical) is returned
        assert observed == [{"model": MODEL, "messages": request["messages"], "max_tokens": 128}]
        await state.ledger.drain()
        totals = await state.ledger.totals(state.owner, turn.chat)
        assert totals["model_calls"] == totals["succeeded"] == 1
        assert totals["usage"]["total_tokens"] == {"known": 10, "unknown": 0}
        assert "Synthetic provider source" not in "".join(event.model_dump_json() for event in state.events)
        assert fixture[-1] == []


async def test_approved_evidence_tool_reference_schema_is_not_a_source_reference(configured_model, human, bound, fixture):
    state = configured_model
    async with admitted_model(state, human, bound, fixture) as turn:
        request = model_request(tools=[{"type": "function", "function": {
            "name": "recall_observation", "description": TOOL_REGISTRY["recall_observation"]["description"],
            "parameters": deepcopy(TOOL_REGISTRY["recall_observation"]["input_schema"]),
        }}])
        physical = AsyncMock(return_value=SimpleNamespace(usage=SimpleNamespace(
            prompt_tokens=8, completion_tokens=2, cached_tokens=0, total_tokens=10,
        )))
        assert await invoke(state, turn, request, physical) is physical.return_value
        physical.assert_awaited_once()
        await state.ledger.drain()
        totals = await state.ledger.totals(state.owner, turn.chat)
        assert totals["model_calls"] == totals["succeeded"] == 1
        assert totals["usage"]["total_tokens"] == {"known": 10, "unknown": 0}
        assert state.context.archive.observation_count == 0


@pytest.mark.parametrize("wait", ["authority", "provider"])
@pytest.mark.parametrize("loss", ["permission", "policy"])
async def test_evidence_source_permission_loss_after_final_authority_wait_refuses_physical_model(
    configured_model, human, bound, fixture, tmp_path, monkeypatch, wait, loss,
):
    state = configured_model
    async with evidence_turn(human, bound, fixture, tmp_path, monkeypatch) as source:
        state.context = source.evidence
        state.context.usage = state.ledger
        state.host._evidence_context = state.context
        captured = await pack(source)
        assert captured.error is None and captured.result["view"] == "partial_preview"
        turn = SimpleNamespace(host=source.host, socket=source.socket, chat=source.chat, lease=source.lease,
                               capture=await state.store.capture_user(state.owner))
        original_source = state.context._source_authorized
        authorized, changed = [], []

        async def source_authorized(*args, **kwargs):
            grant = await original_source(*args, **kwargs)
            authorized.append(True)
            return grant

        monkeypatch.setattr(state.context, "_source_authorized", source_authorized)
        monkeypatch.setenv("FF_POLICY_ENGINE", "true")

        async def revoke():
            if not authorized or changed:
                return
            changed.append(True)
            if loss == "permission":
                await asyncio.to_thread(source.host.tool_permissions.set_agent_scopes,
                                        state.owner, AGENT, {"tools:read": False})
            else:
                monkeypatch.setenv("POLICY_RULES", '[{"effect":"deny"}]')

        if wait == "authority":
            original = type(turn.lease).verify

            async def current_authority(lease, *args, **kwargs):
                proof = await original(lease, *args, **kwargs)
                await revoke()
                return proof

            monkeypatch.setattr(type(turn.lease), "verify", current_authority)
        else:
            original = state.store.capture_user

            async def current_provider(owner):
                provider = await original(owner)
                await revoke()
                return provider

            monkeypatch.setattr(state.store, "capture_user", current_provider)
        request = model_request()
        request["messages"].insert(0, {"role": "tool", "content": json.dumps(captured.result)})
        physical = AsyncMock()
        with pytest.raises(EvidenceDenied):
            await invoke(state, turn, request, physical)
        physical.assert_not_awaited()
        await state.ledger.drain()
        totals = await state.ledger.totals(state.owner, turn.chat)
        assert changed == [True] and totals["model_calls"] == totals["failed"] == 1
        assert totals["usage"]["total_tokens"] == {"known": 0, "unknown": 1}


@pytest.mark.parametrize("loss", ["permission", "consent", "provider", "origin"])
async def test_disabled_flags_do_not_bypass_current_model_authority_for_cached_evidence(
    configured_model, human, bound, fixture, tmp_path, monkeypatch, loss,
):
    state = configured_model
    async with evidence_turn(human, bound, fixture, tmp_path, monkeypatch) as source:
        state.context = source.evidence
        state.context.usage = state.ledger
        state.host._evidence_context = state.context
        captured = await pack(source)
        assert captured.error is None and captured.result["view"] == "partial_preview"
        turn = SimpleNamespace(host=source.host, socket=source.socket, chat=source.chat, lease=source.lease,
                               capture=await state.store.capture_user(state.owner))
        for name in ("observation_packing", "safe_compaction"):
            monkeypatch.setitem(flags._flags, name, False)
            monkeypatch.setenv("FF_" + name.upper(), "false")
        if loss == "permission":
            await asyncio.to_thread(source.host.tool_permissions.set_agent_scopes,
                                    state.owner, AGENT, {"tools:read": False})
        elif loss == "consent":
            await withdraw_consent(state)
        elif loss == "provider":
            await replace_key(state)
        else:
            turn.lease.close()
        request = model_request()
        request["messages"].insert(0, {"role": "tool", "content": json.dumps(captured.result)})
        physical = AsyncMock(return_value=response("Synthetic unauthorized source transformation"))
        with pytest.raises((EvidenceDenied, AssignmentError)):
            await invoke(state, turn, request, physical)
        physical.assert_not_awaited()


async def test_disabled_flags_keep_cached_evidence_summary_guarded_literal_and_complete(
    configured_model, human, bound, fixture, tmp_path, monkeypatch,
):
    state = configured_model
    state.entry["context_tokens"] = 100_000
    state.policy.write_text(json.dumps([state.entry]))
    async with evidence_turn(human, bound, fixture, tmp_path, monkeypatch) as source:
        state.context = source.evidence
        state.context.usage = state.ledger
        state.host._evidence_context = state.context
        captured = await pack(source)
        assert captured.error is None and captured.result["view"] == "partial_preview"
        capture = await state.store.capture_user(state.owner)
        provider = MagicMock(return_value=response("Synthetic retained evidence summary"))
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=provider)),
                                 _evidence_provider_capture=capture)
        state.host._build_llm_client = lambda _config, selected_source: (
            client, selected_source, SimpleNamespace(model=MODEL, base_url=BASE_URL),
        )
        state.host._llm_audit_principals = Orchestrator._llm_audit_principals.__get__(state.host)
        state.host._accumulate_usage = Orchestrator._accumulate_usage.__get__(state.host)
        state.host._derive_chat_title = Orchestrator._derive_chat_title
        state.host._safe_llm_error_metadata = Orchestrator._safe_llm_error_metadata
        state.host.audit_recorder = None
        state.host.token_usage = {}
        state.host._ws_active_chat = {id(source.socket): source.chat}
        state.host._record_llm_call = AsyncMock()
        state.host._emit_llm_usage_report = AsyncMock()
        state.host._record_llm_unconfigured = AsyncMock()
        messages = [{"role": "tool", "content": json.dumps(captured.result)}] + [
            {"role": "tool", "content": f"synthetic-record-{index}: " + "x" * 1600} for index in range(9)
        ]
        for name in ("observation_packing", "safe_compaction"):
            monkeypatch.setitem(flags._flags, name, False)
            monkeypatch.setenv("FF_" + name.upper(), "false")
        result = await Orchestrator._generate_tool_summary(state.host, source.socket, messages,
            chat_id=source.chat, user_id=state.owner)
        if result is None:
            provider.assert_not_called()
        else:
            assert result[0]["type"] == "badge" and result[0]["label"] == "Generated summary"
            assert json.loads(provider.call_args.kwargs["messages"][1]["content"]) == messages
            totals = await state.ledger.totals(state.owner, source.chat)
            assert totals["model_calls"] == 1


@pytest.mark.parametrize("parameter", ["max_tokens", "max_completion_tokens"])
@pytest.mark.parametrize("requested,expected", [(None, 128), (12, 12), (256, 128)])
async def test_typed_model_authority_caps_actual_output_parameter(configured_model, human, bound, fixture,
                                                                parameter, requested, expected):
    state = configured_model
    state.entry["output_parameter"] = parameter
    state.policy.write_text(json.dumps([state.entry]))
    async with admitted_model(state, human, bound, fixture) as turn:
        request = model_request(**({"max_tokens": requested} if requested is not None else {}))
        guard = await prepare(state, turn, request)
        assert request[parameter] == expected
        assert set(request) == {"model", "messages", parameter}
        await asyncio.create_task(guard())
        assert not state.events and fixture[-1] == []


@pytest.mark.parametrize("value", [True, 0, -1, 1.5, "10", None])
async def test_invalid_output_bounds_refuse_before_usage_or_provider(configured_model, human, bound, fixture, value):
    state = configured_model
    async with admitted_model(state, human, bound, fixture) as turn:
        physical = AsyncMock()
        with pytest.raises(EvidenceDenied):
            await invoke(state, turn, model_request(max_tokens=value), physical)
        physical.assert_not_awaited()
        assert not state.events


@pytest.mark.parametrize("change", ["model", "base_url", "owner", "chat", "missing_capture", "foreign_capture"])
async def test_model_route_owner_and_provider_binding_must_match_exactly(configured_model, human, bound, fixture, change):
    state = configured_model
    async with admitted_model(state, human, bound, fixture) as turn:
        values = {}
        if change in {"model", "base_url", "owner", "chat"}:
            values[change] = "synthetic-other"
        elif change == "missing_capture":
            values["provider_capture"] = None
        else:
            state.store.set_sync("foreign-owner", provider=PROVIDER, base_url=BASE_URL, model=MODEL,
                                 api_key="synthetic-foreign-key")
            values["provider_capture"] = await state.store.capture_user("foreign-owner")
        with pytest.raises((EvidenceDenied, AssignmentError)):
            await prepare(state, turn, model_request(), **values)
        assert not state.events


async def test_absent_origin_lease_cannot_admit_physical_model(configured_model):
    state = configured_model
    capture = await state.store.capture_user(state.owner)
    with pytest.raises(AssignmentError):
        await state.context.prepare_model(websocket=object(), owner=state.owner, chat="unbound",
                                          model=MODEL, base_url=BASE_URL, request=model_request(), provider_capture=capture)
    assert not state.events


@pytest.mark.parametrize("part", ["messages", "tools", "output"])
async def test_full_serialized_model_request_fits_operator_budget(configured_model, human, bound, fixture, part):
    state = configured_model
    state.entry.update(context_tokens=1024, max_output_tokens=500)
    state.policy.write_text(json.dumps([state.entry]))
    async with admitted_model(state, human, bound, fixture) as turn:
        request = model_request()
        if part == "messages":
            request["messages"][0]["content"] = "x" * 1024
        elif part == "tools":
            request["tools"] = [{"type": "function", "function": {"name": "synthetic", "description": "x" * 1024}}]
        else:
            request["messages"][0]["content"] = "x" * 520
        with pytest.raises(EvidenceDenied):
            await prepare(state, turn, request)
        assert not state.events


@pytest.mark.parametrize("reason", ["secret", "phi", "size", "unavailable_analyzer", "invalid_analyzer"])
async def test_model_envelope_privacy_is_fail_closed(configured_model, human, bound, fixture, monkeypatch, reason):
    state = configured_model
    state.entry["context_tokens"] = 200_000
    state.policy.write_text(json.dumps([state.entry]))
    async with admitted_model(state, human, bound, fixture) as turn:
        request = model_request()
        if reason == "secret":
            request["messages"][0]["content"] = "Bearer synthetic-private-token"
        elif reason == "phi":
            request["messages"][0]["content"] = "Patient SSN 123-45-6789"
        elif reason == "size":
            request["messages"][0]["content"] = "x" * 65_537
        else:
            gate = PHIGate(build_if_missing=False) if reason == "unavailable_analyzer" else PHIGate(
                analyzer=SimpleNamespace(analyze=lambda **_kwargs: object()))
            monkeypatch.setattr(phi_gate, "get_phi_gate", lambda: gate)
        physical = AsyncMock()
        with pytest.raises((EvidenceError, ValueError)) as error:
            await invoke(state, turn, request, physical)
        physical.assert_not_awaited()
        assert not state.events and "synthetic-private-token" not in str(error.value)


@pytest.mark.parametrize("loss", ["provider_key", "consent", "budget", "request", "origin"])
async def test_prepared_guard_refuses_current_authority_loss(configured_model, human, bound, fixture, loss):
    state = configured_model
    async with admitted_model(state, human, bound, fixture) as turn:
        request = model_request()
        guard = await prepare(state, turn, request)
        if loss == "provider_key":
            await replace_key(state)
        elif loss == "consent":
            await withdraw_consent(state)
        elif loss == "budget":
            state.entry["max_output_tokens"] = 64
            state.policy.write_text(json.dumps([state.entry]))
        elif loss == "request":
            request["messages"][0]["content"] = "Changed request"
        else:
            turn.lease.close()
        with pytest.raises((EvidenceDenied, AssignmentError)):
            await asyncio.create_task(guard())


@pytest.mark.parametrize("loss", ["provider_key", "consent", "budget", "session"])
async def test_changes_during_actual_usage_record_wait_prevent_dispatch(configured_model, human, bound, fixture, loss):
    state = configured_model
    changed = []

    async def record(event):
        state.events.append(event)
        if event.outputs_meta["usage_record"]["phase"] == "pending" and not changed:
            changed.append(True)
            if loss == "provider_key":
                await replace_key(state)
            elif loss == "consent":
                await withdraw_consent(state)
            elif loss == "budget":
                state.entry["max_output_tokens"] = 64
                state.policy.write_text(json.dumps([state.entry]))
            else:
                await asyncio.to_thread(fixture[0].delete, fixture[2])

    state.context.usage = state.ledger = ContextUsage(record)
    async with admitted_model(state, human, bound, fixture) as turn:
        physical = AsyncMock()
        with pytest.raises((EvidenceDenied, AssignmentError)):
            await invoke(state, turn, model_request(), physical)
        physical.assert_not_awaited()
        await state.ledger.drain()
        totals = await state.ledger.totals(state.owner, turn.chat)
        assert changed == [True] and totals["model_calls"] == totals["failed"] == 1
        assert totals["usage"]["total_tokens"] == {"known": 0, "unknown": 1}


async def test_cancelled_origin_during_pending_usage_record_never_dispatches(configured_model, human, bound, fixture):
    state = configured_model
    entered, release = asyncio.Event(), asyncio.Event()
    chats = []

    async def record(event):
        state.events.append(event)
        if event.outputs_meta["usage_record"]["phase"] == "pending":
            entered.set()
            await release.wait()

    state.context.usage = state.ledger = ContextUsage(record)
    physical = AsyncMock()

    async def worker():
        async with admitted_model(state, human, bound, fixture) as turn:
            chats.append(turn.chat)
            return await invoke(state, turn, model_request(), physical)

    task = asyncio.create_task(worker())
    try:
        async with asyncio.timeout(10):
            await entered.wait()
            task.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await state.ledger.drain()
    physical.assert_not_awaited()
    totals = await state.ledger.totals(state.owner, chats[0])
    assert totals["cancelled"] == 1 and totals["pending"] == 0
    assert totals["usage"]["total_tokens"] == {"known": 0, "unknown": 1}


@pytest.mark.parametrize("loss", ["provider_key", "consent"])
async def test_changes_during_real_lease_verification_prevent_dispatch(configured_model, human, bound, fixture, monkeypatch, loss):
    state = configured_model
    async with admitted_model(state, human, bound, fixture) as turn:
        original = type(turn.lease).verify

        async def changed(lease, *args, **kwargs):
            proof = await original(lease, *args, **kwargs)
            if loss == "provider_key":
                await replace_key(state)
            else:
                await withdraw_consent(state)
            return proof

        monkeypatch.setattr(type(turn.lease), "verify", changed)
        physical = AsyncMock()
        with pytest.raises(EvidenceDenied):
            await invoke(state, turn, model_request(), physical)
        physical.assert_not_awaited()


async def test_same_route_key_replacement_cannot_reuse_previously_constructed_client(configured_model, human, bound, fixture):
    state = configured_model
    async with admitted_model(state, human, bound, fixture) as turn:
        await replace_key(state)
        physical = AsyncMock()
        with pytest.raises(EvidenceDenied):
            await invoke(state, turn, model_request(), physical)
        physical.assert_not_awaited()
        assert not state.events


async def test_enabled_resolver_uses_uncached_owner_capture_without_system_fallback(configured_model, monkeypatch):
    state = configured_model
    socket = object()
    state.host.ui_sessions[socket] = {"sub": state.owner}
    state.store.set_system_sync(provider=PROVIDER, base_url=BASE_URL, model="system-model",
                                api_key="synthetic-system-key", updated_by="synthetic-admin")
    cached = await state.store.get(state.owner)
    with state.store._repository.plane_runtime.transaction() as transaction:
        state.store._repository.repository.upsert_user(
            transaction, owner_id=state.owner, provider=PROVIDER, base_url=BASE_URL, model=MODEL,
            api_key_ciphertext=state.store._encrypt_key("synthetic-new-key"),
        )
    assert (await state.store.get(state.owner)) is cached
    monkeypatch.setattr(state.store, "get", AsyncMock(side_effect=AssertionError("cached user lookup")))
    monkeypatch.setattr(state.store, "get_system", AsyncMock(side_effect=AssertionError("system fallback")))
    client, source, resolved = await state.host._resolve_llm_client_for(socket)
    try:
        assert source is CredentialSource.USER and resolved.model == MODEL and resolved.base_url == BASE_URL
        assert client.api_key == "synthetic-new-key" and client.max_retries == 0
        current = await state.store.capture_user(state.owner)
        assert client._evidence_provider_capture.matches(current._record)
        assert "synthetic-new-key" not in repr(current)
    finally:
        client.close()
    state.store.get.assert_not_awaited()
    state.store.get_system.assert_not_awaited()


@pytest.mark.parametrize("posture", ["missing_user", "capture_unavailable"])
async def test_enabled_resolver_refuses_unavailable_user_without_borrowing_system(configured_model, monkeypatch, posture):
    state = configured_model
    socket = object()
    state.host.ui_sessions[socket] = {"sub": "unconfigured-owner" if posture == "missing_user" else state.owner}
    system = AsyncMock(side_effect=AssertionError("system fallback"))
    monkeypatch.setattr(state.store, "get_system", system)
    if posture == "capture_unavailable":
        monkeypatch.setattr(state.store, "capture_user", AsyncMock(side_effect=ValueError("user_config_capture_unavailable")))
    with pytest.raises((LLMUnavailable, ValueError)):
        client, _, _ = await state.host._resolve_llm_client_for(socket)
        client.close()
    system.assert_not_awaited()


async def test_enabled_resolver_preserves_explicit_owner_keyless_configuration(configured_model):
    from llm_config.client_factory import KEYLESS_API_KEY_SENTINEL

    state = configured_model
    socket = object()
    state.host.ui_sessions[socket] = {"sub": state.owner}
    state.store.set_sync(state.owner, provider=PROVIDER, base_url=BASE_URL, model=MODEL, api_key="")
    client, source, resolved = await state.host._resolve_llm_client_for(socket)
    try:
        assert source is CredentialSource.USER and resolved.model == MODEL
        assert client.api_key == KEYLESS_API_KEY_SENTINEL
        assert client._evidence_provider_capture.owner_id == state.owner
    finally:
        client.close()


async def test_unqualified_budget_file_cannot_admit_a_model(configured_model, human, bound, fixture):
    state = configured_model
    state.policy.write_text("[]")
    async with admitted_model(state, human, bound, fixture) as turn:
        physical = AsyncMock()
        with pytest.raises(ContextBudgetUnavailable):
            await invoke(state, turn, model_request(), physical)
        physical.assert_not_awaited()
        assert not state.events
