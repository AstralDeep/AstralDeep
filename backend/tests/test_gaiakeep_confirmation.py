"""Exercises GaiaKeep approval through the actual shared gate and existing Plane proposal test repositories."""

import copy
import json
import re
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from agents.gaiakeep.catalog import TOOLS, is_mutation
from agents.gaiakeep.tests.test_dispatch import server as _gaia_server
from orchestrator import remote_confirmation as rc
from orchestrator import taint
from shared.protocol import MCPRequest, MCPResponse

from tests.test_remote_confirmation_063 import _WS, USER, _FakeDB, _orch, _seed, _tc
from tests.test_remote_confirmation_063 import real_orch as _real_orch

real_orch = _real_orch
gaia_server = _gaia_server


@pytest.fixture(autouse=True)
def owned_machine(monkeypatch):
    from orchestrator import remote_machines
    monkeypatch.setattr(remote_machines, 'resolve_machine', lambda db, owner, ref: {'owner_user_id': owner})


@pytest.mark.parametrize('tool', ['gaiakeep_core_whoami', 'gaiakeep_core_repair'])
@pytest.mark.parametrize('failure', ['foreign', 'missing', 'repository-failure'])
def test_owner_denial_precedes_proposal_or_approval_consumption(monkeypatch, tool, failure):
    from orchestrator import remote_machines
    db = _FakeDB()
    orch = _orch(db)
    calls = []
    def lookup(source, owner, ref):
        calls.append((owner, ref))
        if failure == 'repository-failure':
            raise OSError('private repository detail')
        return None
    monkeypatch.setattr(remote_machines, 'resolve_machine', lookup)
    args = {'machine_id': 'unavailable', 'params': {}, '_remote_op_proposal_id': 'foreign-approval'}
    result = rc.evaluate(orch, object(), 'gaiakeep-1', tool, args, 'chat', 'caller')
    assert result and result[1][0]['type'] == 'alert'
    assert 'private repository detail' not in str(result)
    assert calls == [('caller', 'unavailable')] and db.rows == {}
    assert args['_remote_op_proposal_id'] == 'foreign-approval'


def test_every_mutation_requires_approval():
    for name in TOOLS:
        assert rc.classification_for(name, 'gaiakeep-1') == ('always' if is_mutation(name) else 'never')
        assert taint.is_sink('gaiakeep-1', name) is is_mutation(name)
    assert rc.classification_for('unreviewed', 'gaiakeep-1') == 'always'
    assert taint.classify_source('gaiakeep-1', None) == taint.UNTRUSTED


def test_live_approval_single_use_and_changed_arguments(monkeypatch):
    db = _FakeDB()
    orch = _orch(db)
    ws = object()
    orch.ui_sessions[ws] = {'user_id': 'owner'}
    monkeypatch.setattr(rc, '_machine_label', lambda *a: 'registered DGX')
    args = {'machine_id': 'mine', 'params': {'reason': 'requested'}}
    denied = rc.evaluate(orch, ws, 'gaiakeep-1', 'gaiakeep_core_auditack', args, 'chat', 'owner')
    assert denied and denied[1][0]['type'] == 'card'
    pid = next(iter(db.rows))
    db.rows[pid]['status'] = 'approved'
    approved = dict(args, _remote_op_proposal_id=pid)
    assert rc.evaluate(orch, ws, 'gaiakeep-1', 'gaiakeep_core_auditack', approved, 'chat', 'owner') is None
    assert db.rows[pid]['status'] == 'consumed'
    assert rc.evaluate(orch, ws, 'gaiakeep-1', 'gaiakeep_core_auditack', dict(args, _remote_op_proposal_id=pid), 'chat', 'owner')


@pytest.mark.parametrize('owner,changed,expiry', [('other', False, False), ('owner', True, False), ('owner', False, True)])
def test_approval_denials(owner, changed, expiry, monkeypatch):
    db = _FakeDB()
    orch = _orch(db)
    ws = object()
    orch.ui_sessions[ws] = {'user_id': owner}
    monkeypatch.setattr(rc, '_machine_label', lambda *a: 'machine')
    args = {'machine_id': 'mine', 'params': {'reason': 'requested'}}
    _seed(db, 'p', owner='owner', verb='gaiakeep_core_auditack', args=args, agent_id='gaiakeep-1',
          expires_at=int(time.time()) - 20 if expiry else None)
    if changed:
        args['params'] = {'reason': 'changed'}
    assert rc.evaluate(orch, ws, 'gaiakeep-1', 'gaiakeep_core_auditack', dict(args, _remote_op_proposal_id='p'), 'chat', owner)
    assert db.rows['p']['status'] != 'consumed'


def test_unattended_and_safe_read():
    orch = _orch(_FakeDB())
    assert rc.evaluate(orch, None, 'gaiakeep-1', 'gaiakeep_core_repair', {'machine_id': 'mine', 'params': {}}, 'chat', 'owner')
    assert rc.evaluate(orch, None, 'gaiakeep-1', 'gaiakeep_core_whoami', {'machine_id': 'mine', 'params': {}}, 'chat', 'owner') is None


def test_invalid_arguments_do_not_create_approval():
    db = _FakeDB()
    orch = _orch(db)
    result = rc.evaluate(orch, object(), 'gaiakeep-1', 'gaiakeep_core_auditack',
                         {'machine_id': 'mine', 'params': {'principal': 'attacker'}}, 'chat', 'owner')
    assert result and 'Invalid GaiaKeep arguments' in result[0]
    assert db.rows == {}


def test_summary_excludes_upload_bytes_and_credentials(monkeypatch):
    monkeypatch.setattr(rc, '_machine_label', lambda *a: 'machine')
    summary = rc._gaiakeep_summary(None, 'owner', 'gaiakeep_upload_file',
                                  {'machine_id': 'mine', 'data_base64': 'private-file-content',
                                   '_credentials': {'key': 'private-key'}})
    assert 'private-file-content' not in summary
    assert 'private-key' not in summary
    assert 'sha256' in summary


def test_upload_request_identity_is_retained_before_approval(monkeypatch):
    db = _FakeDB()
    orch = _orch(db)
    ws = object()
    orch.ui_sessions[ws] = {'user_id': 'owner'}
    monkeypatch.setattr(rc, '_machine_label', lambda *a: 'registered DGX')
    tool = 'gaiakeep_upload_file'
    args = {'machine_id': 'mine', 'collection_id': 'scratch', 'path': 'sample.txt', 'data_base64': 'b2s='}
    assert rc.evaluate(orch, ws, 'gaiakeep-1', tool, args, 'chat', 'owner')
    request_id = args['request_id']
    assert re.fullmatch(r'[A-Za-z0-9_-]{16,64}', request_id)
    pid, = db.rows
    stored = json.loads(db.rows[pid]['args_json'])
    assert stored['request_id'] == request_id
    db.rows[pid]['status'] = 'approved'
    altered = dict(args, request_id='different_request_identity', _remote_op_proposal_id=pid)
    assert rc.evaluate(orch, ws, 'gaiakeep-1', tool, altered, 'chat', 'owner')
    assert db.rows[pid]['status'] == 'approved'
    approved = dict(args, _remote_op_proposal_id=pid)
    assert rc.evaluate(orch, ws, 'gaiakeep-1', tool, approved, 'chat', 'owner') is None
    assert approved['request_id'] == request_id and db.rows[pid]['status'] == 'consumed'


def test_core_request_identity_preparation_preserves_approved_parameters(monkeypatch):
    db = _FakeDB()
    orch = _orch(db)
    ws = object()
    orch.ui_sessions[ws] = {'user_id': 'owner'}
    monkeypatch.setattr(rc, '_machine_label', lambda *a: 'registered DGX')
    original = {'wait_ms': 10}
    args = {'machine_id': 'mine', 'params': original}
    assert rc.evaluate(orch, ws, 'gaiakeep-1', 'gaiakeep_core_repair', args, 'chat', 'owner')
    assert original == {'wait_ms': 10}
    assert args['params'] == dict(original, request_id=args['params']['request_id'])
    row, = db.rows.values()
    assert json.loads(row['args_json'])['params'] == args['params']


@pytest.mark.parametrize('strategy', ['ingest', 'have'])
def test_upload_approval_preserves_the_selected_request_identity_mode(monkeypatch, strategy):
    db = _FakeDB()
    orch = _orch(db)
    ws = object()
    orch.ui_sessions[ws] = {'user_id': 'owner'}
    monkeypatch.setattr(rc, '_machine_label', lambda *a: 'registered DGX')
    args = {'machine_id': 'mine', 'collection_id': 'scratch', 'path': 'sample.txt',
            'data_base64': 'b2s=', 'strategy': strategy}
    if strategy == 'ingest':
        args['request_id'] = 'retained_request_identity'
    before = json.loads(json.dumps(args))
    assert rc.evaluate(orch, ws, 'gaiakeep-1', 'gaiakeep_upload_file', args, 'chat', 'owner')
    assert args == before
    row, = db.rows.values()
    assert json.loads(row['args_json']) == before


@pytest.mark.asyncio
async def test_normal_dispatch_requires_approval_and_binds_owner(real_orch, monkeypatch):
    monkeypatch.setattr(rc, '_machine_label', lambda *a: 'registered DGX')
    tool = 'gaiakeep_core_auditack'
    response = await real_orch.execute_single_tool(
        _WS(), _tc(tool, {'machine_id': 'mine', 'params': {'reason': 'requested'}, 'user_id': 'attacker'}),
        {tool: 'gaiakeep-1'}, 'chat', user_id=USER)
    assert 'confirmation_required' in response.error['message']
    row, = real_orch.proposal_storage.rows.values()
    assert row['owner_user_id'] == USER
    assert json.loads(row['args_json'])['user_id'] == USER
    real_orch.tool_permissions.is_tool_allowed.assert_called_once_with(USER, 'gaiakeep-1', tool)


@pytest.mark.asyncio
async def test_normal_dispatch_permission_denial_precedes_proposal(real_orch):
    real_orch.tool_permissions.is_tool_allowed.return_value = False
    tool = 'gaiakeep_core_auditack'
    response = await real_orch.execute_single_tool(
        _WS(), _tc(tool, {'machine_id': 'mine', 'params': {}}),
        {tool: 'gaiakeep-1'}, 'chat', user_id=USER)
    assert 'restricted' in response.error['message']
    assert real_orch.proposal_storage.rows == {}


@pytest.mark.asyncio
async def test_normal_dispatch_mcp_mutation_denied(real_orch):
    ws = _WS()
    real_orch.ui_sessions[ws] = {'_invocation_channel': 'mcp'}
    tool = 'gaiakeep_core_auditack'
    response = await real_orch.execute_single_tool(
        ws, _tc(tool, {'machine_id': 'mine', 'params': {}}),
        {tool: 'gaiakeep-1'}, 'chat', user_id=USER)
    assert 'unattended MCP' in response.error['message']
    assert real_orch.proposal_storage.rows == {}


@pytest.mark.asyncio
async def test_approved_mutation_still_obeys_product_policy(real_orch, monkeypatch):
    from orchestrator import policy
    tool = 'gaiakeep_core_auditack'
    args = {'machine_id': 'mine', 'params': {'reason': 'requested'}, 'user_id': USER}
    _seed(real_orch.proposal_storage, 'approval', owner=USER, verb=tool, args=args, agent_id='gaiakeep-1')
    real_orch._policy_roles = lambda ws: []
    monkeypatch.setattr(policy, 'policy_enabled', lambda: True)
    monkeypatch.setattr(policy, 'load_rules', list)
    monkeypatch.setattr(policy, 'evaluate_policy', lambda *a: policy.PolicyDecision(effect=policy.DENY, reason='policy denial'))
    response = await real_orch.execute_single_tool(
        _WS(), _tc(tool, dict(args, _remote_op_proposal_id='approval')),
        {tool: 'gaiakeep-1'}, 'chat', user_id=USER)
    assert response.error['message'] == 'policy denial'
    assert real_orch.proposal_storage.rows['approval']['status'] == 'consumed'


@pytest.mark.asyncio
async def test_prepared_dispatch_context_reaches_agent(real_orch, gaia_server, monkeypatch):
    from orchestrator.orchestrator import PreparedDispatch
    from shared.feature_flags import flags
    from shared.protocol import MCPRequest
    monkeypatch.setitem(flags._flags, 'hook_system', False)
    real_orch.credential_manager = MagicMock()
    real_orch.credential_manager.get_agent_credentials_encrypted.return_value = None
    real_orch.local_agents = {}
    real_orch.agents = {'gaiakeep-1': object()}
    real_orch.a2a_clients = {}
    real_orch._get_delegation_token = AsyncMock(return_value='delegated-authority')
    real_orch._is_long_running_tool = lambda *a: False
    tool = 'gaiakeep_core_whoami'
    prepared = await real_orch._run_gate_stack(
        _WS(), 'gaiakeep-1', tool, {'machine_id': 'mine', 'params': {}, 'user_id': 'attacker'},
        user_id=USER, auto_subscribe_stream=False)
    assert isinstance(prepared, PreparedDispatch)
    assert prepared.args['user_id'] == USER
    assert prepared.args['_delegation_token'] == 'delegated-authority'
    response = gaia_server.process_request(MCPRequest(
        method='tools/call', params={'name': tool, 'arguments': prepared.args}))
    assert response.error is None
    assert response.result['verdict'] == 'ok'


@pytest.fixture
def approved_gaia_delivery(monkeypatch, gaia_server):
    db = _FakeDB()
    orch = _orch(db)
    websocket = _WS()
    orch.ui_sessions[websocket] = {'user_id': USER}
    orch._send_or_replace_components = AsyncMock(return_value=[{'created': True}])
    async def publish(**arguments):
        return await arguments['mutation']()
    orch.run_detached_conversation_mutation = AsyncMock(side_effect=publish)
    original_policy = rc.policy_for
    policy = original_policy('gaiakeep-1')
    policy.auto_continue = False
    monkeypatch.setattr(rc, 'policy_for', lambda agent: policy if agent == 'gaiakeep-1' else original_policy(agent))
    arguments = {'machine_id': 'mine', 'params': {'request_id': 'retained_native_identity'},
                 'user_id': USER, 'session_id': 'approved-chat'}
    _seed(db, 'approved-gaia', owner=USER, verb='gaiakeep_core_repair', args=arguments,
          status='pending', chat_id='approved-chat', agent_id='gaiakeep-1')
    observed = []

    async def dispatch(socket, tool_call, mapping, chat_id, *, user_id):
        public = json.loads(tool_call.function.arguments)
        assert rc.evaluate(orch, socket, 'gaiakeep-1', tool_call.function.name, public, chat_id, user_id) is None
        public['_credentials'] = {}
        response = gaia_server.process_request(MCPRequest(
            method='tools/call', params={'name': tool_call.function.name, 'arguments': public}))
        response.correlation_id = "approved-gaia-correlation"
        observed.append(response)
        return response

    orch.execute_single_tool = AsyncMock(side_effect=dispatch)
    return SimpleNamespace(db=db, orch=orch, websocket=websocket, observed=observed, arguments=arguments)


@pytest.mark.asyncio
async def test_approved_gaia_result_is_published_once_in_its_owned_conversation(approved_gaia_delivery):
    fixture = approved_gaia_delivery
    await rc.handle_decision(fixture.orch, fixture.websocket, USER,
                             {'proposal_id': 'approved-gaia', 'decision': 'approve', 'machine_id': 'attacker'})
    response, = fixture.observed
    assert response.error is None and response.result['result']['job_id'] == 'real-job'
    assert fixture.db.rows['approved-gaia']['status'] == 'consumed'
    fixture.orch._send_or_replace_components.assert_awaited_once()
    arguments, keywords = fixture.orch._send_or_replace_components.await_args
    assert arguments[0] is fixture.websocket and arguments[2] == 'approved-chat'
    assert keywords == {'user_id': USER}
    expected = {'machine_id': 'mine', 'params': {'request_id': 'retained_native_identity'}}
    assert all(component['_source_agent'] == 'gaiakeep-1'
               and component['_source_tool'] == 'gaiakeep_core_repair'
               and component['_source_params'] == expected for component in arguments[1])
    assert all(component['_source_correlation_id'] == 'approved-gaia-correlation'
               for component in arguments[1])
    children = arguments[1][0]['content']
    assert children and all(child['_source_agent'] == 'gaiakeep-1'
                            and child['_source_tool'] == 'gaiakeep_core_repair'
                            and child['_source_correlation_id'] == 'approved-gaia-correlation' for child in children)
    assert all('_source_agent' not in component for component in response.ui_components)
    assert all('_source_agent' not in child for child in response.ui_components[0]['content'])
    assert arguments[1] is not response.ui_components
    await rc.handle_decision(fixture.orch, fixture.websocket, USER,
                             {'proposal_id': 'approved-gaia', 'decision': 'approve'})
    fixture.orch.execute_single_tool.assert_awaited_once()
    fixture.orch._send_or_replace_components.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('response', [None, MCPResponse(error={'message': 'safe refusal'}),
                                     MCPResponse(result={'status': 'ok'}, ui_components=[])])
async def test_missing_failed_or_empty_approved_gaia_result_is_not_published(approved_gaia_delivery, response):
    fixture = approved_gaia_delivery
    fixture.orch.execute_single_tool = AsyncMock(return_value=copy.deepcopy(response))
    await rc.handle_decision(fixture.orch, fixture.websocket, USER,
                             {'proposal_id': 'approved-gaia', 'decision': 'approve'})
    fixture.orch._send_or_replace_components.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['foreign-owner', 'expired', 'declined'])
async def test_unapproved_gaia_decision_never_dispatches_or_publishes(approved_gaia_delivery, case):
    fixture = approved_gaia_delivery
    owner, decision = USER, 'approve'
    if case == 'foreign-owner':
        owner = 'other-owner'
    elif case == 'expired':
        fixture.db.rows['approved-gaia']['expires_at'] = int(time.time()) - 1
    else:
        decision = 'decline'
    await rc.handle_decision(fixture.orch, fixture.websocket, owner,
                             {'proposal_id': 'approved-gaia', 'decision': decision})
    fixture.orch.execute_single_tool.assert_not_awaited()
    fixture.orch._send_or_replace_components.assert_not_awaited()


@pytest.mark.asyncio
async def test_gaia_publication_failure_never_replays_the_completed_mutation(approved_gaia_delivery):
    fixture = approved_gaia_delivery
    fixture.orch._send_or_replace_components.side_effect = OSError('delivery closed')
    with pytest.raises(OSError, match='delivery closed'):
        await rc.handle_decision(fixture.orch, fixture.websocket, USER,
                                 {'proposal_id': 'approved-gaia', 'decision': 'approve'})
    assert fixture.db.rows['approved-gaia']['status'] == 'consumed'
    assert fixture.observed[0].error is None
    await rc.handle_decision(fixture.orch, fixture.websocket, USER,
                             {'proposal_id': 'approved-gaia', 'decision': 'approve'})
    fixture.orch.execute_single_tool.assert_awaited_once()
    fixture.orch._send_or_replace_components.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['no-socket', 'no-conversation', 'other-agent'])
async def test_gaia_delivery_requires_its_socket_conversation_and_agent(approved_gaia_delivery, case):
    fixture = approved_gaia_delivery
    row = fixture.db.rows['approved-gaia']
    socket = fixture.websocket
    if case == 'no-socket':
        socket = None
    elif case == 'no-conversation':
        row['chat_id'] = None
    else:
        row['agent_id'] = 'remote-compute-1'
    fixture.orch.execute_single_tool = AsyncMock(return_value=MCPResponse(
        result={'status': 'ok'}, ui_components=[{'type': 'text', 'content': 'safe result'}]))
    await rc.handle_decision(fixture.orch, socket, USER,
                             {'proposal_id': 'approved-gaia', 'decision': 'approve'})
    fixture.orch._send_or_replace_components.assert_not_awaited()


@pytest.mark.asyncio
async def test_approved_gaia_permission_denial_still_prevents_result_publication(real_orch):
    real_orch.tool_permissions.is_tool_allowed.return_value = False
    real_orch._send_or_replace_components = AsyncMock()
    real_orch._execute_with_retry = AsyncMock()
    real_orch._serialized_chat = AsyncMock()
    _seed(real_orch.proposal_storage, 'denied-gaia', owner=USER, verb='gaiakeep_core_repair',
          args={'machine_id': 'mine', 'params': {'request_id': 'retained_native_identity'}, 'user_id': USER},
          status='pending', chat_id=None, agent_id='gaiakeep-1')
    await rc.handle_decision(real_orch, _WS(), USER, {'proposal_id': 'denied-gaia', 'decision': 'approve'})
    real_orch._execute_with_retry.assert_not_awaited()
    real_orch._send_or_replace_components.assert_not_awaited()
    assert real_orch.proposal_storage.rows['denied-gaia']['status'] == 'approved'
