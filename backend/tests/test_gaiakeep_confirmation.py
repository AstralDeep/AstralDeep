"""Checks GaiaKeep ownership, mutation refusal, reconciliation IDs and the authenticated read path through the node."""

import copy
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from agents.gaiakeep.catalog import TOOLS, is_mutation
from agents.gaiakeep.tests.test_dispatch import server as _gaia_server
from orchestrator import remote_confirmation as rc
from orchestrator import taint

from tests.test_remote_confirmation_063 import _WS, USER, _FakeDB, _orch, _tc
from tests.test_remote_confirmation_063 import real_orch as _real_orch  # noqa: F401

@pytest.fixture
def real_orch(_real_orch):  # noqa: F811
    _real_orch.tool_permissions.get_tool_scope_map.return_value.update({
        name: entry["scope"] for name, entry in TOOLS.items()})
    return _real_orch
gaia_server = _gaia_server


@pytest.fixture(autouse=True)
def owned_machine(monkeypatch):
    from orchestrator import remote_machines
    monkeypatch.setattr(remote_machines, 'resolve_machine', lambda db, owner, ref: {'owner_user_id': owner})


@pytest.mark.parametrize('tool', ['gaiakeep_core_whoami', 'gaiakeep_core_repair'])
@pytest.mark.parametrize('failure', ['foreign', 'missing', 'repository-failure'])
def test_unavailable_machine_fails_closed_for_reads_and_mutations(monkeypatch, tool, failure):
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
    args = {'machine_id': 'unavailable', 'params': {}}
    result = rc.evaluate(orch, object(), 'gaiakeep-1', tool, args, 'chat', 'caller')
    assert result and result[1][0]['type'] == 'alert'
    assert 'private repository detail' not in str(result)
    assert calls == [('caller', 'unavailable')] and db.rows == {}


def test_every_mutation_requires_approval():
    for name in TOOLS:
        assert rc.classification_for(name, 'gaiakeep-1') == ('always' if is_mutation(name) else 'never')
        assert taint.is_sink('gaiakeep-1', name) is is_mutation(name)
    assert rc.classification_for('unreviewed', 'gaiakeep-1') == 'always'
    assert taint.classify_source('gaiakeep-1', None) == taint.UNTRUSTED


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


def test_core_request_identity_preparation_preserves_parameters():
    db = _FakeDB()
    orch = _orch(db)
    ws = object()
    orch.ui_sessions[ws] = {'sub': 'owner'}
    original = {'wait_ms': 10}
    args = {'machine_id': 'mine', 'params': original}
    assert rc.evaluate(orch, ws, 'gaiakeep-1', 'gaiakeep_core_repair', args, 'chat', 'owner')
    assert original == {'wait_ms': 10}
    assert args['params'] == dict(original, request_id=args['params']['request_id'])
    assert not db.rows


@pytest.mark.parametrize('strategy', ['ingest', 'have'])
def test_upload_refusal_preserves_the_selected_request_identity_mode(strategy):
    db = _FakeDB()
    orch = _orch(db)
    ws = object()
    orch.ui_sessions[ws] = {'sub': 'owner'}
    args = {'machine_id': 'mine', 'collection_id': 'scratch', 'path': 'sample.txt',
            'data_base64': 'b2s=', 'strategy': strategy}
    if strategy == 'ingest':
        args['request_id'] = 'retained_request_identity'
    before = json.loads(json.dumps(args))
    assert rc.evaluate(orch, ws, 'gaiakeep-1', 'gaiakeep_upload_file', args, 'chat', 'owner')
    assert args == before
    assert not db.rows


@pytest.mark.asyncio
async def test_normal_dispatch_requires_approval_and_binds_owner(real_orch, monkeypatch):
    from orchestrator import remote_machines

    observed = []

    def lookup(_source, owner, machine):
        observed.append((owner, machine))
        return {"owner_user_id": owner}

    monkeypatch.setattr(remote_machines, "resolve_machine", lookup)
    tool = 'gaiakeep_core_auditack'
    tool_call = _tc(tool, {'machine_id': 'mine', 'params': {'reason': 'requested'}, 'user_id': 'attacker'})
    real_orch._execute_with_retry_audited = AsyncMock()
    socket = _WS()
    real_orch.ui_sessions[socket] = {'sub': USER}
    response = await real_orch.execute_single_tool(socket, tool_call, {tool: 'gaiakeep-1'}, 'chat', user_id=USER)
    assert response.error and response.error['retryable'] is False
    assert response.result is None and response.ui_components is None
    assert 'attended authorization' in response.error['message']
    assert observed == [(USER, 'mine')]
    assert not real_orch.proposal_storage.rows
    real_orch.tool_permissions.is_tool_allowed.assert_called_once_with(USER, 'gaiakeep-1', tool)
    real_orch._execute_with_retry_audited.assert_not_awaited()
    real_orch.send_ui_render.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['invalid-arguments', 'foreign-machine', 'machine-lookup-failure',
                                 'unattended', 'stale-approval', 'changed-approval', 'permission'])
async def test_normal_gaia_denial_remains_an_error_without_pending_result(real_orch, monkeypatch, case):
    from orchestrator import remote_machines
    tool = 'gaiakeep_core_auditack'
    args = {'machine_id': 'mine', 'params': {'reason': 'requested'}, 'user_id': USER}
    socket = _WS()
    if case == 'invalid-arguments':
        args['params'] = {'principal': 'attacker'}
    elif case == 'foreign-machine':
        monkeypatch.setattr(remote_machines, 'resolve_machine', lambda *a: None)
    elif case == 'machine-lookup-failure':
        def fail(*a):
            raise OSError('private repository detail')
        monkeypatch.setattr(remote_machines, 'resolve_machine', fail)
    elif case == 'unattended':
        socket.machine_claims = {'machine_class': 'scheduler'}
    elif case == 'stale-approval':
        args['_remote_op_proposal_id'] = 'missing-proposal'
    elif case == 'changed-approval':
        real_orch.proposal_storage.rows['approved'] = {'owner_user_id': USER, 'status': 'approved'}
        args['params'] = {'reason': 'changed'}
        args['_remote_op_proposal_id'] = 'approved'
    else:
        real_orch.tool_permissions.is_tool_allowed.return_value = False
    before = copy.deepcopy(real_orch.proposal_storage.rows)
    real_orch._execute_with_retry_audited = AsyncMock()
    tool_call = _tc(tool, args)
    response = await real_orch.execute_single_tool(socket, tool_call, {tool: 'gaiakeep-1'}, 'chat', user_id=USER)
    assert response.error and response.error['retryable'] is False
    assert response.result is None and response.ui_components is None
    assert 'private repository detail' not in json.dumps(response.error)
    assert real_orch.proposal_storage.rows == before
    real_orch.send_ui_render.assert_awaited_once()
    assert all(component['type'] == 'alert'
               for call in real_orch.send_ui_render.await_args_list for component in call.args[1])
    real_orch._execute_with_retry_audited.assert_not_awaited()


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
async def test_permitted_read_still_obeys_product_policy(real_orch, monkeypatch):
    from orchestrator import policy
    tool = 'gaiakeep_core_whoami'
    args = {'machine_id': 'mine', 'params': {}}
    real_orch._policy_roles = lambda ws: []
    monkeypatch.setattr(policy, 'policy_enabled', lambda: True)
    monkeypatch.setattr(policy, 'load_rules', lambda **kwargs: [])
    monkeypatch.setattr(policy, 'evaluate_policy', lambda *a, **kwargs: policy.PolicyDecision(effect=policy.DENY, reason='policy denial'))
    response = await real_orch.execute_single_tool(
        _WS(), _tc(tool, args),
        {tool: 'gaiakeep-1'}, 'chat', user_id=USER)
    assert response.error['message'] == 'policy denial'
    assert not real_orch.proposal_storage.rows


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
