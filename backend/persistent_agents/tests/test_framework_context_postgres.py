"""Fixed framework model calls reuse their immutable owner/Work context for real audit accounting.
Feature-on fixtures retain budget, data-sharing, source, reservation and recovery guards.
"""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from llm_config import research_profile as profile
from llm_config.data_sharing import DataSharingStore
from orchestrator.evidence_context import EvidenceContext
from orchestrator.orchestrator import Orchestrator
from persistent_agents.tests.test_framework_execution_postgres import (
    ANSWER,
    fixture as fixture,
    framework as framework,
    gate_orchestrator as gate_orchestrator,
    integrated as integrated,
    operation as operation,
    plane as plane,
    research as research,
    settle,
    signing_key as signing_key,
    stored,
    submit,
)
from shared.feature_flags import flags

runtime = plane
pytestmark = [pytest.mark.asyncio,
    pytest.mark.parametrize("operation", [{"tokens": 300_000}], indirect=True)]


@pytest.fixture
async def context(framework, monkeypatch, tmp_path):
    state = framework
    orch = state.op.executor.orch
    sharing = DataSharingStore(plane_runtime=state.op.runtime)
    await sharing.acknowledge(state.op.owner)
    path = tmp_path / "context-budget.json"
    path.write_text(json.dumps([{"owner_id": state.op.owner, "provider": "openai",
        "base_url": profile.BASE_URL, "model": profile.MODEL,
        "context_tokens": profile.CONTEXT_TOKENS, "max_output_tokens": profile.OUTPUT_TOKENS,
        "output_parameter": "max_completion_tokens"}]))
    monkeypatch.setenv("ASTRAL_CONTEXT_BUDGET_POLICY", str(path))
    monkeypatch.setitem(flags._flags, "observation_packing", True)
    monkeypatch.setitem(flags._flags, "safe_compaction", False)
    orch._data_sharing_store = sharing
    orch._context_model_call = Orchestrator._context_model_call.__get__(orch)
    state.context = orch._evidence_context = EvidenceContext(orch)
    state.sharing = sharing
    state.budget_path = path
    try:
        yield state
    finally:
        await state.context.usage.drain()


def conversation(state, identity, *, owner=None):
    with state.op.runtime.transaction() as tx:
        return state.op.runtime.repositories.history.conversations.get(tx,
            owner_id=owner or state.op.owner, conversation_id=identity)


def owned_conversation(state):
    identity = str(uuid4())
    with state.op.runtime.transaction() as tx:
        state.op.runtime.repositories.history.conversations.create(tx,
            owner_id=state.op.owner, conversation_id=identity, title="Synthetic owned context",
            agent_id=None, created_at=1)
    state.op.executor.orch.history.get_chat = lambda chat_id, *, user_id: conversation(
        state, chat_id, owner=user_id)
    return identity


@pytest.mark.parametrize("existing", [False, True])
async def test_flag_on_consumed_framework_reservation_accounts_and_recovers_real_work_context(context, existing):
    state = context
    identity = owned_conversation(state) if existing else None
    accepted = await submit(state, **({"conversation_id": identity} if existing else {}))
    context_id = identity or accepted["id"]
    record, actions = await settle(state, accepted["id"])
    assert record["operation"]["terminal_outcome"] == "completed"
    assert record["checkpoint"]["chat_result"]["text"] == ANSWER
    assert len(state.op.model_calls) == 1 and actions[0]["state"] == "succeeded"
    assert all("_raw_token" not in item for item in state.seen)
    await state.context.usage.drain()
    totals = await state.context.usage.totals(state.op.owner, context_id, refresh=True)
    assert totals["model_calls"] == totals["succeeded"] == 1
    assert totals["usage"]["total_tokens"] == {"known": 120, "unknown": 0}
    restored = EvidenceContext(state.op.executor.orch)
    recovered = await restored.usage.totals(state.op.owner, context_id)
    assert recovered["model_calls"] == 1
    assert recovered["usage"]["total_tokens"] == {"known": 120, "unknown": 0}
    assert (await restored.usage.totals(str(uuid4()), context_id))["model_calls"] == 0
    assert conversation(state, accepted["id"]) is None
    assert state.credentials.list(owner_id=state.op.owner)[0]["consumed_admissions"] == 1
    assert await asyncio.to_thread(state.op.audit._repo.verify_chain, state.op.owner) is None


@pytest.mark.parametrize("loss", ["missing_context", "consent", "budget", "work_binding"])
async def test_flag_on_context_loss_refuses_before_the_provider_send(context, monkeypatch, loss):
    from persistent_agents.dispatch_context import current_dispatch

    state = context
    seen = []
    orch = state.op.executor.orch
    original = orch._context_model_call

    async def call(socket, chat, *args, **kwargs):
        reservation = current_dispatch()
        assert reservation.consumed is True and reservation.owner_id == state.op.owner
        assert "_raw_token" not in orch.ui_sessions[socket]
        seen.append(True)
        if loss == "missing_context":
            reservation.conversation_id = None
            chat = None
        elif loss == "consent":
            with state.op.runtime.transaction() as tx:
                state.op.runtime.repositories.preferences.data_sharing.acknowledge(tx,
                    owner_id=state.op.owner, notice_version="synthetic-unaccepted-notice",
                    at=datetime.now(UTC))
        elif loss == "budget":
            state.budget_path.write_text("[]")
        return await original(socket, chat, *args, **kwargs)

    monkeypatch.setattr(orch, "_context_model_call", call)
    if loss == "work_binding":
        original_begin = state.context.usage.begin
        async def begin(*args, **kwargs):
            value = await original_begin(*args, **kwargs)
            current_dispatch().conversation_id = str(uuid4())
            return value
        monkeypatch.setattr(state.context.usage, "begin", begin)
    accepted = await submit(state)
    async with asyncio.timeout(20):
        while not seen or state.runner._active:
            await asyncio.sleep(0.05)
    record, actions, _ = await stored(state.op, accepted["id"])
    assert seen and state.op.model_calls == []
    assert "chat_result" not in record["checkpoint"]
    assert all((action.get("result") or {}).get("result_available") is not True for action in actions)


@pytest.mark.parametrize("enabled", [False, True])
async def test_real_retained_source_reference_cannot_grant_framework_source_authority(
    context, monkeypatch, tmp_path, enabled,
):
    from agents.evidence.evidence_agent import EvidenceAgent
    from orchestrator.evidence_archive import RetentionGrant

    state = context
    monkeypatch.setenv("AGENT_KEY_PATH", str(tmp_path / "evidence-agent.pem"))
    monkeypatch.setitem(flags._flags, "inprocess_agents", True)
    monkeypatch.setitem(flags._flags, "observation_packing", enabled)
    adapter = EvidenceAgent(state.op.executor.orch, port=0)
    state.op.executor.orch.local_agents[adapter.agent_id] = adapter
    state.op.executor.orch.agent_cards[adapter.agent_id] = adapter.card
    assert state.context._view_adapter(tool="recall_observation")[0] is adapter
    finished = asyncio.Event()
    original = state.runner.run_claim

    async def run(claim):
        try:
            return await original(claim)
        finally:
            if claim.assignment.operation.get("authority", {}).get("origin") == "framework":
                finished.set()

    monkeypatch.setattr(state.runner, "run_claim", run)
    identity = owned_conversation(state)
    grant = RetentionGrant(owner_id=state.op.owner, conversation_id=identity,
        audience_id="user:" + state.op.owner, source_agent="web-research-1", source_tool="fetch_page",
        expires_at=datetime.now(UTC) + timedelta(minutes=5))
    observation = state.context.archive.capture("Synthetic private source must remain unavailable.",
        grant=grant, operation_id=str(uuid4()), source_args={"url": "https://example.invalid"},
        outcome="success")
    accepted = await state.operations.submit(state.caller, idempotency_key=str(uuid4()),
        name="Source denial", instructions="Explain " + observation.reference,
        conversation_id=identity, limits={"model_calls": 1, "tool_calls": 1,
            "tokens": profile.RESERVED_TOKENS, "elapsed_ms": profile.RESERVED_MILLISECONDS,
            "max_retries": 0}, deadline_in_seconds=300)
    await asyncio.wait_for(finished.wait(), 20)
    record, actions, _ = await stored(state.op, accepted["id"])
    assert state.op.model_calls == [] and "chat_result" not in record["checkpoint"]
    assert all((action.get("result") or {}).get("result_available") is not True for action in actions)
    assert state.context.archive.observation_count == 1
