"""Exercises Gaia outer failures through the orchestrator's governed transport and ordinary refusal gates. Event-controlled workers cover timeout and cancellation without a live server or clock-derived acceptance bounds."""

import asyncio
import copy
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import threading

import pytest

from orchestrator import gaiakeep_dispatch
from orchestrator.governed_dispatch import GovernedFinalDispatch
from shared.feature_flags import flags
from shared.protocol import MCPResponse
from tests.test_governed_final_dispatch import _Gateway, _Plane, _Repository, _runtime
from tests.test_remote_confirmation_063 import USER, _WS, _tc
from tests.test_remote_confirmation_063 import real_orch as _real_orch  # noqa: F401

@pytest.fixture
def real_orch(_real_orch):  # noqa: F811
    from agents.gaiakeep.catalog import TOOLS

    _real_orch.tool_permissions.get_tool_scope_map.return_value.update({
        name: entry["scope"] for name, entry in TOOLS.items()})
    return _real_orch


@pytest.fixture
def outer_orch(real_orch, monkeypatch):
    from orchestrator import remote_machines

    real_orch.agents = {'gaiakeep-1': object()}
    real_orch.local_agents = {}
    real_orch.a2a_clients = {'gaiakeep-1': object()}
    real_orch.agent_urls = {}
    real_orch._chat_recorders = {}
    real_orch.tool_permissions.get_tool_scope.return_value = 'tools:read'
    real_orch._execute_via_a2a = AsyncMock(return_value=MCPResponse(result={'unexpected': 'fallback'}))
    monkeypatch.setitem(flags._flags, 'progress_streaming', False)
    monkeypatch.setattr(gaiakeep_dispatch, '_record', AsyncMock())
    monkeypatch.setattr(remote_machines, 'resolve_machine', lambda db, owner, ref: {'owner_user_id': owner})
    socket = _WS()
    real_orch._dispatch_socket = socket
    real_orch.ui_sessions[socket] = {'sub': USER}
    real_orch._ws_active_chat = {id(socket): 'conversation'}
    real_orch.audit_recorder = SimpleNamespace(record=AsyncMock(return_value=object()))
    real_orch.history = SimpleNamespace(get_chat=lambda chat, *, user_id: {'id': chat, 'owner': user_id})
    real_orch._map_file_paths = lambda chat, args, **kwargs: args
    real_orch._is_long_running_tool = lambda *args: False
    real_orch._get_delegation_token = AsyncMock(return_value=None)
    real_orch.credential_manager = MagicMock()
    real_orch.credential_manager.get_agent_credentials_encrypted.return_value = None
    real_orch._llm_store = SimpleNamespace(get=AsyncMock(return_value=None), get_system=AsyncMock(return_value=None))
    real_orch._send_or_replace_components = AsyncMock(return_value=[])
    real_orch._serialized_chat = AsyncMock()

    async def publish(*, mutation, **kwargs):
        return await mutation()

    real_orch.run_detached_conversation_mutation = publish
    return real_orch


async def _reviewed_attempt(orch, tool, arguments, invoke):
    from orchestrator import hitl_confirmation, remote_confirmation

    socket = orch._dispatch_socket
    original = orch._execute_with_retry
    responses = []
    public = {key: value for key, value in arguments.items() if not key.startswith('_')}
    public['user_id'] = USER

    async def admitted(websocket, agent, name, prepared, **kwargs):
        assert websocket is socket and agent == 'gaiakeep-1' and name == tool
        assert prepared['user_id'] == USER
        assert {key: prepared[key] for key in public} == public
        result = await invoke(socket)
        responses.append(result)
        return result

    orch._execute_with_retry = admitted
    try:
        response = await orch.execute_single_tool(socket, _tc(tool, public), {tool: 'gaiakeep-1'},
                                                  'conversation', user_id=USER)
        assert response.result['_data']['status'] == 'confirmation_required'
        assert not responses
        proposal, = orch.proposal_storage.rows.values()
        await remote_confirmation.handle_decision(orch, socket, USER,
            {'proposal_id': proposal['proposal_id'], 'decision': 'approve'})
        assert not responses
        pending, = orch._hitl_pending_calls.values()
        assert pending.tool == tool and json.loads(pending.arguments) == public
        await hitl_confirmation.handle_decision(orch, socket, USER,
            {'hitl_request_id': pending.request_id, 'decision': 'approve'})
        assert len(responses) == 1
        assert not orch._hitl_pending_calls
        return responses[0]
    finally:
        orch._execute_with_retry = original


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
@pytest.mark.parametrize('outcome', ['timeout-response', 'raised', 'none'])
async def test_one_governed_attempt_keeps_native_identity_and_never_falls_back(
        outer_orch, mode, outcome):
    gateway = _governed(outer_orch, mode)
    tool = 'gaiakeep_core_whoami'
    arguments = {'machine_id': 'owned-machine', 'params': {}, 'user_id': USER,
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
    retry = outer_orch._execute_with_retry

    async def invoke(socket):
        return await retry(socket, 'gaiakeep-1', tool, arguments, max_retries=9, user_id=USER,
            channel='rest', audit_correlation_id='audit-id', audit_actor_user_id=USER,
            audit_auth_principal=USER, audit_conversation_id='conversation')

    response = await _reviewed_attempt(outer_orch, tool, arguments, invoke) if mutation else await invoke(None)
    verdict = 'unconfirmed' if mutation else 'unavailable'
    assert response.error['data']['verdict'] == verdict
    assert response.error['retryable'] is False
    assert arguments == original and observed == [original]
    assert 'private-key' not in str(response) and 'private-frame' not in str(response)
    outer_orch._execute_via_websocket.assert_awaited_once()
    outer_orch._execute_via_a2a.assert_not_awaited()
    gaiakeep_dispatch._record.assert_awaited_once_with(tool, verdict, True, False)
    if gateway:
        assert len(gateway.calls) == 1
        authorized = gateway.calls[0]
        assert authorized['final_arguments'] == original
        assert authorized['context'].agent_id == 'gaiakeep-1'
        assert all(permit.released for permit in gateway.permits)


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['off', 'shadow', 'enforce'])
@pytest.mark.parametrize('outcome', ['timeout-response', 'raised', 'none'])
async def test_headless_governed_mutation_is_refused_before_dispatch(
        outer_orch, mode, outcome):
    _governed(outer_orch, mode)
    arguments = {'machine_id': 'owned-machine', 'params': {'request_id': 'retained_native_identity'},
                 'user_id': USER, '_delegation_token': 'private-delegation',
                 '_credentials': {'secret': 'private-key'}}
    original = copy.deepcopy(arguments)
    outer_orch._execute_via_websocket = AsyncMock()
    response = await outer_orch._execute_with_retry(
        None, 'gaiakeep-1', 'gaiakeep_core_repair', arguments, max_retries=9, user_id=USER,
        channel='rest', audit_correlation_id='audit-id', audit_actor_user_id=USER,
        audit_auth_principal=USER, audit_conversation_id='conversation')
    assert response.error['code'] == 'source_bound_approval_required'
    assert response.error['retryable'] is False
    assert arguments == original
    assert 'private-key' not in str(response) and 'private-frame' not in str(response)
    outer_orch._execute_via_websocket.assert_not_awaited()
    outer_orch._execute_via_a2a.assert_not_awaited()
    gaiakeep_dispatch._record.assert_not_awaited()


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
    if transport == 'in-process':
        outer_orch.local_agents = {'gaiakeep-1': SimpleNamespace(handle_mcp_request=handler)}
        outer_orch.agents = {}
    else:
        outer_orch.agents = {'gaiakeep-1': SimpleNamespace(send_text=sender)}
    arguments = {'machine_id': 'mine', 'params': {'request_id': 'retained_native_identity'}, 'user_id': USER}
    retry = outer_orch._execute_with_retry

    async def invoke(socket):
        outer_orch.agent_cards['gaiakeep-1'] = SimpleNamespace(
            agent_id='gaiakeep-1', name='GaiaKeep', metadata={'required_identity_claims': ['orcid']})
        return await retry(socket, 'gaiakeep-1', 'gaiakeep_core_repair', arguments,
                           user_id=USER, audit_conversation_id='conversation')

    response = await _reviewed_attempt(outer_orch, 'gaiakeep_core_repair', arguments, invoke)
    assert response.error['code'] == 'required_identity_unavailable'
    assert response.error['retryable'] is False
    assert 'data' not in response.error
    handler.assert_not_awaited()
    sender.assert_not_awaited()
    outer_orch._execute_via_a2a.assert_not_awaited()
    gaiakeep_dispatch._record.assert_awaited_once_with('gaiakeep_core_whoami', 'auth_failed', False, False)


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
async def test_inprocess_headless_mutation_is_refused_before_any_worker(outer_orch):
    outer_orch.agents = {}
    outer_orch.local_agents = {'gaiakeep-1': SimpleNamespace(handle_mcp_request=AsyncMock())}
    outer_orch.pending_requests = {}
    outer_orch.pending_ui_sockets = {}
    outer_orch._pending_request_agent = {}
    outer_orch._register_dispatch_context = lambda *args: None
    outer_orch._dispatch_context = {}
    outer_orch.handle_agent_message = AsyncMock()
    arguments = {'machine_id': 'mine', 'params': {'request_id': 'retained_native_identity'}, 'user_id': USER}
    retry = outer_orch._execute_with_retry

    async def invoke(socket):
        monkeypatch.setattr(asyncio, 'wait_for', controlled_wait)
        return await retry(socket, 'gaiakeep-1', 'gaiakeep_core_repair', arguments,
                           user_id=USER, timeout=7.0, audit_conversation_id='conversation')

    dispatch = asyncio.create_task(_reviewed_attempt(outer_orch, 'gaiakeep_core_repair', arguments, invoke))
    waiter = asyncio.create_task(waiting.wait())
    try:
        await asyncio.wait({dispatch, waiter}, return_when=asyncio.FIRST_COMPLETED)
        if dispatch.done():
            await dispatch
        assert waiting.is_set()
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
        waiter.cancel()
        if not dispatch.done():
            dispatch.cancel()
        await asyncio.gather(dispatch, waiter, *tasks, return_exceptions=True)
    assert finished.is_set() and tasks[0].done()
