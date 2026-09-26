"""Checks native bearer voice guidance through real PostgreSQL admission and the chat wrapper.
The model boundary stops after verified catalog expansion; cookie and preaccepted cases provide comparisons.
"""

import asyncio
import logging
import time

import pytest

from orchestrator import orchestrator as hub
from orchestrator import turn_guidance_authority
from orchestrator.history import ConversationCommitRepository
from orchestrator.user_skill_catalog import SkillCatalogError
from orchestrator.work_admission import OperationOwner, OwnerScope
from persistent_agents.models import AssignmentError
from orchestrator.turn_guidance_authority import TurnGuidanceReader, use_turn_guidance
from tests.test_turn_guidance_ingress_088 import (
    guidance_turn as guidance_turn, metadata as metadata, ingress as ingress,
    surface as surface, command as command, context as context, fixture as fixture,
    runtime as runtime, service as service, signing_key as signing_key,
)
from tests.test_turn_guidance_voice_088 import accepted_voice

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("outcome", ["release", "revoke", "persistent", "cancel"])
async def test_native_voice_rechecks_authority_after_concurrent_voice_update(
    guidance_turn, runtime, fixture, monkeypatch, outcome,
):
    state = guidance_turn[0]
    state.socket.scope["headers"] = [
        (key, value) for key, value in state.socket.scope["headers"] if key not in {b"cookie", b"origin"}]
    state, binding, _ = await accepted_voice(guidance_turn, runtime, fixture, "llm_factory", monkeypatch)
    entered = asyncio.Event()
    read = []
    original = TurnGuidanceReader._current
    loop = asyncio.get_running_loop()

    def observed(self, tx):
        loop.call_soon_threadsafe(entered.set)
        return original(self, tx)

    monkeypatch.setattr(TurnGuidanceReader, "_current", observed)
    started = time.monotonic()
    async def execute():
        from dataclasses import replace
        current = replace(binding, task=asyncio.current_task())
        with use_turn_guidance(current, expected_orchestrator=state.orch):
            reader = TurnGuidanceReader(current, expected_orchestrator=state.orch)
            try:
                return await reader.transaction(lambda tx, repositories: read.append("catalog"),
                                                expected_orchestrator=state.orch)
            finally:
                reader.close()

    try:
        with runtime.transaction() as writer:
            writer.fetch_one("SELECT session_id FROM voice_session WHERE session_id=%s FOR UPDATE",
                             (binding.voice.turn.session_id,))
            task = asyncio.create_task(execute())
            await asyncio.wait_for(entered.wait(), 2)
            await asyncio.sleep(.025)
            assert not task.done()
            if outcome == "revoke":
                writer.execute("UPDATE voice_session SET generation=generation+1 WHERE session_id=%s",
                               (binding.voice.turn.session_id,))
            elif outcome == "persistent":
                with pytest.raises(AssignmentError, match="guidance_read_unavailable"):
                    await asyncio.wait_for(task, 2)
            elif outcome == "cancel":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
        if outcome == "release":
            await asyncio.wait_for(task, 2)
        elif outcome == "revoke":
            with pytest.raises(AssignmentError, match="guidance_read_unavailable"):
                await asyncio.wait_for(task, 2)
        assert read == (["catalog"] if outcome == "release" else [])
        assert time.monotonic() - started < 2
    finally:
        binding.origin.close()


@pytest.mark.parametrize("credential", ["cookie", "native_bearer"])
@pytest.mark.parametrize("preaccepted", [False, True])
async def test_voice_catalog_uses_current_authority_before_model(
    guidance_turn, runtime, fixture, monkeypatch, credential, preaccepted,
):
    await dispatch(guidance_turn, runtime, fixture, monkeypatch,
                   credential=credential, preaccepted=preaccepted)


async def test_native_voice_denial_logs_only_the_bounded_authority_code(
    guidance_turn, runtime, fixture, monkeypatch, caplog,
):
    with caplog.at_level(logging.WARNING, logger=hub.__name__):
        await dispatch(guidance_turn, runtime, fixture, monkeypatch,
                       credential="native_bearer", preaccepted=True, revoke=True)
    messages = [row.getMessage() for row in caplog.records]
    assert any("guidance_read_unavailable" in message and "503" in message for message in messages)
    assert any("guidance read refused check=" in message for message in messages)
    assert all("original voice" not in message and "_raw_token" not in message for message in messages)


@pytest.mark.parametrize("dependency", [False, True])
async def test_guidance_refusal_diagnostics_never_log_exception_text(caplog, dependency):
    with caplog.at_level(logging.WARNING, logger=turn_guidance_authority.__name__):
        with pytest.raises(AssignmentError, match="guidance_read_unavailable"):
            if dependency:
                try:
                    raise RuntimeError("synthetic-private-token-and-transcript")
                except RuntimeError as exc:
                    turn_guidance_authority._refuse(cause=exc)
            else:
                turn_guidance_authority._refuse()
    assert "synthetic-private-token-and-transcript" not in caplog.text
    assert "test_native_voice_guidance_authority.py:test_guidance_refusal_diagnostics_never_log_exception_text:" in caplog.text
    assert ("error=RuntimeError" if dependency else "error=none") in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


async def dispatch(guidance_turn, runtime, fixture, monkeypatch, *, credential, preaccepted, revoke=False):
    state = guidance_turn[0]
    if credential == "native_bearer":
        state.socket.scope["headers"] = [
            (key, value) for key, value in state.socket.scope["headers"]
            if key not in {b"cookie", b"origin"}]
    with monkeypatch.context() as setup:
        if not preaccepted:
            setup.setattr(ConversationCommitRepository, "accept_voice_turn", lambda *_a, **_k: None)
        state, binding, voice = await accepted_voice(guidance_turn, runtime, fixture, "llm_factory", monkeypatch)
    assert (binding.origin.credential is None) == (credential == "native_bearer")
    orch = state.orch
    operation = binding.operations[0]
    turn = binding.voice.turn
    operation_context = {
        "operation_kind": "voice_chat_message", "operation": operation.record,
        "owner": OperationOwner(OwnerScope.USER, fixture[1], None),
        "execution_fence": operation.fence, "guidance_origin": binding.origin,
    }
    token = hub._CONNECTION_OPERATION_CONTEXT.set(operation_context)

    async def available(*_a, **_k):
        return None

    monkeypatch.setattr(orch, "_resolve_llm_client_for", available)
    monkeypatch.setattr(orch, "_deliver_committed_conversation_snapshot", available)
    monkeypatch.setattr(orch, "_broadcast_voice_ack", available)
    monkeypatch.setattr(orch, "_broadcast_voice_turn_state", available)
    orch.voice_services.coordinator = type("NoMedia", (), {"emit_transcript_accepted": staticmethod(available)})()
    orch.voice_services.start_turn_announcements = available
    orch._voice_ack_tasks = set()
    orch.conversation_commits = ConversationCommitRepository(
        plane_runtime=runtime, plane_repositories=runtime.repositories, operation_coordinator=orch.work_admission)
    if revoke:
        actual = turn_guidance_authority.acquire_turn_guidance_reader

        async def stale(**kwargs):
            with runtime.transaction() as tx:
                tx.execute("UPDATE voice_session SET generation=generation+1 WHERE session_id=%s", (turn.session_id,))
            return await actual(**kwargs)

        monkeypatch.setattr(turn_guidance_authority, "acquire_turn_guidance_reader", stale)
    try:
        with pytest.raises(SkillCatalogError) as caught:
            await orch._serialized_chat(
                state.socket, "/weekly original voice", binding.chat_id, None,
                user_id=fixture[1], operation_context=operation_context, voice_dispatch=state.guidance_voice_dispatch)
        assert caught.value.code == "skill_lookup_unavailable"
        assert len(guidance_turn[2]) == (0 if revoke else 1)
        assert voice.get_turn(user_id=fixture[1], turn_id=turn.turn_id).accepted_at is not None
    finally:
        hub._CONNECTION_OPERATION_CONTEXT.reset(token)
        binding.origin.close()
        if orch._voice_ack_tasks:
            await asyncio.gather(*orch._voice_ack_tasks)
