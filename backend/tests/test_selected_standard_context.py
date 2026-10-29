"""Actual standard lifecycle preserves signed turn and operation read proof through inherited tasks.
Real Plane audit records settle one synthetic physical response, including late cancellation; no deployment fact is activated.
"""

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from openai.types.chat import ChatCompletion

from astralplane.repositories.harness_bindings import HarnessProfileIdentity
from astralplane.repositories.harness_capture import HarnessCapture
from llm_config import invocation, research_profile
from llm_config.audit_events import record_llm_call
from orchestrator import context_authority
from orchestrator.context_usage import ContextUsage
from orchestrator.harness import execution_binding
from orchestrator.harness.execution_binding import ModelHarnessBinding
from orchestrator.harness.standard_llm_adapter import StandardLLMHarness
from orchestrator.orchestrator import Orchestrator
from tests.test_call_llm_wave0 import _bare_orch, _FakeCompletions
from tests.test_context_authority import acquire, admitted_turn
from tests.test_evidence_model import (
    configured_model as configured_model, model_controls as model_controls,
    human as human, bound as bound, fixture as fixture, runtime as runtime,
    service as service, signing_key as signing_key, MODEL, BASE_URL,
)

pytestmark = pytest.mark.asyncio


async def configure(state, turn, monkeypatch, *, release=None):
    host, socket, chat, _ = turn
    host.ui_sessions[socket] = {"sub": state.owner}
    lease = await acquire(turn)
    capture = await state.store.capture_user(state.owner)
    bare = _bare_orch(_FakeCompletions())
    for name, value in bare.__dict__.items():
        if not hasattr(host, name):
            setattr(host, name, value)
    host.MAX_RETRIES = Orchestrator.MAX_RETRIES
    host._valid_reasoning_effort = Orchestrator._valid_reasoning_effort
    host._safe_llm_error_metadata = Orchestrator._safe_llm_error_metadata
    host._llm_audit_principals = Orchestrator._llm_audit_principals.__get__(host)
    writes, bodies, started, proofs = [], [], asyncio.Event(), []
    async def durable(event):
        value = await asyncio.to_thread(host.audit_repo.insert, event)
        writes.append(value)
        return value
    ledger = ContextUsage(durable)
    state.context.usage = ledger
    host.audit_recorder = SimpleNamespace(record=durable)
    host._record_llm_call = record_llm_call
    expected = None
    async def current():
        nonlocal expected
        proof = await lease.verify(orchestrator=host, websocket=socket, chat_id=chat)
        if expected is None:
            expected = proof.stable_identity
        assert proof.stable_identity == expected and len(proof.operation_fences) == 1
        proofs.append(proof.stable_identity)
    async def physical(body):
        bodies.append(deepcopy(body))
        started.set()
        if release is not None:
            await release.wait()
        return ChatCompletion(id="synthetic", object="chat.completion", created=1, model=MODEL,
            choices=[{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "bounded"}}],
            usage={"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12,
                   "prompt_tokens_details": {"cached_tokens": 0}})
    profile = research_profile.LOCAL_PROFILE.bind(model=MODEL, base_url=BASE_URL)
    checking = ModelHarnessBinding(None, None, None, None, state.owner, None,
                                   SimpleNamespace(profile=profile), None, None, None)
    metadata = HarnessCapture(state.owner, 1, HarnessProfileIdentity("standard_local", "1.0.0", "a" * 64),
        "b" * 64, profile.name, "synthetic", "c" * 64)
    selected = SimpleNamespace(owner_id=state.owner, adapter_identity=StandardLLMHarness().identity,
        capture=metadata, assert_current=current, request=checking.request, response=checking.response,
        invoke=physical, client=lambda **_: (SimpleNamespace(_evidence_provider_capture=capture),
            host._CredentialSource.USER, SimpleNamespace(model=MODEL, base_url=BASE_URL)))
    monkeypatch.setattr(execution_binding, "prepare_model_binding", AsyncMock(return_value=selected))
    return SimpleNamespace(host=host, socket=socket, chat=chat, lease=lease, ledger=ledger,
                           writes=writes, bodies=bodies, started=started, proofs=proofs)


async def test_actual_selected_call_preserves_original_proof_and_durable_single_usage(
        configured_model, human, bound, fixture, monkeypatch):
    async with admitted_turn(human, bound, fixture, operation=True) as turn:
        state = await configure(configured_model, turn, monkeypatch)
        outer = invocation.InvocationState()
        try:
            with context_authority.use_context_authority(state.lease):
                async def call():
                    return await Orchestrator._call_llm(state.host, state.socket,
                        [{"role": "user", "content": "bounded"}], stream_chat_id=state.chat)
                result = await invocation.invoke_accounted(call, outer)
            assert result.outcome == "succeeded", (result.error_class, len(state.bodies), len(state.proofs))
            assert result.message.content == "bounded" and result.attempts == 1 and result.total_tokens == 12
            assert outer.pending == 0 and len(state.bodies) == 1 and len(state.proofs) >= 3
            assert state.bodies[0]["max_tokens"] == 128 and "max_completion_tokens" not in state.bodies[0]
            totals = await state.ledger.totals(configured_model.owner, state.chat)
            assert totals["model_calls"] == totals["attempts"] == 1 and totals["usage"]["total_tokens"]["known"] == 12
            rows, _ = state.host.audit_repo.list_for_user(configured_model.owner)
            receipts = [row for row in rows if row.action_type == "context.usage"]
            assert receipts and len({row.outputs_meta["usage_record"]["attempt_id"] for row in receipts}) == 1
            assert state.host.audit_repo.verify_chain(configured_model.owner) is None
        finally:
            await state.ledger.drain()
            state.host.ui_sessions.pop(state.socket, None)


async def test_original_task_cancel_keeps_late_durable_usage_on_original_states_once(
        configured_model, human, bound, fixture, monkeypatch):
    release, ready, states = asyncio.Event(), asyncio.Event(), []
    outer = invocation.InvocationState()
    async def worker():
        async with admitted_turn(human, bound, fixture, operation=True) as turn:
            state = await configure(configured_model, turn, monkeypatch, release=release)
            states.append(state)
            ready.set()
            with context_authority.use_context_authority(state.lease):
                async def call():
                    return await Orchestrator._call_llm(state.host, state.socket,
                        [{"role": "user", "content": "bounded"}], stream_chat_id=state.chat)
                return await invocation.invoke_accounted(call, outer)
    task = asyncio.create_task(worker())
    try:
        await asyncio.wait_for(ready.wait(), 10)
        state = states[0]
        await asyncio.wait_for(state.started.wait(), 10)
        assert outer.attempts == 1 and outer.pending == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert outer.attempts == 1 and outer.pending == 1
        release.set()
        await state.ledger.drain()
        assert outer.pending == 0 and outer.total_tokens == 12 and len(state.bodies) == 1
        totals = await state.ledger.totals(configured_model.owner, state.chat)
        assert totals["model_calls"] == 1 and totals["cancelled"] == 1 and totals["charged_cancelled"] == 1
        rows, _ = state.host.audit_repo.list_for_user(configured_model.owner)
        assert any(row.action_type == "context.usage" and row.outputs_meta["usage_record"]["settled"] for row in rows)
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if states:
            await states[0].ledger.drain()
