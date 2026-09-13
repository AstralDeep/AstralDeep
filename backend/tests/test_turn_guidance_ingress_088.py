"""Actual socket admission and managed async handoff reach the current catalog.

These tests deliberately stop immediately after ordinary slash expansion after guidance
expansion. The stop is an explicit failed operation, never evidence of a model
completion, publication, or provider call.
"""
import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from orchestrator import slash_commands, user_skills
from orchestrator.async_tasks import BackgroundTaskManager
from orchestrator.user_skill_catalog import SkillCatalogError, UserSkillFacade
from shared.feature_flags import flags
from tests.helpers.session_plane_runtime import get_session_record, replace_session_record
from tests.test_human_socket_ingress_088 import (
    metadata as metadata, ingress as ingress, surface as surface, command as command,
    context as context, fixture as fixture, runtime as runtime, service as service,
    signing_key as signing_key, registered, send, terminal,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def guidance_turn(metadata, runtime, fixture, tmp_path, monkeypatch):
    state = metadata[0]
    orch = state.orch
    monkeypatch.setitem(flags._flags, 'user_skills', True)
    monkeypatch.setitem(flags._flags, 'slash_commands', True)
    monkeypatch.setitem(flags._flags, 'bg_continuity', False)
    orch._user_skill_store = UserSkillFacade(orch, str(tmp_path))
    user_skills.UserSkillStore(str(tmp_path)).save(fixture[1], name='Weekly shape',
        instructions='Use the exact original weekly outline.', applies_to='always', command='weekly')
    orch.cancelled_sessions = {}
    orch._chat_locks = {}
    orch._workspace_locks = {}
    orch._chain_budgets = {}
    orch.async_task_manager = BackgroundTaskManager(orch.work_admission)
    orch.async_task_manager.bind(plane_runtime=runtime, plane_repositories=runtime.repositories)
    observed = []
    chat = str(uuid4())
    # Conversation publication itself has independent full-path gates. These
    # adapters create no commit and no history success in this guidance probe.
    async def no_stage(*_args, **_kwargs):
        return None, None, None
    async def no_detached(*_args, **_kwargs):
        return None, None
    monkeypatch.setattr(orch, '_begin_conversation_publication', no_stage)
    monkeypatch.setattr(orch, '_begin_detached_conversation_publication', no_detached)
    monkeypatch.setattr(orch.history, 'get_chat', lambda *_a, **_k: {'id': chat})
    monkeypatch.setattr(orch.history, 'get_conversation_record', lambda *_a, **_k: SimpleNamespace(render_revision=0), raising=False)
    monkeypatch.setattr(orch, '_bind_conversation_scope', lambda *_a, **_k: None)
    expand = slash_commands.expand_message
    def stop(text, commands):
        observed.append(expand(text, commands))
        raise SkillCatalogError('qualification_stop_before_model', 503)
    monkeypatch.setattr(slash_commands, 'expand_message', stop)
    try:
        yield state, chat, observed
    finally:
        await orch.async_task_manager.drain(timeout_seconds=5)


@pytest.mark.parametrize('loss', ['none', 'preregistration', 'issuance', 'owner'])
async def test_actual_foreground_admission_expands_only_original_current_guidance(
    guidance_turn, runtime, fixture, monkeypatch, loss,
):
    state, chat, observed = guidance_turn
    if loss != 'preregistration':
        await registered(state)
    original = state.orch._call_work_admission
    async def revoke_after_capture(callback, *args, **kwargs):
        if loss == 'issuance' and getattr(callback, '__name__', '') == '_submit_connection_batch':
            await asyncio.to_thread(replace_session_record, runtime, get_session_record(runtime, fixture[2]))
        if loss == 'owner' and getattr(callback, '__name__', '') == '_submit_connection_batch':
            state.orch.ui_sessions[state.socket]['sub'] = str(uuid4())
        return await original(callback, *args, **kwargs)
    monkeypatch.setattr(state.orch, '_call_work_admission', revoke_after_capture)
    send(state, 'chat_message', message='/weekly original request', chat_id=chat)
    if loss == 'preregistration':
        await state.barrier()
        await registered(state)
    result = await terminal(state)
    assert result['state'] == 'failed'
    if loss == 'none':
        assert len(observed) == 1
        assert 'Use the exact original weekly outline.' in observed[0] and 'original request' in observed[0]
    else:
        assert observed == []


@pytest.mark.parametrize('loss', ['none', 'session_before_lookup', 'cancel_before_lookup'])
async def test_actual_managed_background_uses_original_handoff_and_retires_it(
    guidance_turn, runtime, fixture, monkeypatch, loss,
):
    state, chat, observed = guidance_turn
    await registered(state)
    manager = state.orch.async_task_manager
    entered, release = asyncio.Event(), asyncio.Event()
    original = state.orch.handle_chat_message
    async def held(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)
    monkeypatch.setattr(state.orch, 'handle_chat_message', held)
    send(state, 'chat_message', message='/weekly background request', chat_id=chat, async_mode=True)
    try:
        await asyncio.wait_for(entered.wait(), 5)
        async with asyncio.timeout(5):
            while not manager._tasks:
                await asyncio.sleep(.01)
        task = next(iter(manager._tasks.values()))
        origin = task._guidance_origin
        assert not origin.closed
        if loss == 'session_before_lookup':
            await asyncio.to_thread(replace_session_record, runtime, get_session_record(runtime, fixture[2]))
        elif loss == 'cancel_before_lookup':
            task.asyncio_task.cancel()
        release.set()
        await asyncio.wait_for(asyncio.shield(task.asyncio_task), 5)
        assert task._canonical_status().value == ('cancelled' if loss == 'cancel_before_lookup' else 'failed')
        assert origin.closed and getattr(task, '_guidance_origin', None) is None
        assert len(observed) == (1 if loss == 'none' else 0)
        assert all('original weekly' not in json.dumps(frame) for frame in task.outputs)
    finally:
        release.set()
