"""Checks compaction's host snapshots against signed turn authority and public Plane state.
Typed task, publication, provider and source changes invalidate proposals without altering durable conversations.
"""

import asyncio
from datetime import timedelta
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from cryptography.fernet import Fernet
import pytest

from llm_config.data_sharing import DataSharingStore
from llm_config.user_store import UserLLMConfigStore
from orchestrator.async_tasks import BackgroundTask
from orchestrator.conversation_publication import ConversationPublicationStage, activate_conversation_publication, reset_conversation_publication
from orchestrator.evidence_archive import EvidenceDenied
from orchestrator.hitl_confirmation import PendingCall
from orchestrator.task_state import Task
from tests.test_evidence_service import (
    AGENT, TOOL, bound as bound, evidence_turn, fixture as fixture, human as human,
    pack, runtime as runtime, service as service, signing_key as signing_key,
)

pytestmark = pytest.mark.asyncio


def configure(state, runtime, monkeypatch, tmp_path):
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", Fernet.generate_key().decode())
    store = UserLLMConfigStore(plane_runtime=runtime, plane_repositories=runtime.repositories)
    store.set_sync(state.owner, provider="custom", base_url="https://model.invalid/v1",
                   model="bounded-model", api_key="synthetic-opaque-provider-key")
    consent = DataSharingStore(plane_runtime=runtime, plane_repositories=runtime.repositories)
    consent.acknowledge_sync(state.owner)
    state.host._llm_store = store
    state.host._data_sharing_store = consent
    policy = tmp_path / "context-budget.json"
    policy.write_text(json.dumps([dict(owner_id=state.owner, provider="custom", base_url="https://model.invalid/v1",
        model="bounded-model", context_tokens=8192, max_output_tokens=256, output_parameter="max_tokens")]))
    monkeypatch.setenv("ASTRAL_CONTEXT_BUDGET_POLICY", str(policy))
    state.host.async_task_manager = SimpleNamespace(list_for_user=AsyncMock(return_value=[]))
    state.host.task_manager = SimpleNamespace(get_chat_tasks=lambda _: [], refresh_task=AsyncMock())
    return store, consent


async def snapshot(state, capture):
    return await state.evidence._protected(state.socket, state.owner, state.chat, capture)


async def test_real_host_snapshot_is_stable_and_bound_to_operation_revision(human, bound, fixture, runtime, tmp_path, monkeypatch):
    async with evidence_turn(human, bound, fixture, tmp_path, monkeypatch) as state:
        store, _ = configure(state, runtime, monkeypatch, tmp_path)
        capture = await store.capture_user(state.owner)
        first = await snapshot(state, capture)
        assert first == await snapshot(state, capture) and first["authorized"] is True
        assert len(first["authority"]) == 64
        assert state.host.async_task_manager.list_for_user.await_args.kwargs == {"limit": 257}
        operation = state.binding.operations[0]
        await asyncio.to_thread(operation.coordinator.update_phase, operation.fence, "context_observed")
        assert await snapshot(state, capture) != first


@pytest.mark.parametrize("change", ["permission", "security", "manifest", "pending", "job", "retention"])
async def test_each_host_boundary_changes_snapshot(human, bound, fixture, runtime, tmp_path, monkeypatch, change):
    async with evidence_turn(human, bound, fixture, tmp_path, monkeypatch) as state:
        store, _ = configure(state, runtime, monkeypatch, tmp_path)
        capture = await store.capture_user(state.owner)
        await pack(state)
        first = await snapshot(state, capture)
        if change == "permission":
            state.host.tool_permissions.set_tool_overrides(state.owner, AGENT, {TOOL: False})
        elif change == "security":
            state.host.security_flags[AGENT] = {TOOL: {"blocked": True}}
        elif change == "manifest":
            state.host.agent_cards[AGENT].description = "Different source contract"
        elif change == "pending":
            state.host._hitl_pending_calls["approval"] = PendingCall("approval", state.owner, state.chat, AGENT, TOOL,
                                                                    "{}", ("sensitive",), 100000.0)
        elif change == "job":
            state.host._job_context["job"] = {"user_id": state.owner, "chat_id": state.chat, "goal": "Revised constraints"}
        else:
            grants = json.loads(state.policy.read_text())
            grants[0]["expires_at"] = (state.clock.now + timedelta(minutes=10)).isoformat()
            state.policy.write_text(json.dumps(grants))
        if change == "retention":
            with pytest.raises(EvidenceDenied):
                await snapshot(state, capture)
        else:
            assert await snapshot(state, capture) != first


async def test_actual_background_and_foreground_task_constraints_are_projected(human, bound, fixture, runtime, tmp_path, monkeypatch):
    async with evidence_turn(human, bound, fixture, tmp_path, monkeypatch) as state:
        store, _ = configure(state, runtime, monkeypatch, tmp_path)
        capture = await store.capture_user(state.owner)
        operation = state.binding.operations[0]
        background = BackgroundTask(str(operation.record.operation_id), state.chat, state.owner, title="Current background goal")
        background._operation = operation.record
        background._execution_fence = operation.fence
        foreground = Task(str(operation.record.operation_id), state.chat, state.owner, message="Current foreground goal")
        foreground._operation = operation.record
        foreground._execution_fence = operation.fence
        state.host.async_task_manager.list_for_user.return_value = [background]
        state.host.task_manager.get_chat_tasks = lambda _: [foreground]
        state.host.task_manager.refresh_task.return_value = foreground
        first = await snapshot(state, capture)
        assert first == await snapshot(state, capture)
        foreground.current_tool = TOOL
        assert await snapshot(state, capture) != first
        first = await snapshot(state, capture)
        background.errors.append("Actual failed task")
        assert await snapshot(state, capture) != first


@pytest.mark.parametrize("kind", ["background_unbound", "background_capacity", "foreground_unbound", "foreground_missing", "foreground_capacity"])
async def test_unreadable_task_scope_refuses_snapshot(human, bound, fixture, runtime, tmp_path, monkeypatch, kind):
    async with evidence_turn(human, bound, fixture, tmp_path, monkeypatch) as state:
        store, _ = configure(state, runtime, monkeypatch, tmp_path)
        capture = await store.capture_user(state.owner)
        background = BackgroundTask("task", state.chat, state.owner)
        foreground = Task("task", state.chat, state.owner)
        if kind == "background_unbound":
            state.host.async_task_manager.list_for_user.return_value = [background]
        elif kind == "background_capacity":
            state.host.async_task_manager.list_for_user.return_value = [background] * 257
        else:
            state.host.task_manager.get_chat_tasks = lambda _: [foreground] * (257 if kind == "foreground_capacity" else 1)
            state.host.task_manager.refresh_task.return_value = None if kind == "foreground_missing" else foreground
        with pytest.raises(EvidenceDenied):
            await snapshot(state, capture)


@pytest.mark.parametrize("change", ["owner", "provider", "consent"])
async def test_current_owner_provider_and_acknowledgment_are_required(human, bound, fixture, runtime, tmp_path, monkeypatch, change):
    async with evidence_turn(human, bound, fixture, tmp_path, monkeypatch) as state:
        store, consent = configure(state, runtime, monkeypatch, tmp_path)
        capture = await store.capture_user(state.owner)
        if change == "owner":
            state.owner = "foreign"
        elif change == "provider":
            store.set_sync(state.owner, provider="custom", base_url="https://model.invalid/v1",
                           model="bounded-model", api_key="synthetic-changed-provider-key")
        else:
            consent.state = AsyncMock(return_value=SimpleNamespace(acknowledged=False))
        with pytest.raises(EvidenceDenied):
            await snapshot(state, capture)


async def test_current_publication_summary_layout_and_fence_remain_protected(human, bound, fixture, runtime, tmp_path, monkeypatch):
    from uuid import uuid4
    async with evidence_turn(human, bound, fixture, tmp_path, monkeypatch) as state:
        store, _ = configure(state, runtime, monkeypatch, tmp_path)
        capture = await store.capture_user(state.owner)
        operation = state.binding.operations[0]
        stage = ConversationPublicationStage(state.host.history, str(uuid4()), state.chat, state.owner,
            0, 1, operation_fence=operation.fence)
        token = activate_conversation_publication(stage)
        try:
            first = await snapshot(state, capture)
            stage.set_completion_summary(text="Current unresolved task", source="tool_summary")
            assert await snapshot(state, capture) != first
            first = await snapshot(state, capture)
            stage.layouts.append({"id": "view", "components": ["component"]})
            assert await snapshot(state, capture) != first
            first = await snapshot(state, capture)
            stage.seal(committed=False)
            assert await snapshot(state, capture) != first
        finally:
            reset_conversation_publication(token)


async def test_foreign_background_scope_is_excluded_and_missing_capture_denied(human, bound, fixture, runtime, tmp_path, monkeypatch):
    async with evidence_turn(human, bound, fixture, tmp_path, monkeypatch) as state:
        store, _ = configure(state, runtime, monkeypatch, tmp_path)
        capture = await store.capture_user(state.owner)
        first = await snapshot(state, capture)
        state.host.async_task_manager.list_for_user.return_value = [BackgroundTask("other", "other-conversation", state.owner)]
        assert await snapshot(state, capture) == first
        store.capture_user = AsyncMock(return_value=None)
        with pytest.raises(EvidenceDenied):
            await snapshot(state, capture)


async def test_budget_refuses_missing_or_foreign_user_capture(human, bound, fixture, runtime, tmp_path, monkeypatch):
    async with evidence_turn(human, bound, fixture, tmp_path, monkeypatch) as state:
        store, _ = configure(state, runtime, monkeypatch, tmp_path)
        store.capture_user = AsyncMock(return_value=None)
        with pytest.raises(EvidenceDenied):
            await state.evidence.budget(state.owner)
        with pytest.raises(EvidenceDenied):
            await state.evidence.budget(state.owner, SimpleNamespace(owner_id="foreign"))
