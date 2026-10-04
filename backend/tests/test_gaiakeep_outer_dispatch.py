"""Exercises Gaia outer failures through the orchestrator's governed transport and ordinary refusal gates. Event-controlled workers cover timeout and cancellation without a live server or clock-derived acceptance bounds."""

import asyncio
import copy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock
import threading

import pytest

from orchestrator import gaiakeep_dispatch
from orchestrator.governed_dispatch import GovernedFinalDispatch
from shared.feature_flags import flags
from shared.protocol import MCPResponse
from tests.test_governed_final_dispatch import _Gateway, _Plane, _Repository, _runtime
from tests.test_remote_confirmation_063 import USER, _WS, _tc
from tests.test_remote_confirmation_063 import real_orch as _real_orch

real_orch = _real_orch


@pytest.fixture
def outer_orch(real_orch, monkeypatch):
    real_orch.agents = {'gaiakeep-1': object()}
    real_orch.local_agents = {}
    real_orch.a2a_clients = {'gaiakeep-1': object()}
    real_orch.agent_urls = {}
    real_orch._chat_recorders = {}
    real_orch.tool_permissions.get_tool_scope.return_value = 'tools:read'
    real_orch._execute_via_a2a = AsyncMock(return_value=MCPResponse(result={'unexpected': 'fallback'}))
    monkeypatch.setitem(flags._flags, 'progress_streaming', False)
    monkeypatch.setattr(gaiakeep_dispatch, '_record', AsyncMock())
    return real_orch


def _governed(orch, mode, *, failure=None):
    if mode == 'off':
        orch.governed_final_dispatch = GovernedFinalDispatch.off()
        return None
    gateway = _Gateway(mode, failure=failure)

    async def resolve(agent_id, owner_id):
        assert agent_id == 'gaiakeep-1' and owner_id == USER
        return replace(_runtime(), agent_id=agent_id, owner_id=owner_id)

    orch.governed_final_dispatch = GovernedFinalDispatch.active(
        gateway=gateway, plane=_Plane(), authority_repository=_Repository(), runtime_resolver=resolve)
    return gateway


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['off', 'shadow', 'enforce'])
@pytest.mark.parametrize('mutation', [False, True])
@pytest.mark.parametrize('outcome', ['timeout-response', 'raised', 'none'])
async def test_one_governed_attempt_keeps_native_identity_and_never_falls_back(
        outer_orch, mode, mutation, outcome):
    gateway = _governed(outer_orch, mode)
    tool = 'gaiakeep_core_repair' if mutation else 'gaiakeep_core_whoami'
    params = {'request_id': 'retained_native_identity'} if mutation else {}
    arguments = {'machine_id': 'owned-machine', 'params': params, 'user_id': USER,
                 '_delegation_token': 'private-delegation', '_credentials': {'secret': 'private-key'}}
    original = copy.deepcopy(arguments)
    observed = []

    async def physical(*args, **kwargs):
        observed.append(copy.deepcopy(args[2]))
        if outcome == 'raised':
            raise RuntimeError('private-key private-frame')
        if outcome == 'none':
            return None
        return MCPResponse(request_id='transport-request',
                           error={'message': 'private-key private-frame', 'retryable': True})

    outer_orch._execute_via_websocket = AsyncMock(side_effect=physical)
    response = await outer_orch._execute_with_retry(
        None, 'gaiakeep-1', tool, arguments, max_retries=9, user_id=USER,
        channel='rest', audit_correlation_id='audit-id', audit_actor_user_id=USER,
        audit_auth_principal=USER, audit_conversation_id='conversation')
    verdict = 'unconfirmed' if mutation else 'unavailable'
    assert response.error['data']['verdict'] == verdict
    assert response.error['retryable'] is False
    if mutation:
        assert response.error['data']['reconciliation'] == {'request_id': 'retained_native_identity'}
    assert arguments == original and observed == [original]
    assert 'private-key' not in str(response) and 'private-frame' not in str(response)
    outer_orch._execute_via_websocket.assert_awaited_once()
    outer_orch._execute_via_a2a.assert_not_awaited()
    gaiakeep_dispatch._record.assert_awaited_once_with(tool, verdict, True, mutation)
    if gateway:
        assert len(gateway.calls) == 1
        authorized = gateway.calls[0]
        assert authorized['final_arguments'] == original
        assert authorized['context'].agent_id == 'gaiakeep-1'
        assert all(permit.released for permit in gateway.permits)


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['off', 'shadow', 'enforce'])
async def test_agent_classified_failure_is_preserved_without_second_issue(outer_orch, mode):
    _governed(outer_orch, mode)
    original = MCPResponse(error={'code': -32603, 'retryable': False, 'message': 'safe refusal',
                                  'data': {'verdict': 'upstream_denied'}})
    outer_orch._execute_via_websocket = AsyncMock(return_value=original)
    response = await outer_orch._execute_with_retry(
        None, 'gaiakeep-1', 'gaiakeep_core_whoami', {'machine_id': 'mine', 'params': {}}, user_id=USER)
    assert response is original
    outer_orch._execute_via_websocket.assert_awaited_once()
    outer_orch._execute_via_a2a.assert_not_awaited()
    gaiakeep_dispatch._record.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('agent,tool,explicit,expected', [
    ('gaiakeep-1', 'gaiakeep_core_whoami', None, 130),
    ('gaiakeep-1', 'gaiakeep_core_whoami', 7.0, 7.0),
    ('other-agent', 'other_tool', None, 30.0),
    ('other-agent', 'other_tool', 7.0, 7.0),
])
async def test_default_budget_is_gaia_only_and_explicit_budget_wins(outer_orch, agent, tool, explicit, expected):
    outer_orch.execute_tool_and_wait = AsyncMock(return_value=MCPResponse(result={'ok': True}))
    await outer_orch._execute_with_retry(None, agent, tool, {}, user_id=USER, timeout=explicit)
    outer_orch.execute_tool_and_wait.assert_awaited_once()
    assert outer_orch.execute_tool_and_wait.await_args.kwargs['timeout'] == expected
    gaiakeep_dispatch._record.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('agent,tool,explicit,expected', [
    ('gaiakeep-1', 'gaiakeep_core_whoami', None, 130),
    ('gaiakeep-1', 'gaiakeep_core_whoami', 30.0, 30.0),
    ('other-agent', 'other_tool', None, 30.0),
    ('other-agent', 'other_tool', 7.0, 7.0),
])
async def test_direct_entry_budget_is_gaia_only(outer_orch, agent, tool, explicit, expected):
    outer_orch._dispatch_tool_call = AsyncMock(return_value=MCPResponse(result={'ok': True}))
    await outer_orch.execute_tool_and_wait(agent, tool, {}, timeout=explicit)
    outer_orch._dispatch_tool_call.assert_awaited_once()
    assert outer_orch._dispatch_tool_call.await_args.args[3] == expected


@pytest.mark.asyncio
async def test_other_agent_retains_existing_retry(outer_orch, monkeypatch):
    outer_orch.execute_tool_and_wait = AsyncMock(side_effect=[
        MCPResponse(error={'message': 'retry allowed', 'retryable': True}), MCPResponse(result={'ok': True})])
    outer_orch._safe_send = AsyncMock()
    monkeypatch.setattr(asyncio, 'sleep', AsyncMock())
    response = await outer_orch._execute_with_retry(None, 'other-agent', 'other_tool', {}, max_retries=2)
    assert response.result == {'ok': True}
    assert outer_orch.execute_tool_and_wait.await_count == 2
    asyncio.sleep.assert_awaited_once()
    gaiakeep_dispatch._record.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('denial', ['permission', 'identity'])
async def test_normal_gate_refusal_never_enters_physical_wrapper(outer_orch, denial):
    outer_orch._execute_via_websocket = AsyncMock()
    outer_orch._execute_in_process = AsyncMock()
    if denial == 'permission':
        outer_orch.tool_permissions.is_tool_allowed.return_value = False
    else:
        outer_orch.agent_cards['gaiakeep-1'] = SimpleNamespace(
            agent_id='gaiakeep-1', name='GaiaKeep', metadata={'required_identity_claims': ['orcid']})
    response = await outer_orch.execute_single_tool(
        _WS(), _tc('gaiakeep_core_whoami', {'machine_id': 'mine', 'params': {}}),
        {'gaiakeep_core_whoami': 'gaiakeep-1'}, 'chat', user_id=USER)
    assert response.error['retryable'] is False
    outer_orch._execute_via_websocket.assert_not_awaited()
    outer_orch._execute_in_process.assert_not_awaited()
    gaiakeep_dispatch._record.assert_not_awaited()
    assert outer_orch.proposal_storage.rows == {}


@pytest.mark.asyncio
@pytest.mark.parametrize('transport', ['websocket', 'in-process'])
async def test_late_identity_refusal_remains_predispatch_with_no_handler_or_send(outer_orch, transport):
    handler, sender = AsyncMock(), AsyncMock()
    outer_orch.agent_cards['gaiakeep-1'] = SimpleNamespace(
        agent_id='gaiakeep-1', name='GaiaKeep', metadata={'required_identity_claims': ['orcid']})
    if transport == 'in-process':
        outer_orch.local_agents = {'gaiakeep-1': SimpleNamespace(handle_mcp_request=handler)}
        outer_orch.agents = {}
    else:
        outer_orch.agents = {'gaiakeep-1': SimpleNamespace(send_text=sender)}
    response = await outer_orch._execute_with_retry(
        None, 'gaiakeep-1', 'gaiakeep_core_repair',
        {'machine_id': 'mine', 'params': {'request_id': 'retained_native_identity'}}, user_id=USER)
    assert response.error['code'] == 'required_identity_unavailable'
    assert response.error['retryable'] is False
    assert 'data' not in response.error
    handler.assert_not_awaited()
    sender.assert_not_awaited()
    outer_orch._execute_via_a2a.assert_not_awaited()
    gaiakeep_dispatch._record.assert_awaited_once_with('gaiakeep_core_repair', 'auth_failed', False, True)


@pytest.mark.asyncio
async def test_governed_authorization_refusal_never_enters_gaia_wrapper(outer_orch):
    from orchestrator.lets_gateway import LetsGatewayError

    _governed(outer_orch, 'enforce', failure=LetsGatewayError('receipt_policy_invalid'))
    outer_orch._execute_via_websocket = AsyncMock()
    response = await outer_orch._execute_with_retry(
        None, 'gaiakeep-1', 'gaiakeep_core_whoami', {'machine_id': 'mine', 'params': {}}, user_id=USER)
    assert response.error['code'] == 'receipt_policy_invalid'
    assert response.error['retryable'] is False
    outer_orch._execute_via_websocket.assert_not_awaited()
    gaiakeep_dispatch._record.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('cancel', [False, True])
async def test_inprocess_outer_end_keeps_running_worker_and_records_once(outer_orch, monkeypatch, cancel):
    started, waiting, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    release = threading.Event()
    tasks = []
    loop = asyncio.get_running_loop()

    def process():
        loop.call_soon_threadsafe(started.set)
        release.wait()
        loop.call_soon_threadsafe(finished.set)
        return MCPResponse(result={'verdict': 'ok'})

    async def handle(socket, request):
        tasks.append(asyncio.current_task())
        response = await asyncio.to_thread(process)
        response.request_id = request.request_id
        await socket.send_text(response.to_json())

    async def controlled_wait(future, *, timeout):
        assert timeout == 7.0
        await started.wait()
        waiting.set()
        if cancel:
            return await future
        future.cancel()
        raise TimeoutError

    outer_orch.agents = {}
    outer_orch.local_agents = {'gaiakeep-1': SimpleNamespace(handle_mcp_request=handle)}
    outer_orch.pending_requests = {}
    outer_orch.pending_ui_sockets = {}
    outer_orch._pending_request_agent = {}
    outer_orch._register_dispatch_context = lambda *args: None
    outer_orch._dispatch_context = {}
    outer_orch.handle_agent_message = AsyncMock()
    monkeypatch.setattr(asyncio, 'wait_for', controlled_wait)
    arguments = {'machine_id': 'mine', 'params': {'request_id': 'retained_native_identity'}}
    dispatch = asyncio.create_task(outer_orch._execute_with_retry(
        None, 'gaiakeep-1', 'gaiakeep_core_repair', arguments, user_id=USER, timeout=7.0))
    try:
        await waiting.wait()
        if cancel:
            dispatch.cancel()
            with pytest.raises(asyncio.CancelledError):
                await dispatch
        else:
            response = await dispatch
            assert response.error['data'] == {
                'verdict': 'unconfirmed', 'reconciliation': {'request_id': 'retained_native_identity'}}
            assert response.error['retryable'] is False
        assert len(tasks) == 1 and not tasks[0].done() and not finished.is_set()
        assert outer_orch.pending_requests == {} and outer_orch.pending_ui_sockets == {}
        assert outer_orch._pending_request_agent == {} and outer_orch._dispatch_context == {}
        gaiakeep_dispatch._record.assert_awaited_once_with('gaiakeep_core_repair', 'unconfirmed', True, True)
        outer_orch._execute_via_a2a.assert_not_awaited()
    finally:
        release.set()
        if not dispatch.done():
            dispatch.cancel()
        await asyncio.gather(dispatch, *tasks, return_exceptions=True)
    assert finished.is_set() and tasks[0].done()
