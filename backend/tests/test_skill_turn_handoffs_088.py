"""Tests that HTTP and subtask handoffs retain original guidance authority over real
Plane (orchestrator/turn_guidance_authority.py, subtasks.py, chain_authority.py,
api.py): a turn reader can read but never write or escalate scope.
"""

import asyncio
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from orchestrator import turn_guidance_authority as guidance
from persistent_agents.models import AssignmentError
from tests.test_user_skill_facade_088 import catalog as catalog, command
from tests.test_human_request_authority_088 import human as human, selected
from tests.test_work_control_authority_088 import (
    bound as bound, fixture as fixture, runtime as runtime, service as service,
    signing_key as signing_key, incoming,
)
from tests.helpers.session_plane_runtime import get_session_record, replace_session_record

pytestmark = pytest.mark.asyncio


async def origin_for(human, bound, fixture):
    return await guidance.capture_turn_guidance_from_human(
        await selected(human, bound, fixture), expected_orchestrator=human[2])


async def read(catalog, orch, websocket, chat_id):
    reader = await guidance.acquire_turn_guidance_reader(
        expected_orchestrator=orch, websocket=websocket, chat_id=chat_id)
    try:
        return await catalog.list(caller=reader)
    finally:
        reader.close()


async def test_exact_turn_reader_can_materialize_and_read_but_never_write(catalog, human, bound, fixture):
    origin = await origin_for(human, bound, fixture)
    binding = guidance.bind_http_guidance(origin, expected_orchestrator=human[2],
                                         websocket=None, chat_id='read-only-turn')
    try:
        with guidance.use_turn_guidance(binding, expected_orchestrator=human[2]):
            reader = await guidance.acquire_turn_guidance_reader(expected_orchestrator=human[2], websocket=None, chat_id='read-only-turn')
            try:
                assert await catalog.list(caller=reader) == ()
                with pytest.raises(AssignmentError):
                    await catalog.save(caller=reader, **command())
                with pytest.raises(AssignmentError):
                    await catalog.delete(caller=reader, skill_id=command()['skill_id'],
                                         command_id=command()['command_id'], expected_revision=1)
            finally:
                reader.close()
    finally:
        origin.close()
    assert not fixture[-1]


async def test_turn_reader_cannot_toggle_or_escape_its_closed_scope(catalog, human, bound, fixture):
    saved = await catalog.save(caller=await selected(human, bound, fixture), **command())
    origin = await origin_for(human, bound, fixture)
    try:
        binding = guidance.bind_http_guidance(origin, expected_orchestrator=human[2], websocket=None, chat_id='turn')
        with guidance.use_turn_guidance(binding, expected_orchestrator=human[2]):
            reader = await guidance.acquire_turn_guidance_reader(expected_orchestrator=human[2], websocket=None, chat_id='turn')
            with pytest.raises(AssignmentError):
                await catalog.set_enabled(caller=reader, skill_id=saved.head.skill_id,
                    command_id=command()['command_id'], expected_revision=1, enabled=False)
        try:
            with pytest.raises(AssignmentError):
                await catalog.list(caller=reader)
        finally:
            reader.close()
        assert (await catalog.list(caller=await selected(human, bound, fixture)))[0].enabled
    finally:
        origin.close()


@pytest.mark.parametrize('socket', [False, True])
async def test_actual_rest_task_uses_http_owner_and_original_origin(catalog, human, bound, fixture, socket):
    from orchestrator.api import chat_router
    await catalog.save(caller=await selected(human, bound, fixture), **command())
    orch = human[2]
    ws = object() if socket else None
    orch.ui_clients = [ws] if socket else []
    orch.ui_sessions = {ws: {'sub': fixture[1], '_raw_token': 'untrusted-delivery-token'}} if socket else {}
    orch._save_user_profile = Mock()
    orch.history = SimpleNamespace(get_chat=Mock(return_value=True), create_chat=Mock())
    completed = asyncio.Event()
    observed = []

    async def turn(websocket, message, chat_id, display_message, *, user_id):
        try:
            binding = guidance.current_turn_guidance(expected_orchestrator=orch, websocket=websocket, chat_id=chat_id)
            observed.append((binding, await read(catalog, orch, websocket, chat_id), user_id, message, display_message))
        except Exception as exc:
            observed.append(exc)
        finally:
            completed.set()
    orch.handle_chat_message = turn
    bound[1].include_router(chat_router)
    headers = dict(incoming(bound, fixture).headers)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=bound[1]), base_url='https://app.invalid') as client:
        response = await client.post('/api/chats/rest-turn/messages', headers=headers,
                                     json={'message': '/weekly request', 'display_message': 'Original text'})
    assert response.status_code == 200
    await asyncio.wait_for(completed.wait(), 3)
    assert len(observed) == 1 and isinstance(observed[0], tuple), observed
    binding, skills, owner, message, display = observed[0]
    assert skills[0].command == 'weekly' and owner == fixture[1]
    assert message == '/weekly request' and display == 'Original text'
    assert binding.origin.closed
    assert guidance.current_turn_guidance(expected_orchestrator=orch, websocket=ws, chat_id='rest-turn') is None
    assert not fixture[-1]


async def test_subtask_inherits_actual_parent_origin_and_preserves_tool_budget(catalog, human, bound, fixture):
    from orchestrator import subtasks
    from orchestrator.chain_authority import ChainBudget
    await catalog.save(caller=await selected(human, bound, fixture), **command())
    orch = human[2]
    parent_ws = object()
    orch.ui_sessions = {parent_ws: {'sub': fixture[1]}}
    orch.history = SimpleNamespace(create_chat=Mock(return_value='real-child'))
    orch._chain_budgets = {}
    orch._safe_send = AsyncMock()
    observed = []
    async def turn(websocket, message, chat_id, **kwargs):
        binding = guidance.current_turn_guidance(expected_orchestrator=orch, websocket=websocket, chat_id=chat_id)
        observed.append((binding, await read(catalog, orch, websocket, chat_id), kwargs))
        await websocket.send_json({'type':'chat_message', 'payload':{'text':'Bound child result'}})
    orch.handle_chat_message = turn
    origin = await origin_for(human, bound, fixture)
    parent = guidance.bind_http_guidance(origin, expected_orchestrator=orch,
                                        websocket=parent_ws, chat_id='parent')
    budget = ChainBudget(turn_id='parent').slice(max_hops=3)
    orch._chain_budgets['parent'] = budget.parent
    try:
        with guidance.use_turn_guidance(parent, expected_orchestrator=orch):
            result = await subtasks._run_one(orch, {'title':'Child', 'instruction':'Read guidance'},
                user_id=fixture[1], parent_chat_id='parent', parent_ws=parent_ws,
                allowed_tools=['fetch_page'], budget=budget, correlation_id='synthetic-child', guidance_parent=parent)
        assert result.status == 'ok' and result.digest == 'Bound child result'
        binding, skills, kwargs = observed[0]
        assert binding.parent is parent and skills[0].command == 'weekly'
        assert kwargs['selected_tools'] == ['fetch_page']
        assert not origin.closed and orch._chain_budgets == {'parent': budget.parent}
        assert list(orch.ui_sessions) == [parent_ws]
    finally:
        origin.close()


def rest_setup(human, bound):
    from orchestrator.api import chat_router
    orch = human[2]
    orch.ui_clients, orch.ui_sessions = [], {}
    orch._save_user_profile = Mock()
    orch.history = SimpleNamespace(get_chat=Mock(return_value=True), create_chat=Mock())
    bound[1].include_router(chat_router)
    return orch, httpx.AsyncClient(transport=httpx.ASGITransport(app=bound[1], raise_app_exceptions=False),
                                  base_url='https://app.invalid')


@pytest.mark.parametrize('loss', ['missing_auth', 'bad_cookie', 'replacement', 'boundary'])
async def test_rest_refuses_invalid_original_caller_without_dispatch(catalog, human, bound, fixture, runtime, loss):
    orch, client = rest_setup(human, bound)
    orch.handle_chat_message = AsyncMock()
    headers = dict(incoming(bound, fixture).headers)
    if loss == 'missing_auth':
        headers = {'origin':'https://app.invalid'}
    elif loss == 'bad_cookie':
        headers['cookie'] = 'astral_session=invalid'
    elif loss == 'replacement':
        def replaced(*args, **kwargs):
            replace_session_record(runtime, get_session_record(runtime, fixture[2]))
            return True
        orch.history.get_chat.side_effect = replaced
    else:
        human[1].close()
    async with client:
        response = await client.post('/api/chats/declined/messages', headers=headers, json={'message':'Private input'})
    assert response.status_code in {401, 403, 503}
    orch.handle_chat_message.assert_not_awaited()
    if loss != 'replacement':
        orch.history.get_chat.assert_not_called()
    orch.history.create_chat.assert_not_called()
    assert not fixture[-1]


async def test_rest_new_chat_and_disabled_guidance_preserve_original_dispatch(human, bound, fixture, monkeypatch):
    from shared.feature_flags import flags
    monkeypatch.setitem(flags._flags, 'user_skills', False)
    orch, client = rest_setup(human, bound)
    orch.history.get_chat.return_value = None
    orch.human_request_boundary = None
    completed = asyncio.Event()
    async def turn(websocket, message, chat_id, display_message, **kwargs):
        assert websocket is None and message == 'Ordinary message'
        assert kwargs == {'user_id':fixture[1]}
        completed.set()
    orch.handle_chat_message = turn
    async with client:
        response = await client.post('/api/chats/new/messages', headers=dict(incoming(bound, fixture, cookie=False).headers),
                                     json={'message':'Ordinary message'})
    assert response.status_code == 200
    await asyncio.wait_for(completed.wait(), 3)
    orch.history.create_chat.assert_called_once_with('new', user_id=fixture[1])
    orch._save_user_profile.assert_called_once()


@pytest.mark.parametrize('end', ['create_failure', 'cancel_before_entry', 'failure', 'cancel_running'])
async def test_rest_origin_custody_is_released_on_all_task_exits(catalog, human, bound, fixture, monkeypatch, caplog, end):
    orch, client = rest_setup(human, bound)
    origins, tasks = [], []
    capture = guidance.capture_turn_guidance_from_human
    async def captured(*args, **kwargs):
        origin = await capture(*args, **kwargs)
        origins.append(origin)
        return origin
    monkeypatch.setattr(guidance, 'capture_turn_guidance_from_human', captured)
    create = asyncio.create_task
    def created(coro, *args, **kwargs):
        if coro.cr_code.co_name != 'dispatch':
            return create(coro, *args, **kwargs)
        if end == 'create_failure':
            raise RuntimeError('synthetic task creation failure')
        task = create(coro, *args, **kwargs)
        tasks.append(task)
        if end == 'cancel_before_entry':
            task.cancel()
        return task
    monkeypatch.setattr(asyncio, 'create_task', created)
    started = asyncio.Event()
    async def turn(*args, **kwargs):
        started.set()
        if end == 'failure':
            raise RuntimeError('synthetic-private-turn-content')
        await asyncio.Event().wait()
    orch.handle_chat_message = turn
    async with client:
        response = await client.post('/api/chats/end/messages', headers=dict(incoming(bound, fixture).headers),
                                     json={'message':'Synthetic private message'})
    assert response.status_code == (500 if end == 'create_failure' else 200)
    if end == 'cancel_running':
        await asyncio.wait_for(started.wait(), 3)
        tasks[0].cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.sleep(0)
    assert len(origins) == 1 and origins[0].closed
    assert 'synthetic-private-turn-content' not in caplog.text
    assert end != 'cancel_before_entry' or not started.is_set()


async def test_rest_accepted_task_cannot_read_after_original_session_replacement(catalog, human, bound, fixture, runtime):
    orch, client = rest_setup(human, bound)
    started, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    result = []
    async def turn(websocket, message, chat_id, display_message, **kwargs):
        started.set()
        try:
            await release.wait()
            result.append(await read(catalog, orch, websocket, chat_id))
        except AssignmentError:
            result.append('refused')
        finally:
            finished.set()
    orch.handle_chat_message = turn
    async with client:
        response = await client.post('/api/chats/late/messages', headers=dict(incoming(bound, fixture).headers),
                                     json={'message':'Private input'})
    assert response.status_code == 200 and response.json()['status'] == 'accepted'
    await asyncio.wait_for(started.wait(), 3)
    replace_session_record(runtime, get_session_record(runtime, fixture[2]))
    release.set()
    await asyncio.wait_for(finished.wait(), 3)
    assert result == ['refused']
    with runtime.transaction() as tx:
        assert runtime.repositories.preferences.skills.get_materialization(tx, owner_id=fixture[1]) is None


@pytest.mark.parametrize('loss', ['none', 'final_hop', 'missing_parent', 'wrong_owner', 'wrong_chat', 'closed_parent',
                                 'replacement', 'removed_budget', 'exhausted_budget', 'cancel',
                                 'removed_parent_budget', 'replaced_parent_budget'])
async def test_actual_subtask_fanout_retains_parent_and_child_boundaries(
    catalog, human, bound, fixture, runtime, monkeypatch, loss,
):
    from orchestrator import subtasks
    from orchestrator.chain_authority import ChainBudget
    from orchestrator.orchestrator import Orchestrator
    import types
    orch = human[2]
    parent_ws = object()
    orch.ui_sessions = {parent_ws:{'sub':fixture[1], '_raw_token':'claims-are-not-guidance'}}
    counter = iter(['child-one', 'child-two'])
    orch.history = SimpleNamespace(create_chat=Mock(side_effect=lambda **_: next(counter)))
    orch._chain_budgets = {'parent':ChainBudget(turn_id='parent', chat_id='parent',
                                               max_hops=2 if loss == 'final_hop' else 12)}
    orch._chain_budget_for = types.MethodType(Orchestrator._chain_budget_for, orch)
    orch._safe_send = AsyncMock()
    bindings, reads = [], []
    started = asyncio.Event()
    async def turn(websocket, message, chat_id, **kwargs):
        binding = guidance.current_turn_guidance(expected_orchestrator=orch, websocket=websocket, chat_id=chat_id)
        bindings.append(binding)
        if loss == 'removed_budget':
            orch._chain_budgets.pop(chat_id)
        elif loss == 'removed_parent_budget':
            orch._chain_budgets.pop('parent',None)
        elif loss == 'replaced_parent_budget':
            orch._chain_budgets['parent']=ChainBudget(turn_id='parent',chat_id='parent')
        elif loss == 'exhausted_budget':
            orch._chain_budgets[chat_id].started_at -= 1000
        elif loss == 'cancel':
            await websocket.send_json({'type':'chat_message','payload':{'text':'Discarded partial'}})
            started.set()
            await asyncio.Event().wait()
        reads.append(await read(catalog, orch, websocket, chat_id))
        await websocket.send_json({'type':'chat_message','payload':{'text':'Bound result'}})
    orch.handle_chat_message = turn
    origin = await origin_for(human, bound, fixture)
    holder = []
    async def parent_turn():
        parent = guidance.bind_http_guidance(origin, expected_orchestrator=orch, websocket=parent_ws, chat_id='parent')
        holder.append(parent)
        scope = nullcontext() if loss == 'missing_parent' else guidance.use_turn_guidance(parent, expected_orchestrator=orch)
        with scope:
            if loss == 'closed_parent':
                parent.close()
            elif loss == 'replacement':
                replace_session_record(runtime, get_session_record(runtime, fixture[2]))
            return await subtasks.handle_meta_tool(orch, 'delegate_subtasks',
                {'subtasks':[{'title':'One','instruction':'Read one'}, {'title':'Two','instruction':'Read two'}],
                 '_parent_tools':['fetch_page']}, user_id='other-owner' if loss == 'wrong_owner' else fixture[1],
                chat_id='other-chat' if loss == 'wrong_chat' else 'parent', websocket=parent_ws)
    pending = asyncio.create_task(parent_turn())
    try:
        if loss == 'cancel':
            await asyncio.wait_for(started.wait(), 3)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
        else:
            response = await pending
            if loss in {'missing_parent','wrong_owner','wrong_chat','closed_parent'}:
                assert response.error['message'] == 'guidance_read_unavailable'
                orch.history.create_chat.assert_not_called()
            else:
                results = response.result['subtasks']
                assert len(results) == 2
                assert all(row['status'] == ('ok' if loss in {'none', 'final_hop'} else 'failed') for row in results)
                assert all(row['digest'] == ('Bound result' if loss in {'none', 'final_hop'} else '') for row in results)
        assert len(reads) == (2 if loss in {'none', 'final_hop'} else 0)
        if loss == 'final_hop':
            assert orch._chain_budgets['parent'].spent_hops == orch._chain_budgets['parent'].max_hops == 2
        assert all(binding.closed and binding.websocket._closed for binding in bindings)
        assert list(orch.ui_sessions) == [parent_ws]
        assert set(orch._chain_budgets) == (set() if loss == 'removed_parent_budget' else {'parent'})
        assert not origin.closed
    finally:
        if not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        origin.close()
