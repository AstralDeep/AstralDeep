"""Exercises GaiaKeep approval through the actual shared gate and existing Plane proposal test repositories."""

import json
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from agents.gaiakeep.catalog import TOOLS, is_mutation
from agents.gaiakeep.tests.test_dispatch import server as _gaia_server
from orchestrator import remote_confirmation as rc
from orchestrator import taint

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
